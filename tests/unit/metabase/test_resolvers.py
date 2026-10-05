"""— the two resolvers + warehouse-meta normalization, against the recorded fixture."""

from __future__ import annotations

from parrant.metabase.resolvers import CardResolver
from parrant.metabase.warehouse_meta import CardCorpus, WarehouseMeta
from tests.unit.metabase._fixtures import load_recorded

DIM_ACCOUNTS = "analytics.marts_finance.dim_accounts"
FACT_REVENUE = "analytics.marts_finance.fact_revenue"


def _resolver():
    recorded = load_recorded()
    meta = WarehouseMeta.from_database_metadata([recorded["database_metadata"]["2"]])
    corpus = CardCorpus(recorded["cards"], recorded["snippets"])
    return CardResolver(meta, corpus, dialect="snowflake"), corpus


def _card(corpus: CardCorpus, card_id: int) -> dict:
    card = corpus.card(card_id)
    assert card is not None
    return card


def test_warehouse_meta_normalizes_ids_and_names():
    recorded = load_recorded()
    meta = WarehouseMeta.from_database_metadata([recorded["database_metadata"]["2"]])

    assert meta.field(1002) == (DIM_ACCOUNTS, "country_code")
    assert meta.table(100) == DIM_ACCOUNTS
    # name resolution: fully qualified, schema.table, and bare table all normalize.
    assert meta.resolve_name("ANALYTICS.MARTS_FINANCE.FACT_REVENUE") == FACT_REVENUE
    assert meta.resolve_name("marts_finance.fact_revenue") == FACT_REVENUE
    assert meta.resolve_name("fact_revenue") == FACT_REVENUE
    assert meta.resolve_name("does_not_exist") is None


def test_mbql_card_is_column_precise():
    resolver, corpus = _resolver()
    resolved = resolver.resolve_card(_card(corpus, 128))

    assert resolved.precision == "column"
    by_col = {(c.relation, c.column): c for c in resolved.columns}
    # breakout column carries role=breakout; filter column carries role=filter.
    assert by_col[(DIM_ACCOUNTS, "country_code")].role == "breakout"
    assert by_col[(DIM_ACCOUNTS, "account_status")].role == "filter"
    assert all(c.confidence == "high" for c in resolved.columns)
    assert resolved.table_relations == [DIM_ACCOUNTS]


def test_native_card_column_precise_on_clean_sql():
    resolver, corpus = _resolver()
    resolved = resolver.resolve_card(_card(corpus, 3402))

    assert resolved.precision == "column"
    cols = {(c.relation, c.column) for c in resolved.columns}
    assert (FACT_REVENUE, "amount") in cols
    assert (FACT_REVENUE, "region") in cols
    assert all(c.confidence == "medium" for c in resolved.columns)  # parsed, not structured


def test_native_card_reference_degrades_to_table_grain():
    resolver, corpus = _resolver()
    resolved = resolver.resolve_card(_card(corpus, 3391))  # select * from {{#128}}

    assert resolved.precision == "table"
    assert resolved.unresolved_reason == "select_star"
    assert resolved.upstream_card_ids == [128]
    # the {{#128}} card's own relation is inherited as table-grain reach.
    assert DIM_ACCOUNTS in resolved.table_relations


def test_native_card_with_snippet_records_snippet_and_columns():
    resolver, corpus = _resolver()
    resolved = resolver.resolve_card(_card(corpus, 3403))

    assert resolved.snippet_ids == [23]
    # snippet content references fact_revenue.amount (predicate) -> reach on fact_revenue.
    relations = {c.relation for c in resolved.columns} | set(resolved.table_relations)
    assert FACT_REVENUE in relations


def test_resolution_is_memoized():
    resolver, corpus = _resolver()
    first = resolver.resolve_card(_card(corpus, 128))
    second = resolver.resolve_card(_card(corpus, 128))
    assert first is second
