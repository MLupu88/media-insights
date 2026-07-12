import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.comparison import PeriodComparisonResponse
from app.security.auth import require_internal_secret
from app.services.analytics import parse_analytics_filters
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
    filters = parse_analytics_filters(request.query_params)

    try:
        payload = get_period_comparison(
            db, baseline_project_ids, comparison_project_ids, filters, top_n=top_n
        )
    except ComparisonServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return PeriodComparisonResponse(**payload)
