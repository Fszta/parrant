"""Column-opaque handling for unparseable nodes (semantic views chief among them).

A node whose compiled SQL the parser cannot read must NOT be dropped as a parse failure and
must NOT be treated as blind. Instead it is classified ``opaque``:

* no column-level lineage (we deliberately do not parse it), but
* MODEL-level reach is preserved from the manifest dependency graph (``depends_on.nodes``),
  so a change to an upstream still reaches — and rebuilds — it at model grain;
* it is a *choice*, not a *failure*: it is moved OUT of ``parse_failed`` and excluded from the
  coverage-floor denominator.

Two unparseable shapes are exercised, both synthetic: a ``create semantic view`` stub and a
plain garbage-SQL node. Everything else in the graph is ordinary, parseable SQL.
"""

import json

from parrant.artifacts.registry import ModelRegistry
from parrant.lineage.changeset import ChangesetBuilder
from parrant.lineage.service import LineageService

# --- fixture helpers -------------------------------------------------------

# A CREATE SEMANTIC VIEW stub (Snowflake) — sqlglot cannot parse it, so it is opaque.
_SEMANTIC_VIEW_SQL = (
    "create semantic view revenue_sv "
    "tables (o as orders primary key (order_id)) "
    "dimensions (o.order_status as order_status) "
    "metrics (o.amount as total_amount)"
)

# Plain garbage — the general unparseable case, not a semantic view.
_GARBAGE_SQL = "this is not valid sql @@@ >< ( select"

# Ordinary, parseable upstream whose logic changes between base and head.
_ORDERS_BASE = "select dim.order_id as order_id, dim.amount as amount from d.s.dim_orders as dim"
_ORDERS_HEAD = (
    "select dim.order_id as order_id, dim.amount * 2 as amount from d.s.dim_orders as dim"
)


def _catalog_node(name, columns):
    return {
        "unique_id": f"model.p.{name}",
        "metadata": {"name": name, "schema": "s", "database": "d", "type": "BASE TABLE"},
        "columns": {c: {"name": c, "type": "NUMBER"} for c in columns},
    }


def _manifest_node(name, compiled, depends_on=None, materialized="table"):
    return {
        "name": name,
        "unique_id": f"model.p.{name}",
        "resource_type": "model",
        "language": "sql",
        "schema": "s",
        "database": "d",
        "config": {"materialized": materialized},
        "depends_on": {"nodes": [f"model.p.{d}" for d in (depends_on or [])]},
        "compiled_code": compiled,
    }


def _catalog_nodes():
    # Every node is catalogued (including the opaque ones), so the catalog gap is zero and the
    # ONLY thing that could drag coverage is a parse failure — which opaque must not be.
    return {
        "model.p.dim_orders": _catalog_node("dim_orders", ["order_id", "amount"]),
        "model.p.orders": _catalog_node("orders", ["order_id", "amount"]),
        "model.p.revenue_semantic_view": _catalog_node(
            "revenue_semantic_view", ["order_status", "total_amount"]
        ),
        "model.p.garbage_report": _catalog_node("garbage_report", ["x"]),
    }


def _manifest_nodes(orders_sql):
    return {
        "model.p.dim_orders": _manifest_node("dim_orders", "select 1 as order_id, 1 as amount"),
        "model.p.orders": _manifest_node("orders", orders_sql, depends_on=["dim_orders"]),
        "model.p.revenue_semantic_view": _manifest_node(
            "revenue_semantic_view",
            _SEMANTIC_VIEW_SQL,
            depends_on=["orders"],
            materialized="semantic_view",
        ),
        "model.p.garbage_report": _manifest_node(
            "garbage_report", _GARBAGE_SQL, depends_on=["orders"]
        ),
    }


def _write(tmp_path, tag, orders_sql):
    catalog_path = tmp_path / f"{tag}_catalog.json"
    manifest_path = tmp_path / f"{tag}_manifest.json"
    catalog_path.write_text(
        json.dumps({"metadata": {"adapter_type": "snowflake"}, "nodes": _catalog_nodes()})
    )
    manifest_path.write_text(
        json.dumps(
            {"metadata": {"adapter_type": "snowflake"}, "nodes": _manifest_nodes(orders_sql)}
        )
    )
    return str(catalog_path), str(manifest_path)


def _registry(tmp_path, orders_sql=_ORDERS_HEAD):
    cat, man = _write(tmp_path, "head", orders_sql)
    registry = ModelRegistry(cat, man, adapter_override="snowflake")
    registry.load()
    return registry


def _service(tmp_path, tag, orders_sql):
    cat, man = _write(tmp_path, tag, orders_sql)
    return LineageService(cat, man, adapter="snowflake")


# --- registry-level classification -----------------------------------------


def test_unparseable_nodes_are_opaque_not_parse_failed(tmp_path):
    registry = _registry(tmp_path)

    opaque = registry.get_opaque_models()
    assert opaque == {"revenue_semantic_view", "garbage_report"}
    # A choice, not a failure: neither node is a parse failure.
    assert registry.get_parse_failed_models() == set()


def test_opaque_node_keeps_model_level_upstream_edges(tmp_path):
    registry = _registry(tmp_path)

    # Both opaque nodes carry their manifest depends_on edge at model grain, even though they
    # have no column-level lineage.
    for name in ("revenue_semantic_view", "garbage_report"):
        model = registry.get_model(name)
        assert "orders" in model.upstream
        assert model.metadata.get("opaque") is True


def test_opaque_does_not_drag_the_coverage_floor(tmp_path):
    registry = _registry(tmp_path)
    coverage = registry.get_coverage()

    # Two unparseable nodes are present, yet coverage is COMPLETE: opaque is excluded from the
    # floor. No parse failures, and the opaque count is surfaced separately for honesty.
    assert coverage.complete is True
    assert coverage.parse_failed == 0
    assert coverage.failed_models == []
    assert coverage.opaque == 2
    assert set(coverage.opaque_models) == {"revenue_semantic_view", "garbage_report"}


# --- service-level confidence / resolution / reach -------------------------


def _changeset_report(tmp_path):
    base = _service(tmp_path, "base", _ORDERS_BASE)
    head = _service(tmp_path, "head", _ORDERS_HEAD)
    changes = ChangesetBuilder(base.registry, head.registry).build()
    return base, head, changes, head.get_changeset_impact(changes, base_service=base)


def test_opaque_surfaces_in_confidence_not_in_parse_failed(tmp_path):
    _, _, _, report = _changeset_report(tmp_path)
    confidence = report["confidence"]

    assert set(confidence["opaque_models"]) == {"revenue_semantic_view", "garbage_report"}
    assert confidence["opaque"] == 2
    # Moved OUT of parse_failed: the failure bucket stays empty.
    assert confidence["parse_failed_models"] == []
    assert confidence["parse_failed"] == 0
    # We cannot see column edges through an opaque node, so confidence honestly degrades.
    assert confidence["level"] == "partial"


def test_change_to_upstream_reaches_opaque_node_at_model_grain(tmp_path):
    _, _, _, report = _changeset_report(tmp_path)
    confidence = report["confidence"]
    selection = report["selection"]

    # A change to `orders` reaches both opaque nodes at model grain (they depend on it in the
    # manifest DAG) ...
    assert "revenue_semantic_view" in confidence["opaque_models"]
    assert "garbage_report" in confidence["opaque_models"]
    # ... and, being unanalyzable at the column level, they are folded into the rebuild set
    # (fail-closed): a change to an upstream can rebuild them.
    assert "revenue_semantic_view" in selection["rebuild_models"]
    assert "garbage_report" in selection["rebuild_models"]


def test_opaque_resolution_status_and_reason(tmp_path):
    _, _, _, report = _changeset_report(tmp_path)
    resolution = report["resolution"]

    # A semantic view is labelled as such; a generic unparseable node falls back to the coarse
    # reason. Neither is `parse_failed`.
    assert resolution["revenue_semantic_view"] == {"status": "opaque", "reason": "semantic_view"}
    assert resolution["garbage_report"] == {"status": "opaque", "reason": "unparseable_sql"}


def test_resolution_summary_reconciles_including_opaque(tmp_path):
    _, _, _, report = _changeset_report(tmp_path)
    confidence = report["confidence"]
    summary = report["resolution_summary"]

    assert summary["opaque"] == confidence["opaque"] == 2
    # The reconciliation identity now carries an ``opaque`` term.
    total = (
        summary["catalog_backed"]
        + summary["parsed"]
        + summary["no_column_info"]
        + summary["parse_failed"]
        + summary["unresolved"]
        + summary["opaque"]
    )
    assert total == summary["reachable"]
