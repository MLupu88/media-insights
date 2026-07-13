import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ImportBatchStatus:
    """`PROCESSING` is set the moment the batch row is created, before any
    file is parsed, and is the row's terminal state if the request is
    interrupted by a hard crash — nothing ever sweeps a stuck batch back to
    a different status, so `processing` with `completed_at IS NULL` is
    always distinguishable from a genuinely completed batch.
    """

    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"

    ALL = (PROCESSING, COMPLETED, PARTIALLY_COMPLETED, FAILED)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (Index("ix_import_batches_project_id", "project_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ImportBatchStatus.PROCESSING
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    files_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_review_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project = relationship("Project", back_populates="import_batches")
    uploaded_files = relationship("UploadedFile", back_populates="import_batch")
