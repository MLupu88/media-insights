import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.pages import render, render_project_detail
from app.database import get_db
from app.models.narrative import NarrativeGeneration, NarrativeGenerationStatus, NarrativeValidationStatus
from app.models.project import Project
from app.security.auth import require_web_session
from app.services.analytics import AnalyticsFilterError, parse_analytics_filters
from app.services.analytics_filters import extract_prefixed_filter_params
from app.services.n8n import N8nTriggerError, trigger_narrative_generation
from app.services.narrative_service import (
    NarrativeServiceError,
    create_comparison_generation,
    create_project_generation,
)

router = APIRouter(dependencies=[Depends(require_web_session)])


def _get_project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project


def _trigger_or_fail(db: Session, generation: NarrativeGeneration) -> NarrativeGeneration:
    try:
        trigger_narrative_generation(generation.id, generation.project_id)
    except N8nTriggerError as exc:
        generation.status = NarrativeGenerationStatus.FAILED
        generation.missing_narrative_types = list(generation.narrative_types)
        generation.error_message = str(exc)
        db.commit()
        db.refresh(generation)
        return generation

    generation.status = NarrativeGenerationStatus.RUNNING
    generation.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(generation)
    return generation


@router.get("/api/ui/narrative-generations/{generation_id}/status")
def get_narrative_generation_ui_status(
    generation_id: uuid.UUID, db: Session = Depends(get_db)
):
    """Browser-facing, session-authenticated status probe for automatic
    refresh. The detailed narrative payload remains behind the existing
    internal-secret API; this endpoint exposes status only.
    """
    generation = db.get(NarrativeGeneration, generation_id)
    if generation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Narrative generation not found."
        )
    return JSONResponse(
        content={
            "id": str(generation.id),
            "status": generation.status,
            "terminal": generation.status not in NarrativeGenerationStatus.ACTIVE,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/projects/{project_id}/narratives/start")
def start_project_narrative_generation(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    force_regenerate: bool = Form(False),
):
    project = _get_project_or_404(db, project_id)
    filters = parse_analytics_filters(request.query_params)

    try:
        generation, is_new = create_project_generation(
            db, project, filters, force_regenerate=force_regenerate
        )
    except NarrativeServiceError as exc:
        return render_project_detail(
            request,
            db,
            project,
            active_tab="insights",
            narrative_message={"type": "error", "text": exc.message},
            status_code=exc.status_code,
        )

    if is_new:
        generation = _trigger_or_fail(db, generation)
        if generation.status == NarrativeGenerationStatus.FAILED:
            return RedirectResponse(
                url=f"/narrative-generations/{generation.id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )

    # Post/Redirect/Get prevents a browser refresh from re-submitting the
    # generation form. Successful and reused generations return to Insights.
    return RedirectResponse(
        url=f"/projects/{project.id}?tab=insights",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/compare/narratives/start")
def start_comparison_narrative_generation(
    request: Request,
    db: Session = Depends(get_db),
    baseline_project_ids: list[uuid.UUID] = Form(...),
    comparison_project_ids: list[uuid.UUID] = Form(...),
    force_regenerate: bool = Form(False),
):
    try:
        filters = parse_analytics_filters(request.query_params)
        baseline_params = extract_prefixed_filter_params(request.query_params, "baseline_filter_")
        comparison_params = extract_prefixed_filter_params(request.query_params, "comparison_filter_")
        baseline_filters = parse_analytics_filters(baseline_params) if baseline_params else None
        comparison_filters = parse_analytics_filters(comparison_params) if comparison_params else None
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc

    try:
        generation, is_new = create_comparison_generation(
            db,
            baseline_project_ids,
            comparison_project_ids,
            filters,
            force_regenerate=force_regenerate,
            baseline_filters=baseline_filters,
            comparison_filters=comparison_filters,
        )
    except NarrativeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    if is_new:
        generation = _trigger_or_fail(db, generation)

    return RedirectResponse(
        url=f"/narrative-generations/{generation.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/narrative-generations/{generation_id}")
def narrative_generation_detail_page(
    generation_id: uuid.UUID, request: Request, db: Session = Depends(get_db)
):
    generation = db.get(NarrativeGeneration, generation_id)
    if generation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Narrative generation not found."
        )

    valid_insights = [
        insight
        for insight in generation.insights
        if insight.validation_status == NarrativeValidationStatus.VALID
    ]

    return render(
        request,
        "narrative_generation_detail.html",
        {"generation": generation, "valid_insights": valid_insights},
    )
