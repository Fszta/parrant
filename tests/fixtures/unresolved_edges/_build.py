"""Build the smallest possible dbt manifest + catalog that exercise every unresolved-edge
marker, using a fully abstract placeholder vocabulary (no real project / business / vendor terms).

Nothing here is committed as JSON: :func:`write_fixtures` materializes both artifacts into a
caller-supplied directory (a pytest ``tmp_path``) at test runtime. Both the registry unit test
and the CLI e2e test load those tmp files through the normal ``--manifest/--catalog`` paths.

Abstract vocabulary
-------------------
* project ``demo``; database ``DB``; schemas ``STG`` / ``INT`` / ``SRC``.
* source ``src_x.raw_x`` (present in the catalog but WITHOUT profiled columns -> not
  catalog-backed).
* models ``stg_a``, ``stg_b``, ``int_c``, ``stg_d``.
* the leaked ``select *`` base relation ``rel_z`` (referenced only in ``stg_a``'s SQL, never a
  declared dependency).
* columns are neutral placeholders: ``id`` / ``col_1`` / ``v`` / ``owner_id`` / ``attr_name`` ...

Marker coverage — only the constructs the registry + e2e tests actually assert on. (``pivot_output``
and ``star_rename`` are covered by the parser's inline-SQL unit tests, so no fixture model is
spent on them.)
-------------------------------------------------
* ``stg_a``  -> ``unexpandable_star``  (``select *`` off a subquery on ``rel_z``, undeclared).
* ``stg_d``  -> ``phantom_alias``      (``lateral flatten`` alias ``p`` leaks ``p.value``), while the
  genuine coarse edge to the real source (``raw_x.attr_name``) survives; ``raw_x`` is a source, so
  its column absence is unprovable -> the non-catalog-backed control (never ``fabricated_column``).
* ``int_c``  -> ``fabricated_column``  (emits ``stg_a.owner_id`` / ``stg_a.fab_v*`` — real,
  catalog-backed upstream ``stg_a`` whose catalog lacks those columns) + a clean passthrough
  control (``col_1``/``col_2``/``col_3`` DO exist upstream -> no marker) + a legit coalesce arm
  (``stg_b.v`` exists upstream -> kept).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PROJECT = "demo"
_DB = "DB"

# --------------------------------------------------------------------------------------------- #
# Compiled SQL — one short construct per model.
# --------------------------------------------------------------------------------------------- #

# unexpandable_star: a `select *` off a subquery on `rel_z`, which is NOT a declared upstream.
# The star base `rel_z` leaks as the qualifier on every renamed column (`rel_z.v`).
_SQL_STG_A = """
with
imported as (
    select * from (
        select *
        from DB.SRC.rel_z
    )
),
renamed as (
    select
        v:id::varchar as col_1,
        v:name::varchar as col_2,
        v:ts::varchar as col_3
    from imported
),
deduped as (
    select *
    from renamed
)
select * from deduped
"""

# A trivial catalog-backed leaf that exposes exactly column `v` (the legit coalesce arm upstream).
_SQL_STG_B = "select k as v from DB.SRC.raw_b"

# fabricated_column: `owner_id` / `fab_v1` / `fab_v2` are emitted onto the real, catalog-backed
# upstream `stg_a` whose catalog columns are only {col_1, col_2, col_3}. `col_1..col_3` are the
# clean passthrough control; `stg_b.v` is the legit coalesce arm that must survive.
_SQL_INT_C = """
with
stg_a as (
    select * from DB.STG.stg_a
),
stg_b as (
    select * from DB.STG.stg_b
),
final as (
    select
        stg_a.col_1,
        stg_a.col_2,
        stg_a.col_3,
        stg_a.owner_id,
        coalesce(stg_b.v, stg_a.fab_v1, stg_a.fab_v2) as g_owner
    from stg_a
    left join stg_b
        on stg_a.col_1 = stg_b.v
)
select * from final
"""

# phantom_alias: the `lateral flatten` alias `p` leaks as `p.value`. Here it flattens a literal
# array — an expression with NO traceable upstream column — so it stays an honest phantom_alias
# marker (the flatten-resolution pass only re-attributes when the flattened expression resolves to
# a real upstream; see the parser's flatten-resolution tests for the resolved case). The coarse
# edge to the real source `raw_x.attr_name` (a plain passthrough branch) survives independently.
# `raw_x` is a source (not catalog-backed) -> its column absence is unprovable, so this stays
# phantom_alias only, never fabricated_column.
_SQL_STG_D = """
with
imported as (
    select id, attr_name from DB.SRC.raw_x
),
genuine as (
    select id, attr_name as v_out from imported
),
phantom as (
    select
        id,
        p.value:k::varchar as v_out
    from imported,
        lateral flatten([1, 2, 3]) p
),
final as (
    select
        genuine.id,
        coalesce(genuine.v_out, phantom.v_out) as v_out
    from genuine
    left join phantom using (id)
)
select * from final
"""


def _manifest_node(
    *,
    name: str,
    schema: str,
    compiled_code: str,
    depends_on_nodes: list[str],
    columns: list[str],
) -> tuple[str, dict[str, Any]]:
    unique_id = f"model.{_PROJECT}.{name}"
    return unique_id, {
        "unique_id": unique_id,
        "resource_type": "model",
        "package_name": _PROJECT,
        "name": name,
        "schema": schema,
        "database": _DB,
        "alias": name,
        "path": f"{name}.sql",
        "original_file_path": f"models/{name}.sql",
        "language": "sql",
        "relation_name": f"{_DB}.{schema}.{name}",
        "tags": [],
        "config": {"materialized": "view", "tags": [], "meta": {}},
        "columns": {c: {"name": c} for c in columns},
        "depends_on": {"macros": [], "nodes": depends_on_nodes},
        "compiled_code": compiled_code,
    }


def build_manifest() -> dict[str, Any]:
    source_id = f"source.{_PROJECT}.src_x.raw_x"

    nodes: dict[str, Any] = {}
    for uid, node in [
        _manifest_node(
            name="stg_a",
            schema="STG",
            compiled_code=_SQL_STG_A,
            depends_on_nodes=[],
            columns=["col_1", "col_2", "col_3"],
        ),
        _manifest_node(
            name="stg_b",
            schema="STG",
            compiled_code=_SQL_STG_B,
            depends_on_nodes=[],
            columns=["v"],
        ),
        _manifest_node(
            name="int_c",
            schema="INT",
            compiled_code=_SQL_INT_C,
            depends_on_nodes=["model.demo.stg_a", "model.demo.stg_b"],
            columns=["col_1", "col_2", "col_3", "owner_id", "g_owner"],
        ),
        _manifest_node(
            name="stg_d",
            schema="STG",
            compiled_code=_SQL_STG_D,
            depends_on_nodes=[source_id],
            columns=["id", "v_out"],
        ),
    ]:
        nodes[uid] = node

    sources = {
        source_id: {
            "unique_id": source_id,
            "resource_type": "source",
            "package_name": _PROJECT,
            "source_name": "src_x",
            "name": "raw_x",
            "identifier": "raw_x",
            "schema": "SRC",
            "database": _DB,
            "relation_name": f"{_DB}.SRC.raw_x",
            "columns": {},
        }
    }

    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.9.10",
            "project_name": _PROJECT,
            "adapter_type": "snowflake",
        },
        "nodes": nodes,
        "sources": sources,
        "macros": {},
        "docs": {},
        "exposures": {},
        "metrics": {},
        "groups": {},
        "selectors": {},
        "disabled": {},
        "parent_map": {},
        "child_map": {},
    }


def _catalog_node(*, name: str, schema: str, columns: list[str]) -> tuple[str, dict[str, Any]]:
    unique_id = f"model.{_PROJECT}.{name}"
    return unique_id, {
        "metadata": {"name": name, "schema": schema, "database": _DB, "type": "BASE TABLE"},
        "columns": {c: {"name": c, "type": "VARCHAR", "index": i} for i, c in enumerate(columns)},
    }


def build_catalog() -> dict[str, Any]:
    # Only the models that must be catalog-backed. `stg_d` is deliberately catalog-missing
    # (its columns are recovered from compiled SQL).
    nodes: dict[str, Any] = {}
    for uid, node in [
        _catalog_node(name="stg_a", schema="STG", columns=["col_1", "col_2", "col_3"]),
        _catalog_node(name="stg_b", schema="STG", columns=["v"]),
        _catalog_node(
            name="int_c",
            schema="INT",
            columns=["col_1", "col_2", "col_3", "owner_id", "g_owner"],
        ),
    ]:
        nodes[uid] = node

    # The source `raw_x` is present but WITHOUT profiled columns: a source is never
    # "catalog-backed" (only model-like nodes are), so its column absence stays unprovable
    # (no fabricated_column) — yet its presence lets the coarse edge group under a real upstream.
    source_id = f"source.{_PROJECT}.src_x.raw_x"
    sources = {
        source_id: {
            "metadata": {"name": "raw_x", "schema": "SRC", "database": _DB},
            "source_name": "src_x",
            "columns": {},
        }
    }

    return {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/catalog/v1.json"},
        "nodes": nodes,
        "sources": sources,
    }


def write_fixtures(target_dir: Path) -> tuple[Path, Path]:
    """Write the manifest + catalog into ``target_dir`` and return their paths."""
    target_dir = Path(target_dir)
    manifest_path = target_dir / "manifest.json"
    catalog_path = target_dir / "catalog.json"
    manifest_path.write_text(json.dumps(build_manifest()))
    catalog_path.write_text(json.dumps(build_catalog()))
    return manifest_path, catalog_path
