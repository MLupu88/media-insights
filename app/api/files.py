import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.pages import render_project_detail
from app.database import get_db
from app.models.project import Project
from app.models.uploaded_file import UploadedFile, UploadedFileStatus
from app.security.auth import require_web_session
from app.services.imports import (
    fail_import_batch,
    finalize_import_batch,
    import_uploaded_file,
    record_batch_file_result,
    record_batch_rejected_file,
    retry_import,
    start_import_batch,
)
from app.services.storage import FileTooLargeError, save_upload_file

router = APIRouter(dependencies=[Depends(require_web_session)])


def _get_project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project


@router.post("/projects/{project_id}/files")
def upload_files(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    files: list[UploadFile] = File(default_factory=list),
):
    project = _get_project_or_404(db, project_id)

    results: list[dict] = []

    if not files:
        results.append({"filename": None, "accepted": False, "reason": "No files were selected."})
        return render_project_detail(
            request,
            db,
            project,
            active_tab="files",
            upload_results=results,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    batch = start_import_batch(db, project_id)
    # Captured as a plain UUID immediately, while the session is known
    # healthy -- if a later exception leaves the transaction aborted,
    # `batch.id` (an attribute access on the ORM object) could itself
    # raise `PendingRollbackError` before `fail_import_batch`'s own
    # `db.rollback()` ever runs. A plain UUID has no such risk.
    batch_id = batch.id
    error_reasons: list[str] = []

    try:
        for upload in files:
            filename = upload.filename or "unnamed file"

            if Path(filename).suffix.lower() != ".xlsx":
                reason = "Only .xlsx files are supported."
                results.append({"filename": filename, "accepted": False, "reason": reason})
                record_batch_rejected_file(batch)
                error_reasons.append(f"{filename}: {reason}")
                continue

            try:
                stored_filename, stored_path, _size = save_upload_file(project_id, upload)
            except FileTooLargeError as exc:
                reason = (
                    f"File exceeds the maximum allowed size "
                    f"({exc.max_size_bytes // (1024 * 1024)} MB)."
                )
                results.append({"filename": filename, "accepted": False, "reason": reason})
                record_batch_rejected_file(batch)
                error_reasons.append(f"{filename}: {reason}")
                continue

            uploaded_file = UploadedFile(
                project_id=project_id,
                import_batch_id=batch.id,
                original_filename=filename,
                stored_filename=stored_filename,
                stored_path=stored_path,
                status=UploadedFileStatus.PENDING,
            )
            db.add(uploaded_file)
            db.commit()
            db.refresh(uploaded_file)

            import_uploaded_file(db, uploaded_file)
            db.refresh(uploaded_file)

            record_batch_file_result(batch, uploaded_file)
            if uploaded_file.status == UploadedFileStatus.FAILED and uploaded_file.error_message:
                error_reasons.append(f"{filename}: {uploaded_file.error_message}")

            results.append(
                {
                    "filename": filename,
                    "accepted": True,
                    "status": uploaded_file.status,
                    "reason": uploaded_file.error_message,
                }
            )
    except Exception:
        # Never a raw 500: mark the batch failed (rollback-then-reload,
        # via `batch_id` -- a plain UUID captured before this request did
        # anything that could fail, never the ORM object itself, which
        # could raise `PendingRollbackError` on attribute access against
        # an already-aborted transaction) and render the same Files page
        # the rest of this route renders, with a short, safe message. The
        # underlying exception is not shown to the user.
        fail_import_batch(db, batch_id, "Unexpected error during import.")
        db.refresh(project)
        results.append(
            {
                "filename": None,
                "accepted": False,
                "reason": "An unexpected error occurred while processing this upload. The import has been marked as failed.",
            }
        )
        return render_project_detail(
            request,
            db,
            project,
            active_tab="files",
            upload_results=results,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    finalize_import_batch(db, batch, error_reasons)

    db.refresh(project)
    any_accepted = any(result["accepted"] for result in results)
    return render_project_detail(
        request,
        db,
        project,
        active_tab="files",
        upload_results=results,
        status_code=status.HTTP_200_OK if any_accepted else status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@router.post("/projects/{project_id}/files/{file_id}/retry")
def retry_file_import(
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    _get_project_or_404(db, project_id)

    uploaded_file = db.get(UploadedFile, file_id)
    if uploaded_file is None or uploaded_file.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    try:
        retry_import(db, uploaded_file)
    except ValueError:
        pass

    return RedirectResponse(
        url=f"/projects/{project_id}?tab=files", status_code=status.HTTP_303_SEE_OTHER
    )
