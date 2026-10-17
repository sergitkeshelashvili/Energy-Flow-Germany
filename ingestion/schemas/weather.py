"""Pydantic v2 models for Open-Meteo weather forecast payloads and normalized records."""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from ingestion.schemas.common import BatchTransformResult, DeadLetterRecord, ensure_utc_datetime


class OpenMeteoHourlyUnits(BaseModel):
    """Units dictionary returned in Open-Meteo `hourly_units`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    time: str = Field(min_length=1)
    temperature_2m: str = Field(min_length=1)
    wind_speed_10m: str = Field(min_length=1)
    shortwave_radiation: str = Field(min_length=1)


class OpenMeteoHourlyData(BaseModel):
    """Aligned hourly forecast arrays returned in Open-Meteo `hourly`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    time: list[str]
    temperature_2m: list[StrictFloat | StrictInt | None]
    wind_speed_10m: list[StrictFloat | StrictInt | None]
    shortwave_radiation: list[StrictFloat | StrictInt | None]

    @model_validator(mode="after")
    def validate_hourly_array_alignment(self) -> "OpenMeteoHourlyData":
        """Ensure all hourly arrays are non-empty and have identical lengths."""
        n_time = len(self.time)
        if n_time == 0:
            raise ValueError("Open-Meteo 'hourly.time' array must not be empty.")

        lengths = {
            "time": n_time,
            "temperature_2m": len(self.temperature_2m),
            "wind_speed_10m": len(self.wind_speed_10m),
            "shortwave_radiation": len(self.shortwave_radiation),
        }
        if len(set(lengths.values())) != 1:
            raise ValueError(f"Misaligned Open-Meteo hourly arrays: {lengths}")
        return self


class OpenMeteoForecastResponse(BaseModel):
    """Raw response schema for the Open-Meteo `/v1/forecast` endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    timezone: str = Field(min_length=1)
    utc_offset_seconds: StrictInt = Field(ge=-86400, le=86400)
    hourly_units: OpenMeteoHourlyUnits
    hourly: OpenMeteoHourlyData

    @field_validator("timezone")
    @classmethod
    def validate_iana_timezone(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Open-Meteo 'timezone' must not be empty.")
        if cleaned.upper() not in {"UTC", "GMT"}:
            try:
                ZoneInfo(cleaned)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown IANA timezone '{cleaned}'.") from exc
        return cleaned


class WeatherUnitsMetadata(BaseModel):
    """Units preserved inside the weather forecast metadata subdocument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature_2m: str = Field(min_length=1)
    wind_speed_10m: str = Field(min_length=1)
    shortwave_radiation: str = Field(min_length=1)


class WeatherForecastMetadata(BaseModel):
    """Metadata subdocument (`metaField`) for the `weather_forecasts` time-series collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location_id: str = Field(min_length=1)
    location_name: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    source: Literal["open_meteo"] = "open_meteo"
    data_type: Literal["forecast"] = "forecast"
    source_timezone: str = Field(min_length=1)
    units: WeatherUnitsMetadata


class WeatherForecastRecord(BaseModel):
    """Normalized and validated weather forecast time-series document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    metadata: WeatherForecastMetadata
    is_forecast: Literal[True] = True
    forecast_version: str = Field(min_length=1)
    temperature_2m: float
    wind_speed_10m: float = Field(ge=0.0)
    shortwave_radiation: float = Field(ge=0.0)
    source_timestamp: str = Field(min_length=1)
    source_timezone: str = Field(min_length=1)
    source_utc_offset_seconds: StrictInt
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("timestamp", "ingested_at")
    @classmethod
    def validate_utc_timestamps(cls, value: datetime) -> datetime:
        return ensure_utc_datetime(value)

    @field_validator("temperature_2m", "wind_speed_10m", "shortwave_radiation", mode="before")
    @classmethod
    def reject_boolean_weather_values(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid meteorological measurements.")
        return value

    @field_validator("temperature_2m", "wind_speed_10m", "shortwave_radiation")
    @classmethod
    def validate_finite_float(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Weather forecast variable must be a finite number.")
        return float(value)

    def to_mongo_doc(self) -> dict[str, Any]:
        """Serialize to a MongoDB time-series document preserving BSON UTC datetimes."""
        return self.model_dump(mode="python")


def parse_open_meteo_timestamp_to_utc(
    source_time: str,
    utc_offset_seconds: int,
) -> datetime:
    """Parse an Open-Meteo ISO timestamp and normalize it to UTC.

    Open-Meteo returns wall-clock timestamps shifted by `utc_offset_seconds` for the
    requested timezone (`Europe/Berlin`). Applying `timezone(timedelta(seconds=utc_offset_seconds))`
    and converting to `timezone.utc` deterministically normalizes each forecast hour to UTC.
    """
    cleaned = source_time.strip()
    if not cleaned:
        raise ValueError("Source timestamp string must not be empty.")
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        source_tz = timezone(timedelta(seconds=utc_offset_seconds))
        parsed = parsed.replace(tzinfo=source_tz)
    return parsed.astimezone(timezone.utc)


def transform_open_meteo_response(
    response: OpenMeteoForecastResponse,
    location_id: str = "DE-DUS",
    location_name: str = "Düsseldorf, Germany",
    ingested_at: datetime | None = None,
    forecast_version: str | None = None,
) -> BatchTransformResult[WeatherForecastRecord]:
    """Transform aligned Open-Meteo hourly forecast arrays into validated UTC records.

    Missing (`None`) or invalid variable readings are never fabricated or interpolated;
    they are captured as `DeadLetterRecord` entries with their slice index and raw values.
    """
    ingestion_ts = ensure_utc_datetime(ingested_at or datetime.now(timezone.utc))
    version_tag = forecast_version or ingestion_ts.strftime("%Y%m%dT%H%M%SZ")
    metadata = WeatherForecastMetadata(
        location_id=location_id,
        location_name=location_name,
        latitude=response.latitude,
        longitude=response.longitude,
        source="open_meteo",
        data_type="forecast",
        source_timezone=response.timezone,
        units=WeatherUnitsMetadata(
            temperature_2m=response.hourly_units.temperature_2m,
            wind_speed_10m=response.hourly_units.wind_speed_10m,
            shortwave_radiation=response.hourly_units.shortwave_radiation,
        ),
    )

    valid_records: list[WeatherForecastRecord] = []
    dead_letters: list[DeadLetterRecord] = []

    hourly = response.hourly
    for idx, (raw_time, temp, wind, radiation) in enumerate(
        zip(
            hourly.time,
            hourly.temperature_2m,
            hourly.wind_speed_10m,
            hourly.shortwave_radiation,
            strict=True,
        )
    ):
        raw_slice = {
            "time": raw_time,
            "temperature_2m": temp,
            "wind_speed_10m": wind,
            "shortwave_radiation": radiation,
            "timezone": response.timezone,
            "utc_offset_seconds": response.utc_offset_seconds,
            "location_id": location_id,
            "forecast_version": version_tag,
        }

        if (
            temp is None
            or wind is None
            or radiation is None
            or isinstance(temp, bool)
            or isinstance(wind, bool)
            or isinstance(radiation, bool)
        ):
            missing_fields = [
                name
                for name, val in (
                    ("temperature_2m", temp),
                    ("wind_speed_10m", wind),
                    ("shortwave_radiation", radiation),
                )
                if val is None or isinstance(val, bool)
            ]
            dead_letters.append(
                DeadLetterRecord(
                    source="open_meteo",
                    scope=location_id,
                    stage="record_validation",
                    reason=f"Missing (null) or invalid forecast values at index {idx}: {', '.join(missing_fields)}",
                    raw_payload=raw_slice,
                    record_index=idx,
                    failed_at=ingestion_ts,
                )
            )
            continue

        try:
            utc_timestamp = parse_open_meteo_timestamp_to_utc(
                raw_time,
                response.utc_offset_seconds,
            )
            record = WeatherForecastRecord(
                timestamp=utc_timestamp,
                metadata=metadata,
                is_forecast=True,
                forecast_version=version_tag,
                temperature_2m=float(temp),
                wind_speed_10m=float(wind),
                shortwave_radiation=float(radiation),
                source_timestamp=raw_time,
                source_timezone=response.timezone,
                source_utc_offset_seconds=response.utc_offset_seconds,
                ingested_at=ingestion_ts,
            )
            valid_records.append(record)
        except (ValidationError, ValueError) as exc:
            dead_letters.append(
                DeadLetterRecord(
                    source="open_meteo",
                    scope=location_id,
                    stage="record_validation",
                    reason=str(exc),
                    raw_payload=raw_slice,
                    record_index=idx,
                    failed_at=ingestion_ts,
                )
            )

    return BatchTransformResult(valid_records=valid_records, dead_letters=dead_letters)
