"""MongoDB 7 connection management, collection provisioning, and index setup.

Architectural Notes on MongoDB 7 Time-Series vs. Regular Collections:
1. Time-Series Collections (`electricity_prices`, `weather_forecasts`):
   - Configured with `timeField="timestamp"`, `metaField="metadata"`, and
     `granularity="hours"`.
   - MongoDB time-series collections store measurements in compressed internal
     buckets (`system.buckets.<collection>`) and DO NOT support ordinary
     per-document `update_one(..., upsert=True)` or arbitrary field updates.
   - To achieve idempotent ingestion on time-series collections, the ingestion
     layer must either:
     (a) Filter incoming records against the high-water mark stored in
         `ingestion_checkpoints` before calling `insert_many`, or
     (b) Delete overlapping records within the target `(metadata, timestamp)`
         window using `delete_many` prior to calling `insert_many`.
2. Regular Collections (`ingestion_checkpoints`, `dead_letter_records`):
   - Standard document collections that support full CRUD and atomic upserts
     (e.g., `update_one({"source": ..., "scope": ...}, {"$set": ...}, upsert=True)`).
"""

from collections.abc import Callable
import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import CollectionInvalid, PyMongoError

from common.config import Settings, get_settings
from common.logging import configure_logging, get_logger

logger = get_logger(__name__)

TIME_SERIES_OPTIONS: dict[str, Any] = {
    "timeField": "timestamp",
    "metaField": "metadata",
    "granularity": "hours",
}


class MongoDatabaseManager:
    """Manages synchronous (PyMongo) and asynchronous (Motor) MongoDB 7 connections."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._sync_client: MongoClient[dict[str, Any]] | None = None
        self._async_client: AsyncIOMotorClient[dict[str, Any]] | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    def get_sync_client(self) -> MongoClient[dict[str, Any]]:
        """Return or initialize a timezone-aware synchronous PyMongo client."""
        if self._sync_client is None:
            self._sync_client = MongoClient(
                self._settings.mongo_uri,
                tz_aware=True,
                serverSelectionTimeoutMS=5000,
            )
        return self._sync_client

    def get_sync_db(self) -> Database[dict[str, Any]]:
        """Return the configured target database via PyMongo."""
        return self.get_sync_client()[self._settings.mongo_db_name]

    def get_async_client(self) -> AsyncIOMotorClient[dict[str, Any]]:
        """Return or initialize a timezone-aware asynchronous Motor client."""
        if self._async_client is None:
            self._async_client = AsyncIOMotorClient(
                self._settings.mongo_uri,
                tz_aware=True,
                serverSelectionTimeoutMS=5000,
            )
        return self._async_client

    def get_async_db(self) -> AsyncIOMotorDatabase[dict[str, Any]]:
        """Return the configured target database via Motor."""
        return self.get_async_client()[self._settings.mongo_db_name]

    # --- Synchronous Collection Accessors ---

    def electricity_prices_collection(self) -> Collection[dict[str, Any]]:
        """Return the time-series collection for electricity spot prices."""
        return self.get_sync_db()[self._settings.mongo_collection_electricity_prices]

    def weather_forecasts_collection(self) -> Collection[dict[str, Any]]:
        """Return the time-series collection for weather forecasts."""
        return self.get_sync_db()[self._settings.mongo_collection_weather_forecasts]

    def checkpoints_collection(self) -> Collection[dict[str, Any]]:
        """Return the regular collection for ingestion checkpoints."""
        return self.get_sync_db()[self._settings.mongo_collection_checkpoints]

    def dlq_collection(self) -> Collection[dict[str, Any]]:
        """Return the regular collection for dead-letter queue records."""
        return self.get_sync_db()[self._settings.mongo_collection_dlq]

    def ping_sync(self) -> bool:
        """Execute a synchronous MongoDB ping health check."""
        result = self.get_sync_client().admin.command("ping")
        return bool(result.get("ok") == 1.0)

    async def ping_async(self) -> bool:
        """Execute an asynchronous MongoDB ping health check."""
        result = await self.get_async_client().admin.command("ping")
        return bool(result.get("ok") == 1.0)

    def wait_for_mongodb(
        self,
        *,
        max_retries: int | None = None,
        retry_delay_seconds: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Wait for MongoDB readiness with bounded application-level retries."""
        retries = (
            max_retries
            if max_retries is not None
            else self._settings.mongo_connect_max_retries
        )
        base_delay = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else self._settings.mongo_connect_retry_delay_seconds
        )

        for attempt in range(1, retries + 1):
            try:
                if self.ping_sync():
                    logger.info(
                        "MongoDB readiness verified on attempt %d/%d (host=%s:%d, db=%s).",
                        attempt,
                        retries,
                        self._settings.mongo_host,
                        self._settings.mongo_port,
                        self._settings.mongo_db_name,
                    )
                    return True
            except PyMongoError as exc:
                logger.warning(
                    "MongoDB not ready on attempt %d/%d: %s",
                    attempt,
                    retries,
                    exc,
                )
            if attempt < retries:
                delay = min(15.0, base_delay * (1.5 ** (attempt - 1)))
                sleep_fn(delay)

        logger.error("Exhausted %d attempts waiting for MongoDB readiness.", retries)
        return False

    def close_sync(self) -> None:
        """Close the synchronous PyMongo client if open."""
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    def close_async(self) -> None:
        """Close the asynchronous Motor client if open."""
        if self._async_client is not None:
            self._async_client.close()
            self._async_client = None


def ensure_collections_and_indexes(
    db: Database[dict[str, Any]],
    settings: Settings | None = None,
) -> dict[str, str]:
    """Idempotently create time-series and regular collections along with indexes.

    Returns a mapping of collection names to their provisioned collection type
    ("timeseries" or "regular").
    """
    cfg = settings or get_settings()
    existing_names = set(db.list_collection_names())
    provisioned: dict[str, str] = {}

    # 1. Electricity Price Time-Series Collection
    prices_name = cfg.mongo_collection_electricity_prices
    if prices_name not in existing_names:
        try:
            db.create_collection(prices_name, timeseries=TIME_SERIES_OPTIONS)
            logger.info("Created time-series collection: %s", prices_name)
        except CollectionInvalid:
            logger.debug("Time-series collection %s already exists.", prices_name)
    db[prices_name].create_index(
        [("metadata.bidding_zone", ASCENDING), ("timestamp", ASCENDING)],
        name="idx_bidding_zone_timestamp",
    )
    provisioned[prices_name] = "timeseries"

    # 2. Weather Forecast Time-Series Collection
    weather_name = cfg.mongo_collection_weather_forecasts
    if weather_name not in existing_names:
        try:
            db.create_collection(weather_name, timeseries=TIME_SERIES_OPTIONS)
            logger.info("Created time-series collection: %s", weather_name)
        except CollectionInvalid:
            logger.debug("Time-series collection %s already exists.", weather_name)
    db[weather_name].create_index(
        [("metadata.location_id", ASCENDING), ("timestamp", ASCENDING)],
        name="idx_location_timestamp",
    )
    provisioned[weather_name] = "timeseries"

    # 3. Ingestion Checkpoints Regular Collection (supports atomic upserts)
    checkpoints_name = cfg.mongo_collection_checkpoints
    if checkpoints_name not in existing_names:
        try:
            db.create_collection(checkpoints_name)
            logger.info("Created regular collection: %s", checkpoints_name)
        except CollectionInvalid:
            logger.debug("Regular collection %s already exists.", checkpoints_name)
    db[checkpoints_name].create_index(
        [("source", ASCENDING), ("scope", ASCENDING)],
        unique=True,
        name="uq_checkpoint_source_scope",
    )
    provisioned[checkpoints_name] = "regular"

    # 4. Dead-Letter Queue Regular Collection
    dlq_name = cfg.mongo_collection_dlq
    if dlq_name not in existing_names:
        try:
            db.create_collection(dlq_name)
            logger.info("Created regular collection: %s", dlq_name)
        except CollectionInvalid:
            logger.debug("Regular collection %s already exists.", dlq_name)
    db[dlq_name].create_index(
        [("source", ASCENDING), ("failed_at", DESCENDING)],
        name="idx_dlq_source_failed_at",
    )
    db[dlq_name].create_index(
        [("resolved", ASCENDING), ("failed_at", DESCENDING)],
        name="idx_dlq_resolved_failed_at",
    )
    provisioned[dlq_name] = "regular"

    return provisioned


if __name__ == "__main__":
    configure_logging()
    manager = MongoDatabaseManager()
    try:
        if manager.wait_for_mongodb():
            summary = ensure_collections_and_indexes(
                manager.get_sync_db(),
                manager.settings,
            )
            logger.info("MongoDB 7 infrastructure ready: %s", summary)
    finally:
        manager.close_sync()
