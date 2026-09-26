import pytest
from fastapi.testclient import TestClient
from parrant.lineage.display.html.explore import LineageExplorer
from parrant.artifacts.registry import ModelRegistry
from parrant.lineage.service import LineageService
from pathlib import Path


@pytest.fixture
def registry(dbt_artifacts):
    """Create and load a model registry with real data."""
    registry = ModelRegistry(
        str(dbt_artifacts["catalog_path"]), str(dbt_artifacts["manifest_path"])
    )
    registry.load()
    return registry


@pytest.fixture
def lineage_service(dbt_artifacts):
    """Create a LineageService instance."""
    return LineageService(
        catalog_path=Path(dbt_artifacts["catalog_path"]),
        manifest_path=Path(dbt_artifacts["manifest_path"]),
    )


def test_html_display_nodes(lineage_service, registry):
    """Test that the HTML display correctly processes lineage."""
    lineage_explorer = LineageExplorer(host="127.0.0.1", port=8000)
    lineage_explorer.set_lineage_service(lineage_service)

    start_model_name = "stg_transactions"
    start_column_name = "amount"
    model_obj = registry.get_model(start_model_name)
    column_obj = model_obj.columns.get(start_column_name)

    lineage_explorer._set_column_info(column_obj)
    expected_main_node_id = f"col_{start_model_name}_{start_column_name}"
    lineage_explorer.data.main_node = expected_main_node_id

    lineage_explorer._process_lineage_tree(start_model_name, start_column_name)

    data_dict = lineage_explorer.data.model_dump()

    assert data_dict["column_info"] is not None
    assert data_dict["column_info"]["name"] == start_column_name
    assert data_dict["column_info"]["model"] == start_model_name

    assert data_dict["main_node"] == expected_main_node_id

    main_node = next((n for n in data_dict["nodes"] if n["id"] == expected_main_node_id), None)
    assert main_node is not None, f"Main node {expected_main_node_id} not found in graph nodes"
    assert main_node["is_main"] is True, "Main node not marked as main"
    assert main_node["model"] == start_model_name
    assert main_node["label"] == start_column_name

    assert len(data_dict["nodes"]) > 0, "No nodes were generated in the graph"

    assert len(data_dict["edges"]) > 0, "No edges were generated, expected lineage"

    models_in_graph = {node["model"] for node in data_dict["nodes"]}
    assert (
        start_model_name in models_in_graph
    ), f"Starting model '{start_model_name}' not found in graph nodes"


def test_home_route_renders_explorer_page(lineage_service):
    """GET / — the only route that renders a Jinja2 template — returns the explorer page.

    Guards the ``TemplateResponse`` calling convention: starlette 1.6 removed the legacy
    ``(name, context-with-request)`` shim, turning every page load into
    ``TypeError: unhashable type: 'dict'`` (issue #139). Runs in-process so the
    DeprecationWarning-as-error filter in pyproject.toml catches a deprecated calling
    style while it is still just a warning on older starlette."""
    explorer = LineageExplorer(host="127.0.0.1", port=8000)
    explorer.set_lineage_service(lineage_service)

    with TestClient(explorer.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Parrant</title>" in response.text
    # explore_mode context reached the template
    assert "explore-panel" in response.text


def test_lineage_includes_impact_summary(lineage_service):
    """Test that the lineage endpoint includes impact_summary in the response."""
    lineage_explorer = LineageExplorer(host="127.0.0.1", port=8000)
    lineage_explorer.set_lineage_service(lineage_service)

    # Test with a column that has downstream dependencies
    model = "stg_transactions"
    column = "amount"

    lineage_explorer._process_lineage_tree(model, column)

    # Get impact summary - this is what the endpoint does
    impact_data = lineage_service.get_column_impact(model, column)
    if impact_data and "summary" in impact_data:
        lineage_explorer.data.impact_summary = impact_data["summary"]
    else:
        lineage_explorer.data.impact_summary = None

    data = lineage_explorer.data.model_dump(exclude_none=False)

    assert "impact_summary" in data, "Lineage response should include impact_summary field"

    if data["impact_summary"] is not None:
        summary = data["impact_summary"]
        assert "critical_count" in summary, "Impact summary should include critical_count"
        assert "low_impact_count" in summary, "Impact summary should include low_impact_count"
        assert "affected_models" in summary, "Impact summary should include affected_models"
        assert "affected_exposures" in summary, "Impact summary should include affected_exposures"

        assert isinstance(summary["critical_count"], int)
        assert isinstance(summary["low_impact_count"], int)
        assert isinstance(summary["affected_models"], int)
        assert isinstance(summary["affected_exposures"], int)

        # Verify the summary has reasonable values for a column with downstream dependencies
        assert summary["affected_models"] >= 0
        assert summary["affected_exposures"] >= 0
        assert summary["critical_count"] >= 0
        assert summary["low_impact_count"] >= 0


def test_lineage_without_impact_summary(lineage_service):
    """Test that lineage data is present even when impact_summary is None."""
    lineage_explorer = LineageExplorer(host="127.0.0.1", port=8000)
    lineage_explorer.set_lineage_service(lineage_service)

    model = "stg_transactions"
    column = "amount"

    lineage_explorer._process_lineage_tree(model, column)

    # Explicitly set impact_summary to None to test the case where it's not available
    lineage_explorer.data.impact_summary = None

    data = lineage_explorer.data.model_dump(exclude_none=False)

    # Verify that even if impact_summary is None, the lineage data is still present
    assert "impact_summary" in data
    assert data["impact_summary"] is None
    assert "nodes" in data, "Lineage data should still be present"
    assert "edges" in data, "Lineage edges should still be present"
    assert len(data["nodes"]) > 0, "Should have nodes even when impact_summary is None"
    assert len(data["edges"]) > 0, "Should have edges even when impact_summary is None"


def test_upstream_lineage_returns_full_chain(lineage_service):
    """Test that upstream lineage returns the full chain, not just direct dependencies."""
    upstream = lineage_service._get_upstream_lineage("transactions", "country_name")

    assert "stg_countries" in upstream
    assert "country_name" in upstream["stg_countries"]
    assert "raw_countries" in upstream
    assert "name" in upstream["raw_countries"]


def test_upstream_lineage_source_columns_point_to_actual_sources(lineage_service):
    """Test that source_columns point to actual sources, not self."""
    upstream = lineage_service._get_upstream_lineage("transactions", "country_name")

    stg_lineage = upstream["stg_countries"]["country_name"]
    source_cols = stg_lineage.source_columns

    assert "stg_countries.country_name" not in source_cols
    assert any("raw_countries" in src for src in source_cols)


def test_column_nodes_carry_dbt_tests(lineage_service):
    """Every column node exposes a ``tests`` list; a tested column carries its dbt tests so
    the explorer can render the "Show tests" indicators without re-parsing artifacts."""
    explorer = LineageExplorer(host="127.0.0.1", port=8000)
    explorer.set_lineage_service(lineage_service)
    explorer.data = explorer.data.__class__()  # fresh graph

    explorer._process_lineage_tree("stg_transactions", "transaction_id")
    data = explorer.data.model_dump()

    # transaction_id has not_null + unique in the bundled project.
    node = next(n for n in data["nodes"] if n["id"] == "col_stg_transactions_transaction_id")
    assert isinstance(node["tests"], list)
    assert {t["test_name"] for t in node["tests"]} >= {"not_null", "unique"}
    # Every column node carries a tests list (empty, never None, when untested).
    for n in data["nodes"]:
        if n.get("type") == "column":
            assert isinstance(n.get("tests"), list)


def test_column_test_payload_carries_relationships_referenced_side(lineage_service):
    """A relationships test surfaces its referenced (parent) side in the node payload."""
    explorer = LineageExplorer(host="127.0.0.1", port=8000)
    explorer.set_lineage_service(lineage_service)

    tests = explorer._column_tests_payload("stg_transactions", "account_id")
    rel = next(t for t in tests if t["test_name"] == "relationships")
    assert rel["referenced_model"] == "stg_accounts"
    assert rel["referenced_column"] == "account_id"


def test_impact_analysis_enriched_with_column_tests(lineage_service):
    """The impact payload lists the tests covering each affected column, so a reviewer sees
    which guarantees a change threatens."""
    explorer = LineageExplorer(host="127.0.0.1", port=8000)
    explorer.set_lineage_service(lineage_service)

    impact = lineage_service.get_column_impact("stg_transactions", "transaction_id")
    enriched = explorer._enrich_impact_with_tests(impact)

    affected = enriched["affected_columns"]
    assert affected, "expected downstream affected columns for a tested key"
    # Every affected column has a tests list attached.
    for col in affected:
        assert isinstance(col.get("tests"), list)


def test_rowset_dependents_appear_as_distinct_nodes(lineage_service):
    """A filter-only consumer (flagged_transaction_metrics filters transactions.status in a
    WHERE, never projecting it) must appear as a distinct 'rowset' node with the predicate as
    its note — not be silently absent from the graph."""
    explorer = LineageExplorer(host="127.0.0.1", port=8000)
    explorer.set_lineage_service(lineage_service)
    explorer.data = explorer.data.__class__()  # fresh graph

    explorer._process_lineage_tree("transactions", "status")
    data = explorer.data.model_dump()

    rowset = [n for n in data["nodes"] if n.get("type") == "rowset"]
    assert any(
        n["label"] == "flagged_transaction_metrics" for n in rowset
    ), "expected flagged_transaction_metrics as a row-set node"
    node = next(n for n in rowset if n["label"] == "flagged_transaction_metrics")
    assert node["note"] and "status" in node["note"].lower()
    assert any(
        e.get("type") == "rowset" and e["target"] == node["id"] for e in data["edges"]
    ), "expected a rowset edge into the row-set node"
