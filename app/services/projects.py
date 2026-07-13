import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.schemas.project import ProjectCreate
from app.services.storage import delete_project_upload_dir

logger = logging.getLogger(__name__)


def list_projects(db: Session) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc())
    return list(db.scalars(stmt).all())


def create_project(db: Session, data: ProjectCreate) -> Project:
    project = Project(
        name=data.name,
        quarter=data.quarter,
        period_start=data.period_start,
        period_end=data.period_end,
        client_name=data.client_name,
        description=data.description,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def delete_project(db: Session, project: Project) -> bool:
    """Deletes the project and every related record via the existing ORM
    cascade relationships (`Project.uploaded_files`/`import_batches`/
    `articles`/`classification_batches`/`classifications`/
    `narrative_generations`/`chat_sessions`, each already `cascade="all,
    delete-orphan"`, cascading further into e.g. `NarrativeInsight`/
    `ChatMessage`/`ChatRun`) -- no new deletion logic, only the
    relationships this model already declares.

    The project's upload directory is removed afterward, database-delete
    first: if directory removal fails, the project's data is already gone
    from the database (the correct, safe outcome — an orphaned directory
    is just wasted disk space, cleanable later; the reverse order would
    risk deleting files while dangling DB rows still reference them).

    Returns True if the filesystem cleanup also succeeded, False if the
    database deletion succeeded but the directory could not be removed —
    logged here, never raised, and never exposing the server path to the
    caller.
    """
    project_id = project.id
    db.delete(project)
    db.commit()

    try:
        delete_project_upload_dir(project_id)
        return True
    except OSError:
        logger.exception("Failed to remove upload directory for deleted project %s", project_id)
        return False
