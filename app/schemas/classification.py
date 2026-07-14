import uuid
from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.classification import ClassificationTaxonomy


class ClassificationBatchArticleOut(BaseModel):
    article_id: uuid.UUID
    brand: str
    title: str | None
    subject: str | None
    publication: str | None
    date: date_type | None
    reach: float | None
    medium: str | None
    original_sentiment: str | None
    original_importance: str | None
    is_duplicate: bool


class ClassificationBatchOut(BaseModel):
    batch_id: uuid.UUID
    articles: list[ClassificationBatchArticleOut]


class ClassificationBatchesResponse(BaseModel):
    project_id: uuid.UUID
    batches: list[ClassificationBatchOut]
    already_running: bool = False


class ClassificationResultIn(BaseModel):
    article_id: uuid.UUID
    primary_topic: str
    secondary_topic: str | None = None
    communication_category: str
    sentiment: str
    brand_role: str
    story_key: str | None = None
    confidence: float
    rationale_ro: str | None = None

    @field_validator("primary_topic")
    @classmethod
    def _validate_primary_topic(cls, value: str) -> str:
        if value not in ClassificationTaxonomy.PRIMARY_TOPICS:
            raise ValueError(f"Invalid primary_topic: {value!r}")
        return value

    @field_validator("secondary_topic")
    @classmethod
    def _validate_secondary_topic(cls, value: str | None) -> str | None:
        if value is not None and value not in ClassificationTaxonomy.PRIMARY_TOPICS:
            raise ValueError(f"Invalid secondary_topic: {value!r}")
        return value

    @field_validator("communication_category")
    @classmethod
    def _validate_communication_category(cls, value: str) -> str:
        if value not in ClassificationTaxonomy.COMMUNICATION_CATEGORIES:
            raise ValueError(f"Invalid communication_category: {value!r}")
        return value

    @field_validator("sentiment")
    @classmethod
    def _validate_sentiment(cls, value: str) -> str:
        if value not in ClassificationTaxonomy.SENTIMENTS:
            raise ValueError(f"Invalid sentiment: {value!r}")
        return value

    @field_validator("brand_role")
    @classmethod
    def _validate_brand_role(cls, value: str) -> str:
        if value not in ClassificationTaxonomy.BRAND_ROLES:
            raise ValueError(f"Invalid brand_role: {value!r}")
        return value

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError("confidence must be between 0 and 1.")
        return value


class BulkClassificationRequest(BaseModel):
    project_id: uuid.UUID
    batch_id: uuid.UUID
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    results: list[ClassificationResultIn] = Field(min_length=1)
    partial_save: bool = False


class BulkClassificationResponse(BaseModel):
    status: str
    saved_count: int
    updated_count: int
    rejected_count: int


class BatchCompleteResponse(BaseModel):
    status: str
    batch_id: uuid.UUID


class ProjectSummaryResponse(BaseModel):
    project_id: uuid.UUID
    total_files: int
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    classified_rows: int
    unclassified_valid_rows: int
    classification_percentage: float
    low_confidence_count: int
    active_batch_count: int
    failed_batch_count: int
    last_classification_at: datetime | None
