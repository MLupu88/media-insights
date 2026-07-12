import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.chat import ChatRun, ChatSession
from app.models.project import Project
from app.schemas.chat import (
    AnswerSubmission,
    ChatAnswerResponse,
    ChatPlanningPayloadOut,
    ChatPlanResponse,
    ChatRunAuditOut,
    ChatRunStatusOut,
    ChatSessionListItem,
    ChatSessionOut,
    PlanSubmission,
)
from app.security.auth import require_internal_secret
from app.services.chat_service import get_project_chat_sessions, process_answer, process_plan

router = APIRouter(prefix="/api/internal", dependencies=[Depends(require_internal_secret)])


def _get_run_or_404(db: Session, run_id: uuid.UUID) -> ChatRun:
    run = db.get(ChatRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat run not found.")
    return run


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found."
        )
    return session


@router.get(
    "/chat-runs/{run_id}/planning-payload", response_model=ChatPlanningPayloadOut
)
def get_chat_planning_payload(run_id: uuid.UUID, db: Session = Depends(get_db)):
    """n8n-facing. Returns the persisted `planning_payload_snapshot`
    verbatim — never recomputed.
    """
    run = _get_run_or_404(db, run_id)
    return ChatPlanningPayloadOut(run_id=run.id, snapshot=run.planning_payload_snapshot)


@router.post("/chat-runs/{run_id}/plan", response_model=ChatPlanResponse)
def submit_chat_plan(run_id: uuid.UUID, payload: PlanSubmission, db: Session = Depends(get_db)):
    """n8n-facing. Validates tool names/parameters/scope, executes every
    call server-side, and returns the bounded results. Idempotent: an
    identical retry replays the original response (including its original
    HTTP status) without re-executing anything; a conflicting resubmission
    is rejected with 409.
    """
    run = _get_run_or_404(db, run_id)
    outcome = process_plan(db, run, payload)
    if outcome.http_status != 200:
        raise HTTPException(status_code=outcome.http_status, detail=outcome.error_detail)
    return ChatPlanResponse(tool_results=outcome.tool_results)


@router.post("/chat-runs/{run_id}/answer", response_model=ChatAnswerResponse)
def submit_chat_answer(
    run_id: uuid.UUID, payload: AnswerSubmission, db: Session = Depends(get_db)
):
    """n8n-facing. Validates the final answer against the persisted
    `answer_payload_snapshot`. Idempotent: an identical retry of a
    previously-*rejected* submission replays the same 422 with the same
    stored reason — never a 200 — and never re-runs validation; a
    conflicting resubmission is 409.
    """
    run = _get_run_or_404(db, run_id)
    outcome = process_answer(db, run, payload)
    if outcome.http_status == 200:
        return ChatAnswerResponse(status=outcome.status)
    if outcome.http_status == 422:
        raise HTTPException(status_code=422, detail=outcome.rejection_reason)
    raise HTTPException(status_code=outcome.http_status, detail=outcome.error_detail)


@router.get("/chat-runs/{run_id}/status", response_model=ChatRunStatusOut)
def get_chat_run_status(run_id: uuid.UUID, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    return ChatRunStatusOut(
        id=run.id,
        status=run.status,
        rejection_reason=run.rejection_reason,
        error_message=run.error_message,
    )


@router.get("/chat-runs/{run_id}", response_model=ChatRunAuditOut)
def get_chat_run_audit(run_id: uuid.UUID, db: Session = Depends(get_db)):
    """Debugging only — includes the raw snapshots, hashes, and (if
    applicable) the rejected answer text. Never used by any browser-facing
    surface.
    """
    run = _get_run_or_404(db, run_id)
    return ChatRunAuditOut.model_validate(run)


@router.get("/chat-sessions/{session_id}", response_model=ChatSessionOut)
def get_chat_session(session_id: uuid.UUID, db: Session = Depends(get_db)):
    session = _get_session_or_404(db, session_id)
    return ChatSessionOut.model_validate(session)


@router.get(
    "/projects/{project_id}/chat-sessions", response_model=list[ChatSessionListItem]
)
def list_project_chat_sessions(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    sessions = get_project_chat_sessions(db, project_id)
    return [
        ChatSessionListItem(
            id=s.id,
            scope="comparison" if s.baseline_project_ids else "project",
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]
