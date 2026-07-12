import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.classification import (
    LOW_CONFIDENCE_THRESHOLD,
    Classification,
    ClassificationBatch,
    ClassificationBatchArticle,
    ClassificationBatchStatus,
)
from app.models.project import AnalysisStatus, Project
from app.schemas.classification import BulkClassificationRequest


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


def create_classification_batches(
    db: Session, project: Project, batch_size: int, only_unclassified: bool
) -> list[tuple[ClassificationBatch, list[Article]]]:
    eligible = _eligible_articles(db, project.id, only_unclassified)

    batches: list[tuple[ClassificationBatch, list[Article]]] = []
    for start in range(0, len(eligible), batch_size):
        chunk = eligible[start : start + batch_size]
        if not chunk:
            continue
        batch = ClassificationBatch(
            id=uuid.uuid4(),
            project_id=project.id,
            status=ClassificationBatchStatus.PENDING,
            article_count=len(chunk),
        )
        db.add(batch)
        db.add_all(
            ClassificationBatchArticle(batch_id=batch.id, article_id=article.id)
            for article in chunk
        )
        batches.append((batch, chunk))

    if batches:
        if project.analysis_status == AnalysisStatus.QUEUED:
            project.analysis_status = AnalysisStatus.RUNNING
        db.commit()

    return batches


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


def _recompute_project_classification_status(db: Session, project_id: uuid.UUID) -> None:
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
    elif project.classified_rows > 0:
        if project.classified_rows >= project.valid_rows:
            project.analysis_status = AnalysisStatus.COMPLETE
        else:
            project.analysis_status = AnalysisStatus.PARTIALLY_COMPLETE

    db.commit()


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
                )
            )
            saved_count += 1

    if batch.status == ClassificationBatchStatus.PENDING:
        batch.status = ClassificationBatchStatus.RUNNING
        batch.started_at = datetime.now(timezone.utc)

    db.commit()

    recompute_project_classified_rows(db, request.project_id)

    return saved_count, updated_count


def complete_batch(db: Session, batch_id: uuid.UUID) -> ClassificationBatch:
    batch = db.get(ClassificationBatch, batch_id)
    if batch is None:
        raise ClassificationServiceError("Batch not found.", 404)

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

    _recompute_project_classification_status(db, batch.project_id)

    return batch


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
