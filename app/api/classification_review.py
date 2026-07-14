import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette import status

from app.database import get_db
from app.models.project import Project
from app.security.auth import require_web_session
from app.services.classification import ClassificationServiceError
from app.services.classification_results import (
    approve_classification,
    bulk_approve_classifications,
    correct_classification,
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


@router.post("/projects/{project_id}/classifications/{classification_id}/approve")
def approve_classification_action(
    project_id: uuid.UUID,
    classification_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.api.pages import render_project_detail

    project = _get_project_or_404(db, project_id)

    try:
        approve_classification(db, project_id, classification_id)
    except ClassificationServiceError as exc:
        db.rollback()
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        return render_project_detail(
            request,
            db,
            project,
            active_tab="review",
            review_message={"type": "error", "text": exc.message},
            status_code=exc.status_code,
        )
    except Exception:
        db.rollback()
        raise

    return _redirect_to_review(project_id)


@router.post("/projects/{project_id}/classifications/bulk-approve")
def bulk_approve_classifications_action(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    classification_ids: list[uuid.UUID] = Form(default_factory=list),
):
    from app.api.pages import render_project_detail

    project = _get_project_or_404(db, project_id)

    try:
        bulk_approve_classifications(db, project_id, classification_ids)
    except ClassificationServiceError as exc:
        db.rollback()
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        return render_project_detail(
            request,
            db,
            project,
            active_tab="review",
            review_message={"type": "error", "text": exc.message},
            status_code=exc.status_code,
        )
    except Exception:
        db.rollback()
        raise

    return _redirect_to_review(project_id)


@router.post("/projects/{project_id}/classifications/{classification_id}/correct")
def correct_classification_action(
    project_id: uuid.UUID,
    classification_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    primary_topic: str = Form(...),
    secondary_topic: str = Form(""),
    communication_category: str = Form(...),
    sentiment: str = Form(...),
    brand_role: str = Form(...),
    story_key: str = Form(""),
):
    from app.api.pages import render_project_detail

    project = _get_project_or_404(db, project_id)

    corrections = {
        "primary_topic": primary_topic,
        "secondary_topic": secondary_topic,
        "communication_category": communication_category,
        "sentiment": sentiment,
        "brand_role": brand_role,
        "story_key": story_key,
    }

    try:
        correct_classification(db, project_id, classification_id, corrections)
    except ClassificationServiceError as exc:
        db.rollback()
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
        return render_project_detail(
            request,
            db,
            project,
            active_tab="review",
            review_message={"type": "error", "text": exc.message},
            status_code=exc.status_code,
        )
    except Exception:
        db.rollback()
        raise

    return _redirect_to_review(project_id)
