import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.classification import ClassificationBatch, ClassificationBatchStatus
from app.models.project import Project
from app.models.uploaded_file import UploadedFile, UploadedFileStatus
from app.schemas.classification import (
    BatchCompleteResponse,
    BatchFailRequest,
    BatchFailResponse,
    BulkClassificationRequest,
    BulkClassificationResponse,
    ClassificationBatchArticleOut,
    ClassificationBatchesResponse,
    ClassificationBatchOut,
    ProjectSummaryResponse,
)
from app.schemas.internal import ImportFileRequest, ImportFileResponse
from app.security.auth import require_internal_secret
from app.services.classification import (
    ClassificationServiceError,
    claim_next_classification_batch,
    complete_batch,
    fail_batch,
    get_project_summary,
    has_unclassified_valid_articles,
    recompute_project_classification_status,
    save_classifications_bulk,
)
from app.services.imports import import_uploaded_file, retry_import
from app.services.n8n import N8nTriggerError, trigger_classification
from app.services.storage import InvalidUploadPathError, resolve_upload_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", dependencies=[Depends(require_internal_secret)])


def _continue_classification_in_background(project_id: uuid.UUID) -> None:
    """Runs after the "complete batch" HTTP response has already been sent
    (FastAPI BackgroundTasks). Only pings n8n to start a new execution --
    it never creates a ClassificationBatch itself, so if this fails, or the
    process restarts before it runs at all, nothing is left pending/running
    as a result of it: n8n's own next call to the classification-batches
    endpoint is what actually claims a batch.

    BackgroundTasks is not a durable queue, so this is best-effort by
    design; app.api.classification.start_classification's active-batch
    check (not analysis_status) is the real resumability guard -- the
    status update here is for the display badge only.
    """
    db = SessionLocal()
    try:
        try:
            trigger_classification(project_id)
        except N8nTriggerError:
            logger.exception(
                "Failed to trigger classification continuation for project %s", project_id
            )
            recompute_project_classification_status(db, project_id, continuation_pending=False)
    finally:
        db.close()


@router.post("/import-file", response_model=ImportFileResponse)
def import_file(payload: ImportFileRequest, db: Session = Depends(get_db)):
    project = db.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    try:
        resolved_path = resolve_upload_path(payload.file_path)
    except InvalidUploadPathError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if resolved_path.suffix.lower() != ".xlsx":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only .xlsx files are supported."
        )

    if not resolved_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    uploaded_file = (
        db.query(UploadedFile)
        .filter_by(project_id=payload.project_id, stored_path=str(resolved_path))
        .one_or_none()
    )

    if uploaded_file is None:
        uploaded_file = UploadedFile(
            project_id=payload.project_id,
            original_filename=payload.uploaded_name,
            stored_filename=resolved_path.name,
            stored_path=str(resolved_path),
            retailer_hint=payload.retailer_hint,
            status=UploadedFileStatus.PENDING,
        )
        db.add(uploaded_file)
        db.commit()
        db.refresh(uploaded_file)
        import_uploaded_file(db, uploaded_file)
    elif uploaded_file.status == UploadedFileStatus.FAILED:
        retry_import(db, uploaded_file)
    elif uploaded_file.status != UploadedFileStatus.COMPLETED:
        import_uploaded_file(db, uploaded_file)
    # else: already completed — reuse the existing import results as-is.

    db.refresh(uploaded_file)

    if uploaded_file.status == UploadedFileStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Import failed: {uploaded_file.error_message}",
        )

    return ImportFileResponse(
        status="imported",
        project_id=payload.project_id,
        uploaded_file_id=uploaded_file.id,
        retailer=uploaded_file.detected_retailer or "unknown",
        rows_received=uploaded_file.row_count,
        rows_imported=uploaded_file.valid_row_count,
        rows_invalid=uploaded_file.invalid_row_count,
        rows_duplicate=uploaded_file.duplicate_row_count,
    )


@router.get(
    "/projects/{project_id}/classification-batches",
    response_model=ClassificationBatchesResponse,
)
def get_classification_batches(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    batch_size: int = Query(default=50, ge=1, le=100),
    only_unclassified: bool = Query(default=True),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    batch, articles, already_running = claim_next_classification_batch(
        db, project, batch_size, only_unclassified
    )

    batches: list[ClassificationBatchOut] = []
    if batch is not None:
        batches.append(
            ClassificationBatchOut(
                batch_id=batch.id,
                articles=[
                    ClassificationBatchArticleOut(
                        article_id=article.id,
                        brand=article.retailer,
                        title=article.title,
                        subject=article.subject,
                        publication=article.source,
                        date=article.publication_date,
                        reach=article.audience,
                        medium=article.medium,
                        original_sentiment=article.sentiment_original,
                        original_importance=article.importance_original,
                        is_duplicate=article.is_duplicate,
                    )
                    for article in articles
                ],
            )
        )

    return ClassificationBatchesResponse(
        project_id=project_id, batches=batches, already_running=already_running
    )


@router.post("/classifications/bulk", response_model=BulkClassificationResponse)
def bulk_save_classifications(payload: BulkClassificationRequest, db: Session = Depends(get_db)):
    try:
        saved_count, updated_count = save_classifications_bulk(db, payload)
    except ClassificationServiceError as exc:
        fail_batch(db, payload.batch_id, exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        fail_batch(db, payload.batch_id, "Unexpected error while saving classification results.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error while saving classification results.",
        ) from exc

    return BulkClassificationResponse(
        status="saved",
        saved_count=saved_count,
        updated_count=updated_count,
        rejected_count=0,
    )


@router.post(
    "/classification-batches/{batch_id}/complete",
    response_model=BatchCompleteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def complete_classification_batch(
    batch_id: uuid.UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    try:
        batch, newly_completed = complete_batch(db, batch_id)
    except ClassificationServiceError as exc:
        # A batch that is already FAILED is reported as a conflict, not
        # re-marked failed again -- fail_batch is for batches newly failing
        # here, not for confirming an already-terminal state.
        if exc.status_code != status.HTTP_409_CONFLICT:
            fail_batch(db, batch_id, exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        fail_batch(db, batch_id, "Unexpected error while completing classification batch.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error while completing classification batch.",
        ) from exc

    if newly_completed and has_unclassified_valid_articles(db, batch.project_id):
        background_tasks.add_task(_continue_classification_in_background, batch.project_id)

    return BatchCompleteResponse(status="complete", batch_id=batch.id)


@router.post(
    "/classification-batches/{batch_id}/fail",
    response_model=BatchFailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def fail_classification_batch(
    batch_id: uuid.UUID,
    payload: BatchFailRequest,
    db: Session = Depends(get_db),
):
    """Explicit failure callback for asynchronous n8n processing.

    Validation/parsing failures happen before the bulk-save endpoint, so
    without this callback a claimed batch can remain RUNNING indefinitely.
    The route is idempotent for an already-failed batch and refuses to
    rewrite a completed batch as failed.
    """
    batch = db.get(ClassificationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")
    if batch.status == ClassificationBatchStatus.COMPLETE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Completed classification batch cannot be marked failed.",
        )
    if batch.status != ClassificationBatchStatus.FAILED:
        batch = fail_batch(db, batch_id, payload.error_message)
        if batch is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found.")

    return BatchFailResponse(status="failed", batch_id=batch.id)


@router.get("/projects/{project_id}/summary", response_model=ProjectSummaryResponse)
def project_summary(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    return ProjectSummaryResponse(**get_project_summary(db, project))
