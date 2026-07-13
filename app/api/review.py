import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette import status

from app.database import get_db
from app.models.project import Project
from app.security.auth import require_web_session
from app.services.review import (
    ArticleNotFoundError,
    InvalidBrandError,
    ReviewServiceError,
    UploadedFileNotFoundError,
    bulk_reassign_article_brand,
    clear_brand_mapping,
    confirm_brand_mapping,
    reassign_article_brand,
    recompute_counters_no_commit,
)

router = APIRouter(dependencies=[Depends(require_web_session)])


def _get_project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project


def _redirect_to_review(project_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(
        url=f"/projects/{project_id}?tab=review", status_code=status.HTTP_303_SEE_OTHER
    )


def _redirect_to_files(project_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(
        url=f"/projects/{project_id}?tab=files", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/projects/{project_id}/articles/{article_id}/assign-brand")
def assign_brand(
    project_id: uuid.UUID,
    article_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    brand: str = Form(...),
):
    from app.api.pages import render_project_detail

    project = _get_project_or_404(db, project_id)

    try:
        affected = reassign_article_brand(db, project_id, article_id, brand)
        recompute_counters_no_commit(db, project_id, affected)
        db.commit()
    except ArticleNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        db.rollback()
        return render_project_detail(
            request,
            db,
            project,
            active_tab="review",
            review_message={"type": "error", "text": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception:
        db.rollback()
        raise

    return _redirect_to_review(project_id)


@router.post("/projects/{project_id}/articles/bulk-assign-brand")
def bulk_assign_brand(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    article_ids: list[uuid.UUID] = Form(default_factory=list),
    brand: str = Form(...),
):
    from app.api.pages import render_project_detail

    project = _get_project_or_404(db, project_id)

    try:
        affected = bulk_reassign_article_brand(db, project_id, article_ids, brand)
        recompute_counters_no_commit(db, project_id, affected)
        db.commit()
    except ArticleNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReviewServiceError as exc:
        db.rollback()
        return render_project_detail(
            request,
            db,
            project,
            active_tab="review",
            review_message={"type": "error", "text": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception:
        db.rollback()
        raise

    return _redirect_to_review(project_id)


@router.post("/projects/{project_id}/files/{file_id}/confirm-brand-mapping")
def confirm_brand_mapping_action(
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    db: Session = Depends(get_db),
    brand: str = Form(...),
):
    _get_project_or_404(db, project_id)

    try:
        confirm_brand_mapping(db, project_id, file_id, brand)
        db.commit()
    except UploadedFileNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidBrandError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    return _redirect_to_files(project_id)


@router.post("/projects/{project_id}/files/{file_id}/clear-brand-mapping")
def clear_brand_mapping_action(
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    _get_project_or_404(db, project_id)

    try:
        clear_brand_mapping(db, project_id, file_id)
        db.commit()
    except UploadedFileNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    return _redirect_to_files(project_id)
