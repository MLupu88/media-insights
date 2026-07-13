from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.analytics import AnalyticsFilters, get_project_analytics
from app.services.comparison import ComparisonServiceError, get_period_comparison
from app.services.narrative_service import create_comparison_generation, create_project_generation
from app.services.report_data import (
    ReportNotFoundError,
    build_comparison_report_data,
    build_project_report_data,
)

# `report_db` fixture lives in tests/conftest.py (shared by the report test
# files) — a fresh session, since build_*_report_data must issue its
# REPEATABLE READ SET as the first statement on the session.


def _complete_generation(db_session, generation):
    generation.status = "complete"
    db_session.commit()


def _submit_insight(db_session, generation, narrative_type, key, narrative, evidence_path, evidence_value):
    from app.models.narrative import NarrativeInsight, NarrativeValidationStatus

    insight = NarrativeInsight(
        generation_id=generation.id,
        project_id=generation.project_id,
        narrative_type=narrative_type,
        key=key,
        title="Title",
        narrative=narrative,
        evidence_type="kpi_delta",
        evidence=[{"path": evidence_path, "role": "value", "value": evidence_value}],
        related_brand="Auchan",
        raw_candidate={},
        validation_status=NarrativeValidationStatus.VALID,
    )
    db_session.add(insight)
    db_session.commit()
    return insight


def test_project_report_data_not_found(report_db):
    import uuid

    with pytest.raises(ReportNotFoundError):
        build_project_report_data(report_db, uuid.uuid4(), AnalyticsFilters())


def test_project_report_data_matches_analytics_exactly(
    db_session, report_db, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=3, retailer="Auchan")

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    fresh = get_project_analytics(db_session, project, AnalyticsFilters(), top_n=data.analytics["top_n"])

    assert data.analytics["kpis"] == fresh["kpis"]
    assert data.analytics["brands"] == fresh["brands"]


def test_comparison_report_data_matches_comparison_exactly(
    db_session, report_db, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=2, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    data = build_comparison_report_data(report_db, [a.id], [b.id], AnalyticsFilters())
    fresh = get_period_comparison(db_session, [a.id], [b.id], AnalyticsFilters(), top_n=data.comparison["top_n"])

    assert data.comparison["deltas"]["kpis"] == fresh["deltas"]["kpis"]


def test_comparison_report_data_propagates_service_error(report_db, project_factory):
    b = project_factory()
    with pytest.raises(ComparisonServiceError):
        build_comparison_report_data(report_db, [], [b.id], AnalyticsFilters())


def test_one_analytics_call_per_export_not_per_format(report_db, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    with patch(
        "app.services.report_data.get_project_analytics", side_effect=get_project_analytics
    ) as spy:
        build_project_report_data(report_db, project.id, AnalyticsFilters())
        assert spy.call_count == 1


def test_repeatable_read_isolation_is_set(report_db, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    captured = {}

    def _spy(db, project_arg, filters, top_n):
        captured["isolation"] = db.execute(text("SHOW transaction_isolation")).scalar()
        return get_project_analytics(db, project_arg, filters, top_n=top_n)

    with patch("app.services.report_data.get_project_analytics", side_effect=_spy):
        build_project_report_data(report_db, project.id, AnalyticsFilters())

    assert captured["isolation"] == "repeatable read"


# --- insight handling ---------------------------------------------------------


def test_only_valid_insights_included(db_session, report_db, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    _complete_generation(db_session, generation)

    _submit_insight(db_session, generation, "key_findings", "k1", "Valid insight text.", "kpis.unique_valid_articles", 1)

    from app.models.narrative import NarrativeInsight, NarrativeValidationStatus

    rejected = NarrativeInsight(
        generation_id=generation.id, project_id=generation.project_id, narrative_type="key_findings",
        key="k2", title="Rejected", narrative="Rejected insight text.", evidence_type="kpi_delta",
        evidence=[], raw_candidate={}, validation_status=NarrativeValidationStatus.REJECTED,
        rejection_reason="bad",
    )
    db_session.add(rejected)
    db_session.commit()

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    titles = [i.title for i in data.insights]
    assert "Rejected" not in titles
    assert data.metadata.insight_coverage.available_count == 1


def test_causal_language_excluded(db_session, report_db, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    _complete_generation(db_session, generation)

    _submit_insight(
        db_session, generation, "key_findings", "k1",
        "SOV-ul a crescut din cauza campaniei recente.",
        "kpis.unique_valid_articles", 1,
    )

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    assert data.insights == []
    assert data.metadata.insight_coverage.excluded_causal_count == 1
    assert data.metadata.insight_coverage.included_count == 0


def test_softer_causal_adjacent_language_not_excluded(
    db_session, report_db, project_factory, article_factory
):
    """Boundary test: a permitted phrase ('a contribuit la') must not be
    treated as banned causal language.
    """
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    _complete_generation(db_session, generation)

    _submit_insight(
        db_session, generation, "key_findings", "k1",
        "Campania a contribuit la cresterea acoperirii.",
        "kpis.unique_valid_articles", 1,
    )

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    assert len(data.insights) == 1
    assert data.metadata.insight_coverage.excluded_causal_count == 0


def test_recommendation_vs_interpretation_label(
    db_session, report_db, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    _complete_generation(db_session, generation)

    _submit_insight(db_session, generation, "recommendations", "r1", "Do this.", "kpis.unique_valid_articles", 1)
    _submit_insight(db_session, generation, "key_findings", "k1", "Note this.", "kpis.unique_valid_articles", 1)

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    labels = {i.narrative_type: i.label for i in data.insights}
    assert labels["recommendations"] == "Recommendation"
    assert labels["key_findings"] == "Interpretation"


def test_excluded_insight_never_mutates_original_row(
    db_session, report_db, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    _complete_generation(db_session, generation)

    insight = _submit_insight(
        db_session, generation, "key_findings", "k1",
        "SOV-ul a crescut din cauza campaniei.",
        "kpis.unique_valid_articles", 1,
    )

    build_project_report_data(report_db, project.id, AnalyticsFilters())

    db_session.refresh(insight)
    assert insight.validation_status == "valid"
    assert insight.rejection_reason is None


def test_comparison_insight_scope_matching(db_session, report_db, project_factory, article_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")
    generation, _ = create_comparison_generation(db_session, [a.id], [b.id], AnalyticsFilters())
    _complete_generation(db_session, generation)
    _submit_insight(
        db_session, generation, "comparison_executive_summary", "c1", "Comparison text.",
        "deltas.kpis.unique_valid_articles.absolute_delta", -1,
    )

    data = build_comparison_report_data(report_db, [a.id], [b.id], AnalyticsFilters())
    assert len(data.insights) == 1


# --- empty / partial data --------------------------------------------------------


def test_empty_project_data_shape(report_db, project_factory):
    project = project_factory()
    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    assert data.analytics["kpis"]["unique_valid_articles"] == 0
    assert data.insights == []
    assert data.article_detail == []
    assert data.metadata.article_detail_coverage.shown_count == 0


def test_partially_classified_project_data_shape(
    report_db, project_factory, article_factory, classification_factory
):
    project = project_factory()
    articles = article_factory(project, count=3, retailer="Auchan")
    classification_factory(articles[0])

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())
    assert data.analytics["kpis"]["unique_classified_articles"] == 1
    assert data.analytics["kpis"]["unique_unclassified_articles"] == 2


# --- article detail truncation ---------------------------------------------------


def test_article_detail_truncation_math(report_db, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=5, retailer="Auchan")

    with patch("app.services.report_data.MAX_ARTICLE_DETAIL_ROWS", 2):
        data = build_project_report_data(report_db, project.id, AnalyticsFilters())

    assert data.metadata.article_detail_coverage.shown_count == 2
    assert data.metadata.article_detail_coverage.total_count == 5
    assert data.metadata.article_detail_coverage.truncated is True


def test_article_detail_not_truncated_below_cap(report_db, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3, retailer="Auchan")

    data = build_project_report_data(report_db, project.id, AnalyticsFilters())

    assert data.metadata.article_detail_coverage.shown_count == 3
    assert data.metadata.article_detail_coverage.total_count == 3
    assert data.metadata.article_detail_coverage.truncated is False


def test_comparison_article_detail_labeled_by_period(report_db, project_factory, article_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    data = build_comparison_report_data(report_db, [a.id], [b.id], AnalyticsFilters())
    periods = {row.period for row in data.article_detail}
    assert periods == {"Baseline", "Comparison"}
