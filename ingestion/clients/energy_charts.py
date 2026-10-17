"""Client for fetching German Day-Ahead spot electricity prices from Fraunhofer ISE Energy-Charts API."""

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
from ingestion.schemas.electricity import (
    ElectricityPriceRecord,
    EnergyChartsPriceResponse,
    transform_energy_charts_response,
)

logger = get_logger(__name__)


class EnergyChartsClient(BaseAPIClient):
    """HTTP client for the public Fraunhofer ISE Energy-Charts `/price` endpoint."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.energy-charts.info",
        bidding_zone: str = "DE-LU",
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        super().__init__(
            source="energy_charts",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
            http_client=http_client,
            sleep_fn=sleep_fn,
            jitter_fn=jitter_fn,
        )
        self.bidding_zone = bidding_zone

    def fetch_raw_prices(
        self,
        *,
        bidding_zone: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> EnergyChartsPriceResponse:
        """Fetch and validate the raw Energy-Charts `/price` JSON payload."""
        zone = bidding_zone or self.bidding_zone
        params: dict[str, Any] = {"bzn": zone}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end

        logger.info("Fetching Energy-Charts spot prices for bidding_zone=%s", zone)
        payload = self._get_json("/price", params=params, scope=zone)

        try:
            validated = EnergyChartsPriceResponse.model_validate(payload)
        except ValidationError as exc:
            raise APISchemaValidationError(
                f"Energy-Charts response failed schema/alignment validation: {exc}",
                source="energy_charts",
                scope=zone,
                stage="payload_validation",
                raw_payload=payload,
            ) from exc

        logger.info(
            "Validated Energy-Charts payload for bidding_zone=%s (%d aligned intervals, unit=%s)",
            zone,
            len(validated.unix_seconds),
            validated.unit,
        )
        return validated

    def fetch_price_records(
        self,
        *,
        bidding_zone: str | None = None,
        start: str | None = None,
        end: str | None = None,
        ingested_at: datetime | None = None,
    ) -> BatchTransformResult[ElectricityPriceRecord]:
        """Fetch Energy-Charts prices and transform into individual validated UTC records."""
        zone = bidding_zone or self.bidding_zone
        raw_response = self.fetch_raw_prices(bidding_zone=zone, start=start, end=end)
        result = transform_energy_charts_response(
            raw_response,
            bidding_zone=zone,
            ingested_at=ingested_at,
        )
        logger.info(
            "Transformed Energy-Charts batch for %s: %d valid records, %d DLQ records",
            zone,
            len(result.valid_records),
            len(result.dead_letters),
        )
        return result
