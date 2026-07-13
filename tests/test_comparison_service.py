import uuid

import pytest

from app.models.article import ImportStatus
from app.services.analytics import AnalyticsFilters
from app.services.comparison import (
    ComparisonServiceError,
    _safe_pct_delta,
    derive_period_label,
    get_period_comparison,
)


# ---------------------------------------------------------------------------
# Derived-period labeling
# ---------------------------------------------------------------------------


def test_derive_label_single_quarter(project_factory):
    project = project_factory(quarter="2026-Q2")
    assert derive_period_label([project]) == "Q2 2026"


def test_derive_label_h1(project_factory):
    q1 = project_factory(name="P1", quarter="2026-Q1")
    q2 = project_factory(name="P2", quarter="2026-Q2")
    assert derive_period_label([q1, q2]) == "H1 2026"
    assert derive_period_label([q2, q1]) == "H1 2026"  # order-independent


def test_derive_label_h2(project_factory):
    q3 = project_factory(name="P3", quarter="2026-Q3")
    q4 = project_factory(name="P4", quarter="2026-Q4")
    assert derive_period_label([q3, q4]) == "H2 2026"


def test_derive_label_full_year(project_factory):
    quarters = [project_factory(name=f"P{i}", quarter=f"2026-Q{i}") for i in range(1, 5)]
    assert derive_period_label(quarters) == "FY 2026"


def test_derive_label_generic_fallback_for_odd_combination(project_factory):
    q1_2025 = project_factory(name="A", quarter="2025-Q4")
    q1_2026 = project_factory(name="B", quarter="2026-Q1")
    q2_2026 = project_factory(name="C", quarter="2026-Q2")
    label = derive_period_label([q1_2025, q1_2026, q2_2026])
    assert "3 projects" in label
    assert "2025-Q4" in label and "2026-Q1" in label and "2026-Q2" in label


# ---------------------------------------------------------------------------
# Q/Q, YoY, H1, H2, FY comparisons end-to-end
# ---------------------------------------------------------------------------


def test_quarter_over_quarter_comparison(db_session, project_factory, article_factory):
    q1 = project_factory(name="Q1 Project", quarter="2026-Q1")
    q2 = project_factory(name="Q2 Project", quarter="2026-Q2")
    article_factory(q1, count=2, retailer="Auchan", audience=1000.0)
    article_factory(q2, count=3, retailer="Auchan", audience=1000.0)

    result = get_period_comparison(db_session, [q1.id], [q2.id], AnalyticsFilters())

    assert result["baseline"]["label"] == "Q1 2026"
    assert result["comparison"]["label"] == "Q2 2026"
    assert result["deltas"]["kpis"]["unique_valid_articles"]["baseline"] == 2
    assert result["deltas"]["kpis"]["unique_valid_articles"]["comparison"] == 3
    assert result["deltas"]["kpis"]["unique_valid_articles"]["absolute_delta"] == 1
    assert result["deltas"]["kpis"]["unique_valid_articles"]["percentage_delta"] == 50.0


def test_year_over_year_comparison(db_session, project_factory, article_factory):
    last_year = project_factory(name="Q2 2025", quarter="2025-Q2")
    this_year = project_factory(name="Q2 2026", quarter="2026-Q2")
    article_factory(last_year, count=4, retailer="Auchan")
    article_factory(this_year, count=6, retailer="Auchan")

    result = get_period_comparison(db_session, [last_year.id], [this_year.id], AnalyticsFilters())

    assert result["baseline"]["label"] == "Q2 2025"
    assert result["comparison"]["label"] == "Q2 2026"
    assert result["deltas"]["kpis"]["unique_valid_articles"]["baseline"] == 4
    assert result["deltas"]["kpis"]["unique_valid_articles"]["comparison"] == 6


def test_h1_derived_from_q1_plus_q2(db_session, project_factory, article_factory):
    q1 = project_factory(name="Q1", quarter="2026-Q1")
    q2 = project_factory(name="Q2", quarter="2026-Q2")
    q3 = project_factory(name="Q3", quarter="2026-Q3")
    article_factory(q1, count=2, retailer="Auchan")
    article_factory(q2, count=3, retailer="Auchan")
    article_factory(q3, count=10, retailer="Auchan")  # must not leak into H1

    result = get_period_comparison(db_session, [q1.id, q2.id], [q3.id], AnalyticsFilters())

    assert result["baseline"]["label"] == "H1 2026"
    assert result["baseline"]["kpis"]["unique_valid_articles"] == 5
    assert result["baseline"]["project_count"] == 2


def test_h2_derived_from_q3_plus_q4(db_session, project_factory, article_factory):
    q3 = project_factory(name="Q3", quarter="2026-Q3")
    q4 = project_factory(name="Q4", quarter="2026-Q4")
    article_factory(q3, count=2, retailer="Auchan")
    article_factory(q4, count=4, retailer="Auchan")

    result = get_period_comparison(db_session, [q3.id, q4.id], [q3.id], AnalyticsFilters())

    assert result["baseline"]["label"] == "H2 2026"
    assert result["baseline"]["kpis"]["unique_valid_articles"] == 6


def test_full_year_derived_from_all_four_quarters(db_session, project_factory, article_factory):
    quarters = []
    for i in range(1, 5):
        project = project_factory(name=f"Q{i}", quarter=f"2026-Q{i}")
        article_factory(project, count=i, retailer="Auchan")
        quarters.append(project)

    result = get_period_comparison(
        db_session, [p.id for p in quarters], [quarters[0].id], AnalyticsFilters()
    )

    assert result["baseline"]["label"] == "FY 2026"
    assert result["baseline"]["kpis"]["unique_valid_articles"] == 1 + 2 + 3 + 4
    assert result["baseline"]["project_count"] == 4


# ---------------------------------------------------------------------------
# Missing-project / empty-selection behavior
# ---------------------------------------------------------------------------


def test_empty_baseline_selection_rejected(db_session, project_factory):
    project = project_factory()
    with pytest.raises(ComparisonServiceError) as exc_info:
        get_period_comparison(db_session, [], [project.id], AnalyticsFilters())
    assert exc_info.value.status_code == 422
    assert "Baseline" in exc_info.value.message


def test_empty_comparison_selection_rejected(db_session, project_factory):
    project = project_factory()
    with pytest.raises(ComparisonServiceError) as exc_info:
        get_period_comparison(db_session, [project.id], [], AnalyticsFilters())
    assert exc_info.value.status_code == 422
    assert "Comparison" in exc_info.value.message


def test_unknown_project_id_rejected(db_session, project_factory):
    project = project_factory()
    missing_id = uuid.uuid4()
    with pytest.raises(ComparisonServiceError) as exc_info:
        get_period_comparison(db_session, [project.id], [missing_id], AnalyticsFilters())
    assert exc_info.value.status_code == 404
    assert str(missing_id) in exc_info.value.message


def test_duplicate_project_id_in_selection_is_deduplicated(
    db_session, project_factory, article_factory
):
    project = project_factory(quarter="2026-Q1")
    other = project_factory(name="Other", quarter="2026-Q2")
    article_factory(project, count=3, retailer="Auchan")
    article_factory(other, count=1, retailer="Auchan")

    # Selecting the same project twice on one side must not double-count it.
    result = get_period_comparison(
        db_session, [project.id, project.id], [other.id], AnalyticsFilters()
    )
    assert result["baseline"]["kpis"]["unique_valid_articles"] == 3
    assert result["baseline"]["project_count"] == 1


# ---------------------------------------------------------------------------
# Zero-denominator / percentage vs percentage-point distinction
# ---------------------------------------------------------------------------


def test_safe_pct_delta_zero_baseline_positive_comparison_is_undefined():
    assert _safe_pct_delta(0, 10) is None


def test_safe_pct_delta_zero_to_zero_is_zero():
    assert _safe_pct_delta(0, 0) == 0.0


def test_safe_pct_delta_normal_case():
    assert _safe_pct_delta(100, 150) == 50.0
    assert _safe_pct_delta(100, 50) == -50.0


def test_kpi_delta_undefined_when_baseline_is_zero(db_session, project_factory, article_factory):
    empty_project = project_factory(name="Empty", quarter="2026-Q1")
    populated = project_factory(name="Populated", quarter="2026-Q2")
    article_factory(populated, count=5, retailer="Auchan")

    result = get_period_comparison(
        db_session, [empty_project.id], [populated.id], AnalyticsFilters()
    )

    kpi = result["deltas"]["kpis"]["unique_valid_articles"]
    assert kpi["baseline"] == 0
    assert kpi["comparison"] == 5
    assert kpi["absolute_delta"] == 5
    assert kpi["percentage_delta"] is None  # undefined, never a fabricated huge number


def test_sov_delta_is_percentage_points_not_percentage_change(
    db_session, project_factory, article_factory
):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    # Baseline: Auchan 1/2 = 50% SOV. Comparison: Auchan 1/4 = 25% SOV.
    article_factory(baseline_project, count=1, retailer="Auchan")
    article_factory(baseline_project, count=1, retailer="Carrefour")
    article_factory(comparison_project, count=1, retailer="Auchan")
    article_factory(comparison_project, count=3, retailer="Carrefour")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters()
    )
    auchan = next(row for row in result["deltas"]["brands"] if row["brand"] == "Auchan")

    # A pp delta of -25 (50% -> 25%), NOT a %-change-of-a-percentage (-50%).
    assert auchan["baseline_sov_pct"] == 50.0
    assert auchan["comparison_sov_pct"] == 25.0
    assert auchan["sov_delta_pp"] == -25.0


# ---------------------------------------------------------------------------
# Ranking changes: gain/loss, new entrant, dropout
# ---------------------------------------------------------------------------


def test_brand_rank_change_gain_and_loss(db_session, project_factory, article_factory):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    # Baseline: Carrefour #1 (3), Auchan #2 (1).
    article_factory(baseline_project, count=3, retailer="Carrefour")
    article_factory(baseline_project, count=1, retailer="Auchan")
    # Comparison: Auchan #1 (5), Carrefour #2 (1) -- ranks swap.
    article_factory(comparison_project, count=5, retailer="Auchan")
    article_factory(comparison_project, count=1, retailer="Carrefour")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters()
    )
    by_brand = {row["brand"]: row for row in result["deltas"]["brands"]}

    assert by_brand["Auchan"]["baseline_rank"] == 2
    assert by_brand["Auchan"]["comparison_rank"] == 1
    assert by_brand["Auchan"]["rank_change"] == 1  # improved by one place

    assert by_brand["Carrefour"]["baseline_rank"] == 1
    assert by_brand["Carrefour"]["comparison_rank"] == 2
    assert by_brand["Carrefour"]["rank_change"] == -1  # dropped one place


def test_brand_new_entrant_and_dropout(db_session, project_factory, article_factory):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    article_factory(baseline_project, count=1, retailer="Lidl")
    article_factory(comparison_project, count=1, retailer="Profi")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters()
    )
    by_brand = {row["brand"]: row for row in result["deltas"]["brands"]}

    assert by_brand["Profi"]["is_new_entrant"] is True
    assert by_brand["Profi"]["baseline_rank"] is None
    assert by_brand["Lidl"]["is_dropout"] is True
    assert by_brand["Lidl"]["comparison_rank"] is None


def test_publication_ranking_computed_against_full_set_not_display_top_n(
    db_session, project_factory, article_factory
):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    # 12 distinct publications in baseline, all with 1 article -> a
    # low-ranked one could be misread as a "new entrant" if truncated at a
    # small display top_n instead of the full ranking.
    for i in range(12):
        article_factory(baseline_project, count=1, source=f"Publication {i}")
    article_factory(comparison_project, count=1, source="Publication 11")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters(), top_n=3
    )

    pub_delta = next(
        row
        for row in result["deltas"]["publications_by_volume"]
        if row["publication"] == "Publication 11"
    )
    assert pub_delta["is_new_entrant"] is False
    assert pub_delta["baseline_rank"] is not None

    # Display list is still capped at top_n.
    assert len(result["baseline"]["publications_and_stories"]["publications_by_volume"]) == 3


# ---------------------------------------------------------------------------
# Cross-project duplicate handling
# ---------------------------------------------------------------------------


def test_cross_project_duplicate_deduplicated_once(db_session, project_factory, article_factory):
    q1 = project_factory(name="Q1", quarter="2026-Q1")
    q2 = project_factory(name="Q2", quarter="2026-Q2")

    shared_fingerprint = "shared-fp-comparison-test"
    article_factory(
        q1, count=1, retailer="Auchan", title="Same story", fingerprint=shared_fingerprint
    )
    article_factory(q1, count=1, retailer="Auchan", title="Q1 exclusive")
    article_factory(
        q2, count=1, retailer="Auchan", title="Same story", fingerprint=shared_fingerprint
    )

    # H1-style combined period spanning both quarters.
    other = project_factory(name="Other", quarter="2026-Q3")
    article_factory(other, count=1, retailer="Auchan")

    result = get_period_comparison(db_session, [q1.id, q2.id], [other.id], AnalyticsFilters())

    # 3 raw unique-valid articles across the two projects, 1 collapsed by the
    # cross-project fingerprint pass -> 2 remain.
    assert result["baseline"]["kpis"]["unique_valid_articles"] == 2
    assert result["baseline"]["kpis"]["cross_project_duplicates_excluded"] == 1


def test_single_project_side_has_zero_cross_project_duplicates(
    db_session, project_factory, article_factory
):
    project = project_factory(quarter="2026-Q1")
    other = project_factory(name="Other", quarter="2026-Q2")
    article_factory(project, count=3, retailer="Auchan")
    article_factory(other, count=1, retailer="Auchan")

    result = get_period_comparison(db_session, [project.id], [other.id], AnalyticsFilters())
    assert result["baseline"]["kpis"]["cross_project_duplicates_excluded"] == 0


# ---------------------------------------------------------------------------
# Missing reach
# ---------------------------------------------------------------------------


def test_missing_reach_excluded_from_comparison_reach_deltas(
    db_session, project_factory, article_factory
):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    article_factory(baseline_project, count=1, audience=1000.0)
    article_factory(baseline_project, count=1, audience=None)
    article_factory(comparison_project, count=1, audience=2000.0)

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters()
    )
    reach_delta = result["deltas"]["kpis"]["average_reach"]

    # Baseline average must be 1000 (the single recorded value), not 500
    # (which would happen if the missing value were treated as 0).
    assert reach_delta["baseline"] == 1000.0
    assert reach_delta["comparison"] == 2000.0


# ---------------------------------------------------------------------------
# Filtered comparisons
# ---------------------------------------------------------------------------


def test_filters_apply_identically_to_both_sides(db_session, project_factory, article_factory):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    article_factory(baseline_project, count=2, retailer="Auchan")
    article_factory(baseline_project, count=3, retailer="Carrefour")
    article_factory(comparison_project, count=1, retailer="Auchan")
    article_factory(comparison_project, count=4, retailer="Carrefour")

    result = get_period_comparison(
        db_session,
        [baseline_project.id],
        [comparison_project.id],
        AnalyticsFilters(brand="Auchan"),
    )

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 2
    assert result["comparison"]["kpis"]["unique_valid_articles"] == 1
    assert result["baseline"]["filters"]["brand"] == "Auchan"
    assert result["comparison"]["filters"]["brand"] == "Auchan"


# ---------------------------------------------------------------------------
# Empty and partially classified periods
# ---------------------------------------------------------------------------


def test_comparison_with_one_empty_period_does_not_crash(db_session, project_factory, article_factory):
    empty_project = project_factory(name="Empty", quarter="2026-Q1")
    populated = project_factory(name="Populated", quarter="2026-Q2")
    article_factory(populated, count=2, retailer="Auchan")

    result = get_period_comparison(db_session, [empty_project.id], [populated.id], AnalyticsFilters())

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 0
    assert result["deltas"]["brands"][0]["is_new_entrant"] is True


def test_comparison_with_both_empty_periods(db_session, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")

    result = get_period_comparison(db_session, [a.id], [b.id], AnalyticsFilters())

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 0
    assert result["comparison"]["kpis"]["unique_valid_articles"] == 0
    assert result["deltas"]["brands"] == []
    assert result["deltas"]["kpis"]["unique_valid_articles"]["percentage_delta"] == 0.0


def test_comparison_with_partially_classified_periods(
    db_session, project_factory, article_factory, classification_factory
):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")

    baseline_articles = article_factory(baseline_project, count=3, retailer="Auchan")
    classification_factory(baseline_articles[0], primary_topic="store_expansion")

    comparison_articles = article_factory(comparison_project, count=2, retailer="Auchan")
    classification_factory(comparison_articles[0], primary_topic="promotions_pricing")
    classification_factory(comparison_articles[1], primary_topic="promotions_pricing")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters()
    )

    assert result["baseline"]["kpis"]["unique_classified_articles"] == 1
    assert result["baseline"]["kpis"]["unique_unclassified_articles"] == 2
    assert result["comparison"]["kpis"]["unique_classified_articles"] == 2
    # Topic deltas must not crash even though the classified populations
    # differ in size and topic composition.
    topic_values = {row["value"] for row in result["deltas"]["topics"]["rows"]}
    assert topic_values == {"store_expansion", "promotions_pricing"}


def test_invalid_and_duplicate_articles_excluded_from_comparison(
    db_session, project_factory, article_factory
):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    article_factory(baseline_project, count=2, retailer="Auchan", import_status=ImportStatus.VALID)
    article_factory(
        baseline_project, count=1, retailer="Auchan", import_status=ImportStatus.INVALID
    )
    article_factory(baseline_project, count=1, retailer="Auchan", is_duplicate=True)
    article_factory(comparison_project, count=3, retailer="Auchan")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id], AnalyticsFilters()
    )

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 2


# ---------------------------------------------------------------------------
# Phase D corrections: _merge_filter_options (analytics_needs_review_count,
# source-file identity)
# ---------------------------------------------------------------------------


def test_merged_analytics_needs_review_count_not_doubled_for_overlapping_projects(
    db_session, project_factory, article_factory
):
    """The same project appearing on both sides of a comparison must not
    cause its needs-review rows to be counted twice in the merged options.
    """
    from app.models.article import RetailerReviewStatus

    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(
        project, count=2, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_period_comparison(db_session, [project.id], [project.id])

    assert result["available_filter_options"]["analytics_needs_review_count"] == 2


def test_merged_analytics_needs_review_count_correct_for_disjoint_projects(
    db_session, project_factory, article_factory
):
    from app.models.article import RetailerReviewStatus

    baseline_project = project_factory(name="Baseline")
    comparison_project = project_factory(name="Comparison")
    article_factory(
        baseline_project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )
    article_factory(
        comparison_project, count=2, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_period_comparison(db_session, [baseline_project.id], [comparison_project.id])

    assert result["available_filter_options"]["analytics_needs_review_count"] == 3


def test_merged_source_files_never_collapse_same_named_files_across_projects(
    db_session, project_factory, article_factory, uploaded_file_factory
):
    """Two different files sharing an identical filename, one per project,
    must remain two distinct entries in the merged options -- identity is
    the file's UUID, never its filename.
    """
    baseline_project = project_factory(name="Baseline")
    comparison_project = project_factory(name="Comparison")
    baseline_file = uploaded_file_factory(baseline_project, original_filename="Q2 2026.xlsx")
    comparison_file = uploaded_file_factory(comparison_project, original_filename="Q2 2026.xlsx")
    article_factory(baseline_project, count=1, retailer="Auchan", uploaded_file_id=baseline_file.id)
    article_factory(comparison_project, count=1, retailer="Carrefour", uploaded_file_id=comparison_file.id)

    result = get_period_comparison(db_session, [baseline_project.id], [comparison_project.id])

    same_named_entries = [
        f for f in result["available_filter_options"]["source_files"]
        if f["original_filename"] == "Q2 2026.xlsx"
    ]
    same_named_ids = {f["id"] for f in same_named_entries}
    # Both same-named files remain distinct entries -- identity is the
    # file's UUID, never its filename, so a name collision must never
    # collapse two different projects' files into one option.
    assert same_named_ids == {baseline_file.id, comparison_file.id}
    assert len(same_named_entries) == 2


# ---------------------------------------------------------------------------
# Phase E: same-project brand-vs-brand comparison
# ---------------------------------------------------------------------------


def test_same_project_brand_vs_brand_produces_independent_populations(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=3, retailer="Auchan", audience=100.0)
    article_factory(project, count=2, retailer="Carrefour", audience=50.0)

    result = get_period_comparison(
        db_session, [project.id], [project.id],
        baseline_filters=AnalyticsFilters(brands=("Auchan",)),
        comparison_filters=AnalyticsFilters(brands=("Carrefour",)),
    )

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 3
    assert result["comparison"]["kpis"]["unique_valid_articles"] == 2
    assert result["baseline"]["kpis"]["total_reach"] == 300.0
    assert result["comparison"]["kpis"]["total_reach"] == 100.0


def test_same_project_brand_vs_brand_labels_are_brand_based_not_quarter_collision(
    db_session, project_factory, article_factory
):
    project = project_factory(quarter="2026-Q2")
    article_factory(project, count=1, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    result = get_period_comparison(
        db_session, [project.id], [project.id],
        baseline_filters=AnalyticsFilters(brands=("Auchan",)),
        comparison_filters=AnalyticsFilters(brands=("Carrefour",)),
    )

    assert result["baseline"]["label"] == "Auchan"
    assert result["comparison"]["label"] == "Carrefour"
    assert result["baseline"]["label"] != result["comparison"]["label"]


def test_same_project_brand_vs_brand_multi_brand_label_truncates(
    db_session, project_factory, article_factory
):
    project = project_factory()
    for brand in ("Auchan", "Carrefour", "Lidl"):
        article_factory(project, count=1, retailer=brand)

    result = get_period_comparison(
        db_session, [project.id], [project.id],
        baseline_filters=AnalyticsFilters(brands=("Auchan", "Carrefour", "Lidl")),
        comparison_filters=AnalyticsFilters(brands=("Auchan",)),
    )

    assert result["baseline"]["label"] == "3 brands"
    assert result["comparison"]["label"] == "Auchan"


def test_same_project_same_filters_both_sides_falls_back_to_quarter_label(
    db_session, project_factory, article_factory
):
    """No filter split at all -- completely unchanged pre-Phase-E
    behavior, quarter-collision label included (this is not a new bug,
    just confirming the fallback path is untouched).
    """
    project = project_factory(quarter="2026-Q2")
    article_factory(project, count=1, retailer="Auchan")

    result = get_period_comparison(db_session, [project.id], [project.id])

    assert result["baseline"]["label"] == "Q2 2026"
    assert result["comparison"]["label"] == "Q2 2026"


def test_disjoint_project_labels_unaffected_by_phase_e_change(
    db_session, project_factory, article_factory
):
    baseline_project = project_factory(name="Baseline", quarter="2026-Q1")
    comparison_project = project_factory(name="Comparison", quarter="2026-Q2")
    article_factory(baseline_project, count=1, retailer="Auchan")
    article_factory(comparison_project, count=1, retailer="Auchan")

    result = get_period_comparison(db_session, [baseline_project.id], [comparison_project.id])

    assert result["baseline"]["label"] == "Q1 2026"
    assert result["comparison"]["label"] == "Q2 2026"


def test_same_project_brand_vs_brand_deltas_reflect_independent_populations(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    auchan_articles = article_factory(project, count=4, retailer="Auchan")
    carrefour_articles = article_factory(project, count=2, retailer="Carrefour")
    for a in auchan_articles:
        classification_factory(a, sentiment="positive")
    for a in carrefour_articles:
        classification_factory(a, sentiment="negative")

    result = get_period_comparison(
        db_session, [project.id], [project.id],
        baseline_filters=AnalyticsFilters(brands=("Auchan",)),
        comparison_filters=AnalyticsFilters(brands=("Carrefour",)),
    )

    kpi_delta = result["deltas"]["kpis"]["unique_valid_articles"]
    assert kpi_delta["baseline"] == 4
    assert kpi_delta["comparison"] == 2
    assert kpi_delta["absolute_delta"] == -2


def test_same_project_brand_vs_brand_report_data_has_no_double_counted_articles(
    report_db, project_factory, article_factory
):
    """Disjoint-brand filters on each side must never list the same
    article under both Baseline and Comparison in the exported Article
    Detail rows.
    """
    from app.services.report_data import build_comparison_report_data

    project = project_factory()
    article_factory(project, count=3, retailer="Auchan")
    article_factory(project, count=2, retailer="Carrefour")

    data = build_comparison_report_data(
        report_db, [project.id], [project.id],
        baseline_filters=AnalyticsFilters(brands=("Auchan",)),
        comparison_filters=AnalyticsFilters(brands=("Carrefour",)),
    )

    article_ids = [row.article_id for row in data.article_detail]
    assert len(article_ids) == len(set(article_ids))  # no duplicates
    assert len(data.article_detail) == 5
    baseline_ids = {row.article_id for row in data.article_detail if row.period == "Baseline"}
    comparison_ids = {row.article_id for row in data.article_detail if row.period == "Comparison"}
    assert baseline_ids.isdisjoint(comparison_ids)
    assert data.baseline_label == "Auchan"
    assert data.comparison_label == "Carrefour"


def test_disjoint_project_comparison_unaffected_when_split_filters_omitted(
    db_session, project_factory, article_factory
):
    """Existing project-vs-project comparisons (no baseline_filters/
    comparison_filters passed) are completely unaffected by Phase E.
    """
    baseline_project = project_factory(name="Baseline")
    comparison_project = project_factory(name="Comparison")
    article_factory(baseline_project, count=3, retailer="Auchan")
    article_factory(comparison_project, count=5, retailer="Auchan")

    result = get_period_comparison(
        db_session, [baseline_project.id], [comparison_project.id],
        filters=AnalyticsFilters(brands=("Auchan",)),
    )

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 3
    assert result["comparison"]["kpis"]["unique_valid_articles"] == 5
