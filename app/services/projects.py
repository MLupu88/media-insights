import logging

from sqlalchemy import delete, select
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
    """Delete a project with one database-level cascade operation.

    Every project-owned foreign key is declared with ``ON DELETE CASCADE``.
    Issuing a SQL ``DELETE`` directly against ``projects`` lets PostgreSQL
    remove articles, classifications, batches, narratives and chat records
    without SQLAlchemy loading tens of thousands of child objects into memory.
    This is the critical difference between a quick delete and the previous
    ORM cascade, which could exceed the reverse proxy timeout for large
    projects.

    The upload directory is removed only after the database transaction has
    committed. A filesystem failure is logged and reported as a cleanup warning,
    while the database deletion remains successful.
    """
    project_id = project.id

    # The route loaded this row only to validate the confirmation name. Detach
    # it before the bulk statement so the session cannot attempt ORM cascades
    # or later expire a row that no longer exists.
    if project in db:
        db.expunge(project)

    db.execute(
        delete(Project)
        .where(Project.id == project_id)
        .execution_options(synchronize_session=False)
    )
    db.commit()

    try:
        delete_project_upload_dir(project_id)
        return True
    except OSError:
        logger.exception("Failed to remove upload directory for deleted project %s", project_id)
        return False
