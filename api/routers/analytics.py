"""Analytical summary (`GET /analytics/summary`) and DLQ diagnostics (`GET /admin/dlq-stats`) endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.dependencies import get_api_repository
from api.repository import APIRepository
from api.schemas.responses import AnalyticsSummaryResponse, DLQStatsResponse

router = APIRouter(tags=["Analytics & Admin"])


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.get(
    "/analytics/summary",
    response_model=AnalyticsSummaryResponse,
)
def get_analytics_summary(
    bidding_zone: str = Query(default="DE-LU", min_length=1, description="Electricity bidding zone"),
    location_id: str = Query(default="DE-DUS", min_length=1, description="Weather forecast location ID"),
    start: datetime | None = Query(default=None, description="Optional inclusive UTC start timestamp"),
    end: datetime | None = Query(default=None, description="Optional inclusive UTC end timestamp"),
    limit: int = Query(
        default=168,
        ge=1,
        le=2000,
        description="Maximum number of stored records per stream to analyze",
    ),
    repo: APIRepository = Depends(get_api_repository),
) -> AnalyticsSummaryResponse:
    """Calculate descriptive metrics and exact-timestamp correlations from stored records."""
    if start is not None and end is not None and _to_utc(start) > _to_utc(end):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query parameter 'start' must be less than or equal to 'end'.",
        )
    return repo.get_analytics_summary(
        bidding_zone=bidding_zone,
        location_id=location_id,
        start=start,
        end=end,
        limit=limit,
    )


@router.get(
    "/admin/dlq-stats",
    response_model=DLQStatsResponse,
)
def get_dlq_stats(
    repo: APIRepository = Depends(get_api_repository),
) -> DLQStatsResponse:
    """Return Dead-Letter Queue record counts and sanitized error categories.

    Raw payloads are excluded from the database projection and never exposed.
    """
    return repo.get_dlq_stats()
