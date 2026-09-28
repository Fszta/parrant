"""Tests for arbitrary dbt ``meta`` access (Policy engine).

The loader historically captured ``meta`` for exposures only; model nodes carried just
tool-internal flags (``catalog_missing``, ``star_sources``) and columns captured no meta.
These tests pin the new metadata-agnostic access layer:

  * ``ManifestReader.get_model_meta`` / ``get_column_meta`` merge ``config.meta`` OVER
    top-level ``meta`` (dbt precedence), lowercase column keys, and yield ``{}`` when absent.
  * ``ModelRegistry.get_model_dbt_meta`` / ``get_column_dbt_meta`` round-trip that meta,
    namespaced under ``Model.metadata["dbt_meta"]`` so it never collides with the internal
    flags.

No key is privileged — meta is exposed generically.
"""

import json

from parrant.artifacts.manifest import ManifestReader
from parrant.artifacts.registry import ModelRegistry

# --- ManifestReader-level tests -------------------------------------------


def _write_manifest(tmp_path, nodes):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"metadata": {"adapter_type": "snowflake"}, "nodes": nodes}))
    return ManifestReader(str(path))


def test_get_model_meta_merges_config_over_top_level(tmp_path):
    reader = _write_manifest(
        tmp_path,
        {
            "model.p.orders": {
                "name": "orders",
                "resource_type": "model",
                # legacy top-level meta AND canonical config.meta; config must win.
                "meta": {"critical": False, "owner": "legacy", "team": "finance"},
                "config": {"meta": {"critical": True, "owner": "config"}},
            }
        },
    )
    reader.load()

    meta = reader.get_model_meta("orders")
    assert meta["critical"] is True  # config.meta wins over top-level
    assert meta["owner"] == "config"  # config.meta wins over top-level
    assert meta["team"] == "finance"  # top-level-only key preserved


def test_get_model_meta_absent_is_empty_dict(tmp_path):
    reader = _write_manifest(
        tmp_path,
        {"model.p.orders": {"name": "orders", "resource_type": "model"}},
    )
    reader.load()

    assert reader.get_model_meta("orders") == {}
    assert reader.get_model_meta("does_not_exist") == {}


def test_get_column_meta_merges_and_lowercases(tmp_path):
    reader = _write_manifest(
        tmp_path,
        {
            "model.p.orders": {
                "name": "orders",
                "resource_type": "model",
                "columns": {
                    "Amount": {
                        "name": "Amount",
                        "meta": {"pii": False, "unit": "eur"},
                        "config": {"meta": {"pii": True}},
                    },
                    "status": {"name": "status"},  # declared, no meta
                },
            }
        },
    )
    reader.load()

    col_meta = reader.get_column_meta("orders")
    assert "amount" in col_meta  # key lowercased
    assert col_meta["amount"]["pii"] is True  # config.meta wins
    assert col_meta["amount"]["unit"] == "eur"  # top-level-only key preserved
    assert col_meta["status"] == {}  # declared column, no meta -> {}


def test_get_column_meta_absent_model_is_empty(tmp_path):
    reader = _write_manifest(
        tmp_path,
        {"model.p.orders": {"name": "orders", "resource_type": "model"}},
    )
    reader.load()

    assert reader.get_column_meta("orders") == {}
    assert reader.get_column_meta("missing") == {}


# --- Registry-level round-trip tests --------------------------------------


def _catalog_node(name, columns):
    return {
        "unique_id": f"model.p.{name}",
        "metadata": {"name": name, "schema": "s", "database": "d", "type": "BASE TABLE"},
        "columns": {c: {"name": c, "type": "TEXT"} for c in columns},
    }


def _load_registry(tmp_path, catalog_nodes, manifest_nodes):
    catalog_path = tmp_path / "catalog.json"
    manifest_path = tmp_path / "manifest.json"
    catalog_path.write_text(
        json.dumps({"metadata": {"adapter_type": "snowflake"}, "nodes": catalog_nodes})
    )
    manifest_path.write_text(
        json.dumps({"metadata": {"adapter_type": "snowflake"}, "nodes": manifest_nodes})
    )
    registry = ModelRegistry(str(catalog_path), str(manifest_path), adapter_override="snowflake")
    registry.load()
    return registry


def _orders_manifest_node():
    return {
        "name": "orders",
        "unique_id": "model.p.orders",
        "resource_type": "model",
        "language": "sql",
        "schema": "s",
        "database": "d",
        "compiled_code": "select 1 as id, 2 as amount",
        "meta": {"team": "finance"},
        "config": {"meta": {"critical": True}},
        "columns": {
            "amount": {
                "name": "amount",
                "meta": {"pii": False},
                "config": {"meta": {"pii": True, "readable_by": ["COMPLIANCE"]}},
            }
        },
    }


def test_registry_model_meta_round_trips_and_namespaced(tmp_path):
    registry = _load_registry(
        tmp_path,
        {"model.p.orders": _catalog_node("orders", ["id", "amount"])},
        {"model.p.orders": _orders_manifest_node()},
    )

    meta = registry.get_model_dbt_meta("ORDERS")  # case-insensitive lookup
    assert meta == {"team": "finance", "critical": True}

    # Namespaced under a reserved sub-key so it stays disjoint from internal flags.
    model = registry.get_model("orders")
    assert model.metadata["dbt_meta"] == {"team": "finance", "critical": True}


def test_registry_column_meta_round_trips(tmp_path):
    registry = _load_registry(
        tmp_path,
        {"model.p.orders": _catalog_node("orders", ["id", "amount"])},
        {"model.p.orders": _orders_manifest_node()},
    )

    col_meta = registry.get_column_dbt_meta("orders", "AMOUNT")
    assert col_meta["pii"] is True  # config.meta wins
    assert col_meta["readable_by"] == ["COMPLIANCE"]

    # A column with no meta yields {}, as does an unknown model/column.
    assert registry.get_column_dbt_meta("orders", "id") == {}
    assert registry.get_column_dbt_meta("orders", "nope") == {}
    assert registry.get_model_dbt_meta("nope") == {}


def test_registry_internal_flags_untouched_by_meta(tmp_path):
    """A catalog-missing model keeps its internal ``catalog_missing`` flag AND gains
    ``dbt_meta`` — the two live side by side without collision."""
    registry = _load_registry(
        tmp_path,
        {},  # no catalog entry -> catalog-missing
        {"model.p.orders": _orders_manifest_node()},
    )

    model = registry.get_model("orders")
    assert model.metadata["catalog_missing"] is True  # internal flag preserved
    assert model.metadata["dbt_meta"] == {"team": "finance", "critical": True}
    # amount was recovered from compiled SQL, so its column meta still attaches.
    assert registry.get_column_dbt_meta("orders", "amount")["pii"] is True


# --- config axis: node.config round-trip ----------------------------------


def _granted_manifest_node():
    return {
        "name": "orders",
        "unique_id": "model.p.orders",
        "resource_type": "model",
        "language": "sql",
        "schema": "s",
        "database": "d",
        "compiled_code": "select 1 as id, 2 as amount",
        "meta": {"team": "finance"},
        "config": {
            "materialized": "incremental",
            "tags": ["nightly", "finance"],
            "grants": {"select": ["pii_reader", "analyst"]},
            "meta": {"critical": True},
        },
    }


def test_manifest_get_model_config_returns_node_config(tmp_path):
    reader = _write_manifest(tmp_path, {"model.p.orders": _granted_manifest_node()})
    reader.load()
    config = reader.get_model_config("orders")
    assert config["materialized"] == "incremental"
    assert config["grants"] == {"select": ["pii_reader", "analyst"]}
    assert config["tags"] == ["nightly", "finance"]
    # absent / unknown -> empty dict, never guessed
    reader2 = _write_manifest(
        tmp_path, {"model.p.bare": {"name": "bare", "resource_type": "model"}}
    )
    reader2.load()
    assert reader2.get_model_config("bare") == {}
    assert reader2.get_model_config("nope") == {}


def test_registry_model_config_round_trips_and_namespaced(tmp_path):
    registry = _load_registry(
        tmp_path,
        {"model.p.orders": _catalog_node("orders", ["id", "amount"])},
        {"model.p.orders": _granted_manifest_node()},
    )
    config = registry.get_model_config("ORDERS")  # case-insensitive
    assert config["grants"] == {"select": ["pii_reader", "analyst"]}
    assert config["materialized"] == "incremental"

    # Namespaced under a reserved sub-key, disjoint from dbt_meta and the internal flags.
    model = registry.get_model("orders")
    assert model.metadata["dbt_config"]["materialized"] == "incremental"
    assert model.metadata["dbt_meta"] == {"team": "finance", "critical": True}

    # Unknown model -> empty dict.
    assert registry.get_model_config("nope") == {}
