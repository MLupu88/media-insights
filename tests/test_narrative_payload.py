import uuid

from app.services.analytics import AnalyticsFilters
from app.services.narrative_payload import (
    EVIDENCE_ARTICLES_PER_ENTITY,
    build_comparison_snapshot,
    build_project_snapshot,
    compute_input_hash,
)


def test_build_project_snapshot_shape(db_session, project_factory, article_factory, classification_factory):
    project = project_factory()
    articles = article_factory(project, count=3, retailer="Auchan")
    for article in articles:
        classification_factory(article, primary_topic="store_expansion")

    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    assert snapshot["scope"] == "project"
    assert snapshot["data"]["kpis"]["unique_valid_articles"] == 3
    assert isinstance(snapshot["evidence_pool"], list)
    assert len(snapshot["evidence_pool"]) > 0


def test_project_snapshot_is_json_safe(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    assert isinstance(snapshot["data"]["project_id"], str)
    for item in snapshot["evidence_pool"]:
        assert isinstance(item["article_id"], str)
        uuid.UUID(item["article_id"])  # does not raise


def test_evidence_pool_bounded_per_entity(db_session, project_factory, article_factory):
    # A fixed `source` collapses the brand and publication dimensions onto
    # the same 10 articles, so the pool bound is actually exercised instead
    # of being masked by each article having a distinct publication.
    project = project_factory()
    article_factory(project, count=10, retailer="Auchan", source="Ziarul Financiar")

    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    auchan_articles = [item for item in snapshot["evidence_pool"] if item["brand"] == "Auchan"]
    assert len(auchan_articles) <= EVIDENCE_ARTICLES_PER_ENTITY


def test_build_comparison_snapshot_shape(db_session, project_factory, article_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=2, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    snapshot = build_comparison_snapshot(db_session, [a.id], [b.id], AnalyticsFilters())

    assert snapshot["scope"] == "comparison"
    assert snapshot["data"]["baseline"]["kpis"]["unique_valid_articles"] == 2
    assert snapshot["data"]["comparison"]["kpis"]["unique_valid_articles"] == 1
    assert "deltas" in snapshot["data"]


def test_comparison_evidence_pool_deduplicated_across_periods(
    db_session, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    snapshot = build_comparison_snapshot(db_session, [a.id], [b.id], AnalyticsFilters())
    ids = [item["article_id"] for item in snapshot["evidence_pool"]]
    assert len(ids) == len(set(ids))


def test_compute_input_hash_stable_for_identical_input(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    hash_a = compute_input_hash(snapshot, "ro", ["executive_summary"], "narrative-v1", AnalyticsFilters())
    hash_b = compute_input_hash(snapshot, "ro", ["executive_summary"], "narrative-v1", AnalyticsFilters())
    assert hash_a == hash_b


def test_compute_input_hash_changes_with_narrative_types(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    hash_a = compute_input_hash(snapshot, "ro", ["executive_summary"], "narrative-v1", AnalyticsFilters())
    hash_b = compute_input_hash(snapshot, "ro", ["key_findings"], "narrative-v1", AnalyticsFilters())
    assert hash_a != hash_b


def test_compute_input_hash_is_narrative_type_order_independent(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    hash_a = compute_input_hash(
        snapshot, "ro", ["executive_summary", "key_findings"], "narrative-v1", AnalyticsFilters()
    )
    hash_b = compute_input_hash(
        snapshot, "ro", ["key_findings", "executive_summary"], "narrative-v1", AnalyticsFilters()
    )
    assert hash_a == hash_b


# --- filter_identity: explicit canonical filter identity in the hash --------


def test_compute_input_hash_singular_and_plural_one_brand_are_identical(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    hash_a = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1", AnalyticsFilters(brand="Auchan")
    )
    hash_b = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1", AnalyticsFilters(brands=("Auchan",))
    )
    assert hash_a == hash_b


def test_compute_input_hash_brand_order_does_not_matter(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    hash_a = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1",
        AnalyticsFilters(brands=("Carrefour", "Auchan")),
    )
    hash_b = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1",
        AnalyticsFilters(brands=("Auchan", "Carrefour")),
    )
    assert hash_a == hash_b


def test_compute_input_hash_source_file_order_does_not_matter(
    db_session, project_factory, article_factory, uploaded_file_factory
):
    import uuid

    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())
    id_a, id_b = uuid.uuid4(), uuid.uuid4()

    hash_a = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1",
        AnalyticsFilters(uploaded_file_ids=(id_a, id_b)),
    )
    hash_b = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1",
        AnalyticsFilters(uploaded_file_ids=(id_b, id_a)),
    )
    assert hash_a == hash_b


def test_compute_input_hash_needs_review_toggle_changes_the_hash(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    snapshot = build_project_snapshot(db_session, project, AnalyticsFilters())

    hash_a = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1",
        AnalyticsFilters(include_needs_review=False),
    )
    hash_b = compute_input_hash(
        snapshot, "ro", ["executive_summary"], "narrative-v1",
        AnalyticsFilters(include_needs_review=True),
    )
    assert hash_a != hash_b


def test_compute_input_hash_distinct_source_file_sets_never_collide(
    db_session, project_factory, article_factory
):
    """Two different filter sets that happen to produce identical analytics
    output (e.g. two different single-source-file filters that each match
    zero articles) must still hash differently -- the whole point of an
    explicit filter_identity rather than relying only on the snapshot's
    own embedded data.
    """
    import uuid

    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    filters_a = AnalyticsFilters(uploaded_file_ids=(id_a,))
    filters_b = AnalyticsFilters(uploaded_file_ids=(id_b,))
    snapshot_a = build_project_snapshot(db_session, project, filters_a)
    snapshot_b = build_project_snapshot(db_session, project, filters_b)

    hash_a = compute_input_hash(snapshot_a, "ro", ["executive_summary"], "narrative-v1", filters_a)
    hash_b = compute_input_hash(snapshot_b, "ro", ["executive_summary"], "narrative-v1", filters_b)
    assert hash_a != hash_b
