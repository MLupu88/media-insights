import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.pages import render, render_project_detail
from app.database import get_db
from app.models.chat import ChatRun, ChatRunStatus, ChatSession
from app.models.project import Project
from app.security.auth import require_web_session
from app.services.analytics import AnalyticsFilterError, parse_analytics_filters
from app.services.analytics_filters import extract_prefixed_filter_params
from app.services.chat_service import (
    ChatServiceError,
    create_run,
    find_or_create_comparison_session,
    find_or_create_project_session,
    retry_run,
)
from app.services.n8n import N8nTriggerError, trigger_chat_run

router = APIRouter(dependencies=[Depends(require_web_session)])


def _get_project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found."
        )
    return session


def _get_run_or_404(db: Session, run_id: uuid.UUID) -> ChatRun:
    run = db.get(ChatRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat run not found.")
    return run


def _trigger_or_fail(db: Session, run: ChatRun) -> ChatRun:
    try:
        trigger_chat_run(run.id, run.session_id)
    except N8nTriggerError as exc:
        run.status = ChatRunStatus.FAILED
        run.error_message = str(exc)
        db.commit()
        db.refresh(run)
    return run


@router.get("/api/ui/chat-runs/{run_id}/status")
def get_chat_run_ui_status(run_id: uuid.UUID, db: Session = Depends(get_db)):
    """Browser-facing, session-authenticated status probe used only for
    progressive UI polling. It deliberately returns the smallest possible
    payload and never exposes prompts, evidence, errors, or internal secrets.
    """
    run = _get_run_or_404(db, run_id)
    return JSONResponse(
        content={
            "id": str(run.id),
            "status": run.status,
            "terminal": run.status in ChatRunStatus.TERMINAL,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/projects/{project_id}/chat/ask")
def ask_project_chat(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    question: str = Form(...),
):
    project = _get_project_or_404(db, project_id)
    filters = parse_analytics_filters(request.query_params)

    try:
        session = find_or_create_project_session(db, project, filters)
        run = create_run(db, session, question)
    except ChatServiceError as exc:
        return render_project_detail(
            request,
            db,
            project,
            active_tab="chat",
            chat_message={"type": "error", "text": exc.message},
            status_code=exc.status_code,
        )

    _trigger_or_fail(db, run)

    # Post/Redirect/Get: the browser must land on a GET page after creating a
    # run. Otherwise a manual refresh re-submits the last question and creates
    # a duplicate run/message.
    return RedirectResponse(
        url=f"/projects/{project.id}?tab=chat",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/compare/chat/ask")
def ask_comparison_chat(
    request: Request,
    db: Session = Depends(get_db),
    baseline_project_ids: list[uuid.UUID] = Form(...),
    comparison_project_ids: list[uuid.UUID] = Form(...),
    question: str = Form(...),
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
        session = find_or_create_comparison_session(
            db, baseline_project_ids, comparison_project_ids,
            filters=baseline_filters or filters, comparison_filters=comparison_filters,
        )
        run = create_run(db, session, question)
    except ChatServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    _trigger_or_fail(db, run)

    return RedirectResponse(
        url=f"/chat-sessions/{session.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/chat-sessions/{session_id}/ask")
def ask_in_chat_session(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    question: str = Form(...),
):
    session = _get_session_or_404(db, session_id)

    try:
        run = create_run(db, session, question)
    except ChatServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    _trigger_or_fail(db, run)

    return RedirectResponse(
        url=f"/chat-sessions/{session.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/chat-runs/{run_id}/retry")
def retry_chat_run(run_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)

    try:
        new_run = retry_run(db, run)
    except ChatServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    _trigger_or_fail(db, new_run)

    session = new_run.session
    if session.baseline_project_ids:
        return RedirectResponse(
            url=f"/chat-sessions/{session.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    project = _get_project_or_404(db, session.project_id)
    return RedirectResponse(
        url=f"/projects/{project.id}?tab=chat",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/chat-sessions/{session_id}")
def chat_session_detail_page(
    session_id: uuid.UUID, request: Request, db: Session = Depends(get_db)
):
    session = _get_session_or_404(db, session_id)
    return render(
        request,
        "chat_session_detail.html",
        {"chat_session": session},
    )
