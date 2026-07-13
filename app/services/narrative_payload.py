"""Builds the immutable, bounded evidence payload ("source snapshot") that a
narrative generation is grounded against.

Computed exactly once, at generation-creation time, from the existing Phase
4/5 analytics/comparison engines (`app/services/analytics.py`,
`app/services/comparison.py`) — never recomputed afterwards. The resulting
snapshot is persisted verbatim onto `NarrativeGeneration.source_snapshot`
(see `app/models/narrative.py`); both the n8n-facing payload endpoint and the
deterministic validator read that persisted copy, so later changes to the
underlying articles/classifications can never retroactively affect a
generation already created.

Snapshot shape (both scopes): `{"scope": "project" | "comparison", "data":
<the get_project_analytics or get_period_comparison dict>, "evidence_pool":
[...]}`. `evidence_pool` is the bounded set of representative articles a
candidate insight is allowed to cite by ID/URL — built from the same
top-ranked brands/topics/publications/stories already surfaced in `data`,
never a raw scan of the full population.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.classification import Classification
from app.models.project import Project
from app.services.analytics import (
    DEFAULT_TOP_N,
    AnalyticsFilters,
    apply_common_filters,
    get_project_analytics,
    serialize_analytics_filters,
)
from app.services.comparison import get_period_comparison
from app.services.json_safe import hash_json, to_json_safe

EVIDENCE_ARTICLES_PER_ENTITY = 3


def _evidence_base_query(project_ids: list[uuid.UUID], filters: AnalyticsFilters):
    """Multi-project equivalent of `analytics._base_query`, generalized here
    because the evidence sampler must be able to draw from more than one
    project at once (comparison-scoped generations), which the single-
    project analytics helper does not support.
    """
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


def _article_evidence_entry(article: Article) -> dict:
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


def _sample_evidence_articles(
    db: Session, project_ids: list[uuid.UUID], filters: AnalyticsFilters, analytics: dict
) -> list[dict]:
    seen_ids: set[uuid.UUID] = set()
    sampled: list[dict] = []

    def _add(article: Article) -> None:
        if article.id in seen_ids:
            return
        seen_ids.add(article.id)
        sampled.append(_article_evidence_entry(article))

    def _sample_for(entity_filter) -> None:
        stmt = _evidence_base_query(project_ids, filters).where(entity_filter).limit(
            EVIDENCE_ARTICLES_PER_ENTITY
        )
        for article, _classification in db.execute(stmt).all():
            _add(article)

    for row in analytics["brands"]["by_volume"]:
        _sample_for(Article.retailer == row["brand"])
    for row in analytics["topics"]["top_topics_by_volume"]:
        _sample_for(Classification.primary_topic == row["value"])
    for row in analytics["publications_and_stories"]["publications_by_volume"]:
        _sample_for(Article.source == row["publication"])
    for row in analytics["publications_and_stories"]["stories_by_volume"]:
        _sample_for(Classification.story_key == row["story_key"])

    for item in analytics["sentiment"]["low_confidence_items"]:
        article = db.get(Article, item["article_id"])
        if article is not None:
            _add(article)

    return sampled


def build_project_snapshot(
    db: Session,
    project: Project,
    filters: AnalyticsFilters | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    filters = filters or AnalyticsFilters()
    analytics = get_project_analytics(db, project, filters, top_n=top_n)
    evidence_pool = _sample_evidence_articles(db, [project.id], filters, analytics)
    return to_json_safe(
        {"scope": "project", "data": analytics, "evidence_pool": evidence_pool}
    )


def build_comparison_snapshot(
    db: Session,
    baseline_project_ids: list[uuid.UUID],
    comparison_project_ids: list[uuid.UUID],
    filters: AnalyticsFilters | None = None,
    top_n: int = DEFAULT_TOP_N,
    baseline_filters: AnalyticsFilters | None = None,
    comparison_filters: AnalyticsFilters | None = None,
) -> dict:
    """`baseline_filters`/`comparison_filters` (Phase E) each default to
    `filters` when omitted -- every existing call site unaffected. Each
    side's evidence is sampled against its OWN filters, so a same-project
    brand-vs-brand generation never cites an article outside the brand it
    was actually attributed to.
    """
    filters = filters or AnalyticsFilters()
    effective_baseline_filters = baseline_filters or filters
    effective_comparison_filters = comparison_filters or filters
    comparison = get_period_comparison(
        db, baseline_project_ids, comparison_project_ids, filters, top_n=top_n,
        baseline_filters=effective_baseline_filters, comparison_filters=effective_comparison_filters,
    )

    baseline_pool = _sample_evidence_articles(
        db, baseline_project_ids, effective_baseline_filters, comparison["baseline"]
    )
    comparison_pool = _sample_evidence_articles(
        db, comparison_project_ids, effective_comparison_filters, comparison["comparison"]
    )
    seen_ids = {item["article_id"] for item in baseline_pool}
    evidence_pool = baseline_pool + [
        item for item in comparison_pool if item["article_id"] not in seen_ids
    ]

    return to_json_safe(
        {"scope": "comparison", "data": comparison, "evidence_pool": evidence_pool}
    )


def compute_input_hash(
    snapshot: dict,
    language: str,
    narrative_types: list[str],
    prompt_contract_version: str,
    filters: AnalyticsFilters,
    comparison_filters: AnalyticsFilters | None = None,
) -> str:
    """SHA-256 of the exact persisted snapshot plus the request parameters
    that shape what was asked for. Identical inputs (same underlying data,
    same request) always hash identically, which is what drives the
    dedup/reuse rule in `app/services/narrative_service.py`.

    `filter_identity` is an explicit, canonical identity for the filters
    that produced `snapshot` -- not just an implicit echo via the
    snapshot's own embedded analytics data. Without it, two different
    filter sets that happen to produce identical analytics output (e.g.
    two different single-source-file filters that each match zero
    articles) would hash identically -- a latent collision, not just a
    cosmetic gap.

    `comparison_filters` (Phase E — same-project brand-vs-brand
    comparison): when the baseline and comparison sides use genuinely
    different filters, `filters` alone (the baseline side) is not enough
    to distinguish e.g. an "Auchan vs Carrefour" generation from a "Lidl
    vs Profi" one for the same project pair -- both must be folded into
    the identity. Omitted (the default) for project-scoped generations,
    which have only one filter set.
    """
    canonical_payload = {
        "snapshot": snapshot,
        "language": language,
        "narrative_types": sorted(narrative_types),
        "prompt_contract_version": prompt_contract_version,
        "filter_identity": serialize_analytics_filters(filters),
    }
    if comparison_filters is not None:
        canonical_payload["comparison_filter_identity"] = serialize_analytics_filters(comparison_filters)
    return hash_json(canonical_payload)
