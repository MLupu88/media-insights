import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.project import Project
from app.models.uploaded_file import UploadedFile, UploadedFileStatus
from app.schemas.classification import (
    BatchCompleteResponse,
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
    complete_batch,
    create_classification_batches,
    get_project_summary,
    save_classifications_bulk,
)
from app.services.imports import import_uploaded_file, retry_import
from app.services.storage import InvalidUploadPathError, resolve_upload_path

router = APIRouter(prefix="/api/internal", dependencies=[Depends(require_internal_secret)])


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

    batches = create_classification_batches(db, project, batch_size, only_unclassified)

    return ClassificationBatchesResponse(
        project_id=project_id,
        batches=[
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
            for batch, articles in batches
        ],
    )


@router.post("/classifications/bulk", response_model=BulkClassificationResponse)
def bulk_save_classifications(payload: BulkClassificationRequest, db: Session = Depends(get_db)):
    try:
        saved_count, updated_count = save_classifications_bulk(db, payload)
    except ClassificationServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return BulkClassificationResponse(
        status="saved",
        saved_count=saved_count,
        updated_count=updated_count,
        rejected_count=0,
    )


@router.post(
    "/classification-batches/{batch_id}/complete", response_model=BatchCompleteResponse
)
def complete_classification_batch(batch_id: uuid.UUID, db: Session = Depends(get_db)):
    try:
        batch = complete_batch(db, batch_id)
    except ClassificationServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return BatchCompleteResponse(status="complete", batch_id=batch.id)


@router.get("/projects/{project_id}/summary", response_model=ProjectSummaryResponse)
def project_summary(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    return ProjectSummaryResponse(**get_project_summary(db, project))
