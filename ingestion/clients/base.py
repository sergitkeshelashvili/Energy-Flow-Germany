"""Resilient HTTP client foundation with bounded retries, exponential backoff, jitter, and Retry-After support."""

from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import time
from typing import Any

import httpx

from common.logging import get_logger
from ingestion.schemas.common import DeadLetterRecord, FailureStage, SourceName

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class IngestionClientError(Exception):
    """Base exception for ingestion HTTP and payload validation failures."""

    def __init__(
        self,
        message: str,
        *,
        source: SourceName,
        scope: str,
        stage: FailureStage = "http_fetch",
        status_code: int | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.source: SourceName = source
        self.scope: str = scope
        self.stage: FailureStage = stage
        self.status_code: int | None = status_code
        self.raw_payload: dict[str, Any] = raw_payload or {}

    def to_dead_letter(self, failed_at: datetime | None = None) -> DeadLetterRecord:
        """Convert this client error into a DeadLetterRecord for persistence."""
        payload = dict(self.raw_payload)
        if self.status_code is not None and "status_code" not in payload:
            payload["status_code"] = self.status_code
        return DeadLetterRecord(
            source=self.source,
            scope=self.scope,
            stage=self.stage,
            reason=str(self),
            raw_payload=payload,
            record_index=None,
            failed_at=failed_at or datetime.now(timezone.utc),
        )


class APITimeoutError(IngestionClientError):
    """Raised when an HTTP request times out after exhausting bounded retries."""


class APIRateLimitError(IngestionClientError):
    """Raised when HTTP 429 rate limiting persists beyond bounded retries or max backoff."""


class APIHTTPError(IngestionClientError):
    """Raised on non-retryable HTTP errors (e.g. 400, 404) or exhausted 5xx errors."""


class APIMalformedJSONError(IngestionClientError):
    """Raised when the upstream endpoint returns invalid or non-object JSON."""


class APISchemaValidationError(IngestionClientError):
    """Raised when the upstream JSON payload fails Pydantic v2 schema or alignment validation."""


def parse_retry_after(
    header_value: str | None,
    now: datetime | None = None,
) -> float | None:
    """Parse an HTTP `Retry-After` header (seconds or RFC-7231 HTTP-date) into delay seconds."""
    if header_value is None:
        return None
    cleaned = header_value.strip()
    if not cleaned:
        return None

    try:
        seconds = float(cleaned)
        return max(0.0, seconds)
    except ValueError:
        pass

    try:
        retry_dt = parsedate_to_datetime(cleaned)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        reference_now = now or datetime.now(timezone.utc)
        delta = (retry_dt - reference_now).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError, OverflowError):
        return None


def compute_backoff_delay(
    attempt: int,
    base_seconds: float,
    max_seconds: float,
    retry_after: float | None = None,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    """Compute bounded exponential backoff with jitter, respecting `Retry-After` if present."""
    exp_delay = min(max_seconds, base_seconds * (2**attempt))
    jitter = max(0.0, jitter_fn(0.0, exp_delay * 0.25))
    computed = min(max_seconds, exp_delay + jitter)

    if retry_after is not None:
        return max(retry_after, computed)
    return computed


class BaseAPIClient:
    """Base synchronous HTTP client with timeouts, bounded retries, and Retry-After handling."""

    def __init__(
        self,
        *,
        source: SourceName,
        base_url: str,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.source: SourceName = source
        self.base_url: str = base_url.rstrip("/")
        self.timeout_seconds: float = timeout_seconds
        self.max_retries: int = max_retries
        self.backoff_base_seconds: float = backoff_base_seconds
        self.backoff_max_seconds: float = backoff_max_seconds
        self._external_client = http_client is not None
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "EnergyFlowGermany/1.0"},
        )
        self._sleep_fn = sleep_fn
        self._jitter_fn = jitter_fn

    def close(self) -> None:
        """Close the underlying httpx.Client if owned by this instance."""
        if not self._external_client:
            self._client.close()

    def __enter__(self) -> "BaseAPIClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any],
        scope: str,
    ) -> dict[str, Any]:
        """Execute a GET request with bounded retries and parse the response as a JSON object."""
        url = f"{self.base_url}/{path.lstrip('/')}" if self._external_client else path

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    delay = compute_backoff_delay(
                        attempt=attempt,
                        base_seconds=self.backoff_base_seconds,
                        max_seconds=self.backoff_max_seconds,
                        jitter_fn=self._jitter_fn,
                    )
                    logger.warning(
                        "Timeout querying %s (scope=%s, attempt=%d/%d). Retrying in %.2fs.",
                        self.source,
                        scope,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self._sleep_fn(delay)
                    continue
                raise APITimeoutError(
                    f"Timed out after {self.max_retries + 1} attempts querying {self.source} ({path}).",
                    source=self.source,
                    scope=scope,
                    stage="http_fetch",
                    raw_payload={"path": path, "params": params, "error": str(exc)},
                ) from exc
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    delay = compute_backoff_delay(
                        attempt=attempt,
                        base_seconds=self.backoff_base_seconds,
                        max_seconds=self.backoff_max_seconds,
                        jitter_fn=self._jitter_fn,
                    )
                    logger.warning(
                        "Network error querying %s (scope=%s, attempt=%d/%d): %s. Retrying in %.2fs.",
                        self.source,
                        scope,
                        attempt + 1,
                        self.max_retries + 1,
                        exc,
                        delay,
                    )
                    self._sleep_fn(delay)
                    continue
                raise APIHTTPError(
                    f"Request error after {self.max_retries + 1} attempts querying {self.source}: {exc}",
                    source=self.source,
                    scope=scope,
                    stage="http_fetch",
                    raw_payload={"path": path, "params": params, "error": str(exc)},
                ) from exc

            status_code = response.status_code

            if status_code == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > self.backoff_max_seconds:
                    raise APIRateLimitError(
                        f"HTTP 429 from {self.source} requested Retry-After={retry_after:.1f}s "
                        f"which exceeds max backoff cap ({self.backoff_max_seconds:.1f}s).",
                        source=self.source,
                        scope=scope,
                        stage="http_fetch",
                        status_code=429,
                        raw_payload={
                            "path": path,
                            "params": params,
                            "retry_after": retry_after,
                        },
                    )
                if attempt < self.max_retries:
                    delay = compute_backoff_delay(
                        attempt=attempt,
                        base_seconds=self.backoff_base_seconds,
                        max_seconds=self.backoff_max_seconds,
                        retry_after=retry_after,
                        jitter_fn=self._jitter_fn,
                    )
                    logger.warning(
                        "Rate limited (HTTP 429) by %s (scope=%s, attempt=%d/%d). Sleeping %.2fs.",
                        self.source,
                        scope,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self._sleep_fn(delay)
                    continue
                raise APIRateLimitError(
                    f"HTTP 429 rate limit persisted after {self.max_retries + 1} attempts for {self.source}.",
                    source=self.source,
                    scope=scope,
                    stage="http_fetch",
                    status_code=429,
                    raw_payload={
                        "path": path,
                        "params": params,
                        "retry_after": retry_after,
                    },
                )

            if status_code in RETRYABLE_STATUS_CODES:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if (
                    attempt < self.max_retries
                    and (retry_after is None or retry_after <= self.backoff_max_seconds)
                ):
                    delay = compute_backoff_delay(
                        attempt=attempt,
                        base_seconds=self.backoff_base_seconds,
                        max_seconds=self.backoff_max_seconds,
                        retry_after=retry_after,
                        jitter_fn=self._jitter_fn,
                    )
                    logger.warning(
                        "Transient HTTP %d from %s (scope=%s, attempt=%d/%d). Retrying in %.2fs.",
                        status_code,
                        self.source,
                        scope,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self._sleep_fn(delay)
                    continue
                raise APIHTTPError(
                    f"Transient HTTP {status_code} from {self.source} failed after {attempt + 1} attempts.",
                    source=self.source,
                    scope=scope,
                    stage="http_fetch",
                    status_code=status_code,
                    raw_payload={
                        "path": path,
                        "params": params,
                        "body_preview": response.text[:500],
                    },
                )

            if status_code >= 400:
                raise APIHTTPError(
                    f"Non-retryable HTTP {status_code} returned by {self.source} ({path}).",
                    source=self.source,
                    scope=scope,
                    stage="http_fetch",
                    status_code=status_code,
                    raw_payload={
                        "path": path,
                        "params": params,
                        "body_preview": response.text[:500],
                    },
                )

            try:
                parsed = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise APIMalformedJSONError(
                    f"Malformed JSON returned by {self.source} ({path}): {exc}",
                    source=self.source,
                    scope=scope,
                    stage="payload_validation",
                    status_code=status_code,
                    raw_payload={
                        "path": path,
                        "params": params,
                        "body_preview": response.text[:500],
                    },
                ) from exc

            if not isinstance(parsed, dict):
                raise APIMalformedJSONError(
                    f"Expected JSON object from {self.source} ({path}), got {type(parsed).__name__}.",
                    source=self.source,
                    scope=scope,
                    stage="payload_validation",
                    status_code=status_code,
                    raw_payload={
                        "path": path,
                        "params": params,
                        "parsed_type": type(parsed).__name__,
                    },
                )

            return parsed

        raise APIHTTPError(
            f"Exhausted retries querying {self.source} ({path}).",
            source=self.source,
            scope=scope,
            stage="http_fetch",
            raw_payload={"path": path, "params": params},
        )
