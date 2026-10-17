"""MongoDB repository layer for idempotent time-series ingestion, checkpoints, and DLQ.

Idempotency & Deduplication Strategy (No Time-Series Upserts):
--------------------------------------------------------------
MongoDB 7 time-series collections (`electricity_prices`, `weather_forecasts`) store
measurements in compressed internal buckets and do NOT support ordinary document
`update_one(..., upsert=True)` operations.

To guarantee idempotent, duplicate-free writes without time-series upserts:
1. In-Batch Deduplication:
   Incoming records are first deduplicated in memory by their natural composite key:
   - Electricity prices: `(metadata.bidding_zone, timestamp)`
   - Weather forecasts: `(metadata.location_id, timestamp)`
   and sorted chronologically by UTC `timestamp`.
2. Database-Level Deduplication (Read-Filter-Insert / Window Replace):
   - `ElectricityPriceRepository`: Queries the existing UTC timestamps in the target
     `[min_timestamp, max_timestamp]` window for the `metadata.bidding_zone`, filters
     out any timestamp already present in the collection, and calls `insert_many` only
     for net-new timestamps.
   - `WeatherForecastRepository`: By default (`replace_overlapping_window=False`),
     queries existing UTC timestamps in `[min_timestamp, max_timestamp]` for
     `metadata.location_id`, skips already-present timestamps, and inserts only new
     forecast timestamps via `insert_many`. Optionally (`replace_overlapping_window=True`),
     it performs a scoped `delete_many` on `(metadata.location_id, timestamp window)`
     followed by `insert_many` to refresh forecast horizons without using `update`/`upsert`.
3. Regular Collections for Control & Error State:
   - `CheckpointRepository` operates on the regular `ingestion_checkpoints` collection,
     which supports atomic `update_one({"source": ..., "scope": ...}, {"$set": ...}, upsert=True)`.
   - `DeadLetterRepository` appends rejected records/payloads to the regular
     `dead_letter_records` collection via `insert_many` / `insert_one`.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymongo.collection import Collection

from common.logging import get_logger
from ingestion.schemas.common import (
    DeadLetterRecord,
    IngestionCheckpoint,
    SourceName,
    ensure_utc_datetime,
)
from ingestion.schemas.electricity import ElectricityPriceRecord
from ingestion.schemas.weather import WeatherForecastRecord

logger = get_logger(__name__)


@dataclass(frozen=True)
class PersistenceStats:
    """Summary metrics returned after persisting a batch of time-series records."""

    inserted_count: int
    skipped_duplicate_count: int
    deleted_window_count: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    max_timestamp: datetime | None = None


def _normalize_mongo_timestamp(raw_ts: datetime) -> datetime:
    """Ensure a datetime returned by PyMongo (which may be naive UTC in mocks) is UTC-aware."""
    if raw_ts.tzinfo is None:
        return raw_ts.replace(tzinfo=timezone.utc)
    return ensure_utc_datetime(raw_ts)


class ElectricityPriceRepository:
    """Repository for the `electricity_prices` MongoDB 7 time-series collection."""

    def __init__(self, collection: Collection[dict[str, Any]]) -> None:
        self._collection = collection

    def get_existing_timestamps(
        self,
        *,
        bidding_zone: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
    ) -> set[datetime]:
        """Return the set of UTC timestamps already stored for `bidding_zone` in the window."""
        start_utc = ensure_utc_datetime(start_timestamp)
        end_utc = ensure_utc_datetime(end_timestamp)
        cursor = self._collection.find(
            {
                "metadata.bidding_zone": bidding_zone,
                "timestamp": {"$gte": start_utc, "$lte": end_utc},
            },
            {"timestamp": 1, "_id": 0},
        )
        return {_normalize_mongo_timestamp(doc["timestamp"]) for doc in cursor if "timestamp" in doc}

    def save_records(self, records: list[ElectricityPriceRecord]) -> PersistenceStats:
        """Deduplicate and insert electricity spot price records without using upserts."""
        if not records:
            return PersistenceStats(inserted_count=0, skipped_duplicate_count=0)

        # 1. In-memory deduplication keyed by (bidding_zone, UTC timestamp)
        deduped_by_key: dict[tuple[str, datetime], ElectricityPriceRecord] = {}
        in_batch_duplicates = 0
        for record in records:
            key = (record.metadata.bidding_zone, record.timestamp)
            if key in deduped_by_key:
                in_batch_duplicates += 1
            deduped_by_key[key] = record

        records_by_zone: dict[str, list[ElectricityPriceRecord]] = defaultdict(list)
        for (_, _), rec in sorted(deduped_by_key.items(), key=lambda item: item[0][1]):
            records_by_zone[rec.metadata.bidding_zone].append(rec)

        docs_to_insert: list[dict[str, Any]] = []
        db_duplicates = 0
        all_timestamps: list[datetime] = []

        # 2. Database-level deduplication per bidding zone
        for zone, zone_records in records_by_zone.items():
            zone_timestamps = [r.timestamp for r in zone_records]
            all_timestamps.extend(zone_timestamps)
            min_ts = min(zone_timestamps)
            max_ts = max(zone_timestamps)

            existing_ts = self.get_existing_timestamps(
                bidding_zone=zone,
                start_timestamp=min_ts,
                end_timestamp=max_ts,
            )

            for rec in zone_records:
                if rec.timestamp in existing_ts:
                    db_duplicates += 1
                else:
                    docs_to_insert.append(rec.to_mongo_doc())

        if docs_to_insert:
            self._collection.insert_many(docs_to_insert, ordered=False)

        total_skipped = in_batch_duplicates + db_duplicates
        window_start = min(all_timestamps) if all_timestamps else None
        window_end = max(all_timestamps) if all_timestamps else None

        logger.info(
            "ElectricityPriceRepository: inserted=%d, skipped_duplicates=%d (in_batch=%d, existing_db=%d)",
            len(docs_to_insert),
            total_skipped,
            in_batch_duplicates,
            db_duplicates,
        )
        return PersistenceStats(
            inserted_count=len(docs_to_insert),
            skipped_duplicate_count=total_skipped,
            deleted_window_count=0,
            window_start=window_start,
            window_end=window_end,
            max_timestamp=window_end,
        )


class WeatherForecastRepository:
    """Repository for the `weather_forecasts` MongoDB 7 time-series collection."""

    def __init__(self, collection: Collection[dict[str, Any]]) -> None:
        self._collection = collection

    def get_existing_timestamps(
        self,
        *,
        location_id: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
    ) -> set[datetime]:
        """Return the set of UTC timestamps already stored for `location_id` in the window."""
        start_utc = ensure_utc_datetime(start_timestamp)
        end_utc = ensure_utc_datetime(end_timestamp)
        cursor = self._collection.find(
            {
                "metadata.location_id": location_id,
                "timestamp": {"$gte": start_utc, "$lte": end_utc},
            },
            {"timestamp": 1, "_id": 0},
        )
        return {_normalize_mongo_timestamp(doc["timestamp"]) for doc in cursor if "timestamp" in doc}

    def save_records(
        self,
        records: list[WeatherForecastRecord],
        *,
        replace_overlapping_window: bool = False,
    ) -> PersistenceStats:
        """Deduplicate and insert weather forecast records without using time-series upserts.

        When `replace_overlapping_window=False` (default), already-stored `(location_id, timestamp)`
        pairs are skipped and only net-new forecast timestamps are inserted.
        When `replace_overlapping_window=True`, existing forecast records in the batch's
        `[min_timestamp, max_timestamp]` window for `location_id` are deleted via `delete_many`
        before inserting the new forecast batch via `insert_many`.
        """
        if not records:
            return PersistenceStats(inserted_count=0, skipped_duplicate_count=0)

        # 1. In-memory deduplication keyed by (location_id, UTC timestamp)
        deduped_by_key: dict[tuple[str, datetime], WeatherForecastRecord] = {}
        in_batch_duplicates = 0
        for record in records:
            key = (record.metadata.location_id, record.timestamp)
            if key in deduped_by_key:
                in_batch_duplicates += 1
            deduped_by_key[key] = record

        records_by_location: dict[str, list[WeatherForecastRecord]] = defaultdict(list)
        for (_, _), rec in sorted(deduped_by_key.items(), key=lambda item: item[0][1]):
            records_by_location[rec.metadata.location_id].append(rec)

        docs_to_insert: list[dict[str, Any]] = []
        db_duplicates = 0
        deleted_count = 0
        all_timestamps: list[datetime] = []

        # 2. Database-level idempotency per location_id
        for location_id, loc_records in records_by_location.items():
            loc_timestamps = [r.timestamp for r in loc_records]
            all_timestamps.extend(loc_timestamps)
            min_ts = min(loc_timestamps)
            max_ts = max(loc_timestamps)

            if replace_overlapping_window:
                delete_result = self._collection.delete_many(
                    {
                        "metadata.location_id": location_id,
                        "timestamp": {"$gte": min_ts, "$lte": max_ts},
                    }
                )
                deleted_count += delete_result.deleted_count
                docs_to_insert.extend(rec.to_mongo_doc() for rec in loc_records)
            else:
                existing_ts = self.get_existing_timestamps(
                    location_id=location_id,
                    start_timestamp=min_ts,
                    end_timestamp=max_ts,
                )
                for rec in loc_records:
                    if rec.timestamp in existing_ts:
                        db_duplicates += 1
                    else:
                        docs_to_insert.append(rec.to_mongo_doc())

        if docs_to_insert:
            self._collection.insert_many(docs_to_insert, ordered=False)

        total_skipped = in_batch_duplicates + db_duplicates
        window_start = min(all_timestamps) if all_timestamps else None
        window_end = max(all_timestamps) if all_timestamps else None

        logger.info(
            "WeatherForecastRepository: inserted=%d, skipped_duplicates=%d, deleted_window=%d",
            len(docs_to_insert),
            total_skipped,
            deleted_count,
        )
        return PersistenceStats(
            inserted_count=len(docs_to_insert),
            skipped_duplicate_count=total_skipped,
            deleted_window_count=deleted_count,
            window_start=window_start,
            window_end=window_end,
            max_timestamp=window_end,
        )


class CheckpointRepository:
    """Repository for the regular `ingestion_checkpoints` collection (supports atomic upsert)."""

    def __init__(self, collection: Collection[dict[str, Any]]) -> None:
        self._collection = collection

    def get_checkpoint(self, *, source: SourceName, scope: str) -> IngestionCheckpoint | None:
        """Retrieve the current checkpoint for `(source, scope)` if one exists."""
        doc = self._collection.find_one({"source": source, "scope": scope}, {"_id": 0})
        if doc is None:
            return None
        for dt_field in (
            "last_successful_timestamp",
            "window_start_timestamp",
            "window_end_timestamp",
            "last_ingested_at",
        ):
            if doc.get(dt_field) is not None:
                doc[dt_field] = _normalize_mongo_timestamp(doc[dt_field])
        return IngestionCheckpoint.model_validate(doc)

    def save_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        """Atomically upsert the checkpoint document in the regular checkpoints collection."""
        doc = checkpoint.to_mongo_doc()
        self._collection.update_one(
            {"source": checkpoint.source, "scope": checkpoint.scope},
            {"$set": doc},
            upsert=True,
        )
        logger.info(
            "Saved checkpoint for source=%s, scope=%s (status=%s, last_ts=%s)",
            checkpoint.source,
            checkpoint.scope,
            checkpoint.status,
            checkpoint.last_successful_timestamp,
        )


class DeadLetterRepository:
    """Repository for storing invalid payloads and records in the regular `dead_letter_records` collection."""

    def __init__(self, collection: Collection[dict[str, Any]]) -> None:
        self._collection = collection

    def save_dead_letter(self, record: DeadLetterRecord) -> int:
        """Insert a single DeadLetterRecord into the DLQ collection."""
        self._collection.insert_one(record.to_mongo_doc())
        logger.warning(
            "Stored DLQ record for source=%s, scope=%s, stage=%s: %s",
            record.source,
            record.scope,
            record.stage,
            record.reason,
        )
        return 1

    def save_dead_letters(self, records: list[DeadLetterRecord]) -> int:
        """Insert multiple DeadLetterRecord entries into the DLQ collection."""
        if not records:
            return 0
        docs = [rec.to_mongo_doc() for rec in records]
        self._collection.insert_many(docs, ordered=False)
        logger.warning(
            "Stored %d DLQ records for source=%s, scope=%s",
            len(docs),
            records[0].source,
            records[0].scope,
        )
        return len(docs)
