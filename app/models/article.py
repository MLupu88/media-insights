import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ImportStatus:
    VALID = "valid"
    INVALID = "invalid"

    ALL = (VALID, INVALID)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index("ix_articles_project_id", "project_id"),
        Index("ix_articles_uploaded_file_id", "uploaded_file_id"),
        Index("ix_articles_fingerprint", "fingerprint"),
        Index("ix_articles_project_id_fingerprint", "project_id", "fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
    )

    original_row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    retailer: Mapped[str] = mapped_column(String(64), nullable=False)
    medium: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publication_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    article_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mediatrust_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience: Mapped[float | None] = mapped_column(Float, nullable=True)
    ave: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_original: Mapped[str | None] = mapped_column(String(64), nullable=True)
    importance_original: Mapped[str | None] = mapped_column(String(64), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    county: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_audience: Mapped[float | None] = mapped_column(Float, nullable=True)
    subfolder_1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subfolder_2: Mapped[str | None] = mapped_column(String(255), nullable=True)

    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duplicate_of_article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("articles.id", ondelete="SET NULL"), nullable=True
    )

    import_status: Mapped[str] = mapped_column(String(16), nullable=False)
    import_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project = relationship("Project", back_populates="articles")
    uploaded_file = relationship("UploadedFile", back_populates="articles")
    duplicate_of = relationship("Article", remote_side=[id])
    classification = relationship(
        "Classification", back_populates="article", uselist=False, cascade="all, delete-orphan"
    )
