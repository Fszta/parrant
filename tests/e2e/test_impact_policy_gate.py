"""End-to-end: the installed ``parrant impact`` console script honours the policy gate.

Runs the real entry point as a subprocess (not the in-process CliRunner) so the actual process
exit code under ``--fail-on policy`` is proven: a BLOCK verdict -> non-zero, a no-change run -> 0.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
_POLICY = str(REPO_ROOT / "tests" / "resources" / "policies" / "block_on_removed.yml")
_NAIVE_PII_POLICY = str(REPO_ROOT / "tests" / "resources" / "policies" / "naive_pii.yml")


def _load(path):
    with open(path) as f:
        return json.load(f)


def _find_catalog_node(catalog, model_name):
    for node_id, node in catalog["nodes"].items():
        if node.get("metadata", {}).get("name", "").lower() == model_name:
            return node_id
    raise AssertionError(f"{model_name} not in catalog")


@pytest.fixture
def mutated_base(dbt_artifacts, tmp_path):
    """A base whose stg_accounts still has ``legacy_col`` -> head shows a REMOVED change."""
    catalog = copy.deepcopy(_load(dbt_artifacts["catalog_path"]))
    manifest = copy.deepcopy(_load(dbt_artifacts["manifest_path"]))
    node_id = _find_catalog_node(catalog, "stg_accounts")
    catalog["nodes"][node_id]["columns"]["legacy_col"] = {"name": "legacy_col", "type": "TEXT"}
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return {"catalog": str(tmp_path / "catalog.json"), "manifest": str(tmp_path / "manifest.json")}


def _run(args, env_clean=True):
    env = None
    if env_clean:
        import os

        env = {k: v for k, v in os.environ.items()}
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY", "GITHUB_EVENT_PATH"):
            env.pop(var, None)
    return subprocess.run(
        ["poetry", "run", "parrant", "impact", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )


def test_fail_on_policy_block_exits_nonzero(dbt_artifacts, mutated_base):
    result = _run(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            mutated_base["manifest"],
            "--base-catalog",
            mutated_base["catalog"],
            "--ci",
            "--fail-on",
            "policy",
            "--policy",
            _POLICY,
        ]
    )
    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_naive_pii_block_renders_as_fail_safe_not_proven(dbt_artifacts, mutated_base):
    """e2e comment snapshot: the naive `meta.pii eq true` policy blocks via a fail-safe UNKNOWN
    (the column carries no `pii` meta), so the PR comment MUST render its block as
    fail-safe-driven — never as a proven match. Locks in the honesty distinction end-to-end."""
    result = _run(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            mutated_base["manifest"],
            "--base-catalog",
            mutated_base["catalog"],
            "--policy",
            _NAIVE_PII_POLICY,
        ]
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    md = result.stdout
    # The verdict blocks, and the "why this verdict" section explains it.
    assert "Policy verdict — BLOCK" in md
    assert "Why this verdict" in md
    assert "naive-pii-guard" in md
    # The load-bearing honesty distinction: this block fired on a fail-safe default, not a proof.
    assert "fired on a fail-safe default" in md
    assert "meta missing" in md
    # The naive-pii-guard line must NOT be dressed up as a proven match.
    pii_line = next(ln for ln in md.splitlines() if "naive-pii-guard" in ln)
    assert "proven match" not in pii_line


def test_fail_on_policy_no_change_exits_zero(dbt_artifacts):
    result = _run(
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
            "policy",
            "--policy",
            _POLICY,
        ]
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


_WARN_PII_POLICY = str(REPO_ROOT / "tests" / "resources" / "policies" / "warn_pii_fail_closed.yml")
def test_warn_policy_unknown_exit_code_invariant(dbt_artifacts, mutated_base):
    """Issue #124 exit-code invariance: a warn-only fail_closed policy whose rule stays UNKNOWN
    exits 0 under `--fail-on policy` — exactly as before the unproven telemetry (a warn-only
    policy can never block; the new surface is report-only)."""
    result = _run(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            mutated_base["manifest"],
            "--base-catalog",
            mutated_base["catalog"],
            "--ci",
            "--fail-on",
            "policy",
            "--policy",
            _WARN_PII_POLICY,
        ]
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_warn_policy_unknown_surfaces_unproven_in_report(dbt_artifacts, mutated_base):
    """The suppressed warn-rule UNKNOWN is no longer silent: the Markdown report (the same body
    posted as the sticky PR comment) carries the honest unproven line, and the JSON verdict
    carries the machine-readable record."""
    md_result = _run(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            mutated_base["manifest"],
            "--base-catalog",
            mutated_base["catalog"],
            "--policy",
            _WARN_PII_POLICY,
        ]
    )
    assert md_result.returncode == 0, f"stdout={md_result.stdout}\nstderr={md_result.stderr}"
    md = md_result.stdout
    assert "unproven warn-rule condition" in md
    assert "warn-pii-guard" in md
    # The verdict itself stays ALLOW: nothing fired, nothing blocked.
    assert "Policy verdict — ALLOW" in md

    json_result = _run(
        [
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--base-manifest",
            mutated_base["manifest"],
            "--base-catalog",
            mutated_base["catalog"],
            "--policy",
            _WARN_PII_POLICY,
            "--format",
            "json",
        ]
    )
    assert json_result.returncode == 0, f"stderr={json_result.stderr}"
    verdict = json.loads(json_result.stdout)["policy_verdict"]
    assert verdict["decision"] == "allow"
    assert verdict["unproven_count"] >= 1
    assert any(rec["rule_id"] == "warn-pii-guard" for rec in verdict["unproven"])
