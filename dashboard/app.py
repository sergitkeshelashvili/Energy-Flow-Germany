"""Streamlit SCADA control-room dashboard for EnergyFlowGermany.

All data is fetched exclusively through the FastAPI backend (`dashboard/api_client.py`).
This module never connects directly to MongoDB.
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dashboard.api_client import (
    APIResult,
    DashboardAPIClient,
    get_default_api_base_url,
    get_default_http_timeout,
)
from dashboard.components.charts import (
    build_electricity_price_chart,
    build_forecast_6h_chart,
    build_weather_comparison_chart,
    build_weather_forecast_chart,
)
from dashboard.components.styles import (
    SCADA_CSS,
    render_badge_html,
    render_freshness_badge_html,
    render_telemetry_card_html,
)
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

WEATHER_CITY_LABELS = {
    "DE-DUS": "Düsseldorf",
    "DE-BER": "Berlin",
    "DE-HAM": "Hamburg",
    "DE-MUC": "Munich",
    "DE-FRA": "Frankfurt",
}


@st.cache_data(ttl=60, show_spinner=False)
def cached_fetch_bundle(
    base_url: str,
    timeout_seconds: float,
    bidding_zone: str,
    location_ids: tuple[str, ...],
    analytics_limit: int,
    forecast_history_window: int,
) -> dict[str, Any]:
    """Fetch all dashboard telemetry endpoints from FastAPI with a 60-second cache TTL."""
    with DashboardAPIClient(base_url=base_url, timeout_seconds=timeout_seconds) as client:
        selected_ids = location_ids or ("DE-DUS",)
        analytics_by_location = {
            location_id: client.fetch_analytics_summary(
                bidding_zone=bidding_zone,
                location_id=location_id,
                limit=analytics_limit,
            )
            for location_id in selected_ids
        }
        primary_location_id = selected_ids[0]
        weather_comparison: dict[str, dict[str, Any]] = {}
        weather_comparison_errors: dict[str, str] = {}
        for location_id, result in analytics_by_location.items():
            if result.ok and result.data is not None:
                weather = result.data.get("weather", {})
                weather_comparison[location_id] = {
                    "location_name": weather.get(
                        "location_name",
                        WEATHER_CITY_LABELS.get(location_id, location_id),
                    ),
                    "series": result.data.get("weather_series", []),
                }
            else:
                weather_comparison_errors[location_id] = result.error or "No forecast data available."

        return {
            "health": client.fetch_health(),
            "telemetry": client.fetch_latest_telemetry(
                bidding_zone=bidding_zone,
                location_id=primary_location_id,
            ),
            "analytics": analytics_by_location[primary_location_id],
            "weather_comparison": weather_comparison,
            "weather_comparison_errors": weather_comparison_errors,
            "forecast": client.fetch_6h_forecast(
                bidding_zone=bidding_zone,
                history_window=forecast_history_window,
            ),
            "dlq": client.fetch_dlq_stats(),
        }


def _render_top_header(
    health_result: APIResult,
    last_successful_update: datetime | None,
    selected_location_ids: tuple[str, ...],
) -> None:
    """Render the SCADA control-room header bar with live connectivity and update status."""
    health_data = health_result.data or {}
    api_up = health_data.get("api_status") == "up" or health_result.ok
    mongo_status = health_data.get("mongodb_status", "disconnected" if not health_result.ok else "connected")

    api_badge = render_badge_html(
        f"FASTAPI: {'ONLINE' if api_up else 'OFFLINE'}",
        variant="ok" if api_up else "err",
    )
    mongo_badge = render_badge_html(
        f"MONGODB: {str(mongo_status).upper()}",
        variant="ok" if mongo_status == "connected" else "err",
    )
    last_update_str = format_utc_timestamp(last_successful_update, fallback="No successful update yet")
    city_names = ", ".join(
        WEATHER_CITY_LABELS.get(location_id, location_id)
        for location_id in selected_location_ids
    ) or "No cities selected"

    st.markdown(
        f"""
        <div class="scada-header">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                <div>
                    <div class="scada-title">ENERGYFLOW GERMANY // ENERGY MARKET &amp; WEATHER INTELLIGENCE</div>
                    <div class="scada-subtitle">
                        Bidding Zone: DE-LU (Day-Ahead Spot) &nbsp;|&nbsp; Weather Cities: {city_names} &nbsp;|&nbsp; Last Successful Update: {last_update_str}
                    </div>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    {api_badge}
                    {mongo_badge}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_tab_live_monitor(
    telemetry_result: APIResult,
    weather_comparison: dict[str, dict[str, Any]],
) -> None:
    """Render TAB 1: LIVE MONITOR."""
    if not telemetry_result.ok or telemetry_result.data is None:
        st.error(
            f"Unable to load latest telemetry from FastAPI: {telemetry_result.error or 'Unknown error'}"
        )
        return

    payload = telemetry_result.data
    elec: dict[str, Any] | None = payload.get("electricity_price")
    weather: dict[str, Any] | None = payload.get("weather_forecast")
    aligned = bool(payload.get("timestamps_aligned", False))
    delta_seconds = payload.get("timestamp_delta_seconds")
    note = payload.get("note", "")

    # Stream Timestamp Alignment Banner
    alignment_text = format_alignment_badge(aligned, delta_seconds)
    alignment_variant = "ok" if aligned else "info"
    st.markdown(
        f'<div class="scada-panel" style="padding:10px 14px;">'
        f'<span class="scada-panel-label" style="margin-right:10px;">STREAM TIMESTAMP ALIGNMENT:</span>'
        f"{render_badge_html(alignment_text, variant=alignment_variant)}"
        f'<span class="scada-meta-row" style="margin-left:12px;">{note}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )

    col_elec, col_weather = st.columns([1, 1])

    with col_elec:
        st.subheader("Day-Ahead Electricity Spot Price")
        if elec is None:
            st.warning(
                "No electricity spot price records are currently stored in MongoDB. "
                "Run the ingestion pipeline (`python -m ingestion.runner`) to populate data."
            )
        else:
            unit = elec.get("unit", "EUR / MWh")
            price_str = format_price_with_unit(elec.get("price"), unit=unit)
            freshness = evaluate_data_freshness(elec.get("timestamp"))
            badge_html = render_freshness_badge_html(freshness)
            meta_lines = [
                f"Bidding Zone      : {elec.get('bidding_zone', 'DE-LU')}",
                f"Market Timestamp  : {format_utc_timestamp(elec.get('timestamp'))}",
                f"Ingested At (UTC) : {format_utc_timestamp(elec.get('ingested_at'))}",
                f"Data Source       : {elec.get('source', 'energy_charts')} (Fraunhofer ISE Energy-Charts)",
                f"License / Origin  : {elec.get('license_info') or 'Bundesnetzagentur | SMARD.de (CC BY 4.0)'}",
            ]
            st.markdown(
                render_telemetry_card_html(
                    title="LATEST SPOT MARKET PRICE (OBSERVED AUCTION)",
                    primary_value=price_str,
                    badge_html=badge_html,
                    meta_lines=meta_lines,
                ),
                unsafe_allow_html=True,
            )

    outlook_cities = [
        (location_id, forecast)
        for location_id, forecast in weather_comparison.items()
        if forecast.get("series")
    ]
    if outlook_cities:
        st.divider()
        st.subheader("City Outlook · About 24 Hours Ahead")
        outlook_columns = st.columns(len(outlook_cities))
        for column, (location_id, forecast) in zip(outlook_columns, outlook_cities, strict=False):
            series = forecast["series"]
            point = series[min(24, len(series) - 1)]
            with column:
                st.metric(
                    WEATHER_CITY_LABELS.get(location_id, location_id),
                    f"{float(point['temperature_2m']):.1f} °C",
                    help=f"Forecast hour: {format_utc_timestamp(point.get('timestamp'))}",
                )
                st.caption(
                    f"Wind {float(point['wind_speed_10m']):.1f} km/h · "
                    f"Solar {float(point['shortwave_radiation']):.0f} W/m²"
                )

    with col_weather:
        st.subheader("Meteorological Forecast Readout")
        if weather is None:
            st.warning(
                "No weather forecast records are currently stored in MongoDB. "
                "Run the ingestion pipeline (`python -m ingestion.runner`) to populate data."
            )
        else:
            units = weather.get("units", {})
            temp_str = format_weather_metric(
                weather.get("temperature_2m"),
                units.get("temperature_2m", "°C"),
            )
            wind_str = format_weather_metric(
                weather.get("wind_speed_10m"),
                units.get("wind_speed_10m", "km/h"),
            )
            rad_str = format_weather_metric(
                weather.get("shortwave_radiation"),
                units.get("shortwave_radiation", "W/m²"),
            )
            freshness = evaluate_data_freshness(weather.get("timestamp"))
            forecast_pill = render_badge_html("FORECAST VALUE", variant="info")
            freshness_pill = render_freshness_badge_html(freshness)

            meta_lines = [
                f"Weather Location  : {weather.get('location_name', weather.get('location_id', 'Unknown'))} ({weather.get('location_id', 'Unknown')})",
                f"Coordinates       : {weather.get('latitude', 51.2277):.4f}°N, {weather.get('longitude', 6.7735):.4f}°E",
                f"Forecast Hour UTC : {format_utc_timestamp(weather.get('timestamp'))}",
                f"Source Wall Time  : {weather.get('source_timestamp', 'N/A')} ({weather.get('source_timezone', 'Europe/Berlin')})",
                f"Ingested At (UTC) : {format_utc_timestamp(weather.get('ingested_at'))}",
                f"Data Source       : {weather.get('source', 'open_meteo')} (Open-Meteo Forecast API)",
            ]
            st.markdown(
                render_telemetry_card_html(
                    title="WIND / SOLAR / TEMPERATURE FORECAST",
                    primary_value=f"{wind_str}  |  {rad_str}  |  {temp_str}",
                    badge_html=f"{forecast_pill} &nbsp; {freshness_pill}",
                    meta_lines=meta_lines,
                ),
                unsafe_allow_html=True,
            )


def _render_tab_energy_analytics(
    analytics_result: APIResult,
    weather_comparison: dict[str, dict[str, Any]],
    weather_comparison_errors: dict[str, str],
) -> None:
    """Render TAB 2: ENERGY ANALYTICS."""
    if not analytics_result.ok or analytics_result.data is None:
        st.error(
            f"Unable to load analytics summary from FastAPI: {analytics_result.error or 'Unknown error'}"
        )
        return

    data = analytics_result.data
    elec = data.get("electricity", {})
    weather = data.get("weather", {})
    corr = data.get("alignment_and_correlation", {})
    elec_series: list[dict[str, Any]] = data.get("electricity_series", [])
    weather_series: list[dict[str, Any]] = data.get("weather_series", [])

    if data.get("status") == "insufficient_data" and data.get("insufficient_data_reason"):
        st.info(f"Data Completeness Notice: {data['insufficient_data_reason']}")

    # 1. Electricity Price Statistics & Chart
    st.subheader("Historical Day-Ahead Electricity Spot Price Analytics")
    elec_unit = elec.get("unit", "EUR / MWh")
    elec_count = int(elec.get("record_count", 0))
    elec_range_str = format_time_range(elec.get("time_range"))

    st.caption(
        f"**Bidding Zone:** `{elec.get('bidding_zone', 'DE-LU')}` &nbsp;|&nbsp; "
        f"**Stored Records Analyzed:** `{elec_count}` &nbsp;|&nbsp; "
        f"**UTC Time Range:** `{elec_range_str}` &nbsp;|&nbsp; "
        f"**Unit:** `{elec_unit}`"
    )

    if elec_count == 0 or not elec_series:
        st.warning("No stored electricity price records available for the selected window.")
    else:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Mean Spot Price", f"{float(elec['mean_price']):.2f}")
        m2.metric("Median Spot Price", f"{float(elec['median_price']):.2f}")
        m3.metric("Min Spot Price", f"{float(elec['min_price']):.2f}")
        m4.metric("Max Spot Price", f"{float(elec['max_price']):.2f}")
        m5.metric("Negative Price Hours", f"{int(elec.get('negative_price_hours', 0))} h")
        st.caption(f"All displayed spot prices are in {elec_unit}.")

        fig_price = build_electricity_price_chart(elec_series, unit=elec_unit)
        st.plotly_chart(fig_price, use_container_width=True)

    st.divider()

    # 2. Weather Forecast Analytics & Exact-Timestamp Correlations
    st.subheader("Meteorological Forecast Series & Price-Weather Correlation")
    w_count = int(weather.get("record_count", 0))
    w_range_str = format_time_range(weather.get("time_range"))
    st.caption(
        f"**Location:** `{weather.get('location_name', weather.get('location_id', 'Unknown'))} ({weather.get('location_id', 'Unknown')})` &nbsp;|&nbsp; "
        f"**Stored Forecast Records:** `{w_count}` &nbsp;|&nbsp; "
        f"**UTC Time Range:** `{w_range_str}`"
    )

    if w_count > 0 and weather_series:
        temp_stats = weather.get("temperature_2m") or {}
        wind_stats = weather.get("wind_speed_10m") or {}
        rad_stats = weather.get("shortwave_radiation") or {}

        w1, w2, w3 = st.columns(3)
        w1.metric(
            f"Mean Forecast Wind (10m)",
            format_weather_metric(wind_stats.get("mean_value"), wind_stats.get("unit", "km/h")),
            help=f"Range: {wind_stats.get('min_value')} to {wind_stats.get('max_value')} {wind_stats.get('unit', 'km/h')}",
        )
        w2.metric(
            f"Mean Forecast Solar Radiation",
            format_weather_metric(rad_stats.get("mean_value"), rad_stats.get("unit", "W/m²")),
            help=f"Range: {rad_stats.get('min_value')} to {rad_stats.get('max_value')} {rad_stats.get('unit', 'W/m²')}",
        )
        w3.metric(
            f"Mean Forecast Temperature (2m)",
            format_weather_metric(temp_stats.get("mean_value"), temp_stats.get("unit", "°C")),
            help=f"Range: {temp_stats.get('min_value')} to {temp_stats.get('max_value')} {temp_stats.get('unit', '°C')}",
        )

        fig_weather = build_weather_forecast_chart(
            weather_series,
            temp_unit=temp_stats.get("unit", "°C"),
            wind_unit=wind_stats.get("unit", "km/h"),
            rad_unit=rad_stats.get("unit", "W/m²"),
        )
        st.plotly_chart(fig_weather, use_container_width=True)
    else:
        st.warning("No stored weather forecast records available for the selected window.")

    st.subheader("Weather Forecast Comparison by City")
    available_forecasts = {
        location_id: city
        for location_id, city in weather_comparison.items()
        if city["series"]
    }
    if available_forecasts:
        st.caption(
            "Hourly Open-Meteo forecasts for the selected locations. "
            "Electricity spot prices remain shared across the DE-LU bidding zone."
        )
        st.plotly_chart(
            build_weather_comparison_chart(available_forecasts),
            use_container_width=True,
        )
    else:
        st.info("No forecast series are available for the selected cities yet.")
    if weather_comparison_errors:
        unavailable_cities = ", ".join(
            WEATHER_CITY_LABELS.get(location_id, location_id)
            for location_id in weather_comparison_errors
        )
        st.caption(
            f"Forecasts are not available yet for: {unavailable_cities}. "
            "The ingestion worker will fetch them on its next cycle."
        )

    # Aligned Pearson Correlations
    aligned_count = int(corr.get("aligned_record_count", 0))
    st.markdown(
        f"**Exact-Timestamp Price vs. Weather Pearson Correlations** "
        f"(Aligned UTC Intervals: `{aligned_count}`, Minimum Required: `{corr.get('minimum_required_records', 3)}`)"
    )
    if corr.get("status") == "ok":
        c1, c2, c3 = st.columns(3)
        c1.metric(
            "r(Price, Wind Speed 10m)",
            format_correlation(corr.get("price_vs_wind_speed_10m_corr")),
        )
        c2.metric(
            "r(Price, Shortwave Radiation)",
            format_correlation(corr.get("price_vs_shortwave_radiation_corr")),
        )
        c3.metric(
            "r(Price, Temperature 2m)",
            format_correlation(corr.get("price_vs_temperature_2m_corr")),
        )
    else:
        st.info(corr.get("note") or "Insufficient exact-timestamp overlap for correlation calculation.")

    st.divider()

    # 3. Generation & Renewable-Share Charts (Only if Ingested)
    st.subheader("Generation Mix & Renewable Share")
    if has_generation_or_renewable_share_data(data):
        st.write("Generation and renewable-share series available.")
    else:
        st.info(
            "Generation mix and renewable-share charts are intentionally omitted: the current "
            "ingestion pipeline collects Day-Ahead Spot Prices (`/price`) and Open-Meteo Weather "
            "Forecasts (`/v1/forecast`). No synthetic or placeholder generation data is displayed."
        )


def _render_tab_forecast_and_quality(
    forecast_result: APIResult,
    analytics_result: APIResult,
    dlq_result: APIResult,
) -> None:
    """Render TAB 3: FORECAST & DATA QUALITY."""
    st.subheader("6-Hour Deterministic Baseline Electricity Price Forecast")

    if not forecast_result.ok or forecast_result.data is None:
        st.error(
            f"Unable to load 6-hour baseline forecast from FastAPI: {forecast_result.error or 'Unknown error'}"
        )
    else:
        fc_data = forecast_result.data
        unit = fc_data.get("unit", "EUR / MWh")
        fc_points: list[dict[str, Any]] = fc_data.get("forecast_points", [])
        obs_series: list[dict[str, Any]] = (
            (analytics_result.data or {}).get("electricity_series", [])
            if analytics_result.ok
            else []
        )

        # Method & Non-ML disclosure badge
        st.markdown(
            f"{render_badge_html('DETERMINISTIC STATISTICAL BASELINE (NON-ML)', variant='info')} &nbsp; "
            f"**Method ID:** `{fc_data.get('method')}` &nbsp;|&nbsp; "
            f"**History Records Used:** `{fc_data.get('history_records_used', 0)}` "
            f"(Min required: `{fc_data.get('minimum_history_required', 3)}`)",
            unsafe_allow_html=True,
        )

        if fc_data.get("status") != "ok" or not fc_points:
            st.warning(
                fc_data.get("insufficient_data_reason")
                or "Insufficient historical electricity price records to compute the 6-hour baseline forecast."
            )
        else:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(
                "Last Observed Price",
                format_price_with_unit(fc_data.get("reference_last_price"), unit),
            )
            k2.metric(
                "Trailing Mean Price",
                format_price_with_unit(fc_data.get("trailing_mean_price"), unit),
            )
            k3.metric(
                "Hourly Drift Estimate",
                format_price_with_unit(fc_data.get("hourly_drift_estimate"), f"{unit}/h"),
            )
            k4.metric(
                "+6h Baseline Projection",
                format_price_with_unit(fc_points[-1].get("forecast_price"), unit),
            )

            # Plot recent observed prices (up to last 24 points) alongside the 6h forecast
            fig_fc = build_forecast_6h_chart(
                observed_series=obs_series[-24:],
                forecast_points=fc_points,
                unit=unit,
            )
            st.plotly_chart(fig_fc, use_container_width=True)

            # Horizon Table
            table_rows = [
                {
                    "Horizon": f"+{pt['step_horizon_hours']}h",
                    "Target Timestamp (UTC)": format_utc_timestamp(pt.get("target_timestamp")),
                    f"Projected Price ({unit})": f"{float(pt['forecast_price']):.2f}",
                    "Persistence Weight (0.5^h)": f"{float(pt['persistence_weight']):.4f}",
                    "Mean-Reversion Weight (1 - 0.5^h)": f"{float(pt['mean_reversion_weight']):.4f}",
                }
                for pt in fc_points
            ]
            st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

        # Methodology & Limitations Box
        with st.expander("Forecast Methodology & Documented Limitations", expanded=True):
            st.markdown(f"**Method Description:** {fc_data.get('method_description', '')}")
            st.markdown("**Documented Limitations:**")
            for limitation in fc_data.get("limitations", []):
                st.markdown(f"- {limitation}")

    st.divider()

    # Dead-Letter Queue (DLQ) & Data Quality Section
    st.subheader("Data Quality & Dead-Letter Queue (DLQ) Diagnostics")
    if not dlq_result.ok or dlq_result.data is None:
        st.error(
            f"Unable to load DLQ diagnostics from FastAPI: {dlq_result.error or 'Unknown error'}"
        )
        return

    dlq_data = dlq_result.data
    total_dlq = int(dlq_data.get("total_records", 0))
    unresolved_dlq = int(dlq_data.get("unresolved_count", 0))
    resolved_dlq = int(dlq_data.get("resolved_count", 0))
    latest_failed = format_utc_timestamp(dlq_data.get("latest_failed_at"), fallback="None")

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Total DLQ Records", str(total_dlq))
    d2.metric("Unresolved DLQ Records", str(unresolved_dlq))
    d3.metric("Resolved DLQ Records", str(resolved_dlq))
    d4.metric("Latest Failure (UTC)", latest_failed)

    categories: list[dict[str, Any]] = dlq_data.get("categories", [])
    if not categories:
        st.success("Zero dead-letter queue records logged. All ingested records passed validation.")
    else:
        cat_rows = [
            {
                "Source": item.get("source", "unknown"),
                "Pipeline Stage": item.get("stage", "unknown"),
                "Sanitized Error Category": item.get("error_category", "unknown"),
                "Record Count": int(item.get("count", 0)),
                "Latest Failure (UTC)": format_utc_timestamp(item.get("latest_failed_at")),
            }
            for item in categories
        ]
        st.dataframe(pd.DataFrame(cat_rows), use_container_width=True, hide_index=True)
        st.caption(
            "Security Note: Raw payloads are excluded at the MongoDB projection layer in FastAPI "
            "(`GET /admin/dlq-stats`) so sensitive upstream payloads are never exposed."
        )


def main() -> None:
    """Main Streamlit entrypoint for EnergyFlowGermany."""
    st.set_page_config(
        page_title="EnergyFlowGermany — Energy Market & Weather Intelligence",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(SCADA_CSS, unsafe_allow_html=True)

    # Sidebar Controls & Configuration
    with st.sidebar:
        st.markdown("### ENERGY FLOW GERMANY")
        st.caption("REAL-TIME ENERGY DATA & INTELLIGENCE")
        api_base_url = st.text_input(
            "FastAPI Base URL",
            value=get_default_api_base_url(),
            help="All dashboard queries go exclusively through this FastAPI URL.",
        )
        http_timeout = st.number_input(
            "HTTP Timeout (seconds)",
            min_value=1.0,
            max_value=60.0,
            value=float(get_default_http_timeout()),
            step=1.0,
        )
        bidding_zone = st.text_input("Bidding Zone", value="DE-LU")
        selected_location_ids = tuple(
            st.multiselect(
                "Weather Cities",
                options=list(WEATHER_CITY_LABELS),
                default=list(WEATHER_CITY_LABELS),
                format_func=lambda location_id: WEATHER_CITY_LABELS[location_id],
                help="Choose one city or several to compare their hourly forecasts.",
            )
        )
        analytics_limit = st.slider(
            "Analytics Window (Records)",
            min_value=12,
            max_value=336,
            value=168,
            step=12,
        )
        forecast_history_window = st.slider(
            "Forecast Trailing Window (Hours)",
            min_value=3,
            max_value=72,
            value=24,
            step=3,
        )

        if st.button("Refresh Telemetry Now", use_container_width=True):
            st.cache_data.clear()

    query_location_ids = selected_location_ids or ("DE-DUS",)
    if not selected_location_ids:
        st.sidebar.caption("No city selected; showing Düsseldorf until you choose a city.")

    bundle = cached_fetch_bundle(
        base_url=api_base_url,
        timeout_seconds=float(http_timeout),
        bidding_zone=bidding_zone,
        location_ids=query_location_ids,
        analytics_limit=int(analytics_limit),
        forecast_history_window=int(forecast_history_window),
    )

    successful_results = [
        result
        for result in bundle.values()
        if isinstance(result, APIResult) and result.ok
    ]
    if successful_results:
        latest_fetch = max(result.fetched_at for result in successful_results)
        st.session_state["last_successful_update_utc"] = latest_fetch

    last_successful_update: datetime | None = st.session_state.get("last_successful_update_utc")

    _render_top_header(bundle["health"], last_successful_update, query_location_ids)

    tab_live, tab_analytics, tab_forecast = st.tabs(
        [
            "TAB 1: LIVE MONITOR",
            "TAB 2: ENERGY ANALYTICS",
            "TAB 3: FORECAST & DATA QUALITY",
        ]
    )

    with tab_live:
        _render_tab_live_monitor(bundle["telemetry"], bundle["weather_comparison"])

    with tab_analytics:
        _render_tab_energy_analytics(
            bundle["analytics"],
            bundle["weather_comparison"],
            bundle["weather_comparison_errors"],
        )

    with tab_forecast:
        _render_tab_forecast_and_quality(
            forecast_result=bundle["forecast"],
            analytics_result=bundle["analytics"],
            dlq_result=bundle["dlq"],
        )

    # Global Source Attribution Footer
    st.markdown(
        """
        <div class="scada-footer">
            <strong>Data Attribution &amp; Architecture Notice:</strong>
            German Day-Ahead Electricity Spot Prices provided by <strong>Fraunhofer ISE Energy-Charts API</strong>
            (primary source: Bundesnetzagentur | SMARD.de, licensed under CC BY 4.0).
            Hourly meteorological forecasts for selected German cities provided by <strong>Open-Meteo Weather Forecast API</strong>.
            The Streamlit frontend accesses data exclusively via the <strong>EnergyFlowGermany FastAPI service</strong>.
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
