import uuid

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.project import Project
from app.schemas.project import ProjectCreate
from app.security.auth import require_web_session
from app.services.classification import get_project_summary
from app.services.projects import create_project, list_projects

router = APIRouter(dependencies=[Depends(require_web_session)])
templates = Jinja2Templates(directory="app/templates")


def render(request: Request, template_name: str, context: dict, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        template_name,
        {"authenticated": True, **context},
        status_code=status_code,
    )


def render_project_detail(
    request: Request,
    db: Session,
    project: Project,
    active_tab: str = "overview",
    upload_results: list[dict] | None = None,
    classification_message: dict | None = None,
    status_code: int = 200,
):
    uploaded_files = list(
        sorted(project.uploaded_files, key=lambda f: f.created_at, reverse=True)
    )
    classification_summary = get_project_summary(db, project)
    return render(
        request,
        "project_detail.html",
        {
            "project": project,
            "uploaded_files": uploaded_files,
            "active_tab": active_tab,
            "upload_results": upload_results or [],
            "classification_summary": classification_summary,
            "classification_message": classification_message,
        },
        status_code=status_code,
    )


@router.get("/")
def projects_page(request: Request, db: Session = Depends(get_db)):
    projects = list_projects(db)
    return render(
        request,
        "projects.html",
        {"projects": projects, "form_errors": {}, "form_values": {}, "open_modal": False},
    )


@router.post("/projects")
def create_project_action(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    quarter: str = Form(""),
    description: str = Form(""),
):
    try:
        data = ProjectCreate(name=name, quarter=quarter, description=description)
    except ValidationError as exc:
        form_errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][0])
            form_errors.setdefault(field, error["msg"])

        projects = list_projects(db)
        return render(
            request,
            "projects.html",
            {
                "projects": projects,
                "form_errors": form_errors,
                "form_values": {"name": name, "quarter": quarter, "description": description},
                "open_modal": True,
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    create_project(db, data)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/projects/{project_id}")
def project_detail_page(
    project_id: uuid.UUID, request: Request, db: Session = Depends(get_db), tab: str = "overview"
):
    project = db.get(Project, project_id)
    if project is None:
        projects = list_projects(db)
        return render(
            request,
            "projects.html",
            {
                "projects": projects,
                "form_errors": {},
                "form_values": {},
                "open_modal": False,
                "not_found_id": str(project_id),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    active_tab = tab if tab in ("overview", "files", "classification") else "overview"
    return render_project_detail(request, db, project, active_tab=active_tab)
