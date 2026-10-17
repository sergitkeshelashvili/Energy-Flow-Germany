"""HTTP client wrapper for fetching analytical data exclusively from the FastAPI service.

The Streamlit dashboard must never connect directly to MongoDB. All data access is
performed over HTTP against the FastAPI endpoints with explicit timeouts and structured
error handling.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from typing import Any

import httpx


def get_default_api_base_url() -> str:
    """Resolve the FastAPI base URL from environment variables (`API_BASE_URL` or `API_INTERNAL_URL`)."""
    raw = os.getenv("API_BASE_URL") or os.getenv("API_INTERNAL_URL") or "http://localhost:8000"
    return raw.strip().rstrip("/")


def get_default_http_timeout() -> float:
    """Resolve the HTTP timeout in seconds from environment variables."""
    raw = os.getenv("HTTP_TIMEOUT_SECONDS", "20.0").strip()
    try:
        val = float(raw)
        return val if val > 0 else 20.0
    except ValueError:
        return 20.0


@dataclass(frozen=True)
class APIResult:
    """Result envelope returned by `DashboardAPIClient` calls."""

    ok: bool
    status_code: int | None
    data: dict[str, Any] | None
    error: str | None
    fetched_at: datetime


class DashboardAPIClient:
    """Synchronous HTTP client for querying the EnergyFlowGermany FastAPI backend."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or get_default_api_base_url()).rstrip("/")
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else get_default_http_timeout()
        )
        self._external_client = http_client is not None
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "EnergyFlowGermany-Dashboard/1.0"},
        )

    def close(self) -> None:
        """Close the underlying httpx.Client if owned by this instance."""
        if not self._external_client:
            self._client.close()

    def __enter__(self) -> "DashboardAPIClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        allow_503_json: bool = False,
    ) -> APIResult:
        """Execute a GET request against FastAPI and return a structured `APIResult`."""
        fetched_at = datetime.now(timezone.utc)
        url = f"{self.base_url}/{path.lstrip('/')}" if self._external_client else path

        try:
            response = self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            return APIResult(
                ok=False,
                status_code=None,
                data=None,
                error=f"API request timed out after {self.timeout_seconds:.1f}s ({path}): {exc}",
                fetched_at=fetched_at,
            )
        except httpx.RequestError as exc:
            return APIResult(
                ok=False,
                status_code=None,
                data=None,
                error=f"FastAPI service unreachable at {self.base_url}{path}: {exc}",
                fetched_at=fetched_at,
            )

        status_code = response.status_code
        parsed_json: dict[str, Any] | None = None
        try:
            body = response.json()
            if isinstance(body, dict):
                parsed_json = body
        except (json.JSONDecodeError, ValueError):
            parsed_json = None

        if 200 <= status_code < 300:
            if parsed_json is None:
                return APIResult(
                    ok=False,
                    status_code=status_code,
                    data=None,
                    error=f"Invalid JSON payload returned by {path} (HTTP {status_code}).",
                    fetched_at=fetched_at,
                )
            return APIResult(
                ok=True,
                status_code=status_code,
                data=parsed_json,
                error=None,
                fetched_at=fetched_at,
            )

        if status_code == 503 and allow_503_json and parsed_json is not None:
            return APIResult(
                ok=False,
                status_code=status_code,
                data=parsed_json,
                error="Service degraded (HTTP 503).",
                fetched_at=fetched_at,
            )

        detail = (
            parsed_json.get("detail")
            if isinstance(parsed_json, dict) and "detail" in parsed_json
            else response.text[:300]
        )
        return APIResult(
            ok=False,
            status_code=status_code,
            data=parsed_json,
            error=f"HTTP {status_code} from {path}: {detail}",
            fetched_at=fetched_at,
        )

    def fetch_health(self) -> APIResult:
        """Query `GET /health`."""
        return self._get("/health", allow_503_json=True)

    def fetch_latest_telemetry(
        self,
        *,
        bidding_zone: str = "DE-LU",
        location_id: str = "DE-DUS",
    ) -> APIResult:
        """Query `GET /telemetry/latest`."""
        return self._get(
            "/telemetry/latest",
            params={"bidding_zone": bidding_zone, "location_id": location_id},
        )

    def fetch_analytics_summary(
        self,
        *,
        bidding_zone: str = "DE-LU",
        location_id: str = "DE-DUS",
        limit: int = 168,
    ) -> APIResult:
        """Query `GET /analytics/summary`."""
        return self._get(
            "/analytics/summary",
            params={
                "bidding_zone": bidding_zone,
                "location_id": location_id,
                "limit": limit,
            },
        )

    def fetch_6h_forecast(
        self,
        *,
        bidding_zone: str = "DE-LU",
        history_window: int = 24,
    ) -> APIResult:
        """Query `GET /forecast/6h`."""
        return self._get(
            "/forecast/6h",
            params={
                "bidding_zone": bidding_zone,
                "history_window": history_window,
            },
        )

    def fetch_dlq_stats(self) -> APIResult:
        """Query `GET /admin/dlq-stats`."""
        return self._get("/admin/dlq-stats")
