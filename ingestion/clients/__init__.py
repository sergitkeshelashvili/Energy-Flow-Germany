"""HTTP client wrappers for external public APIs."""

from ingestion.clients.base import (
    APIHTTPError,
    APIMalformedJSONError,
    APIRateLimitError,
    APISchemaValidationError,
    APITimeoutError,
    BaseAPIClient,
    IngestionClientError,
    compute_backoff_delay,
    parse_retry_after,
)
from ingestion.clients.energy_charts import EnergyChartsClient
from ingestion.clients.open_meteo import DEFAULT_HOURLY_VARIABLES, OpenMeteoClient

__all__ = [
    "APIHTTPError",
    "APIMalformedJSONError",
    "APIRateLimitError",
    "APISchemaValidationError",
    "APITimeoutError",
    "BaseAPIClient",
    "DEFAULT_HOURLY_VARIABLES",
    "EnergyChartsClient",
    "IngestionClientError",
    "OpenMeteoClient",
    "compute_backoff_delay",
    "parse_retry_after",
]
