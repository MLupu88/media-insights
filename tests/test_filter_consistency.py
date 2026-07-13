"""Phase D corrections — proves the shared `apply_common_filters` helper
produces byte-identical article populations across every consumer that
uses it (analytics, report export, chat evidence, narrative evidence),
and documents/tests the needs-review truth table once, centrally.

Every comparison below calls the four raw query-builder functions
*directly* (not through their higher-level callers like
`_fetch_entries`/`_collect_article_detail`/`_get_project_articles`/
`_sample_evidence_articles`) so the comparison is never able to pass by
coincidence: none of `_base_query`, `_article_detail_query`,
`_articles_query`, or `_evidence_base_query` apply their own LIMIT,
ordering-with-truncation, or classification/confidence restriction --
those are only added by each *caller* afterward. Non-common filters
(publication/primary_topic/communication_category/sentiment/state) are
held at their defaults throughout, since they aren't part of what
`apply_common_filters` is responsible for.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.article import Article, RetailerReviewStatus
from app.services.analytics import AnalyticsFilters, apply_common_filters
from app.services.analytics import _base_query as analytics_base_query
from app.services.chat_tools import _articles_query
from app.services.narrative_payload import _evidence_base_query
from app.services.report_data import _article_detail_query


def _ids(db_session, stmt) -> set:
    return {row[0].id for row in db_session.execute(stmt).all()}


@pytest.fixture
def seeded_project(db_session, project_factory, article_factory, uploaded_file_factory):
    project = project_factory()
    file_a = uploaded_file_factory(project)
    file_b = uploaded_file_factory(project)
    article_factory(project, count=2, retailer="Auchan", uploaded_file_id=file_a.id)
    article_factory(project, count=2, retailer="Carrefour", uploaded_file_id=file_b.id)
    article_factory(project, count=1, retailer="Lidl", uploaded_file_id=file_a.id)
    article_factory(
        project, count=2, retailer="unknown", uploaded_file_id=file_b.id,
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )
    return project, file_a, file_b


def _all_populations(db_session, project, filters: AnalyticsFilters) -> list[set]:
    project_ids = [project.id]
    return [
        _ids(db_session, analytics_base_query(project.id, filters)),
        _ids(db_session, _article_detail_query(project_ids, filters)),
        _ids(db_session, _articles_query(project_ids, filters)),
        _ids(db_session, _evidence_base_query(project_ids, filters)),
    ]


def _assert_all_equal(populations: list[set]) -> None:
    first = populations[0]
    for other in populations[1:]:
        assert other == first


def test_consistency_multiple_brands(db_session, seeded_project):
    project, _file_a, _file_b = seeded_project
    filters = AnalyticsFilters(brands=("Auchan", "Carrefour"))
    populations = _all_populations(db_session, project, filters)
    _assert_all_equal(populations)
    assert len(populations[0]) == 4


def test_consistency_source_file_filter(db_session, seeded_project):
    project, file_a, _file_b = seeded_project
    filters = AnalyticsFilters(uploaded_file_ids=(file_a.id,))
    populations = _all_populations(db_session, project, filters)
    _assert_all_equal(populations)
    assert len(populations[0]) == 3


def test_consistency_needs_review_only(db_session, seeded_project):
    project, _file_a, _file_b = seeded_project
    filters = AnalyticsFilters(include_needs_review=True)
    populations = _all_populations(db_session, project, filters)
    _assert_all_equal(populations)
    assert len(populations[0]) == 2


def test_consistency_brands_plus_needs_review(db_session, seeded_project):
    project, _file_a, _file_b = seeded_project
    filters = AnalyticsFilters(brands=("Auchan",), include_needs_review=True)
    populations = _all_populations(db_session, project, filters)
    _assert_all_equal(populations)
    assert len(populations[0]) == 4  # 2 Auchan + 2 needs-review


def test_consistency_all_filters_combined(db_session, seeded_project):
    project, file_a, _file_b = seeded_project
    filters = AnalyticsFilters(
        brands=("Auchan", "Lidl"), uploaded_file_ids=(file_a.id,), include_needs_review=True
    )
    populations = _all_populations(db_session, project, filters)
    _assert_all_equal(populations)
    # brands OR needs-review (per the truth table), each side additionally
    # narrowed by the shared uploaded_file_ids predicate: Auchan+Lidl in
    # file_a (3) plus needs-review rows in file_a (0, they're all in
    # file_b) = 3.
    assert len(populations[0]) == 3


# --- needs-review truth table -- one shared, parameterized test -------------


@pytest.mark.parametrize(
    "brands,include_needs_review,expected_count",
    [
        ((), False, 7),  # full population, unresolved coverage included
        ((), True, 2),  # needs-review rows only
        (("Auchan",), False, 2),  # selected confirmed brand only
        (("Auchan",), True, 4),  # selected confirmed brand plus needs-review
    ],
)
def test_needs_review_truth_table(
    db_session, seeded_project, brands, include_needs_review, expected_count
):
    project, _file_a, _file_b = seeded_project
    filters = AnalyticsFilters(brands=brands, include_needs_review=include_needs_review)
    stmt = select(Article).where(Article.project_id == project.id)
    stmt = apply_common_filters(stmt, filters)
    result = db_session.execute(stmt).scalars().all()
    assert len(result) == expected_count
