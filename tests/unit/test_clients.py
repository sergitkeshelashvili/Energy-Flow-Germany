"""Unit tests for EnergyChartsClient and OpenMeteoClient using mocked HTTP transport."""

from datetime import datetime, timezone
import json
from typing import Any

import httpx
import pytest

from ingestion.clients.base import (
    APIHTTPError,
    APIMalformedJSONError,
    APIRateLimitError,
    APISchemaValidationError,
    APITimeoutError,
    parse_retry_after,
)
from ingestion.clients.energy_charts import EnergyChartsClient
from ingestion.clients.open_meteo import OpenMeteoClient


def test_energy_charts_client_requests_de_lu_and_parses_fixture(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json=energy_charts_fixture_dict)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = EnergyChartsClient(http_client=mock_client)
    batch = client.fetch_price_records()

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url.path == "/price"
    assert req.url.params["bzn"] == "DE-LU"
    assert len(batch.valid_records) == 4
    assert len(batch.dead_letters) == 0


def test_open_meteo_client_requests_dusseldorf_and_explicit_europe_berlin_timezone(
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json=open_meteo_fixture_dict)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenMeteoClient(http_client=mock_client)
    batch = client.fetch_forecast_records()

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url.path == "/v1/forecast"
    assert float(req.url.params["latitude"]) == pytest.approx(51.2277)
    assert float(req.url.params["longitude"]) == pytest.approx(6.7735)
    assert req.url.params["timezone"] == "Europe/Berlin"
    assert req.url.params["hourly"] == "temperature_2m,wind_speed_10m,shortwave_radiation"
    assert len(batch.valid_records) == 4
    assert all(rec.is_forecast for rec in batch.valid_records)


def test_client_handles_http_429_with_retry_after_and_recovers(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    attempts = 0
    slept_delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "4"})
        return httpx.Response(200, json=energy_charts_fixture_dict)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = EnergyChartsClient(
        http_client=mock_client,
        max_retries=2,
        backoff_base_seconds=0.5,
        backoff_max_seconds=10.0,
        sleep_fn=slept_delays.append,
        jitter_fn=lambda a, b: 0.0,
    )

    batch = client.fetch_price_records()
    assert attempts == 2
    assert len(slept_delays) == 1
    assert slept_delays[0] == pytest.approx(4.0)
    assert len(batch.valid_records) == 4


def test_client_stops_retrying_on_persistent_http_429_and_excessive_retry_after() -> None:
    attempts = 0
    slept_delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = EnergyChartsClient(
        http_client=mock_client,
        max_retries=2,
        backoff_base_seconds=0.5,
        backoff_max_seconds=5.0,
        sleep_fn=slept_delays.append,
        jitter_fn=lambda a, b: 0.1,
    )

    with pytest.raises(APIRateLimitError, match="HTTP 429 rate limit persisted after 3 attempts"):
        client.fetch_price_records()

    assert attempts == 3
    assert len(slept_delays) == 2

    # Also verify excessive Retry-After fails immediately without sleeping
    slept_excessive: list[float] = []
    excessive_client = EnergyChartsClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(429, headers={"Retry-After": "120"})
            )
        ),
        max_retries=3,
        backoff_max_seconds=10.0,
        sleep_fn=slept_excessive.append,
    )
    with pytest.raises(APIRateLimitError, match="exceeds max backoff cap"):
        excessive_client.fetch_price_records()
    assert len(slept_excessive) == 0


def test_client_handles_timeouts_malformed_json_and_unexpected_schema() -> None:
    slept: list[float] = []

    # 1. Bounded timeout retries
    timeout_attempts = 0

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal timeout_attempts
        timeout_attempts += 1
        raise httpx.ReadTimeout("Read timed out", request=request)

    timeout_client = OpenMeteoClient(
        http_client=httpx.Client(transport=httpx.MockTransport(timeout_handler)),
        max_retries=2,
        backoff_base_seconds=0.25,
        sleep_fn=slept.append,
        jitter_fn=lambda a, b: 0.05,
    )
    with pytest.raises(APITimeoutError, match="Timed out after 3 attempts"):
        timeout_client.fetch_forecast_records()
    assert timeout_attempts == 3
    assert len(slept) == 2

    # 2. Non-retryable 404 HTTP error does not retry
    http_404_attempts = 0

    def not_found_handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_404_attempts
        http_404_attempts += 1
        return httpx.Response(404, text="Not Found")

    http_404_client = EnergyChartsClient(
        http_client=httpx.Client(transport=httpx.MockTransport(not_found_handler)),
        max_retries=3,
        sleep_fn=lambda s: None,
    )
    with pytest.raises(APIHTTPError, match="Non-retryable HTTP 404"):
        http_404_client.fetch_price_records()
    assert http_404_attempts == 1

    # 3. Malformed JSON response
    malformed_client = EnergyChartsClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"{invalid_json"))
        ),
    )
    with pytest.raises(APIMalformedJSONError, match="Malformed JSON"):
        malformed_client.fetch_price_records()

    # 4. Unexpected schema (missing required fields)
    unexpected_schema_client = OpenMeteoClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=json.dumps({"unexpected": "schema"}).encode())
            )
        ),
    )
    with pytest.raises(APISchemaValidationError, match="failed schema/alignment validation"):
        unexpected_schema_client.fetch_forecast_records()


def test_parse_retry_after_supports_seconds_and_http_date() -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("7.5", now=now) == pytest.approx(7.5)
    assert parse_retry_after("Fri, 16 Oct 2026 12:00:15 GMT", now=now) == pytest.approx(15.0)
    assert parse_retry_after("invalid-header", now=now) is None
    assert parse_retry_after(None, now=now) is None
