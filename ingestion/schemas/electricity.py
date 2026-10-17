"""Pydantic v2 models for Energy-Charts electricity spot price payloads and normalized records."""

import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from ingestion.schemas.common import BatchTransformResult, DeadLetterRecord, ensure_utc_datetime


class EnergyChartsPriceResponse(BaseModel):
    """Raw response schema for Fraunhofer ISE Energy-Charts `/price` endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    unix_seconds: list[StrictInt]
    price: list[StrictFloat | StrictInt | None]
    unit: str = Field(min_length=1)
    license_info: str = Field(default="")

    @field_validator("unit")
    @classmethod
    def validate_non_blank_unit(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Energy-Charts 'unit' must not be empty.")
        return cleaned

    @model_validator(mode="after")
    def validate_array_alignment(self) -> "EnergyChartsPriceResponse":
        """Ensure unix_seconds and price arrays are non-empty and strictly aligned."""
        if not self.unix_seconds:
            raise ValueError("Energy-Charts 'unix_seconds' array must not be empty.")
        if len(self.unix_seconds) != len(self.price):
            raise ValueError(
                f"Misaligned Energy-Charts arrays: len(unix_seconds)={len(self.unix_seconds)} "
                f"!= len(price)={len(self.price)}"
            )
        return self


class ElectricityPriceMetadata(BaseModel):
    """Metadata subdocument (`metaField`) for the `electricity_prices` time-series collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bidding_zone: str = Field(min_length=1)
    source: Literal["energy_charts"] = "energy_charts"
    unit: str = Field(min_length=1)
    license_info: str = Field(default="")


class ElectricityPriceRecord(BaseModel):
    """Normalized and validated electricity spot price time-series document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    metadata: ElectricityPriceMetadata
    price: float
    unit: str = Field(min_length=1)
    source_unix_seconds: StrictInt
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("timestamp", "ingested_at")
    @classmethod
    def validate_utc_timestamps(cls, value: datetime) -> datetime:
        return ensure_utc_datetime(value)

    @field_validator("price", mode="before")
    @classmethod
    def reject_boolean_price(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid electricity prices.")
        return value

    @field_validator("price")
    @classmethod
    def validate_finite_price(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Electricity price must be a finite number.")
        return float(value)

    def to_mongo_doc(self) -> dict[str, Any]:
        """Serialize to a MongoDB time-series document preserving BSON UTC datetimes."""
        return self.model_dump(mode="python")


def transform_energy_charts_response(
    response: EnergyChartsPriceResponse,
    bidding_zone: str = "DE-LU",
    ingested_at: datetime | None = None,
) -> BatchTransformResult[ElectricityPriceRecord]:
    """Transform aligned `(unix_seconds, price)` pairs into validated records or DLQ entries.

    Missing (`None`) or non-finite prices are never fabricated; they are captured as
    `DeadLetterRecord` entries with their original index and source values.
    """
    ingestion_ts = ensure_utc_datetime(ingested_at or datetime.now(timezone.utc))
    metadata = ElectricityPriceMetadata(
        bidding_zone=bidding_zone,
        source="energy_charts",
        unit=response.unit,
        license_info=response.license_info,
    )

    valid_records: list[ElectricityPriceRecord] = []
    dead_letters: list[DeadLetterRecord] = []

    for idx, (unix_sec, raw_price) in enumerate(
        zip(response.unix_seconds, response.price, strict=True)
    ):
        if raw_price is None or isinstance(raw_price, bool):
            dead_letters.append(
                DeadLetterRecord(
                    source="energy_charts",
                    scope=bidding_zone,
                    stage="record_validation",
                    reason=f"Missing (null) or invalid price value at index {idx}.",
                    raw_payload={
                        "unix_seconds": unix_sec,
                        "price": raw_price,
                        "unit": response.unit,
                        "bidding_zone": bidding_zone,
                    },
                    record_index=idx,
                    failed_at=ingestion_ts,
                )
            )
            continue

        try:
            utc_timestamp = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
            record = ElectricityPriceRecord(
                timestamp=utc_timestamp,
                metadata=metadata,
                price=float(raw_price),
                unit=response.unit,
                source_unix_seconds=unix_sec,
                ingested_at=ingestion_ts,
            )
            valid_records.append(record)
        except (ValidationError, ValueError, OverflowError, OSError) as exc:
            dead_letters.append(
                DeadLetterRecord(
                    source="energy_charts",
                    scope=bidding_zone,
                    stage="record_validation",
                    reason=str(exc),
                    raw_payload={
                        "unix_seconds": unix_sec,
                        "price": raw_price,
                        "unit": response.unit,
                        "bidding_zone": bidding_zone,
                    },
                    record_index=idx,
                    failed_at=ingestion_ts,
                )
            )

    return BatchTransformResult(valid_records=valid_records, dead_letters=dead_letters)
