import pytest

from app.services.analytics import AnalyticsFilters
from app.services.chat_tools import (
    ChatScopeContext,
    ToolName,
    ToolValidationError,
    build_scope_context,
    execute_tool_call,
    find_latest_matching_generation,
    validate_and_parse_tool_call,
)
from app.services.narrative_service import create_comparison_generation, create_project_generation


def _project_scope(project, filters=None) -> ChatScopeContext:
    return ChatScopeContext(
        kind="project", project=project, baseline_projects=None, comparison_projects=None,
        filters=filters or AnalyticsFilters(),
    )


def _comparison_scope(baseline, comparison, filters=None) -> ChatScopeContext:
    return ChatScopeContext(
        kind="comparison", project=None, baseline_projects=baseline, comparison_projects=comparison,
        filters=filters or AnalyticsFilters(),
    )


def test_valid_tool_selection(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    scope = _project_scope(project)

    params = validate_and_parse_tool_call(
        db_session, scope, ToolName.GET_BRAND_PERFORMANCE, {"brand": "Auchan"}
    )
    assert params.brand == "Auchan"


def test_unknown_tool_rejected(db_session, project_factory):
    project = project_factory()
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Unknown tool"):
        validate_and_parse_tool_call(db_session, scope, "get_secret_data", {})


def test_tool_not_valid_for_project_scope(db_session, project_factory):
    project = project_factory()
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="not valid for scope"):
        validate_and_parse_tool_call(db_session, scope, ToolName.GET_PERIOD_COMPARISON, {})


def test_tool_not_valid_for_comparison_scope(db_session, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    scope = _comparison_scope([a], [b])

    with pytest.raises(ToolValidationError, match="not valid for scope"):
        validate_and_parse_tool_call(db_session, scope, ToolName.GET_PROJECT_KPIS, {})


def test_malformed_parameters_rejected(db_session, project_factory):
    project = project_factory()
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Malformed parameters"):
        validate_and_parse_tool_call(
            db_session, scope, ToolName.GET_BRAND_PERFORMANCE, {"top_n": 999}
        )


def test_unknown_brand_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Unknown brand"):
        validate_and_parse_tool_call(
            db_session, scope, ToolName.GET_BRAND_PERFORMANCE, {"brand": "NotARealBrand"}
        )


def test_unknown_topic_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Unknown topic"):
        validate_and_parse_tool_call(
            db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"topic": "not_a_topic"}
        )


def test_unknown_sentiment_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Unknown sentiment"):
        validate_and_parse_tool_call(
            db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"sentiment": "furious"}
        )


def test_unknown_publication_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan", source="Ziarul")
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Unknown publication"):
        validate_and_parse_tool_call(
            db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"publication": "Made Up Gazette"}
        )


def test_unknown_story_key_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    scope = _project_scope(project)

    with pytest.raises(ToolValidationError, match="Unknown story_key"):
        validate_and_parse_tool_call(
            db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"story_key": "nope"}
        )


def test_known_story_key_accepted(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1, retailer="Auchan")[0]
    classification_factory(article, story_key="story-xyz")
    scope = _project_scope(project)

    params = validate_and_parse_tool_call(
        db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"story_key": "story-xyz"}
    )
    assert params.story_key == "story-xyz"


def test_get_brand_performance_resolves_niche_brand_outside_top_n(
    db_session, project_factory, article_factory
):
    project = project_factory()
    # 15 distinct high-volume brands push "Niche" out of the default top_n.
    for i in range(15):
        article_factory(project, count=5, retailer=f"Brand{i}")
    article_factory(project, count=1, retailer="Niche")
    scope = _project_scope(project)

    params = validate_and_parse_tool_call(
        db_session, scope, ToolName.GET_BRAND_PERFORMANCE, {"brand": "Niche", "top_n": 5}
    )
    result = execute_tool_call(db_session, scope, ToolName.GET_BRAND_PERFORMANCE, params)

    assert len(result["by_volume"]) == 5
    assert all(row["brand"] != "Niche" for row in result["by_volume"])
    assert result["requested_brand"]["brand"] == "Niche"


def test_get_project_articles_period_selector_for_comparison_scope(
    db_session, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan", source="Baseline Pub")
    article_factory(b, count=1, retailer="Carrefour", source="Comparison Pub")
    scope = _comparison_scope([a], [b])

    baseline_params = validate_and_parse_tool_call(
        db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"period": "baseline"}
    )
    baseline_result = execute_tool_call(
        db_session, scope, ToolName.GET_PROJECT_ARTICLES, baseline_params
    )
    assert baseline_result["articles"][0]["brand"] == "Auchan"

    comparison_params = validate_and_parse_tool_call(
        db_session, scope, ToolName.GET_PROJECT_ARTICLES, {"period": "comparison"}
    )
    comparison_result = execute_tool_call(
        db_session, scope, ToolName.GET_PROJECT_ARTICLES, comparison_params
    )
    assert comparison_result["articles"][0]["brand"] == "Carrefour"


def test_get_period_comparison_executor_shape(db_session, project_factory, article_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=2, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")
    scope = _comparison_scope([a], [b])

    params = validate_and_parse_tool_call(db_session, scope, ToolName.GET_PERIOD_COMPARISON, {})
    result = execute_tool_call(db_session, scope, ToolName.GET_PERIOD_COMPARISON, params)

    assert result["baseline"]["kpis"]["unique_valid_articles"] == 2
    assert result["comparison"]["kpis"]["unique_valid_articles"] == 1
    assert "deltas" in result


def test_build_scope_context_project(db_session, project_factory):
    project = project_factory()
    from app.services.chat_service import find_or_create_project_session

    session = find_or_create_project_session(db_session, project)
    scope = build_scope_context(db_session, session)

    assert scope.kind == "project"
    assert scope.project.id == project.id


def test_build_scope_context_comparison(db_session, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    from app.services.chat_service import find_or_create_comparison_session

    session = find_or_create_comparison_session(db_session, [a.id], [b.id])
    scope = build_scope_context(db_session, session)

    assert scope.kind == "comparison"
    assert {p.id for p in scope.baseline_projects} == {a.id}
    assert {p.id for p in scope.comparison_projects} == {b.id}


# --- Regression coverage for the JSONB none_as_null fix ----------------------


def test_find_latest_matching_generation_project_scope_uses_sql_null(
    db_session, project_factory, article_factory
):
    """Regression test: NarrativeGeneration.baseline_project_ids must be
    true SQL NULL (not the JSONB scalar `null`) for a project-scoped
    generation, or this lookup silently returns nothing.
    """
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    generation.status = "complete"
    db_session.commit()

    scope = _project_scope(project)
    found = find_latest_matching_generation(db_session, scope)

    assert found is not None
    assert found.id == generation.id


def test_find_latest_matching_generation_comparison_scope_excludes_project_scoped(
    db_session, project_factory, article_factory
):
    """Regression test: a project-scoped generation (baseline_project_ids
    SQL NULL) must never be picked up by a comparison-scope lookup that
    filters on `.isnot(None)`.
    """
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    project_generation, _ = create_project_generation(db_session, a, AnalyticsFilters())
    project_generation.status = "complete"
    comparison_generation, _ = create_comparison_generation(
        db_session, [a.id], [b.id], AnalyticsFilters()
    )
    comparison_generation.status = "complete"
    db_session.commit()

    scope = _comparison_scope([a], [b])
    found = find_latest_matching_generation(db_session, scope)

    assert found is not None
    assert found.id == comparison_generation.id
    assert found.id != project_generation.id


def test_find_latest_matching_generation_returns_none_when_absent(db_session, project_factory):
    project = project_factory()
    scope = _project_scope(project)
    assert find_latest_matching_generation(db_session, scope) is None
