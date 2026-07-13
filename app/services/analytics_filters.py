"""The one canonical filter contract for every analytics query in this app.

Deliberately a leaf module: only depends on `Article`/`RetailerReviewStatus`
models, `CANONICAL_RETAILERS`, and SQLAlchemy — never on `analytics.py`,
`report_data.py`, `chat_tools.py`, or `narrative_payload.py`. Those four
modules all need both `AnalyticsFilters` and `apply_common_filters`; keeping
either in `analytics.py` (which also needs them) would force those modules
to import the full aggregation engine and would create an import cycle.
`app/services/analytics.py` re-exports everything defined here unchanged,
so every existing `from app.services.analytics import AnalyticsFilters`
(and friends) keeps working without modification.

Needs-review semantics — the truth table `apply_common_filters` implements:

| brands   | include_needs_review | Result                                            |
|----------|-----------------------|----------------------------------------------------|
| none     | false                 | full analytical population, unresolved coverage included |
| none     | true                  | needs-review rows only                             |
| selected | false                 | selected confirmed brands only, needs-review excluded |
| selected | true                  | selected confirmed brands plus needs-review rows    |

An article with `retailer_review_status == 'needs_review'` has no confirmed
canonical brand. It is never represented as a fake brand ("Unknown"/"Needs
Review"/etc.) anywhere. `include_needs_review` is a separate, explicit flag
— never conflated with brand identity, never a pseudo-brand string.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, and_, or_

from app.models.article import Article, RetailerReviewStatus
from app.services.retailers import CANONICAL_RETAILERS


class AnalyticsFilterError(Exception):
    """Raised for any filter value that fails validation — an unsupported
    brand, a malformed source-file id, a contradictory direct construction,
    or a malformed element type. Callers translate this into a controlled
    400/422 response; it is never silently absorbed into a narrower filter
    set.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class AnalyticsFilters:
    """`brand` (singular) is the pre-Phase-D legacy field, kept for the
    handful of callers that still construct it directly (chat/narrative/
    report scoping). `brands` (plural) is the canonical multi-select field.
    `__post_init__` normalizes/validates both into one canonical state —
    see its docstring for the exact rules. Never construct `brands`/
    `uploaded_file_ids` by hand-editing `__dict__`; always go through the
    constructor so validation runs.
    """

    brand: str | None = None
    brands: tuple[str, ...] = ()
    uploaded_file_ids: tuple[uuid.UUID, ...] = ()
    include_needs_review: bool = False
    publication: str | None = None
    primary_topic: str | None = None
    communication_category: str | None = None
    sentiment: str | None = None
    state: str = "all"

    def __post_init__(self) -> None:
        # --- brands / brand: type-check, then normalize, then validate ---
        if not isinstance(self.brands, (tuple, list)):
            raise AnalyticsFilterError(
                f"brands must be a tuple or list, got {type(self.brands).__name__}"
            )
        for value in self.brands:
            if not isinstance(value, str) or not value:
                raise AnalyticsFilterError(f"brands must contain non-empty strings, got {value!r}")

        # Normalize (dedupe/sort) BEFORE any conflict comparison, so
        # brand="Auchan", brands=("Auchan", "Auchan") is correctly accepted
        # rather than compared against the raw, un-deduplicated tuple.
        normalized_brands = tuple(sorted(set(self.brands)))

        for value in normalized_brands:
            if value not in CANONICAL_RETAILERS:
                raise AnalyticsFilterError(f"Unsupported brand: {value!r}")
        if self.brand is not None and self.brand not in CANONICAL_RETAILERS:
            raise AnalyticsFilterError(f"Unsupported brand: {self.brand!r}")

        if self.brand is not None and normalized_brands and normalized_brands != (self.brand,):
            raise AnalyticsFilterError(
                f"Conflicting brand filters: brand={self.brand!r} vs brands={self.brands!r}"
            )

        final_brands = normalized_brands or ((self.brand,) if self.brand is not None else ())
        final_brand = final_brands[0] if len(final_brands) == 1 else None
        object.__setattr__(self, "brands", final_brands)
        object.__setattr__(self, "brand", final_brand)

        # --- uploaded_file_ids: type-check, then normalize ---
        if not isinstance(self.uploaded_file_ids, (tuple, list)):
            raise AnalyticsFilterError(
                f"uploaded_file_ids must be a tuple or list, got {type(self.uploaded_file_ids).__name__}"
            )
        for file_id in self.uploaded_file_ids:
            if not isinstance(file_id, uuid.UUID):
                raise AnalyticsFilterError(
                    f"uploaded_file_ids must be UUID instances, got {file_id!r}"
                )
        object.__setattr__(self, "uploaded_file_ids", tuple(sorted(set(self.uploaded_file_ids))))


VALID_STATES: tuple[str, ...] = ("all", "classified", "unclassified")


def extract_prefixed_filter_params(query_params, prefix: str) -> dict[str, list[str]]:
    """Builds a plain dict-of-lists view of every query param starting with
    `prefix`, with the prefix stripped — for Phase E's same-project
    brand-vs-brand comparison, where baseline/comparison need independently
    parseable filter sets from one query string (`baseline_filter_brand=
    Auchan&comparison_filter_brand=Carrefour`). Feed the result straight
    into `parse_analytics_filters`. Returns an empty dict (parses to
    all-default filters) if no key with this prefix is present.

    Deliberately uses `baseline_filter_`/`comparison_filter_`, not the
    shorter `baseline_`/`comparison_` — those collide with the unrelated
    `baseline_project_ids`/`comparison_project_ids` query params (which
    also start with `baseline_`/`comparison_`), which would otherwise be
    picked up as a spurious, unrecognized filter key, making
    `extract_prefixed_filter_params` return a non-empty dict even when the
    caller supplied no real per-side filters at all -- silently producing
    an all-default `AnalyticsFilters()` that then wins over the intended
    shared `filters` in `effective_baseline_filters = baseline_filters or
    filters`.
    """
    keys = getattr(query_params, "keys", None)
    all_keys = list(keys()) if keys is not None else list(query_params)
    result: dict[str, list[str]] = {}
    for key in all_keys:
        if key.startswith(prefix):
            result[key[len(prefix):]] = _getlist(query_params, key)
    return result


def _getlist(query_params, key: str) -> list[str]:
    """`parse_analytics_filters` is documented to accept "any dict-like
    mapping," not only Starlette's `QueryParams` — a plain `dict` has no
    `.getlist()`, only `.get()`. A plain dict's value may itself already be
    a `list`/`tuple` (the shape `serialize_analytics_filters` produces, or
    a persisted JSONB payload) or a bare scalar string (the shape older
    plain-dict-constructing tests use) — both are handled.
    """
    getlist = getattr(query_params, "getlist", None)
    if getlist is not None:
        return list(getlist(key))
    value = query_params.get(key)
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _parse_multi_value(query_params, *keys: str) -> list[str]:
    """One shared multi-value parsing shape, generalized to accept several
    alias keys (repeated query params, an equivalent comma-separated
    value, or a persisted-JSON list) — every key is treated identically.
    """
    values: list[str] = []
    for key in keys:
        for raw in _getlist(query_params, key):
            for part in raw.split(","):
                cleaned = part.strip()
                if cleaned:
                    values.append(cleaned)
    return values


def _parse_brands(query_params) -> tuple[str, ...]:
    return tuple(_parse_multi_value(query_params, "brand", "brands"))


def _parse_uploaded_file_ids(query_params) -> tuple[uuid.UUID, ...]:
    """Reads three historical key aliases on read: the canonical
    `source_file`/`source_files`, and the current (pre-this-correction)
    interim Phase D key `uploaded_file_ids`, which may already exist in
    local persisted `ChatSession`/`NarrativeGeneration` rows or fixtures.
    Writing only ever uses the canonical `source_files` key
    (`serialize_analytics_filters`). A malformed UUID string raises —
    unlike a brand, there's no separate downstream validation step for
    this, so the parser itself must reject it.
    """
    raw = _parse_multi_value(query_params, "source_file", "source_files", "uploaded_file_ids")
    parsed: list[uuid.UUID] = []
    for value in raw:
        try:
            parsed.append(uuid.UUID(value))
        except ValueError:
            raise AnalyticsFilterError(f"Malformed source file id: {value!r}") from None
    return tuple(parsed)


def _parse_bool(query_params, *keys: str) -> bool:
    """Reads the first present key among aliases — canonical `needs_review`
    first, falling back to the interim Phase D key `include_needs_review`.
    """
    for key in keys:
        value = query_params.get(key)
        if value is not None:
            return str(value).strip().lower() in ("1", "true", "yes", "on")
    return False


def parse_analytics_filters(query_params) -> AnalyticsFilters:
    """Parse filters from a Starlette QueryParams (or any dict-like)
    mapping — used identically for URL query strings and for persisted
    JSONB filter payloads (see `app.services.chat_tools.build_scope_context`),
    so both surfaces can never diverge. Understands three historical
    shapes on read (canonical, Phase C, and the current interim Phase D
    shape) — see `_parse_uploaded_file_ids`/`_parse_bool` above — while
    only ever emitting the canonical shape via `serialize_analytics_filters`.

    Converts raw strings into typed values; `AnalyticsFilters.__post_init__`
    does the actual brand-acceptance/structural validation, so parsed and
    directly-constructed filters go through the same gate.
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
        brands=_parse_brands(query_params),
        uploaded_file_ids=_parse_uploaded_file_ids(query_params),
        include_needs_review=_parse_bool(query_params, "needs_review", "include_needs_review"),
        publication=_clean("publication"),
        primary_topic=_clean("primary_topic"),
        communication_category=_clean("communication_category"),
        sentiment=_clean("sentiment"),
        state=state,
    )


def serialize_analytics_filters(filters: AnalyticsFilters) -> dict:
    """The one canonical, stable serialization of an `AnalyticsFilters` —
    suitable for URLs (via `build_query_string`), cache keys, export
    metadata, and persisted JSON. Order-independent input always
    normalizes identically (guaranteed by `__post_init__`, re-sorted here
    too so a filter constructed directly in code still serializes
    canonically). Keys are emitted in a fixed order, matching exactly what
    `parse_analytics_filters` reads (`source_files`, `needs_review` — NOT
    the legacy/interim `uploaded_file_ids`/`include_needs_review` key
    names, which are read-only aliases), and empty/default values are
    omitted, so `parse_analytics_filters(serialize_analytics_filters(x)) == x`.
    """
    result: dict = {}
    if filters.brands:
        result["brands"] = sorted(set(filters.brands))
    elif filters.brand:
        result["brand"] = filters.brand
    if filters.uploaded_file_ids:
        result["source_files"] = sorted({str(u) for u in filters.uploaded_file_ids})
    if filters.include_needs_review:
        result["needs_review"] = "1"
    if filters.publication:
        result["publication"] = filters.publication
    if filters.primary_topic:
        result["primary_topic"] = filters.primary_topic
    if filters.communication_category:
        result["communication_category"] = filters.communication_category
    if filters.sentiment:
        result["sentiment"] = filters.sentiment
    if filters.state and filters.state != "all":
        result["state"] = filters.state
    return result


def serialize_phase_c_analytics_filters(filters: AnalyticsFilters) -> dict:
    """Exact reproduction of Phase C's `AnalyticsFilters.asdict()` shape
    (verified via `git show reporting-scope-phase-c-complete:app/services/analytics.py`
    and `git show reporting-scope-phase-c-complete:app/services/chat_service.py`)
    — six fields, always present, never touching `uploaded_file_ids`/
    `brands`/`include_needs_review` regardless of how the live dataclass
    grows. Used ONLY to compute a legacy `ChatSession.scope_key` for
    fallback lookups against sessions created before this correction —
    never for new writes. Because it never touches the UUID-bearing field,
    it can never raise.
    """
    return {
        "brand": filters.brand,
        "publication": filters.publication,
        "primary_topic": filters.primary_topic,
        "communication_category": filters.communication_category,
        "sentiment": filters.sentiment,
        "state": filters.state,
    }


def apply_common_filters(stmt: Select, filters: AnalyticsFilters) -> Select:
    """Adds ONLY the brand / needs-review / source-file predicates shared
    by every article-population query in this app (see the module
    docstring's truth table). Callers own everything else: project
    scoping, import_status/is_duplicate exclusion, classification joins,
    publication/topic/category/sentiment/state, ordering, and limits —
    this function must never add or assume any of those, and never
    receives raw query params, only an already-normalized
    `AnalyticsFilters`.
    """
    if filters.brands or filters.include_needs_review:
        conditions = []
        if filters.brands:
            conditions.append(
                and_(
                    Article.retailer_review_status != RetailerReviewStatus.NEEDS_REVIEW,
                    Article.retailer.in_(filters.brands),
                )
            )
        if filters.include_needs_review:
            conditions.append(Article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW)
        stmt = stmt.where(or_(*conditions))

    if filters.uploaded_file_ids:
        stmt = stmt.where(Article.uploaded_file_id.in_(filters.uploaded_file_ids))

    return stmt
