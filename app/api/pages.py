import dataclasses
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.classification import (
    LOW_CONFIDENCE_THRESHOLD,
    ClassificationReviewStatus,
    ClassificationTaxonomy,
)
from app.models.project import Project
from app.schemas.project import ProjectCreate
from app.security.auth import require_web_session
from app.services.analytics import (
    AnalyticsFilterError,
    AnalyticsFilters,
    clamp_top_n,
    get_project_analytics,
    parse_analytics_filters,
    serialize_analytics_filters,
)
from app.services.analytics_filters import extract_prefixed_filter_params
from app.services.chat_service import find_comparison_session, get_project_own_chat_session
from app.services.classification import get_project_summary
from app.services.classification_labels import (
    BRAND_ROLE_LABELS,
    COMMUNICATION_CATEGORY_LABELS,
    PRIMARY_TOPIC_LABELS,
    REVIEW_STATUS_LABELS,
    SENTIMENT_LABELS,
    humanize_taxonomy_value,
)
from app.services.classification_results import (
    ClassificationResultsFilterError,
    ClassificationResultsQuery,
    get_classification_review_queue,
    list_classification_results,
    parse_classification_results_query,
)
from app.services.comparison import ComparisonServiceError, get_period_comparison
from app.services.narrative_service import get_project_narrative_generations
from app.services.projects import create_project, delete_project, list_projects
from app.services.retailers import CANONICAL_RETAILERS
from app.services.review import count_needs_review, get_review_groups

router = APIRouter(dependencies=[Depends(require_web_session)])
templates = Jinja2Templates(directory="app/templates")


def _extract_prefixed_params(query_params, prefix: str) -> dict:
    """Plain-dict view of every single-valued query param starting with
    `prefix`, prefix stripped -- e.g. results_primary_topic=X becomes
    {"primary_topic": "X"}. Distinct prefixes (results_/review_) keep the
    Classification tab's table filters and the Review tab's queue paging
    from ever colliding with each other or with unrelated params sharing a
    plain name like `primary_topic` (already used by the Analytics tab).
    """
    result: dict = {}
    for key in query_params.keys():
        if key.startswith(prefix):
            result[key[len(prefix):]] = query_params.get(key)
    return result


templates.env.filters["primary_topic_label"] = lambda v: humanize_taxonomy_value(
    v, PRIMARY_TOPIC_LABELS
)
templates.env.filters["communication_category_label"] = lambda v: humanize_taxonomy_value(
    v, COMMUNICATION_CATEGORY_LABELS
)
templates.env.filters["sentiment_label"] = lambda v: humanize_taxonomy_value(v, SENTIMENT_LABELS)
templates.env.filters["brand_role_label"] = lambda v: humanize_taxonomy_value(v, BRAND_ROLE_LABELS)
templates.env.filters["review_status_label"] = lambda v: humanize_taxonomy_value(
    v, REVIEW_STATUS_LABELS
)

templates.env.globals["low_confidence_threshold"] = LOW_CONFIDENCE_THRESHOLD

templates.env.globals["classification_taxonomy"] = {
    "primary_topics": ClassificationTaxonomy.PRIMARY_TOPICS,
    "communication_categories": ClassificationTaxonomy.COMMUNICATION_CATEGORIES,
    "sentiments": ClassificationTaxonomy.SENTIMENTS,
    "brand_roles": ClassificationTaxonomy.BRAND_ROLES,
    "review_statuses": ClassificationReviewStatus.ALL,
}


def _format_number(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


templates.env.filters["number"] = _format_number


def build_query_string(params: dict) -> str:
    """URL-encodes a dict of query params for template-built links,
    dropping empty/None values. Used so export links carry the current
    filters without manually concatenating strings in Jinja — an export
    must reflect exactly the filtered view it was linked from, never
    silently widen to the whole unfiltered project.
    """
    clean = {k: v for k, v in params.items() if v not in (None, "", [])}
    return urlencode(clean, doseq=True)


templates.env.globals["build_query_string"] = build_query_string


def pagination_url(project_id, query_params: dict, tab: str, page_param: str, page_number: int) -> str:
    """Builds a pagination link's full URL in Python -- Jinja's dict
    literal syntax does not support Python's `**` unpacking (`{**a, 'k':
    v}` is a Jinja TemplateSyntaxError), so this merge cannot be done
    inline in the template.
    """
    merged = {**query_params, "tab": tab, page_param: page_number}
    return f"/projects/{project_id}?{build_query_string(merged)}"


templates.env.globals["pagination_url"] = pagination_url


def _build_analytics_filter_chips(
    request: Request, filters: AnalyticsFilters, source_files: list[dict], project_id
) -> list[dict]:
    """One removable "chip" per active analytics filter dimension —
    "clear all" is just the existing bare `?tab=analytics` link (dropping
    every query param at once); this covers "clear one filter," each chip
    linking to the identical view with only its own dimension's query
    param(s) removed, every other active filter (and `top_n`) untouched.
    Every chip URL is a fully-qualified path (not just a `?query` relative
    reference), so it's unambiguous regardless of caller/context.

    Every clear_url is built purely from the normalized `AnalyticsFilters`
    object via `serialize_analytics_filters(dataclasses.replace(filters, ...))`
    — never from raw `request.query_params`, which could otherwise preserve
    duplicate values, non-canonical ordering, legacy key aliases, or
    unrelated malformed raw values. Only genuinely non-filter page params
    (`tab`, `top_n`) are merged in separately.
    """
    top_n_param = request.query_params.get("top_n")

    def _clear_url(**cleared_fields) -> str:
        cleared = dataclasses.replace(filters, **cleared_fields)
        params: dict = {**serialize_analytics_filters(cleared), "tab": "analytics"}
        if top_n_param:
            params["top_n"] = top_n_param
        return f"/projects/{project_id}?" + build_query_string(params)

    chips: list[dict] = []

    if filters.brands:
        chips.append(
            {
                "label": f"Brand: {', '.join(sorted(filters.brands))}",
                "clear_url": _clear_url(brand=None, brands=()),
            }
        )

    if filters.include_needs_review:
        chips.append({"label": "Needs review", "clear_url": _clear_url(include_needs_review=False)})

    if filters.uploaded_file_ids:
        selected_ids = {str(u) for u in filters.uploaded_file_ids}
        names = [f["original_filename"] for f in source_files if str(f["id"]) in selected_ids]
        label = ", ".join(names) if names else f"{len(filters.uploaded_file_ids)} selected"
        chips.append({"label": f"Source file: {label}", "clear_url": _clear_url(uploaded_file_ids=())})

    if filters.publication:
        chips.append({"label": f"Publication: {filters.publication}", "clear_url": _clear_url(publication=None)})
    if filters.primary_topic:
        chips.append({"label": f"Topic: {filters.primary_topic}", "clear_url": _clear_url(primary_topic=None)})
    if filters.communication_category:
        chips.append(
            {
                "label": f"Category: {filters.communication_category}",
                "clear_url": _clear_url(communication_category=None),
            }
        )
    if filters.sentiment:
        chips.append({"label": f"Sentiment: {filters.sentiment}", "clear_url": _clear_url(sentiment=None)})
    if filters.state != "all":
        chips.append({"label": f"State: {filters.state}", "clear_url": _clear_url(state="all")})

    return chips


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
    review_message: dict | None = None,
    analytics_message: dict | None = None,
    status_code: int = 200,
):
    uploaded_files = list(
        sorted(project.uploaded_files, key=lambda f: f.created_at, reverse=True)
    )
    import_batches = list(project.import_batches)
    classification_summary = get_project_summary(db, project)

    # Computed on every render (not just when the Review tab is active) so
    # the tab's badge count is always accurate regardless of which tab the
    # page is currently showing. Matches `review_backlog_count` in
    # `analytics.py::get_period_analytics` exactly -- the true Review-tab
    # backlog, distinct from analytics' filtered-population needs-review
    # counts (see analytics.py's module docstring).
    review_backlog_count = count_needs_review(db, project.id)

    analytics_summary = None
    analytics_filter_chips = None
    export_params: dict = {}
    if active_tab == "analytics":
        try:
            filters = parse_analytics_filters(request.query_params)
        except AnalyticsFilterError as exc:
            analytics_message = {"type": "error", "text": exc.message}
            status_code = status.HTTP_400_BAD_REQUEST
        else:
            top_n = clamp_top_n(request.query_params.get("top_n"))
            analytics_summary = get_project_analytics(db, project, filters, top_n=top_n)
            analytics_filter_chips = _build_analytics_filter_chips(
                request, filters, analytics_summary["available_filter_options"]["source_files"], project.id
            )
            # Built from the same normalized `filters` object the query
            # itself used, never re-derived from the response dict — so
            # export links always carry exactly the canonical shape
            # `parse_analytics_filters` would read back.
            export_params = serialize_analytics_filters(filters)

    narrative_generations = None
    if active_tab == "insights":
        narrative_generations = get_project_narrative_generations(db, project.id)

    chat_session = None
    if active_tab == "chat":
        chat_session = get_project_own_chat_session(db, project.id)

    classification_results = None
    classification_results_query = None
    results_query_params: dict = {}
    if active_tab == "classification":
        try:
            classification_results_query = parse_classification_results_query(
                _extract_prefixed_params(request.query_params, "results_")
            )
        except ClassificationResultsFilterError as exc:
            classification_message = {"type": "error", "text": exc.message}
            status_code = status.HTTP_400_BAD_REQUEST
            # Fall back to an all-defaults query so the filter form/table
            # section still renders (reset, with the error banner above)
            # instead of crashing on a None query object.
            classification_results_query = ClassificationResultsQuery()
        else:
            classification_results = list_classification_results(
                db, project.id, classification_results_query
            )
            q = classification_results_query
            results_query_params = {
                "results_search": q.search,
                "results_primary_topic": q.primary_topic,
                "results_communication_category": q.communication_category,
                "results_sentiment": q.sentiment,
                "results_brand_role": q.brand_role,
                "results_confidence": q.confidence_bucket,
                "results_review_status": q.review_status,
                "results_date_from": q.date_from.isoformat() if q.date_from else None,
                "results_date_to": q.date_to.isoformat() if q.date_to else None,
                "results_sort": q.sort,
            }

    review_groups = None
    classification_review_page = None
    if active_tab == "review":
        review_groups = get_review_groups(db, project.id)
        try:
            review_page_number = int(request.query_params.get("review_page", 1))
        except (TypeError, ValueError):
            review_page_number = 1
        classification_review_page = get_classification_review_queue(
            db, project.id, page=review_page_number
        )

    return render(
        request,
        "project_detail.html",
        {
            "project": project,
            "uploaded_files": uploaded_files,
            "import_batches": import_batches,
            "active_tab": active_tab,
            "upload_results": upload_results or [],
            "classification_summary": classification_summary,
            "classification_message": classification_message,
            "analytics_summary": analytics_summary,
            "analytics_filter_chips": analytics_filter_chips,
            "analytics_message": analytics_message,
            "export_params": export_params,
            "narrative_generations": narrative_generations,
            "narrative_message": narrative_message,
            "chat_session": chat_session,
            "chat_message": chat_message,
            "review_groups": review_groups,
            "review_message": review_message,
            "review_backlog_count": review_backlog_count,
            "classification_results": classification_results,
            "classification_results_query": classification_results_query,
            "results_query_params": results_query_params,
            "classification_review_page": classification_review_page,
            "canonical_retailers": CANONICAL_RETAILERS,
        },
        status_code=status_code,
    )


@router.get("/")
def projects_page(request: Request, db: Session = Depends(get_db)):
    projects = list_projects(db)
    deleted_project_name = request.query_params.get("deleted")
    cleanup_warning = request.query_params.get("cleanup_warning") == "1"
    return render(
        request,
        "projects.html",
        {
            "projects": projects,
            "form_errors": {},
            "form_values": {},
            "open_modal": False,
            "deleted_project_name": deleted_project_name,
            "cleanup_warning": cleanup_warning,
        },
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
            # A model-level validator (e.g. "quarter or a complete date
            # range is required") has no single field association — this
            # web form only exposes `quarter`, so its error surfaces there.
            field = str(error["loc"][0]) if error["loc"] else "quarter"
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


@router.post("/projects/{project_id}/delete")
def delete_project_action(
    project_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    confirm_name: str = Form(""),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    if confirm_name != project.name:
        # Defense in depth -- the confirmation modal's own JS already
        # requires an exact match before the submit button is enabled,
        # but the server must not trust client-side validation for a
        # destructive action.
        projects = list_projects(db)
        return render(
            request,
            "projects.html",
            {
                "projects": projects,
                "form_errors": {},
                "form_values": {},
                "open_modal": False,
                "delete_error": "Project name did not match. Nothing was deleted.",
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    project_name = project.name
    filesystem_cleanup_ok = delete_project(db, project)

    redirect_params = {"deleted": project_name}
    if not filesystem_cleanup_ok:
        # Never expose the server path -- just signal that manual cleanup
        # may be needed, already logged server-side with the full detail.
        redirect_params["cleanup_warning"] = "1"
    return RedirectResponse(
        url=f"/?{urlencode(redirect_params)}", status_code=status.HTTP_303_SEE_OTHER
    )


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
        if tab in ("overview", "files", "classification", "review", "analytics", "insights", "chat")
        else "overview"
    )

    chat_message = None
    if request.query_params.get("chat_deleted") == "1":
        chat_message = {"type": "success", "text": "The conversation was deleted."}
    elif "chat_deleted_all" in request.query_params:
        count = request.query_params.get("chat_deleted_all")
        chat_message = (
            {"type": "success", "text": "All conversations for this project were deleted."}
            if count != "0"
            else {"type": "info", "text": "There were no conversations to delete."}
        )

    narrative_message = None
    if request.query_params.get("insights_deleted") == "1":
        narrative_message = {"type": "success", "text": "The narrative generation was deleted."}
    elif "insights_deleted_all" in request.query_params:
        count = request.query_params.get("insights_deleted_all")
        narrative_message = (
            {"type": "success", "text": "All insights for this project were deleted."}
            if count != "0"
            else {"type": "info", "text": "There were no insights to delete."}
        )

    return render_project_detail(
        request,
        db,
        project,
        active_tab=active_tab,
        chat_message=chat_message,
        narrative_message=narrative_message,
    )


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
    export_params: dict = {}
    status_code = status.HTTP_200_OK
    if baseline_project_ids and comparison_project_ids:
        # Phase E — same-project brand-vs-brand comparison: `baseline_filter_*`/
        # `comparison_filter_*`-prefixed params parse into independent per-side
        # filters; publication/primary_topic/communication_category/sentiment/
        # state remain a single shared control (the unprefixed `filters`),
        # applied to both sides -- matching the API/export routes exactly.
        try:
            filters = parse_analytics_filters(request.query_params)
            baseline_params = extract_prefixed_filter_params(request.query_params, "baseline_filter_")
            comparison_params = extract_prefixed_filter_params(request.query_params, "comparison_filter_")
            baseline_filters = parse_analytics_filters(baseline_params) if baseline_params else None
            comparison_filters = parse_analytics_filters(comparison_params) if comparison_params else None
        except AnalyticsFilterError as exc:
            error_message = exc.message
            status_code = status.HTTP_400_BAD_REQUEST
            filters = None
        if filters is not None:
            top_n = clamp_top_n(request.query_params.get("top_n"))
            try:
                comparison_result = get_period_comparison(
                    db, baseline_project_ids, comparison_project_ids, filters, top_n=top_n,
                    baseline_filters=baseline_filters, comparison_filters=comparison_filters,
                )
                export_params = {
                    **serialize_analytics_filters(filters),
                    "baseline_project_ids": [str(pid) for pid in baseline_project_ids],
                    "comparison_project_ids": [str(pid) for pid in comparison_project_ids],
                }
                if baseline_filters is not None:
                    export_params.update(
                        {f"baseline_filter_{k}": v for k, v in serialize_analytics_filters(baseline_filters).items()}
                    )
                if comparison_filters is not None:
                    export_params.update(
                        {
                            f"comparison_filter_{k}": v
                            for k, v in serialize_analytics_filters(comparison_filters).items()
                        }
                    )
            except ComparisonServiceError as exc:
                error_message = exc.message
            chat_session = find_comparison_session(
                db, baseline_project_ids, comparison_project_ids,
                filters=baseline_filters or filters, comparison_filters=comparison_filters,
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
            "export_params": export_params,
        },
        status_code=status_code,
    )
