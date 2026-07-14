import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.classification import (
    LOW_CONFIDENCE_THRESHOLD,
    Classification,
    ClassificationBatch,
    ClassificationBatchArticle,
    ClassificationBatchStatus,
    ClassificationReviewStatus,
)
from app.models.project import AnalysisStatus, Project
from app.schemas.classification import BulkClassificationRequest

FAILURE_MESSAGE_MAX_LENGTH = 500


def initial_review_status(confidence: float) -> str:
    """The review_status a freshly-produced classification starts at.

    Low-confidence output needs a human look (PENDING, enters the
    Classification Review queue); confident output is auto-approved so the
    queue only ever holds what genuinely needs attention -- not all
    30,000+ classifications in a large project. A human can still act on an
    auto-approved row at any time (Edit -> CORRECTED); moving an approved
    row back to PENDING is deliberately not done here -- that is reserved
    for a dedicated "flag for review" action if one is ever added, not an
    automatic side effect of confidence alone.
    """
    return (
        ClassificationReviewStatus.PENDING
        if confidence < LOW_CONFIDENCE_THRESHOLD
        else ClassificationReviewStatus.APPROVED
    )


class ClassificationServiceError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _eligible_articles(db: Session, project_id: uuid.UUID, only_unclassified: bool) -> list[Article]:
    stmt = select(Article).where(
        Article.project_id == project_id,
        Article.import_status == ImportStatus.VALID,
    )

    active_batch_article_ids = (
        select(ClassificationBatchArticle.article_id)
        .join(ClassificationBatch, ClassificationBatch.id == ClassificationBatchArticle.batch_id)
        .where(
            ClassificationBatch.project_id == project_id,
            ClassificationBatch.status.in_(ClassificationBatchStatus.ACTIVE),
        )
    )
    stmt = stmt.where(Article.id.notin_(active_batch_article_ids))

    if only_unclassified:
        classified_article_ids = select(Classification.article_id).where(
            Classification.project_id == project_id
        )
        stmt = stmt.where(Article.id.notin_(classified_article_ids))

    stmt = stmt.order_by(Article.created_at.asc(), Article.id.asc())
    return list(db.scalars(stmt).all())


def has_unclassified_valid_articles(db: Session, project_id: uuid.UUID) -> bool:
    """Whether any valid article for this project still lacks a
    classification. Used both to decide whether a completed/failed batch
    should be treated as "more work remains" (project stays running /
    resumable) and to decide whether to schedule an async continuation.
    """
    return (
        db.scalar(
            select(Article.id)
            .where(
                Article.project_id == project_id,
                Article.import_status == ImportStatus.VALID,
                Article.id.notin_(
                    select(Classification.article_id).where(
                        Classification.project_id == project_id
                    )
                ),
            )
            .limit(1)
        )
        is not None
    )


def _articles_for_batch(db: Session, batch_id: uuid.UUID) -> list[Article]:
    return list(
        db.scalars(
            select(Article)
            .join(ClassificationBatchArticle, ClassificationBatchArticle.article_id == Article.id)
            .where(ClassificationBatchArticle.batch_id == batch_id)
            .order_by(Article.created_at.asc(), Article.id.asc())
        ).all()
    )


def _lock_project_for_classification_claim(db: Session, project_id: uuid.UUID) -> None:
    """Project-scoped transaction lock, held until this transaction commits
    or rolls back. Mirrors app.services.dedup.lock_project_for_dedup.

    Without this, two concurrent "get next batch" calls for the same
    project could both observe "no active batch" and both proceed to
    create one -- the unique index below would then reject the second
    INSERT, but only *after* both had already done the (wasted, and
    user-visibly racy) work of picking articles. The lock serializes the
    whole check-then-act sequence so the second caller sees the first
    caller's committed batch instead.
    """
    db.execute(select(Project.id).where(Project.id == project_id).with_for_update())


def claim_next_classification_batch(
    db: Session, project: Project, batch_size: int, only_unclassified: bool
) -> tuple[ClassificationBatch | None, list[Article], bool]:
    """Atomically obtains at most one batch to process for this project.

    Returns (batch, articles, already_running):
    - a RUNNING batch already exists -> (None, [], True); nothing is
      created or re-sent, the caller must not re-dispatch to n8n.
    - a PENDING batch exists -> it is claimed (set RUNNING) and returned
      exactly once -> (batch, articles, False).
    - neither exists -> a new batch of up to batch_size eligible articles
      is created, set RUNNING immediately, and returned -> (batch,
      articles, False). If none are eligible -> (None, [], False).

    Serialized per-project via _lock_project_for_classification_claim; the
    partial unique index on classification_batches(project_id) WHERE
    status IN ('pending','running') is an independent, DB-level backstop
    against the same race (handled below via IntegrityError).
    """
    _lock_project_for_classification_claim(db, project.id)

    running = db.scalar(
        select(ClassificationBatch).where(
            ClassificationBatch.project_id == project.id,
            ClassificationBatch.status == ClassificationBatchStatus.RUNNING,
        )
    )
    if running is not None:
        return None, [], True

    pending = db.scalar(
        select(ClassificationBatch).where(
            ClassificationBatch.project_id == project.id,
            ClassificationBatch.status == ClassificationBatchStatus.PENDING,
        )
    )
    if pending is not None:
        pending.status = ClassificationBatchStatus.RUNNING
        pending.started_at = datetime.now(timezone.utc)
        project.analysis_status = AnalysisStatus.RUNNING
        db.commit()
        return pending, _articles_for_batch(db, pending.id), False

    eligible = _eligible_articles(db, project.id, only_unclassified)
    chunk = eligible[:batch_size]
    if not chunk:
        return None, [], False

    batch = ClassificationBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        status=ClassificationBatchStatus.RUNNING,
        article_count=len(chunk),
        started_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.add_all(
        ClassificationBatchArticle(batch_id=batch.id, article_id=article.id)
        for article in chunk
    )
    project.analysis_status = AnalysisStatus.RUNNING

    try:
        db.commit()
    except IntegrityError:
        # Defense-in-depth: the project-row lock above should make this
        # unreachable in practice, but if it ever fires, fall back to
        # whatever the winning concurrent call committed rather than
        # erroring the request.
        db.rollback()
        existing = db.scalar(
            select(ClassificationBatch).where(
                ClassificationBatch.project_id == project.id,
                ClassificationBatch.status.in_(ClassificationBatchStatus.ACTIVE),
            )
        )
        if existing is None:
            raise
        if existing.status == ClassificationBatchStatus.RUNNING:
            return None, [], True
        existing.status = ClassificationBatchStatus.RUNNING
        existing.started_at = datetime.now(timezone.utc)
        db.commit()
        return existing, _articles_for_batch(db, existing.id), False

    return batch, chunk, False


def recompute_project_classified_rows(db: Session, project_id: uuid.UUID) -> None:
    classified_rows = (
        db.scalar(
            select(func.count(Classification.id)).where(Classification.project_id == project_id)
        )
        or 0
    )
    project = db.get(Project, project_id)
    if project is None:
        return
    project.classified_rows = classified_rows
    db.commit()


def recompute_project_classification_status(
    db: Session, project_id: uuid.UUID, *, continuation_pending: bool
) -> None:
    """Re-derives the project's analysis_status after a batch stops being
    active (completed or failed).

    `continuation_pending` distinguishes the two callers: True from
    complete_batch (an async trigger for the next batch has just been
    scheduled, so "more work remains" should still read as running), False
    from fail_batch and from the background continuation's own failure
    handler (nothing further is queued, so the project must land on a
    status that does not block "Start Classification" -- see
    app.api.classification.start_classification's active-batch check,
    which is the actual resumability guard; this status is for display).
    """
    project = db.get(Project, project_id)
    if project is None:
        return

    active_batches = (
        db.scalar(
            select(func.count(ClassificationBatch.id)).where(
                ClassificationBatch.project_id == project_id,
                ClassificationBatch.status.in_(ClassificationBatchStatus.ACTIVE),
            )
        )
        or 0
    )

    if active_batches > 0:
        project.analysis_status = AnalysisStatus.RUNNING
    elif has_unclassified_valid_articles(db, project_id):
        if continuation_pending:
            project.analysis_status = AnalysisStatus.RUNNING
        else:
            project.analysis_status = (
                AnalysisStatus.PARTIALLY_COMPLETE
                if project.classified_rows > 0
                else AnalysisStatus.FAILED
            )
    elif project.classified_rows > 0:
        project.analysis_status = AnalysisStatus.COMPLETE

    db.commit()


def fail_batch(db: Session, batch_id: uuid.UUID, message: str) -> ClassificationBatch | None:
    """Marks a batch failed after any error during its processing, so it
    never stays stuck pending/running (the root cause of the 754-batch
    production incident this hotfix follows). Rolls back first so a
    partially-applied failed write (e.g. a bad DeepSeek result mid-loop)
    never leaves the session in an unusable state before this reload; only
    a short, safe message is persisted -- never a raw exception message,
    payload, or article content.
    """
    db.rollback()
    batch = db.get(ClassificationBatch, batch_id)
    if batch is None:
        return None

    safe_message = (message or "").strip()[:FAILURE_MESSAGE_MAX_LENGTH] or "Classification batch failed."
    batch.status = ClassificationBatchStatus.FAILED
    batch.completed_at = datetime.now(timezone.utc)
    batch.error_message = safe_message
    db.commit()

    recompute_project_classification_status(db, batch.project_id, continuation_pending=False)

    return batch


def save_classifications_bulk(
    db: Session, request: BulkClassificationRequest
) -> tuple[int, int]:
    project = db.get(Project, request.project_id)
    if project is None:
        raise ClassificationServiceError("Project not found.", 404)

    batch = db.get(ClassificationBatch, request.batch_id)
    if batch is None:
        raise ClassificationServiceError("Batch not found.", 404)
    if batch.project_id != request.project_id:
        raise ClassificationServiceError("Batch does not belong to the specified project.", 422)

    article_ids = [result.article_id for result in request.results]
    if len(article_ids) != len(set(article_ids)):
        raise ClassificationServiceError("Duplicate article_id values in results.", 422)

    batch_article_ids = set(
        db.scalars(
            select(ClassificationBatchArticle.article_id).where(
                ClassificationBatchArticle.batch_id == batch.id
            )
        ).all()
    )

    if not request.partial_save and len(request.results) != len(batch_article_ids):
        raise ClassificationServiceError(
            f"Result count ({len(request.results)}) does not match the batch article "
            f"count ({len(batch_article_ids)}). Pass partial_save=true to save a subset.",
            422,
        )

    articles = {
        article.id: article
        for article in db.scalars(select(Article).where(Article.id.in_(article_ids))).all()
    }

    for result in request.results:
        article = articles.get(result.article_id)
        if article is None:
            raise ClassificationServiceError(f"Article {result.article_id} does not exist.", 422)
        if article.project_id != request.project_id:
            raise ClassificationServiceError(
                f"Article {result.article_id} does not belong to the specified project.", 422
            )
        if result.article_id not in batch_article_ids:
            raise ClassificationServiceError(
                f"Article {result.article_id} does not belong to batch {batch.id}.", 422
            )

    existing = {
        classification.article_id: classification
        for classification in db.scalars(
            select(Classification).where(Classification.article_id.in_(article_ids))
        ).all()
    }

    saved_count = 0
    updated_count = 0

    for result in request.results:
        existing_classification = existing.get(result.article_id)
        if existing_classification is not None:
            existing_classification.primary_topic = result.primary_topic
            existing_classification.secondary_topic = result.secondary_topic
            existing_classification.communication_category = result.communication_category
            existing_classification.sentiment = result.sentiment
            existing_classification.brand_role = result.brand_role
            existing_classification.story_key = result.story_key
            existing_classification.confidence = result.confidence
            existing_classification.rationale_ro = result.rationale_ro
            existing_classification.model = request.model
            existing_classification.prompt_version = request.prompt_version
            updated_count += 1
        else:
            db.add(
                Classification(
                    id=uuid.uuid4(),
                    article_id=result.article_id,
                    project_id=request.project_id,
                    primary_topic=result.primary_topic,
                    secondary_topic=result.secondary_topic,
                    communication_category=result.communication_category,
                    sentiment=result.sentiment,
                    brand_role=result.brand_role,
                    story_key=result.story_key,
                    confidence=result.confidence,
                    rationale_ro=result.rationale_ro,
                    model=request.model,
                    prompt_version=request.prompt_version,
                    review_status=initial_review_status(result.confidence),
                )
            )
            saved_count += 1

    db.commit()

    recompute_project_classified_rows(db, request.project_id)

    return saved_count, updated_count


def complete_batch(db: Session, batch_id: uuid.UUID) -> tuple[ClassificationBatch, bool]:
    """Marks a batch complete. Idempotent and safe under concurrent replay:
    replaying the same completion request for a batch that is already
    COMPLETE returns it unchanged (newly_completed=False) instead of
    re-running the missing-classification check or re-triggering a second
    continuation -- the caller uses newly_completed to decide whether to
    schedule the async "start next batch" continuation.

    SELECT ... FOR UPDATE on the batch row is what actually makes this safe
    against two concurrent completion requests for the same batch (e.g. a
    retried "Complete Batch" call from n8n): the second call blocks here
    until the first's transaction commits, then re-reads the row and finds
    it already COMPLETE, landing on the idempotent early return above
    instead of racing to also flip it and also report newly_completed=True.
    """
    batch = db.scalar(
        select(ClassificationBatch).where(ClassificationBatch.id == batch_id).with_for_update()
    )
    if batch is None:
        raise ClassificationServiceError("Batch not found.", 404)

    if batch.status == ClassificationBatchStatus.COMPLETE:
        return batch, False

    if batch.status == ClassificationBatchStatus.FAILED:
        raise ClassificationServiceError(
            "Batch has already failed and cannot be completed.", 409
        )

    batch_article_ids = set(
        db.scalars(
            select(ClassificationBatchArticle.article_id).where(
                ClassificationBatchArticle.batch_id == batch.id
            )
        ).all()
    )
    classified_ids = set(
        db.scalars(
            select(Classification.article_id).where(
                Classification.article_id.in_(batch_article_ids)
            )
        ).all()
    )
    missing = batch_article_ids - classified_ids
    if missing:
        raise ClassificationServiceError(
            f"Batch is missing {len(missing)} classification(s) out of "
            f"{len(batch_article_ids)}.",
            422,
        )

    batch.status = ClassificationBatchStatus.COMPLETE
    batch.completed_at = datetime.now(timezone.utc)
    db.commit()

    has_remaining = has_unclassified_valid_articles(db, batch.project_id)
    recompute_project_classification_status(
        db, batch.project_id, continuation_pending=has_remaining
    )

    return batch, True


def get_project_summary(db: Session, project: Project) -> dict:
    low_confidence_count = (
        db.scalar(
            select(func.count(Classification.id)).where(
                Classification.project_id == project.id,
                Classification.confidence < LOW_CONFIDENCE_THRESHOLD,
            )
        )
        or 0
    )

    active_batch_count = (
        db.scalar(
            select(func.count(ClassificationBatch.id)).where(
                ClassificationBatch.project_id == project.id,
                ClassificationBatch.status.in_(ClassificationBatchStatus.ACTIVE),
            )
        )
        or 0
    )

    failed_batch_count = (
        db.scalar(
            select(func.count(ClassificationBatch.id)).where(
                ClassificationBatch.project_id == project.id,
                ClassificationBatch.status == ClassificationBatchStatus.FAILED,
            )
        )
        or 0
    )

    last_classification_at = db.scalar(
        select(func.max(Classification.classified_at)).where(
            Classification.project_id == project.id
        )
    )

    unclassified_valid_rows = max(project.valid_rows - project.classified_rows, 0)
    classification_percentage = (
        round(project.classified_rows / project.valid_rows * 100, 1)
        if project.valid_rows > 0
        else 0.0
    )

    return {
        "project_id": project.id,
        "total_files": project.total_files,
        "total_rows": project.total_rows,
        "valid_rows": project.valid_rows,
        "invalid_rows": project.invalid_rows,
        "duplicate_rows": project.duplicate_rows,
        "classified_rows": project.classified_rows,
        "unclassified_valid_rows": unclassified_valid_rows,
        "classification_percentage": classification_percentage,
        "low_confidence_count": low_confidence_count,
        "active_batch_count": active_batch_count,
        "failed_batch_count": failed_batch_count,
        "last_classification_at": last_classification_at,
    }
