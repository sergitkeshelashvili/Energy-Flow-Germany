"""Pure formatting, freshness classification, and data-transformation functions for the dashboard.

Kept separate from Streamlit UI rendering so all formatting and data-quality logic can be
deterministically unit-tested offline.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Literal

FreshnessLevel = Literal["fresh", "forecast_horizon", "stale", "missing"]


@dataclass(frozen=True)
class FreshnessStatus:
    """Structured freshness assessment for a timestamped telemetry record."""

    level: FreshnessLevel
    label: str
    age_Text: str
    delta_seconds: float | None


def parse_iso_utc(raw_timestamp: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string or datetime into a UTC-aware datetime."""
    if raw_timestamp is None:
        return None
    if isinstance(raw_timestamp, datetime):
        if raw_timestamp.tzinfo is None:
            return raw_timestamp.replace(tzinfo=timezone.utc)
        return raw_timestamp.astimezone(timezone.utc)
    cleaned = str(raw_timestamp).strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc_timestamp(raw_timestamp: str | datetime | None, fallback: str = "N/A") -> str:
    """Format a timestamp consistently as `YYYY-MM-DD HH:MM UTC`."""
    dt = parse_iso_utc(raw_timestamp)
    if dt is None:
        return fallback
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def format_time_range(
    time_range: dict[str, Any] | None,
    fallback: str = "No time range available",
) -> str:
    """Format a `{start_timestamp, end_timestamp}` dictionary into an explicit UTC range string."""
    if not time_range:
        return fallback
    start_str = format_utc_timestamp(time_range.get("start_timestamp"), fallback="")
    end_str = format_utc_timestamp(time_range.get("end_timestamp"), fallback="")
    if not start_str or not end_str:
        return fallback
    return f"{start_str} → {end_str}"


def format_price_with_unit(
    price: float | int | None,
    unit: str = "EUR / MWh",
    fallback: str = "N/A",
) -> str:
    """Format an electricity price value with 2 decimal places and explicit unit."""
    if (
        price is None
        or isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(float(price))
    ):
        return fallback
    clean_unit = unit.strip() or "EUR / MWh"
    return f"{float(price):,.2f} {clean_unit}"


def format_weather_metric(
    value: float | int | None,
    unit: str,
    decimals: int = 1,
    fallback: str = "N/A",
) -> str:
    """Format a meteorological variable with explicit unit."""
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return fallback
    clean_unit = unit.strip()
    formatted = f"{float(value):,.{decimals}f}"
    return f"{formatted} {clean_unit}" if clean_unit else formatted


def format_correlation(value: float | None, fallback: str = "Insufficient data") -> str:
    """Format a Pearson correlation coefficient in `[-1.0, +1.0]` with directional sign."""
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return fallback
    return f"{float(value):+.3f}"


def evaluate_data_freshness(
    record_timestamp: str | datetime | None,
    *,
    now: datetime | None = None,
    stale_threshold_seconds: float = 7200.0,
) -> FreshnessStatus:
    """Evaluate data freshness of a record timestamp relative to `now` (UTC).

    - If `record_timestamp` is missing/unparseable -> `level="missing"`
    - If `record_timestamp` is in the future (`age_seconds < -300`) -> `level="forecast_horizon"`
      (typical for day-ahead prices or multi-day weather forecasts)
    - If `0 <= age_seconds <= stale_threshold_seconds` -> `level="fresh"`
    - If `age_seconds > stale_threshold_seconds` -> `level="stale"`
    """
    dt = parse_iso_utc(record_timestamp)
    if dt is None:
        return FreshnessStatus(
            level="missing",
            label="NO DATA",
            age_Text="No timestamp available",
            delta_seconds=None,
        )

    ref_now = parse_iso_utc(now) or datetime.now(timezone.utc)
    age_seconds = (ref_now - dt).total_seconds()

    if age_seconds < -300.0:
        ahead_hours = abs(age_seconds) / 3600.0
        if ahead_hours >= 1.0:
            age_text = f"+{ahead_hours:.1f}h ahead (scheduled/forecast horizon)"
        else:
            age_text = f"+{abs(age_seconds) / 60.0:.0f}m ahead (scheduled/forecast horizon)"
        return FreshnessStatus(
            level="forecast_horizon",
            label="SCHEDULED / FORECAST HORIZON",
            age_Text=age_text,
            delta_seconds=age_seconds,
        )

    clamped_age = max(0.0, age_seconds)
    if clamped_age < 60.0:
        age_text = f"{clamped_age:.0f}s ago"
    elif clamped_age < 3600.0:
        age_text = f"{clamped_age / 60.0:.0f}m ago"
    else:
        age_text = f"{clamped_age / 3600.0:.1f}h ago"

    if clamped_age <= stale_threshold_seconds:
        return FreshnessStatus(
            level="fresh",
            label="FRESH",
            age_Text=age_text,
            delta_seconds=age_seconds,
        )

    return FreshnessStatus(
        level="stale",
        label="STALE",
        age_Text=age_text,
        delta_seconds=age_seconds,
    )


def format_alignment_badge(
    timestamps_aligned: bool,
    timestamp_delta_seconds: float | None,
) -> str:
    """Format an explicit status message describing timestamp alignment between streams."""
    if timestamp_delta_seconds is None:
        return "UNALIGNED (One or both streams unavailable)"
    if timestamps_aligned or abs(timestamp_delta_seconds) < 1e-6:
        return "ALIGNED (0s delta — exact UTC match)"
    delta_hours = timestamp_delta_seconds / 3600.0
    if abs(delta_hours) >= 1.0:
        return f"SEPARATE TIMESTAMPS (Δ = {delta_hours:+.1f}h — not merged)"
    return f"SEPARATE TIMESTAMPS (Δ = {timestamp_delta_seconds:+.0f}s — not merged)"


def has_generation_or_renewable_share_data(summary_payload: dict[str, Any] | None) -> bool:
    """Return True only if actual generation mix or renewable-share records exist in the payload.

    Prevents rendering fabricated generation or renewable-share charts when only
    electricity spot prices and weather forecasts have been ingested.
    """
    if not summary_payload or not isinstance(summary_payload, dict):
        return False
    gen_series = summary_payload.get("generation_series")
    ren_series = summary_payload.get("renewable_share_series")
    return bool(
        (isinstance(gen_series, list) and len(gen_series) > 0)
        or (isinstance(ren_series, list) and len(ren_series) > 0)
    )
