import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.project import Project
from app.schemas.analytics import ProjectAnalyticsResponse
from app.security.auth import require_internal_secret
from app.services.analytics import get_project_analytics, parse_analytics_filters

router = APIRouter(prefix="/api/internal", dependencies=[Depends(require_internal_secret)])


@router.get("/projects/{project_id}/analytics", response_model=ProjectAnalyticsResponse)
def project_analytics(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    top_n: int = Query(default=10, ge=1, le=50),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    filters = parse_analytics_filters(request.query_params)
    payload = get_project_analytics(db, project, filters, top_n=top_n)

    return ProjectAnalyticsResponse(**payload)
