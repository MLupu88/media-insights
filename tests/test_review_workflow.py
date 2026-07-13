import threading
import time
from datetime import date

import pytest

from app.database import SessionLocal
from app.models.article import Article, RetailerConfidence, RetailerReviewStatus
from app.models.project import Project
from app.services.dedup import lock_project_for_dedup
from app.services.excel_parser import compute_fingerprint
from app.services.review import reassign_article_brand


def _needs_review_article(article_factory, project, uploaded_file=None, **overrides):
    defaults = dict(
        count=1,
        retailer="unknown",
        title="Some story",
        source="Some source",
        publication_date=date(2026, 4, 1),
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
        retailer_confidence=RetailerConfidence.NEEDS_REVIEW,
        retailer_raw_value="Local Shop XYZ",
    )
    defaults.update(overrides)
    if uploaded_file is not None:
        defaults["uploaded_file_id"] = uploaded_file.id
    return article_factory(project, **defaults)[0]


# --- Review tab scoping ------------------------------------------------------


def test_review_tab_lists_needs_review_articles_grouped_by_file(
    authenticated_client, db_session, project_factory, article_factory, uploaded_file_factory
):
    project = project_factory()
    uf = uploaded_file_factory(project, original_filename="Ambiguous.xlsx")
    _needs_review_article(article_factory, project, uploaded_file=uf, title="Row A")
    _needs_review_article(article_factory, project, uploaded_file=uf, title="Row B")

    response = authenticated_client.get(f"/projects/{project.id}?tab=review")
    assert response.status_code == 200
    assert "Ambiguous.xlsx" in response.text
    assert "Row A" in response.text
    assert "Row B" in response.text
    assert "Review (2)" in response.text


def test_review_tab_never_lists_another_projects_rows(
    authenticated_client, db_session, project_factory, article_factory, uploaded_file_factory
):
    project_a = project_factory(name="Review Scope A")
    project_b = project_factory(name="Review Scope B")
    uf_b = uploaded_file_factory(project_b, original_filename="OtherProject.xlsx")
    _needs_review_article(article_factory, project_b, uploaded_file=uf_b, title="Belongs to B")

    response = authenticated_client.get(f"/projects/{project_a.id}?tab=review")
    assert response.status_code == 200
    assert "Belongs to B" not in response.text
    assert "OtherProject.xlsx" not in response.text
    assert "No rows need review" in response.text


def test_review_badge_absent_when_nothing_needs_review(authenticated_client, project_factory):
    project = project_factory()
    response = authenticated_client.get(f"/projects/{project.id}?tab=overview")
    assert response.status_code == 200
    assert "Review (" not in response.text


# --- single correction ---------------------------------------------------


def test_single_correction_assigns_brand_and_confirms_status(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article = _needs_review_article(article_factory, project)

    response = authenticated_client.post(
        f"/projects/{project.id}/articles/{article.id}/assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=review"

    db_session.refresh(article)
    assert article.retailer == "Auchan"
    assert article.retailer_review_status == RetailerReviewStatus.CONFIRMED
    assert article.retailer_confidence == RetailerConfidence.MANUAL_CORRECTION


def test_single_correction_updates_uploaded_file_and_project_counters(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article = _needs_review_article(article_factory, project)

    authenticated_client.post(
        f"/projects/{project.id}/articles/{article.id}/assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )

    db_session.refresh(project)
    assert project.valid_rows == 1


def test_correction_on_legacy_file_with_no_batch_recomputes_counters_without_crashing(
    authenticated_client, db_session, project_factory, article_factory, uploaded_file_factory
):
    """`import_batch_id=NULL` (a file that predates Phase B's ImportBatch
    table) must not break counter recomputation — the batch-level step is
    simply skipped for that file, UploadedFile/Project totals still update.
    """
    project = project_factory()
    uploaded_file = uploaded_file_factory(project, import_batch_id=None)
    article = _needs_review_article(article_factory, project, uploaded_file=uploaded_file)

    response = authenticated_client.post(
        f"/projects/{project.id}/articles/{article.id}/assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_session.refresh(project)
    assert project.valid_rows == 1


def test_bulk_correction_with_no_rows_selected_is_rejected(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.post(
        f"/projects/{project.id}/articles/bulk-assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 422


# --- invalid input -----------------------------------------------------------


def test_single_correction_rejects_unsupported_brand(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article = _needs_review_article(article_factory, project)

    response = authenticated_client.post(
        f"/projects/{project.id}/articles/{article.id}/assign-brand",
        data={"brand": "Not A Real Brand"},
        follow_redirects=False,
    )
    assert response.status_code == 422

    db_session.refresh(article)
    assert article.retailer == "unknown"
    assert article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW


def test_bulk_correction_rejects_unsupported_brand_and_touches_nothing(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    a1 = _needs_review_article(article_factory, project, title="A1")
    a2 = _needs_review_article(article_factory, project, title="A2")

    response = authenticated_client.post(
        f"/projects/{project.id}/articles/bulk-assign-brand",
        data={"article_ids": [str(a1.id), str(a2.id)], "brand": "Not A Real Brand"},
        follow_redirects=False,
    )
    assert response.status_code == 422

    db_session.refresh(a1)
    db_session.refresh(a2)
    assert a1.retailer == "unknown"
    assert a2.retailer == "unknown"


# --- cross-project rejection -------------------------------------------------


def test_single_correction_rejects_article_from_another_project(
    authenticated_client, db_session, project_factory, article_factory
):
    owner_project = project_factory(name="Owner Project")
    other_project = project_factory(name="Other Project")
    article = _needs_review_article(article_factory, owner_project)

    response = authenticated_client.post(
        f"/projects/{other_project.id}/articles/{article.id}/assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 404

    db_session.refresh(article)
    assert article.retailer == "unknown"


def test_bulk_correction_rejects_any_article_from_another_project(
    authenticated_client, db_session, project_factory, article_factory
):
    owner_project = project_factory(name="Bulk Owner")
    other_project = project_factory(name="Bulk Other")
    own_article = _needs_review_article(article_factory, owner_project, title="Own")
    foreign_article = _needs_review_article(article_factory, other_project, title="Foreign")

    response = authenticated_client.post(
        f"/projects/{owner_project.id}/articles/bulk-assign-brand",
        data={"article_ids": [str(own_article.id), str(foreign_article.id)], "brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 404

    # All-or-nothing: even the article that DID belong to this project
    # must be untouched, since the whole bulk action rolled back.
    db_session.refresh(own_article)
    assert own_article.retailer == "unknown"


# --- dedup cascade: becomes a duplicate of an existing canonical -------------


def test_correction_makes_row_a_duplicate_of_existing_canonical(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    fp = compute_fingerprint("Auchan", "Shared Title", "Shared Source", date(2026, 4, 1), None)

    canonical = article_factory(
        project, count=1, retailer="Auchan", title="Shared Title", source="Shared Source",
        publication_date=date(2026, 4, 1), fingerprint=fp, is_duplicate=False,
    )[0]
    ambiguous = _needs_review_article(
        article_factory, project, title="Shared Title", source="Shared Source",
        publication_date=date(2026, 4, 1), fingerprint="fp-ambiguous",
    )

    authenticated_client.post(
        f"/projects/{project.id}/articles/{ambiguous.id}/assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )

    db_session.refresh(ambiguous)
    db_session.refresh(canonical)
    assert ambiguous.is_duplicate is True
    assert ambiguous.duplicate_of_article_id == canonical.id
    assert canonical.is_duplicate is False


# --- dedup cascade: previously-canonical row leaves, promotes a new one -----


def test_correction_promotes_new_canonical_and_repoints_all_siblings(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    fp = compute_fingerprint("Carrefour", "Other Title", "Other Source", date(2026, 4, 2), None)

    old_canonical = article_factory(
        project, count=1, retailer="Carrefour", title="Other Title", source="Other Source",
        publication_date=date(2026, 4, 2), fingerprint=fp, is_duplicate=False,
    )[0]
    dup1 = article_factory(
        project, count=1, retailer="Carrefour", title="Other Title", source="Other Source",
        publication_date=date(2026, 4, 2), fingerprint=fp, is_duplicate=True,
        duplicate_of_article_id=old_canonical.id,
    )[0]
    dup2 = article_factory(
        project, count=1, retailer="Carrefour", title="Other Title", source="Other Source",
        publication_date=date(2026, 4, 2), fingerprint=fp, is_duplicate=True,
        duplicate_of_article_id=old_canonical.id,
    )[0]

    authenticated_client.post(
        f"/projects/{project.id}/articles/{old_canonical.id}/assign-brand",
        data={"brand": "Lidl"},
        follow_redirects=False,
    )

    db_session.refresh(old_canonical)
    db_session.refresh(dup1)
    db_session.refresh(dup2)

    assert old_canonical.retailer == "Lidl"
    assert old_canonical.is_duplicate is False  # canonical for its NEW fingerprint

    # Exactly one of the former duplicates is promoted; the other is
    # re-pointed at the promoted one, not left pointing at the old id.
    promoted = [a for a in (dup1, dup2) if not a.is_duplicate]
    still_duplicate = [a for a in (dup1, dup2) if a.is_duplicate]
    assert len(promoted) == 1
    assert len(still_duplicate) == 1
    assert still_duplicate[0].duplicate_of_article_id == promoted[0].id


# --- dedup cascade: previously-duplicate row becomes unique -----------------


def test_correction_makes_a_previously_duplicate_row_unique(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    fp = compute_fingerprint("Carrefour", "Title", "Source", date(2026, 4, 2), None)

    canonical = article_factory(
        project, count=1, retailer="Carrefour", title="Title", source="Source",
        publication_date=date(2026, 4, 2), fingerprint=fp, is_duplicate=False,
    )[0]
    duplicate = article_factory(
        project, count=1, retailer="Carrefour", title="Title", source="Source",
        publication_date=date(2026, 4, 2), fingerprint=fp, is_duplicate=True,
        duplicate_of_article_id=canonical.id,
    )[0]

    authenticated_client.post(
        f"/projects/{project.id}/articles/{duplicate.id}/assign-brand",
        data={"brand": "Lidl"},
        follow_redirects=False,
    )

    db_session.refresh(duplicate)
    db_session.refresh(canonical)
    assert duplicate.retailer == "Lidl"
    assert duplicate.is_duplicate is False
    assert duplicate.duplicate_of_article_id is None
    # The original canonical is completely unaffected — it never had any
    # other duplicates besides the one that just left.
    assert canonical.is_duplicate is False


# --- bulk correction ----------------------------------------------------


def test_bulk_correction_applies_brand_to_every_selected_row(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    rows = [
        _needs_review_article(article_factory, project, title=f"Row {i}", fingerprint=f"fp-{i}")
        for i in range(3)
    ]

    response = authenticated_client.post(
        f"/projects/{project.id}/articles/bulk-assign-brand",
        data={"article_ids": [str(r.id) for r in rows], "brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    for row in rows:
        db_session.refresh(row)
        assert row.retailer == "Auchan"
        assert row.retailer_review_status == RetailerReviewStatus.CONFIRMED
        assert row.retailer_confidence == RetailerConfidence.MANUAL_CORRECTION


# --- transaction rollback on failure -----------------------------------------


def test_failure_during_counter_recompute_rolls_back_the_whole_correction(
    authenticated_client, db_session, project_factory, article_factory, monkeypatch
):
    project = project_factory()
    article = _needs_review_article(article_factory, project)

    import app.api.review as review_api

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure during counter recompute")

    monkeypatch.setattr(review_api, "recompute_counters_no_commit", _boom)

    with pytest.raises(RuntimeError):
        authenticated_client.post(
            f"/projects/{project.id}/articles/{article.id}/assign-brand",
            data={"brand": "Auchan"},
            follow_redirects=False,
        )

    db_session.refresh(article)
    assert article.retailer == "unknown"
    assert article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW


# --- concurrency, where practical --------------------------------------------


def test_project_lock_serializes_concurrent_dedup_operations(project_factory):
    project = project_factory()
    project_id = project.id

    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        lock_project_for_dedup(session_a, project_id)

        b_acquired = threading.Event()

        def _try_lock_b():
            lock_project_for_dedup(session_b, project_id)
            b_acquired.set()

        thread = threading.Thread(target=_try_lock_b)
        thread.start()

        time.sleep(0.3)
        assert not b_acquired.is_set(), "session B should still be blocked while A holds the project lock"

        session_a.rollback()
        thread.join(timeout=5)
        assert b_acquired.is_set(), "session B should acquire the lock once session A releases it"
    finally:
        session_b.rollback()
        session_a.close()
        session_b.close()


def test_concurrent_corrections_to_the_same_new_fingerprint_never_produce_two_canonicals(
    db_session, project_factory, article_factory
):
    project = project_factory()
    project_id = project.id
    x = _needs_review_article(article_factory, project, title="Same", source="Same", fingerprint="fp-x")
    y = _needs_review_article(article_factory, project, title="Same", source="Same", fingerprint="fp-y")
    x_id, y_id = x.id, y.id

    session_a = SessionLocal()
    session_b = SessionLocal()
    results: dict[str, str] = {}

    def _correct(session, article_id, key):
        try:
            reassign_article_brand(session, project_id, article_id, "Auchan")
            session.commit()
            results[key] = "ok"
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            session.rollback()
            results[key] = f"error: {exc}"

    t1 = threading.Thread(target=_correct, args=(session_a, x_id, "x"))
    t2 = threading.Thread(target=_correct, args=(session_b, y_id, "y"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    session_a.close()
    session_b.close()

    db_session.refresh(x)
    db_session.refresh(y)

    canonicals = [a for a in (x, y) if not a.is_duplicate]
    duplicates = [a for a in (x, y) if a.is_duplicate]
    assert len(canonicals) == 1, f"expected exactly one canonical, got {canonicals} (results={results})"
    assert len(duplicates) == 1
    assert duplicates[0].duplicate_of_article_id == canonicals[0].id
