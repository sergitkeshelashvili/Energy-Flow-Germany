"""Automated tests for the EnergyFlowGermany FastAPI service and APIRepository."""

from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from api.dependencies import get_api_repository
from api.main import create_app
from api.repository import APIRepository
from ingestion.repository import (
    DeadLetterRepository,
    ElectricityPriceRepository,
    WeatherForecastRepository,
)
from ingestion.schemas.common import DeadLetterRecord
from ingestion.schemas.electricity import (
    EnergyChartsPriceResponse,
    transform_energy_charts_response,
)
from ingestion.schemas.weather import (
    OpenMeteoForecastResponse,
    transform_open_meteo_response,
)
from tests.conftest import FakeMongoCollection


def _build_test_client(
    *,
    electricity_coll: FakeMongoCollection | None = None,
    weather_coll: FakeMongoCollection | None = None,
    dlq_coll: FakeMongoCollection | None = None,
    mongo_healthy: bool = True,
) -> tuple[TestClient, FakeMongoCollection, FakeMongoCollection, FakeMongoCollection]:
    e_coll = electricity_coll or FakeMongoCollection(is_timeseries=True)
    w_coll = weather_coll or FakeMongoCollection(is_timeseries=True)
    d_coll = dlq_coll or FakeMongoCollection(is_timeseries=False)

    repo = APIRepository(
        electricity_collection=e_coll,  # type: ignore[arg-type]
        weather_collection=w_coll,  # type: ignore[arg-type]
        dlq_collection=d_coll,  # type: ignore[arg-type]
        ping_fn=lambda: mongo_healthy,
        database_name="test_energy_flow_germany",
    )
    app = create_app()
    app.dependency_overrides[get_api_repository] = lambda: repo
    return TestClient(app), e_coll, w_coll, d_coll


def test_health_endpoint_reports_api_and_mongodb_separately() -> None:
    # 1. Healthy MongoDB
    client_ok, _, _, _ = _build_test_client(mongo_healthy=True)
    resp_ok = client_ok.get("/health")
    assert resp_ok.status_code == 200
    body_ok = resp_ok.json()
    assert body_ok["api_status"] == "up"
    assert body_ok["mongodb_status"] == "connected"
    assert body_ok["overall_status"] == "healthy"
    assert body_ok["database_name"] == "test_energy_flow_germany"

    # 2. Disconnected MongoDB
    client_deg, _, _, _ = _build_test_client(mongo_healthy=False)
    resp_deg = client_deg.get("/health")
    assert resp_deg.status_code == 503
    body_deg = resp_deg.json()
    assert body_deg["api_status"] == "up"
    assert body_deg["mongodb_status"] == "disconnected"
    assert body_deg["overall_status"] == "degraded"


def test_telemetry_latest_does_not_combine_unrelated_timestamps(
    energy_charts_fixture_dict: dict[str, Any],
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    client, e_coll, w_coll, _ = _build_test_client()

    # Shift Energy-Charts timestamps by +2 hours so latest timestamps differ
    shifted_ec = dict(energy_charts_fixture_dict)
    shifted_ec["unix_seconds"] = [ts + 7200 for ts in energy_charts_fixture_dict["unix_seconds"]]

    ec_batch = transform_energy_charts_response(
        EnergyChartsPriceResponse.model_validate(shifted_ec),
        bidding_zone="DE-LU",
    )
    om_batch = transform_open_meteo_response(
        OpenMeteoForecastResponse.model_validate(open_meteo_fixture_dict),
        location_id="DE-DUS",
    )

    ElectricityPriceRepository(e_coll).save_records(ec_batch.valid_records)  # type: ignore[arg-type]
    WeatherForecastRepository(w_coll).save_records(om_batch.valid_records)  # type: ignore[arg-type]

    resp = client.get("/telemetry/latest")
    assert resp.status_code == 200
    data = resp.json()

    assert data["timestamps_aligned"] is False
    assert data["timestamp_delta_seconds"] == 7200.0
    assert data["electricity_price"]["price"] == 88.35
    assert data["electricity_price"]["unit"] == "EUR / MWh"
    assert data["electricity_price"]["source"] == "energy_charts"
    assert data["electricity_price"]["timestamp"] != data["weather_forecast"]["timestamp"]
    assert data["weather_forecast"]["source"] == "open_meteo"
    assert data["weather_forecast"]["is_forecast"] is True
    assert data["weather_forecast"]["units"]["temperature_2m"] == "°C"


def test_analytics_summary_calculates_metrics_and_handles_insufficient_data(
    energy_charts_fixture_dict: dict[str, Any],
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    client, e_coll, w_coll, _ = _build_test_client()

    # 1. Empty collections -> insufficient_data
    empty_resp = client.get("/analytics/summary")
    assert empty_resp.status_code == 200
    empty_data = empty_resp.json()
    assert empty_data["status"] == "insufficient_data"
    assert empty_data["electricity"]["record_count"] == 0
    assert empty_data["weather"]["record_count"] == 0
    assert empty_data["alignment_and_correlation"]["status"] == "insufficient_aligned_data"

    # 2. Populate 4 aligned records (2026-10-15T22:00Z .. 2026-10-16T01:00Z)
    ec_batch = transform_energy_charts_response(
        EnergyChartsPriceResponse.model_validate(energy_charts_fixture_dict),
        bidding_zone="DE-LU",
    )
    om_batch = transform_open_meteo_response(
        OpenMeteoForecastResponse.model_validate(open_meteo_fixture_dict),
        location_id="DE-DUS",
    )
    ElectricityPriceRepository(e_coll).save_records(ec_batch.valid_records)  # type: ignore[arg-type]
    WeatherForecastRepository(w_coll).save_records(om_batch.valid_records)  # type: ignore[arg-type]

    resp = client.get("/analytics/summary")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert data["insufficient_data_reason"] is None
    assert data["electricity"]["record_count"] == 4
    assert data["electricity"]["min_price"] == 75.0
    assert data["electricity"]["max_price"] == 88.35
    assert data["electricity"]["mean_price"] == 81.7425
    assert data["electricity"]["time_range"]["start_timestamp"] is not None
    assert data["weather"]["record_count"] == 4
    assert data["weather"]["temperature_2m"]["min_value"] == 11.2
    assert data["weather"]["temperature_2m"]["max_value"] == 12.4
    assert data["alignment_and_correlation"]["status"] == "ok"
    assert data["alignment_and_correlation"]["aligned_record_count"] == 4
    assert data["alignment_and_correlation"]["price_vs_wind_speed_10m_corr"] is not None


def test_forecast_6h_is_deterministic_and_handles_insufficient_history(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    client, e_coll, _, _ = _build_test_client()

    # 1. Insufficient data when collection is empty
    insufficient = client.get("/forecast/6h")
    assert insufficient.status_code == 200
    ins_body = insufficient.json()
    assert ins_body["status"] == "insufficient_data"
    assert ins_body["forecast_points"] == []
    assert ins_body["is_machine_learning_model"] is False

    # 2. Populate 4 historical electricity price records
    ec_batch = transform_energy_charts_response(
        EnergyChartsPriceResponse.model_validate(energy_charts_fixture_dict),
        bidding_zone="DE-LU",
    )
    ElectricityPriceRepository(e_coll).save_records(ec_batch.valid_records)  # type: ignore[arg-type]

    resp1 = client.get("/forecast/6h")
    resp2 = client.get("/forecast/6h")
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    body1 = resp1.json()
    body2 = resp2.json()

    # Strictly deterministic (no random noise)
    assert body1 == body2
    assert body1["status"] == "ok"
    assert body1["is_machine_learning_model"] is False
    assert body1["history_records_used"] == 4
    assert len(body1["limitations"]) >= 3
    assert len(body1["forecast_points"]) == 6
    assert [pt["step_horizon_hours"] for pt in body1["forecast_points"]] == [1, 2, 3, 4, 5, 6]


def test_admin_dlq_stats_aggregates_categories_and_never_exposes_raw_payload() -> None:
    client, _, _, d_coll = _build_test_client()
    dlq_repo = DeadLetterRepository(d_coll)  # type: ignore[arg-type]

    sensitive_marker = "SENSITIVE_RAW_PAYLOAD_DO_NOT_EXPOSE_12345"
    dlq_repo.save_dead_letters(
        [
            DeadLetterRecord(
                source="energy_charts",
                scope="DE-LU",
                stage="record_validation",
                reason="Missing (null) price value at index 2.",
                raw_payload={"secret": sensitive_marker, "price": None},
                record_index=2,
                failed_at=datetime(2026, 10, 16, 10, 0, 0, tzinfo=timezone.utc),
                resolved=False,
            ),
            DeadLetterRecord(
                source="open_meteo",
                scope="DE-DUS",
                stage="http_fetch",
                reason="HTTP 429 rate limit persisted after 3 attempts for open_meteo.",
                raw_payload={"secret": sensitive_marker, "status_code": 429},
                record_index=None,
                failed_at=datetime(2026, 10, 16, 11, 0, 0, tzinfo=timezone.utc),
                resolved=True,
            ),
        ]
    )

    resp = client.get("/admin/dlq-stats")
    assert resp.status_code == 200
    raw_text = resp.text
    assert sensitive_marker not in raw_text
    assert "raw_payload" not in raw_text

    body = resp.json()
    assert body["total_records"] == 2
    assert body["unresolved_count"] == 1
    assert body["resolved_count"] == 1
    assert body["by_source"] == {"energy_charts": 1, "open_meteo": 1}
    assert body["by_stage"] == {"record_validation": 1, "http_fetch": 1}
    categories = {c["error_category"] for c in body["categories"]}
    assert categories == {"missing_null_value", "http_rate_limit_429"}


def test_pymongo_exception_returns_503_structured_error() -> None:
    class FailingCollection(FakeMongoCollection):
        def find(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise PyMongoError("Simulated connection reset")

    client, _, _, _ = _build_test_client(
        electricity_coll=FailingCollection(is_timeseries=True),
    )
    resp = client.get("/telemetry/latest")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "database_unavailable"


def test_analytics_summary_rejects_inverted_time_range() -> None:
    client, _, _, _ = _build_test_client()
    resp = client.get(
        "/analytics/summary",
        params={
            "start": "2026-10-16T10:00:00Z",
            "end": "2026-10-16T02:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert "start" in resp.json()["detail"]


def test_forecast_version_deduplication_prefers_newest_ingestion(
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    client, _, w_coll, _ = _build_test_client()
    response_v1 = OpenMeteoForecastResponse.model_validate(open_meteo_fixture_dict)
    batch_v1 = transform_open_meteo_response(
        response_v1,
        location_id="DE-DUS",
        ingested_at=datetime(2026, 10, 16, 6, 0, 0, tzinfo=timezone.utc),
        forecast_version="20261016T060000Z",
    )

    updated_payload = dict(open_meteo_fixture_dict)
    updated_hourly = dict(open_meteo_fixture_dict["hourly"])
    updated_hourly["temperature_2m"] = [20.0, 20.0, 20.0, 20.0]
    updated_payload["hourly"] = updated_hourly
    response_v2 = OpenMeteoForecastResponse.model_validate(updated_payload)
    batch_v2 = transform_open_meteo_response(
        response_v2,
        location_id="DE-DUS",
        ingested_at=datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc),
        forecast_version="20261016T120000Z",
    )

    # Insert both versions directly into the collection to simulate multi-version history
    w_coll.insert_many([r.to_mongo_doc() for r in batch_v1.valid_records])
    w_coll.insert_many([r.to_mongo_doc() for r in batch_v2.valid_records])
    assert len(w_coll.documents) == 8

    latest_resp = client.get("/telemetry/latest")
    assert latest_resp.status_code == 200
    latest_weather = latest_resp.json()["weather_forecast"]
    assert latest_weather["forecast_version"] == "20261016T120000Z"
    assert latest_weather["temperature_2m"] == 20.0

    summary_resp = client.get("/analytics/summary")
    assert summary_resp.status_code == 200
    weather_summary = summary_resp.json()["weather"]
    assert weather_summary["record_count"] == 4
    assert weather_summary["temperature_2m"]["mean_value"] == 20.0

