"""unit tests — the offline relation join and the ``(model,column) -> card -> dashboard``
reach index. Pure (no dbt build), so they run under ``test-unit``."""

from __future__ import annotations

from datetime import datetime, timezone

from parrant.metabase.join import build_relation_index, normalize_relation
from parrant.metabase.reach import (
    MetabaseReach,
    build_reach_confidence,
    dashboard_reach_name,
)
from parrant.models.schema import (
    MetabaseCard,
    MetabaseColumnRef,
    MetabaseCoverage,
    MetabaseDashboard,
    MetabaseLineage,
    MetabaseProvenance,
    Model,
)

# --- helpers ----------------------------------------------------------------


def _model(name: str, database: str, schema: str) -> Model:
    return Model(name=name, schema=schema, database=database, resource_type="model")


class _FakeProvider:
    def __init__(self, models):
        self._models = models

    def get_models(self):
        return self._models


def _lineage(dashboards, cards, relations=None) -> MetabaseLineage:
    return MetabaseLineage(
        schema_version=1,
        provenance=MetabaseProvenance(
            generated_at="2026-08-21T08:00:00Z",
            metabase_base_url="https://mb.example.com",
            extractor_version="9.9.9",
        ),
        coverage=MetabaseCoverage(
            cards_total=len(cards),
            cards_resolved_column=len(cards),
            cards_resolved_table_only=0,
            cards_unresolved=0,
            dashboards_total=len(dashboards),
            snippets_total=0,
        ),
        relations=relations or {},
        cards=cards,
        dashboards=dashboards,
    )


# --- normalization / relation index ----------------------------------------


def test_normalize_strips_quotes_brackets_and_lowercases():
    assert normalize_relation('"ANALYTICS"."MARTS"."DIM"') == "analytics.marts.dim"
    assert normalize_relation("`proj`.`ds`.`t`") == "proj.ds.t"
    assert normalize_relation("[db].[dbo].[T]") == "db.dbo.t"
    # Idempotent on an already-normalized key.
    assert normalize_relation("analytics.marts.dim") == "analytics.marts.dim"


def test_relation_index_from_model_fields():
    provider = _FakeProvider({"dim_accounts": _model("dim_accounts", "ANALYTICS", "MARTS_FINANCE")})
    index = build_relation_index(provider)
    assert index["analytics.marts_finance.dim_accounts"] == "dim_accounts"
    # Unambiguous schema.table fallback (single-DB Metabase omits the db component).
    assert index["marts_finance.dim_accounts"] == "dim_accounts"


def test_relation_index_prefers_relation_name_resolver():
    provider = _FakeProvider({"dim_accounts": _model("dim_accounts", "test", "main")})

    # A model whose physical table differs from its name via alias/identifier.
    def resolver(name):
        return '"ANALYTICS"."MARTS"."DIM_ACCOUNTS"'

    index = build_relation_index(provider, resolver)
    assert index["analytics.marts.dim_accounts"] == "dim_accounts"
    # The (database, schema, name) fallback is ALSO indexed, so both keys resolve.
    assert index["test.main.dim_accounts"] == "dim_accounts"


def test_ambiguous_schema_table_is_dropped_not_guessed():
    provider = _FakeProvider(
        {
            "a_dim": _model("dim", "db_a", "shared"),
            "b_dim": _model("dim", "db_b", "shared"),
        }
    )
    index = build_relation_index(provider)
    # Each exact db.schema.table still resolves...
    assert index["db_a.shared.dim"] == "a_dim"
    assert index["db_b.shared.dim"] == "b_dim"
    # ...but the ambiguous bare schema.table is dropped (never guessed).
    assert "shared.dim" not in index


# --- reach index ------------------------------------------------------------


def _executive_setup():
    relation_index = {"test.main.transactions": "transactions"}
    card = MetabaseCard(
        card_id=501,
        name="Exec by holder",
        query_kind="mbql",
        precision="column",
        columns=[
            MetabaseColumnRef(
                relation="test.main.transactions", column="account_holder", role="field"
            )
        ],
        table_relations=["test.main.transactions"],
    )
    dashboard = MetabaseDashboard(
        dashboard_id=55,
        name="Executive KPIs",
        url="https://mb.example.com/dashboard/55",
        card_ids=[501],
        meta={"tier": "executive", "owner": "cfo-office"},
    )
    lineage = _lineage([dashboard], [card])
    return MetabaseReach.build(lineage, relation_index), lineage


def test_reached_dashboards_column_precise():
    reach, _ = _executive_setup()
    entries = reach.reached_dashboards(columns=[("transactions", "account_holder")], models=[])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "metabase.dashboard.55"
    assert entry["type"] == "dashboard"
    assert entry["source"] == "metabase"
    assert entry["precision"] == "column"
    assert entry["via_cards"] == [501]
    assert entry["meta"]["tier"] == "executive"
    # F4: the column-precise chain (changed column -> card field -> dashboard) is carried,
    # not collapsed to dashboard grain.
    assert entry["via_columns"] == [
        {"model": "transactions", "column": "account_holder", "card_id": 501, "role": "field"}
    ]


def test_column_precise_card_does_not_overfire_on_other_columns():
    reach, _ = _executive_setup()
    # A different column on the same model must NOT reach the column-precise card.
    entries = reach.reached_dashboards(
        columns=[("transactions", "amount")], models=["transactions"]
    )
    assert entries == []


def test_table_only_card_fires_on_model():
    relation_index = {"test.main.transactions": "transactions"}
    card = MetabaseCard(
        card_id=900,
        name="Native select *",
        query_kind="native",
        precision="table",
        columns=[],
        table_relations=["test.main.transactions"],
        unresolved_reason="select_star",
    )
    dashboard = MetabaseDashboard(
        dashboard_id=88, name="Coarse", card_ids=[900], meta={"tier": "x"}
    )
    reach = MetabaseReach.build(_lineage([dashboard], [card]), relation_index)
    entries = reach.reached_dashboards(
        columns=[("transactions", "anything")], models=["transactions"]
    )
    assert len(entries) == 1
    assert entries[0]["precision"] == "table"
    # Honest table-grain degradation (F4): the dashboard is reached, but no column is proven,
    # so the chain is empty rather than guessed.
    assert entries[0]["via_columns"] == []


def test_via_columns_carries_multiple_fields_and_roles():
    """F4: a card reading several columns of a model carries each as a distinct via-column
    with its role, deterministically ordered — so the report names every affected field."""
    relation_index = {"test.main.transactions": "transactions"}
    card = MetabaseCard(
        card_id=501,
        name="Multi-field",
        query_kind="mbql",
        precision="column",
        columns=[
            MetabaseColumnRef(
                relation="test.main.transactions", column="amount", role="aggregation"
            ),
            MetabaseColumnRef(
                relation="test.main.transactions", column="account_holder", role="breakout"
            ),
        ],
        table_relations=["test.main.transactions"],
    )
    dashboard = MetabaseDashboard(dashboard_id=55, name="D", card_ids=[501], meta={})
    reach = MetabaseReach.build(_lineage([dashboard], [card]), relation_index)

    entries = reach.reached_dashboards(
        columns=[("transactions", "amount"), ("transactions", "account_holder")], models=[]
    )
    assert entries[0]["via_columns"] == [
        {"model": "transactions", "column": "account_holder", "card_id": 501, "role": "breakout"},
        {"model": "transactions", "column": "amount", "card_id": 501, "role": "aggregation"},
    ]


def test_dashboard_meta_merges_source_and_provenance():
    reach, _ = _executive_setup()
    meta = reach.dashboard_meta("metabase.dashboard.55")
    assert meta is not None
    assert meta["source"] == "metabase"  # provenance injected for policy meta.source
    assert meta["tier"] == "executive"  # consumer data
    assert meta["name"] == "Executive KPIs"
    assert reach.dashboard_meta("metabase.dashboard.999") is None


def test_dashboard_reach_name_helper():
    assert dashboard_reach_name(55) == "metabase.dashboard.55"


# --- confidence -------------------------------------------------------------


def test_confidence_absent_when_no_lineage():
    conf = build_reach_confidence(None, [])
    assert conf.level == "absent"
    assert conf.stale is False


def test_confidence_fresh_snapshot_full():
    _, lineage = _executive_setup()
    now = datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc)  # 1h after generated_at
    entries = [{"precision": "column"}]
    conf = build_reach_confidence(lineage, entries, max_age_hours=24.0, now=now)
    assert conf.stale is False
    assert conf.level == "full"
    assert conf.dashboards_reached == 1
    assert conf.cards_column_precise == 1
    assert conf.cards_table_only == 0


def test_confidence_stale_snapshot_partial():
    _, lineage = _executive_setup()
    now = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)  # ~4 days later
    conf = build_reach_confidence(lineage, [{"precision": "column"}], max_age_hours=24.0, now=now)
    assert conf.stale is True
    assert conf.level == "partial"
    assert conf.snapshot_age_hours is not None and conf.snapshot_age_hours > 24
