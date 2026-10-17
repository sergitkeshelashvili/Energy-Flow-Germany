"""Shared Pydantic v2 schemas for checkpoints, dead-letter queue records, and batch results."""

from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceName = Literal["energy_charts", "open_meteo"]
FailureStage = Literal["http_fetch", "payload_validation", "record_validation"]
CheckpointStatus = Literal["success", "partial_failure", "failed"]

RecordT = TypeVar("RecordT", bound=BaseModel)


def ensure_utc_datetime(value: datetime) -> datetime:
    """Validate that a datetime is timezone-aware and normalized to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("Datetime must be timezone-aware.")
    return value.astimezone(timezone.utc)


class DeadLetterRecord(BaseModel):
    """Schema for invalid payloads or records stored in the regular DLQ collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceName
    scope: str = Field(min_length=1)
    stage: FailureStage
    reason: str = Field(min_length=1)
    raw_payload: dict[str, Any]
    record_index: int | None = Field(default=None, ge=0)
    failed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = Field(default=False)

    @field_validator("failed_at")
    @classmethod
    def validate_utc_failed_at(cls, value: datetime) -> datetime:
        return ensure_utc_datetime(value)

    def to_mongo_doc(self) -> dict[str, Any]:
        """Serialize to a MongoDB-compatible dictionary preserving BSON datetime types."""
        return self.model_dump(mode="python")


class IngestionCheckpoint(BaseModel):
    """Schema for stateful ingestion progress stored in the regular checkpoints collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceName
    scope: str = Field(min_length=1)
    last_successful_timestamp: datetime | None = None
    window_start_timestamp: datetime | None = None
    window_end_timestamp: datetime | None = None
    last_ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    records_inserted: int = Field(default=0, ge=0)
    records_skipped_duplicate: int = Field(default=0, ge=0)
    records_dlq: int = Field(default=0, ge=0)
    status: CheckpointStatus = "success"

    @field_validator(
        "last_successful_timestamp",
        "window_start_timestamp",
        "window_end_timestamp",
        "last_ingested_at",
    )
    @classmethod
    def validate_optional_utc_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_utc_datetime(value)

    def to_mongo_doc(self) -> dict[str, Any]:
        """Serialize to a MongoDB-compatible dictionary preserving BSON datetime types."""
        return self.model_dump(mode="python")


class BatchTransformResult(BaseModel, Generic[RecordT]):
    """Container holding validated time-series records alongside any rejected DLQ records."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    valid_records: list[RecordT]
    dead_letters: list[DeadLetterRecord]
