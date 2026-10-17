"""API route modules."""

from api.routers.analytics import router as analytics_router
from api.routers.health import router as health_router
from api.routers.prices import router as telemetry_router
from api.routers.weather import router as forecast_router

__all__ = [
    "analytics_router",
    "forecast_router",
    "health_router",
    "telemetry_router",
]
