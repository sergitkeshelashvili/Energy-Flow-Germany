"""Unit tests for MongoDB repositories, time-series deduplication, DLQ, and IngestionRunner."""

from datetime import datetime, timezone
from typing import Any

import httpx

from ingestion.clients.energy_charts import EnergyChartsClient
from ingestion.clients.open_meteo import OpenMeteoClient
from ingestion.repository import (
    CheckpointRepository,
    DeadLetterRepository,
    ElectricityPriceRepository,
    WeatherForecastRepository,
)
from ingestion.runner import IngestionRunner
from ingestion.schemas.electricity import (
    EnergyChartsPriceResponse,
    transform_energy_charts_response,
)
from ingestion.schemas.weather import (
    OpenMeteoForecastResponse,
    transform_open_meteo_response,
)
from tests.conftest import FakeMongoCollection


def test_electricity_price_repository_deduplicates_without_timeseries_upsert(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    ts_collection = FakeMongoCollection(is_timeseries=True)
    repo = ElectricityPriceRepository(ts_collection)  # type: ignore[arg-type]

    response = EnergyChartsPriceResponse.model_validate(energy_charts_fixture_dict)
    batch = transform_energy_charts_response(response, bidding_zone="DE-LU")

    # 1. Initial write with an in-batch duplicate appended
    duplicated_input = batch.valid_records + [batch.valid_records[0]]
    first_stats = repo.save_records(duplicated_input)

    assert first_stats.inserted_count == 4
    assert first_stats.skipped_duplicate_count == 1
    assert len(ts_collection.documents) == 4

    # 2. Re-running save_records on the exact same batch inserts 0 documents and never calls upsert
    second_stats = repo.save_records(batch.valid_records)
    assert second_stats.inserted_count == 0
    assert second_stats.skipped_duplicate_count == 4
    assert len(ts_collection.documents) == 4


def test_weather_forecast_repository_deduplicates_and_supports_window_replacement(
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    ts_collection = FakeMongoCollection(is_timeseries=True)
    repo = WeatherForecastRepository(ts_collection)  # type: ignore[arg-type]

    response = OpenMeteoForecastResponse.model_validate(open_meteo_fixture_dict)
    batch = transform_open_meteo_response(response, location_id="DE-DUS")

    # 1. Default append-only deduplication
    first_stats = repo.save_records(batch.valid_records)
    assert first_stats.inserted_count == 4
    assert first_stats.skipped_duplicate_count == 0
    assert len(ts_collection.documents) == 4

    second_stats = repo.save_records(batch.valid_records, replace_overlapping_window=False)
    assert second_stats.inserted_count == 0
    assert second_stats.skipped_duplicate_count == 4
    assert len(ts_collection.documents) == 4

    # 2. Window replacement mode (delete_many + insert_many, still no update_one/upsert)
    refresh_stats = repo.save_records(batch.valid_records, replace_overlapping_window=True)
    assert refresh_stats.deleted_window_count == 4
    assert refresh_stats.inserted_count == 4
    assert len(ts_collection.documents) == 4


def test_ingestion_runner_end_to_end_with_partial_dlq_and_checkpoints(
    energy_charts_fixture_dict: dict[str, Any],
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    ec_payload = dict(energy_charts_fixture_dict)
    ec_payload["price"] = [84.52, None, 75.00, 88.35]  # 1 null price -> DLQ

    ec_client = EnergyChartsClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=ec_payload))
        )
    )
    om_client = OpenMeteoClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=open_meteo_fixture_dict)
            )
        )
    )

    prices_coll = FakeMongoCollection(is_timeseries=True)
    weather_coll = FakeMongoCollection(is_timeseries=True)
    checkpoints_coll = FakeMongoCollection(is_timeseries=False)
    dlq_coll = FakeMongoCollection(is_timeseries=False)

    runner = IngestionRunner(
        energy_charts_client=ec_client,
        open_meteo_client=om_client,
        electricity_repo=ElectricityPriceRepository(prices_coll),  # type: ignore[arg-type]
        weather_repo=WeatherForecastRepository(weather_coll),  # type: ignore[arg-type]
        checkpoint_repo=CheckpointRepository(checkpoints_coll),  # type: ignore[arg-type]
        dlq_repo=DeadLetterRepository(dlq_coll),  # type: ignore[arg-type]
    )

    now = datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc)
    checkpoints = runner.run_all(now=now)

    ec_cp = checkpoints["energy_charts"]
    assert ec_cp.status == "partial_failure"
    assert ec_cp.records_inserted == 3
    assert ec_cp.records_dlq == 1
    assert len(prices_coll.documents) == 3
    assert len(dlq_coll.documents) == 1
    assert dlq_coll.documents[0]["record_index"] == 1

    om_cp = checkpoints["open_meteo"]
    assert om_cp.status == "success"
    assert om_cp.records_inserted == 4
    assert om_cp.records_dlq == 0
    assert len(weather_coll.documents) == 4


def test_ingestion_runner_records_dlq_on_upstream_http_failure_and_preserves_watermark(
    energy_charts_fixture_dict: dict[str, Any],
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    prices_coll = FakeMongoCollection(is_timeseries=True)
    weather_coll = FakeMongoCollection(is_timeseries=True)
    checkpoints_coll = FakeMongoCollection(is_timeseries=False)
    dlq_coll = FakeMongoCollection(is_timeseries=False)

    # First run succeeds for Energy-Charts
    ok_ec_client = EnergyChartsClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=energy_charts_fixture_dict)
            )
        )
    )
    failing_ec_client = EnergyChartsClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(500, text="Server Error"))
        ),
        max_retries=1,
        sleep_fn=lambda s: None,
    )
    om_client = OpenMeteoClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=open_meteo_fixture_dict)
            )
        )
    )

    cp_repo = CheckpointRepository(checkpoints_coll)  # type: ignore[arg-type]
    dlq_repo = DeadLetterRepository(dlq_coll)  # type: ignore[arg-type]
    ec_repo = ElectricityPriceRepository(prices_coll)  # type: ignore[arg-type]
    om_repo = WeatherForecastRepository(weather_coll)  # type: ignore[arg-type]

    runner_ok = IngestionRunner(
        energy_charts_client=ok_ec_client,
        open_meteo_client=om_client,
        electricity_repo=ec_repo,
        weather_repo=om_repo,
        checkpoint_repo=cp_repo,
        dlq_repo=dlq_repo,
    )
    first_cp = runner_ok.ingest_electricity_prices()
    assert first_cp.status == "success"
    watermark = first_cp.last_successful_timestamp
    assert watermark is not None

    # Second run fails with HTTP 500 -> recorded in DLQ, watermark preserved
    runner_fail = IngestionRunner(
        energy_charts_client=failing_ec_client,
        open_meteo_client=om_client,
        electricity_repo=ec_repo,
        weather_repo=om_repo,
        checkpoint_repo=cp_repo,
        dlq_repo=dlq_repo,
    )
    second_cp = runner_fail.ingest_electricity_prices()
    assert second_cp.status == "failed"
    assert second_cp.last_successful_timestamp == watermark
    assert second_cp.records_dlq == 1
    assert len(dlq_coll.documents) == 1
    assert dlq_coll.documents[0]["stage"] == "http_fetch"


def test_ingestion_runner_skips_unnecessary_requests_within_min_fetch_interval(
    energy_charts_fixture_dict: dict[str, Any],
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    ec_calls = 0
    om_calls = 0

    def ec_handler(req: httpx.Request) -> httpx.Response:
        nonlocal ec_calls
        ec_calls += 1
        return httpx.Response(200, json=energy_charts_fixture_dict)

    def om_handler(req: httpx.Request) -> httpx.Response:
        nonlocal om_calls
        om_calls += 1
        return httpx.Response(200, json=open_meteo_fixture_dict)

    runner = IngestionRunner(
        energy_charts_client=EnergyChartsClient(
            http_client=httpx.Client(transport=httpx.MockTransport(ec_handler))
        ),
        open_meteo_client=OpenMeteoClient(
            http_client=httpx.Client(transport=httpx.MockTransport(om_handler))
        ),
        electricity_repo=ElectricityPriceRepository(FakeMongoCollection(is_timeseries=True)),  # type: ignore[arg-type]
        weather_repo=WeatherForecastRepository(FakeMongoCollection(is_timeseries=True)),  # type: ignore[arg-type]
        checkpoint_repo=CheckpointRepository(FakeMongoCollection(is_timeseries=False)),  # type: ignore[arg-type]
        dlq_repo=DeadLetterRepository(FakeMongoCollection(is_timeseries=False)),  # type: ignore[arg-type]
        min_fetch_interval_seconds=3600,
    )

    t0 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    runner.run_all(now=t0)
    assert ec_calls == 1
    assert om_calls == 1

    # 10 minutes later (< 3600s) -> skips upstream HTTP requests completely
    t1 = datetime(2026, 10, 16, 12, 10, 0, tzinfo=timezone.utc)
    runner.run_all(now=t1, force=False)
    assert ec_calls == 1
    assert om_calls == 1

    # Force=True -> bypasses interval guard and queries upstream APIs
    runner.run_all(now=t1, force=True)
    assert ec_calls == 2
    assert om_calls == 2

