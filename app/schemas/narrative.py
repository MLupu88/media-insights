import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.services.narrative_contract import NarrativeTypes

EVIDENCE_ROLES: tuple[str, ...] = ("baseline", "comparison", "delta", "value")


class EvidenceReference(BaseModel):
    path: str = Field(min_length=1)
    role: str
    value: float

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in EVIDENCE_ROLES:
            raise ValueError(f"Invalid evidence role: {value!r}")
        return value


class CandidateInsight(BaseModel):
    """Structural shape of one DeepSeek-generated insight, before grounding
    validation. Parsed per-candidate (not as part of one big list) so a
    single malformed candidate never discards the rest of a submission.
    """

    narrative_type: str
    key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1)
    narrative: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(min_length=1)
    related_brand: str | None = None
    related_topic: str | None = None
    related_publication: str | None = None
    related_story_key: str | None = None
    related_article_ids: list[uuid.UUID] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    confidence: float | None = None
    caveat: str | None = None

    @field_validator("narrative_type")
    @classmethod
    def _validate_narrative_type(cls, value: str) -> str:
        if value not in NarrativeTypes.ALL:
            raise ValueError(f"Invalid narrative_type: {value!r}")
        return value

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float | None) -> float | None:
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError("confidence must be between 0 and 1.")
        return value


class NarrativeResultsSubmission(BaseModel):
    """n8n's results payload. `insights` is intentionally untyped `dict`
    entries (not `list[CandidateInsight]`) — each is structurally validated
    independently inside the validator so one malformed item cannot fail
    the whole request.
    """

    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    payload_schema_version: str = Field(min_length=1)
    insights: list[dict] = Field(default_factory=list)


class NarrativeInsightOut(BaseModel):
    id: uuid.UUID
    narrative_type: str | None
    key: str | None
    title: str | None
    narrative: str | None
    evidence_type: str | None
    evidence: list[EvidenceReference] | None
    baseline_value: float | None
    comparison_value: float | None
    delta: float | None
    related_brand: str | None
    related_topic: str | None
    related_publication: str | None
    related_story_key: str | None
    related_article_ids: list[uuid.UUID] | None
    source_urls: list[str] | None
    confidence: float | None
    caveat: str | None

    model_config = {"from_attributes": True}


class NarrativeInsightAuditOut(NarrativeInsightOut):
    validation_status: str
    rejection_reason: str | None
    raw_candidate: dict


class NarrativeGenerationOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    narrative_types: list[str]
    baseline_project_ids: list[uuid.UUID] | None
    comparison_project_ids: list[uuid.UUID] | None
    language: str
    status: str
    missing_narrative_types: list[str] | None
    model: str | None
    prompt_version: str | None
    prompt_contract_version: str
    payload_schema_version: str
    validator_version: str
    input_hash: str
    regenerated_from_generation_id: uuid.UUID | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    insights: list[NarrativeInsightOut]


class NarrativeGenerationAuditOut(NarrativeGenerationOut):
    insights: list[NarrativeInsightAuditOut]


class NarrativeGenerationStatusOut(BaseModel):
    id: uuid.UUID
    status: str
    missing_narrative_types: list[str] | None


class NarrativeGenerationListItem(BaseModel):
    id: uuid.UUID
    status: str
    narrative_types: list[str]
    scope: str
    created_at: datetime
    model: str | None
    prompt_version: str | None
    input_hash: str
    regenerated_from_generation_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class NarrativePayloadOut(BaseModel):
    generation_id: uuid.UUID
    narrative_types: list[str]
    language: str
    prompt_contract_version: str
    payload_schema_version: str
    source_snapshot: dict


class NarrativeResultsResponse(BaseModel):
    status: str
    missing_narrative_types: list[str]
