"""Phase D — centralized AnalyticsFilters contract: parsing, canonical
serialization, and the brand / source-file / needs-review filtering rules.
"""

import uuid

import pytest

from app.models.article import RetailerReviewStatus
from app.services.analytics import (
    AnalyticsFilterError,
    AnalyticsFilters,
    get_project_analytics,
    parse_analytics_filters,
    serialize_analytics_filters,
)


class _QP(dict):
    """Minimal stand-in for Starlette's `QueryParams`, supporting the
    repeated-key `getlist` shape the parser needs, built from a plain
    dict of lists — used so tests can express `?brand=A&brand=B` without
    depending on a real request object.
    """

    def __init__(self, multi: dict[str, list[str]] | None = None, **single):
        super().__init__(single)
        self._multi = multi or {}

    def getlist(self, key):
        if key in self._multi:
            return self._multi[key]
        value = self.get(key)
        return [value] if value is not None else []


# --- no-filter behavior unchanged -------------------------------------------


def test_no_filter_behavior_is_unchanged(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan", audience=100.0)
    article_factory(project, count=1, retailer="Carrefour", audience=50.0)

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["unique_valid_articles"] == 3
    assert {row["brand"] for row in result["brands"]["by_volume"]} == {"Auchan", "Carrefour"}
    assert result["filters"]["brand"] is None
    assert result["filters"]["brands"] == []
    assert result["filters"]["uploaded_file_ids"] == []
    assert result["filters"]["include_needs_review"] is False


def test_parse_analytics_filters_with_empty_query_string_matches_default():
    filters = parse_analytics_filters(_QP())
    assert filters == AnalyticsFilters()


# --- brand selection ----------------------------------------------------


def test_one_selected_brand(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    result = get_project_analytics(db_session, project, AnalyticsFilters(brands=("Auchan",)))

    assert result["kpis"]["unique_valid_articles"] == 2
    assert {row["brand"] for row in result["brands"]["by_volume"]} == {"Auchan"}


def test_multiple_selected_brands(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")
    article_factory(project, count=1, retailer="Lidl")

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(brands=("Auchan", "Carrefour"))
    )

    assert result["kpis"]["unique_valid_articles"] == 3
    assert {row["brand"] for row in result["brands"]["by_volume"]} == {"Auchan", "Carrefour"}


def test_duplicate_brand_parameters_normalize_correctly():
    filters = parse_analytics_filters(
        _QP(multi={"brand": ["Auchan", "Auchan", "Carrefour"]})
    )
    assert filters.brands == ("Auchan", "Carrefour")


def test_unsupported_brand_is_rejected_server_side():
    with pytest.raises(AnalyticsFilterError):
        parse_analytics_filters(_QP(multi={"brand": ["Not A Real Brand"]}))


def test_unsupported_brand_mixed_with_valid_ones_rejects_the_whole_request():
    """A single unsupported value invalidates the entire filter set -- it
    is never silently narrowed to just the valid values.
    """
    with pytest.raises(AnalyticsFilterError):
        parse_analytics_filters(_QP(multi={"brand": ["Auchan", "Not A Real Brand"]}))


def test_zero_selected_brands_means_no_brand_restriction(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    result = get_project_analytics(db_session, project, AnalyticsFilters(brands=()))

    assert result["kpis"]["unique_valid_articles"] == 2


# --- source-file selection ------------------------------------------------


def test_one_selected_source_file(db_session, project_factory, article_factory, uploaded_file_factory):
    project = project_factory()
    file_a = uploaded_file_factory(project)
    file_b = uploaded_file_factory(project)
    article_factory(project, count=2, retailer="Auchan", uploaded_file_id=file_a.id)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_b.id)

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(uploaded_file_ids=(file_a.id,))
    )

    assert result["kpis"]["unique_valid_articles"] == 2


def test_multiple_selected_source_files(db_session, project_factory, article_factory, uploaded_file_factory):
    project = project_factory()
    file_a = uploaded_file_factory(project)
    file_b = uploaded_file_factory(project)
    file_c = uploaded_file_factory(project)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_a.id)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_b.id)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_c.id)

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(uploaded_file_ids=(file_a.id, file_b.id))
    )

    assert result["kpis"]["unique_valid_articles"] == 2


def test_legacy_file_with_no_import_batch_is_selectable(
    db_session, project_factory, article_factory, uploaded_file_factory
):
    project = project_factory()
    legacy_file = uploaded_file_factory(project, import_batch_id=None)
    article_factory(project, count=2, retailer="Auchan", uploaded_file_id=legacy_file.id)

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(uploaded_file_ids=(legacy_file.id,))
    )

    assert result["kpis"]["unique_valid_articles"] == 2
    source_file_ids = {f["id"] for f in result["available_filter_options"]["source_files"]}
    assert legacy_file.id in source_file_ids


def test_cross_project_file_id_is_safely_excluded(
    db_session, project_factory, article_factory, uploaded_file_factory
):
    project_a = project_factory(name="Cross File A")
    project_b = project_factory(name="Cross File B")
    file_b = uploaded_file_factory(project_b)
    article_factory(project_b, count=3, retailer="Auchan", uploaded_file_id=file_b.id)
    article_factory(project_a, count=1, retailer="Auchan")

    # Filtering project A's analytics by a file that belongs to project B
    # must match nothing from B — never leak B's articles into A's view.
    result = get_project_analytics(
        db_session, project_a, AnalyticsFilters(uploaded_file_ids=(file_b.id,))
    )

    assert result["kpis"]["unique_valid_articles"] == 0


def test_invalid_uuid_in_source_file_param_is_rejected():
    with pytest.raises(AnalyticsFilterError):
        parse_analytics_filters(_QP(multi={"source_file": ["not-a-uuid"]}))


def test_valid_and_invalid_uuids_mixed_rejects_the_whole_request(uploaded_file_factory, project_factory):
    """A single malformed value invalidates the entire filter set -- it is
    never silently narrowed to just the valid values.
    """
    project = project_factory()
    real_file = uploaded_file_factory(project)
    with pytest.raises(AnalyticsFilterError):
        parse_analytics_filters(_QP(multi={"source_file": [str(real_file.id), "not-a-uuid"]}))


# --- combined brand + file filtering ----------------------------------------


def test_combined_brand_and_source_file_filtering(
    db_session, project_factory, article_factory, uploaded_file_factory
):
    project = project_factory()
    file_a = uploaded_file_factory(project)
    file_b = uploaded_file_factory(project)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_a.id)
    article_factory(project, count=1, retailer="Carrefour", uploaded_file_id=file_a.id)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_b.id)

    result = get_project_analytics(
        db_session,
        project,
        AnalyticsFilters(brands=("Auchan",), uploaded_file_ids=(file_a.id,)),
    )

    assert result["kpis"]["unique_valid_articles"] == 1


# --- needs-review semantics --------------------------------------------------


def test_include_needs_review_true_isolates_unresolved_rows(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters(include_needs_review=True))

    assert result["kpis"]["unique_valid_articles"] == 1


def test_include_needs_review_false_with_no_brand_filter_still_includes_them(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters(include_needs_review=False))

    assert result["kpis"]["unique_valid_articles"] == 2


def test_needs_review_rows_excluded_from_brand_rankings(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(
        project, count=3, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["unique_valid_articles"] == 5
    brands = {row["brand"] for row in result["brands"]["by_volume"]}
    assert brands == {"Auchan"}
    assert "unknown" not in brands
    # SOV must be computed over the brand-eligible population only.
    auchan_row = next(r for r in result["brands"]["by_volume"] if r["brand"] == "Auchan")
    assert auchan_row["sov_pct"] == 100.0


def test_needs_review_rows_retained_in_project_level_totals(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan", audience=100.0)
    article_factory(
        project, count=1, retailer="unknown", audience=50.0,
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["unique_valid_articles"] == 2
    assert result["kpis"]["total_reach"] == 150.0


def test_needs_review_rows_excluded_from_available_brand_options(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert "unknown" not in result["available_filter_options"]["brands"]


def test_explicit_needs_review_counts_are_exposed(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(
        project, count=2, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["kpis"]["current_view_needs_review_count"] == 2
    assert result["kpis"]["review_backlog_count"] == 2
    assert result["available_filter_options"]["analytics_needs_review_count"] == 2


def test_analytics_needs_review_count_in_available_options_ignores_active_brand_filter(
    db_session, project_factory, article_factory
):
    """Matches the existing `_available_filter_options` convention: the
    checkbox's own label count is never hidden by narrowing to a brand.
    """
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(
        project, count=2, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters(brands=("Auchan",)))

    assert result["available_filter_options"]["analytics_needs_review_count"] == 2


def test_review_backlog_count_exceeds_analytics_needs_review_count_for_invalid_or_duplicate_rows(
    db_session, project_factory, article_factory
):
    """review_backlog_count (matches review.py::count_needs_review exactly,
    no import_status/is_duplicate restriction) must be strictly larger than
    analytics_needs_review_count (scoped to the unique-valid population)
    whenever an invalid or duplicate row is also needs-review -- proving
    the two are never silently conflated.
    """
    from app.models.article import ImportStatus

    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
        import_status=ImportStatus.INVALID,
    )
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
        is_duplicate=True,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters())

    assert result["available_filter_options"]["analytics_needs_review_count"] == 1
    assert result["kpis"]["review_backlog_count"] == 3
    assert result["kpis"]["review_backlog_count"] > result["available_filter_options"]["analytics_needs_review_count"]


def test_current_view_needs_review_count_narrower_than_analytics_needs_review_count_when_filtered(
    db_session, project_factory, article_factory, uploaded_file_factory
):
    project = project_factory()
    file_a = uploaded_file_factory(project)
    file_b = uploaded_file_factory(project)
    article_factory(project, count=1, retailer="Auchan", uploaded_file_id=file_a.id)
    article_factory(
        project, count=1, retailer="unknown", uploaded_file_id=file_a.id,
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )
    article_factory(
        project, count=1, retailer="unknown", uploaded_file_id=file_b.id,
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(
        db_session, project, AnalyticsFilters(uploaded_file_ids=(file_a.id,))
    )

    assert result["available_filter_options"]["analytics_needs_review_count"] == 2
    assert result["kpis"]["current_view_needs_review_count"] == 1
    assert (
        result["kpis"]["current_view_needs_review_count"]
        < result["available_filter_options"]["analytics_needs_review_count"]
    )


def test_needs_review_never_appears_as_a_pseudo_brand_string(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(
        project, count=1, retailer="unknown",
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
    )

    result = get_project_analytics(db_session, project, AnalyticsFilters(include_needs_review=True))

    brands = {row["brand"] for row in result["brands"]["by_volume"]}
    assert "Unknown" not in brands
    assert "Needs Review" not in brands
    assert "Unassigned" not in brands
    assert "unknown" not in brands


# --- canonical serialization -------------------------------------------------


def test_serialization_is_deterministic():
    filters = AnalyticsFilters(brands=("Auchan", "Carrefour"))
    assert serialize_analytics_filters(filters) == serialize_analytics_filters(filters)


def test_serialization_is_order_independent():
    a = AnalyticsFilters(brands=("Carrefour", "Auchan"))
    b = AnalyticsFilters(brands=("Auchan", "Carrefour"))
    assert serialize_analytics_filters(a) == serialize_analytics_filters(b)
    assert serialize_analytics_filters(a)["brands"] == ["Auchan", "Carrefour"]


def test_serialization_of_uploaded_file_ids_is_order_independent():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    first = AnalyticsFilters(uploaded_file_ids=(id_a, id_b))
    second = AnalyticsFilters(uploaded_file_ids=(id_b, id_a))
    assert serialize_analytics_filters(first) == serialize_analytics_filters(second)


def test_serialization_omits_empty_and_default_values():
    assert serialize_analytics_filters(AnalyticsFilters()) == {}


def test_parse_then_serialize_normalizes_query_strings_with_different_order():
    first = parse_analytics_filters(_QP(multi={"brand": ["Carrefour", "Auchan"]}))
    second = parse_analytics_filters(_QP(multi={"brand": ["Auchan", "Carrefour"]}))
    assert serialize_analytics_filters(first) == serialize_analytics_filters(second)


# --- parser/serializer round trip (parse(serialize(x)) == x) ----------------


def _round_trip(filters: AnalyticsFilters) -> AnalyticsFilters:
    return parse_analytics_filters(serialize_analytics_filters(filters))


def test_round_trip_defaults():
    assert _round_trip(AnalyticsFilters()) == AnalyticsFilters()


def test_round_trip_one_brand():
    filters = AnalyticsFilters(brands=("Auchan",))
    assert _round_trip(filters) == filters


def test_round_trip_multiple_brands_two_input_orders():
    first = AnalyticsFilters(brands=("Carrefour", "Auchan"))
    second = AnalyticsFilters(brands=("Auchan", "Carrefour"))
    assert _round_trip(first) == first
    assert _round_trip(second) == second
    assert _round_trip(first) == _round_trip(second)


def test_round_trip_one_source_file(uploaded_file_factory, project_factory):
    project = project_factory()
    file = uploaded_file_factory(project)
    filters = AnalyticsFilters(uploaded_file_ids=(file.id,))
    assert _round_trip(filters) == filters


def test_round_trip_multiple_source_files(uploaded_file_factory, project_factory):
    project = project_factory()
    file_a = uploaded_file_factory(project)
    file_b = uploaded_file_factory(project)
    filters = AnalyticsFilters(uploaded_file_ids=(file_a.id, file_b.id))
    assert _round_trip(filters) == filters


def test_round_trip_needs_review_only():
    filters = AnalyticsFilters(include_needs_review=True)
    assert _round_trip(filters) == filters


def test_round_trip_brands_and_needs_review():
    filters = AnalyticsFilters(brands=("Auchan", "Carrefour"), include_needs_review=True)
    assert _round_trip(filters) == filters


def test_round_trip_all_filters_combined(uploaded_file_factory, project_factory):
    project = project_factory()
    file = uploaded_file_factory(project)
    filters = AnalyticsFilters(
        brands=("Auchan", "Carrefour"),
        uploaded_file_ids=(file.id,),
        include_needs_review=True,
        publication="Ziarul Financiar",
        primary_topic="store_expansion",
        communication_category="corporate",
        sentiment="positive",
        state="classified",
    )
    assert _round_trip(filters) == filters


# --- direct construction cannot bypass server-side validation (B2) ----------


def test_direct_construction_rejects_unsupported_brand():
    with pytest.raises(AnalyticsFilterError):
        AnalyticsFilters(brands=("Not A Real Brand",))


def test_direct_construction_rejects_conflicting_brand_and_brands():
    with pytest.raises(AnalyticsFilterError):
        AnalyticsFilters(brand="Auchan", brands=("Carrefour",))


def test_direct_construction_accepts_brand_matching_a_duplicated_brands_tuple():
    """brand="Auchan" combined with brands containing only (duplicated)
    "Auchan" entries is not a contradiction -- normalize (dedupe) BEFORE
    comparing, per the corrected __post_init__ ordering.
    """
    filters = AnalyticsFilters(brand="Auchan", brands=("Auchan", "Auchan"))
    assert filters.brands == ("Auchan",)
    assert filters.brand == "Auchan"


def test_direct_construction_rejects_non_string_brand_element():
    with pytest.raises(AnalyticsFilterError):
        AnalyticsFilters(brands=(1, 2))


def test_direct_construction_rejects_non_tuple_brands():
    with pytest.raises(AnalyticsFilterError):
        AnalyticsFilters(brands="Auchan")


def test_direct_construction_rejects_non_uuid_uploaded_file_id():
    with pytest.raises(AnalyticsFilterError):
        AnalyticsFilters(uploaded_file_ids=("not-a-uuid",))


def test_direct_construction_dedupes_and_sorts_brands_and_file_ids():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    filters = AnalyticsFilters(
        brands=("Carrefour", "Auchan", "Auchan"),
        uploaded_file_ids=(id_b, id_a, id_a),
    )
    assert filters.brands == tuple(sorted({"Auchan", "Carrefour"}))
    assert filters.uploaded_file_ids == tuple(sorted({id_a, id_b}))
