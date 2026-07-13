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

Needs-review semantics (Phase D — reporting-scope): an article with
`retailer_review_status == 'needs_review'` has no confirmed canonical
brand. It is never represented as a fake brand ("Unknown"/"Needs
Review"/etc.) anywhere. The rule, applied consistently:

- Project-level totals (`_compute_kpis` — unique valid articles, reach,
  publication count, ...) always include needs-review articles, because
  they ARE otherwise-valid imported coverage; excluding them would
  understate "how much was actually imported."
- Every BRAND-KEYED view (brand rankings/shares, topic-mix-by-brand,
  sentiment-by-brand) always EXCLUDES needs-review articles from its own
  grouping and denominators, because they cannot be attributed to any
  brand — including them would either require inventing a pseudo-brand
  bucket (never done) or silently understate every real brand's share
  for a reason that has nothing to do with competitive dynamics.
- `AnalyticsFilters.include_needs_review` is an explicit, separate flag
  used only to add needs-review rows into (or isolate them within) the
  filtered *population* the request is asking about — it never overrides
  the brand-keyed exclusion rule above.

Future phases must not reinterpret this: needs-review is a *data-quality
state*, never a brand.

The filter contract itself (`AnalyticsFilters`, its parser/serializer, and
the shared `apply_common_filters` WHERE-clause helper) lives in
`app.services.analytics_filters`, a leaf module with no dependency on this
one -- `report_data.py`/`chat_tools.py`/`narrative_payload.py` all need it
too, and keeping it here would create an import cycle. This module
re-exports it unchanged for backward compatibility.
"""

import statistics
import uuid
from collections import Counter, defaultdict

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus, RetailerReviewStatus
from app.models.classification import LOW_CONFIDENCE_THRESHOLD, Classification, ClassificationTaxonomy
from app.models.project import Project
from app.models.uploaded_file import UploadedFile
from app.services.analytics_filters import (  # noqa: F401 -- re-exported for backward compatibility
    AnalyticsFilterError,
    AnalyticsFilters,
    apply_common_filters,
    parse_analytics_filters,
    serialize_analytics_filters,
)

DEFAULT_TOP_N = 10
MIN_TOP_N = 1
MAX_TOP_N = 50

Entry = tuple[Article, Classification | None]


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


def _fetch_entries(db: Session, project_id: uuid.UUID, filters: AnalyticsFilters) -> list[Entry]:
    rows = db.execute(_base_query(project_id, filters)).all()
    return [(row[0], row[1]) for row in rows]


def _exclude_needs_review(entries: list[Entry]) -> list[Entry]:
    """Every brand-keyed view calls this on its own input before grouping
    by `article.retailer` — see the module docstring's needs-review
    semantics. Never applied to project-level totals.
    """
    return [
        entry for entry in entries if entry[0].retailer_review_status != RetailerReviewStatus.NEEDS_REVIEW
    ]


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

    `analytics_needs_review_count` reflects needs-review rows within the
    unique-valid analytical population (`import_status == VALID AND
    is_duplicate == False`) — NOT the full Review-tab backlog, which may
    be larger whenever an invalid/duplicate row is also needs-review (see
    `review.py::count_needs_review`, matched exactly by
    `get_period_analytics`'s separate `review_backlog_count` KPI).
    """
    entries: list[Entry] = []
    for project_id in project_ids:
        entries.extend(_fetch_entries(db, project_id, AnalyticsFilters()))

    # Never a selectable "brand" — needs-review rows have no confirmed
    # canonical brand (see module docstring).
    brand_eligible_entries = _exclude_needs_review(entries)
    analytics_needs_review_count = len(entries) - len(brand_eligible_entries)

    # One extra query, not per-entry — every file belonging to the
    # project(s) is listed regardless of whether it currently has any
    # unique-valid articles, including legacy files with no ImportBatch.
    uploaded_files = (
        db.execute(
            select(UploadedFile.id, UploadedFile.original_filename)
            .where(UploadedFile.project_id.in_(project_ids))
            .order_by(UploadedFile.original_filename.asc())
        )
        .all()
    )

    return {
        "brands": sorted({article.retailer for article, _ in brand_eligible_entries}),
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
        "source_files": [
            {"id": row.id, "original_filename": row.original_filename} for row in uploaded_files
        ],
        "analytics_needs_review_count": analytics_needs_review_count,
    }


def get_available_filter_options(db: Session, project_ids: list[uuid.UUID]) -> dict:
    """Public accessor for `_available_filter_options` — used by
    `comparison.py::_merge_filter_options` to recompute a correct,
    non-double-counted `analytics_needs_review_count` over the unique
    union of both comparison sides' project ids, rather than blindly
    summing each side's independently-computed count (which double-counts
    whenever the two sides share a project).
    """
    return _available_filter_options(db, project_ids)


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
    # Needs-review rows are otherwise-valid imported coverage and stay
    # counted in every total above them — this count exists purely to
    # make the gap between "total" and "ranked by brand" explicit, never
    # to imply they should be subtracted from unique_valid_articles.
    # Scoped to the CURRENTLY FILTERED population (unlike
    # `review_backlog_count`/`analytics_needs_review_count`, see
    # `get_period_analytics`/`_available_filter_options`).
    current_view_needs_review_count = sum(
        1 for article, _ in entries if article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW
    )

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
        "current_view_needs_review_count": current_view_needs_review_count,
    }


def _compute_brand_performance(entries: list[Entry], top_n: int) -> dict:
    # Needs-review rows have no confirmed canonical brand — never ranked,
    # never counted toward any brand's SOV/reach-share denominator (see
    # module docstring). They remain fully counted in `_compute_kpis`.
    brand_eligible = _exclude_needs_review(entries)
    total_unique = len(brand_eligible)
    total_reach = sum(_reach_values(brand_eligible))

    by_brand: dict[str, list[Entry]] = defaultdict(list)
    for article, classification in brand_eligible:
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
    # Brand-keyed — needs-review rows excluded (see module docstring).
    matrix: dict[str, Counter] = defaultdict(Counter)
    brand_totals: dict[str, int] = defaultdict(int)
    for article, classification in _exclude_needs_review(classified_entries):
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

    # Brand-keyed — needs-review rows excluded (see module docstring);
    # `overall` above intentionally stays computed over every classified
    # entry, needs-review included, since it's not brand-keyed.
    by_brand: dict[str, Counter] = defaultdict(Counter)
    brand_totals: dict[str, int] = defaultdict(int)
    for article, classification in _exclude_needs_review(classified_entries):
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


def _review_backlog_count(db: Session, project_ids: list[uuid.UUID]) -> int:
    """Every `retailer_review_status == 'needs_review'` row for the given
    project(s), with NO other restriction — matches
    `app.services.review.count_needs_review` exactly (confirmed by direct
    read of that module: it filters only on `project_id` and
    `retailer_review_status`, never `import_status`/`is_duplicate`). This
    is the true Review-tab backlog, a superset of
    `available_filter_options["analytics_needs_review_count"]` whenever an
    invalid/duplicate row is also needs-review.
    """
    stmt = select(func.count(Article.id)).where(
        Article.project_id.in_(project_ids),
        Article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW,
    )
    return db.execute(stmt).scalar_one()


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

    project_ids = [p.id for p in projects]
    kpis = _compute_kpis(projects, entries, cross_project_duplicates_excluded)
    # The true Review-tab backlog (matches `review.py::count_needs_review`
    # exactly — no import_status/is_duplicate restriction), distinct from
    # `current_view_needs_review_count` above (scoped to the currently
    # filtered unique-valid population) and from
    # `available_filter_options["analytics_needs_review_count"]` (scoped
    # to the full unique-valid population, ignoring active filters).
    kpis["review_backlog_count"] = _review_backlog_count(db, project_ids)

    return {
        "project_ids": project_ids,
        "filters": {
            "brand": filters.brand,
            "brands": list(filters.brands),
            "uploaded_file_ids": [str(u) for u in filters.uploaded_file_ids],
            "include_needs_review": filters.include_needs_review,
            "publication": filters.publication,
            "primary_topic": filters.primary_topic,
            "communication_category": filters.communication_category,
            "sentiment": filters.sentiment,
            "state": filters.state,
        },
        "available_filter_options": _available_filter_options(db, project_ids),
        "top_n": top_n,
        "kpis": kpis,
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
