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
from app.services.imports import import_uploaded_file, retry_import
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

    for upload in files:
        filename = upload.filename or "unnamed file"

        if Path(filename).suffix.lower() != ".xlsx":
            results.append(
                {
                    "filename": filename,
                    "accepted": False,
                    "reason": "Only .xlsx files are supported.",
                }
            )
            continue

        try:
            stored_filename, stored_path, _size = save_upload_file(project_id, upload)
        except FileTooLargeError as exc:
            results.append(
                {
                    "filename": filename,
                    "accepted": False,
                    "reason": f"File exceeds the maximum allowed size ({exc.max_size_bytes // (1024 * 1024)} MB).",
                }
            )
            continue

        uploaded_file = UploadedFile(
            project_id=project_id,
            original_filename=filename,
            stored_filename=stored_filename,
            stored_path=stored_path,
            status=UploadedFileStatus.PENDING,
        )
        db.add(uploaded_file)
        db.commit()
        db.refresh(uploaded_file)

        import_uploaded_file(db, uploaded_file)

        results.append(
            {
                "filename": filename,
                "accepted": True,
                "status": uploaded_file.status,
                "reason": uploaded_file.error_message,
            }
        )

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
