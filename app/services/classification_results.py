"""Classification Results & Review.

Two read surfaces, both server-side paginated over Article JOIN
Classification:
  - list_classification_results: the full, filterable/sortable table for
    the Classification tab (every classified article).
  - get_classification_review_queue: the Classification Review queue for
    the Review tab (review_status == PENDING only).

And the human-review write workflow: approve_classification /
bulk_approve_classifications / correct_classification.

Independent of app.services.review (Article-level brand-assignment
review) -- nothing here touches retailer_review_status/retailer_confidence,
and nothing there touches Classification.review_status.

`get_effective_classification_values` is the one documented place future
Analytics/Insights work should read classification values from. It is
intentionally thin: a human correction is written in place onto the same
row's primary_topic/secondary_topic/communication_category/sentiment/
brand_role/story_key fields (with the pre-correction AI output archived
into original_ai_labels, never overwritten again after the first
correction) -- so the row's current fields are *always* the effective
values, and no separate merge step is ever needed. original_ai_labels is a
pure audit trail and must never be read by analytical code.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.article import Article
from app.models.classification import (
    LOW_CONFIDENCE_THRESHOLD,
    Classification,
    ClassificationReviewStatus,
    ClassificationTaxonomy,
)
from app.services.classification import ClassificationServiceError

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
REVIEW_QUEUE_PAGE_SIZE = 50

CONFIDENCE_BUCKETS = ("all", "low", "high")
SORT_OPTIONS = (
    "date_desc",
    "date_asc",
    "reach_desc",
    "reach_asc",
    "confidence_desc",
    "confidence_asc",
)
DEFAULT_SORT = "date_desc"

# The only fields a human correction may change. confidence/rationale_ro
# are always the AI's own output and are never part of this set.
CORRECTABLE_FIELDS = (
    "primary_topic",
    "secondary_topic",
    "communication_category",
    "sentiment",
    "brand_role",
    "story_key",
)


class ClassificationResultsFilterError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# --- read: filtered/sorted/paginated results table --------------------------


@dataclass(frozen=True)
class ClassificationResultsQuery:
    search: str | None = None
    primary_topic: str | None = None
    communication_category: str | None = None
    sentiment: str | None = None
    brand_role: str | None = None
    confidence_bucket: str = "all"
    review_status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    sort: str = DEFAULT_SORT
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


def _clean_str(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_classification_results_query(query_params) -> ClassificationResultsQuery:
    """Strict on taxonomy/enum-shaped params (rejects unsupported values,
    matching AnalyticsFilters' philosophy) and forgiving on
    pagination/date params (clamped/ignored on bad input instead of
    raising, matching clamp_top_n's philosophy) -- a malformed page number
    should never break the page, but a typo'd topic name should never
    silently widen to "all topics".
    """
    primary_topic = _clean_str(query_params.get("primary_topic"))
    if primary_topic is not None and primary_topic not in ClassificationTaxonomy.PRIMARY_TOPICS:
        raise ClassificationResultsFilterError(f"Unsupported primary_topic: {primary_topic!r}")

    communication_category = _clean_str(query_params.get("communication_category"))
    if (
        communication_category is not None
        and communication_category not in ClassificationTaxonomy.COMMUNICATION_CATEGORIES
    ):
        raise ClassificationResultsFilterError(
            f"Unsupported communication_category: {communication_category!r}"
        )

    sentiment = _clean_str(query_params.get("sentiment"))
    if sentiment is not None and sentiment not in ClassificationTaxonomy.SENTIMENTS:
        raise ClassificationResultsFilterError(f"Unsupported sentiment: {sentiment!r}")

    brand_role = _clean_str(query_params.get("brand_role"))
    if brand_role is not None and brand_role not in ClassificationTaxonomy.BRAND_ROLES:
        raise ClassificationResultsFilterError(f"Unsupported brand_role: {brand_role!r}")

    confidence_bucket = _clean_str(query_params.get("confidence")) or "all"
    if confidence_bucket not in CONFIDENCE_BUCKETS:
        raise ClassificationResultsFilterError(
            f"Unsupported confidence bucket: {confidence_bucket!r}"
        )

    review_status = _clean_str(query_params.get("review_status"))
    if review_status is not None and review_status not in ClassificationReviewStatus.ALL:
        raise ClassificationResultsFilterError(f"Unsupported review_status: {review_status!r}")

    sort = _clean_str(query_params.get("sort")) or DEFAULT_SORT
    if sort not in SORT_OPTIONS:
        raise ClassificationResultsFilterError(f"Unsupported sort: {sort!r}")

    try:
        page = int(query_params.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    try:
        page_size = int(query_params.get("page_size", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))

    return ClassificationResultsQuery(
        search=_clean_str(query_params.get("search")),
        primary_topic=primary_topic,
        communication_category=communication_category,
        sentiment=sentiment,
        brand_role=brand_role,
        confidence_bucket=confidence_bucket,
        review_status=review_status,
        date_from=_parse_date(_clean_str(query_params.get("date_from"))),
        date_to=_parse_date(_clean_str(query_params.get("date_to"))),
        sort=sort,
        page=page,
        page_size=page_size,
    )


@dataclass
class ClassificationResultsPage:
    rows: list[tuple[Article, Classification]]
    total_count: int
    page: int
    page_size: int
    total_pages: int


def _base_results_stmt(project_id: uuid.UUID) -> Select:
    return (
        select(Article, Classification)
        .join(Classification, Classification.article_id == Article.id)
        .where(Article.project_id == project_id)
    )


def _apply_query_filters(stmt: Select, q: ClassificationResultsQuery) -> Select:
    if q.search:
        pattern = f"%{q.search}%"
        stmt = stmt.where(
            or_(
                Article.title.ilike(pattern),
                Article.subject.ilike(pattern),
                Article.source.ilike(pattern),
                Classification.story_key.ilike(pattern),
            )
        )
    if q.primary_topic:
        stmt = stmt.where(Classification.primary_topic == q.primary_topic)
    if q.communication_category:
        stmt = stmt.where(Classification.communication_category == q.communication_category)
    if q.sentiment:
        stmt = stmt.where(Classification.sentiment == q.sentiment)
    if q.brand_role:
        stmt = stmt.where(Classification.brand_role == q.brand_role)
    if q.confidence_bucket == "low":
        stmt = stmt.where(Classification.confidence < LOW_CONFIDENCE_THRESHOLD)
    elif q.confidence_bucket == "high":
        stmt = stmt.where(Classification.confidence >= LOW_CONFIDENCE_THRESHOLD)
    if q.review_status:
        stmt = stmt.where(Classification.review_status == q.review_status)
    if q.date_from:
        stmt = stmt.where(Article.publication_date >= q.date_from)
    if q.date_to:
        stmt = stmt.where(Article.publication_date <= q.date_to)
    return stmt


_SORT_COLUMNS = {
    "date_desc": (Article.publication_date.desc(),),
    "date_asc": (Article.publication_date.asc(),),
    "reach_desc": (Article.audience.desc(),),
    "reach_asc": (Article.audience.asc(),),
    "confidence_desc": (Classification.confidence.desc(),),
    "confidence_asc": (Classification.confidence.asc(),),
}


def list_classification_results(
    db: Session, project_id: uuid.UUID, q: ClassificationResultsQuery
) -> ClassificationResultsPage:
    base_stmt = _apply_query_filters(_base_results_stmt(project_id), q)

    total_count = db.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0
    total_pages = max(1, -(-total_count // q.page_size))  # ceil division
    page = min(q.page, total_pages)

    order_columns = _SORT_COLUMNS[q.sort]
    # Article.id is always the final tiebreaker so paging never
    # double-shows or skips a row across two requests with equal sort keys
    # (e.g. many articles sharing a publication_date).
    stmt = (
        base_stmt.order_by(*order_columns, Article.id.asc())
        .limit(q.page_size)
        .offset((page - 1) * q.page_size)
    )
    rows = [(article, classification) for article, classification in db.execute(stmt).all()]

    return ClassificationResultsPage(
        rows=rows,
        total_count=total_count,
        page=page,
        page_size=q.page_size,
        total_pages=total_pages,
    )


# --- read: classification review queue --------------------------------------


@dataclass
class ClassificationReviewPage:
    rows: list[tuple[Article, Classification]]
    total_count: int
    page: int
    page_size: int
    total_pages: int


def get_classification_review_queue(
    db: Session, project_id: uuid.UUID, page: int = 1, page_size: int = REVIEW_QUEUE_PAGE_SIZE
) -> ClassificationReviewPage:
    """Classifications needing human attention: review_status == PENDING
    only -- every classification the AI produces starts PENDING regardless
    of its confidence, so this already covers "low confidence" and
    "not yet approved" as one condition. Approve/Correct are the only two
    ways to leave this queue; once acted on, a row never reappears here
    (a lower future confidence signal never un-does a human's approval).
    Ordered lowest-confidence-first so the least certain AI output is
    triaged first within the queue.
    """
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    base_stmt = (
        select(Article, Classification)
        .join(Classification, Classification.article_id == Article.id)
        .where(
            Article.project_id == project_id,
            Classification.review_status == ClassificationReviewStatus.PENDING,
        )
    )
    total_count = db.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0
    total_pages = max(1, -(-total_count // page_size))
    page = max(1, min(page, total_pages))

    stmt = (
        base_stmt.order_by(Classification.confidence.asc(), Article.id.asc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = [(article, classification) for article, classification in db.execute(stmt).all()]
    return ClassificationReviewPage(
        rows=rows, total_count=total_count, page=page, page_size=page_size, total_pages=total_pages
    )


def count_classification_review_backlog(db: Session, project_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count(Classification.id)).where(
                Classification.project_id == project_id,
                Classification.review_status == ClassificationReviewStatus.PENDING,
            )
        )
        or 0
    )


# --- write: approve / correct -------------------------------------------------


def _get_classification_or_raise(
    db: Session, project_id: uuid.UUID, classification_id: uuid.UUID
) -> Classification:
    classification = db.execute(
        select(Classification).where(
            Classification.id == classification_id, Classification.project_id == project_id
        )
    ).scalar_one_or_none()
    if classification is None:
        raise ClassificationServiceError(
            f"Classification {classification_id} not found in project {project_id}.", 404
        )
    return classification


def approve_classification(
    db: Session, project_id: uuid.UUID, classification_id: uuid.UUID
) -> Classification:
    classification = _get_classification_or_raise(db, project_id, classification_id)
    classification.review_status = ClassificationReviewStatus.APPROVED
    classification.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return classification


def bulk_approve_classifications(
    db: Session, project_id: uuid.UUID, classification_ids: list[uuid.UUID]
) -> int:
    if not classification_ids:
        raise ClassificationServiceError("No rows were selected.", 422)

    unique_ids = list(dict.fromkeys(classification_ids))
    rows = (
        db.execute(
            select(Classification).where(
                Classification.id.in_(unique_ids), Classification.project_id == project_id
            )
        )
        .scalars()
        .all()
    )

    found_ids = {row.id for row in rows}
    missing = [str(cid) for cid in unique_ids if cid not in found_ids]
    if missing:
        raise ClassificationServiceError(
            f"Classification(s) not found in project {project_id}: {', '.join(missing)}", 404
        )

    now = datetime.now(timezone.utc)
    for row in rows:
        row.review_status = ClassificationReviewStatus.APPROVED
        row.reviewed_at = now
    db.commit()
    return len(rows)


def _normalize_correction_input(raw: dict) -> dict:
    normalized = dict(raw)
    for required_field in ("primary_topic", "communication_category", "sentiment", "brand_role"):
        value = normalized.get(required_field)
        normalized[required_field] = value.strip() if isinstance(value, str) else value
    for optional_field in ("secondary_topic", "story_key"):
        value = normalized.get(optional_field)
        normalized[optional_field] = (
            value.strip() if isinstance(value, str) and value.strip() else None
        )
    return normalized


def _validate_correction(corrections: dict) -> None:
    if corrections.get("primary_topic") not in ClassificationTaxonomy.PRIMARY_TOPICS:
        raise ClassificationServiceError(
            f"Invalid primary_topic: {corrections.get('primary_topic')!r}", 422
        )
    secondary_topic = corrections.get("secondary_topic")
    if secondary_topic is not None and secondary_topic not in ClassificationTaxonomy.PRIMARY_TOPICS:
        raise ClassificationServiceError(f"Invalid secondary_topic: {secondary_topic!r}", 422)
    if (
        corrections.get("communication_category")
        not in ClassificationTaxonomy.COMMUNICATION_CATEGORIES
    ):
        raise ClassificationServiceError(
            f"Invalid communication_category: {corrections.get('communication_category')!r}", 422
        )
    if corrections.get("sentiment") not in ClassificationTaxonomy.SENTIMENTS:
        raise ClassificationServiceError(
            f"Invalid sentiment: {corrections.get('sentiment')!r}", 422
        )
    if corrections.get("brand_role") not in ClassificationTaxonomy.BRAND_ROLES:
        raise ClassificationServiceError(
            f"Invalid brand_role: {corrections.get('brand_role')!r}", 422
        )


def correct_classification(
    db: Session, project_id: uuid.UUID, classification_id: uuid.UUID, corrections: dict
) -> Classification:
    """Applies a human correction to the editable fields. The pre-correction
    AI output is archived into original_ai_labels the first time this is
    ever called for a given row (never again after that), so repeated
    corrections never lose the true original AI output. confidence and
    rationale_ro are never touched here.
    """
    classification = _get_classification_or_raise(db, project_id, classification_id)

    normalized = _normalize_correction_input(corrections)
    _validate_correction(normalized)

    if classification.original_ai_labels is None:
        classification.original_ai_labels = {
            field: getattr(classification, field) for field in CORRECTABLE_FIELDS
        }

    for field in CORRECTABLE_FIELDS:
        setattr(classification, field, normalized[field])

    classification.review_status = ClassificationReviewStatus.CORRECTED
    classification.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return classification


# --- effective values: the one place Analytics/Insights should read from ----


def get_effective_classification_values(classification: Classification) -> dict:
    """The values Analytics/Insights must always read: the current row
    state, which is the human correction if one was ever made
    (review_status == 'corrected') or the original AI output otherwise.
    original_ai_labels is a pure audit trail and must never be read by
    analytical code -- this function exists so future Analytics/Insights
    work has one obvious, documented place to get classification values
    from, instead of reaching for original_ai_labels or re-deriving this
    logic per call site.
    """
    return {field: getattr(classification, field) for field in CORRECTABLE_FIELDS}
