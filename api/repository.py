"""Dedicated read-only repository and analytical computation layer for the FastAPI service."""

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import math
import statistics
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.collection import Collection

from api.schemas.responses import (
    AlignedCorrelationMetrics,
    AnalyticsSummaryResponse,
    DLQCategorySummary,
    DLQStatsResponse,
    ElectricityAnalyticsMetrics,
    ElectricitySeriesPoint,
    ForecastPoint,
    HealthResponse,
    LatestElectricityPrice,
    LatestTelemetryResponse,
    LatestWeatherForecast,
    SixHourForecastResponse,
    TimeRangeSummary,
    WeatherAnalyticsMetrics,
    WeatherSeriesPoint,
    WeatherUnits,
    WeatherVariableStats,
)
from common.logging import get_logger
from ingestion.schemas.common import ensure_utc_datetime

logger = get_logger(__name__)

MIN_ALIGNED_RECORDS_FOR_CORRELATION = 3
MIN_HISTORY_FOR_FORECAST = 3

BASELINE_FORECAST_METHOD = "deterministic_damped_drift_mean_reversion_baseline"
BASELINE_FORECAST_VERSION = "baseline-v1.0"
BASELINE_FORECAST_DESCRIPTION = (
    "Deterministic statistical baseline that blends the most recent observed spot price "
    "plus exponentially damped trailing hourly drift (weight 0.5^h at horizon h) with "
    "the arithmetic mean of the trailing historical window (weight 1 - 0.5^h). "
    "Contains zero random noise and does not use a machine learning model."
)
BASELINE_FORECAST_LIMITATIONS = [
    "Statistical heuristic baseline only — not a trained or evaluated machine learning model.",
    "Relies exclusively on trailing price persistence, damped linear drift, and mean reversion.",
    "Does not model day-ahead auction bid curves, grid congestion, plant outages, or demand shocks.",
    "Cannot anticipate sudden regime shifts or negative price spikes beyond trailing price history.",
]


def _normalize_ts(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return ensure_utc_datetime(value)


def _ingested_sort_key(doc: dict[str, Any]) -> datetime:
    raw = doc.get("ingested_at")
    if isinstance(raw, datetime):
        return _normalize_ts(raw)
    return datetime.min.replace(tzinfo=timezone.utc)


def _deduplicate_by_timestamp_keep_latest_version(
    docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate records by UTC `timestamp`, keeping the newest `ingested_at` version."""
    latest_by_ts: dict[datetime, dict[str, Any]] = {}
    for doc in docs:
        ts = _normalize_ts(doc["timestamp"])
        existing = latest_by_ts.get(ts)
        if existing is None or _ingested_sort_key(doc) >= _ingested_sort_key(existing):
            latest_by_ts[ts] = doc
    return [latest_by_ts[ts] for ts in sorted(latest_by_ts.keys())]


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Compute sample Pearson correlation coefficient, returning None if variance is zero."""
    n = len(xs)
    if n < MIN_ALIGNED_RECORDS_FOR_CORRELATION or len(ys) != n:
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    den_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(den_x * den_y)
    if denom <= 1e-12:
        return None
    clamped = max(-1.0, min(1.0, num / denom))
    return round(clamped, 4)


def _classify_dlq_reason(stage: str, reason: str) -> str:
    """Map a raw DLQ reason string to a sanitized, non-sensitive error category."""
    lowered = reason.lower()
    if "missing (null)" in lowered or "null" in lowered:
        return "missing_null_value"
    if "misaligned" in lowered or "alignment" in lowered:
        return "array_alignment_error"
    if "429" in lowered or "rate limit" in lowered:
        return "http_rate_limit_429"
    if "timed out" in lowered or "timeout" in lowered:
        return "http_timeout"
    if "malformed json" in lowered or "json object" in lowered:
        return "malformed_json"
    if stage == "http_fetch":
        return "http_upstream_error"
    if stage == "payload_validation":
        return "schema_validation_error"
    return "record_validation_error"


class APIRepository:
    """Read-only MongoDB repository backing all FastAPI analytical endpoints."""

    def __init__(
        self,
        *,
        electricity_collection: Collection[dict[str, Any]],
        weather_collection: Collection[dict[str, Any]],
        dlq_collection: Collection[dict[str, Any]],
        ping_fn: Callable[[], bool],
        database_name: str = "energy_flow_germany",
    ) -> None:
        self._electricity = electricity_collection
        self._weather = weather_collection
        self._dlq = dlq_collection
        self._ping_fn = ping_fn
        self._database_name = database_name

    def get_health(self, now: datetime | None = None) -> HealthResponse:
        """Check API and MongoDB connectivity independently."""
        checked_at = _normalize_ts(now or datetime.now(timezone.utc))
        try:
            is_connected = bool(self._ping_fn())
        except Exception as exc:
            logger.error("MongoDB health check failed: %s", exc)
            is_connected = False

        return HealthResponse(
            api_status="up",
            mongodb_status="connected" if is_connected else "disconnected",
            overall_status="healthy" if is_connected else "degraded",
            database_name=self._database_name,
            checked_at=checked_at,
        )

    def _query_sorted_records(
        self,
        collection: Collection[dict[str, Any]],
        filter_spec: dict[str, Any],
        *,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query documents from a MongoDB collection and sort deterministically by (timestamp, ingested_at)."""
        cursor = collection.find(filter_spec, {"_id": 0})
        if not isinstance(cursor, list) and hasattr(cursor, "sort") and hasattr(cursor, "limit"):
            direction = DESCENDING if descending else ASCENDING
            cursor = cursor.sort([("timestamp", direction), ("ingested_at", DESCENDING)])
            if limit is not None:
                cursor = cursor.limit(limit)
            docs = list(cursor)
        else:
            docs = list(cursor)
            docs.sort(
                key=lambda d: (_normalize_ts(d["timestamp"]), _ingested_sort_key(d)),
                reverse=descending,
            )
            if limit is not None:
                docs = docs[:limit]
        return docs

    def get_latest_electricity_price(
        self,
        bidding_zone: str = "DE-LU",
    ) -> LatestElectricityPrice | None:
        """Return the latest stored electricity price record for `bidding_zone`."""
        docs = self._query_sorted_records(
            self._electricity,
            {"metadata.bidding_zone": bidding_zone},
            descending=True,
            limit=1,
        )
        if not docs:
            return None
        doc = docs[0]
        meta = doc.get("metadata", {})
        ingested_raw = doc.get("ingested_at")
        return LatestElectricityPrice(
            timestamp=_normalize_ts(doc["timestamp"]),
            bidding_zone=meta.get("bidding_zone", bidding_zone),
            price=float(doc["price"]),
            unit=doc.get("unit") or meta.get("unit", "EUR / MWh"),
            source=meta.get("source", "energy_charts"),
            license_info=meta.get("license_info", ""),
            ingested_at=_normalize_ts(ingested_raw) if ingested_raw is not None else None,
        )

    def get_latest_weather_forecast(
        self,
        location_id: str = "DE-DUS",
    ) -> LatestWeatherForecast | None:
        """Return the latest stored weather forecast record for `location_id`."""
        docs = self._query_sorted_records(
            self._weather,
            {"metadata.location_id": location_id},
            descending=True,
            limit=1,
        )
        if not docs:
            return None
        doc = docs[0]
        meta = doc.get("metadata", {})
        units_meta = meta.get("units", {})
        ingested_raw = doc.get("ingested_at")
        return LatestWeatherForecast(
            timestamp=_normalize_ts(doc["timestamp"]),
            source_timestamp=str(doc.get("source_timestamp", "")),
            source_timezone=str(
                doc.get("source_timezone") or meta.get("source_timezone", "Europe/Berlin")
            ),
            location_id=meta.get("location_id", location_id),
            location_name=meta.get("location_name", location_id),
            latitude=float(meta.get("latitude", 51.2277)),
            longitude=float(meta.get("longitude", 6.7735)),
            is_forecast=bool(doc.get("is_forecast", True)),
            forecast_version=doc.get("forecast_version"),
            temperature_2m=float(doc["temperature_2m"]),
            wind_speed_10m=float(doc["wind_speed_10m"]),
            shortwave_radiation=float(doc["shortwave_radiation"]),
            units=WeatherUnits(
                temperature_2m=units_meta.get("temperature_2m", "°C"),
                wind_speed_10m=units_meta.get("wind_speed_10m", "km/h"),
                shortwave_radiation=units_meta.get("shortwave_radiation", "W/m²"),
            ),
            source=meta.get("source", "open_meteo"),
            ingested_at=_normalize_ts(ingested_raw) if ingested_raw is not None else None,
        )

    def get_latest_telemetry(
        self,
        *,
        bidding_zone: str = "DE-LU",
        location_id: str = "DE-DUS",
    ) -> LatestTelemetryResponse:
        """Return latest electricity price and weather forecast without combining unrelated timestamps."""
        latest_price = self.get_latest_electricity_price(bidding_zone=bidding_zone)
        latest_weather = self.get_latest_weather_forecast(location_id=location_id)

        if latest_price is not None and latest_weather is not None:
            delta_sec = (latest_price.timestamp - latest_weather.timestamp).total_seconds()
            aligned = delta_sec == 0.0
            if aligned:
                note = (
                    "Latest electricity price and weather forecast share the same UTC timestamp "
                    f"({latest_price.timestamp.isoformat()})."
                )
            else:
                note = (
                    "Latest electricity price and weather forecast have different UTC timestamps "
                    f"(delta={delta_sec:.0f}s) and are reported separately without merging."
                )
            return LatestTelemetryResponse(
                electricity_price=latest_price,
                weather_forecast=latest_weather,
                timestamps_aligned=aligned,
                timestamp_delta_seconds=delta_sec,
                note=note,
            )

        missing: list[str] = []
        if latest_price is None:
            missing.append(f"electricity_prices(bidding_zone={bidding_zone})")
        if latest_weather is None:
            missing.append(f"weather_forecasts(location_id={location_id})")

        return LatestTelemetryResponse(
            electricity_price=latest_price,
            weather_forecast=latest_weather,
            timestamps_aligned=False,
            timestamp_delta_seconds=None,
            note=f"No records available for: {', '.join(missing)}.",
        )

    def get_analytics_summary(
        self,
        *,
        bidding_zone: str = "DE-LU",
        location_id: str = "DE-DUS",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 168,
    ) -> AnalyticsSummaryResponse:
        """Calculate summary statistics and aligned correlations from actual stored records."""
        time_filter: dict[str, Any] = {}
        if start is not None:
            time_filter["$gte"] = _normalize_ts(start)
        if end is not None:
            time_filter["$lte"] = _normalize_ts(end)

        elec_filter: dict[str, Any] = {"metadata.bidding_zone": bidding_zone}
        weather_filter: dict[str, Any] = {"metadata.location_id": location_id}
        if time_filter:
            elec_filter["timestamp"] = dict(time_filter)
            weather_filter["timestamp"] = dict(time_filter)

        raw_elec_docs = self._query_sorted_records(
            self._electricity,
            elec_filter,
            descending=True,
            limit=limit,
        )
        elec_docs = _deduplicate_by_timestamp_keep_latest_version(raw_elec_docs)

        raw_weather_docs = self._query_sorted_records(
            self._weather,
            weather_filter,
            descending=True,
            limit=limit,
        )
        weather_docs = _deduplicate_by_timestamp_keep_latest_version(raw_weather_docs)

        # 1. Electricity Summary Metrics
        if elec_docs:
            prices = [float(d["price"]) for d in elec_docs]
            elec_timestamps = [_normalize_ts(d["timestamp"]) for d in elec_docs]
            first_meta = elec_docs[0].get("metadata", {})
            elec_unit = elec_docs[0].get("unit") or first_meta.get("unit", "EUR / MWh")
            electricity_metrics = ElectricityAnalyticsMetrics(
                bidding_zone=bidding_zone,
                source=first_meta.get("source", "energy_charts"),
                unit=elec_unit,
                record_count=len(elec_docs),
                time_range=TimeRangeSummary(
                    start_timestamp=min(elec_timestamps),
                    end_timestamp=max(elec_timestamps),
                ),
                min_price=round(min(prices), 4),
                max_price=round(max(prices), 4),
                mean_price=round(statistics.fmean(prices), 4),
                median_price=round(statistics.median(prices), 4),
                negative_price_hours=sum(1 for p in prices if p < 0.0),
            )
            electricity_series = [
                ElectricitySeriesPoint(
                    timestamp=_normalize_ts(d["timestamp"]),
                    price=float(d["price"]),
                    unit=d.get("unit") or first_meta.get("unit", "EUR / MWh"),
                )
                for d in elec_docs
            ]
        else:
            electricity_metrics = ElectricityAnalyticsMetrics(
                bidding_zone=bidding_zone,
                source="energy_charts",
                unit="EUR / MWh",
                record_count=0,
            )
            electricity_series = []

        # 2. Weather Summary Metrics
        if weather_docs:
            temps = [float(d["temperature_2m"]) for d in weather_docs]
            winds = [float(d["wind_speed_10m"]) for d in weather_docs]
            rads = [float(d["shortwave_radiation"]) for d in weather_docs]
            weather_timestamps = [_normalize_ts(d["timestamp"]) for d in weather_docs]
            w_meta = weather_docs[0].get("metadata", {})
            w_units = w_meta.get("units", {})
            weather_metrics = WeatherAnalyticsMetrics(
                location_id=location_id,
                location_name=w_meta.get("location_name", location_id),
                source=w_meta.get("source", "open_meteo"),
                is_forecast=True,
                record_count=len(weather_docs),
                time_range=TimeRangeSummary(
                    start_timestamp=min(weather_timestamps),
                    end_timestamp=max(weather_timestamps),
                ),
                temperature_2m=WeatherVariableStats(
                    unit=w_units.get("temperature_2m", "°C"),
                    min_value=round(min(temps), 4),
                    max_value=round(max(temps), 4),
                    mean_value=round(statistics.fmean(temps), 4),
                ),
                wind_speed_10m=WeatherVariableStats(
                    unit=w_units.get("wind_speed_10m", "km/h"),
                    min_value=round(min(winds), 4),
                    max_value=round(max(winds), 4),
                    mean_value=round(statistics.fmean(winds), 4),
                ),
                shortwave_radiation=WeatherVariableStats(
                    unit=w_units.get("shortwave_radiation", "W/m²"),
                    min_value=round(min(rads), 4),
                    max_value=round(max(rads), 4),
                    mean_value=round(statistics.fmean(rads), 4),
                ),
            )
            weather_series = [
                WeatherSeriesPoint(
                    timestamp=_normalize_ts(d["timestamp"]),
                    temperature_2m=float(d["temperature_2m"]),
                    wind_speed_10m=float(d["wind_speed_10m"]),
                    shortwave_radiation=float(d["shortwave_radiation"]),
                )
                for d in weather_docs
            ]
        else:
            weather_metrics = WeatherAnalyticsMetrics(
                location_id=location_id,
                location_name=location_id,
                source="open_meteo",
                is_forecast=True,
                record_count=0,
            )
            weather_series = []

        # 3. Exact UTC Timestamp Alignment & Correlation
        elec_by_ts = {_normalize_ts(d["timestamp"]): d for d in elec_docs}
        weather_by_ts = {_normalize_ts(d["timestamp"]): d for d in weather_docs}
        common_ts = sorted(set(elec_by_ts.keys()) & set(weather_by_ts.keys()))

        insufficient_reasons: list[str] = []
        if not elec_docs:
            insufficient_reasons.append(
                f"No stored electricity price records for bidding_zone='{bidding_zone}'."
            )
        if not weather_docs:
            insufficient_reasons.append(
                f"No stored weather forecast records for location_id='{location_id}'."
            )

        if len(common_ts) < MIN_ALIGNED_RECORDS_FOR_CORRELATION:
            align_reason = (
                f"Found {len(common_ts)} aligned UTC timestamps between electricity prices and "
                f"weather forecasts; at least {MIN_ALIGNED_RECORDS_FOR_CORRELATION} are required "
                "for correlation analysis."
            )
            insufficient_reasons.append(align_reason)
            alignment_metrics = AlignedCorrelationMetrics(
                aligned_record_count=len(common_ts),
                minimum_required_records=MIN_ALIGNED_RECORDS_FOR_CORRELATION,
                status="insufficient_aligned_data",
                time_range=(
                    TimeRangeSummary(
                        start_timestamp=common_ts[0],
                        end_timestamp=common_ts[-1],
                    )
                    if common_ts
                    else None
                ),
                note=align_reason,
            )
        else:
            aligned_prices = [float(elec_by_ts[ts]["price"]) for ts in common_ts]
            aligned_winds = [float(weather_by_ts[ts]["wind_speed_10m"]) for ts in common_ts]
            aligned_rads = [float(weather_by_ts[ts]["shortwave_radiation"]) for ts in common_ts]
            aligned_temps = [float(weather_by_ts[ts]["temperature_2m"]) for ts in common_ts]

            alignment_metrics = AlignedCorrelationMetrics(
                aligned_record_count=len(common_ts),
                minimum_required_records=MIN_ALIGNED_RECORDS_FOR_CORRELATION,
                status="ok",
                time_range=TimeRangeSummary(
                    start_timestamp=common_ts[0],
                    end_timestamp=common_ts[-1],
                ),
                price_vs_wind_speed_10m_corr=_pearson_correlation(aligned_prices, aligned_winds),
                price_vs_shortwave_radiation_corr=_pearson_correlation(aligned_prices, aligned_rads),
                price_vs_temperature_2m_corr=_pearson_correlation(aligned_prices, aligned_temps),
                note="Correlations computed strictly across exact matching UTC timestamps.",
            )

        overall_status = "insufficient_data" if insufficient_reasons else "ok"
        return AnalyticsSummaryResponse(
            status=overall_status,
            insufficient_data_reason=" ".join(insufficient_reasons) if insufficient_reasons else None,
            electricity=electricity_metrics,
            weather=weather_metrics,
            alignment_and_correlation=alignment_metrics,
            electricity_series=electricity_series,
            weather_series=weather_series,
        )

    def get_6h_forecast(
        self,
        *,
        bidding_zone: str = "DE-LU",
        history_window: int = 24,
    ) -> SixHourForecastResponse:
        """Compute a deterministic 6-hour baseline electricity price forecast from stored records."""
        raw_docs = self._query_sorted_records(
            self._electricity,
            {"metadata.bidding_zone": bidding_zone},
            descending=True,
            limit=history_window,
        )
        docs = _deduplicate_by_timestamp_keep_latest_version(raw_docs)

        if len(docs) < MIN_HISTORY_FOR_FORECAST:
            reason = (
                f"Insufficient historical electricity price records for bidding_zone='{bidding_zone}': "
                f"found {len(docs)}, require at least {MIN_HISTORY_FOR_FORECAST}."
            )
            return SixHourForecastResponse(
                status="insufficient_data",
                insufficient_data_reason=reason,
                bidding_zone=bidding_zone,
                unit="EUR / MWh",
                horizon_hours=6,
                history_records_used=len(docs),
                minimum_history_required=MIN_HISTORY_FOR_FORECAST,
                method=BASELINE_FORECAST_METHOD,
                model_version=BASELINE_FORECAST_VERSION,
                is_machine_learning_model=False,
                method_description=BASELINE_FORECAST_DESCRIPTION,
                limitations=BASELINE_FORECAST_LIMITATIONS,
                forecast_points=[],
            )

        timestamps = [_normalize_ts(d["timestamp"]) for d in docs]
        prices = [float(d["price"]) for d in docs]
        first_meta = docs[-1].get("metadata", {})
        unit = docs[-1].get("unit") or first_meta.get("unit", "EUR / MWh")

        last_ts = timestamps[-1]
        last_price = prices[-1]
        trailing_mean = statistics.fmean(prices)
        elapsed_hours = max((last_ts - timestamps[0]).total_seconds() / 3600.0, 1.0)
        hourly_drift = (prices[-1] - prices[0]) / elapsed_hours

        forecast_points: list[ForecastPoint] = []
        for step in range(1, 7):
            persistence_weight = round(0.5**step, 6)
            mean_reversion_weight = round(1.0 - persistence_weight, 6)
            damped_drift_sum = hourly_drift * sum(0.5**i for i in range(1, step + 1))
            projected_trend = last_price + damped_drift_sum
            point_estimate = round(
                persistence_weight * projected_trend + mean_reversion_weight * trailing_mean,
                2,
            )
            forecast_points.append(
                ForecastPoint(
                    step_horizon_hours=step,
                    target_timestamp=last_ts + timedelta(hours=step),
                    forecast_price=point_estimate,
                    unit=unit,
                    persistence_weight=persistence_weight,
                    mean_reversion_weight=mean_reversion_weight,
                )
            )

        return SixHourForecastResponse(
            status="ok",
            insufficient_data_reason=None,
            bidding_zone=bidding_zone,
            unit=unit,
            horizon_hours=6,
            history_records_used=len(docs),
            minimum_history_required=MIN_HISTORY_FOR_FORECAST,
            history_time_range=TimeRangeSummary(
                start_timestamp=timestamps[0],
                end_timestamp=last_ts,
            ),
            reference_last_timestamp=last_ts,
            reference_last_price=round(last_price, 4),
            trailing_mean_price=round(trailing_mean, 4),
            hourly_drift_estimate=round(hourly_drift, 4),
            method=BASELINE_FORECAST_METHOD,
            model_version=BASELINE_FORECAST_VERSION,
            is_machine_learning_model=False,
            method_description=BASELINE_FORECAST_DESCRIPTION,
            limitations=BASELINE_FORECAST_LIMITATIONS,
            forecast_points=forecast_points,
        )

    def get_dlq_stats(self) -> DLQStatsResponse:
        """Aggregate DLQ counts and sanitized error categories without exposing raw_payload."""
        safe_projection = {
            "source": 1,
            "stage": 1,
            "reason": 1,
            "failed_at": 1,
            "resolved": 1,
            "_id": 0,
        }
        docs = list(self._dlq.find({}, safe_projection))

        if not docs:
            return DLQStatsResponse(
                total_records=0,
                unresolved_count=0,
                resolved_count=0,
                by_source={},
                by_stage={},
                categories=[],
                latest_failed_at=None,
            )

        by_source: Counter[str] = Counter()
        by_stage: Counter[str] = Counter()
        unresolved = 0
        resolved = 0
        all_failed_at: list[datetime] = []

        category_counts: Counter[tuple[str, str, str]] = Counter()
        category_latest: dict[tuple[str, str, str], datetime] = defaultdict(
            lambda: datetime.min.replace(tzinfo=timezone.utc)
        )

        for doc in docs:
            source = str(doc.get("source", "unknown"))
            stage = str(doc.get("stage", "unknown"))
            reason = str(doc.get("reason", ""))
            is_resolved = bool(doc.get("resolved", False))

            by_source[source] += 1
            by_stage[stage] += 1
            if is_resolved:
                resolved += 1
            else:
                unresolved += 1

            category = _classify_dlq_reason(stage, reason)
            key = (source, stage, category)
            category_counts[key] += 1

            raw_failed = doc.get("failed_at")
            if isinstance(raw_failed, datetime):
                failed_utc = _normalize_ts(raw_failed)
                all_failed_at.append(failed_utc)
                if failed_utc > category_latest[key]:
                    category_latest[key] = failed_utc

        categories = [
            DLQCategorySummary(
                source=src,
                stage=stg,
                error_category=cat,
                count=cnt,
                latest_failed_at=(
                    category_latest[(src, stg, cat)]
                    if (src, stg, cat) in category_latest
                    and category_latest[(src, stg, cat)] != datetime.min.replace(tzinfo=timezone.utc)
                    else None
                ),
            )
            for (src, stg, cat), cnt in sorted(
                category_counts.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1], item[0][2]),
            )
        ]

        return DLQStatsResponse(
            total_records=len(docs),
            unresolved_count=unresolved,
            resolved_count=resolved,
            by_source=dict(by_source),
            by_stage=dict(by_stage),
            categories=categories,
            latest_failed_at=max(all_failed_at) if all_failed_at else None,
        )
