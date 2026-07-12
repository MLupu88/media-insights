import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.services.chat_contract import ChatMessageRole, ChatRunStatus, ChatValidationStatus

__all__ = [
    "ChatMessageRole",
    "ChatRunStatus",
    "ChatValidationStatus",
    "ChatSession",
    "ChatMessage",
    "ChatRun",
]


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Ownership/navigation anchor only — never used to compute
    # comparison-scoped answers. See app/services/chat_service.py.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # `none_as_null=True` so a Python `None` binds to true SQL NULL rather
    # than the JSONB scalar `null` — otherwise `.is_(None)` queries against
    # these columns silently misbehave (a JSONB `null` is not SQL NULL in
    # Postgres). See app/services/chat_service.py::get_project_own_chat_session.
    baseline_project_ids: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    comparison_project_ids: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    filters: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ro")

    # SHA-256 over the canonicalized session identity (scope kind, project
    # id or sorted/deduped baseline+comparison ids, filters, language).
    # Unique so find-or-create is a concurrency-safe upsert, never a race
    # between a check and an insert.
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="chat_sessions", foreign_keys=[project_id])
    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
        foreign_keys="ChatMessage.session_id",
    )
    runs = relationship(
        "ChatRun",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatRun.created_at",
        foreign_keys="ChatRun.session_id",
    )

    __table_args__ = (
        Index("ux_chat_sessions_scope_key", "scope_key", unique=True),
        Index("ix_chat_sessions_project_id", "project_id"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_id_created_at", "session_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Set only on assistant messages — which run produced this answer.
    # `use_alter` breaks the chat_messages <-> chat_runs circular FK so
    # both tables can be created in a single, ordered migration.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_runs.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_chat_messages_run_id",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session = relationship(
        "ChatSession", back_populates="messages", foreign_keys=[session_id]
    )
    run = relationship("ChatRun", foreign_keys=[run_id])


class ChatRun(Base):
    __tablename__ = "chat_runs"
    __table_args__ = (
        Index("ix_chat_runs_session_id_status", "session_id", "status"),
        # One active (pending/running) run per session at a time — the
        # DB-level concurrency guard against double-submits/races. Strictly
        # stronger than "one active run per user_message_id" (which it also
        # guarantees) and simpler to reason about.
        Index(
            "ux_chat_runs_active_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    retry_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_runs.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ChatRunStatus.PENDING
    )

    # Idempotency: identical retries of a plan/answer submission replay the
    # persisted outcome (including its original HTTP status) rather than
    # re-executing tools or re-validating; conflicting resubmissions are
    # rejected. See app/services/chat_service.py.
    plan_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Persisted at run-creation time, before n8n is even triggered — the
    # exact bounded context the planning step receives. Never recomputed.
    planning_payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Persisted the moment tool execution succeeds — the exact bounded
    # context (question, history, scope, versions, tool_results) the answer
    # step receives. The single source of truth for evidence grounding;
    # there is no separate top-level tool_results column.
    answer_payload_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # SHA-256 over the exact answer_payload_snapshot — audit fingerprint
    # only, never used for dedup/reuse (unlike narrative generations, every
    # chat question always executes fresh).
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    related_brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    related_topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_publication: Mapped[str | None] = mapped_column(String(500), nullable=True)
    related_story_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_article_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_urls: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(32), nullable=False)

    validation_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session = relationship("ChatSession", back_populates="runs", foreign_keys=[session_id])
    user_message = relationship("ChatMessage", foreign_keys=[user_message_id])
    retry_of = relationship("ChatRun", remote_side=[id])
