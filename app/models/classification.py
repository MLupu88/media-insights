import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

CURRENT_MODEL = "deepseek-chat"
CURRENT_PROMPT_VERSION = "retail-deepseek-v2"

LOW_CONFIDENCE_THRESHOLD = 0.6


class ClassificationTaxonomy:
    PRIMARY_TOPICS: tuple[str, ...] = (
        "promotions_pricing",
        "products_private_label",
        "store_expansion",
        "financial_results",
        "investment_operations",
        "sustainability",
        "csr_community",
        "employer_branding",
        "digital_ecommerce",
        "logistics_operations",
        "partnerships_campaigns",
        "market_research",
        "leadership",
        "crisis_controversy",
        "regulation",
        "corporate_reputation",
        "incidental_mention",
        "other",
    )

    COMMUNICATION_CATEGORIES: tuple[str, ...] = (
        "commercial",
        "corporate",
        "product",
        "employer_branding",
        "csr",
        "thought_leadership",
        "reactive_crisis",
        "earned_editorial",
        "incidental",
    )

    SENTIMENTS: tuple[str, ...] = ("positive", "neutral", "negative", "mixed")

    BRAND_ROLES: tuple[str, ...] = (
        "primary_focus",
        "secondary_mention",
        "incidental_mention",
    )


class ClassificationReviewStatus:
    """Human review state for one Classification row -- independent of
    RetailerReviewStatus (Article-level brand-assignment review), which
    this deliberately does not touch or replace.

    A freshly-produced classification starts PENDING only if its
    confidence is below LOW_CONFIDENCE_THRESHOLD; confident output starts
    APPROVED (see app.services.classification.initial_review_status,
    applied at save time -- this default is a defensive fallback only,
    never the actual decision path). This keeps the Classification Review
    queue (review_status == PENDING) scoped to what genuinely needs a
    human look, never all 30,000+ classifications in a large project.
    APPROVED rows can still be edited (-> CORRECTED); moving a row back to
    PENDING is reserved for a dedicated "flag for review" action, not an
    automatic side effect. Existing rows created before this review
    workflow existed are backfilled to APPROVED by the migration
    (mirroring RetailerReviewStatus's precedent: historical data is
    treated as already-resolved, never dumped into a new review queue
    retroactively).
    """

    PENDING = "pending"
    APPROVED = "approved"
    CORRECTED = "corrected"

    ALL = (PENDING, APPROVED, CORRECTED)


class ClassificationBatchStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"

    ALL = (PENDING, RUNNING, COMPLETE, FAILED)
    ACTIVE = (PENDING, RUNNING)


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        Index("ix_classifications_project_id", "project_id"),
        Index("ix_classifications_project_id_review_status", "project_id", "review_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    primary_topic: Mapped[str] = mapped_column(String(64), nullable=False)
    secondary_topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    communication_category: Mapped[str] = mapped_column(String(64), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    brand_role: Mapped[str] = mapped_column(String(32), nullable=False)
    story_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    rationale_ro: Mapped[str | None] = mapped_column(Text, nullable=True)

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)

    # Human review audit trail. `original_ai_labels` is a snapshot of the
    # six editable fields (primary_topic/secondary_topic/
    # communication_category/sentiment/brand_role/story_key) taken the
    # FIRST time a correction is made, and never overwritten again --
    # confidence/rationale_ro are never edited, so they always remain the
    # AI's own original output and need no separate snapshot. The current
    # row fields are always the "effective" values (AI-original if never
    # corrected, human-corrected otherwise); see
    # app.services.classification_results.get_effective_classification_values.
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ClassificationReviewStatus.PENDING
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_ai_labels: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    classified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    article = relationship("Article", back_populates="classification")
    project = relationship("Project", back_populates="classifications")


class ClassificationBatch(Base):
    __tablename__ = "classification_batches"
    __table_args__ = (
        Index("ix_classification_batches_project_id", "project_id"),
        Index("ix_classification_batches_project_id_status", "project_id", "status"),
        # One active (pending/running) batch per project at a time -- the
        # DB-level guard against duplicate batch creation from concurrent
        # "get next batch" calls, mirroring ux_chat_runs_active_per_session.
        Index(
            "ux_classification_batches_active_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ClassificationBatchStatus.PENDING
    )
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    project = relationship("Project", back_populates="classification_batches")
    batch_articles = relationship(
        "ClassificationBatchArticle", back_populates="batch", cascade="all, delete-orphan"
    )


class ClassificationBatchArticle(Base):
    __tablename__ = "classification_batch_articles"
    __table_args__ = (
        Index("ix_classification_batch_articles_article_id", "article_id"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("classification_batches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("articles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    batch = relationship("ClassificationBatch", back_populates="batch_articles")
    article = relationship("Article")
