"""The deterministic data-mapping layer for Phase 6C report exports.

The only place report generation calls into analytics/comparison/narrative
services. `build_project_report_data`/`build_comparison_report_data` each
call `get_project_analytics`/`get_period_comparison` exactly once, at the
system-max ranking (`MAX_TOP_N`), and retain the full result — the PPTX and
XLSX builders apply their own, smaller, documented display caps on that one
retained result; neither ever re-queries at a different `top_n`.

Snapshot consistency is a real Postgres guarantee, not an assumption: each
builder issues `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`
as the first statement on the request's session (Postgres requires this to
precede any other query), so every read in one call sees one stable
snapshot. This guarantees only that *one export* (one HTTP request, one
call to one of these functions) is internally consistent — a PPTX and an
XLSX requested as two separate HTTP requests do not share a snapshot; they
share the same deterministic builder and mapping contract, so they match
each other whenever the underlying data hasn't changed between the two
requests (see the Phase 6C plan for the exact wording of this guarantee).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.classification import Classification
from app.models.narrative import NarrativeInsight, NarrativeValidationStatus
from app.models.project import Project
from app.services.analytics import MAX_TOP_N, AnalyticsFilters, get_project_analytics
from app.services.chat_contract import CAUSAL_LANGUAGE_MARKERS
from app.services.chat_tools import ChatScopeContext, find_latest_matching_generation
from app.services.comparison import get_period_comparison
from app.services.report_contract import (
    CHAT_EXCLUSION_NOTE,
    MAX_ARTICLE_DETAIL_ROWS,
    MAX_INSIGHTS_PER_REPORT,
    POPULATION_DEFINITION,
)


class ReportNotFoundError(Exception):
    pass


class ReportTooLargeError(Exception):
    pass


@dataclass
class ReportInsight:
    id: uuid.UUID
    narrative_type: str
    title: str
    narrative: str
    related_brand: str | None
    related_topic: str | None
    related_publication: str | None
    related_story_key: str | None
    confidence: float | None
    caveat: str | None
    label: str  # "Interpretation" | "Recommendation"


@dataclass
class ArticleDetailRow:
    article_id: uuid.UUID
    title: str | None
    brand: str
    publication: str | None
    publication_date: date | None
    primary_topic: str | None
    communication_category: str | None
    sentiment: str | None
    brand_role: str | None
    confidence: float | None
    reach: float | None
    article_url: str | None
    mediatrust_url: str | None
    period: str | None = None  # "Baseline" | "Comparison" — comparison exports only


@dataclass
class SectionCoverage:
    shown_count: int
    total_count: int

    @property
    def truncated(self) -> bool:
        return self.total_count > self.shown_count


@dataclass
class InsightCoverage:
    available_count: int
    included_count: int
    excluded_causal_count: int


@dataclass
class ReportMetadata:
    scope_label: str
    filters_label: str
    generated_at: datetime
    population_definition: str
    chat_exclusion_note: str
    article_detail_coverage: SectionCoverage
    insight_coverage: InsightCoverage


@dataclass
class ProjectReportData:
    project_name: str
    project_quarter: str
    filters: AnalyticsFilters
    analytics: dict
    insights: list[ReportInsight]
    article_detail: list[ArticleDetailRow]
    metadata: ReportMetadata


@dataclass
class ComparisonReportData:
    baseline_label: str
    comparison_label: str
    filters: AnalyticsFilters
    comparison: dict
    insights: list[ReportInsight]
    article_detail: list[ArticleDetailRow]
    metadata: ReportMetadata


def _describe_filters(filters: AnalyticsFilters) -> str:
    parts: list[str] = []
    if filters.brand:
        parts.append(f"Brand: {filters.brand}")
    if filters.publication:
        parts.append(f"Publication: {filters.publication}")
    if filters.primary_topic:
        parts.append(f"Primary topic: {filters.primary_topic}")
    if filters.communication_category:
        parts.append(f"Category: {filters.communication_category}")
    if filters.sentiment:
        parts.append(f"Sentiment: {filters.sentiment}")
    if filters.state and filters.state != "all":
        parts.append(f"State: {filters.state}")
    return "; ".join(parts) if parts else "None"


def _label_for_narrative_type(narrative_type: str) -> str:
    return "Recommendation" if narrative_type == "recommendations" else "Interpretation"


def _contains_causal_language(text_value: str) -> bool:
    lowered = text_value.lower()
    return any(marker in lowered for marker in CAUSAL_LANGUAGE_MARKERS)


def _collect_insights(
    db: Session, scope: ChatScopeContext
) -> tuple[list[ReportInsight], InsightCoverage]:
    """Only `validation_status="valid"` insights are ever candidates. A
    report-layer-only, read-only causal-language scan (reusing
    `chat_contract.CAUSAL_LANGUAGE_MARKERS` verbatim) additionally excludes
    any insight whose text reads as a causal claim — this never mutates the
    underlying `NarrativeInsight` row; it only decides whether this report
    embeds it.
    """
    generation = find_latest_matching_generation(db, scope)
    if generation is None:
        return [], InsightCoverage(available_count=0, included_count=0, excluded_causal_count=0)

    stmt = (
        select(NarrativeInsight)
        .where(
            NarrativeInsight.generation_id == generation.id,
            NarrativeInsight.validation_status == NarrativeValidationStatus.VALID,
        )
        .order_by(NarrativeInsight.created_at)
    )
    rows = list(db.execute(stmt).scalars().all())

    included: list[ReportInsight] = []
    excluded_causal = 0
    for row in rows:
        if _contains_causal_language(row.narrative):
            excluded_causal += 1
            continue
        if len(included) >= MAX_INSIGHTS_PER_REPORT:
            continue
        included.append(
            ReportInsight(
                id=row.id,
                narrative_type=row.narrative_type,
                title=row.title,
                narrative=row.narrative,
                related_brand=row.related_brand,
                related_topic=row.related_topic,
                related_publication=row.related_publication,
                related_story_key=row.related_story_key,
                confidence=row.confidence,
                caveat=row.caveat,
                label=_label_for_narrative_type(row.narrative_type),
            )
        )

    return included, InsightCoverage(
        available_count=len(rows),
        included_count=len(included),
        excluded_causal_count=excluded_causal,
    )


def _article_detail_query(project_ids: list[uuid.UUID], filters: AnalyticsFilters):
    """Same filter-application shape as `analytics._base_query`/
    `narrative_payload._evidence_base_query`/`chat_tools._articles_query`
    — duplicated here rather than reached into, matching the established
    precedent from both prior phases rather than introducing a new
    cross-module reach-in.
    """
    stmt = (
        select(Article, Classification)
        .outerjoin(Classification, Classification.article_id == Article.id)
        .where(
            Article.project_id.in_(project_ids),
            Article.import_status == ImportStatus.VALID,
            Article.is_duplicate.is_(False),
        )
    )
    if filters.brand:
        stmt = stmt.where(Article.retailer == filters.brand)
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


def _collect_article_detail(
    db: Session,
    project_ids: list[uuid.UUID],
    filters: AnalyticsFilters,
    period: str | None = None,
) -> tuple[list[ArticleDetailRow], int]:
    base = _article_detail_query(project_ids, filters)

    total_count = db.execute(base.with_only_columns(func.count())).scalar_one()

    rows = db.execute(base.order_by(Article.id).limit(MAX_ARTICLE_DETAIL_ROWS)).all()
    detail = [
        ArticleDetailRow(
            article_id=article.id,
            title=article.title,
            brand=article.retailer,
            publication=article.source,
            publication_date=article.publication_date,
            primary_topic=classification.primary_topic if classification else None,
            communication_category=classification.communication_category if classification else None,
            sentiment=classification.sentiment if classification else None,
            brand_role=classification.brand_role if classification else None,
            confidence=classification.confidence if classification else None,
            reach=article.audience,
            article_url=article.article_url,
            mediatrust_url=article.mediatrust_url,
            period=period,
        )
        for article, classification in rows
    ]
    return detail, total_count


def build_project_report_data(
    db: Session, project_id: uuid.UUID, filters: AnalyticsFilters | None = None
) -> ProjectReportData:
    filters = filters or AnalyticsFilters()

    # Must be the first statement on this session — Postgres requires SET
    # TRANSACTION ISOLATION LEVEL to precede any other query in the
    # transaction. Nothing may run on `db` before this call.
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

    project = db.get(Project, project_id)
    if project is None:
        raise ReportNotFoundError(f"Project {project_id} not found.")

    analytics = get_project_analytics(db, project, filters, top_n=MAX_TOP_N)

    scope = ChatScopeContext(
        kind="project",
        project=project,
        baseline_projects=None,
        comparison_projects=None,
        filters=filters,
    )
    insights, insight_coverage = _collect_insights(db, scope)
    article_detail, total_articles = _collect_article_detail(db, [project.id], filters)

    metadata = ReportMetadata(
        scope_label=f"{project.name} ({project.quarter})",
        filters_label=_describe_filters(filters),
        generated_at=datetime.now(timezone.utc),
        population_definition=POPULATION_DEFINITION,
        chat_exclusion_note=CHAT_EXCLUSION_NOTE,
        article_detail_coverage=SectionCoverage(
            shown_count=len(article_detail), total_count=total_articles
        ),
        insight_coverage=insight_coverage,
    )

    return ProjectReportData(
        project_name=project.name,
        project_quarter=project.quarter,
        filters=filters,
        analytics=analytics,
        insights=insights,
        article_detail=article_detail,
        metadata=metadata,
    )


def build_comparison_report_data(
    db: Session,
    baseline_project_ids: list[uuid.UUID],
    comparison_project_ids: list[uuid.UUID],
    filters: AnalyticsFilters | None = None,
) -> ComparisonReportData:
    filters = filters or AnalyticsFilters()

    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

    # Raises ComparisonServiceError for a missing/empty selection — left to
    # propagate to the API layer, exactly like /api/internal/compare.
    comparison = get_period_comparison(
        db, baseline_project_ids, comparison_project_ids, filters, top_n=MAX_TOP_N
    )

    unique_baseline_ids = list(dict.fromkeys(baseline_project_ids))
    unique_comparison_ids = list(dict.fromkeys(comparison_project_ids))
    baseline_projects = [db.get(Project, pid) for pid in unique_baseline_ids]
    comparison_projects = [db.get(Project, pid) for pid in unique_comparison_ids]

    scope = ChatScopeContext(
        kind="comparison",
        project=None,
        baseline_projects=baseline_projects,
        comparison_projects=comparison_projects,
        filters=filters,
    )
    insights, insight_coverage = _collect_insights(db, scope)

    baseline_detail, baseline_total = _collect_article_detail(
        db, unique_baseline_ids, filters, period="Baseline"
    )
    comparison_detail, comparison_total = _collect_article_detail(
        db, unique_comparison_ids, filters, period="Comparison"
    )
    article_detail = baseline_detail + comparison_detail
    total_articles = baseline_total + comparison_total

    metadata = ReportMetadata(
        scope_label=f"{comparison['baseline']['label']} vs {comparison['comparison']['label']}",
        filters_label=_describe_filters(filters),
        generated_at=datetime.now(timezone.utc),
        population_definition=POPULATION_DEFINITION,
        chat_exclusion_note=CHAT_EXCLUSION_NOTE,
        article_detail_coverage=SectionCoverage(
            shown_count=len(article_detail), total_count=total_articles
        ),
        insight_coverage=insight_coverage,
    )

    return ComparisonReportData(
        baseline_label=comparison["baseline"]["label"],
        comparison_label=comparison["comparison"]["label"],
        filters=filters,
        comparison=comparison,
        insights=insights,
        article_detail=article_detail,
        metadata=metadata,
    )
