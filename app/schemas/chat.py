import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.chat_contract import (
    MAX_ANSWER_LENGTH,
    MAX_EVIDENCE_ENTRIES,
    MAX_RELATED_ARTICLE_IDS,
    MAX_SOURCE_URLS,
    MAX_TOOL_CALLS_PER_RUN,
    MAX_TOOL_PARAM_STRING_LENGTH,
    AnswerType,
)

EVIDENCE_KINDS: tuple[str, ...] = ("metric", "narrative_insight")
EVIDENCE_ROLES: tuple[str, ...] = ("baseline", "comparison", "delta", "value")


class ChatEvidenceReference(BaseModel):
    kind: str
    tool_call_index: int | None = None
    path: str | None = None
    role: str | None = None
    value: float | None = None
    narrative_insight_id: uuid.UUID | None = None

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in EVIDENCE_KINDS:
            raise ValueError(f"Invalid evidence kind: {value!r}")
        return value

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str | None) -> str | None:
        if value is not None and value not in EVIDENCE_ROLES:
            raise ValueError(f"Invalid evidence role: {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_shape_matches_kind(self) -> "ChatEvidenceReference":
        if self.kind == "metric":
            if self.tool_call_index is None or self.path is None or self.value is None:
                raise ValueError(
                    "kind='metric' requires tool_call_index, path, and value."
                )
            if self.narrative_insight_id is not None:
                raise ValueError("kind='metric' must not set narrative_insight_id.")
        else:  # narrative_insight
            if self.narrative_insight_id is None:
                raise ValueError("kind='narrative_insight' requires narrative_insight_id.")
            if self.tool_call_index is not None or self.path is not None or self.value is not None:
                raise ValueError(
                    "kind='narrative_insight' must not set tool_call_index/path/value."
                )
        return self


# --- Tool parameter schemas --------------------------------------------------


class GetProjectKpisParams(BaseModel):
    pass


class GetBrandPerformanceParams(BaseModel):
    brand: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    top_n: int = Field(default=10, ge=1, le=20)


class GetTopicDistributionParams(BaseModel):
    top_n: int = Field(default=10, ge=1, le=20)


class GetSentimentDistributionParams(BaseModel):
    pass


class GetPublicationRankingsParams(BaseModel):
    top_n: int = Field(default=10, ge=1, le=20)


class GetStoryClustersParams(BaseModel):
    top_n: int = Field(default=10, ge=1, le=20)


class GetProjectArticlesParams(BaseModel):
    brand: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    topic: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    sentiment: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    publication: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    story_key: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    period: str | None = None
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("period")
    @classmethod
    def _validate_period(cls, value: str | None) -> str | None:
        if value is not None and value not in ("baseline", "comparison"):
            raise ValueError(f"Invalid period: {value!r}")
        return value


class GetPeriodComparisonParams(BaseModel):
    top_n: int = Field(default=10, ge=1, le=20)


class GetValidNarrativeInsightsParams(BaseModel):
    narrative_type: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)


# --- Plan / answer submissions -----------------------------------------------


class ToolCallIn(BaseModel):
    tool: str
    parameters: dict = Field(default_factory=dict)


class PlanSubmission(BaseModel):
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    payload_schema_version: str = Field(min_length=1)
    tool_calls: list[ToolCallIn] = Field(min_length=1, max_length=MAX_TOOL_CALLS_PER_RUN)


class AnswerSubmission(BaseModel):
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    payload_schema_version: str = Field(min_length=1)
    answer_text: str = Field(min_length=1, max_length=MAX_ANSWER_LENGTH)
    answer_type: str
    evidence: list[ChatEvidenceReference] = Field(min_length=1, max_length=MAX_EVIDENCE_ENTRIES)
    related_brand: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    related_topic: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    related_publication: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    related_story_key: str | None = Field(default=None, max_length=MAX_TOOL_PARAM_STRING_LENGTH)
    related_article_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=MAX_RELATED_ARTICLE_IDS
    )
    source_urls: list[str] = Field(default_factory=list, max_length=MAX_SOURCE_URLS)

    @field_validator("answer_type")
    @classmethod
    def _validate_answer_type(cls, value: str) -> str:
        if value not in AnswerType.ALL:
            raise ValueError(f"Invalid answer_type: {value!r}")
        return value


# --- Responses ----------------------------------------------------------------


class ChatPlanningPayloadOut(BaseModel):
    run_id: uuid.UUID
    snapshot: dict


class ChatPlanResponse(BaseModel):
    tool_results: list[dict]


class ChatAnswerResponse(BaseModel):
    status: str
    rejection_reason: str | None = None


class ChatRunStatusOut(BaseModel):
    id: uuid.UUID
    status: str
    rejection_reason: str | None
    error_message: str | None


class ChatRunOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    session_id: uuid.UUID
    status: str
    answer_type: str | None
    evidence: list[ChatEvidenceReference] | None
    related_brand: str | None
    related_topic: str | None
    related_publication: str | None
    related_story_key: str | None
    related_article_ids: list[uuid.UUID] | None
    source_urls: list[str] | None
    model: str | None
    prompt_version: str | None
    prompt_contract_version: str
    payload_schema_version: str
    validator_version: str
    tool_call_count: int
    validation_status: str | None
    rejection_reason: str | None
    error_message: str | None
    retry_of_run_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ChatRunAuditOut(ChatRunOut):
    tool_calls: list[dict] | None
    planning_payload_snapshot: dict
    answer_payload_snapshot: dict | None
    source_hash: str | None
    plan_request_hash: str | None
    answer_request_hash: str | None
    answer_text: str | None


class ChatMessageOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    run: ChatRunOut | None = None


class ChatSessionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    project_id: uuid.UUID
    baseline_project_ids: list[uuid.UUID] | None
    comparison_project_ids: list[uuid.UUID] | None
    language: str
    messages: list[ChatMessageOut]


class ChatSessionListItem(BaseModel):
    id: uuid.UUID
    scope: str
    created_at: datetime
    updated_at: datetime
