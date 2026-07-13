"""Cross-period comparison built on top of the Phase 4/5 analytics engine.

A "period" is the union of one or more project(s)' unique-valid articles —
see `app/services/analytics.py::get_period_analytics` for exactly how that
union and its cross-project deduplication work. This module computes deltas
between two such periods (baseline and comparison), reusing that same engine
for both sides so the numbers feeding a delta are always identical to what a
user would see looking at either period on its own — the UI and the internal
API both call `get_period_comparison` directly, so they cannot drift apart.

Ranking deltas (brands, publications, stories) are always computed against
the full, system-capped ranking (`MAX_TOP_N`), never against a display-
truncated list — otherwise an entity ranked just outside a small `top_n`
would misleadingly show up as a "new entrant" instead of a real rank change.
`top_n` only truncates which rows are returned for display, exactly like in
Phase 4.
"""

import uuid

from sqlalchemy.orm import Session

from app.models.project import Project
from app.services.analytics import (
    DEFAULT_TOP_N,
    MAX_TOP_N,
    MIN_TOP_N,
    AnalyticsFilters,
    get_available_filter_options,
    get_period_analytics,
)


class ComparisonServiceError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _resolve_projects(db: Session, project_ids: list[uuid.UUID], label: str) -> list[Project]:
    unique_ids = list(dict.fromkeys(project_ids))  # de-dupe, preserve order
    if not unique_ids:
        raise ComparisonServiceError(f"{label} must include at least one project.", 422)

    projects = []
    for project_id in unique_ids:
        project = db.get(Project, project_id)
        if project is None:
            raise ComparisonServiceError(f"{label}: project {project_id} not found.", 404)
        projects.append(project)
    return projects


def derive_period_label(projects: list[Project]) -> str:
    """Purely cosmetic label derived from the selected projects' quarters.

    Never gates which projects can be combined — always falls back to a
    generic listing when the selection doesn't form a clean single-quarter /
    H1 / H2 / FY pattern.
    """
    parsed: list[tuple[int, int]] | None = []
    for project in projects:
        try:
            year_str, q_str = project.quarter.split("-Q")
            parsed.append((int(year_str), int(q_str)))
        except (ValueError, AttributeError):
            parsed = None
            break

    if not parsed:
        return f"{len(projects)} project" + ("s" if len(projects) != 1 else "")

    if len(parsed) == 1:
        year, quarter = parsed[0]
        return f"Q{quarter} {year}"

    years = {year for year, _ in parsed}
    quarters = {quarter for _, quarter in parsed}

    if len(years) == 1:
        year = next(iter(years))
        if len(parsed) == 2 and quarters == {1, 2}:
            return f"H1 {year}"
        if len(parsed) == 2 and quarters == {3, 4}:
            return f"H2 {year}"
        if len(parsed) == 4 and quarters == {1, 2, 3, 4}:
            return f"FY {year}"

    quarter_labels = ", ".join(sorted(f"{y}-Q{q}" for y, q in parsed))
    return f"{len(projects)} projects ({quarter_labels})"


MAX_LABEL_BRANDS = 2


def _label_for_filters(filters: AnalyticsFilters) -> str:
    """Brand-list-aware label for one side of a same-project brand-vs-brand
    comparison (Phase E), where `derive_period_label` would otherwise
    collapse both sides to the same quarter label (e.g. "Q2 2026 vs Q2
    2026", giving no hint a brand comparison happened).
    """
    if filters.brands:
        sorted_brands = sorted(filters.brands)
        if len(sorted_brands) <= MAX_LABEL_BRANDS:
            return " + ".join(sorted_brands)
        return f"{len(sorted_brands)} brands"
    if filters.uploaded_file_ids:
        return f"{len(filters.uploaded_file_ids)} source file(s)"
    return "Selection"


def _period_side_label(
    projects: list[Project], other_projects: list[Project], filters: AnalyticsFilters, other_filters: AnalyticsFilters
) -> str:
    """Same-project-both-sides with genuinely different filters (Phase E
    brand-vs-brand comparison): use a filter-derived label per side instead
    of the identical quarter label both sides would otherwise share. Every
    other case (disjoint projects, or same projects with identical
    filters) is completely unchanged.
    """
    same_projects = {p.id for p in projects} == {p.id for p in other_projects}
    if same_projects and filters != other_filters:
        return _label_for_filters(filters)
    return derive_period_label(projects)


def _reslice_for_display(period: dict, top_n: int) -> dict:
    """`period` was computed at MAX_TOP_N so ranking deltas see the full
    picture; this trims only the *returned* ranked lists down to the
    caller's requested top_n for display, leaving every KPI, distribution,
    and concentration figure untouched.
    """
    result = dict(period)
    result["top_n"] = top_n
    result["brands"] = {
        **period["brands"],
        "by_volume": period["brands"]["by_volume"][:top_n],
        "by_reach": period["brands"]["by_reach"][:top_n],
    }
    result["topics"] = {
        **period["topics"],
        "top_topics_by_volume": period["topics"]["top_topics_by_volume"][:top_n],
        "top_topics_by_reach": period["topics"]["top_topics_by_reach"][:top_n],
    }
    result["sentiment"] = {
        **period["sentiment"],
        "low_confidence_items": period["sentiment"]["low_confidence_items"][:top_n],
    }
    result["publications_and_stories"] = {
        **period["publications_and_stories"],
        "publications_by_volume": period["publications_and_stories"]["publications_by_volume"][
            :top_n
        ],
        "publications_by_reach": period["publications_and_stories"]["publications_by_reach"][
            :top_n
        ],
        "stories_by_volume": period["publications_and_stories"]["stories_by_volume"][:top_n],
        "stories_by_reach": period["publications_and_stories"]["stories_by_reach"][:top_n],
    }
    return result


def _safe_pct_delta(baseline: float, comparison: float) -> float | None:
    """Percentage *change* (not percentage points). Undefined when the
    baseline is zero and the comparison isn't — reported as None rather than
    a fabricated huge number. Zero-to-zero is a real, reportable 0.0% change.
    """
    if baseline == 0:
        return 0.0 if comparison == 0 else None
    return round((comparison - baseline) / baseline * 100, 1)


_KPI_DELTA_FIELDS: tuple[str, ...] = (
    "total_imported_rows",
    "valid_rows",
    "invalid_rows",
    "duplicate_rows",
    "duplicate_share_pct",
    "cross_project_duplicates_excluded",
    "unique_valid_articles",
    "unique_classified_articles",
    "unique_unclassified_articles",
    "total_reach",
    "average_reach",
    "median_reach",
    "publication_count",
    "low_confidence_count",
)


def _kpi_deltas(baseline_kpis: dict, comparison_kpis: dict) -> dict:
    deltas = {}
    for field in _KPI_DELTA_FIELDS:
        baseline_value = baseline_kpis.get(field)
        comparison_value = comparison_kpis.get(field)
        if baseline_value is None or comparison_value is None:
            deltas[field] = {
                "baseline": baseline_value,
                "comparison": comparison_value,
                "absolute_delta": None,
                "percentage_delta": None,
            }
            continue
        deltas[field] = {
            "baseline": baseline_value,
            "comparison": comparison_value,
            "absolute_delta": round(comparison_value - baseline_value, 1),
            "percentage_delta": _safe_pct_delta(baseline_value, comparison_value),
        }
    return deltas


def _brand_deltas(baseline_by_volume: list[dict], comparison_by_volume: list[dict]) -> list[dict]:
    baseline_by_name = {row["brand"]: row for row in baseline_by_volume}
    comparison_by_name = {row["brand"]: row for row in comparison_by_volume}
    baseline_rank = {row["brand"]: i + 1 for i, row in enumerate(baseline_by_volume)}
    comparison_rank = {row["brand"]: i + 1 for i, row in enumerate(comparison_by_volume)}

    all_brands = sorted(set(baseline_by_name) | set(comparison_by_name))

    rows = []
    for brand in all_brands:
        baseline_row = baseline_by_name.get(brand)
        comparison_row = comparison_by_name.get(brand)
        baseline_sov = baseline_row["sov_pct"] if baseline_row else 0.0
        comparison_sov = comparison_row["sov_pct"] if comparison_row else 0.0
        baseline_reach_share = baseline_row["reach_share_pct"] if baseline_row else 0.0
        comparison_reach_share = comparison_row["reach_share_pct"] if comparison_row else 0.0
        b_rank = baseline_rank.get(brand)
        c_rank = comparison_rank.get(brand)

        rows.append(
            {
                "brand": brand,
                "baseline_sov_pct": baseline_sov,
                "comparison_sov_pct": comparison_sov,
                "sov_delta_pp": round(comparison_sov - baseline_sov, 1),
                "baseline_reach_share_pct": baseline_reach_share,
                "comparison_reach_share_pct": comparison_reach_share,
                "reach_share_delta_pp": round(comparison_reach_share - baseline_reach_share, 1),
                "baseline_rank": b_rank,
                "comparison_rank": c_rank,
                "rank_change": (b_rank - c_rank) if (b_rank and c_rank) else None,
                "is_new_entrant": baseline_row is None and comparison_row is not None,
                "is_dropout": baseline_row is not None and comparison_row is None,
            }
        )

    return sorted(rows, key=lambda r: (-r["comparison_sov_pct"], r["brand"]))


def _distribution_deltas(baseline_dist: list[dict], comparison_dist: list[dict]) -> dict:
    baseline_by_value = {row["value"]: row for row in baseline_dist}
    comparison_by_value = {row["value"]: row for row in comparison_dist}
    all_values = sorted(set(baseline_by_value) | set(comparison_by_value))

    rows = []
    for value in all_values:
        baseline_row = baseline_by_value.get(value)
        comparison_row = comparison_by_value.get(value)
        baseline_pct = baseline_row["pct"] if baseline_row else 0.0
        comparison_pct = comparison_row["pct"] if comparison_row else 0.0
        rows.append(
            {
                "value": value,
                "baseline_count": baseline_row["count"] if baseline_row else 0,
                "comparison_count": comparison_row["count"] if comparison_row else 0,
                "baseline_pct": baseline_pct,
                "comparison_pct": comparison_pct,
                "pct_delta_pp": round(comparison_pct - baseline_pct, 1),
                "is_new": baseline_row is None and comparison_row is not None,
                "is_gone": baseline_row is not None and comparison_row is None,
            }
        )

    return {
        # Named "rows", not "items" — a dict literally has an .items() method,
        # and Jinja's attribute resolution on a plain dict would silently
        # return that bound method instead of this list.
        "rows": sorted(rows, key=lambda r: r["value"]),
        "emerging": sorted(rows, key=lambda r: (-r["pct_delta_pp"], r["value"]))[:5],
        "declining": sorted(rows, key=lambda r: (r["pct_delta_pp"], r["value"]))[:5],
    }


def _ranking_deltas(
    baseline_list: list[dict], comparison_list: list[dict], key_field: str
) -> list[dict]:
    baseline_by_key = {row[key_field]: row for row in baseline_list}
    comparison_by_key = {row[key_field]: row for row in comparison_list}
    baseline_rank = {row[key_field]: i + 1 for i, row in enumerate(baseline_list)}
    comparison_rank = {row[key_field]: i + 1 for i, row in enumerate(comparison_list)}

    all_keys = sorted(set(baseline_by_key) | set(comparison_by_key))

    rows = []
    for key in all_keys:
        baseline_row = baseline_by_key.get(key)
        comparison_row = comparison_by_key.get(key)
        b_rank = baseline_rank.get(key)
        c_rank = comparison_rank.get(key)
        rows.append(
            {
                key_field: key,
                "baseline_rank": b_rank,
                "comparison_rank": c_rank,
                "rank_change": (b_rank - c_rank) if (b_rank and c_rank) else None,
                "is_new_entrant": baseline_row is None and comparison_row is not None,
                "is_dropout": baseline_row is not None and comparison_row is None,
            }
        )

    return sorted(
        rows,
        key=lambda r: (
            r["comparison_rank"] if r["comparison_rank"] is not None else float("inf"),
            r[key_field],
        ),
    )


def _concentration_deltas(baseline_conc: dict, comparison_conc: dict) -> dict:
    return {
        key: {
            "baseline": baseline_conc[key],
            "comparison": comparison_conc[key],
            "delta_pp": round(comparison_conc[key] - baseline_conc[key], 1),
        }
        for key in baseline_conc
    }


def _volatility_from_rank_deltas(rank_deltas: list[dict]) -> dict:
    present_in_both = [row for row in rank_deltas if row["rank_change"] is not None]
    entrants = sum(1 for row in rank_deltas if row["is_new_entrant"])
    dropouts = sum(1 for row in rank_deltas if row["is_dropout"])

    avg_rank_change = (
        round(sum(abs(row["rank_change"]) for row in present_in_both) / len(present_in_both), 1)
        if present_in_both
        else None
    )

    return {
        "avg_rank_change": avg_rank_change,
        "entrants_count": entrants,
        "dropouts_count": dropouts,
    }


def _merge_filter_options(
    db: Session,
    baseline_options: dict,
    comparison_options: dict,
    unique_project_ids: list[uuid.UUID],
) -> dict:
    """Union of both periods' available filter options, so the comparison
    filter dropdowns never hide a value that exists in only one side.

    `_available_filter_options` (Phase D) also returns `source_files`
    (a list of dicts, not a set-mergeable string list, already correctly
    deduplicated by `id` — never by filename, so two different projects'
    files sharing a name are never collapsed into one option) and
    `analytics_needs_review_count` (a plain int) — each merged with its
    own appropriate rule below, not the blind set-union the plain
    string-list keys use.

    `analytics_needs_review_count` is NOT a blind sum of the two sides'
    independently-computed counts — that would double-count whenever
    baseline and comparison share a project. It's recomputed directly
    from the unique union of both sides' project ids.
    """
    string_list_keys = (
        "brands",
        "publications",
        "primary_topics",
        "communication_categories",
        "sentiments",
    )
    merged = {
        key: sorted(set(baseline_options[key]) | set(comparison_options[key]))
        for key in string_list_keys
    }

    seen_file_ids = set()
    merged_files = []
    for option in (*baseline_options["source_files"], *comparison_options["source_files"]):
        if option["id"] not in seen_file_ids:
            seen_file_ids.add(option["id"])
            merged_files.append(option)
    merged["source_files"] = sorted(merged_files, key=lambda f: f["original_filename"])

    merged["analytics_needs_review_count"] = get_available_filter_options(db, unique_project_ids)[
        "analytics_needs_review_count"
    ]

    return merged


def get_period_comparison(
    db: Session,
    baseline_project_ids: list[uuid.UUID],
    comparison_project_ids: list[uuid.UUID],
    filters: AnalyticsFilters | None = None,
    top_n: int = DEFAULT_TOP_N,
    baseline_filters: AnalyticsFilters | None = None,
    comparison_filters: AnalyticsFilters | None = None,
) -> dict:
    """`baseline_filters`/`comparison_filters` (Phase E — same-project
    brand-vs-brand comparison) each default to `filters` when not supplied,
    so every existing call site's behavior is completely unchanged when
    they're omitted. When they differ and both sides are the same
    project(s), each side's population and label are computed
    independently against its own filters — never a shared/blended
    population, and never double-counted (each side's `get_period_analytics`
    call is fully independent).
    """
    filters = filters or AnalyticsFilters()
    effective_baseline_filters = baseline_filters or filters
    effective_comparison_filters = comparison_filters or filters
    display_top_n = max(MIN_TOP_N, min(MAX_TOP_N, top_n))

    baseline_projects = _resolve_projects(db, baseline_project_ids, "Baseline")
    comparison_projects = _resolve_projects(db, comparison_project_ids, "Comparison")

    # Always compute rankings at the system cap so rank-change/entrant/
    # dropout math sees the full picture, never a display-truncated slice.
    baseline_full = get_period_analytics(db, baseline_projects, effective_baseline_filters, top_n=MAX_TOP_N)
    comparison_full = get_period_analytics(db, comparison_projects, effective_comparison_filters, top_n=MAX_TOP_N)

    publication_volume_deltas = _ranking_deltas(
        baseline_full["publications_and_stories"]["publications_by_volume"],
        comparison_full["publications_and_stories"]["publications_by_volume"],
        key_field="publication",
    )
    publication_reach_deltas = _ranking_deltas(
        baseline_full["publications_and_stories"]["publications_by_reach"],
        comparison_full["publications_and_stories"]["publications_by_reach"],
        key_field="publication",
    )
    story_volume_deltas = _ranking_deltas(
        baseline_full["publications_and_stories"]["stories_by_volume"],
        comparison_full["publications_and_stories"]["stories_by_volume"],
        key_field="story_key",
    )
    story_reach_deltas = _ranking_deltas(
        baseline_full["publications_and_stories"]["stories_by_reach"],
        comparison_full["publications_and_stories"]["stories_by_reach"],
        key_field="story_key",
    )

    deltas = {
        "kpis": _kpi_deltas(baseline_full["kpis"], comparison_full["kpis"]),
        "brands": _brand_deltas(
            baseline_full["brands"]["by_volume"], comparison_full["brands"]["by_volume"]
        ),
        "topics": _distribution_deltas(
            baseline_full["topics"]["primary_topic_distribution"],
            comparison_full["topics"]["primary_topic_distribution"],
        ),
        "categories": _distribution_deltas(
            baseline_full["topics"]["communication_category_distribution"],
            comparison_full["topics"]["communication_category_distribution"],
        ),
        "sentiment": _distribution_deltas(
            baseline_full["sentiment"]["overall_distribution"],
            comparison_full["sentiment"]["overall_distribution"],
        ),
        "brand_role": _distribution_deltas(
            baseline_full["sentiment"]["brand_role_distribution"],
            comparison_full["sentiment"]["brand_role_distribution"],
        ),
        "publications_by_volume": publication_volume_deltas,
        "publications_by_reach": publication_reach_deltas,
        "stories_by_volume": story_volume_deltas,
        "stories_by_reach": story_reach_deltas,
        "publication_concentration": _concentration_deltas(
            baseline_full["publications_and_stories"]["publication_concentration"],
            comparison_full["publications_and_stories"]["publication_concentration"],
        ),
        "story_concentration": _concentration_deltas(
            baseline_full["publications_and_stories"]["story_concentration"],
            comparison_full["publications_and_stories"]["story_concentration"],
        ),
    }

    volatility = {
        "publications": _volatility_from_rank_deltas(publication_volume_deltas),
        "stories": _volatility_from_rank_deltas(story_volume_deltas),
    }

    return {
        "baseline": {
            **_reslice_for_display(baseline_full, display_top_n),
            "label": _period_side_label(
                baseline_projects, comparison_projects, effective_baseline_filters, effective_comparison_filters
            ),
            "project_count": len(baseline_projects),
        },
        "comparison": {
            **_reslice_for_display(comparison_full, display_top_n),
            "label": _period_side_label(
                comparison_projects, baseline_projects, effective_comparison_filters, effective_baseline_filters
            ),
            "project_count": len(comparison_projects),
        },
        "available_filter_options": _merge_filter_options(
            db,
            baseline_full["available_filter_options"],
            comparison_full["available_filter_options"],
            list(dict.fromkeys([p.id for p in baseline_projects] + [p.id for p in comparison_projects])),
        ),
        "top_n": display_top_n,
        "deltas": deltas,
        "volatility": volatility,
    }
