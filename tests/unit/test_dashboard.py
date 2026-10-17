"""Unit tests for Streamlit dashboard formatting functions, charts, and FastAPI HTTP client."""

from datetime import datetime, timezone

import httpx

from dashboard.api_client import DashboardAPIClient
from dashboard.components.charts import build_electricity_price_chart, build_forecast_6h_chart
from dashboard.formatters import (
    evaluate_data_freshness,
    format_alignment_badge,
    format_correlation,
    format_price_with_unit,
    format_time_range,
    format_utc_timestamp,
    format_weather_metric,
    has_generation_or_renewable_share_data,
)


def test_timestamp_and_time_range_formatters() -> None:
    assert format_utc_timestamp("2026-10-16T01:00:00Z") == "2026-10-16 01:00 UTC"
    assert format_utc_timestamp("2026-10-16T03:00:00+02:00") == "2026-10-16 01:00 UTC"
    assert format_utc_timestamp(None) == "N/A"
    assert format_utc_timestamp("not-a-timestamp") == "N/A"

    tr = {
        "start_timestamp": "2026-10-15T22:00:00Z",
        "end_timestamp": "2026-10-16T01:00:00Z",
    }
    assert format_time_range(tr) == "2026-10-15 22:00 UTC → 2026-10-16 01:00 UTC"
    assert format_time_range(None) == "No time range available"


def test_price_weather_and_correlation_formatters_handle_missing_and_non_finite() -> None:
    assert format_price_with_unit(84.52, "EUR / MWh") == "84.52 EUR / MWh"
    assert format_price_with_unit(-12.3, "EUR / MWh") == "-12.30 EUR / MWh"
    assert format_price_with_unit(None) == "N/A"
    assert format_price_with_unit(float("nan")) == "N/A"
    assert format_price_with_unit(True) == "N/A"

    assert format_weather_metric(18.54, "km/h", decimals=1) == "18.5 km/h"
    assert format_weather_metric(None, "°C") == "N/A"
    assert format_weather_metric(False, "°C") == "N/A"

    assert format_correlation(-0.7421) == "-0.742"
    assert format_correlation(0.512) == "+0.512"
    assert format_correlation(None) == "Insufficient data"
    assert format_correlation(True) == "Insufficient data"


def test_evaluate_data_freshness_classifies_fresh_stale_forecast_and_missing() -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Fresh (30 minutes old)
    fresh = evaluate_data_freshness("2026-10-16T11:30:00Z", now=now)
    assert fresh.level == "fresh"
    assert fresh.label == "FRESH"
    assert "30m ago" in fresh.age_Text

    # 2. Stale (5 hours old, threshold 2h)
    stale = evaluate_data_freshness("2026-10-16T07:00:00Z", now=now, stale_threshold_seconds=7200)
    assert stale.level == "stale"
    assert stale.label == "STALE"

    # 3. Future / Forecast horizon (+6 hours ahead)
    future = evaluate_data_freshness("2026-10-16T18:00:00Z", now=now)
    assert future.level == "forecast_horizon"
    assert "ahead" in future.age_Text

    # 4. Missing
    missing = evaluate_data_freshness(None, now=now)
    assert missing.level == "missing"
    assert missing.label == "NO DATA"


def test_alignment_badge_and_generation_data_guard() -> None:
    assert "ALIGNED" in format_alignment_badge(True, 0.0)
    assert "SEPARATE TIMESTAMPS" in format_alignment_badge(False, 7200.0)
    assert "UNALIGNED" in format_alignment_badge(False, None)

    assert has_generation_or_renewable_share_data(None) is False
    assert has_generation_or_renewable_share_data({"electricity_series": [{"price": 80.0}]}) is False
    assert (
        has_generation_or_renewable_share_data(
            {"generation_series": [{"timestamp": "2026-10-16T00:00:00Z", "wind_mw": 15000}]}
        )
        is True
    )


def test_forecast_chart_distinguishes_observed_and_forecast_traces() -> None:
    observed = [
        {"timestamp": "2026-10-16T00:00:00Z", "price": 75.0},
        {"timestamp": "2026-10-16T01:00:00Z", "price": 88.35},
    ]
    forecast = [
        {"step_horizon_hours": 1, "target_timestamp": "2026-10-16T02:00:00Z", "forecast_price": 86.1},
        {"step_horizon_hours": 2, "target_timestamp": "2026-10-16T03:00:00Z", "forecast_price": 83.4},
    ]
    fig = build_forecast_6h_chart(observed, forecast, unit="EUR / MWh")
    assert len(fig.data) == 2
    assert "Observed" in fig.data[0].name
    assert fig.data[0].line.dash == "solid"
    assert "Baseline Forecast" in fig.data[1].name
    assert fig.data[1].line.dash == "dash"

    price_fig = build_electricity_price_chart(observed, unit="EUR / MWh")
    assert len(price_fig.data) == 1


def test_dashboard_api_client_handles_healthy_degraded_and_offline_states() -> None:
    # 1. Degraded 503 /health still returns parsed JSON payload
    degraded_client = DashboardAPIClient(
        base_url="http://api:8000",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    503,
                    json={
                        "api_status": "up",
                        "mongodb_status": "disconnected",
                        "overall_status": "degraded",
                    },
                )
            )
        ),
    )
    health_res = degraded_client.fetch_health()
    assert health_res.ok is False
    assert health_res.status_code == 503
    assert health_res.data is not None
    assert health_res.data["mongodb_status"] == "disconnected"

    # 2. Complete API downtime (ConnectError) returns ok=False with descriptive error
    def offline_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=req)

    offline_client = DashboardAPIClient(
        base_url="http://api:8000",
        http_client=httpx.Client(transport=httpx.MockTransport(offline_handler)),
    )
    telemetry_res = offline_client.fetch_latest_telemetry()
    assert telemetry_res.ok is False
    assert telemetry_res.data is None
    assert telemetry_res.error is not None
    assert "unreachable" in telemetry_res.error.lower()
