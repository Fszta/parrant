"""End-to-end tests for lineage API."""

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
import requests

EXPECTED_CRYPTO_PORTFOLIO_COLUMNS = 37
TEST_MODEL = "int_trade_flow"
TEST_COLUMN = "trade_status"
TARGET_MODEL = "crypto_portfolio_daily"


class ServerError(Exception):
    """Raised when server fails to start or respond."""


@contextmanager
def lineage_server(catalog_path: Path, manifest_path: Path, port: int) -> Iterator[int]:
    """Context manager for starting and stopping the lineage server."""
    process = None
    try:
        process = subprocess.Popen(
            [
                "poetry",
                "run",
                "parrant",
                "--explore",
                "--catalog",
                str(catalog_path),
                "--manifest",
                str(manifest_path),
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parent.parent.parent,
        )

        if not _wait_for_server(port, max_retries=15, delay=1.0):
            stdout, stderr = process.communicate(timeout=5)
            raise ServerError(
                f"Server failed to start on port {port}. "
                f"STDOUT: {stdout.decode() if stdout else 'None'}. "
                f"STDERR: {stderr.decode() if stderr else 'None'}"
            )

        yield port

    finally:
        if process:
            _terminate_process(process)


def _wait_for_server(port: int, max_retries: int = 15, delay: float = 1.0) -> bool:
    """Wait for the server to be ready by checking the /api/models endpoint."""
    for attempt in range(max_retries):
        try:
            response = requests.get(f"http://127.0.0.1:{port}/api/models", timeout=2)
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        if attempt < max_retries - 1:
            time.sleep(delay)

    return False


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Safely terminate a subprocess with timeout."""
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def _get_lineage_response(port: int, model: str, column: str) -> dict[str, Any]:
    """Get lineage response from the API endpoint."""
    endpoint = f"http://127.0.0.1:{port}/api/lineage/{model}/{column}"
    try:
        response = requests.get(endpoint, timeout=30)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
    except requests.exceptions.RequestException as e:
        raise AssertionError(f"Failed to get lineage from {endpoint}: {e}")


def _extract_column_set(data: dict[str, Any]) -> set[str]:
    """Extract all column identifiers from the API response."""
    if not data or "nodes" not in data:
        return set()

    columns = set()
    for node in data.get("nodes", []):
        if node.get("type") == "column":
            model = node.get("model", "")
            column = node.get("label", "")
            if model and column:
                columns.add(f"{model}.{column}")

    return columns


def _count_columns_by_model(data: dict[str, Any], model_name: str) -> int:
    """Count columns for a specific model in the response."""
    if not data or "nodes" not in data:
        return 0

    return sum(
        1
        for node in data.get("nodes", [])
        if node.get("type") == "column" and node.get("model") == model_name
    )


@pytest.fixture
def server_port() -> int:
    """Get a free port for testing."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return cast(int, port)


class ResultDict(TypedDict):
    restart: int
    total_columns: int
    target_model_columns: int
    columns: set[str]


def test_lineage_api_determinism_across_restarts(
    dbt_artifacts: dict[str, Any], server_port: int
) -> None:
    """Verify lineage API returns identical results across server restarts."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])
    num_restarts = 3

    results: list[ResultDict] = []

    for restart_num in range(num_restarts):
        with lineage_server(catalog_path, manifest_path, server_port) as port:
            response_data = _get_lineage_response(port, TEST_MODEL, TEST_COLUMN)

            all_columns = _extract_column_set(response_data)
            target_model_count = _count_columns_by_model(response_data, TARGET_MODEL)

            results.append(
                ResultDict(
                    restart=restart_num + 1,
                    total_columns=len(all_columns),
                    target_model_columns=target_model_count,
                    columns=all_columns,
                )
            )

        if restart_num < num_restarts - 1:
            time.sleep(0.5)

    assert len(results) == num_restarts, f"Expected {num_restarts} results, got {len(results)}"

    first_result = results[0]

    for result in results[1:]:
        assert result["total_columns"] == first_result["total_columns"], (
            f"Restart {result['restart']} total columns ({result['total_columns']}) "
            f"differs from restart 1 ({first_result['total_columns']})"
        )

        assert result["target_model_columns"] == first_result["target_model_columns"], (
            f"Restart {result['restart']} {TARGET_MODEL} columns "
            f"({result['target_model_columns']}) differs from restart 1 "
            f"({first_result['target_model_columns']})"
        )

        assert result["columns"] == first_result["columns"], (
            f"Restart {result['restart']} column set differs from restart 1. "
            f"Missing: {sorted(first_result['columns'] - result['columns'])}. "
            f"Extra: {sorted(result['columns'] - first_result['columns'])}"
        )


class RequestResultDict(TypedDict):
    request: int
    total_columns: int
    columns: set[str]


def test_lineage_api_determinism_within_instance(
    dbt_artifacts: dict[str, Any], server_port: int
) -> None:
    """Verify multiple requests to the same server instance return identical results."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])
    num_requests = 5

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        results: list[RequestResultDict] = []

        for request_num in range(num_requests):
            response_data = _get_lineage_response(port, TEST_MODEL, TEST_COLUMN)
            all_columns = _extract_column_set(response_data)

            results.append(
                RequestResultDict(
                    request=request_num + 1,
                    total_columns=len(all_columns),
                    columns=all_columns,
                )
            )

            if request_num < num_requests - 1:
                time.sleep(0.2)

        assert len(results) == num_requests, f"Expected {num_requests} results, got {len(results)}"

        first_result = results[0]

        for result in results[1:]:
            assert result["total_columns"] == first_result["total_columns"], (
                f"Request {result['request']} total columns ({result['total_columns']}) "
                f"differs from request 1 ({first_result['total_columns']})"
            )

            assert result["columns"] == first_result["columns"], (
                f"Request {result['request']} column set differs from request 1. "
                f"Missing: {sorted(first_result['columns'] - result['columns'])}. "
                f"Extra: {sorted(result['columns'] - first_result['columns'])}"
            )


def test_lineage_api_column_count(dbt_artifacts: dict[str, Any], server_port: int) -> None:
    """Verify the API returns the expected number of columns for the target model."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        response_data = _get_lineage_response(port, TEST_MODEL, TEST_COLUMN)
        target_model_count = _count_columns_by_model(response_data, TARGET_MODEL)

        assert target_model_count == EXPECTED_CRYPTO_PORTFOLIO_COLUMNS, (
            f"Expected {EXPECTED_CRYPTO_PORTFOLIO_COLUMNS} columns in {TARGET_MODEL}, "
            f"got {target_model_count}"
        )


def test_snapshot_api_support(dbt_artifacts: dict[str, Any], server_port: int) -> None:
    """Verify that snapshots are accessible through the API and have correct resource_type."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        models_response = requests.get(f"http://127.0.0.1:{port}/api/models", timeout=10)
        models_response.raise_for_status()
        models_data = models_response.json()

        snapshot_found = False
        snapshot_resource_type = None

        def find_snapshot_in_tree(nodes: list) -> None:
            nonlocal snapshot_found, snapshot_resource_type
            for node in nodes:
                if node.get("type") == "model":
                    resource_type = node.get("resource_type")
                    name = node.get("name") or node.get("display_name", "")
                    if name and "snapshot" in name.lower():
                        snapshot_found = True
                        snapshot_resource_type = resource_type
                        return
                elif node.get("type") == "folder" and "children" in node:
                    find_snapshot_in_tree(node["children"])

        find_snapshot_in_tree(models_data)

        if not snapshot_found:
            all_resource_types = set()

            def collect_resource_types(nodes: list) -> None:
                for node in nodes:
                    if node.get("type") == "model":
                        rt = node.get("resource_type")
                        if rt:
                            all_resource_types.add(rt)
                    elif node.get("type") == "folder" and "children" in node:
                        collect_resource_types(node["children"])

            collect_resource_types(models_data)
            pytest.skip(
                f"No snapshot found in API models response. "
                f"Found resource types: {all_resource_types}. "
                f"Ensure dbt snapshot has been run."
            )

        assert (
            snapshot_resource_type == "snapshot"
        ), f"Snapshot should have resource_type 'snapshot', got '{snapshot_resource_type}'"

        snapshot_name = "accounts_snapshot"
        snapshot_column = "account_id"

        try:
            lineage_response = requests.get(
                f"http://127.0.0.1:{port}/api/lineage/{snapshot_name}/{snapshot_column}", timeout=30
            )
            assert lineage_response.status_code in [
                200,
                400,
            ], f"Lineage endpoint should return 200 or 400, got {lineage_response.status_code}"
        except requests.exceptions.RequestException as e:
            pytest.skip(f"Could not test snapshot lineage endpoint: {e}")


def test_home_page_renders(dbt_artifacts: dict[str, Any], server_port: int) -> None:
    """The root page must render — it is the only template-rendering route, so API-only
    coverage misses it entirely (issue #139 shipped a server that answered every /api/*
    call but 500'd on the page users actually open)."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        response = requests.get(f"http://127.0.0.1:{port}/", timeout=10)

        assert response.status_code == 200, f"GET / returned {response.status_code}"
        assert "text/html" in response.headers.get("content-type", "")
        assert "<title>Parrant</title>" in response.text


def test_coverage_endpoint(dbt_artifacts: dict[str, Any], server_port: int) -> None:
    """Verify /api/coverage exposes the artifact coverage block to the explorer UI."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        response = requests.get(f"http://127.0.0.1:{port}/api/coverage", timeout=10)
        response.raise_for_status()
        coverage = response.json()

        for field in (
            "models_in_manifest",
            "models_in_catalog",
            "parsed_ok",
            "complete",
        ):
            assert field in coverage, f"Coverage missing '{field}': {coverage}"
        assert isinstance(coverage["complete"], bool)
        assert coverage["models_in_manifest"] >= coverage["models_in_catalog"]


def test_lineage_response_includes_confidence(
    dbt_artifacts: dict[str, Any], server_port: int
) -> None:
    """Verify /api/lineage carries the impact confidence block in impact_summary."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        response_data = _get_lineage_response(port, TEST_MODEL, TEST_COLUMN)

        summary = response_data.get("impact_summary")
        assert summary is not None, "Expected impact_summary in lineage response"
        confidence = summary.get("confidence")
        assert confidence is not None, f"Expected confidence in impact_summary: {summary}"
        assert confidence.get("level") in ("full", "partial")
        assert "reachable_models" in confidence
        assert "resolved_models" in confidence


def test_upstream_lineage_returns_full_chain(
    dbt_artifacts: dict[str, Any], server_port: int
) -> None:
    """Verify API returns full upstream chain with proper edges."""
    catalog_path = Path(dbt_artifacts["catalog_path"])
    manifest_path = Path(dbt_artifacts["manifest_path"])

    with lineage_server(catalog_path, manifest_path, server_port) as port:
        response_data = _get_lineage_response(port, "accounts_tiering", "country_id")

        models_in_response = {
            node["model"] for node in response_data.get("nodes", []) if node.get("type") == "column"
        }
        expected_models = {
            "accounts_tiering",
            "int_monthly_account_metrics",
            "int_transactions_enriched",
            "stg_accounts",
            "raw_accounts",
        }
        assert expected_models.issubset(
            models_in_response
        ), f"Missing upstream models. Expected {expected_models}, got {models_in_response}"

        edges = response_data.get("edges", [])
        lineage_edges = [e for e in edges if e.get("type") == "lineage"]

        edge_targets = {e["target"] for e in lineage_edges}
        assert "col_accounts_tiering_country_id" in edge_targets, "Missing edge to main node"
        assert (
            "col_int_monthly_account_metrics_country_id" in edge_targets
        ), "Missing intermediate edge"
        assert "col_stg_accounts_country_id" in edge_targets, "Missing edge to stg model"
