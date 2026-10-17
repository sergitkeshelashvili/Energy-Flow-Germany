"""FastAPI dependency injection providers for MongoDB collections, repository, and settings."""

from functools import lru_cache

from fastapi import Depends

from api.repository import APIRepository
from common.config import Settings, get_settings
from common.database import MongoDatabaseManager


@lru_cache(maxsize=1)
def get_db_manager() -> MongoDatabaseManager:
    """Return a singleton MongoDatabaseManager for the FastAPI application."""
    return MongoDatabaseManager(get_settings())


def get_api_repository(
    db_manager: MongoDatabaseManager = Depends(get_db_manager),
    settings: Settings = Depends(get_settings),
) -> APIRepository:
    """Provide an `APIRepository` instance backed by the configured MongoDB collections."""
    return APIRepository(
        electricity_collection=db_manager.electricity_prices_collection(),
        weather_collection=db_manager.weather_forecasts_collection(),
        dlq_collection=db_manager.dlq_collection(),
        ping_fn=db_manager.ping_sync,
        database_name=settings.mongo_db_name,
    )
