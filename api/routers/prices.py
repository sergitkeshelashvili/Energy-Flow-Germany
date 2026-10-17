"""Telemetry endpoint (`GET /telemetry/latest`) returning latest electricity price and weather forecast."""

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_api_repository
from api.repository import APIRepository
from api.schemas.responses import LatestTelemetryResponse

router = APIRouter(tags=["Telemetry"])


@router.get(
    "/telemetry/latest",
    response_model=LatestTelemetryResponse,
)
def get_latest_telemetry(
    bidding_zone: str = Query(default="DE-LU", min_length=1, description="Electricity bidding zone"),
    location_id: str = Query(default="DE-DUS", min_length=1, description="Weather forecast location ID"),
    repo: APIRepository = Depends(get_api_repository),
) -> LatestTelemetryResponse:
    """Return the latest stored electricity spot price and weather forecast independently.

    Timestamps, units, and sources are preserved per stream and never silently merged.
    """
    return repo.get_latest_telemetry(
        bidding_zone=bidding_zone,
        location_id=location_id,
    )
