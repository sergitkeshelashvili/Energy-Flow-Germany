"""Entrypoint and orchestrator for the electricity price and weather forecast ingestion pipeline.

Supports:
- One-shot execution (`--once` or `INGESTION_RUN_ONCE=true`)
- Continuous background worker loop with configurable polling interval (`INGESTION_POLL_INTERVAL_SECONDS`)
- Checkpoint-based rate-limit protection (`INGESTION_MIN_FETCH_INTERVAL_SECONDS`) to avoid
  unnecessary upstream API requests when recent data has already been ingested
- Application-level retries for MongoDB readiness
- Graceful shutdown on `SIGINT` / `SIGTERM`
"""

import argparse
from datetime import datetime, timezone
import signal
import threading
from types import FrameType
from typing import Any

from common.config import Settings, WeatherLocation, get_settings
from common.database import MongoDatabaseManager, ensure_collections_and_indexes
from common.logging import configure_logging, get_logger
from ingestion.clients.base import IngestionClientError
from ingestion.clients.energy_charts import EnergyChartsClient
from ingestion.clients.open_meteo import OpenMeteoClient
from ingestion.repository import (
    CheckpointRepository,
    DeadLetterRepository,
    ElectricityPriceRepository,
    WeatherForecastRepository,
)
from ingestion.schemas.common import CheckpointStatus, IngestionCheckpoint, ensure_utc_datetime

logger = get_logger(__name__)


def _max_optional_datetime(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _should_skip_recent_fetch(
    checkpoint: IngestionCheckpoint | None,
    *,
    now: datetime,
    min_fetch_interval_seconds: int,
    force: bool,
) -> bool:
    """Return True if a recent successful ingestion occurred within `min_fetch_interval_seconds`."""
    if force or min_fetch_interval_seconds <= 0 or checkpoint is None:
        return False
    if checkpoint.status == "failed" or checkpoint.last_successful_timestamp is None:
        return False
    elapsed = (now - checkpoint.last_ingested_at).total_seconds()
    return 0.0 <= elapsed < float(min_fetch_interval_seconds)


class IngestionRunner:
    """Coordinates data extraction, Pydantic v2 validation, DLQ routing, and MongoDB persistence."""

    def __init__(
        self,
        *,
        energy_charts_client: EnergyChartsClient,
        open_meteo_client: OpenMeteoClient,
        electricity_repo: ElectricityPriceRepository,
        weather_repo: WeatherForecastRepository,
        checkpoint_repo: CheckpointRepository,
        dlq_repo: DeadLetterRepository,
        min_fetch_interval_seconds: int = 0,
        energy_poll_interval_seconds: int | None = None,
        weather_poll_interval_seconds: int | None = None,
        weather_locations: list[WeatherLocation] | None = None,
    ) -> None:
        self.energy_charts_client = energy_charts_client
        self.open_meteo_client = open_meteo_client
        self.electricity_repo = electricity_repo
        self.weather_repo = weather_repo
        self.checkpoint_repo = checkpoint_repo
        self.dlq_repo = dlq_repo
        self.weather_locations = weather_locations
        self.min_fetch_interval_seconds = min_fetch_interval_seconds
        self.energy_poll_interval_seconds = (
            energy_poll_interval_seconds
            if energy_poll_interval_seconds is not None
            else min_fetch_interval_seconds
        )
        self.weather_poll_interval_seconds = (
            weather_poll_interval_seconds
            if weather_poll_interval_seconds is not None
            else min_fetch_interval_seconds
        )

    def ingest_electricity_prices(
        self,
        *,
        bidding_zone: str | None = None,
        start: str | None = None,
        end: str | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> IngestionCheckpoint:
        """Execute end-to-end ingestion for Energy-Charts electricity spot prices."""
        zone = bidding_zone or self.energy_charts_client.bidding_zone
        ingestion_ts = ensure_utc_datetime(now or datetime.now(timezone.utc))
        previous_cp = self.checkpoint_repo.get_checkpoint(source="energy_charts", scope=zone)

        if _should_skip_recent_fetch(
            previous_cp,
            now=ingestion_ts,
            min_fetch_interval_seconds=self.energy_poll_interval_seconds,
            force=force,
        ):
            assert previous_cp is not None
            elapsed = (ingestion_ts - previous_cp.last_ingested_at).total_seconds()
            logger.info(
                "Skipping Energy-Charts API request for %s: last ingestion was %.0fs ago "
                "(< energy_poll_interval=%ds).",
                zone,
                elapsed,
                self.energy_poll_interval_seconds,
            )
            return previous_cp

        previous_last_ts = previous_cp.last_successful_timestamp if previous_cp else None

        try:
            batch = self.energy_charts_client.fetch_price_records(
                bidding_zone=zone,
                start=start,
                end=end,
                ingested_at=ingestion_ts,
            )
        except IngestionClientError as exc:
            logger.error("Energy-Charts ingestion failed for %s: %s", zone, exc)
            dlq_count = self.dlq_repo.save_dead_letter(exc.to_dead_letter(failed_at=ingestion_ts))
            failed_cp = IngestionCheckpoint(
                source="energy_charts",
                scope=zone,
                last_successful_timestamp=previous_last_ts,
                last_ingested_at=ingestion_ts,
                records_inserted=0,
                records_skipped_duplicate=0,
                records_dlq=dlq_count,
                status="failed",
            )
            self.checkpoint_repo.save_checkpoint(failed_cp)
            return failed_cp

        dlq_count = self.dlq_repo.save_dead_letters(batch.dead_letters)
        stats = self.electricity_repo.save_records(batch.valid_records)

        if not batch.valid_records and dlq_count > 0:
            status: CheckpointStatus = "failed"
        elif dlq_count > 0:
            status = "partial_failure"
        else:
            status = "success"

        checkpoint = IngestionCheckpoint(
            source="energy_charts",
            scope=zone,
            last_successful_timestamp=_max_optional_datetime(previous_last_ts, stats.max_timestamp),
            window_start_timestamp=stats.window_start,
            window_end_timestamp=stats.window_end,
            last_ingested_at=ingestion_ts,
            records_inserted=stats.inserted_count,
            records_skipped_duplicate=stats.skipped_duplicate_count,
            records_dlq=dlq_count,
            status=status,
        )
        self.checkpoint_repo.save_checkpoint(checkpoint)
        return checkpoint

    def ingest_weather_forecasts(
        self,
        *,
        location_id: str | None = None,
        location_name: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        timezone_name: str | None = None,
        replace_overlapping_window: bool = False,
        now: datetime | None = None,
        force: bool = False,
    ) -> IngestionCheckpoint:
        """Execute end-to-end ingestion for Open-Meteo hourly weather forecasts."""
        loc_id = location_id or self.open_meteo_client.location_id
        loc_name = location_name or self.open_meteo_client.location_name
        ingestion_ts = ensure_utc_datetime(now or datetime.now(timezone.utc))
        previous_cp = self.checkpoint_repo.get_checkpoint(source="open_meteo", scope=loc_id)

        if _should_skip_recent_fetch(
            previous_cp,
            now=ingestion_ts,
            min_fetch_interval_seconds=self.weather_poll_interval_seconds,
            force=force,
        ):
            assert previous_cp is not None
            elapsed = (ingestion_ts - previous_cp.last_ingested_at).total_seconds()
            logger.info(
                "Skipping Open-Meteo API request for %s: last ingestion was %.0fs ago "
                "(< weather_poll_interval=%ds).",
                loc_id,
                elapsed,
                self.weather_poll_interval_seconds,
            )
            return previous_cp

        previous_last_ts = previous_cp.last_successful_timestamp if previous_cp else None

        try:
            batch = self.open_meteo_client.fetch_forecast_records(
                latitude=latitude,
                longitude=longitude,
                timezone=timezone_name,
                location_id=loc_id,
                location_name=loc_name,
                ingested_at=ingestion_ts,
            )
        except IngestionClientError as exc:
            logger.error("Open-Meteo ingestion failed for %s: %s", loc_id, exc)
            dlq_count = self.dlq_repo.save_dead_letter(exc.to_dead_letter(failed_at=ingestion_ts))
            failed_cp = IngestionCheckpoint(
                source="open_meteo",
                scope=loc_id,
                last_successful_timestamp=previous_last_ts,
                last_ingested_at=ingestion_ts,
                records_inserted=0,
                records_skipped_duplicate=0,
                records_dlq=dlq_count,
                status="failed",
            )
            self.checkpoint_repo.save_checkpoint(failed_cp)
            return failed_cp

        dlq_count = self.dlq_repo.save_dead_letters(batch.dead_letters)
        stats = self.weather_repo.save_records(
            batch.valid_records,
            replace_overlapping_window=replace_overlapping_window,
        )

        if not batch.valid_records and dlq_count > 0:
            status: CheckpointStatus = "failed"
        elif dlq_count > 0:
            status = "partial_failure"
        else:
            status = "success"

        checkpoint = IngestionCheckpoint(
            source="open_meteo",
            scope=loc_id,
            last_successful_timestamp=_max_optional_datetime(previous_last_ts, stats.max_timestamp),
            window_start_timestamp=stats.window_start,
            window_end_timestamp=stats.window_end,
            last_ingested_at=ingestion_ts,
            records_inserted=stats.inserted_count,
            records_skipped_duplicate=stats.skipped_duplicate_count,
            records_dlq=dlq_count,
            status=status,
        )
        self.checkpoint_repo.save_checkpoint(checkpoint)
        return checkpoint

    def run_all(
        self,
        *,
        replace_weather_window: bool = False,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Ingest electricity once and weather forecasts for every configured location."""
        electricity_cp = self.ingest_electricity_prices(now=now, force=force)
        locations = self.weather_locations or [
            WeatherLocation(
                location_id=self.open_meteo_client.location_id,
                location_name=self.open_meteo_client.location_name,
                latitude=self.open_meteo_client.latitude,
                longitude=self.open_meteo_client.longitude,
                timezone=self.open_meteo_client.timezone,
            )
        ]
        weather_by_location = {
            location.location_id: self.ingest_weather_forecasts(
                location_id=location.location_id,
                location_name=location.location_name,
                latitude=location.latitude,
                longitude=location.longitude,
                timezone_name=location.timezone,
                replace_overlapping_window=replace_weather_window,
                now=now,
                force=force,
            )
            for location in locations
        }
        return {
            "energy_charts": electricity_cp,
            # Keep the original single-location key for callers that use it.
            "open_meteo": next(iter(weather_by_location.values())),
            "weather_by_location": weather_by_location,
        }


def run_pipeline_from_settings(
    settings: Settings | None = None,
    *,
    run_once: bool | None = None,
    force: bool = False,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Bootstrap clients and MongoDB repositories from Settings and execute ingestion."""
    cfg = settings or get_settings()
    configure_logging(cfg.log_level)

    single_shot = cfg.ingestion_run_once if run_once is None else run_once
    shutdown_event = stop_event or threading.Event()

    def _signal_handler(signum: int, _frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received signal %s; initiating graceful ingestion worker shutdown.", sig_name)
        shutdown_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    db_manager = MongoDatabaseManager(cfg)
    try:
        if not db_manager.wait_for_mongodb():
            raise RuntimeError("MongoDB failed to become ready within configured retry bounds.")

        db = db_manager.get_sync_db()
        ensure_collections_and_indexes(db, cfg)

        with (
            EnergyChartsClient(
                base_url=cfg.energy_charts_base_url,
                bidding_zone=cfg.energy_charts_bidding_zone,
                timeout_seconds=cfg.http_timeout_seconds,
                max_retries=cfg.http_max_retries,
                backoff_base_seconds=cfg.http_backoff_base_seconds,
                backoff_max_seconds=cfg.http_backoff_max_seconds,
            ) as ec_client,
            OpenMeteoClient(
                base_url=cfg.open_meteo_base_url,
                location_id=cfg.weather_locations[0].location_id,
                location_name=cfg.weather_locations[0].location_name,
                latitude=cfg.weather_locations[0].latitude,
                longitude=cfg.weather_locations[0].longitude,
                timezone=cfg.weather_locations[0].timezone,
                timeout_seconds=cfg.http_timeout_seconds,
                max_retries=cfg.http_max_retries,
                backoff_base_seconds=cfg.http_backoff_base_seconds,
                backoff_max_seconds=cfg.http_backoff_max_seconds,
            ) as om_client,
        ):
            runner = IngestionRunner(
                energy_charts_client=ec_client,
                open_meteo_client=om_client,
                electricity_repo=ElectricityPriceRepository(
                    db_manager.electricity_prices_collection()
                ),
                weather_repo=WeatherForecastRepository(
                    db_manager.weather_forecasts_collection()
                ),
                checkpoint_repo=CheckpointRepository(db_manager.checkpoints_collection()),
                dlq_repo=DeadLetterRepository(db_manager.dlq_collection()),
                energy_poll_interval_seconds=cfg.energy_poll_interval_seconds,
                weather_poll_interval_seconds=cfg.weather_poll_interval_seconds,
                weather_locations=cfg.weather_locations,
            )

            loop_sleep_seconds = min(
                cfg.energy_poll_interval_seconds,
                cfg.weather_poll_interval_seconds,
            )
            latest_results: dict[str, Any] = {}
            while not shutdown_event.is_set():
                latest_results = runner.run_all(force=force)
                weather_status = ", ".join(
                    f"{location_id}(status={checkpoint.status}, "
                    f"inserted={checkpoint.records_inserted}, "
                    f"skipped={checkpoint.records_skipped_duplicate}, "
                    f"dlq={checkpoint.records_dlq})"
                    for location_id, checkpoint in latest_results["weather_by_location"].items()
                )
                logger.info(
                    "Completed ingestion cycle: energy_charts(status=%s, inserted=%d, skipped=%d, dlq=%d), "
                    "weather=[%s]",
                    latest_results["energy_charts"].status,
                    latest_results["energy_charts"].records_inserted,
                    latest_results["energy_charts"].records_skipped_duplicate,
                    latest_results["energy_charts"].records_dlq,
                    weather_status,
                )
                if single_shot:
                    break
                logger.info(
                    "Ingestion worker sleeping for %ds until next poll cycle "
                    "(energy_poll_interval=%ds, weather_poll_interval=%ds).",
                    loop_sleep_seconds,
                    cfg.energy_poll_interval_seconds,
                    cfg.weather_poll_interval_seconds,
                )
                shutdown_event.wait(timeout=float(loop_sleep_seconds))

            logger.info("Ingestion worker exited cleanly.")
            return latest_results
    finally:
        db_manager.close_sync()


def main() -> None:
    parser = argparse.ArgumentParser(description="EnergyFlowGermany Ingestion Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single ingestion cycle and exit immediately.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass min_fetch_interval_seconds and fetch upstream APIs immediately.",
    )
    args = parser.parse_args()
    run_once_arg: bool | None = True if args.once else None
    run_pipeline_from_settings(run_once=run_once_arg, force=args.force)


if __name__ == "__main__":
    main()
