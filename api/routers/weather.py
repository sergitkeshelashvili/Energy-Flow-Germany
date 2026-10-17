"""Deterministic 6-hour baseline forecast endpoint (`GET /forecast/6h`)."""

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_api_repository
from api.repository import APIRepository
from api.schemas.responses import SixHourForecastResponse

router = APIRouter(tags=["Forecast"])


@router.get(
    "/forecast/6h",
    response_model=SixHourForecastResponse,
)
def get_6h_forecast(
    bidding_zone: str = Query(default="DE-LU", min_length=1, description="Electricity bidding zone"),
    history_window: int = Query(
        default=24,
        ge=3,
        le=168,
        description="Number of trailing stored hourly electricity price records used by the baseline",
    ),
    repo: APIRepository = Depends(get_api_repository),
) -> SixHourForecastResponse:
    """Compute a deterministic 6-hour baseline electricity price forecast.

    Uses damped trailing hourly drift blended with historical mean reversion.
    Does not add random noise and does not claim machine learning capability.
    """
    return repo.get_6h_forecast(
        bidding_zone=bidding_zone,
        history_window=history_window,
    )
