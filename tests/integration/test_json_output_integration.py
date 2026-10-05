"""Integration tests for the machine-readable ``--format json`` CLI output.

The tool is intended to be AI-first: automation and agents need a single,
self-contained, machine-readable document describing a column's lineage and
downstream impact rather than human-formatted text.
"""

import json

from click.testing import CliRunner

from parrant.cli.main import cli


def _run_json(dbt_artifacts, select):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--select",
            select,
            "--format",
            "json",
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
        ],
    )
    return result


def test_json_output_is_valid_and_structured(dbt_artifacts):
    result = _run_json(dbt_artifacts, "accounts_tiering.account_id")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)

    assert payload["model"] == "accounts_tiering"
    assert payload["column"] == "account_id"
    # Stable envelope: both directions plus impact are present for a mid-DAG column.
    for section in ("upstream", "downstream"):
        assert section in payload
        for key in ("models", "sources", "direct_refs", "exposures"):
            assert key in payload[section]


def test_json_upstream_contains_serialized_lineage(dbt_artifacts):
    result = _run_json(dbt_artifacts, "+accounts_tiering.account_id")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    upstream_models = payload["upstream"]["models"]
    assert upstream_models, "expected upstream model lineage"

    # Every serialized lineage entry must carry the core lineage fields as JSON.
    for columns in upstream_models.values():
        for lineage in columns.values():
            assert isinstance(lineage["source_columns"], list)
            assert lineage["transformation_type"] in ("direct", "renamed", "derived")


def test_json_downstream_includes_impact_analysis(dbt_artifacts):
    result = _run_json(dbt_artifacts, "accounts_tiering.account_id")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert "impact" in payload
    summary = payload["impact"]["summary"]
    for metric in (
        "affected_models",
        "affected_columns",
        "affected_exposures",
        "critical_count",
        "low_impact_count",
    ):
        assert metric in summary
        assert isinstance(summary[metric], int)


def test_json_upstream_only_has_no_impact(dbt_artifacts):
    """Impact analysis is downstream-only; upstream-only queries must omit it."""
    result = _run_json(dbt_artifacts, "+accounts_tiering.account_id")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert "impact" not in payload
    assert "downstream" not in payload
