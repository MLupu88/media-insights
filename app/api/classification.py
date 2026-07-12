import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.pages import render_project_detail
from app.database import get_db
from app.models.article import Article, ImportStatus
from app.models.project import AnalysisStatus, Project
from app.security.auth import require_web_session
from app.services.n8n import N8nTriggerError, trigger_classification

router = APIRouter(dependencies=[Depends(require_web_session)])


def _get_project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project


@router.post("/projects/{project_id}/classification/start")
def start_classification(
    project_id: uuid.UUID, request: Request, db: Session = Depends(get_db)
):
    project = _get_project_or_404(db, project_id)

    if project.analysis_status in (AnalysisStatus.QUEUED, AnalysisStatus.RUNNING):
        return render_project_detail(
            request,
            db,
            project,
            active_tab="classification",
            classification_message={
                "type": "info",
                "text": "Classification is already in progress for this project.",
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    has_valid_articles = db.scalar(
        select(Article.id)
        .where(Article.project_id == project_id, Article.import_status == ImportStatus.VALID)
        .limit(1)
    )
    if not has_valid_articles:
        return render_project_detail(
            request,
            db,
            project,
            active_tab="classification",
            classification_message={
                "type": "error",
                "text": "Import valid articles before starting classification.",
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    project.analysis_status = AnalysisStatus.QUEUED
    db.commit()

    try:
        trigger_classification(project.id)
    except N8nTriggerError as exc:
        project.analysis_status = AnalysisStatus.FAILED
        db.commit()
        return render_project_detail(
            request,
            db,
            project,
            active_tab="classification",
            classification_message={"type": "error", "text": str(exc)},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    project.analysis_status = AnalysisStatus.RUNNING
    db.commit()

    return render_project_detail(
        request,
        db,
        project,
        active_tab="classification",
        classification_message={
            "type": "success",
            "text": "Classification started. Use Refresh status to track progress.",
        },
        status_code=status.HTTP_200_OK,
    )
