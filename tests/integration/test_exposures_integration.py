from pathlib import Path

import pytest

from parrant.lineage.display.text import TextDisplay
from parrant.lineage.service import LineageSelector, LineageService


@pytest.fixture
def lineage_service(dbt_artifacts):
    """Create a LineageService instance."""
    return LineageService(
        catalog_path=Path(dbt_artifacts["catalog_path"]),
        manifest_path=Path(dbt_artifacts["manifest_path"]),
    )


def test_display_downstream_with_exposures_does_not_crash(lineage_service, capsys):
    """Regression: display_downstream must render the 'exposures' set key without crashing.

    Downstream lineage returns a dict that mixes {model: {col: lineage}} entries with an
    'exposures' set. display_downstream used to iterate every value as a dict and call
    .items() on the set, raising "'set' object has no attribute 'items'".
    """
    selector = LineageSelector(
        model="transactions", column="transaction_id", upstream=False, downstream=True
    )
    downstream = lineage_service.get_column_info(selector)["downstream"]

    # Sanity: this column's downstream is exactly the case that used to crash.
    assert "exposures" in downstream
    assert isinstance(downstream["exposures"], set)

    # Before the fix this raised AttributeError: 'set' object has no attribute 'items'.
    TextDisplay().display_downstream(downstream)

    out = capsys.readouterr().out
    assert "Downstream dependencies:" in out
    assert "Exposures:" in out
    assert "transactions_dashboard" in out
    assert "api_transactions_endpoint" in out


def test_display_downstream_with_models_and_exposures(lineage_service, capsys):
    """Regression: a downstream dict mixing model entries and the exposures set renders fully."""
    selector = LineageSelector(
        model="stg_transactions", column="transaction_id", upstream=False, downstream=True
    )
    downstream = lineage_service.get_column_info(selector)["downstream"]

    # This column flows into the `transactions` model AND onward to exposures.
    assert "transactions" in downstream
    assert "exposures" in downstream

    TextDisplay().display_downstream(downstream)

    out = capsys.readouterr().out
    assert "Model transactions:" in out
    assert "Exposures:" in out


def test_transactions_lineage_includes_exposures(lineage_service):
    """Test that transactions-related lineage includes both exposures."""
    selector = LineageSelector(
        model="transactions", column="transaction_id", upstream=False, downstream=True
    )
    column_info = lineage_service.get_column_info(selector)
    downstream_lineage = column_info["downstream"]

    assert (
        "exposures" in downstream_lineage
    ), "transactions.transaction_id downstream lineage should include exposures"

    exposures = downstream_lineage.get("exposures", set())
    assert isinstance(exposures, set), "exposures should be a set"

    expected_exposures = {"transactions_dashboard", "api_transactions_endpoint"}

    assert (
        exposures == expected_exposures
    ), f"Expected exposures {expected_exposures}, got {exposures}"

    selector_amount = LineageSelector(
        model="transactions", column="amount", upstream=False, downstream=True
    )
    column_info_amount = lineage_service.get_column_info(selector_amount)
    exposures_amount = column_info_amount["downstream"].get("exposures", set())
    assert (
        exposures_amount == expected_exposures
    ), f"transactions.amount should also include both exposures, got {exposures_amount}"


def test_stg_transactions_lineage_includes_exposures(lineage_service):
    """Test that stg_transactions lineage (which feeds into transactions) includes exposures."""
    # stg_transactions -> transactions -> exposures
    selector = LineageSelector(
        model="stg_transactions", column="transaction_id", upstream=False, downstream=True
    )
    column_info = lineage_service.get_column_info(selector)
    downstream_lineage = column_info["downstream"]

    assert (
        "transactions" in downstream_lineage
    ), "stg_transactions.transaction_id should flow to transactions model"

    exposures = downstream_lineage.get("exposures", set())
    expected_exposures = {"transactions_dashboard", "api_transactions_endpoint"}

    assert (
        exposures == expected_exposures
    ), f"stg_transactions.transaction_id should flow through transactions to both exposures, got {exposures}"


def test_account_tiering_lineage_includes_report(lineage_service):
    """Test that account_tiering-related lineage includes the report exposure."""
    selector = LineageSelector(
        model="accounts_tiering", column="account_id", upstream=False, downstream=True
    )
    column_info = lineage_service.get_column_info(selector)
    downstream_lineage = column_info["downstream"]

    exposures = downstream_lineage.get("exposures", set())
    expected_exposures = {"account_tiering_report"}

    assert (
        exposures == expected_exposures
    ), f"accounts_tiering.account_id should include account_tiering_report, got {exposures}"


def test_int_monthly_account_metrics_lineage_includes_report(lineage_service):
    """Test that int_monthly_account_metrics lineage includes account_tiering_report."""
    # int_monthly_account_metrics -> accounts_tiering -> account_tiering_report
    selector = LineageSelector(
        model="int_monthly_account_metrics", column="account_id", upstream=False, downstream=True
    )
    column_info = lineage_service.get_column_info(selector)
    downstream_lineage = column_info["downstream"]

    exposures = downstream_lineage.get("exposures", set())
    expected_exposures = {"account_tiering_report"}

    assert (
        exposures == expected_exposures
    ), f"int_monthly_account_metrics.account_id should include account_tiering_report, got {exposures}"


def test_unrelated_lineage_excludes_transactions_exposures(lineage_service):
    """Test that unrelated lineage does not include transactions exposures."""
    # account_holder column in int_monthly_account_metrics flows to accounts_tiering,
    # but NOT to transactions, so should NOT include transactions exposures
    selector = LineageSelector(
        model="int_monthly_account_metrics",
        column="account_holder",
        upstream=False,
        downstream=True,
    )
    column_info = lineage_service.get_column_info(selector)
    downstream_lineage = column_info["downstream"]

    exposures = downstream_lineage.get("exposures", set())

    assert (
        "transactions_dashboard" not in exposures
    ), "account_holder lineage should NOT include transactions_dashboard"
    assert (
        "api_transactions_endpoint" not in exposures
    ), "account_holder lineage should NOT include api_transactions_endpoint"


def test_exposures_not_in_upstream_lineage(lineage_service):
    """Test that exposures are not included in upstream lineage (only downstream)."""
    selector = LineageSelector(
        model="transactions", column="transaction_id", upstream=True, downstream=False
    )
    column_info = lineage_service.get_column_info(selector)
    upstream_lineage = column_info["upstream"]

    assert "exposures" not in upstream_lineage, "Upstream lineage should not include exposures"


def test_exposure_metadata_in_lineage_explorer(lineage_service):
    """Test that exposure metadata is correctly included in lineage explorer."""
    from parrant.lineage.display.html.explore import LineageExplorer

    lineage_explorer = LineageExplorer(host="127.0.0.1", port=8000)
    lineage_explorer.set_lineage_service(lineage_service)

    lineage_explorer._process_lineage_tree("transactions", "transaction_id")

    data_dict = lineage_explorer.data.model_dump()
    exposure_nodes = [node for node in data_dict["nodes"] if node.get("type") == "exposure"]

    expected_exposure_names = {"transactions_dashboard", "api_transactions_endpoint"}

    actual_exposure_names = {node["model"] for node in exposure_nodes}
    assert (
        actual_exposure_names == expected_exposure_names
    ), f"Expected exposure nodes {expected_exposure_names}, got {actual_exposure_names}"

    # Check that exposure edges are created
    exposure_edges = [edge for edge in data_dict["edges"] if edge.get("type") == "exposure"]

    assert len(exposure_edges) > 0, "Expected exposure edges to be created in the graph"
