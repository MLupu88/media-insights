import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import get_settings


class FileTooLargeError(Exception):
    def __init__(self, max_size_bytes: int):
        self.max_size_bytes = max_size_bytes
        super().__init__(f"File exceeds the maximum allowed size of {max_size_bytes} bytes.")


class InvalidUploadPathError(Exception):
    pass


def resolve_upload_path(file_path: str) -> Path:
    """Resolve a candidate file path and ensure it stays inside the configured
    upload root, rejecting any attempt at path traversal (e.g. via "..").
    """
    settings = get_settings()
    root = Path(settings.upload_root_dir).resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise InvalidUploadPathError(
            "file_path must be inside the configured upload directory."
        )
    return resolved


def project_upload_dir(project_id: uuid.UUID) -> Path:
    settings = get_settings()
    directory = Path(settings.upload_root_dir) / str(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def generate_stored_filename(original_filename: str) -> str:
    suffix = Path(original_filename).suffix.lower() or ".xlsx"
    return f"{uuid.uuid4().hex}{suffix}"


def save_upload_file(project_id: uuid.UUID, upload_file: UploadFile) -> tuple[str, str, int]:
    """Stream an UploadFile to disk under the project's upload directory.

    Returns (stored_filename, stored_path, size_bytes). Raises FileTooLargeError
    and removes any partially written file if the size limit is exceeded.
    """
    settings = get_settings()
    directory = project_upload_dir(project_id)
    stored_filename = generate_stored_filename(upload_file.filename or "upload.xlsx")
    stored_path = directory / stored_filename

    size_bytes = 0
    chunk_size = 1024 * 1024
    with open(stored_path, "wb") as out_file:
        while chunk := upload_file.file.read(chunk_size):
            size_bytes += len(chunk)
            if size_bytes > settings.max_upload_size_bytes:
                out_file.close()
                stored_path.unlink(missing_ok=True)
                raise FileTooLargeError(settings.max_upload_size_bytes)
            out_file.write(chunk)

    return stored_filename, str(stored_path), size_bytes
