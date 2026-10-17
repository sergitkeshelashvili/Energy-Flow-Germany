"""Shared pytest fixtures for local JSON payloads and MongoDB collection test doubles."""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass
class FakeDeleteResult:
    deleted_count: int


def _get_nested(doc: dict[str, Any], dotted_key: str) -> Any:
    current: Any = doc
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _matches_filter(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, condition in query.items():
        actual = _get_nested(doc, key)
        if isinstance(condition, dict):
            if "$gte" in condition and (actual is None or actual < condition["$gte"]):
                return False
            if "$lte" in condition and (actual is None or actual > condition["$lte"]):
                return False
        elif actual != condition:
            return False
    return True


class FakeMongoCollection:
    """In-memory MongoDB collection double that enforces MongoDB 7 time-series restrictions."""

    def __init__(self, *, is_timeseries: bool = False) -> None:
        self.is_timeseries = is_timeseries
        self.documents: list[dict[str, Any]] = []

    def insert_one(self, document: dict[str, Any]) -> None:
        self.documents.append(deepcopy(document))

    def insert_many(self, documents: list[dict[str, Any]], ordered: bool = True) -> None:
        _ = ordered
        for doc in documents:
            self.documents.append(deepcopy(doc))

    def find(
        self,
        filter_spec: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        query = filter_spec or {}
        matched = [deepcopy(doc) for doc in self.documents if _matches_filter(doc, query)]
        if not projection:
            return matched
        include_keys = {k for k, v in projection.items() if v == 1}
        if not include_keys:
            return matched
        return [{k: doc[k] for k in include_keys if k in doc} for doc in matched]

    def find_one(
        self,
        filter_spec: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        results = self.find(filter_spec, projection)
        return results[0] if results else None

    def delete_many(self, filter_spec: dict[str, Any]) -> FakeDeleteResult:
        kept: list[dict[str, Any]] = []
        deleted = 0
        for doc in self.documents:
            if _matches_filter(doc, filter_spec):
                deleted += 1
            else:
                kept.append(doc)
        self.documents = kept
        return FakeDeleteResult(deleted_count=deleted)

    def update_one(
        self,
        filter_spec: dict[str, Any],
        update_doc: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> None:
        if self.is_timeseries:
            raise AssertionError(
                "MongoDB 7 time-series collections do not support ordinary update_one/upsert operations!"
            )
        set_fields = update_doc.get("$set", {})
        for idx, doc in enumerate(self.documents):
            if _matches_filter(doc, filter_spec):
                updated = deepcopy(doc)
                updated.update(deepcopy(set_fields))
                self.documents[idx] = updated
                return
        if upsert:
            new_doc = deepcopy(filter_spec)
            new_doc.update(deepcopy(set_fields))
            self.documents.append(new_doc)


@pytest.fixture
def energy_charts_fixture_dict() -> dict[str, Any]:
    """Load the local Energy-Charts DE-LU JSON fixture."""
    path = FIXTURES_DIR / "energy_charts_price_de_lu.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def open_meteo_fixture_dict() -> dict[str, Any]:
    """Load the local Open-Meteo Düsseldorf forecast JSON fixture."""
    path = FIXTURES_DIR / "open_meteo_forecast_dusseldorf.json"
    return json.loads(path.read_text(encoding="utf-8"))
