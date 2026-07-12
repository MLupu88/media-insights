from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "msl-insights"}


@router.get("/health/details")
def health_details(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()

    database_connected = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database_connected = False

    upload_directory_writable = True
    try:
        upload_dir = Path(settings.upload_root_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        probe_file = upload_dir / ".write_check"
        probe_file.write_text("ok")
        probe_file.unlink()
    except OSError:
        upload_directory_writable = False

    return {
        "status": "ok" if database_connected and upload_directory_writable else "degraded",
        "database_connected": database_connected,
        "upload_directory_writable": upload_directory_writable,
        "version": settings.app_version,
        "environment": settings.environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
