"""Plotly chart builders styled for the dark SCADA control-room interface."""

import statistics
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from dashboard.formatters import parse_iso_utc

CITY_COLORS = {
    "DE-DUS": "#FF4B4B",
    "DE-BER": "#36CFC9",
    "DE-HAM": "#F7C948",
    "DE-MUC": "#A78BFA",
    "DE-FRA": "#6EA8FE",
}

SCADA_PLOT_LAYOUT: dict[str, Any] = {
    "template": "plotly_dark",
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": "Inter, ui-sans-serif, system-ui, sans-serif", "color": "#E8EAF0", "size": 12},
    "margin": {"l": 62, "r": 28, "t": 62, "b": 48},
    "hovermode": "x unified",
    "hoverlabel": {"bgcolor": "#1B202B", "bordercolor": "#3A4250", "font": {"color": "#F5F6FA"}},
}

GRID_COLOR = "rgba(181, 191, 207, 0.12)"


def build_electricity_price_chart(
    electricity_series: list[dict[str, Any]],
    unit: str = "EUR / MWh",
) -> go.Figure:
    """Build an interactive historical day-ahead electricity spot price chart."""
    timestamps = [parse_iso_utc(pt.get("timestamp")) for pt in electricity_series]
    prices = [float(pt["price"]) for pt in electricity_series]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=prices,
            mode="lines+markers",
            name=f"Day-Ahead Spot Price ({unit})",
            line={"color": "#FF4B4B", "width": 3.0},
            fill="tozeroy",
            fillcolor="rgba(255, 75, 75, 0.12)",
            hovertemplate=f"%{{x|%Y-%m-%d %H:%M UTC}}<br>Price: <b>%{{y:.2f}} {unit}</b><extra></extra>",
        )
    )

    if prices:
        mean_price = statistics.fmean(prices)
        fig.add_hline(
            y=mean_price,
            line_dash="dash",
            line_width=1.4,
            line_color="#F7C948",
            annotation_text=f"Mean {mean_price:.1f}",
            annotation_position="top left",
            annotation_font_color="#F7C948",
        )

    # Zero-price reference threshold
    fig.add_hline(
        y=0.0,
        line_dash="dot",
        line_color="#9AA3B2",
        annotation_text="0.00 EUR/MWh Threshold",
        annotation_position="bottom right",
    )

    fig.update_layout(
        **SCADA_PLOT_LAYOUT,
        title={"text": f"German Day-Ahead Electricity Spot Prices ({unit}) — UTC", "x": 0.01},
        xaxis={
            "title": "Timestamp (UTC)",
            "gridcolor": GRID_COLOR,
            "zeroline": False,
        },
        yaxis={
            "title": f"Spot Price ({unit})",
            "gridcolor": GRID_COLOR,
            "zeroline": False,
        },
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1.0,
            "font": {"color": "#C2C8D2"},
        },
        height=410,
    )
    return fig


def build_weather_forecast_chart(
    weather_series: list[dict[str, Any]],
    *,
    temp_unit: str = "°C",
    wind_unit: str = "km/h",
    rad_unit: str = "W/m²",
) -> go.Figure:
    """Build three clear, independently scaled weather forecast panels."""
    timestamps = [parse_iso_utc(pt.get("timestamp")) for pt in weather_series]
    temps = [float(pt["temperature_2m"]) for pt in weather_series]
    winds = [float(pt["wind_speed_10m"]) for pt in weather_series]
    rads = [float(pt["shortwave_radiation"]) for pt in weather_series]

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            f"Air Temperature · {temp_unit}",
            f"Wind Speed at 10 m · {wind_unit}",
            f"Shortwave Radiation · {rad_unit}",
        ),
    )

    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=temps,
            mode="lines",
            name=f"Temperature ({temp_unit})",
            line={"color": "#FF4B4B", "width": 2.8},
            hovertemplate="%{x|%a %d %b, %H:%M UTC}<br><b>%{y:.1f} °C</b><extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=winds,
            mode="lines",
            name=f"Wind speed ({wind_unit})",
            line={"color": "#36CFC9", "width": 2.8},
            hovertemplate="%{x|%a %d %b, %H:%M UTC}<br><b>%{y:.1f} km/h</b><extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=rads,
            mode="lines",
            name=f"Solar radiation ({rad_unit})",
            line={"color": "#F7C948", "width": 2.6},
            fill="tozeroy",
            fillcolor="rgba(247, 201, 72, 0.10)",
            hovertemplate="%{x|%a %d %b, %H:%M UTC}<br><b>%{y:.0f} W/m²</b><extra></extra>",
        ),
        row=3,
        col=1,
    )

    fig.update_layout(
        **SCADA_PLOT_LAYOUT,
        height=650,
        showlegend=False,
    )
    fig.update_xaxes(gridcolor=GRID_COLOR, zeroline=False)
    fig.update_xaxes(title_text="Forecast Time (UTC)", gridcolor=GRID_COLOR, row=3, col=1)
    fig.update_yaxes(title_text=temp_unit, gridcolor=GRID_COLOR, zeroline=False, row=1, col=1)
    fig.update_yaxes(title_text=wind_unit, gridcolor=GRID_COLOR, zeroline=False, row=2, col=1)
    fig.update_yaxes(title_text=rad_unit, gridcolor=GRID_COLOR, zeroline=False, row=3, col=1)
    return fig


def build_weather_comparison_chart(
    forecasts_by_city: dict[str, dict[str, Any]],
) -> go.Figure:
    """Compare temperature, wind, and solar-radiation forecasts across cities."""
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            "Forecast Temperature (°C)",
            "Forecast Wind Speed (km/h)",
            "Forecast Shortwave Radiation (W/m²)",
        ),
    )
    fallback_colors = ["#FF4B4B", "#36CFC9", "#F7C948", "#A78BFA", "#6EA8FE"]
    variables = (
        ("temperature_2m", 1),
        ("wind_speed_10m", 2),
        ("shortwave_radiation", 3),
    )

    for index, (location_id, forecast) in enumerate(forecasts_by_city.items()):
        series = forecast.get("series", [])
        if not series:
            continue
        timestamps = [parse_iso_utc(point.get("timestamp")) for point in series]
        color = CITY_COLORS.get(location_id, fallback_colors[index % len(fallback_colors)])
        city_name = forecast.get("location_name", location_id)
        for variable, row in variables:
            fig.add_trace(
                go.Scatter(
                    x=timestamps,
                    y=[float(point[variable]) for point in series],
                    mode="lines",
                    name=city_name,
                    legendgroup=location_id,
                    showlegend=row == 1,
                    line={"color": color, "width": 2.6},
                    hovertemplate=(
                        f"<b>{city_name}</b><br>%{{x|%a %d %b, %H:%M UTC}}<br>"
                        "%{y:.1f}<extra></extra>"
                    ),
                ),
                row=row,
                col=1,
            )

    fig.update_layout(
        **SCADA_PLOT_LAYOUT,
        title={"text": "City Forecasts · Shared UTC Timeline", "x": 0.01},
        height=690,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.03,
            "xanchor": "left",
            "x": 0.0,
            "font": {"color": "#C2C8D2"},
        },
        legend_groupclick="togglegroup",
    )
    fig.update_xaxes(gridcolor=GRID_COLOR, zeroline=False)
    fig.update_xaxes(title_text="Forecast Time (UTC)", row=3, col=1)
    fig.update_yaxes(title_text="°C", gridcolor=GRID_COLOR, zeroline=False, row=1, col=1)
    fig.update_yaxes(title_text="km/h", gridcolor=GRID_COLOR, zeroline=False, row=2, col=1)
    fig.update_yaxes(title_text="W/m²", gridcolor=GRID_COLOR, zeroline=False, row=3, col=1)
    return fig


def build_forecast_6h_chart(
    observed_series: list[dict[str, Any]],
    forecast_points: list[dict[str, Any]],
    unit: str = "EUR / MWh",
) -> go.Figure:
    """Build a chart clearly distinguishing observed spot prices from 6-hour baseline predictions."""
    fig = go.Figure()

    obs_timestamps = [parse_iso_utc(pt.get("timestamp")) for pt in observed_series]
    obs_prices = [float(pt["price"]) for pt in observed_series]

    fc_timestamps = [parse_iso_utc(pt.get("target_timestamp")) for pt in forecast_points]
    fc_prices = [float(pt["forecast_price"]) for pt in forecast_points]

    if obs_timestamps:
        fig.add_trace(
            go.Scatter(
                x=obs_timestamps,
                y=obs_prices,
                mode="lines+markers",
                name=f"Observed Spot Price ({unit})",
                line={"color": "#FF4B4B", "width": 3.0, "dash": "solid"},
                fill="tozeroy",
                fillcolor="rgba(255, 75, 75, 0.11)",
                hovertemplate=(
                    f"<b>OBSERVED</b><br>%{{x|%a %d %b, %H:%M UTC}}<br>"
                    f"Spot Price: <b>%{{y:.2f}} {unit}</b><extra></extra>"
                ),
            )
        )

    if fc_timestamps:
        # Connect dashed forecast trace visually from the last observed point if available
        plot_fc_x = ([obs_timestamps[-1]] + fc_timestamps) if obs_timestamps else fc_timestamps
        plot_fc_y = ([obs_prices[-1]] + fc_prices) if obs_prices else fc_prices

        fig.add_trace(
            go.Scatter(
                x=plot_fc_x,
                y=plot_fc_y,
                mode="lines+markers",
                name=f"6h Deterministic Baseline Forecast ({unit})",
                line={"color": "#F7C948", "width": 2.8, "dash": "dash"},
                marker={"size": 7, "symbol": "diamond", "color": "#F7C948"},
                hovertemplate=(
                    f"<b>BASELINE FORECAST</b><br>%{{x|%a %d %b, %H:%M UTC}}<br>"
                    f"Projected Price: <b>%{{y:.2f}} {unit}</b><extra></extra>"
                ),
            )
        )

    if obs_timestamps and obs_timestamps[-1] is not None:
        origin_ts = obs_timestamps[-1]
        fig.add_vline(
            x=origin_ts,
            line_dash="dot",
            line_color="#A6AEBB",
        )

    fig.update_layout(
        **SCADA_PLOT_LAYOUT,
        title={
            "text": f"Observed Spot Prices vs. 6-Hour Deterministic Baseline Forecast ({unit})",
            "x": 0.01,
        },
        xaxis={
            "title": "Timestamp (UTC)",
            "gridcolor": GRID_COLOR,
        },
        yaxis={
            "title": f"Price ({unit})",
            "gridcolor": GRID_COLOR,
        },
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1.0,
            "font": {"color": "#C2C8D2"},
        },
        height=430,
    )
    return fig
