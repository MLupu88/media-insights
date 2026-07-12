from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.schemas.project import ProjectCreate


def list_projects(db: Session) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc())
    return list(db.scalars(stmt).all())


def create_project(db: Session, data: ProjectCreate) -> Project:
    project = Project(
        name=data.name,
        quarter=data.quarter,
        description=data.description,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project
