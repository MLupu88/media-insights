import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UploadedFileStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = (PENDING, PROCESSING, COMPLETED, FAILED)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (Index("ix_uploaded_files_project_id", "project_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    retailer_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_retailer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workbook_sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UploadedFileStatus.PENDING
    )

    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="uploaded_files")
    articles = relationship(
        "Article", back_populates="uploaded_file", cascade="all, delete-orphan"
    )
