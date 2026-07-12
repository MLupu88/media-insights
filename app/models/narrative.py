import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class NarrativeGenerationStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIALLY_COMPLETE = "partially_complete"
    FAILED = "failed"

    ALL = (PENDING, RUNNING, COMPLETE, PARTIALLY_COMPLETE, FAILED)
    ACTIVE = (PENDING, RUNNING)
    REUSABLE = (COMPLETE, PARTIALLY_COMPLETE)


class NarrativeValidationStatus:
    VALID = "valid"
    REJECTED = "rejected"

    ALL = (VALID, REJECTED)


class NarrativeGeneration(Base):
    __tablename__ = "narrative_generations"
    __table_args__ = (
        Index(
            "ix_narrative_generations_project_id_input_hash", "project_id", "input_hash"
        ),
        Index("ix_narrative_generations_project_id_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Ownership/navigation anchor only — never used to compute analytics,
    # evidence pools, or grounding validation. See app/services/narrative_service.py.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    narrative_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    baseline_project_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    comparison_project_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    filters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Immutable payload captured once at creation time. Never recomputed —
    # the payload-fetch endpoint and the results validator both read this
    # exact value, so underlying data changes after creation cannot affect
    # this generation's outcome.
    source_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ro")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=NarrativeGenerationStatus.PENDING
    )
    missing_narrative_types: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # n8n/DeepSeek-reported, informational only — never authoritative.
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # App-controlled, stamped from app.services.narrative_contract at
    # creation time. Cannot be overridden by n8n's results submission.
    prompt_contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(32), nullable=False)

    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    regenerated_from_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("narrative_generations.id", ondelete="SET NULL"),
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project = relationship(
        "Project", back_populates="narrative_generations", foreign_keys=[project_id]
    )
    regenerated_from = relationship("NarrativeGeneration", remote_side=[id])
    insights = relationship(
        "NarrativeInsight",
        back_populates="generation",
        cascade="all, delete-orphan",
        order_by="NarrativeInsight.created_at",
        foreign_keys="NarrativeInsight.generation_id",
    )


class NarrativeInsight(Base):
    __tablename__ = "narrative_insights"
    __table_args__ = (
        Index(
            "ix_narrative_insights_generation_id_validation_status",
            "generation_id",
            "validation_status",
        ),
        Index("ix_narrative_insights_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("narrative_generations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    # Nullable: a candidate that fails structural (Pydantic) validation has
    # no reliable parsed fields, but is still persisted for audit via
    # `raw_candidate` below rather than being silently dropped.
    narrative_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The authoritative grounding record: a list of
    # {"path", "role", "value"} objects, each validated independently
    # against the generation's persisted source_snapshot.
    evidence: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Derived summary columns only, populated from validated `evidence`
    # entries — never independently validated.
    baseline_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    comparison_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)

    related_brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    related_topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_publication: Mapped[str | None] = mapped_column(String(500), nullable=True)
    related_story_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_article_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_urls: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    caveat: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The exact submitted candidate object, always populated — the audit
    # fallback when structural validation fails and no other field is
    # trustworthy.
    raw_candidate: Mapped[dict] = mapped_column(JSONB, nullable=False)

    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    generation = relationship(
        "NarrativeGeneration", back_populates="insights", foreign_keys=[generation_id]
    )
