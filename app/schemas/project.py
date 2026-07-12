import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

QUARTER_PATTERN = re.compile(r"^\d{4}-Q[1-4]$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    quarter: str
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
    def quarter_must_match_pattern(cls, value: str) -> str:
        value = value.strip()
        if not QUARTER_PATTERN.match(value):
            raise ValueError("Quarter must be in the format YYYY-Q1..Q4, e.g. 2026-Q2.")
        return value

    @field_validator("description")
    @classmethod
    def blank_description_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    quarter: str
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
