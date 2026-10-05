"""Integration test for predicate (filter/join) impact, end-to-end on real dbt artifacts.

The test project's ``flagged_transaction_metrics`` mart uses ``transactions.status`` ONLY
in a ``WHERE`` predicate (it projects ``account_id`` and a ``count(*)``) — the classic
filter-only-aggregate shape. A change to ``transactions.status`` shifts which rows it keeps
— a real impact that column-value lineage alone misses — so it must surface as a distinct
``filter`` severity, while a change to a column it *projects* (``account_id``) surfaces as
an ordinary value impact.
"""

from pathlib import Path

from parrant.artifacts.registry import ModelRegistry
from parrant.lineage.service import LineageService

_FILTER_MODEL = "flagged_transaction_metrics"


def _registry(dbt_artifacts) -> ModelRegistry:
    registry = ModelRegistry(
        str(dbt_artifacts["catalog_path"]), str(dbt_artifacts["manifest_path"])
    )
    registry.load()
    return registry


def test_predicate_source_is_captured_and_indexed(dbt_artifacts):
    registry = _registry(dbt_artifacts)

    model = registry.get_model(_FILTER_MODEL)
    # `status` is used only in the WHERE, so it's a predicate source...
    assert "transactions.status" in {s.lower() for s in model.predicate_sources}
    # ...and never a projected value.
    projected = {
        s.lower()
        for column in model.columns.values()
        for lineage in (column.lineage or [])
        for s in lineage.source_columns
    }
    assert "transactions.status" not in projected

    # The reverse index resolves transactions.status -> this model (filter dependent).
    assert _FILTER_MODEL in registry.get_filter_dependents("transactions.status")
    # A column it actually projects is NOT a filter dependency (it's a value one).
    assert _FILTER_MODEL not in registry.get_filter_dependents("transactions.account_id")


def test_filter_only_consumer_surfaces_as_filter_severity(dbt_artifacts):
    service = LineageService(
        Path(dbt_artifacts["catalog_path"]), Path(dbt_artifacts["manifest_path"])
    )

    # Changing transactions.status: the filter-only consumer is flagged as a row-set impact.
    impact = service.get_column_impact("transactions", "status")
    affected = {m["name"] for m in impact["affected_models"]}
    assert _FILTER_MODEL in affected
    filter_cols = [
        c
        for c in impact["affected_columns"]
        if c["model"] == _FILTER_MODEL and c["severity"] == "filter"
    ]
    assert filter_cols, impact["affected_columns"]

    # Changing a column it PROJECTS (account_id) is an ordinary value impact, not 'filter'.
    value_impact = service.get_column_impact("transactions", "account_id")
    value_cols = [c for c in value_impact["affected_columns"] if c["model"] == _FILTER_MODEL]
    assert value_cols, "flagged_transaction_metrics projects account_id"
    assert all(c["severity"] != "filter" for c in value_cols), value_cols
