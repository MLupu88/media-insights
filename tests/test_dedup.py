from datetime import date, datetime, timedelta, timezone

from app.models.article import RetailerConfidence, RetailerReviewStatus
from app.services.dedup import canonical_sort_key, order_by_canonical, reconcile_fingerprint_group
from app.services.excel_parser import compute_fingerprint
from app.services.imports import _load_seen_fingerprints


def test_reconcile_fingerprint_group_picks_earliest_created_at(project_factory, article_factory):
    project = project_factory()
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    fp = compute_fingerprint("Auchan", "Shared", "Source", date(2026, 4, 1), None)

    late = article_factory(
        project, count=1, retailer="Auchan", title="Shared", source="Source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=base + timedelta(seconds=10),
    )[0]
    earliest = article_factory(
        project, count=1, retailer="Auchan", title="Shared", source="Source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=base,
    )[0]
    middle = article_factory(
        project, count=1, retailer="Auchan", title="Shared", source="Source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=base + timedelta(seconds=5),
    )[0]

    canonical = reconcile_fingerprint_group([late, earliest, middle])

    assert canonical.id == earliest.id
    assert earliest.is_duplicate is False
    assert earliest.duplicate_of_article_id is None
    assert late.is_duplicate is True and late.duplicate_of_article_id == earliest.id
    assert middle.is_duplicate is True and middle.duplicate_of_article_id == earliest.id


def test_reconcile_fingerprint_group_ties_break_by_id(project_factory, article_factory):
    project = project_factory()
    same_time = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    fp = compute_fingerprint("Auchan", "Shared", "Source", date(2026, 4, 1), None)

    rows = article_factory(
        project, count=1, retailer="Auchan", title="Shared", source="Source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=same_time,
    ) + article_factory(
        project, count=1, retailer="Auchan", title="Shared", source="Source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=same_time,
    )
    expected_canonical = min(rows, key=lambda a: a.id)

    canonical = reconcile_fingerprint_group(rows)

    assert canonical.id == expected_canonical.id


def test_canonical_sort_key_matches_order_by_canonical_direction(project_factory, article_factory):
    """A cheap sanity check that the Python sort key and the SQL ORDER BY
    express the same direction (ascending created_at, then id) — not
    just that each is internally consistent.
    """
    project = project_factory()
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    a = article_factory(project, count=1, retailer="Auchan", created_at=base)[0]
    b = article_factory(project, count=1, retailer="Auchan", created_at=base + timedelta(seconds=1))[0]

    assert canonical_sort_key(a) < canonical_sort_key(b)


def test_import_time_and_correction_time_reconciliation_choose_the_same_canonical(
    db_session, project_factory, article_factory
):
    """The core proof this phase requires: import-time's own
    canonical-selection query (`_load_seen_fingerprints`, via
    `order_by_canonical`) and correction-time's shared primitive
    (`reconcile_fingerprint_group`) agree on which row is canonical for
    the exact same set of rows.
    """
    project = project_factory()
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    fp = compute_fingerprint("Auchan", "Same title", "Same source", date(2026, 4, 1), None)

    a1 = article_factory(
        project, count=1, retailer="Auchan", title="Same title", source="Same source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=base + timedelta(seconds=10),
    )[0]
    a2 = article_factory(
        project, count=1, retailer="Auchan", title="Same title", source="Same source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=base,  # earliest
    )[0]
    a3 = article_factory(
        project, count=1, retailer="Auchan", title="Same title", source="Same source",
        publication_date=date(2026, 4, 1), fingerprint=fp, created_at=base + timedelta(seconds=20),
    )[0]

    # Import-time's own canonical-selection query agrees a2 (earliest) wins.
    seen = _load_seen_fingerprints(db_session, project.id)
    assert seen[fp] == a2.id

    # Correction-time's shared primitive agrees too, on the same rows.
    canonical = reconcile_fingerprint_group([a1, a2, a3])
    assert canonical.id == a2.id
    assert a1.is_duplicate is True and a1.duplicate_of_article_id == a2.id
    assert a3.is_duplicate is True and a3.duplicate_of_article_id == a2.id
    assert a2.is_duplicate is False


def test_reconcile_fingerprint_group_single_row_is_its_own_canonical(project_factory, article_factory):
    project = project_factory()
    solo = article_factory(project, count=1, retailer="Auchan")[0]

    canonical = reconcile_fingerprint_group([solo])

    assert canonical.id == solo.id
    assert solo.is_duplicate is False
    assert solo.duplicate_of_article_id is None
