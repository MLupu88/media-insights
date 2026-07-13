import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.comparison import PeriodComparisonResponse
from app.security.auth import require_internal_secret
from app.services.analytics import AnalyticsFilterError, parse_analytics_filters
from app.services.analytics_filters import extract_prefixed_filter_params
from app.services.comparison import ComparisonServiceError, get_period_comparison

router = APIRouter(prefix="/api/internal", dependencies=[Depends(require_internal_secret)])


@router.get("/compare", response_model=PeriodComparisonResponse)
def compare_periods(
    request: Request,
    db: Session = Depends(get_db),
    baseline_project_ids: list[uuid.UUID] = Query(default_factory=list),
    comparison_project_ids: list[uuid.UUID] = Query(default_factory=list),
    top_n: int = Query(default=10, ge=1, le=50),
):
    # Phase E — same-project brand-vs-brand comparison: `baseline_filter_*`/
    # `comparison_filter_*`-prefixed query params (e.g.
    # `baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour`)
    # parse into independent per-side filters. Falls back to the shared
    # `filters` (unprefixed params) when absent, so every existing caller
    # is completely unaffected.
    try:
        filters = parse_analytics_filters(request.query_params)
        baseline_params = extract_prefixed_filter_params(request.query_params, "baseline_filter_")
        comparison_params = extract_prefixed_filter_params(request.query_params, "comparison_filter_")
        baseline_filters = parse_analytics_filters(baseline_params) if baseline_params else None
        comparison_filters = parse_analytics_filters(comparison_params) if comparison_params else None
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc

    try:
        payload = get_period_comparison(
            db, baseline_project_ids, comparison_project_ids, filters, top_n=top_n,
            baseline_filters=baseline_filters, comparison_filters=comparison_filters,
        )
    except ComparisonServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return PeriodComparisonResponse(**payload)
