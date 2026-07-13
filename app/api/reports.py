import re
import unicodedata
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.security.auth import require_web_session
from app.services.analytics import AnalyticsFilterError, parse_analytics_filters
from app.services.analytics_filters import extract_prefixed_filter_params
from app.services.comparison import ComparisonServiceError
from app.services.report_data import (
    ReportNotFoundError,
    ReportTooLargeError,
    build_comparison_report_data,
    build_project_report_data,
)
from app.services.report_pptx import build_comparison_pptx, build_project_pptx
from app.services.report_xlsx import build_comparison_xlsx, build_project_xlsx

router = APIRouter(dependencies=[Depends(require_web_session)])

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_FILENAME_COMPONENT_CHARS = 100


def _safe_filename_component(text: str) -> str:
    """ASCII-folds and strips anything outside `[A-Za-z0-9._-]`, collapsing
    runs into a single `-`. This eliminates CR/LF, quotes, semicolons, and
    any other header-injection-capable character *by construction* (an
    allowlist, not a denylist of specific bad characters), and caps length
    so an extreme project name can't produce an oversized
    `Content-Disposition` header.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE_FILENAME_CHARS_RE.sub("-", folded).strip("-")
    if not cleaned:
        cleaned = "report"
    return cleaned[:MAX_FILENAME_COMPONENT_CHARS]


def _report_filename(scope_label: str, extension: str) -> str:
    date_part = datetime.now(timezone.utc).date().isoformat()
    return f"{_safe_filename_component(scope_label)}_report_{date_part}.{extension}"


def _file_response(content: bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/report.pptx")
def project_report_pptx(project_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    try:
        filters = parse_analytics_filters(request.query_params)
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
    try:
        data = build_project_report_data(db, project_id, filters)
    except ReportNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        content = build_project_pptx(data)
    except ReportTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc} Narrow the filters and try again.",
        ) from exc

    filename = _report_filename(f"{data.project_name}_{data.project_quarter}", "pptx")
    return _file_response(content, PPTX_MEDIA_TYPE, filename)


@router.get("/projects/{project_id}/report.xlsx")
def project_report_xlsx(project_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    try:
        filters = parse_analytics_filters(request.query_params)
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
    try:
        data = build_project_report_data(db, project_id, filters)
    except ReportNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        content = build_project_xlsx(data)
    except ReportTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc} Narrow the filters and try again.",
        ) from exc

    filename = _report_filename(f"{data.project_name}_{data.project_quarter}", "xlsx")
    return _file_response(content, XLSX_MEDIA_TYPE, filename)


@router.get("/compare/report.pptx")
def comparison_report_pptx(
    request: Request,
    db: Session = Depends(get_db),
    baseline_project_ids: list[uuid.UUID] = Query(default_factory=list),
    comparison_project_ids: list[uuid.UUID] = Query(default_factory=list),
):
    try:
        filters = parse_analytics_filters(request.query_params)
        baseline_params = extract_prefixed_filter_params(request.query_params, "baseline_filter_")
        comparison_params = extract_prefixed_filter_params(request.query_params, "comparison_filter_")
        baseline_filters = parse_analytics_filters(baseline_params) if baseline_params else None
        comparison_filters = parse_analytics_filters(comparison_params) if comparison_params else None
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
    try:
        data = build_comparison_report_data(
            db, baseline_project_ids, comparison_project_ids, filters,
            baseline_filters=baseline_filters, comparison_filters=comparison_filters,
        )
    except ComparisonServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        content = build_comparison_pptx(data)
    except ReportTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc} Narrow the filters and try again.",
        ) from exc

    filename = _report_filename(f"{data.baseline_label}_vs_{data.comparison_label}", "pptx")
    return _file_response(content, PPTX_MEDIA_TYPE, filename)


@router.get("/compare/report.xlsx")
def comparison_report_xlsx(
    request: Request,
    db: Session = Depends(get_db),
    baseline_project_ids: list[uuid.UUID] = Query(default_factory=list),
    comparison_project_ids: list[uuid.UUID] = Query(default_factory=list),
):
    try:
        filters = parse_analytics_filters(request.query_params)
        baseline_params = extract_prefixed_filter_params(request.query_params, "baseline_filter_")
        comparison_params = extract_prefixed_filter_params(request.query_params, "comparison_filter_")
        baseline_filters = parse_analytics_filters(baseline_params) if baseline_params else None
        comparison_filters = parse_analytics_filters(comparison_params) if comparison_params else None
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
    try:
        data = build_comparison_report_data(
            db, baseline_project_ids, comparison_project_ids, filters,
            baseline_filters=baseline_filters, comparison_filters=comparison_filters,
        )
    except ComparisonServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        content = build_comparison_xlsx(data)
    except ReportTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc} Narrow the filters and try again.",
        ) from exc

    filename = _report_filename(f"{data.baseline_label}_vs_{data.comparison_label}", "xlsx")
    return _file_response(content, XLSX_MEDIA_TYPE, filename)
