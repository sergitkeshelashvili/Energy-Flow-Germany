"""FastAPI application entrypoint, lifespan management, and structured error handling."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from api.dependencies import get_api_repository, get_db_manager
from api.routers import (
    analytics_router,
    forecast_router,
    health_router,
    telemetry_router,
)
from api.schemas.responses import ErrorResponse
from common.database import ensure_collections_and_indexes
from common.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Configure structured logging, verify MongoDB readiness with retries, and close on shutdown."""
    configure_logging()
    logger.info("Starting EnergyFlowGermany FastAPI service.")
    if get_api_repository not in application.dependency_overrides:
        try:
            db_manager = get_db_manager()
            if db_manager.wait_for_mongodb():
                ensure_collections_and_indexes(db_manager.get_sync_db(), db_manager.settings)
        except Exception as exc:
            logger.warning("Startup MongoDB readiness check did not complete: %s", exc)
    try:
        yield
    finally:
        if get_db_manager.cache_info().currsize > 0:
            db_manager = get_db_manager()
            db_manager.close_sync()
            db_manager.close_async()
        logger.info("Stopped EnergyFlowGermany FastAPI service.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    application = FastAPI(
        title="EnergyFlowGermany Analytical API",
        description=(
            "Portfolio data engineering API serving validated German day-ahead electricity "
            "spot prices (Energy-Charts), weather forecasts (Open-Meteo), deterministic "
            "6-hour baseline forecasts, and DLQ diagnostics. No authentication is enabled."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(PyMongoError)
    async def pymongo_exception_handler(
        request: Request,
        exc: PyMongoError,
    ) -> JSONResponse:
        logger.error("Database error serving %s %s: %s", request.method, request.url.path, exc)
        payload = ErrorResponse(
            error="database_unavailable",
            detail="Database operation failed while processing request.",
            timestamp=datetime.now(timezone.utc),
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(mode="json"),
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception("Unhandled error serving %s %s", request.method, request.url.path)
        payload = ErrorResponse(
            error="internal_server_error",
            detail="An unexpected internal error occurred.",
            timestamp=datetime.now(timezone.utc),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=payload.model_dump(mode="json"),
        )

    application.include_router(health_router)
    application.include_router(telemetry_router)
    application.include_router(analytics_router)
    application.include_router(forecast_router)

    return application


app = create_app()
