"""Integration tests for the diff-driven ``impact`` command.

Uses the real dbt test-project artifacts as *head* and synthesizes a mutated
*base* (retyped / removed columns, changed compiled SQL) in a temp dir, then
drives the CLI end-to-end through the ChangesetBuilder + aggregation + output.
"""

import copy
import json

import pytest
from click.testing import CliRunner

from parrant.cli.main import impact


def _load(path):
    with open(path) as f:
        return json.load(f)


def _find_catalog_node(catalog, model_name):
    for node_id, node in catalog["nodes"].items():
        if node.get("metadata", {}).get("name", "").lower() == model_name:
            return node_id
    raise AssertionError(f"{model_name} not in catalog")


@pytest.fixture
def base_artifacts(dbt_artifacts, tmp_path):
    """A mutated copy of the head artifacts to act as the diff base."""
    catalog = copy.deepcopy(_load(dbt_artifacts["catalog_path"]))
    manifest = copy.deepcopy(_load(dbt_artifacts["manifest_path"]))

    # In the BASE, stg_accounts.account_id has a different type and there is an
    # extra column that head no longer has -> head shows type_changed + removed.
    node_id = _find_catalog_node(catalog, "stg_accounts")
    catalog["nodes"][node_id]["columns"]["account_id"]["type"] = "DECIMAL(38,2)"
    catalog["nodes"][node_id]["columns"]["legacy_col"] = {
        "name": "legacy_col",
        "type": "TEXT",
    }

    base_catalog = tmp_path / "catalog.json"
    base_manifest = tmp_path / "manifest.json"
    base_catalog.write_text(json.dumps(catalog))
    base_manifest.write_text(json.dumps(manifest))
    return {"catalog": str(base_catalog), "manifest": str(base_manifest)}


@pytest.fixture
def logic_change_base(dbt_artifacts, tmp_path):
    """A base whose stg_accounts derives account_id differently -> a logic change.

    Head keeps ``cast(id as integer) as account_id``; the base wraps it in ``abs(...)``, so both
    sides carry a parseable defining expression and the per-column diff flags account_id as a
    real *meaning change* (not the fail-safe), carrying an explain reason + base→head exprs.
    """
    catalog = copy.deepcopy(_load(dbt_artifacts["catalog_path"]))
    manifest = copy.deepcopy(_load(dbt_artifacts["manifest_path"]))
    node = manifest["nodes"]["model.test_project.stg_accounts"]
    node["compiled_code"] = node["compiled_code"].replace(
        "cast(id as integer) as account_id", "abs(cast(id as integer)) as account_id"
    )
    base_catalog = tmp_path / "catalog.json"
    base_manifest = tmp_path / "manifest.json"
    base_catalog.write_text(json.dumps(catalog))
    base_manifest.write_text(json.dumps(manifest))
    return {"catalog": str(base_catalog), "manifest": str(base_manifest)}


def _run_impact(args):
    return CliRunner().invoke(impact, args)


def test_impact_explain_annotates_changed_column(dbt_artifacts, logic_change_base):
    args = [
        "--manifest",
        str(dbt_artifacts["manifest_path"]),
        "--catalog",
        str(dbt_artifacts["catalog_path"]),
        "--base-manifest",
        logic_change_base["manifest"],
        "--base-catalog",
        logic_change_base["catalog"],
    ]
    # Default human report: the compact semantic reason shows (the default gate explains
    # itself) but NOT the base→head expression trace.
    plain = _run_impact(args)
    assert plain.exit_code == 0, plain.output
    assert "_why:_" in plain.output
    assert "meaning changed" in plain.output
    assert "→ `" not in plain.output  # no expression trace without --explain

    # --explain additionally surfaces the base→head expression trace.
    explained = _run_impact([*args, "--explain"])
    assert explained.exit_code == 0, explained.output
    assert "_why:_" in explained.output
    assert "meaning changed" in explained.output
    assert "→ `" in explained.output

    # JSON always carries the explain block regardless of the flag.
    json_result = _run_impact([*args, "--format", "json"])
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    logic_entries = [
        c for c in payload["by_change"] if c.get("kind") == "logic_changed" and "explain" in c
    ]
    assert logic_entries, payload["by_change"]
    assert logic_entries[0]["explain"]["reason"]


def test_impact_requires_a_change_source(dbt_artifacts):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
        ]
    )
    assert result.exit_code == 1
    assert "base-manifest" in result.output or "git-base" in result.output


def test_impact_two_manifest_json_report(dbt_artifacts, base_artifacts):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            base_artifacts["manifest"],
            "--base-catalog",
            base_artifacts["catalog"],
            "--format",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    changeset = payload["changeset"]
    assert changeset["source"] == "two-manifest"
    assert changeset["by_kind"].get("type_changed", 0) >= 1
    # legacy_col existed in base only -> removed
    assert changeset["by_kind"].get("removed", 0) >= 1

    # Superset of the single-column impact block.
    for key in ("summary", "affected_models", "affected_columns", "affected_exposures"):
        assert key in payload
    for metric in (
        "affected_models",
        "affected_columns",
        "affected_exposures",
        "critical_count",
        "low_impact_count",
        "unresolved_changes",
    ):
        assert metric in payload["summary"]

    # account_id is consumed widely downstream, so retyping it must have impact.
    assert payload["summary"]["affected_models"] >= 1
    assert "by_change" in payload


@pytest.fixture
def missing_head_sql_artifacts(dbt_artifacts, tmp_path):
    """A HEAD whose stg_accounts has NO compiled SQL available (embedded or on disk).

    Written into a bare temp dir so the ``target/compiled/**`` on-disk recovery cannot
    find it either — the model's logic diff is genuinely impossible on the head side.
    """
    catalog = copy.deepcopy(_load(dbt_artifacts["catalog_path"]))
    manifest = copy.deepcopy(_load(dbt_artifacts["manifest_path"]))
    node = manifest["nodes"]["model.test_project.stg_accounts"]
    node.pop("compiled_code", None)
    node.pop("compiled_path", None)
    head_catalog = tmp_path / "head_catalog.json"
    head_manifest = tmp_path / "head_manifest.json"
    head_catalog.write_text(json.dumps(catalog))
    head_manifest.write_text(json.dumps(manifest))
    return {"catalog": str(head_catalog), "manifest": str(head_manifest)}


def test_impact_missing_head_sql_is_indeterminate_widens_and_degrades(
    dbt_artifacts, missing_head_sql_artifacts
):
    """A model with compiled SQL on base but none on head must never read as silent-safe.

    The unprovable logic diff must (a) surface as explicit indeterminate changes in the
    JSON report, (b) degrade the impact confidence, and (c) widen the rebuild selection.
    """
    result = _run_impact(
        [
            "--manifest",
            missing_head_sql_artifacts["manifest"],
            "--catalog",
            missing_head_sql_artifacts["catalog"],
            "--base-manifest",
            str(dbt_artifacts["manifest_path"]),
            "--base-catalog",
            str(dbt_artifacts["catalog_path"]),
            "--format",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    # (a) explicit indeterminate changes for the SQL-less model in the JSON report.
    indeterminate = [
        c
        for c in payload["changeset"]["changes"]
        if c["model"] == "stg_accounts" and c.get("logic_diff_status") == "indeterminate"
    ]
    assert indeterminate, payload["changeset"]["changes"]
    assert all(c["kind"] == "logic_changed" for c in indeterminate)
    assert all(c["semantic"] == "indeterminate" for c in indeterminate)

    # (b) confidence is degraded and names the model.
    confidence = payload["confidence"]
    assert confidence["level"] == "partial"
    assert "stg_accounts" in confidence["indeterminate_logic_models"]
    assert confidence["indeterminate_logic"] >= 1

    # (c) the rebuild selection widens: nothing downstream may be skipped.
    selection = payload["selection"]
    assert selection["widened_to_all_reachable"] is True
    assert selection["skippable_models"] == []
    assert "stg_accounts" in selection["rebuild_models"]


def test_impact_identical_catalogs_stamp_structural_diff_degraded(dbt_artifacts, logic_change_base):
    """ONE catalog mounted on both sides => removed/type_changed can never surface.

    ``logic_change_base`` copies the head catalog unchanged, reproducing the real deployment
    where a single prod catalog backs both sides. The report must say so explicitly.
    """
    args = [
        "--manifest",
        str(dbt_artifacts["manifest_path"]),
        "--catalog",
        str(dbt_artifacts["catalog_path"]),
        "--base-manifest",
        logic_change_base["manifest"],
        "--base-catalog",
        logic_change_base["catalog"],
    ]
    result = _run_impact([*args, "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    stamp = payload.get("structural_diff")
    assert stamp is not None, "identical base/head catalogs must stamp the report as degraded"
    assert stamp["status"] == "degraded"
    assert stamp["reason"]
    assert payload["summary"]["structural_diff"] == "degraded"

    # The markdown / PR-comment surface carries a visible honesty line too.
    markdown = _run_impact(args)
    assert markdown.exit_code == 0, markdown.output
    assert "Structural diff degraded" in markdown.output


def test_impact_distinct_catalogs_carry_no_degraded_stamp(dbt_artifacts, base_artifacts):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            base_artifacts["manifest"],
            "--base-catalog",
            base_artifacts["catalog"],
            "--format",
            "json",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "structural_diff" not in payload
    assert "structural_diff" not in payload["summary"]


def test_impact_two_manifest_markdown_default(dbt_artifacts, base_artifacts):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            base_artifacts["manifest"],
            "--base-catalog",
            base_artifacts["catalog"],
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Column-level impact" in result.output
    # New criticality-first layout: a downstream headline instead of a flat "Affected
    # columns" table (columns are split into 🔴 recomputes-logic and 🟢 pass-through).
    assert "downstream" in result.output


def test_impact_identical_manifests_report_no_change(dbt_artifacts):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            str(dbt_artifacts["manifest_path"]),
            "--base-catalog",
            str(dbt_artifacts["catalog_path"]),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "No column changes detected" in result.output


@pytest.fixture
def no_gh_env(monkeypatch):
    """Ensure CI runs of these tests don't resolve a real PR context and post."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(var, raising=False)


def _ci_args(dbt_artifacts, base_artifacts, *extra):
    return [
        "--manifest",
        str(dbt_artifacts["manifest_path"]),
        "--catalog",
        str(dbt_artifacts["catalog_path"]),
        "--base-manifest",
        base_artifacts["manifest"],
        "--base-catalog",
        base_artifacts["catalog"],
        "--ci",
        *extra,
    ]


def test_ci_without_pr_context_skips_comment(dbt_artifacts, base_artifacts, no_gh_env):
    result = _run_impact(_ci_args(dbt_artifacts, base_artifacts))
    # Default --fail-on none -> warn only, exit 0.
    assert result.exit_code == 0, result.output
    assert "no PR context" in result.output
    # Report is still printed to the log.
    assert "Column-level impact" in result.output


def test_ci_fail_on_any_gates_the_check(dbt_artifacts, base_artifacts, no_gh_env):
    result = _run_impact(_ci_args(dbt_artifacts, base_artifacts, "--fail-on", "any"))
    # The synthesized diff retypes/removes a widely-consumed column, so there is
    # downstream impact -> the 'any' gate must fail the check.
    assert result.exit_code == 1, result.output
    assert "fail-on any" in result.output


def test_scope_git_requires_base_manifest(dbt_artifacts):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--scope-git",
            "origin/main",
        ]
    )
    assert result.exit_code == 1
    assert "scope-git" in result.output


def test_scope_git_filters_to_changed_models(dbt_artifacts, base_artifacts, monkeypatch):
    # The two-manifest diff detects a change on stg_accounts, but we scope to a
    # git diff that touched no matching model -> the changeset is emptied.
    from parrant.lineage import changeset

    monkeypatch.setattr(
        changeset,
        "_git_changed_sql_files",
        lambda ref, repo_dir=None, git_head="HEAD": ["macros/only.sql"],
    )
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            base_artifacts["manifest"],
            "--base-catalog",
            base_artifacts["catalog"],
            "--scope-git",
            "origin/main",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "No column changes detected" in result.output


def test_ci_no_change_passes_gate(dbt_artifacts, no_gh_env):
    result = _run_impact(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            str(dbt_artifacts["manifest_path"]),
            "--base-catalog",
            str(dbt_artifacts["catalog_path"]),
            "--ci",
            "--fail-on",
            "critical",
        ]
    )
    # No changes -> nothing to gate on, even a blocking policy passes.
    assert result.exit_code == 0, result.output
