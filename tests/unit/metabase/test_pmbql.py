"""pMBQL (MBQL 5) normalizer + end-to-end resolution against the recorded v0.63 fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from parrant.metabase.pmbql import is_pmbql, normalize_dataset_query
from parrant.metabase.resolvers import CardResolver
from parrant.metabase.warehouse_meta import CardCorpus, WarehouseMeta
from tests.unit.metabase._fixtures import load_recorded

_PMBQL_PATH = Path(__file__).parents[2] / "resources" / "metabase" / "recorded_pmbql.json"

# Sample-DB field ids (from database_metadata["1"]).
ORDERS_TABLE = 2
PEOPLE_TABLE = 1
F_TOTAL = 5
F_CREATED_AT = 13
F_USER_ID = 11
F_PEOPLE_ID = 4
F_PEOPLE_NAME = 48


def load_pmbql() -> dict[str, Any]:
    return json.loads(_PMBQL_PATH.read_text(encoding="utf-8"))


def _cards_by_id() -> dict[int, dict]:
    return {c["id"]: c for c in load_pmbql()["cards"]}


def _resolver():
    recorded = load_pmbql()
    meta = WarehouseMeta.from_database_metadata([recorded["database_metadata"]["1"]])
    corpus = CardCorpus(recorded["cards"], recorded["snippets"])
    return CardResolver(meta, corpus, dialect="sqlite"), meta, corpus


# --- direct normalizer unit tests -----------------------------------------


def test_detects_pmbql_and_ignores_legacy():
    cards = _cards_by_id()
    assert is_pmbql(cards[43]["dataset_query"]) is True  # structured pMBQL
    assert is_pmbql(cards[41]["dataset_query"]) is True  # native pMBQL
    assert is_pmbql({"type": "query", "query": {}}) is False
    assert is_pmbql({"type": "native", "native": {}}) is False
    assert is_pmbql({}) is False


def test_structured_envelope_and_field_ref_reorder():
    card = _cards_by_id()[43]
    legacy = normalize_dataset_query(card["dataset_query"])

    assert legacy["type"] == "query"
    assert legacy["database"] == 1
    query = legacy["query"]
    assert query["source-table"] == ORDERS_TABLE

    # breakout field ref reordered: pMBQL [field, opts, 13] -> legacy [field, 13, opts].
    breakout_ref = query["breakout"][0]
    assert breakout_ref[0] == "field"
    assert breakout_ref[1] == F_CREATED_AT
    assert isinstance(breakout_ref[2], dict)
    assert "lib/uuid" not in breakout_ref[2]  # noise stripped
    assert breakout_ref[2].get("temporal-unit") == "month"  # semantic opt kept

    # aggregation clause: opts dropped, inner field ref reordered.
    agg = query["aggregation"][0]
    assert agg == ["sum", ["field", F_TOTAL, {"base-type": "type/Float"}]]

    # filters (plural) -> filter (singular); operator opts dropped.
    assert query["filter"] == [">", ["field", F_TOTAL, {"base-type": "type/Float"}], 10]


def test_multiple_filters_wrap_in_and():
    query = {
        "lib/type": "mbql/query",
        "database": 1,
        "stages": [
            {
                "lib/type": "mbql.stage/mbql",
                "source-table": ORDERS_TABLE,
                "filters": [
                    [">", {"lib/uuid": "a"}, ["field", {"lib/uuid": "b"}, F_TOTAL], 10],
                    ["<", {"lib/uuid": "c"}, ["field", {"lib/uuid": "d"}, F_TOTAL], 99],
                ],
            }
        ],
    }
    legacy = normalize_dataset_query(query)["query"]
    assert legacy["filter"][0] == "and"
    assert legacy["filter"][1] == [">", ["field", F_TOTAL, {}], 10]
    assert legacy["filter"][2] == ["<", ["field", F_TOTAL, {}], 99]


def test_source_card_becomes_card_token():
    card = _cards_by_id()[45]
    legacy = normalize_dataset_query(card["dataset_query"])
    assert legacy["type"] == "query"
    assert legacy["query"]["source-table"] == "card__43"


def test_join_shape():
    card = _cards_by_id()[44]
    legacy = normalize_dataset_query(card["dataset_query"])["query"]
    assert legacy["source-table"] == ORDERS_TABLE

    join = legacy["joins"][0]
    assert join["alias"] == "People"
    assert join["source-table"] == PEOPLE_TABLE  # pulled out of the nested join stage
    # conditions (plural) -> condition (singular), refs reordered.
    assert join["condition"][0] == "="
    assert join["condition"][1] == ["field", F_USER_ID, {"base-type": "type/Integer"}]
    assert join["condition"][2][0] == "field"
    assert join["condition"][2][1] == F_PEOPLE_ID
    assert join["condition"][2][2].get("join-alias") == "People"  # semantic opt kept


def test_native_envelope_and_template_tags_list_to_dict():
    card = _cards_by_id()[50]
    legacy = normalize_dataset_query(card["dataset_query"])

    assert legacy["type"] == "native"
    assert legacy["database"] == 1
    assert legacy["native"]["query"].startswith("SELECT id FROM orders")

    tags = legacy["native"]["template-tags"]
    assert isinstance(tags, dict)
    assert set(tags) == {"created", "snippet: active2", "#49"}
    # dimension field ref inside a tag is reordered.
    dim = tags["created"]["dimension"]
    assert dim[0] == "field" and dim[1] == F_CREATED_AT
    assert tags["snippet: active2"]["snippet-id"] == 2
    assert tags["#49"]["card-id"] == 49


def test_legacy_passthrough_is_identity():
    recorded = load_recorded()
    legacy_query = recorded["cards"][0]["dataset_query"]
    assert normalize_dataset_query(legacy_query) is legacy_query


def test_idempotent():
    card = _cards_by_id()[43]
    once = normalize_dataset_query(card["dataset_query"])
    twice = normalize_dataset_query(once)
    assert twice == once


# --- end-to-end resolution via CardResolver --------------------------------


def test_structured_card_resolves_column_precise():
    resolver, meta, corpus = _resolver()
    resolved = resolver.resolve_card(corpus.card(43))

    orders = meta.table(ORDERS_TABLE)
    assert resolved.precision == "column"
    by_col = {(c.relation, c.column): c for c in resolved.columns}
    assert (orders, "total") in by_col
    assert (orders, "created_at") in by_col
    assert by_col[(orders, "created_at")].role == "breakout"
    assert by_col[(orders, "total")].role == "aggregation"


def test_join_card_picks_up_joined_table_column():
    resolver, meta, corpus = _resolver()
    resolved = resolver.resolve_card(corpus.card(44))

    orders = meta.table(ORDERS_TABLE)
    people = meta.table(PEOPLE_TABLE)
    assert resolved.precision == "column"
    cols = {(c.relation, c.column) for c in resolved.columns}
    assert (orders, "total") in cols
    assert (people, "name") in cols  # projected through the join
    assert (people, "id") in cols  # join condition
    assert people in resolved.table_relations


def test_on_card_records_upstream_card():
    resolver, meta, corpus = _resolver()
    resolved = resolver.resolve_card(corpus.card(45))

    orders = meta.table(ORDERS_TABLE)
    assert resolved.upstream_card_ids == [43]
    relations = {c.relation for c in resolved.columns} | set(resolved.table_relations)
    assert orders in relations


def test_native_card_expands_snippet_and_dimension_and_card_ref():
    resolver, meta, corpus = _resolver()
    resolved = resolver.resolve_card(corpus.card(50))

    orders = meta.table(ORDERS_TABLE)
    assert resolved.snippet_ids == [2]
    assert 49 in resolved.upstream_card_ids
    cols = {(c.relation, c.column) for c in resolved.columns}
    assert (orders, "created_at") in cols  # recovered from the dimension tag


def test_native_pmbql_card_query_kind_after_in_place_normalize():
    resolver, _meta, corpus = _resolver()
    card = corpus.card(41)  # native pMBQL
    assert is_pmbql(card["dataset_query"])  # pre: pMBQL shape
    resolver.resolve_card(card)
    # resolve_card normalized in place so extract._to_card reads a legacy native query.
    assert card["dataset_query"]["type"] == "native"
    assert card["dataset_query"]["database"] == 1
