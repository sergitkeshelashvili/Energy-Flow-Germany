"""Pydantic v2 response schemas for the EnergyFlowGermany FastAPI service."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    """Standardized error payload returned on API or database errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: str
    detail: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HealthResponse(BaseModel):
    """Health check response reporting API and MongoDB connectivity separately."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_status: Literal["up"] = "up"
    mongodb_status: Literal["connected", "disconnected"]
    overall_status: Literal["healthy", "degraded"]
    database_name: str
    checked_at: datetime


class LatestElectricityPrice(BaseModel):
    """Latest stored electricity spot price record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    bidding_zone: str
    price: float
    unit: str
    source: str
    license_info: str = ""
    ingested_at: datetime | None = None


class WeatherUnits(BaseModel):
    """Measurement units for weather forecast variables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature_2m: str
    wind_speed_10m: str
    shortwave_radiation: str


class LatestWeatherForecast(BaseModel):
    """Latest stored weather forecast record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    source_timestamp: str
    source_timezone: str
    location_id: str
    location_name: str
    latitude: float
    longitude: float
    is_forecast: bool = True
    forecast_version: str | None = None
    temperature_2m: float
    wind_speed_10m: float
    shortwave_radiation: float
    units: WeatherUnits
    source: str
    ingested_at: datetime | None = None


class LatestTelemetryResponse(BaseModel):
    """Response for `GET /telemetry/latest`.

    Electricity price and weather forecast records are reported independently with their
    own timestamps, units, and sources so unrelated timestamps are never silently merged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    electricity_price: LatestElectricityPrice | None = None
    weather_forecast: LatestWeatherForecast | None = None
    timestamps_aligned: bool = False
    timestamp_delta_seconds: float | None = None
    note: str


class TimeRangeSummary(BaseModel):
    """Start and end UTC timestamps for a summarized dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_timestamp: datetime
    end_timestamp: datetime


class ElectricityAnalyticsMetrics(BaseModel):
    """Descriptive statistics calculated from stored electricity price records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bidding_zone: str
    source: str
    unit: str
    record_count: int = Field(ge=0)
    time_range: TimeRangeSummary | None = None
    min_price: float | None = None
    max_price: float | None = None
    mean_price: float | None = None
    median_price: float | None = None
    negative_price_hours: int = Field(default=0, ge=0)


class WeatherVariableStats(BaseModel):
    """Min, max, and mean summary for a single weather forecast variable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unit: str
    min_value: float
    max_value: float
    mean_value: float


class WeatherAnalyticsMetrics(BaseModel):
    """Descriptive statistics calculated from stored weather forecast records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location_id: str
    location_name: str
    source: str
    is_forecast: bool = True
    record_count: int = Field(ge=0)
    time_range: TimeRangeSummary | None = None
    temperature_2m: WeatherVariableStats | None = None
    wind_speed_10m: WeatherVariableStats | None = None
    shortwave_radiation: WeatherVariableStats | None = None


class AlignedCorrelationMetrics(BaseModel):
    """Pearson correlations computed strictly across exact matching UTC timestamps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aligned_record_count: int = Field(ge=0)
    minimum_required_records: int = 3
    status: Literal["ok", "insufficient_aligned_data"]
    time_range: TimeRangeSummary | None = None
    price_vs_wind_speed_10m_corr: float | None = None
    price_vs_shortwave_radiation_corr: float | None = None
    price_vs_temperature_2m_corr: float | None = None
    note: str | None = None


class ElectricitySeriesPoint(BaseModel):
    """Single electricity price point for time-series visualization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    price: float
    unit: str


class WeatherSeriesPoint(BaseModel):
    """Single weather forecast point for time-series visualization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    temperature_2m: float
    wind_speed_10m: float
    shortwave_radiation: float


class AnalyticsSummaryResponse(BaseModel):
    """Response for `GET /analytics/summary`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "insufficient_data"]
    insufficient_data_reason: str | None = None
    electricity: ElectricityAnalyticsMetrics
    weather: WeatherAnalyticsMetrics
    alignment_and_correlation: AlignedCorrelationMetrics
    electricity_series: list[ElectricitySeriesPoint] = Field(default_factory=list)
    weather_series: list[WeatherSeriesPoint] = Field(default_factory=list)


class ForecastPoint(BaseModel):
    """Single deterministic baseline forecast step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_horizon_hours: int = Field(ge=1, le=6)
    target_timestamp: datetime
    forecast_price: float
    unit: str
    persistence_weight: float = Field(ge=0.0, le=1.0)
    mean_reversion_weight: float = Field(ge=0.0, le=1.0)


class SixHourForecastResponse(BaseModel):
    """Response for `GET /forecast/6h` providing a deterministic baseline price forecast."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "insufficient_data"]
    insufficient_data_reason: str | None = None
    bidding_zone: str
    unit: str
    horizon_hours: int = 6
    history_records_used: int = Field(ge=0)
    minimum_history_required: int = 3
    history_time_range: TimeRangeSummary | None = None
    reference_last_timestamp: datetime | None = None
    reference_last_price: float | None = None
    trailing_mean_price: float | None = None
    hourly_drift_estimate: float | None = None
    method: str = "deterministic_damped_drift_mean_reversion_baseline"
    model_version: str = "baseline-v1.0"
    is_machine_learning_model: Literal[False] = False
    method_description: str
    limitations: list[str]
    forecast_points: list[ForecastPoint] = Field(default_factory=list)


class DLQCategorySummary(BaseModel):
    """Aggregated DLQ count grouped by source, stage, and sanitized error category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    stage: str
    error_category: str
    count: int = Field(ge=0)
    latest_failed_at: datetime | None = None


class DLQStatsResponse(BaseModel):
    """Response for `GET /admin/dlq-stats`.

    Exposes only aggregated counts and sanitized error categories—raw payloads are excluded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_records: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    resolved_count: int = Field(ge=0)
    by_source: dict[str, int]
    by_stage: dict[str, int]
    categories: list[DLQCategorySummary]
    latest_failed_at: datetime | None = None
