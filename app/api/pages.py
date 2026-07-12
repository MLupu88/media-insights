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
from app.services.analytics import clamp_top_n, get_project_analytics, parse_analytics_filters
from app.services.chat_service import find_comparison_session, get_project_own_chat_session
from app.services.classification import get_project_summary
from app.services.comparison import ComparisonServiceError, get_period_comparison
from app.services.narrative_service import get_project_narrative_generations
from app.services.projects import create_project, list_projects

router = APIRouter(dependencies=[Depends(require_web_session)])
templates = Jinja2Templates(directory="app/templates")


def _format_number(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


templates.env.filters["number"] = _format_number


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
    narrative_message: dict | None = None,
    chat_message: dict | None = None,
    status_code: int = 200,
):
    uploaded_files = list(
        sorted(project.uploaded_files, key=lambda f: f.created_at, reverse=True)
    )
    classification_summary = get_project_summary(db, project)

    analytics_summary = None
    if active_tab == "analytics":
        filters = parse_analytics_filters(request.query_params)
        top_n = clamp_top_n(request.query_params.get("top_n"))
        analytics_summary = get_project_analytics(db, project, filters, top_n=top_n)

    narrative_generations = None
    if active_tab == "insights":
        narrative_generations = get_project_narrative_generations(db, project.id)

    chat_session = None
    if active_tab == "chat":
        chat_session = get_project_own_chat_session(db, project.id)

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
            "analytics_summary": analytics_summary,
            "narrative_generations": narrative_generations,
            "narrative_message": narrative_message,
            "chat_session": chat_session,
            "chat_message": chat_message,
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
    active_tab = (
        tab
        if tab in ("overview", "files", "classification", "analytics", "insights", "chat")
        else "overview"
    )
    return render_project_detail(request, db, project, active_tab=active_tab)


@router.get("/compare")
def compare_page(request: Request, db: Session = Depends(get_db)):
    projects = list_projects(db)

    def _parse_ids(param_name: str) -> list[uuid.UUID]:
        parsed: list[uuid.UUID] = []
        for value in request.query_params.getlist(param_name):
            try:
                parsed.append(uuid.UUID(value))
            except (ValueError, AttributeError):
                continue
        return parsed

    baseline_project_ids = _parse_ids("baseline_project_ids")
    comparison_project_ids = _parse_ids("comparison_project_ids")

    comparison_result = None
    error_message = None
    chat_session = None
    if baseline_project_ids and comparison_project_ids:
        filters = parse_analytics_filters(request.query_params)
        top_n = clamp_top_n(request.query_params.get("top_n"))
        try:
            comparison_result = get_period_comparison(
                db, baseline_project_ids, comparison_project_ids, filters, top_n=top_n
            )
        except ComparisonServiceError as exc:
            error_message = exc.message
        chat_session = find_comparison_session(
            db, baseline_project_ids, comparison_project_ids, filters
        )

    return render(
        request,
        "compare.html",
        {
            "projects": projects,
            "selected_baseline_ids": {str(pid) for pid in baseline_project_ids},
            "selected_comparison_ids": {str(pid) for pid in comparison_project_ids},
            "comparison_result": comparison_result,
            "chat_session": chat_session,
            "error_message": error_message,
        },
    )
