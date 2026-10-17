"""Unit tests for Pydantic v2 ingestion schemas and transformation logic."""

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from ingestion.schemas.electricity import (
    EnergyChartsPriceResponse,
    transform_energy_charts_response,
)
from ingestion.schemas.weather import (
    OpenMeteoForecastResponse,
    transform_open_meteo_response,
)


def test_energy_charts_schema_and_transformation_preserves_alignment_and_metadata(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    ingested_at = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    response = EnergyChartsPriceResponse.model_validate(energy_charts_fixture_dict)
    result = transform_energy_charts_response(
        response,
        bidding_zone="DE-LU",
        ingested_at=ingested_at,
    )

    assert len(result.dead_letters) == 0
    assert len(result.valid_records) == 4

    first = result.valid_records[0]
    assert first.source_unix_seconds == 1792101600
    assert first.timestamp == datetime.fromtimestamp(1792101600, tz=timezone.utc)
    assert first.timestamp.tzinfo == timezone.utc
    assert first.price == 84.52
    assert first.unit == "EUR / MWh"
    assert first.metadata.bidding_zone == "DE-LU"
    assert first.metadata.source == "energy_charts"
    assert first.metadata.unit == "EUR / MWh"
    assert "SMARD.de" in first.metadata.license_info
    assert first.ingested_at == ingested_at


def test_energy_charts_rejects_misaligned_arrays(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    broken = dict(energy_charts_fixture_dict)
    broken["price"] = [84.52, 79.10]  # length 2 vs unix_seconds length 4

    with pytest.raises(ValidationError, match="Misaligned Energy-Charts arrays"):
        EnergyChartsPriceResponse.model_validate(broken)


def test_energy_charts_null_price_routed_to_dlq_without_fabrication(
    energy_charts_fixture_dict: dict[str, Any],
) -> None:
    payload = dict(energy_charts_fixture_dict)
    payload["price"] = [84.52, None, 75.00, float("nan")]

    response = EnergyChartsPriceResponse.model_validate(payload)
    result = transform_energy_charts_response(response, bidding_zone="DE-LU")

    assert len(result.valid_records) == 2
    assert [r.price for r in result.valid_records] == [84.52, 75.00]
    assert len(result.dead_letters) == 2
    assert result.dead_letters[0].record_index == 1
    assert result.dead_letters[0].source == "energy_charts"
    assert result.dead_letters[0].stage == "record_validation"
    assert result.dead_letters[1].record_index == 3


def test_open_meteo_schema_and_transformation_normalizes_europe_berlin_to_utc(
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    ingested_at = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    response = OpenMeteoForecastResponse.model_validate(open_meteo_fixture_dict)
    result = transform_open_meteo_response(
        response,
        location_id="DE-DUS",
        location_name="Düsseldorf, Germany",
        ingested_at=ingested_at,
    )

    assert len(result.dead_letters) == 0
    assert len(result.valid_records) == 4

    first = result.valid_records[0]
    # "2026-10-16T00:00" in Europe/Berlin (CEST, utc_offset_seconds=7200) -> 2026-10-15T22:00:00Z
    assert first.source_timestamp == "2026-10-16T00:00"
    assert first.source_timezone == "Europe/Berlin"
    assert first.source_utc_offset_seconds == 7200
    assert first.timestamp == datetime(2026, 10, 15, 22, 0, 0, tzinfo=timezone.utc)
    assert first.is_forecast is True
    assert first.metadata.data_type == "forecast"
    assert first.metadata.location_id == "DE-DUS"
    assert first.metadata.location_name == "Düsseldorf, Germany"
    assert first.metadata.units.temperature_2m == "°C"
    assert first.metadata.units.wind_speed_10m == "km/h"
    assert first.metadata.units.shortwave_radiation == "W/m²"
    assert first.temperature_2m == 12.4
    assert first.wind_speed_10m == 18.5
    assert first.shortwave_radiation == 0.0
    assert first.ingested_at == ingested_at


def test_open_meteo_rejects_misaligned_hourly_arrays(
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    broken = dict(open_meteo_fixture_dict)
    broken["hourly"] = dict(open_meteo_fixture_dict["hourly"])
    broken["hourly"]["wind_speed_10m"] = [18.5, 19.2]  # 2 items instead of 4

    with pytest.raises(ValidationError, match="Misaligned Open-Meteo hourly arrays"):
        OpenMeteoForecastResponse.model_validate(broken)


def test_open_meteo_missing_and_invalid_values_routed_to_dlq_without_fabrication(
    open_meteo_fixture_dict: dict[str, Any],
) -> None:
    payload = dict(open_meteo_fixture_dict)
    hourly = dict(open_meteo_fixture_dict["hourly"])
    hourly["temperature_2m"] = [12.4, None, 11.5, 11.2]
    hourly["wind_speed_10m"] = [18.5, 19.2, -5.0, 17.8]  # negative wind speed at index 2
    payload["hourly"] = hourly

    response = OpenMeteoForecastResponse.model_validate(payload)
    result = transform_open_meteo_response(response, location_id="DE-DUS")

    assert len(result.valid_records) == 2
    assert [r.source_timestamp for r in result.valid_records] == [
        "2026-10-16T00:00",
        "2026-10-16T03:00",
    ]
    assert len(result.dead_letters) == 2
    assert result.dead_letters[0].record_index == 1
    assert "temperature_2m" in result.dead_letters[0].reason
    assert result.dead_letters[1].record_index == 2
    assert result.dead_letters[1].source == "open_meteo"
