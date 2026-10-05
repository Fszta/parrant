"""
Centralized adapter/dialect normalization for parrant.

This module defines a single source of truth for mapping raw dbt adapter
names to sqlglot dialect names. For example, dbt may report the adapter
"sqlserver" while sqlglot expects the dialect name "tsql".

Most common warehouse adapters (snowflake, bigquery, redshift, databricks,
postgres, duckdb, ...) already share their name with the corresponding
sqlglot dialect, so they resolve correctly without an explicit entry. The
mapping below covers the adapters whose dbt name differs from the sqlglot
dialect name, plus a few identity entries that pin well-supported adapters
to their verified dialect.

Extend ADAPTER_TO_DIALECT as needed to support additional adapters.
"""

import logging

logger = logging.getLogger(__name__)

# Mapping from dbt adapter name (metadata.adapter_type) to sqlglot dialect.
#
# Only entries verified against sqlglot's supported dialects belong here. When
# the dbt adapter name already equals a valid sqlglot dialect (snowflake,
# bigquery, redshift, databricks, postgres, duckdb, spark, trino, presto,
# athena, clickhouse, ...) an explicit identity entry is optional -- the
# fallthrough in normalize_adapter returns the lowercased name unchanged.
ADAPTER_TO_DIALECT: dict[str, str] = {
    # --- adapters whose dbt name differs from the sqlglot dialect name ---
    # The T-SQL family: dbt reports sqlserver/synapse/fabric, sqlglot uses "tsql".
    "sqlserver": "tsql",
    "synapse": "tsql",
    "fabric": "tsql",
    # --- identity pins for common, verified adapters (documented support) ---
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "redshift": "redshift",
    "databricks": "databricks",
    "spark": "spark",
    "trino": "trino",
    "presto": "presto",
    "athena": "athena",
    "postgres": "postgres",
    "duckdb": "duckdb",
}


def _known_sqlglot_dialects() -> set[str]:
    """Return the set of dialect names sqlglot actually supports.

    Derived from sqlglot at runtime so the check stays correct as sqlglot is
    upgraded. Falls back to an empty set if the internal enum is unavailable,
    in which case the unknown-dialect warning is simply skipped.
    """
    try:
        from sqlglot.dialects.dialect import Dialects

        return {member.value for member in Dialects if member.value}
    except Exception:  # pragma: no cover - defensive, sqlglot API drift
        return set()


_KNOWN_DIALECTS: set[str] = _known_sqlglot_dialects()

# Track adapters we have already warned about so the warning fires once per
# unresolved adapter instead of on every column parsed.
_warned_adapters: set[str] = set()


def normalize_adapter(adapter_name: str | None) -> str | None:
    """Normalize a dbt adapter name to a sqlglot dialect name.

    If adapter_name is None or empty, returns it unchanged.
    If there is no mapping defined, returns the lowercased adapter_name.

    When the resolved dialect is not a dialect sqlglot recognizes, a one-time
    WARNING is emitted per adapter so the user gets a visible signal that SQL
    parsing will fall back to sqlglot's default behavior instead of silently
    degrading.
    """
    if not adapter_name:
        return adapter_name
    lower = adapter_name.lower()
    dialect = ADAPTER_TO_DIALECT.get(lower, lower)

    if _KNOWN_DIALECTS and dialect not in _KNOWN_DIALECTS and lower not in _warned_adapters:
        _warned_adapters.add(lower)
        logger.warning(
            "dbt adapter '%s' resolved to dialect '%s', which is not a known "
            "sqlglot dialect; SQL parsing may be less accurate. Consider adding "
            "a mapping in adapter_mapping.ADAPTER_TO_DIALECT.",
            adapter_name,
            dialect,
        )

    return dialect
