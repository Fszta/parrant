"""Unit tests for the registry coverage signal.

Coverage is built entirely from data computed at load time: the manifest node
inventory, the catalog-seeded model set, and the SQL parse tallies. These tests
pin the honest boundaries — a fully-covered project reports ``complete``; a
partially-built one spells out every gap.
"""

import json

import pytest

from parrant.artifacts.exceptions import RegistryNotLoadedError
from parrant.artifacts.registry import ModelRegistry


def _write(tmp_path, catalog_data, manifest_data):
    catalog_path = tmp_path / "catalog.json"
    manifest_path = tmp_path / "manifest.json"
    catalog_path.write_text(json.dumps(catalog_data))
    manifest_path.write_text(json.dumps(manifest_data))
    return str(catalog_path), str(manifest_path)


def _catalog_node(name, columns):
    return {
        "unique_id": f"model.p.{name}",
        "metadata": {"name": name, "schema": "s", "database": "d"},
        "columns": {c: {"name": c, "type": "INTEGER"} for c in columns},
    }


def _manifest_node(name, compiled=None, depends_on=None):
    node = {
        "name": name,
        "resource_type": "model",
        "language": "sql",
        "depends_on": {"nodes": depends_on or []},
    }
    if compiled is not None:
        node["compiled_code"] = compiled
    return node


def test_coverage_complete_when_catalog_and_parse_cover_everything(tmp_path):
    catalog = {
        "nodes": {
            "model.p.a": _catalog_node("a", ["id"]),
            "model.p.b": _catalog_node("b", ["id"]),
        }
    }
    manifest = {
        "nodes": {
            "model.p.a": _manifest_node("a", compiled="select 1 as id"),
            "model.p.b": _manifest_node("b", compiled="select 2 as id"),
        }
    }
    catalog_path, manifest_path = _write(tmp_path, catalog, manifest)

    registry = ModelRegistry(catalog_path, manifest_path)
    registry.load()
    coverage = registry.get_coverage()

    assert coverage.complete is True
    assert coverage.models_in_manifest == 2
    assert coverage.models_in_catalog == 2
    assert coverage.parsed_ok == 2
    assert coverage.parse_failed == 0
    assert coverage.skipped_no_sql == 0
    assert coverage.not_in_catalog_count == 0
    assert coverage.failed_models == []
    assert coverage.skipped_models == []


def test_coverage_partial_when_catalog_is_sparse(tmp_path):
    # Manifest declares three models; the catalog only physically built one.
    catalog = {"nodes": {"model.p.a": _catalog_node("a", ["id"])}}
    manifest = {
        "nodes": {
            "model.p.a": _manifest_node("a", compiled="select 1 as id"),
            "model.p.b": _manifest_node("b", compiled="select 2 as id"),
            "model.p.c": _manifest_node("c", compiled="select 3 as id"),
        }
    }
    catalog_path, manifest_path = _write(tmp_path, catalog, manifest)

    registry = ModelRegistry(catalog_path, manifest_path)
    registry.load()
    coverage = registry.get_coverage()

    assert coverage.complete is False
    assert coverage.models_in_manifest == 3
    assert coverage.models_in_catalog == 1
    assert coverage.not_in_catalog_count == 2
    # b and c are absent from the catalog but present in the manifest with compiled SQL,
    # so the universe now seeds and parses them too: all three parse OK (the catalog gap
    # is about data types, not analyzability).
    assert coverage.parsed_ok == 3


def test_coverage_reports_skipped_models_without_sql(tmp_path):
    # A catalog model whose manifest node has no compiled SQL and no file on disk
    # is honestly reported as skipped (not silently dropped).
    catalog = {
        "nodes": {
            "model.p.a": _catalog_node("a", ["id"]),
            "model.p.b": _catalog_node("b", ["id"]),
        }
    }
    manifest = {
        "nodes": {
            "model.p.a": _manifest_node("a", compiled="select 1 as id"),
            "model.p.b": _manifest_node("b"),  # no compiled_code, no file
        }
    }
    catalog_path, manifest_path = _write(tmp_path, catalog, manifest)

    registry = ModelRegistry(catalog_path, manifest_path)
    registry.load()
    coverage = registry.get_coverage()

    assert coverage.complete is False
    assert coverage.skipped_no_sql == 1
    assert "b" in coverage.skipped_models
    assert coverage.parsed_ok == 1


def test_get_coverage_requires_loaded_registry(tmp_path):
    catalog = {"nodes": {"model.p.a": _catalog_node("a", ["id"])}}
    manifest = {"nodes": {"model.p.a": _manifest_node("a", compiled="select 1 as id")}}
    catalog_path, manifest_path = _write(tmp_path, catalog, manifest)

    registry = ModelRegistry(catalog_path, manifest_path)
    with pytest.raises(RegistryNotLoadedError):
        registry.get_coverage()
