"""Server-side analytics aggregation for a project's imported/classified articles.

Single source of truth consumed by both the browser Analytics tab
(`app/api/pages.py`) and the internal analytics API (`app/api/analytics.py`),
so the two surfaces can never drift apart. Also the foundation for Phase 5's
multi-project period comparisons (`app/services/comparison.py`), via
`get_period_analytics`.

Canonical population: unless documented otherwise, every figure in this module
is computed over "unique valid articles" — `Article.import_status == 'valid'
AND Article.is_duplicate == False`. The four raw "pipeline" KPI fields
(total_imported_rows, valid_rows, invalid_rows, duplicate_rows,
duplicate_share_pct) are the one exception: they always read directly from
the summed `Project.total_rows` / `valid_rows` / `invalid_rows` /
`duplicate_rows` of the project(s) in scope, and are never affected by the
analytics filters or by `top_n`.

`top_n` is presentation-only: every count/sum/percentage in this module is
computed against the complete filtered population first; `top_n` is applied
as the very last step, only to slice which already-computed ranked rows are
returned. No percentage or denominator is ever computed from a truncated list.

Rankings are deterministic: every sort uses an explicit secondary
alphabetical tiebreaker (on brand/topic/publication/story_key/title) so exact
ties never depend on incidental database fetch order.
"""

import statistics
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.classification import LOW_CONFIDENCE_THRESHOLD, Classification, ClassificationTaxonomy
from app.models.project import Project

DEFAULT_TOP_N = 10
MIN_TOP_N = 1
MAX_TOP_N = 50

VALID_STATES: tuple[str, ...] = ("all", "classified", "unclassified")

Entry = tuple[Article, Classification | None]


@dataclass(frozen=True)
class AnalyticsFilters:
    brand: str | None = None
    publication: str | None = None
    primary_topic: str | None = None
    communication_category: str | None = None
    sentiment: str | None = None
    state: str = "all"


def parse_analytics_filters(query_params) -> AnalyticsFilters:
    """Parse filters from a Starlette QueryParams (or any dict-like) mapping.

    Used identically by the UI route(s) and the internal API route(s) so
    filter semantics can never diverge between surfaces.
    """

    def _clean(name: str) -> str | None:
        value = query_params.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    state = query_params.get("state") or "all"
    if state not in VALID_STATES:
        state = "all"

    return AnalyticsFilters(
        brand=_clean("brand"),
        publication=_clean("publication"),
        primary_topic=_clean("primary_topic"),
        communication_category=_clean("communication_category"),
        sentiment=_clean("sentiment"),
        state=state,
    )


def clamp_top_n(value) -> int:
    """Forgiving top_n parser for browser routes (never raises)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TOP_N
    return max(MIN_TOP_N, min(MAX_TOP_N, n))


def _base_query(project_id: uuid.UUID, filters: AnalyticsFilters) -> Select:
    stmt = (
        select(Article, Classification)
        .outerjoin(Classification, Classification.article_id == Article.id)
        .where(
            Article.project_id == project_id,
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


def _fetch_entries(db: Session, project_id: uuid.UUID, filters: AnalyticsFilters) -> list[Entry]:
    rows = db.execute(_base_query(project_id, filters)).all()
    return [(row[0], row[1]) for row in rows]


def _deduplicate_across_projects(entries: list[Entry]) -> tuple[list[Entry], int]:
    """Collapse articles that share a fingerprint across more than one
    project (each side already deduplicated *within* its own project by
    Phase 2/4) down to a single canonical entry: earliest `created_at`, ties
    broken by `id` ascending — matching the exact convention already used by
    `app/services/imports.py::_load_seen_fingerprints`.

    Returns the deduplicated entries and how many entries were removed.
    """
    by_fingerprint: dict[str, list[Entry]] = defaultdict(list)
    for article, classification in entries:
        by_fingerprint[article.fingerprint].append((article, classification))

    deduplicated: list[Entry] = []
    removed_count = 0
    for group in by_fingerprint.values():
        if len(group) == 1:
            deduplicated.append(group[0])
            continue
        canonical = min(group, key=lambda entry: (entry[0].created_at, entry[0].id))
        deduplicated.append(canonical)
        removed_count += len(group) - 1

    return deduplicated, removed_count


def _available_filter_options(db: Session, project_ids: list[uuid.UUID]) -> dict:
    """Always computed from the full unique-valid population across the
    given project(s), ignoring every currently-active filter, so selecting
    one filter never removes options from the other dropdowns.
    """
    entries: list[Entry] = []
    for project_id in project_ids:
        entries.extend(_fetch_entries(db, project_id, AnalyticsFilters()))

    return {
        "brands": sorted({article.retailer for article, _ in entries}),
        "publications": sorted({article.source for article, _ in entries if article.source}),
        "primary_topics": sorted(
            {classification.primary_topic for _, classification in entries if classification}
        ),
        "communication_categories": sorted(
            {
                classification.communication_category
                for _, classification in entries
                if classification
            }
        ),
        "sentiments": sorted(
            {classification.sentiment for _, classification in entries if classification}
        ),
    }


def _safe_avg(total: float, count: int) -> float | None:
    if count == 0:
        return None
    return round(total / count, 1)


def _safe_pct(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 1)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 1)


def _reach_values(entries: list[Entry]) -> list[float]:
    return [article.audience for article, _ in entries if article.audience is not None]


def _compute_kpis(
    projects: list[Project], entries: list[Entry], cross_project_duplicates_excluded: int
) -> dict:
    unique_valid = len(entries)
    reach_values = _reach_values(entries)
    classified_entries = [e for e in entries if e[1] is not None]
    low_confidence_count = sum(
        1 for _, c in classified_entries if c.confidence < LOW_CONFIDENCE_THRESHOLD
    )
    publications = {article.source for article, _ in entries if article.source}

    total_imported_rows = sum(p.total_rows for p in projects)
    valid_rows = sum(p.valid_rows for p in projects)
    invalid_rows = sum(p.invalid_rows for p in projects)
    duplicate_rows = sum(p.duplicate_rows for p in projects)

    return {
        # Pipeline KPIs: always the summed project-wide totals, never
        # affected by analytics filters or by top_n.
        "total_imported_rows": total_imported_rows,
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "duplicate_rows": duplicate_rows,
        "duplicate_share_pct": _safe_pct(duplicate_rows, valid_rows),
        # Only non-zero when combining more than one project into a period
        # (Phase 5). Counted separately from duplicate_rows above, which
        # remains each project's own Phase 2 within-project duplicate count.
        "cross_project_duplicates_excluded": cross_project_duplicates_excluded,
        # Analytical KPIs: respect the active filters.
        "unique_valid_articles": unique_valid,
        "unique_classified_articles": len(classified_entries),
        "unique_unclassified_articles": unique_valid - len(classified_entries),
        "total_reach": round(sum(reach_values), 1) if reach_values else 0.0,
        "average_reach": _safe_avg(sum(reach_values), len(reach_values)),
        "median_reach": _median(reach_values),
        "reach_missing_count": unique_valid - len(reach_values),
        "publication_count": len(publications),
        "low_confidence_count": low_confidence_count,
    }


def _compute_brand_performance(entries: list[Entry], top_n: int) -> dict:
    total_unique = len(entries)
    total_reach = sum(_reach_values(entries))

    by_brand: dict[str, list[Entry]] = defaultdict(list)
    for article, classification in entries:
        by_brand[article.retailer].append((article, classification))

    brand_rows = []
    for brand, brand_entries in by_brand.items():
        reach_values = _reach_values(brand_entries)
        brand_total_reach = sum(reach_values)
        primary_focus = sum(
            1 for _, c in brand_entries if c is not None and c.brand_role == "primary_focus"
        )
        mentioned_only = sum(
            1
            for _, c in brand_entries
            if c is not None and c.brand_role in ("secondary_mention", "incidental_mention")
        )
        brand_rows.append(
            {
                "brand": brand,
                "article_count": len(brand_entries),
                "sov_pct": _safe_pct(len(brand_entries), total_unique),
                "total_reach": round(brand_total_reach, 1),
                "reach_share_pct": _safe_pct(brand_total_reach, total_reach),
                "average_reach": _safe_avg(brand_total_reach, len(reach_values)),
                "median_reach": _median(reach_values),
                "primary_focus_count": primary_focus,
                "mentioned_only_count": mentioned_only,
            }
        )

    by_volume = sorted(brand_rows, key=lambda r: (-r["article_count"], r["brand"]))
    by_reach = sorted(brand_rows, key=lambda r: (-r["total_reach"], r["brand"]))

    return {
        "by_volume": by_volume[:top_n],
        "by_reach": by_reach[:top_n],
        "brand_count": len(brand_rows),
    }


def _distribution(entries: list[Entry], key_fn, total: int) -> list[dict]:
    grouped: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        key = key_fn(entry)
        if key is None:
            continue
        grouped[key].append(entry)

    result = []
    for key, items in grouped.items():
        reach_values = _reach_values(items)
        result.append(
            {
                "value": key,
                "count": len(items),
                "pct": _safe_pct(len(items), total),
                "total_reach": round(sum(reach_values), 1) if reach_values else 0.0,
            }
        )
    return sorted(result, key=lambda r: (-r["count"], r["value"]))


def _topic_mix_by_brand(classified_entries: list[Entry]) -> list[dict]:
    matrix: dict[str, Counter] = defaultdict(Counter)
    brand_totals: dict[str, int] = defaultdict(int)
    for article, classification in classified_entries:
        matrix[article.retailer][classification.primary_topic] += 1
        brand_totals[article.retailer] += 1

    rows = []
    for brand, counter in matrix.items():
        total = brand_totals[brand]
        ranked_topics = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        rows.append(
            {
                "brand": brand,
                "topics": [
                    {"topic": topic, "count": count, "pct": _safe_pct(count, total)}
                    for topic, count in ranked_topics
                ],
            }
        )
    return sorted(rows, key=lambda r: r["brand"])


def _compute_topic_analytics(entries: list[Entry], top_n: int) -> dict:
    classified_entries = [e for e in entries if e[1] is not None]
    total_classified = len(classified_entries)

    with_secondary = [e for e in classified_entries if e[1].secondary_topic]
    without_secondary_count = total_classified - len(with_secondary)

    primary_dist = _distribution(
        classified_entries, lambda e: e[1].primary_topic, total_classified
    )
    secondary_dist = _distribution(
        with_secondary, lambda e: e[1].secondary_topic, len(with_secondary)
    )
    category_dist = _distribution(
        classified_entries, lambda e: e[1].communication_category, total_classified
    )

    return {
        "classified_count": total_classified,
        "primary_topic_distribution": primary_dist,
        "secondary_topic_distribution": secondary_dist,
        "classified_without_secondary_topic_count": without_secondary_count,
        "communication_category_distribution": category_dist,
        "topic_mix_by_brand": _topic_mix_by_brand(classified_entries),
        "top_topics_by_volume": primary_dist[:top_n],
        "top_topics_by_reach": sorted(
            primary_dist, key=lambda r: (-r["total_reach"], r["value"])
        )[:top_n],
    }


def _compute_sentiment_analytics(entries: list[Entry], top_n: int) -> dict:
    classified_entries = [e for e in entries if e[1] is not None]
    total_classified = len(classified_entries)

    overall = _distribution(classified_entries, lambda e: e[1].sentiment, total_classified)

    by_brand: dict[str, Counter] = defaultdict(Counter)
    brand_totals: dict[str, int] = defaultdict(int)
    for article, classification in classified_entries:
        by_brand[article.retailer][classification.sentiment] += 1
        brand_totals[article.retailer] += 1

    sentiment_by_brand = [
        {
            "brand": brand,
            "total": brand_totals[brand],
            "counts": {s: counter.get(s, 0) for s in ClassificationTaxonomy.SENTIMENTS},
        }
        for brand, counter in sorted(by_brand.items())
    ]

    brand_role_counts = Counter(c.brand_role for _, c in classified_entries)
    primary_focus = brand_role_counts.get("primary_focus", 0)
    mentioned_only = sum(
        brand_role_counts.get(role, 0) for role in ("secondary_mention", "incidental_mention")
    )

    low_confidence_entries = [
        (article, classification)
        for article, classification in classified_entries
        if classification.confidence < LOW_CONFIDENCE_THRESHOLD
    ]
    low_confidence_sorted = sorted(
        low_confidence_entries, key=lambda e: (e[1].confidence, e[0].title or "")
    )

    return {
        "overall_distribution": overall,
        "sentiment_by_brand": sentiment_by_brand,
        "brand_role_distribution": [
            {
                "value": role,
                "count": brand_role_counts.get(role, 0),
                "pct": _safe_pct(brand_role_counts.get(role, 0), total_classified),
            }
            for role in ClassificationTaxonomy.BRAND_ROLES
        ],
        "primary_focus_vs_mentioned_only": {
            "primary_focus": primary_focus,
            "mentioned_only": mentioned_only,
            "primary_focus_pct": _safe_pct(primary_focus, total_classified),
            "mentioned_only_pct": _safe_pct(mentioned_only, total_classified),
        },
        "low_confidence_total_count": len(low_confidence_entries),
        "low_confidence_items": [
            {
                "article_id": article.id,
                "title": article.title,
                "brand": article.retailer,
                "primary_topic": classification.primary_topic,
                "confidence": classification.confidence,
            }
            for article, classification in low_confidence_sorted[:top_n]
        ],
    }


def _concentration_pct(ranked_rows: list[dict], n: int, value_key: str, total: float) -> float:
    top_sum = sum(row[value_key] for row in ranked_rows[:n])
    return _safe_pct(top_sum, total)


def _compute_publications_and_stories(entries: list[Entry], top_n: int) -> dict:
    total_unique = len(entries)
    total_reach = sum(_reach_values(entries))

    pub_map: dict[str, list[Entry]] = defaultdict(list)
    for article, classification in entries:
        if article.source:
            pub_map[article.source].append((article, classification))

    pub_rows = []
    for source, pub_entries in pub_map.items():
        reach_values = _reach_values(pub_entries)
        pub_rows.append(
            {
                "publication": source,
                "article_count": len(pub_entries),
                "volume_pct": _safe_pct(len(pub_entries), total_unique),
                "total_reach": round(sum(reach_values), 1) if reach_values else 0.0,
                "reach_pct": _safe_pct(sum(reach_values), total_reach),
            }
        )

    pubs_by_volume = sorted(pub_rows, key=lambda r: (-r["article_count"], r["publication"]))
    pubs_by_reach = sorted(pub_rows, key=lambda r: (-r["total_reach"], r["publication"]))

    publication_concentration = {
        "top3_volume_pct": _concentration_pct(pubs_by_volume, 3, "article_count", total_unique),
        "top5_volume_pct": _concentration_pct(pubs_by_volume, 5, "article_count", total_unique),
        "top3_reach_pct": _concentration_pct(pubs_by_reach, 3, "total_reach", total_reach),
        "top5_reach_pct": _concentration_pct(pubs_by_reach, 5, "total_reach", total_reach),
    }

    # Story clustering is classified-only, and only for articles with a
    # non-null story_key. See module docstring for why this population is
    # kept distinct from "all unique valid articles."
    classified_entries = [e for e in entries if e[1] is not None]
    with_story_key = [e for e in classified_entries if e[1].story_key]
    without_story_key_count = len(classified_entries) - len(with_story_key)

    story_map: dict[str, list[Entry]] = defaultdict(list)
    for article, classification in with_story_key:
        story_map[classification.story_key].append((article, classification))

    story_rows = []
    for story_key, story_entries in story_map.items():
        reach_values = _reach_values(story_entries)
        story_rows.append(
            {
                "story_key": story_key,
                "article_count": len(story_entries),
                "total_reach": round(sum(reach_values), 1) if reach_values else 0.0,
            }
        )

    total_story_articles = len(with_story_key)
    total_story_reach = sum(_reach_values(with_story_key))

    stories_by_volume = sorted(story_rows, key=lambda r: (-r["article_count"], r["story_key"]))
    stories_by_reach = sorted(story_rows, key=lambda r: (-r["total_reach"], r["story_key"]))

    story_concentration = {
        "top3_volume_pct": _concentration_pct(
            stories_by_volume, 3, "article_count", total_story_articles
        ),
        "top5_volume_pct": _concentration_pct(
            stories_by_volume, 5, "article_count", total_story_articles
        ),
        "top3_reach_pct": _concentration_pct(
            stories_by_reach, 3, "total_reach", total_story_reach
        ),
        "top5_reach_pct": _concentration_pct(
            stories_by_reach, 5, "total_reach", total_story_reach
        ),
    }

    return {
        "publications_by_volume": pubs_by_volume[:top_n],
        "publications_by_reach": pubs_by_reach[:top_n],
        "publication_concentration": publication_concentration,
        "stories_by_volume": stories_by_volume[:top_n],
        "stories_by_reach": stories_by_reach[:top_n],
        "story_concentration": story_concentration,
        "classified_with_story_key_count": len(with_story_key),
        "classified_without_story_key_count": without_story_key_count,
        "unique_story_cluster_count": len(story_map),
    }


def get_period_analytics(
    db: Session,
    projects: list[Project],
    filters: AnalyticsFilters | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Generalized, multi-project form of the Phase 4 analytics engine.

    A "period" is the union of one or more projects' unique-valid articles.
    For a single project this is exactly Phase 4's original computation. For
    more than one, a cross-project fingerprint dedup pass runs first (see
    `_deduplicate_across_projects`) so the same underlying article imported
    into two projects is never double-counted.
    """
    if not projects:
        raise ValueError("get_period_analytics requires at least one project.")

    filters = filters or AnalyticsFilters()
    top_n = max(MIN_TOP_N, min(MAX_TOP_N, top_n))

    all_entries: list[Entry] = []
    for project in projects:
        all_entries.extend(_fetch_entries(db, project.id, filters))

    if len(projects) > 1:
        entries, cross_project_duplicates_excluded = _deduplicate_across_projects(all_entries)
    else:
        entries, cross_project_duplicates_excluded = all_entries, 0

    return {
        "project_ids": [p.id for p in projects],
        "filters": {
            "brand": filters.brand,
            "publication": filters.publication,
            "primary_topic": filters.primary_topic,
            "communication_category": filters.communication_category,
            "sentiment": filters.sentiment,
            "state": filters.state,
        },
        "available_filter_options": _available_filter_options(db, [p.id for p in projects]),
        "top_n": top_n,
        "kpis": _compute_kpis(projects, entries, cross_project_duplicates_excluded),
        "brands": _compute_brand_performance(entries, top_n),
        "topics": _compute_topic_analytics(entries, top_n),
        "sentiment": _compute_sentiment_analytics(entries, top_n),
        "publications_and_stories": _compute_publications_and_stories(entries, top_n),
    }


def get_project_analytics(
    db: Session,
    project: Project,
    filters: AnalyticsFilters | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Phase 4's original single-project entrypoint. Thin wrapper over
    `get_period_analytics([project])`, reshaped back to the exact response
    shape Phase 4 shipped (`project_id`, not `project_ids`) so every existing
    caller and test is completely unaffected.
    """
    result = get_period_analytics(db, [project], filters, top_n)
    result = {**result, "project_id": project.id}
    del result["project_ids"]
    return result
