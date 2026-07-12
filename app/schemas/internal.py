import uuid

from pydantic import BaseModel, Field


class ImportFileRequest(BaseModel):
    project_id: uuid.UUID
    quarter: str
    file_path: str
    uploaded_name: str = Field(min_length=1)
    retailer_hint: str | None = None


class ImportFileResponse(BaseModel):
    status: str
    project_id: uuid.UUID
    uploaded_file_id: uuid.UUID
    retailer: str
    rows_received: int
    rows_imported: int
    rows_invalid: int
    rows_duplicate: int
