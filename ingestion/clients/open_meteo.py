"""Client for fetching German meteorological forecast data from the public Open-Meteo API."""

from collections.abc import Callable
from datetime import datetime
import random
import time
from typing import Any

import httpx
from pydantic import ValidationError

from common.logging import get_logger
from ingestion.clients.base import APISchemaValidationError, BaseAPIClient
from ingestion.schemas.common import BatchTransformResult
from ingestion.schemas.weather import (
    OpenMeteoForecastResponse,
    WeatherForecastRecord,
    transform_open_meteo_response,
)

logger = get_logger(__name__)

DEFAULT_HOURLY_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "wind_speed_10m",
    "shortwave_radiation",
)


class OpenMeteoClient(BaseAPIClient):
    """HTTP client for the public Open-Meteo `/v1/forecast` endpoint.

    Defaults to Düsseldorf, Germany (`latitude=51.2277`, `longitude=6.7735`) and
    explicitly requests `timezone=Europe/Berlin`.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.open-meteo.com/v1/forecast",
        location_id: str = "DE-DUS",
        location_name: str = "Düsseldorf, Germany",
        latitude: float = 51.2277,
        longitude: float = 6.7735,
        timezone: str = "Europe/Berlin",
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url.endswith("/forecast"):
            normalized_base_url = normalized_base_url[: -len("/forecast")]
        super().__init__(
            source="open_meteo",
            base_url=normalized_base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
            http_client=http_client,
            sleep_fn=sleep_fn,
            jitter_fn=jitter_fn,
        )
        self.location_id = location_id
        self.location_name = location_name
        self.latitude = latitude
        self.longitude = longitude
        self.timezone = timezone

    def fetch_raw_forecast(
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        timezone: str | None = None,
        location_id: str | None = None,
    ) -> OpenMeteoForecastResponse:
        """Fetch and validate the raw Open-Meteo `/forecast` JSON payload."""
        lat = self.latitude if latitude is None else latitude
        lon = self.longitude if longitude is None else longitude
        tz = timezone or self.timezone
        loc_id = location_id or self.location_id

        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(DEFAULT_HOURLY_VARIABLES),
            "timezone": tz,
        }

        logger.info(
            "Fetching Open-Meteo forecast for location_id=%s (lat=%.4f, lon=%.4f, tz=%s)",
            loc_id,
            lat,
            lon,
            tz,
        )
        payload = self._get_json("/forecast", params=params, scope=loc_id)

        try:
            validated = OpenMeteoForecastResponse.model_validate(payload)
        except ValidationError as exc:
            raise APISchemaValidationError(
                f"Open-Meteo response failed schema/alignment validation: {exc}",
                source="open_meteo",
                scope=loc_id,
                stage="payload_validation",
                raw_payload=payload,
            ) from exc

        logger.info(
            "Validated Open-Meteo forecast payload for %s (%d aligned hours, timezone=%s)",
            loc_id,
            len(validated.hourly.time),
            validated.timezone,
        )
        return validated

    def fetch_forecast_records(
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        timezone: str | None = None,
        location_id: str | None = None,
        location_name: str | None = None,
        ingested_at: datetime | None = None,
    ) -> BatchTransformResult[WeatherForecastRecord]:
        """Fetch Open-Meteo hourly forecasts and transform into validated UTC records."""
        loc_id = location_id or self.location_id
        loc_name = location_name or self.location_name
        raw_response = self.fetch_raw_forecast(
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            location_id=loc_id,
        )
        result = transform_open_meteo_response(
            raw_response,
            location_id=loc_id,
            location_name=loc_name,
            ingested_at=ingested_at,
        )
        logger.info(
            "Transformed Open-Meteo batch for %s: %d valid forecast records, %d DLQ records",
            loc_id,
            len(result.valid_records),
            len(result.dead_letters),
        )
        return result
