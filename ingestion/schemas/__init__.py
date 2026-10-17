"""Pydantic v2 data validation schemas for ingested payloads and MongoDB documents."""

from ingestion.schemas.common import (
    BatchTransformResult,
    CheckpointStatus,
    DeadLetterRecord,
    FailureStage,
    IngestionCheckpoint,
    SourceName,
    ensure_utc_datetime,
)
from ingestion.schemas.electricity import (
    ElectricityPriceMetadata,
    ElectricityPriceRecord,
    EnergyChartsPriceResponse,
    transform_energy_charts_response,
)
from ingestion.schemas.weather import (
    OpenMeteoForecastResponse,
    OpenMeteoHourlyData,
    OpenMeteoHourlyUnits,
    WeatherForecastMetadata,
    WeatherForecastRecord,
    WeatherUnitsMetadata,
    parse_open_meteo_timestamp_to_utc,
    transform_open_meteo_response,
)

__all__ = [
    "BatchTransformResult",
    "CheckpointStatus",
    "DeadLetterRecord",
    "ElectricityPriceMetadata",
    "ElectricityPriceRecord",
    "EnergyChartsPriceResponse",
    "FailureStage",
    "IngestionCheckpoint",
    "OpenMeteoForecastResponse",
    "OpenMeteoHourlyData",
    "OpenMeteoHourlyUnits",
    "SourceName",
    "WeatherForecastMetadata",
    "WeatherForecastRecord",
    "WeatherUnitsMetadata",
    "ensure_utc_datetime",
    "parse_open_meteo_timestamp_to_utc",
    "transform_energy_charts_response",
    "transform_open_meteo_response",
]
