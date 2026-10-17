"""Data ingestion package for fetching, validating, and storing energy and weather data."""

from typing import Any

from ingestion.repository import (
    CheckpointRepository,
    DeadLetterRepository,
    ElectricityPriceRepository,
    PersistenceStats,
    WeatherForecastRepository,
)

__all__ = [
    "CheckpointRepository",
    "DeadLetterRepository",
    "ElectricityPriceRepository",
    "IngestionRunner",
    "PersistenceStats",
    "WeatherForecastRepository",
    "run_pipeline_from_settings",
]


def __getattr__(name: str) -> Any:
    """Lazily import runner symbols so `python -m ingestion.runner` avoids runpy warnings."""
    if name in {"IngestionRunner", "run_pipeline_from_settings"}:
        from ingestion import runner

        return getattr(runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
