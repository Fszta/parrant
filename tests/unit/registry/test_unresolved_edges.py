"""Registry detection — the ``unexpandable_star`` unresolved edge + marker handoff.

The parser cannot tell a leaked ``select *`` base relation from a real source (both are bare
relations). The registry can: a star base that is NOT one of the model's declared upstream
dependencies is a fabricated edge. This is the ``unexpandable_star`` marker.

We drive a fully-synthetic, abstract manifest/catalog built at runtime (no committed JSON, no
credentials) by :mod:`tests.fixtures.unresolved_edges._build` — every identifier is a placeholder
(``stg_a``, ``int_c``, ``raw_x``, columns ``col_1`` / ``owner_id`` / ``v`` ...).
"""

import sys
import tempfile
from pathlib import Path

from parrant.artifacts.registry import ModelRegistry

_FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "unresolved_edges"
sys.path.insert(0, str(_FIXTURE_DIR))
import _build  # type: ignore[import-not-found]  # noqa: E402  (path-injected fixture builder)

# Materialize the manifest + catalog once into a tmp dir for the whole module.
_TMP = tempfile.TemporaryDirectory()
_MANIFEST, _CATALOG = _build.write_fixtures(Path(_TMP.name))


def _loaded_registry():
    registry = ModelRegistry(
        str(_CATALOG),
        str(_MANIFEST),
        adapter_override="snowflake",
    )
    registry.load()
    return registry


def _markers(model):
    return (model.metadata or {}).get("unresolved_edges") or []


def test_unexpandable_star_declared_and_phantom_token_dropped():
    """A star base that is not a declared upstream -> unexpandable_star marker, token dropped."""
    registry = _loaded_registry()
    model = registry.get_model("stg_a")

    markers = _markers(model)
    reasons = {m["reason"] for m in markers}
    assert "unexpandable_star" in reasons

    col_1 = next(m for m in markers if m["column"] == "col_1")
    assert col_1["reason"] == "unexpandable_star"
    assert col_1["model"] == "stg_a"  # registry stamps the model name
    assert "rel_z" in (col_1["detail"] or "")

    # The fabricated deep-source token is gone from the resolved lineage.
    sources = {
        token
        for lineage in (model.columns["col_1"].lineage or [])
        for token in lineage.source_columns
    }
    assert not any("rel_z" in token for token in sources), sources


def test_declared_source_edge_is_not_dropped_failsafe():
    """Fail-safe: a token qualified by a REAL declared upstream (``raw_x``) is kept, not marked."""
    registry = _loaded_registry()
    model = registry.get_model("stg_d")

    markers = _markers(model)
    # stg_d flatten phantoms are `phantom_alias`, never `unexpandable_star`
    # (its base relation `raw_x` IS a declared source).
    assert {m["reason"] for m in markers} == {"phantom_alias"}

    v_out_sources = {
        token
        for lineage in (model.columns["v_out"].lineage or [])
        for token in lineage.source_columns
    }
    # The genuine (if coarse) edge to the real source `raw_x` survives.
    assert "raw_x.attr_name" in v_out_sources
    # The phantom flatten alias does not.
    assert not any(token.startswith("p.") for token in v_out_sources)


def test_marker_set_is_complete_and_stamped():
    """Every attached marker carries the model name and a valid reason (machine surface)."""
    registry = _loaded_registry()
    valid_reasons = {
        "phantom_alias",
        "unexpandable_star",
        "fabricated_column",
        "star_rename",
        "pivot_output",
        "other",
    }
    for name in ("stg_a", "stg_d"):
        model = registry.get_model(name)
        for marker in _markers(model):
            assert marker["model"] == name
            assert marker["reason"] in valid_reasons
            assert marker["column"]


def test_fabricated_same_named_column_declared_and_token_dropped():
    """A same-named column emitted onto a REAL catalog-backed upstream that lacks it.

    ``int_c`` mis-attributes derived outputs like ``owner_id`` onto the base relation
    ``stg_a`` — a real, catalog-backed upstream whose catalog columns do NOT include it.
    The relation is real, so ``phantom_alias`` / ``unexpandable_star`` both miss it; only the
    catalog-aware check catches it.
    """
    registry = _loaded_registry()
    model = registry.get_model("int_c")

    fabricated = [m for m in _markers(model) if m["reason"] == "fabricated_column"]
    assert fabricated, "expected fabricated_column markers on the macro-inlined int model"

    owner = next(m for m in fabricated if m["column"] == "owner_id")
    assert owner["model"] == "int_c"
    assert owner["detail"] == "stg_a.owner_id"

    # The fabricated token is gone from the resolved lineage (fail-safe: only it, model edge kept).
    sources = {
        token
        for lineage in (model.columns["owner_id"].lineage or [])
        for token in lineage.source_columns
    }
    assert "stg_a.owner_id" not in sources


def test_fabricated_detection_keeps_legit_passthrough_failsafe():
    """A column mixing a legit passthrough with fabricated tokens keeps only the real one.

    ``g_owner`` coalesces ``stg_b.v`` (a REAL column of a catalog-backed upstream — legit) with two
    fabricated ``stg_a.fab_v*`` tokens (absent upstream). The legit edge must survive; only the
    fabricated tokens are dropped.
    """
    registry = _loaded_registry()
    model = registry.get_model("int_c")

    sources = {
        token
        for lineage in (model.columns["g_owner"].lineage or [])
        for token in lineage.source_columns
    }
    assert "stg_b.v" in sources  # kept — column exists upstream
    assert not any("fab_" in token for token in sources)  # fabricated tokens dropped

    fabricated_details = {
        m["detail"] for m in _markers(model) if m["column"] == "g_owner"
    }
    assert fabricated_details == {
        "stg_a.fab_v1",
        "stg_a.fab_v2",
    }


def test_real_upstream_columns_are_not_marked_control():
    """Control: passthrough columns that DO exist in the immediate upstream stay clean."""
    registry = _loaded_registry()
    model = registry.get_model("int_c")

    for col_name in ("col_1", "col_2", "col_3"):
        sources = {
            token
            for lineage in (model.columns[col_name].lineage or [])
            for token in lineage.source_columns
        }
        assert sources == {f"stg_a.{col_name}"}
        assert not [
            m
            for m in _markers(model)
            if m["column"] == col_name and m["reason"] == "fabricated_column"
        ]


def test_non_catalog_backed_upstream_absence_is_not_provable():
    """Fail-safe: a token on a non-catalog-backed upstream (a source) is never fabricated_column.

    ``stg_d`` emits ``raw_x.attr_name`` — ``attr_name`` is not a real column of the ``raw_x``
    source, but ``raw_x`` is a source (NOT catalog-backed), so absence is not provable and we must
    SKIP rather than fabricate a marker (that would over-widen). Its only markers stay
    ``phantom_alias`` (the flatten aliases), and the coarse ``raw_x.attr_name`` edge is kept.
    """
    registry = _loaded_registry()
    model = registry.get_model("stg_d")

    reasons = {m["reason"] for m in _markers(model)}
    assert "fabricated_column" not in reasons
    assert reasons == {"phantom_alias"}

    v_out_sources = {
        token
        for lineage in (model.columns["v_out"].lineage or [])
        for token in lineage.source_columns
    }
    assert "raw_x.attr_name" in v_out_sources
