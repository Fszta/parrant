import json
from typing import Any

import click

from parrant.models.schema import Column, ColumnLineage, Coverage

from .base import LineageStaticDisplay

# Keys in a lineage refs dict that hold plain string sets rather than
# {column: ColumnLineage} maps.
_SPECIAL_SET_KEYS = ("exposures", "sources", "direct_refs")


def serialize_refs(
    refs: dict[str, dict[str, ColumnLineage] | set[str]],
) -> dict[str, Any]:
    """Convert a lineage refs dict into a JSON-serializable structure.

    Produces a stable shape regardless of which special sets are present::

        {
            "models": {model: {column: {source_columns, transformation_type, ...}}},
            "sources": [...],
            "direct_refs": [...],
            "exposures": [...],
        }
    """
    models: dict[str, Any] = {}
    sources: list = []
    direct_refs: list = []
    exposures: list = []

    for key, value in refs.items():
        if key == "sources" and isinstance(value, set):
            sources = sorted(value)
        elif key == "direct_refs" and isinstance(value, set):
            direct_refs = sorted(value)
        elif key == "exposures" and isinstance(value, set):
            exposures = sorted(value)
        elif key not in _SPECIAL_SET_KEYS and isinstance(value, dict):
            models[key] = {
                col_name: lineage.model_dump(mode="json")
                for col_name, lineage in sorted(value.items())
            }

    return {
        "models": models,
        "sources": sources,
        "direct_refs": direct_refs,
        "exposures": exposures,
    }


class JsonDisplay(LineageStaticDisplay):
    """Machine-readable JSON output for column lineage and impact analysis.

    Unlike the streaming text/dot displays, this accumulates a single document and
    emits it on :meth:`save`, so downstream tooling and AI agents can consume the
    full lineage picture as one JSON object.
    """

    def __init__(self) -> None:
        self._result: dict[str, Any] = {}

    def display_column_info(self, column: Column) -> None:
        self._result["model"] = column.model_name
        self._result["column"] = column.name
        self._result["data_type"] = column.data_type
        self._result["description"] = column.description

    def set_model_description(self, description: str | None) -> None:
        """Attach the selected column's parent model description (its dbt docs).

        Kept alongside the column's own ``description`` so an agent triaging
        "what breaks if I change X" also sees what the *model* X lives in is.
        """
        self._result["model_description"] = description

    def display_upstream(self, refs: dict[str, dict[str, ColumnLineage] | set[str]]) -> None:
        self._result["upstream"] = serialize_refs(refs)

    def display_downstream(self, refs: dict[str, dict[str, ColumnLineage] | set[str]]) -> None:
        self._result["downstream"] = serialize_refs(refs)

    def set_impact(self, impact: dict[str, Any] | None) -> None:
        """Attach impact-analysis results to the JSON document."""
        if impact is not None:
            self._result["impact"] = impact

    def display_coverage(self, coverage: Coverage) -> None:
        """Attach the top-level coverage block (strictly additive)."""
        self._result["coverage"] = coverage.model_dump()

    def save(self) -> None:
        click.echo(json.dumps(self._result, indent=2, sort_keys=False))
