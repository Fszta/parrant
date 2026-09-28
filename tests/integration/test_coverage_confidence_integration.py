"""Integration tests for the coverage/confidence signal in CLI output.

The test-project artifacts are fully built, so coverage must read as ``complete``
(no scary warnings) and impact confidence as ``full``. These tests lock in that
quiet-when-complete behaviour and the strictly-additive JSON shape.
"""

import json

from click.testing import CliRunner

from parrant.cli.main import cli


def _run(dbt_artifacts, select, fmt):
    return CliRunner().invoke(
        cli,
        [
            "--select",
            select,
            "--format",
            fmt,
            "--catalog",
            str(dbt_artifacts["catalog_path"]),
            "--manifest",
            str(dbt_artifacts["manifest_path"]),
        ],
    )


def test_json_carries_top_level_coverage_block(dbt_artifacts):
    result = _run(dbt_artifacts, "stg_transactions.transaction_id+", "json")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert "coverage" in payload, "coverage must be a top-level, additive block"

    coverage = payload["coverage"]
    for key in (
        "models_in_manifest",
        "models_in_catalog",
        "parsed_ok",
        "parse_failed",
        "skipped_no_sql",
        "not_in_catalog_count",
        "failed_models",
        "skipped_models",
        "complete",
    ):
        assert key in coverage

    # The bundled fixture project is fully built -> complete, no gaps.
    assert coverage["complete"] is True
    assert coverage["not_in_catalog_count"] == 0
    assert coverage["parse_failed"] == 0


def test_json_impact_carries_confidence_block(dbt_artifacts):
    result = _run(dbt_artifacts, "stg_transactions.transaction_id+", "json")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert "impact" in payload
    confidence = payload["impact"]["confidence"]

    assert set(confidence) == {
        "reachable_models",
        "resolved_models",
        "unanalyzable_models",
        "no_column_info",
        "parse_failed",
        "no_column_info_models",
        "parse_failed_models",
        "no_column_info_truncated",
        "parse_failed_truncated",
        "partial_edges",
        "partial_edges_models",
        "opaque",
        "opaque_models",
        "opaque_truncated",
        "indeterminate_logic",
        "indeterminate_logic_models",
        "level",
    }
    # Everything reachable is in the catalog and parsed, so confidence is full even
    # though the column does not propagate to every downstream model.
    assert confidence["level"] == "full"
    assert confidence["resolved_models"] <= confidence["reachable_models"]
    # Full confidence means no coverage gap: nothing reachable was unanalyzable.
    assert confidence["unanalyzable_models"] == 0
    assert confidence["no_column_info"] == 0
    assert confidence["parse_failed"] == 0
    # Machine output is never truncated; the lists are complete and match the counts.
    assert confidence["no_column_info_truncated"] is False
    assert confidence["parse_failed_truncated"] is False
    assert len(confidence["no_column_info_models"]) == confidence["no_column_info"]
    assert len(confidence["parse_failed_models"]) == confidence["parse_failed"]


def test_text_prints_quiet_complete_footer(dbt_artifacts):
    result = _run(dbt_artifacts, "stg_transactions.transaction_id+", "text")
    assert result.exit_code == 0, result.output

    # Quiet, reassuring tone on a complete project: says "complete", no lower-bound
    # warning.
    assert "complete." in result.output
    assert "Coverage:" in result.output
    assert "lower bound" not in result.output
