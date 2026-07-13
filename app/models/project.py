import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProjectStatus:
    CREATED = "created"
    IMPORTING = "importing"
    IMPORTED = "imported"
    CLASSIFYING = "classifying"
    CLASSIFIED = "classified"
    ANALYZED = "analyzed"

    ALL = (CREATED, IMPORTING, IMPORTED, CLASSIFYING, CLASSIFIED, ANALYZED)


class AnalysisStatus:
    """Project-level classification status.

    Reuses the existing `analysis_status` column (rather than adding a new
    field or overloading `Project.status`, which tracks import lifecycle) to
    track classification progress across the project's batches.
    """

    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "running"
    PARTIALLY_COMPLETE = "partially_complete"
    COMPLETE = "complete"
    FAILED = "failed"

    ALL = (NOT_STARTED, QUEUED, RUNNING, PARTIALLY_COMPLETE, COMPLETE, FAILED)


class Project(Base):
    """`quarter` is optional as of the reporting-scope model: a project must
    supply a valid `quarter` OR a complete `period_start`/`period_end` range
    (both together are also allowed) — enforced at both the Pydantic layer
    (`ProjectCreate`) and the DB layer (`ck_projects_period_integrity`), so
    a path that bypasses the API still can't create an invalid row.
    """

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "(quarter IS NOT NULL OR (period_start IS NOT NULL AND period_end IS NOT NULL)) "
            "AND (period_start IS NULL) = (period_end IS NULL) "
            "AND (period_start IS NULL OR period_end IS NULL OR period_end >= period_start)",
            name="ck_projects_period_integrity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    quarter: Mapped[str | None] = mapped_column(String(16), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProjectStatus.CREATED
    )

    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classified_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    analysis_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AnalysisStatus.NOT_STARTED
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    uploaded_files = relationship(
        "UploadedFile",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="UploadedFile.created_at.desc()",
    )
    import_batches = relationship(
        "ImportBatch",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ImportBatch.created_at.desc()",
    )
    articles = relationship(
        "Article", back_populates="project", cascade="all, delete-orphan"
    )
    classification_batches = relationship(
        "ClassificationBatch",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ClassificationBatch.created_at.desc()",
    )
    classifications = relationship(
        "Classification", back_populates="project", cascade="all, delete-orphan"
    )
    narrative_generations = relationship(
        "NarrativeGeneration",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="NarrativeGeneration.created_at.desc()",
        foreign_keys="NarrativeGeneration.project_id",
    )
    chat_sessions = relationship(
        "ChatSession",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ChatSession.created_at.desc()",
        foreign_keys="ChatSession.project_id",
    )
