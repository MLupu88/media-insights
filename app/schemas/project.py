import re
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QUARTER_PATTERN = re.compile(r"^\d{4}-Q[1-4]$")


class ProjectCreate(BaseModel):
    """A project must supply a valid `quarter` OR a complete
    `period_start`/`period_end` range (both together are also allowed —
    a friendly label alongside a precise range, not a conflict). Enforced
    here as the user-friendly first layer; the DB's
    `ck_projects_period_integrity` CHECK constraint is the backstop for any
    path that bypasses this schema.
    """

    name: str = Field(min_length=1, max_length=255)
    quarter: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    client_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank.")
        return value

    @field_validator("quarter")
    @classmethod
    def quarter_must_match_pattern_if_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not QUARTER_PATTERN.match(value):
            raise ValueError("Quarter must be in the format YYYY-Q1..Q4, e.g. 2026-Q2.")
        return value

    @field_validator("client_name")
    @classmethod
    def blank_client_name_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("description")
    @classmethod
    def blank_description_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def validate_period(self) -> "ProjectCreate":
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError(
                "period_start and period_end must both be provided or both be omitted."
            )
        if self.period_start is not None and self.period_end is not None:
            if self.period_end < self.period_start:
                raise ValueError("period_end must not be before period_start.")
        if not self.quarter and not (self.period_start and self.period_end):
            raise ValueError(
                "Provide either a quarter (e.g. 2026-Q2) or a complete "
                "period_start/period_end date range."
            )
        return self


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    quarter: str | None
    period_start: date | None
    period_end: date | None
    client_name: str | None
    description: str | None
    status: str
    total_files: int
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    classified_rows: int
    analysis_status: str
    created_at: datetime
    updated_at: datetime
