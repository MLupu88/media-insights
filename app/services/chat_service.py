"""Orchestrates chat session/run lifecycle: concurrency-safe session
find-or-create, run creation with an immutable planning snapshot, idempotent
plan/answer processing against n8n's two-step contract, and retry lineage.

Mirrors `app/services/narrative_service.py`'s job/run pattern, extended with
strict state transitions and idempotent replay — n8n may retry a request
that actually already succeeded or failed, and this module guarantees tool
execution and answer validation each happen at most once per run.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatRun, ChatSession
from app.models.project import Project
from app.schemas.chat import AnswerSubmission, PlanSubmission
from app.services.analytics import AnalyticsFilters, serialize_analytics_filters
from app.services.analytics_filters import serialize_phase_c_analytics_filters
from app.services.chat_contract import (
    MAX_CONVERSATION_HISTORY_MESSAGES,
    MAX_QUESTION_LENGTH,
    MAX_TOOL_RESULTS_BYTES,
    PAYLOAD_SCHEMA_VERSION,
    PROMPT_CONTRACT_VERSION,
    VALIDATOR_VERSION,
    ChatMessageRole,
    ChatRunStatus,
    ChatValidationStatus,
)
from app.services.chat_tools import (
    COMPARISON_SCOPE_TOOLS,
    PROJECT_SCOPE_TOOLS,
    TOOL_REGISTRY,
    ChatScopeContext,
    ToolValidationError,
    build_scope_context,
    execute_tool_call,
    find_latest_matching_generation,
    validate_and_parse_tool_call,
)
from app.services.chat_validation import validate_answer
from app.services.json_safe import hash_json


class ChatServiceError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass
class PlanOutcome:
    http_status: int
    tool_results: list[dict] | None = None
    error_detail: str | None = None


@dataclass
class AnswerOutcome:
    http_status: int
    status: str | None = None
    rejection_reason: str | None = None
    error_detail: str | None = None


# --- Session identity -----------------------------------------------------


def _scope_key_payload(
    kind: str,
    project_id: uuid.UUID | None,
    baseline_project_ids: list[uuid.UUID] | None,
    comparison_project_ids: list[uuid.UUID] | None,
    serialized_filters: dict,
    language: str,
) -> dict:
    return {
        "kind": kind,
        "project_id": str(project_id) if project_id else None,
        "baseline_project_ids": sorted({str(pid) for pid in baseline_project_ids})
        if baseline_project_ids
        else None,
        "comparison_project_ids": sorted({str(pid) for pid in comparison_project_ids})
        if comparison_project_ids
        else None,
        "filters": serialized_filters,
        "language": language,
    }


def compute_scope_key(
    kind: str,
    project_id: uuid.UUID | None,
    baseline_project_ids: list[uuid.UUID] | None,
    comparison_project_ids: list[uuid.UUID] | None,
    filters: AnalyticsFilters,
    language: str,
    comparison_filters: AnalyticsFilters | None = None,
) -> str:
    """`comparison_filters` (Phase E — same-project brand-vs-brand
    comparison): when baseline and comparison sides use genuinely
    different filters, `filters` (the baseline side) alone is not enough
    to distinguish e.g. an "Auchan vs Carrefour" session from a "Lidl vs
    Profi" one for the same project pair -- both must be folded into the
    scope_key, or two different brand-pair chat sessions would collide
    into one row. Omitted (the default) for every existing call shape.
    """
    payload = _scope_key_payload(
        kind, project_id, baseline_project_ids, comparison_project_ids,
        serialize_analytics_filters(filters), language,
    )
    if comparison_filters is not None:
        payload["comparison_filter_identity"] = serialize_analytics_filters(comparison_filters)
    return hash_json(payload)


def _compute_legacy_phase_c_scope_key(
    kind: str,
    project_id: uuid.UUID | None,
    baseline_project_ids: list[uuid.UUID] | None,
    comparison_project_ids: list[uuid.UUID] | None,
    filters: AnalyticsFilters,
    language: str,
) -> str:
    """Reproduces the exact Phase C `compute_scope_key` payload shape
    (six-field `filters`, always present -- see
    `serialize_phase_c_analytics_filters`'s docstring), so a `ChatSession`
    created before this correction remains reachable by the same logical
    filter request. Used only for fallback reads, never for new writes.
    """
    payload = _scope_key_payload(
        kind, project_id, baseline_project_ids, comparison_project_ids,
        serialize_phase_c_analytics_filters(filters), language,
    )
    return hash_json(payload)


def _find_by_scope_key(db: Session, scope_key: str) -> ChatSession | None:
    return db.execute(
        select(ChatSession).where(ChatSession.scope_key == scope_key)
    ).scalar_one_or_none()


def _find_existing_session(
    db: Session,
    kind: str,
    project_id: uuid.UUID | None,
    baseline_project_ids: list[uuid.UUID] | None,
    comparison_project_ids: list[uuid.UUID] | None,
    filters: AnalyticsFilters,
    language: str,
    comparison_filters: AnalyticsFilters | None = None,
) -> tuple[str, ChatSession | None]:
    """Returns the canonical scope_key plus, if found, an existing session
    -- checking the canonical key first, then (only when it differs) the
    exact legacy Phase C key, so a pre-existing session is reused rather
    than silently duplicated. On a legacy hit, the session is returned
    as-is; only future writes use the canonical key. The legacy fallback
    never includes `comparison_filters` -- Phase C had no such concept.
    """
    scope_key = compute_scope_key(
        kind, project_id, baseline_project_ids, comparison_project_ids, filters, language,
        comparison_filters=comparison_filters,
    )
    existing = _find_by_scope_key(db, scope_key)
    if existing is not None:
        return scope_key, existing

    legacy_scope_key = _compute_legacy_phase_c_scope_key(
        kind, project_id, baseline_project_ids, comparison_project_ids, filters, language
    )
    if legacy_scope_key != scope_key:
        existing = _find_by_scope_key(db, legacy_scope_key)
        if existing is not None:
            return scope_key, existing

    return scope_key, None


def _upsert_session(
    db: Session,
    scope_key: str,
    *,
    project_id: uuid.UUID,
    baseline_project_ids: list[str] | None,
    comparison_project_ids: list[str] | None,
    filters: AnalyticsFilters,
    language: str,
) -> ChatSession:
    """Concurrency-safe find-or-create: an `ON CONFLICT DO NOTHING` upsert
    on the unique `scope_key`, never a check-then-insert race. Two
    simultaneous requests for the same scope always resolve to one row.
    Always writes the canonical filter shape -- see
    `serialize_analytics_filters`.
    """
    stmt = (
        pg_insert(ChatSession)
        .values(
            project_id=project_id,
            baseline_project_ids=baseline_project_ids,
            comparison_project_ids=comparison_project_ids,
            filters=serialize_analytics_filters(filters),
            language=language,
            scope_key=scope_key,
        )
        .on_conflict_do_nothing(index_elements=["scope_key"])
        .returning(ChatSession.id)
    )
    result = db.execute(stmt)
    db.commit()
    new_id = result.scalar_one_or_none()
    if new_id is not None:
        return db.get(ChatSession, new_id)
    return db.execute(
        select(ChatSession).where(ChatSession.scope_key == scope_key)
    ).scalar_one()


def find_or_create_project_session(
    db: Session, project: Project, filters: AnalyticsFilters | None = None, language: str = "ro"
) -> ChatSession:
    filters = filters or AnalyticsFilters()
    scope_key, existing = _find_existing_session(
        db, "project", project.id, None, None, filters, language
    )
    if existing is not None:
        return existing
    return _upsert_session(
        db,
        scope_key,
        project_id=project.id,
        baseline_project_ids=None,
        comparison_project_ids=None,
        filters=filters,
        language=language,
    )


def find_or_create_comparison_session(
    db: Session,
    baseline_project_ids: list[uuid.UUID],
    comparison_project_ids: list[uuid.UUID],
    filters: AnalyticsFilters | None = None,
    language: str = "ro",
    comparison_filters: AnalyticsFilters | None = None,
) -> ChatSession:
    """`comparison_filters` (Phase E): when given and distinct from
    `filters`, distinguishes this session's scope_key from a different
    same-project brand-pair's session (see `compute_scope_key`). The
    session's own stored `filters`/tool-execution scope remains `filters`
    (the baseline side) -- chat tool execution is not yet split-filter
    aware; this only prevents two different brand-pair sessions from
    colliding into one row.
    """
    filters = filters or AnalyticsFilters()
    unique_baseline = sorted({str(pid) for pid in baseline_project_ids})
    unique_comparison = sorted({str(pid) for pid in comparison_project_ids})
    if not unique_baseline or not unique_comparison:
        raise ChatServiceError(
            "Both baseline and comparison project selections are required.", 422
        )
    scope_key, existing = _find_existing_session(
        db, "comparison", None, baseline_project_ids, comparison_project_ids, filters, language,
        comparison_filters=comparison_filters,
    )
    if existing is not None:
        return existing
    anchor_project_id = uuid.UUID(unique_baseline[0])
    return _upsert_session(
        db,
        scope_key,
        project_id=anchor_project_id,
        baseline_project_ids=unique_baseline,
        comparison_project_ids=unique_comparison,
        filters=filters,
        language=language,
    )


def get_project_own_chat_session(db: Session, project_id: uuid.UUID) -> ChatSession | None:
    """The project's own project-scoped session (not a comparison session
    that merely happens to be anchored here) — for the project workspace's
    Chat tab. Read-only; never creates one.
    """
    stmt = select(ChatSession).where(
        ChatSession.project_id == project_id, ChatSession.baseline_project_ids.is_(None)
    )
    return db.execute(stmt).scalar_one_or_none()


def find_comparison_session(
    db: Session,
    baseline_project_ids: list[uuid.UUID],
    comparison_project_ids: list[uuid.UUID],
    filters: AnalyticsFilters | None = None,
    language: str = "ro",
    comparison_filters: AnalyticsFilters | None = None,
) -> ChatSession | None:
    """Read-only lookup for the comparison chat area on `/compare` — shows
    a "View conversation" link if a session already exists, never creates
    one on a page load. `comparison_filters` (Phase E) distinguishes a
    same-project brand-pair session from a different brand-pair session
    for the same project pair -- see `compute_scope_key`.
    """
    if not baseline_project_ids or not comparison_project_ids:
        return None
    filters = filters or AnalyticsFilters()
    _scope_key, existing = _find_existing_session(
        db, "comparison", None, baseline_project_ids, comparison_project_ids, filters, language,
        comparison_filters=comparison_filters,
    )
    return existing


# --- Planning snapshot ------------------------------------------------------


def _describe_scope(scope: ChatScopeContext) -> dict:
    if scope.kind == "project":
        return {
            "kind": "project",
            "project_name": scope.project.name,
            "project_quarter": scope.project.quarter,
        }
    return {
        "kind": "comparison",
        "baseline_labels": [f"{p.name} ({p.quarter})" for p in scope.baseline_projects],
        "comparison_labels": [f"{p.name} ({p.quarter})" for p in scope.comparison_projects],
    }


def _describe_tool_registry(scope_kind: str) -> list[dict]:
    allowed = PROJECT_SCOPE_TOOLS if scope_kind == "project" else COMPARISON_SCOPE_TOOLS
    return [
        {
            "tool": name,
            "parameters_schema": TOOL_REGISTRY[name].params_schema.model_json_schema(),
        }
        for name in allowed
    ]


def _conversation_history_before(
    db: Session, session: ChatSession, before_message_id: uuid.UUID | None
) -> list[dict]:
    stmt = select(ChatMessage).where(ChatMessage.session_id == session.id)
    if before_message_id is not None:
        reference = db.get(ChatMessage, before_message_id)
        stmt = stmt.where(ChatMessage.created_at < reference.created_at)
    stmt = stmt.order_by(ChatMessage.created_at.desc()).limit(MAX_CONVERSATION_HISTORY_MESSAGES)
    messages = list(db.execute(stmt).scalars().all())
    messages.reverse()
    return [{"role": m.role, "content": m.content} for m in messages]


def _build_planning_payload_snapshot(
    session: ChatSession,
    question: str,
    history: list[dict],
    scope: ChatScopeContext,
    narrative_insights_available: bool,
) -> dict:
    return {
        "question": question,
        "conversation_history": history,
        "scope": _describe_scope(scope),
        "tool_registry": _describe_tool_registry(scope.kind),
        "narrative_insights_available": narrative_insights_available,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
        "language": session.language,
    }


def _new_run(
    db: Session,
    session: ChatSession,
    user_message_id: uuid.UUID,
    planning_payload_snapshot: dict,
    retry_of_run_id: uuid.UUID | None = None,
) -> ChatRun:
    run = ChatRun(
        session_id=session.id,
        user_message_id=user_message_id,
        retry_of_run_id=retry_of_run_id,
        status=ChatRunStatus.PENDING,
        planning_payload_snapshot=planning_payload_snapshot,
        prompt_contract_version=PROMPT_CONTRACT_VERSION,
        payload_schema_version=PAYLOAD_SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ChatServiceError(
            "A question is already being answered in this session.", 409
        ) from exc
    db.refresh(run)
    return run


def create_run(db: Session, session: ChatSession, question: str) -> ChatRun:
    """Appends the user's question and creates its `pending` run. Guarded
    server-side, not just in the UI: an app-level pre-check plus the DB's
    partial unique index (`ux_chat_runs_active_per_session`) together
    guarantee at most one active run per session, and a lost race rolls
    back the whole transaction — including the just-added question — so no
    duplicate user message is ever left behind.
    """
    question = question.strip()
    if not question:
        raise ChatServiceError("Question must not be empty.", 422)
    if len(question) > MAX_QUESTION_LENGTH:
        raise ChatServiceError(
            f"Question exceeds the {MAX_QUESTION_LENGTH}-character limit.", 422
        )

    existing_active = db.execute(
        select(ChatRun).where(
            ChatRun.session_id == session.id, ChatRun.status.in_(ChatRunStatus.ACTIVE)
        )
    ).scalar_one_or_none()
    if existing_active is not None:
        raise ChatServiceError("A question is already being answered in this session.", 409)

    history = _conversation_history_before(db, session, before_message_id=None)
    scope = build_scope_context(db, session)
    narrative_insights_available = find_latest_matching_generation(db, scope) is not None

    message = ChatMessage(session_id=session.id, role=ChatMessageRole.USER, content=question)
    db.add(message)
    db.flush()

    planning_payload_snapshot = _build_planning_payload_snapshot(
        session, question, history, scope, narrative_insights_available
    )
    return _new_run(db, session, message.id, planning_payload_snapshot)


def retry_run(db: Session, failed_run: ChatRun) -> ChatRun:
    if failed_run.status != ChatRunStatus.FAILED:
        raise ChatServiceError("Only a failed run can be retried.", 409)

    session = failed_run.session
    user_message = failed_run.user_message

    existing_active = db.execute(
        select(ChatRun).where(
            ChatRun.session_id == session.id, ChatRun.status.in_(ChatRunStatus.ACTIVE)
        )
    ).scalar_one_or_none()
    if existing_active is not None:
        raise ChatServiceError("A question is already being answered in this session.", 409)

    history = _conversation_history_before(db, session, before_message_id=user_message.id)
    scope = build_scope_context(db, session)
    narrative_insights_available = find_latest_matching_generation(db, scope) is not None

    planning_payload_snapshot = _build_planning_payload_snapshot(
        session, user_message.content, history, scope, narrative_insights_available
    )
    return _new_run(
        db, session, user_message.id, planning_payload_snapshot, retry_of_run_id=failed_run.id
    )


# --- Plan processing (idempotent) -------------------------------------------


def process_plan(db: Session, run: ChatRun, submission: PlanSubmission) -> PlanOutcome:
    request_hash = hash_json(submission.model_dump(mode="json"))

    if run.status != ChatRunStatus.PENDING:
        if run.plan_request_hash == request_hash:
            if run.answer_payload_snapshot is not None:
                return PlanOutcome(200, tool_results=run.answer_payload_snapshot["tool_results"])
            return PlanOutcome(422, error_detail=run.error_message)
        return PlanOutcome(409, error_detail="A different plan was already submitted for this run.")

    if submission.payload_schema_version != run.payload_schema_version:
        return _fail_plan(
            db, run, request_hash,
            "payload_schema_version does not match this run's persisted contract.",
        )

    scope = build_scope_context(db, run.session)

    validated_calls: list[tuple[str, BaseModel]] = []
    for tool_call in submission.tool_calls:
        try:
            params = validate_and_parse_tool_call(db, scope, tool_call.tool, tool_call.parameters)
        except ToolValidationError as exc:
            return _fail_plan(db, run, request_hash, exc.message)
        validated_calls.append((tool_call.tool, params))

    tool_results = [
        execute_tool_call(db, scope, name, params) for name, params in validated_calls
    ]

    serialized_size = len(json.dumps(tool_results).encode("utf-8"))
    if serialized_size > MAX_TOOL_RESULTS_BYTES:
        return _fail_plan(db, run, request_hash, "Tool results exceeded the size bound.")

    answer_payload_snapshot = {**run.planning_payload_snapshot, "tool_results": tool_results}
    source_hash = hash_json(answer_payload_snapshot)

    run.tool_calls = [tc.model_dump() for tc in submission.tool_calls]
    run.tool_call_count = len(submission.tool_calls)
    run.answer_payload_snapshot = answer_payload_snapshot
    run.source_hash = source_hash
    run.plan_request_hash = request_hash
    run.model = submission.model
    run.prompt_version = submission.prompt_version
    run.status = ChatRunStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)

    return PlanOutcome(200, tool_results=tool_results)


def _fail_plan(db: Session, run: ChatRun, request_hash: str, reason: str) -> PlanOutcome:
    run.status = ChatRunStatus.FAILED
    run.error_message = reason
    run.plan_request_hash = request_hash
    db.commit()
    db.refresh(run)
    return PlanOutcome(422, error_detail=reason)


# --- Answer processing (idempotent) -----------------------------------------


def process_answer(db: Session, run: ChatRun, submission: AnswerSubmission) -> AnswerOutcome:
    request_hash = hash_json(submission.model_dump(mode="json"))

    if run.status != ChatRunStatus.RUNNING:
        if run.status == ChatRunStatus.PENDING:
            return AnswerOutcome(409, error_detail="This run has no submitted plan yet.")
        if run.answer_request_hash == request_hash:
            if run.status == ChatRunStatus.COMPLETE:
                return AnswerOutcome(200, status="complete")
            return AnswerOutcome(422, status="failed", rejection_reason=run.rejection_reason)
        return AnswerOutcome(
            409, error_detail="A different answer was already submitted for this run."
        )

    if submission.payload_schema_version != run.payload_schema_version:
        return _fail_answer(
            db, run, request_hash, submission,
            "payload_schema_version does not match this run's persisted contract.",
        )

    result = validate_answer(submission, run.answer_payload_snapshot)
    if not result.valid:
        return _fail_answer(db, run, request_hash, submission, result.reason)

    message = ChatMessage(
        session_id=run.session_id,
        role=ChatMessageRole.ASSISTANT,
        content=submission.answer_text,
        run_id=run.id,
    )
    db.add(message)

    _apply_submission_fields(run, submission)
    run.answer_request_hash = request_hash
    run.status = ChatRunStatus.COMPLETE
    run.validation_status = ChatValidationStatus.VALID
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)

    return AnswerOutcome(200, status="complete")


def _apply_submission_fields(run: ChatRun, submission: AnswerSubmission) -> None:
    """Persists the submitted answer's content and citations onto the run,
    regardless of validation outcome — audit trail either way (see
    app/models/chat.py). A rejected answer's fields are never surfaced
    through a ChatMessage, only through this run row.
    """
    run.answer_text = submission.answer_text
    run.answer_type = submission.answer_type
    run.evidence = [e.model_dump(mode="json") for e in submission.evidence]
    run.related_brand = submission.related_brand
    run.related_topic = submission.related_topic
    run.related_publication = submission.related_publication
    run.related_story_key = submission.related_story_key
    run.related_article_ids = [str(a) for a in submission.related_article_ids]
    run.source_urls = submission.source_urls
    run.model = submission.model
    run.prompt_version = submission.prompt_version


def _fail_answer(
    db: Session, run: ChatRun, request_hash: str, submission: AnswerSubmission, reason: str
) -> AnswerOutcome:
    _apply_submission_fields(run, submission)
    run.answer_request_hash = request_hash
    run.status = ChatRunStatus.FAILED
    run.validation_status = ChatValidationStatus.REJECTED
    run.rejection_reason = reason
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return AnswerOutcome(422, status="failed", rejection_reason=reason)


# --- Queries -----------------------------------------------------------------


def get_project_chat_sessions(db: Session, project_id: uuid.UUID) -> list[ChatSession]:
    stmt = (
        select(ChatSession)
        .where(ChatSession.project_id == project_id)
        .order_by(ChatSession.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())
