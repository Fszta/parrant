"""Tests for adapter_mapping module."""

import logging

import pytest

from parrant.artifacts import adapter_mapping
from parrant.artifacts.adapter_mapping import (
    ADAPTER_TO_DIALECT,
    normalize_adapter,
)


@pytest.fixture(autouse=True)
def _reset_warned_adapters():
    """Ensure the one-time warning state does not leak between tests."""
    adapter_mapping._warned_adapters.clear()
    yield
    adapter_mapping._warned_adapters.clear()


class TestAdapterMapping:
    """Test adapter name normalization."""

    def test_sqlserver_maps_to_tsql(self):
        """Test that sqlserver adapter maps to tsql dialect."""
        assert normalize_adapter("sqlserver") == "tsql"
        assert normalize_adapter("SQLSERVER") == "tsql"
        assert normalize_adapter("SQLServer") == "tsql"

    def test_tsql_family_maps_to_tsql(self):
        """Synapse and Fabric adapters also use the tsql dialect."""
        assert normalize_adapter("synapse") == "tsql"
        assert normalize_adapter("fabric") == "tsql"

    @pytest.mark.parametrize(
        "adapter,expected",
        [
            ("snowflake", "snowflake"),
            ("bigquery", "bigquery"),
            ("redshift", "redshift"),
            ("databricks", "databricks"),
            ("spark", "spark"),
            ("trino", "trino"),
            ("presto", "presto"),
            ("athena", "athena"),
            ("postgres", "postgres"),
            ("duckdb", "duckdb"),
        ],
    )
    def test_common_adapters_map_to_valid_dialects(self, adapter, expected):
        """Common warehouse adapters resolve to their sqlglot dialect."""
        assert normalize_adapter(adapter) == expected

    def test_all_mapped_targets_are_valid_sqlglot_dialects(self):
        """Every configured target must be a real sqlglot dialect."""
        known = adapter_mapping._known_sqlglot_dialects()
        assert known, "expected sqlglot to expose its dialects"
        for adapter, dialect in ADAPTER_TO_DIALECT.items():
            assert dialect in known, f"{adapter} -> {dialect} is not a sqlglot dialect"

    def test_unmapped_but_valid_adapter_returns_lowercase(self):
        """An unmapped adapter that is still a valid dialect passes through."""
        # clickhouse has no explicit entry but is a valid sqlglot dialect.
        assert "clickhouse" not in ADAPTER_TO_DIALECT
        assert normalize_adapter("ClickHouse") == "clickhouse"

    def test_none_and_empty_strings(self):
        """Test that None and empty strings are handled gracefully."""
        assert normalize_adapter(None) is None
        assert normalize_adapter("") == ""

    def test_case_insensitivity(self):
        """Test that adapter names are case insensitive."""
        assert normalize_adapter("SNOWFLAKE") == "snowflake"
        assert normalize_adapter("BigQuery") == "bigquery"

    def test_adapter_to_dialect_mapping(self):
        """Test that the mapping dictionary contains expected entries."""
        assert "sqlserver" in ADAPTER_TO_DIALECT
        assert ADAPTER_TO_DIALECT["sqlserver"] == "tsql"


class TestUnknownDialectWarning:
    """Test the one-time warning for unresolved dialects."""

    def test_unknown_adapter_warns(self, caplog):
        """An adapter resolving to an unknown dialect emits a WARNING."""
        with caplog.at_level(logging.WARNING, logger=adapter_mapping.__name__):
            result = normalize_adapter("vertica")
        assert result == "vertica"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "vertica" in warnings[0].getMessage()

    def test_unknown_adapter_warns_only_once(self, caplog):
        """Repeated calls for the same unknown adapter warn a single time."""
        with caplog.at_level(logging.WARNING, logger=adapter_mapping.__name__):
            normalize_adapter("vertica")
            normalize_adapter("vertica")
            normalize_adapter("VERTICA")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_known_adapter_does_not_warn(self, caplog):
        """Adapters that map cleanly must not emit a warning."""
        with caplog.at_level(logging.WARNING, logger=adapter_mapping.__name__):
            normalize_adapter("snowflake")
            normalize_adapter("sqlserver")
            normalize_adapter("clickhouse")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
