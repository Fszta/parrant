"""Regression guard: a ``select *`` passthrough intermediate that is CATALOG-MISSING must not
drop the column-lineage edge — otherwise a downstream ``inferred_meta.*`` fold silently resolves
to UNKNOWN and a fail-closed PII policy is disarmed.

This exercises the REAL SQLGlot-backed provider (NOT the FakeRegistry stub) over a synthetic
manifest.json + catalog.json that faithfully mirror the production chain:

    account_holders.account_holder_email           (mart, config.grants.select=[ANALYST_PRD])
      <- dim_account_holders_pii.account_holder_email          (import CTE + explicit select)
        <- stg_account_contract__account_holders.account_holder_email
                                                    (SCD-1 collapse: `select * from <scd_1 cte>`,
                                                     the cte = `qualify row_number() over cdc_history`)
          <- stg_account_contract__account_holders_cdc_history.account_holder_email  (meta.pii:true)

Only the changed mart is catalog-backed (a deferred / partial CI build), so the collapse is
catalog-missing. Its compiled SQL is a pure ``select *``, which parses to NO explicit columns —
so the ONLY way its ``account_holder_email`` column (and its edge up to the cdc_history seed) can
exist is via the star-source passthrough materialising it. Before the fix that passthrough only
attached lineage to pre-existing columns and created none, so the column vanished, the fold went
UNKNOWN, and the ``warn``/``fail_closed`` PII rule did NOT fire (ALLOW — a silent PII exposure).
"""

import json

from parrant.lineage.changeset import ChangeKind, ColumnChange
from parrant.lineage.policy import MetaIndex, evaluate_policy, parse_policy
from parrant.lineage.sqlglot_provider import build_sqlglot_provider
from parrant.models.schema import GateDecision, SemanticChangeKind

DB, SCH = "analytics", "main"
CDC = "stg_account_contract__account_holders_cdc_history"
COLLAPSE = "stg_account_contract__account_holders"
DIM = "dim_account_holders_pii"
MART = "account_holders"
COL = "account_holder_email"

_CDC_SQL = f"""with
source as (select * from "{DB}"."{SCH}"."raw_account_holders"),
renamed as (
    select
        cast(id as varchar) as account_holder_id,
        email as account_holder_email,
        cast(created_at as timestamp) as account_holder_created_at
    from source
)
select * from renamed"""

# The SCD-1 collapse: `select * from <cte>` where the cte is the macro-generated
# `qualify row_number() ...` over the cdc_history relation. A pure star passthrough.
_COLLAPSE_SQL = f"""with
account_scd_1 as (
    select *
    from "{DB}"."{SCH}"."{CDC}"
    qualify row_number() over (
        partition by account_holder_id order by account_holder_created_at desc
    ) = 1
)
select * from account_scd_1"""

_DIM_SQL = f"""with
{COLLAPSE} as (select * from "{DB}"."{SCH}"."{COLLAPSE}")
select account_holder_id, account_holder_email
from {COLLAPSE}"""

_MART_SQL = f"""with
{DIM} as (select * from "{DB}"."{SCH}"."{DIM}")
select account_holder_id, account_holder_email
from {DIM}"""


def _manifest_node(name, sql, columns, config=None, col_meta=None):
    cols = {}
    for c in columns:
        cols[c] = {"name": c, "meta": (col_meta or {}).get(c, {})}
    cfg = {"materialized": "table"}
    if config:
        cfg.update(config)
    return {
        "unique_id": f"model.analytics.{name}",
        "name": name,
        "resource_type": "model",
        "package_name": "analytics",
        "path": f"{name}.sql",
        "original_file_path": f"models/{name}.sql",
        "schema": SCH,
        "database": DB,
        "language": "sql",
        "compiled_code": sql,
        "depends_on": {"nodes": []},
        "config": cfg,
        "columns": cols,
    }


def _catalog_node(name, columns):
    return {
        "unique_id": f"model.analytics.{name}",
        "metadata": {"name": name, "schema": SCH, "database": DB, "type": "BASE TABLE"},
        "columns": {c: {"name": c, "type": "TEXT", "index": i} for i, c in enumerate(columns)},
    }


_COLS = ["account_holder_id", "account_holder_email", "account_holder_created_at"]
_MART_COLS = ["account_holder_id", "account_holder_email"]


def _build_provider(tmp_path):
    """A deferred/partial CI build: only the changed mart is catalog-backed."""
    manifest = {
        "metadata": {"adapter_type": "snowflake"},
        "nodes": {
            f"model.analytics.{CDC}": _manifest_node(
                CDC, _CDC_SQL, _COLS, col_meta={COL: {"pii": True}}
            ),
            f"model.analytics.{COLLAPSE}": _manifest_node(COLLAPSE, _COLLAPSE_SQL, _COLS),
            f"model.analytics.{DIM}": _manifest_node(DIM, _DIM_SQL, _MART_COLS),
            f"model.analytics.{MART}": _manifest_node(
                MART, _MART_SQL, _MART_COLS, config={"grants": {"select": ["ANALYST_PRD"]}}
            ),
        },
        "sources": {},
        "exposures": {},
    }
    # Deferred build: only the changed mart is in the fresh catalog; upstreams are not rebuilt.
    catalog = {
        "metadata": {},
        "nodes": {f"model.analytics.{MART}": _catalog_node(MART, _MART_COLS)},
    }

    m = tmp_path / "manifest.json"
    c = tmp_path / "catalog.json"
    m.write_text(json.dumps(manifest))
    c.write_text(json.dumps(catalog))
    provider = build_sqlglot_provider(str(c), str(m), adapter_override="snowflake")
    return provider


def test_catalog_missing_star_passthrough_keeps_pii_edge(tmp_path):
    """The full column-lineage chain must resolve through the catalog-missing ``select *``
    collapse, so inferred_meta.pii folds to TRUE at the mart."""
    provider = _build_provider(tmp_path)

    # The collapse is catalog-missing yet its star-passthrough column must be materialised
    # WITH an edge up to the cdc_history seed.
    edges = provider.get_column_lineage(COLLAPSE, COL)
    assert edges, "catalog-missing `select *` collapse dropped its column edge"
    assert {s for e in edges for s in e.source_columns} == {f"{CDC}.{COL}"}

    # The fold reaches the seed's pii:true through every hop.
    idx = MetaIndex(provider)
    for model in (MART, DIM, COLLAPSE):
        lk = idx.inferred_meta(model, COL, "pii")
        assert lk.present is True and lk.value is True, f"{model} folded to {lk}"


def test_pii_warn_rule_fires_under_deferred_build(tmp_path):
    """The production PII-exposure rule (``action: warn``, ``on_missing_meta: fail_closed``) must
    FIRE (WARN) when a PII column is surfaced into an analyst-granted mart — even when the upstream
    is deferred out of the catalog. Before the fix the broken edge -> UNKNOWN, and a fail-closed
    *warn* rule does not fire on UNKNOWN, so the gate silently ALLOWED the exposure."""
    provider = _build_provider(tmp_path)
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_closed"},
            "rules": [
                {
                    "id": "pii-exposure",
                    "scope": "change",
                    "predicate": {
                        "all": [
                            {"inferred_meta": {"key": "pii", "op": "is_true"}},
                            {
                                "config": {
                                    "key": "grants.select",
                                    "op": "not_subset_of",
                                    "value": [
                                        "COMPLIANCE",
                                        "COMPLIANCE_PRD",
                                        "PAYMENT_OPS",
                                        "PAYMENT_OPS_PRD",
                                    ],
                                }
                            },
                        ]
                    },
                    "action": [{"type": "warn"}],
                }
            ],
        }
    )
    change = ColumnChange(MART, COL, ChangeKind.ADDED, semantic=SemanticChangeKind.MEANING_CHANGED)
    impact = {
        "by_change": [
            {
                "model": MART,
                "column": COL,
                "kind": ChangeKind.ADDED.value,
                "resolved": True,
                "reached_models": [],
                "reached_columns": [],
                "reached_exposures": [],
            }
        ]
    }
    verdict = evaluate_policy([change], impact, provider, policy)
    assert verdict.decision is GateDecision.WARN
    assert verdict.fired_rules == 1
    assert verdict.skipped_missing_meta == 0
