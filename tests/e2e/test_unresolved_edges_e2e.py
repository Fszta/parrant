"""End-to-end (CLI) coverage for the unresolved-column-edges fix.

This is the CI-collected replacement for the manual repro that used to live under
``tests/fixtures/unresolved_edges/`` (a dir ``poetry run test-e2e`` never collects). It drives
**this repo's own CLI in-process** (``parrant.cli.main:cli`` / ``:impact`` via click's
``CliRunner``) against a fully-synthetic, abstract no-creds fixture built at runtime by
:mod:`tests.fixtures.unresolved_edges._build` (no committed JSON) — so it exercises the real
command path end-to-end with **no dbt project, no Snowflake, no credentials**.

Its added value over the API-level unit tests (``tests/unit/{parser,registry,lineage}``) is that
it proves the two invariants survive all the way through the CLI's JSON report:

* **Phantom-free lineage:** for the flatten model's affected column, every ``source_columns``
  token emitted by ``parrant --select`` is qualified by a *real* upstream of that model — the
  phantom ``lateral flatten`` alias (``p.value...``) is gone, while the genuine coarse edge
  to the real source survives.
* **Confidence degrades, model rebuilt:** a change reaching a marker-bearing model makes
  ``parrant impact`` report ``confidence.level == "partial"`` and folds that model into the rebuild
  set (never ``skippable``), so CI cannot silently skip it.

Assertion invariant (same as the retired repro): we do NOT match ``source_columns`` by value or
sequence (that sequence is unstable). We assert the stable, semantic property — every qualifier in a
resolved column's ``source_columns`` must be one of the REAL upstreams parrant itself grouped the
edge under (``upstream.models`` keys are always real dbt nodes/sources). A phantom is exactly a
token whose qualifier is not one of those.
"""

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
from click.testing import CliRunner

from parrant.cli.main import cli, impact

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "unresolved_edges"
sys.path.insert(0, str(_FIXTURE_DIR))
import _build  # type: ignore[import-not-found]  # noqa: E402  (path-injected fixture builder)

# Materialize the abstract manifest + catalog once into a tmp dir for the whole module.
_TMP = tempfile.TemporaryDirectory()
_MANIFEST, _CATALOG = _build.write_fixtures(Path(_TMP.name))

# The flatten model + its affected column (real upstream is the source ``raw_x``; the phantom is
# the ``lateral flatten(attr_name) p`` pseudo-alias ``p``).
_FLATTEN_MODEL = "stg_d"
_FLATTEN_COLUMN = "v_out"
_FLATTEN_ALIAS = "p"
_GENUINE_EDGE = "raw_x.attr_name"

# The marker-bearing model (catalog-aware ``fabricated_column``) and an upstream that feeds it.
_PHANTOM_MODEL = "int_c"
_CHANGED_UPSTREAM = "stg_a"


def _json_from_output(output: str) -> Dict[str, Any]:
    """Parse the JSON report out of the CLI stdout (skipping any leading log lines)."""
    start = output.index("{")
    payload, _ = json.JSONDecoder().raw_decode(output[start:])
    return payload


def _run(command: Any, args: List[str]) -> Dict[str, Any]:
    result = CliRunner().invoke(command, args, catch_exceptions=False)
    assert result.exit_code == 0, f"exit={result.exit_code}\noutput={result.output}"
    return _json_from_output(result.output)


def _qualifier(token: str) -> str:
    """The table-qualifier of a ``source_columns`` token, lowercased and unquoted.

    ``attribute.value:x`` -> ``attribute``; ``records.attr_name`` -> ``records``; a bare column
    (no dot) -> ``""`` (treated as non-phantom).
    """
    if "." not in token:
        return ""
    return token.split(".", 1)[0].strip().strip('"').lower()


def _legit_upstreams(upstream_block: Dict[str, Any]) -> Set[str]:
    """The REAL upstreams parrant grouped this column's edges under (ground-truth qualifiers).

    ``upstream.models`` keys are always real dbt nodes; ``sources``/``direct_refs`` are real too.
    Any ``source_columns`` qualifier outside this set is a fabricated phantom.
    """
    legit = {name.lower() for name in upstream_block.get("models", {})}
    legit |= {str(name).split(".")[-1].lower() for name in upstream_block.get("sources", [])}
    legit |= {str(name).split(".")[-1].lower() for name in upstream_block.get("direct_refs", [])}
    return legit


def _all_source_tokens(upstream_block: Dict[str, Any]) -> Set[str]:
    tokens: Set[str] = set()
    for source_cols in upstream_block.get("models", {}).values():
        for edge in source_cols.values():
            tokens |= set(edge.get("source_columns", []))
    return tokens


def _phantom_tokens(upstream_block: Dict[str, Any]) -> Set[str]:
    legit = _legit_upstreams(upstream_block)
    return {
        token for token in _all_source_tokens(upstream_block) if _qualifier(token) not in legit | {""}
    }


# --------------------------------------------------------------------------------------------- #
# Detection: the flatten model's column carries no phantom source, via the CLI.
# --------------------------------------------------------------------------------------------- #
def test_flatten_column_has_no_phantom_source_via_cli() -> None:
    report = _run(
        cli,
        [
            "--select",
            f"+{_FLATTEN_MODEL}.{_FLATTEN_COLUMN}",
            "--manifest",
            str(_MANIFEST),
            "--catalog",
            str(_CATALOG),
            "--adapter",
            "snowflake",
            "-f",
            "json",
        ],
    )
    assert report["model"] == _FLATTEN_MODEL
    assert report["column"] == _FLATTEN_COLUMN
    upstream = report["upstream"]

    # Core detection invariant: no source token is qualified by a non-upstream (phantom) relation.
    phantoms = _phantom_tokens(upstream)
    assert not phantoms, f"phantom source tokens survived in the CLI lineage: {phantoms}"

    # Specifically, the `lateral flatten` pseudo-alias is gone...
    tokens = _all_source_tokens(upstream)
    assert not any(_qualifier(token) == _FLATTEN_ALIAS for token in tokens), tokens

    # ...while the genuine (coarse) edge to the REAL source survives.
    assert _GENUINE_EDGE in tokens, tokens


# --------------------------------------------------------------------------------------------- #
# Propagation: a change reaching a phantom-bearing model degrades confidence and forces its rebuild.
# --------------------------------------------------------------------------------------------- #
@pytest.fixture
def impact_reaching_phantom(tmp_path: Path) -> Dict[str, Any]:
    """Run ``parrant impact`` for a change on ``stg_a`` (an upstream of the
    marker-bearing ``int_c``).

    Base = the head manifest verbatim + a base catalog carrying one EXTRA column on
    ``stg_a`` -> head shows a REMOVED column there, which reaches the marker model.
    No dbt run required: both artifacts are synthesized in a tmp dir from the runtime fixture.
    """
    head_catalog = json.loads(_CATALOG.read_text())
    base_catalog = copy.deepcopy(head_catalog)
    mutated = None
    for node in base_catalog["nodes"].values():
        if node.get("metadata", {}).get("name", "").lower() == _CHANGED_UPSTREAM:
            node["columns"]["extra_col"] = {
                "name": "extra_col",
                "type": "BOOLEAN",
                "index": len(node["columns"]),
            }
            mutated = node
    assert mutated is not None, f"{_CHANGED_UPSTREAM} not found in fixture catalog"

    base_manifest_path = tmp_path / "base_manifest.json"
    base_catalog_path = tmp_path / "base_catalog.json"
    base_manifest_path.write_text(_MANIFEST.read_text())  # base manifest == head manifest
    base_catalog_path.write_text(json.dumps(base_catalog))

    return _run(
        impact,
        [
            "--manifest",
            str(_MANIFEST),
            "--catalog",
            str(_CATALOG),
            "--base-manifest",
            str(base_manifest_path),
            "--base-catalog",
            str(base_catalog_path),
            "--adapter",
            "snowflake",
            "-f",
            "json",
        ],
    )


def test_change_reaching_phantom_model_degrades_confidence(impact_reaching_phantom) -> None:
    confidence = impact_reaching_phantom["confidence"]
    # A reachable marker-bearing model drops the whole change to `partial` confidence.
    assert confidence["level"] == "partial", confidence
    assert _PHANTOM_MODEL in confidence["partial_edges_models"], confidence


def test_phantom_model_is_rebuilt_not_skippable(impact_reaching_phantom) -> None:
    selection = impact_reaching_phantom["selection"]
    # Partial confidence widens the rebuild to the whole reachable set: the marker model is in
    # the rebuild set and NEVER in the skippable set — CI cannot silently under-build it.
    assert _PHANTOM_MODEL in selection["rebuild_models"], selection
    assert _PHANTOM_MODEL not in selection["skippable_models"], selection
    assert selection["skippable_models"] == [], selection
    assert selection["widened_to_all_reachable"] is True, selection


def test_marker_reason_surfaced_end_to_end(impact_reaching_phantom) -> None:
    # The per-model resolution surfaces the marker's status + reason all the way to the CLI JSON.
    resolution = impact_reaching_phantom["resolution"]
    entry = resolution[_PHANTOM_MODEL]
    assert entry["status"] == "partial_edges", entry
    assert entry["reason"] == "fabricated_column", entry
    assert impact_reaching_phantom["resolution_summary"]["partial_edges"] >= 1
