"""Integration tests for compiled-SQL disk fallback.

Real dbt manifests are frequently produced without embedded ``compiled_code``
(e.g. ``dbt parse`` or ``dbt docs generate`` runs that don't compile, or manifests
committed from CI). In that state the tool used to extract *zero* lineage because
``_process_lineage`` only looked at ``compiled_code``/``compiled_sql`` on the node.

These tests reproduce that scenario portably by stripping the embedded compiled SQL
from the generated test-project manifest while leaving the on-disk
``target/compiled/**`` files in place, then asserting lineage is still recovered
via the disk fallback.
"""

import json
from pathlib import Path

import pytest

from parrant.artifacts.manifest import ManifestReader
from parrant.artifacts.registry import ModelRegistry


def _strip_compiled_code(manifest_path: Path, dest: Path) -> Path:
    """Write a copy of the manifest with all embedded compiled SQL removed."""
    manifest = json.loads(manifest_path.read_text())
    stripped = 0
    for node in manifest.get("nodes", {}).values():
        for key in ("compiled_code", "compiled_sql"):
            if node.get(key):
                node[key] = None
                stripped += 1
    dest.write_text(json.dumps(manifest))
    assert stripped > 0, "fixture manifest had no embedded compiled SQL to strip"
    return dest


@pytest.fixture
def manifest_without_compiled_code(dbt_artifacts):
    """A manifest identical to the real one but without embedded compiled SQL.

    Written into the project's ``target/`` dir (next to ``compiled/``) so the on-disk
    fallback resolves paths exactly as it would for a real ``manifest.json``.
    """
    manifest_path = Path(dbt_artifacts["manifest_path"])
    dest = manifest_path.with_name("manifest_no_compiled.json")
    _strip_compiled_code(manifest_path, dest)
    yield dest
    dest.unlink(missing_ok=True)


def test_manifest_reader_falls_back_to_disk(dbt_artifacts, manifest_without_compiled_code):
    """ManifestReader recovers compiled SQL from target/compiled/** when not embedded."""
    reader = ManifestReader(str(manifest_without_compiled_code))
    reader.load()

    sql = reader.get_compiled_sql("stg_accounts")
    assert sql, "expected compiled SQL to be recovered from disk"
    assert "select" in sql.lower()


def test_registry_extracts_lineage_without_embedded_compiled_code(
    dbt_artifacts, manifest_without_compiled_code
):
    """The registry still extracts column lineage using the on-disk compiled files."""
    registry = ModelRegistry(
        str(dbt_artifacts["catalog_path"]), str(manifest_without_compiled_code)
    )
    registry.load()

    models = registry.get_models()

    # Count how many SQL models got at least one column with lineage.
    models_with_lineage = [
        name
        for name, model in models.items()
        if model.language == "sql" and any(col.lineage for col in model.columns.values())
    ]
    assert models_with_lineage, "no lineage extracted despite compiled files on disk"

    # Spot-check a known lineage edge that must survive the disk fallback.
    column = models["stg_accounts"].columns["account_id"]
    assert column.lineage
    sources = set()
    for lineage in column.lineage:
        sources.update(lineage.source_columns)
    assert any("raw_accounts" in src for src in sources)
