"""The fixed chat tool registry.

The model never supplies a project ID, SQL, or an ORM filter — only
validated, closed-vocabulary parameters. Which project(s)/comparison an
executor queries is resolved entirely from the run's `ChatSession` (via
`ChatScopeContext`), never from the tool call itself. Every executor is a
thin wrapper over the existing, unchanged Phase 4/5/6A services
(`app/services/analytics.py`, `app/services/comparison.py`,
`app/models/narrative.py`).
"""

import uuid
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.chat import ChatSession
from app.models.classification import Classification, ClassificationTaxonomy
from app.models.narrative import (
    NarrativeGeneration,
    NarrativeGenerationStatus,
    NarrativeInsight,
    NarrativeValidationStatus,
)
from app.models.project import Project
from app.schemas.chat import (
    GetBrandPerformanceParams,
    GetPeriodComparisonParams,
    GetProjectArticlesParams,
    GetProjectKpisParams,
    GetPublicationRankingsParams,
    GetSentimentDistributionParams,
    GetStoryClustersParams,
    GetTopicDistributionParams,
    GetValidNarrativeInsightsParams,
)
from app.services.analytics import (
    DEFAULT_TOP_N,
    MAX_TOP_N,
    AnalyticsFilters,
    apply_common_filters,
    get_period_analytics,
    get_project_analytics,
    parse_analytics_filters,
)
from app.services.comparison import get_period_comparison as _get_period_comparison_service
from app.services.json_safe import to_json_safe


class ToolName:
    GET_PROJECT_KPIS = "get_project_kpis"
    GET_BRAND_PERFORMANCE = "get_brand_performance"
    GET_TOPIC_DISTRIBUTION = "get_topic_distribution"
    GET_SENTIMENT_DISTRIBUTION = "get_sentiment_distribution"
    GET_PUBLICATION_RANKINGS = "get_publication_rankings"
    GET_STORY_CLUSTERS = "get_story_clusters"
    GET_PROJECT_ARTICLES = "get_project_articles"
    GET_PERIOD_COMPARISON = "get_period_comparison"
    GET_VALID_NARRATIVE_INSIGHTS = "get_valid_narrative_insights"


class ToolValidationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass
class ChatScopeContext:
    kind: str  # "project" | "comparison"
    project: Project | None
    baseline_projects: list[Project] | None
    comparison_projects: list[Project] | None
    filters: AnalyticsFilters


def build_scope_context(db: Session, session: ChatSession) -> ChatScopeContext:
    # Reuses the same canonical parser used for URL query strings, not a
    # raw kwarg splat -- correctly reads all three historical persisted
    # shapes (Phase C, the interim pre-correction Phase D shape, and the
    # final canonical shape) and produces properly-typed UUIDs, unlike a
    # direct `AnalyticsFilters(**(session.filters or {}))` splat would.
    filters = parse_analytics_filters(session.filters or {})
    if session.baseline_project_ids:
        baseline_projects = [
            db.get(Project, uuid.UUID(pid)) for pid in session.baseline_project_ids
        ]
        comparison_projects = [
            db.get(Project, uuid.UUID(pid)) for pid in session.comparison_project_ids
        ]
        return ChatScopeContext(
            kind="comparison",
            project=None,
            baseline_projects=baseline_projects,
            comparison_projects=comparison_projects,
            filters=filters,
        )
    project = db.get(Project, session.project_id)
    return ChatScopeContext(
        kind="project",
        project=project,
        baseline_projects=None,
        comparison_projects=None,
        filters=filters,
    )


def _resolve_period_projects(scope: ChatScopeContext, period: str | None) -> list[Project]:
    if scope.kind == "project":
        return [scope.project]
    if period == "comparison":
        return scope.comparison_projects
    return scope.baseline_projects


def _available_options_for_projects(
    db: Session, projects: list[Project], filters: AnalyticsFilters
) -> dict:
    result = get_period_analytics(db, projects, filters, top_n=DEFAULT_TOP_N)
    return result["available_filter_options"]


def _known_story_keys(db: Session, project_ids: list[uuid.UUID]) -> set[str]:
    stmt = (
        select(Classification.story_key)
        .join(Article, Article.id == Classification.article_id)
        .where(
            Article.project_id.in_(project_ids),
            Article.import_status == ImportStatus.VALID,
            Article.is_duplicate.is_(False),
            Classification.story_key.isnot(None),
        )
        .distinct()
    )
    return {row[0] for row in db.execute(stmt).all()}


def _article_entry(article: Article) -> dict:
    return {
        "article_id": str(article.id),
        "title": article.title,
        "article_url": article.article_url,
        "mediatrust_url": article.mediatrust_url,
        "publication_date": article.publication_date.isoformat()
        if article.publication_date
        else None,
        "source": article.source,
        "brand": article.retailer,
    }


def _articles_query(project_ids: list[uuid.UUID], filters: AnalyticsFilters):
    stmt = (
        select(Article, Classification)
        .outerjoin(Classification, Classification.article_id == Article.id)
        .where(
            Article.project_id.in_(project_ids),
            Article.import_status == ImportStatus.VALID,
            Article.is_duplicate.is_(False),
        )
        .order_by(Article.id)
    )
    stmt = apply_common_filters(stmt, filters)
    if filters.publication:
        stmt = stmt.where(Article.source == filters.publication)
    if filters.primary_topic:
        stmt = stmt.where(Classification.primary_topic == filters.primary_topic)
    if filters.communication_category:
        stmt = stmt.where(Classification.communication_category == filters.communication_category)
    if filters.sentiment:
        stmt = stmt.where(Classification.sentiment == filters.sentiment)
    if filters.state == "classified":
        stmt = stmt.where(Classification.id.isnot(None))
    elif filters.state == "unclassified":
        stmt = stmt.where(Classification.id.is_(None))
    return stmt


def find_latest_matching_generation(
    db: Session, scope: ChatScopeContext
) -> NarrativeGeneration | None:
    if scope.kind == "project":
        stmt = (
            select(NarrativeGeneration)
            .where(
                NarrativeGeneration.project_id == scope.project.id,
                NarrativeGeneration.baseline_project_ids.is_(None),
                NarrativeGeneration.status.in_(NarrativeGenerationStatus.REUSABLE),
            )
            .order_by(NarrativeGeneration.created_at.desc())
        )
        return db.execute(stmt).scalars().first()

    anchor_id = scope.baseline_projects[0].id
    baseline_ids = {str(p.id) for p in scope.baseline_projects}
    comparison_ids = {str(p.id) for p in scope.comparison_projects}
    stmt = (
        select(NarrativeGeneration)
        .where(
            NarrativeGeneration.project_id == anchor_id,
            NarrativeGeneration.baseline_project_ids.isnot(None),
            NarrativeGeneration.status.in_(NarrativeGenerationStatus.REUSABLE),
        )
        .order_by(NarrativeGeneration.created_at.desc())
    )
    for generation in db.execute(stmt).scalars().all():
        if (
            set(generation.baseline_project_ids) == baseline_ids
            and set(generation.comparison_project_ids) == comparison_ids
        ):
            return generation
    return None


# --- Executors ----------------------------------------------------------------


def _get_project_kpis(db: Session, scope: ChatScopeContext, params: GetProjectKpisParams) -> dict:
    analytics = get_project_analytics(db, scope.project, scope.filters, top_n=DEFAULT_TOP_N)
    return {
        "kpis": analytics["kpis"],
        "available_filter_options": analytics["available_filter_options"],
    }


def _get_brand_performance(
    db: Session, scope: ChatScopeContext, params: GetBrandPerformanceParams
) -> dict:
    # Always computed at the system-max ranking so a niche brand outside
    # the requested top_n still resolves via `requested_brand`.
    analytics = get_project_analytics(db, scope.project, scope.filters, top_n=MAX_TOP_N)
    brands = analytics["brands"]
    requested_row = None
    if params.brand:
        requested_row = next(
            (row for row in brands["by_volume"] if row["brand"] == params.brand), None
        )
    return {
        "by_volume": brands["by_volume"][: params.top_n],
        "by_reach": brands["by_reach"][: params.top_n],
        "brand_count": brands["brand_count"],
        "requested_brand": requested_row,
        "available_filter_options": analytics["available_filter_options"],
    }


def _get_topic_distribution(
    db: Session, scope: ChatScopeContext, params: GetTopicDistributionParams
) -> dict:
    analytics = get_project_analytics(db, scope.project, scope.filters, top_n=params.top_n)
    topics = analytics["topics"]
    return {
        "top_topics_by_volume": topics["top_topics_by_volume"],
        "top_topics_by_reach": topics["top_topics_by_reach"],
        "communication_category_distribution": topics["communication_category_distribution"],
        "classified_count": topics["classified_count"],
        "available_filter_options": analytics["available_filter_options"],
    }


def _get_sentiment_distribution(
    db: Session, scope: ChatScopeContext, params: GetSentimentDistributionParams
) -> dict:
    analytics = get_project_analytics(db, scope.project, scope.filters, top_n=DEFAULT_TOP_N)
    return {
        "sentiment": analytics["sentiment"],
        "available_filter_options": analytics["available_filter_options"],
    }


def _get_publication_rankings(
    db: Session, scope: ChatScopeContext, params: GetPublicationRankingsParams
) -> dict:
    analytics = get_project_analytics(db, scope.project, scope.filters, top_n=params.top_n)
    pubs = analytics["publications_and_stories"]
    return {
        "publications_by_volume": pubs["publications_by_volume"],
        "publications_by_reach": pubs["publications_by_reach"],
        "publication_concentration": pubs["publication_concentration"],
        "available_filter_options": analytics["available_filter_options"],
    }


def _get_story_clusters(
    db: Session, scope: ChatScopeContext, params: GetStoryClustersParams
) -> dict:
    analytics = get_project_analytics(db, scope.project, scope.filters, top_n=params.top_n)
    pubs = analytics["publications_and_stories"]
    return {
        "stories_by_volume": pubs["stories_by_volume"],
        "stories_by_reach": pubs["stories_by_reach"],
        "story_concentration": pubs["story_concentration"],
        "available_filter_options": analytics["available_filter_options"],
    }


def _get_project_articles(
    db: Session, scope: ChatScopeContext, params: GetProjectArticlesParams
) -> dict:
    projects = _resolve_period_projects(scope, params.period)
    project_ids = [p.id for p in projects]
    call_filters = AnalyticsFilters(
        brand=params.brand,
        publication=params.publication,
        primary_topic=params.topic,
        communication_category=None,
        sentiment=params.sentiment,
        state="all",
    )
    stmt = _articles_query(project_ids, call_filters)
    if params.story_key:
        stmt = stmt.where(Classification.story_key == params.story_key)
    stmt = stmt.limit(params.limit)
    rows = db.execute(stmt).all()
    articles = [_article_entry(article) for article, _classification in rows]
    return {"articles": articles, "count": len(articles)}


def _get_period_comparison(
    db: Session, scope: ChatScopeContext, params: GetPeriodComparisonParams
) -> dict:
    baseline_ids = [p.id for p in scope.baseline_projects]
    comparison_ids = [p.id for p in scope.comparison_projects]
    return _get_period_comparison_service(
        db, baseline_ids, comparison_ids, scope.filters, top_n=params.top_n
    )


def _get_valid_narrative_insights(
    db: Session, scope: ChatScopeContext, params: GetValidNarrativeInsightsParams
) -> dict:
    generation = find_latest_matching_generation(db, scope)
    if generation is None:
        return {"insights": [], "generation_id": None}

    stmt = select(NarrativeInsight).where(
        NarrativeInsight.generation_id == generation.id,
        NarrativeInsight.validation_status == NarrativeValidationStatus.VALID,
    )
    if params.narrative_type:
        stmt = stmt.where(NarrativeInsight.narrative_type == params.narrative_type)
    insights = db.execute(stmt).scalars().all()

    return {
        "insights": [
            {
                "id": str(insight.id),
                "narrative_type": insight.narrative_type,
                "title": insight.title,
                "narrative": insight.narrative,
                "related_brand": insight.related_brand,
                "related_topic": insight.related_topic,
                "related_publication": insight.related_publication,
                "related_story_key": insight.related_story_key,
                "confidence": insight.confidence,
                "caveat": insight.caveat,
            }
            for insight in insights
        ],
        "generation_id": str(generation.id),
    }


@dataclass
class ToolSpec:
    params_schema: type[BaseModel]
    executor: Callable[[Session, ChatScopeContext, BaseModel], dict]


TOOL_REGISTRY: dict[str, ToolSpec] = {
    ToolName.GET_PROJECT_KPIS: ToolSpec(GetProjectKpisParams, _get_project_kpis),
    ToolName.GET_BRAND_PERFORMANCE: ToolSpec(GetBrandPerformanceParams, _get_brand_performance),
    ToolName.GET_TOPIC_DISTRIBUTION: ToolSpec(GetTopicDistributionParams, _get_topic_distribution),
    ToolName.GET_SENTIMENT_DISTRIBUTION: ToolSpec(
        GetSentimentDistributionParams, _get_sentiment_distribution
    ),
    ToolName.GET_PUBLICATION_RANKINGS: ToolSpec(
        GetPublicationRankingsParams, _get_publication_rankings
    ),
    ToolName.GET_STORY_CLUSTERS: ToolSpec(GetStoryClustersParams, _get_story_clusters),
    ToolName.GET_PROJECT_ARTICLES: ToolSpec(GetProjectArticlesParams, _get_project_articles),
    ToolName.GET_PERIOD_COMPARISON: ToolSpec(GetPeriodComparisonParams, _get_period_comparison),
    ToolName.GET_VALID_NARRATIVE_INSIGHTS: ToolSpec(
        GetValidNarrativeInsightsParams, _get_valid_narrative_insights
    ),
}

PROJECT_SCOPE_TOOLS: tuple[str, ...] = (
    ToolName.GET_PROJECT_KPIS,
    ToolName.GET_BRAND_PERFORMANCE,
    ToolName.GET_TOPIC_DISTRIBUTION,
    ToolName.GET_SENTIMENT_DISTRIBUTION,
    ToolName.GET_PUBLICATION_RANKINGS,
    ToolName.GET_STORY_CLUSTERS,
    ToolName.GET_PROJECT_ARTICLES,
    ToolName.GET_VALID_NARRATIVE_INSIGHTS,
)

COMPARISON_SCOPE_TOOLS: tuple[str, ...] = (
    ToolName.GET_PERIOD_COMPARISON,
    ToolName.GET_PROJECT_ARTICLES,
    ToolName.GET_VALID_NARRATIVE_INSIGHTS,
)


def _validate_entity_scope(
    db: Session, scope: ChatScopeContext, tool_name: str, params: BaseModel
) -> None:
    """String tool parameters must resolve against real scope data, not
    just pass Pydantic type validation — an unknown brand/topic/
    publication/sentiment/story_key is a hard rejection, never a silently
    empty result the model could misread as "no coverage."
    """
    if tool_name == ToolName.GET_BRAND_PERFORMANCE and params.brand:
        options = _available_options_for_projects(db, [scope.project], scope.filters)
        if params.brand not in options["brands"]:
            raise ToolValidationError(f"Unknown brand: {params.brand!r}.")

    if tool_name == ToolName.GET_PROJECT_ARTICLES:
        projects = _resolve_period_projects(scope, params.period)
        if params.brand or params.publication:
            options = _available_options_for_projects(db, projects, scope.filters)
            if params.brand and params.brand not in options["brands"]:
                raise ToolValidationError(f"Unknown brand: {params.brand!r}.")
            if params.publication and params.publication not in options["publications"]:
                raise ToolValidationError(f"Unknown publication: {params.publication!r}.")
        if params.topic and params.topic not in ClassificationTaxonomy.PRIMARY_TOPICS:
            raise ToolValidationError(f"Unknown topic: {params.topic!r}.")
        if params.sentiment and params.sentiment not in ClassificationTaxonomy.SENTIMENTS:
            raise ToolValidationError(f"Unknown sentiment: {params.sentiment!r}.")
        if params.story_key:
            known = _known_story_keys(db, [p.id for p in projects])
            if params.story_key not in known:
                raise ToolValidationError(f"Unknown story_key: {params.story_key!r}.")


def validate_and_parse_tool_call(
    db: Session, scope: ChatScopeContext, tool_name: str, raw_params: dict
) -> BaseModel:
    """Validates a single tool call — unknown tool, scope-mismatched tool,
    malformed parameters, or out-of-scope entity values all raise
    `ToolValidationError`. Does not execute the tool. The overall per-run
    call-count cap is enforced structurally by `PlanSubmission`'s
    `max_length` (see app/schemas/chat.py), not here.
    """
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        raise ToolValidationError(f"Unknown tool: {tool_name!r}.")

    allowed = PROJECT_SCOPE_TOOLS if scope.kind == "project" else COMPARISON_SCOPE_TOOLS
    if tool_name not in allowed:
        raise ToolValidationError(f"Tool {tool_name!r} is not valid for scope {scope.kind!r}.")

    try:
        params = spec.params_schema.model_validate(raw_params)
    except ValidationError as exc:
        first_error = exc.errors()[0] if exc.errors() else None
        message = first_error["msg"] if first_error else str(exc)
        raise ToolValidationError(
            f"Malformed parameters for {tool_name!r}: {message}"
        ) from exc

    _validate_entity_scope(db, scope, tool_name, params)
    return params


def execute_tool_call(db: Session, scope: ChatScopeContext, tool_name: str, params: BaseModel) -> dict:
    executor = TOOL_REGISTRY[tool_name].executor
    return to_json_safe(executor(db, scope, params))
