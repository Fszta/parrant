"""— incremental dashboard reuse: skip the N+1 detail fetch when a dashboard's
``updated_at`` is unchanged vs the ``--previous`` snapshot.

These exercise ``run_extract(config.previous=...)``: a dashboard whose shell ``updated_at``
matches the previous snapshot is REUSED (its detail endpoint is never hit), while a changed
or unstamped one is REFETCHED. Reuse still re-intersects card_ids against the in-scope cards
and always recomputes meta from the fresh mapping.
"""

from __future__ import annotations

from parrant.metabase.client import MetabaseClient
from parrant.metabase.extract import ExtractConfig, run_extract
from parrant.models.schema import (
    MetabaseCoverage,
    MetabaseDashboard,
    MetabaseLineage,
    MetabaseProvenance,
)
from tests.unit.metabase._fixtures import FakeSession, build_recorded

_DB_META_2 = {
    "2": {
        "id": 2,
        "name": "Analytics",
        "engine": "snowflake",
        "details": {"db": "ANALYTICS"},
        "tables": [
            {
                "id": 200,
                "name": "FACT_REVENUE",
                "schema": "MARTS_FINANCE",
                "db_id": 2,
                "fields": [{"id": 2001, "name": "AMOUNT", "base_type": "type/Float"}],
            }
        ],
    }
}


def _mbql_card(card_id: int, database: int = 2) -> dict:
    return {
        "id": card_id,
        "name": f"card {card_id}",
        "collection_id": 1,
        "archived": False,
        "updated_at": "2024-01-01T00:00:00Z",
        "dataset_query": {
            "type": "query",
            "database": database,
            "query": {"source-table": 200, "aggregation": [["count"]]},
        },
    }


class SpyClient(MetabaseClient):
    """A client that records which dashboard ids were actually fetched (detail round-trip).

    ``get_dashboards`` is the single choke point the extract funnels new-or-changed dashboards
    through, so recording its argument is a faithful spy on "which details were fetched"; a
    reused dashboard must never appear here.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fetched_dashboard_ids: list[int] = []

    def get_dashboards(self, dashboard_ids: list[int], max_workers: int = 8) -> dict[int, dict]:
        self.fetched_dashboard_ids.extend(dashboard_ids)
        return super().get_dashboards(dashboard_ids, max_workers=max_workers)


def _spy_client(session: FakeSession) -> SpyClient:
    return SpyClient(
        base_url="https://metabase.example.com",
        api_key="k",
        session=session,
        sleep=lambda _s: None,
    )


def _config(previous: MetabaseLineage | None, **overrides) -> ExtractConfig:
    kwargs = {
        "metabase_base_url": "https://metabase.example.com",
        "database_ids": [2],
        "extractor_version": "9.9.9",
        "dialect": "snowflake",
        "previous": previous,
    }
    kwargs.update(overrides)
    return ExtractConfig(**kwargs)  # type: ignore[arg-type]


def _prev_snapshot(
    dashboards: list[MetabaseDashboard],
    schema_version: int = 2,
    database_ids: list[int] | None = None,
) -> MetabaseLineage:
    return MetabaseLineage(
        schema_version=schema_version,
        provenance=MetabaseProvenance(
            generated_at="2024-01-01T00:00:00Z",
            metabase_base_url="https://metabase.example.com",
            extractor_version="9.9.9",
            # Stamp the scope so reuse (which requires a matching --database-id scope) is enabled;
            # defaults to the [2] the tests extract with.
            database_ids=database_ids if database_ids is not None else [2],
        ),
        coverage=MetabaseCoverage(
            cards_total=0,
            cards_resolved_column=0,
            cards_resolved_table_only=0,
            cards_unresolved=0,
            dashboards_total=len(dashboards),
            snippets_total=0,
        ),
        dashboards=dashboards,
    )


def test_reuses_unchanged_dashboard_and_refetches_changed_one():
    # Dashboard 10 is unchanged (same updated_at) → reused; dashboard 20 changed → refetched.
    previous = _prev_snapshot(
        [
            MetabaseDashboard(
                dashboard_id=10, name="stable", card_ids=[1], updated_at="v1", meta={}
            ),
            MetabaseDashboard(
                dashboard_id=20, name="changing", card_ids=[1], updated_at="v1", meta={}
            ),
        ]
    )
    recorded = build_recorded(
        cards=[_mbql_card(1)],
        dashboards=[
            {"id": 10, "name": "stable", "collection_id": 1, "updated_at": "v1"},  # SAME
            {"id": 20, "name": "changing", "collection_id": 1, "updated_at": "v2"},  # CHANGED
        ],
        dashboard_details={
            "10": {"id": 10, "dashcards": [{"card_id": 1, "card": {"id": 1}}]},
            "20": {"id": 20, "dashcards": [{"card_id": 1, "card": {"id": 1}}]},
        },
        database_metadata=_DB_META_2,
    )
    session = FakeSession(recorded)
    client = _spy_client(session)

    lineage = run_extract(_config(previous), client)

    # The reused dashboard's detail endpoint was NOT hit; the changed one was.
    assert 10 not in client.fetched_dashboard_ids
    assert 20 in client.fetched_dashboard_ids
    detail_urls = [url for url, _ in session.get_calls if "/api/dashboard/" in url]
    assert not any(url.endswith("/api/dashboard/10") for url in detail_urls)
    assert any(url.endswith("/api/dashboard/20") for url in detail_urls)

    # Both dashboards still present and sorted by id.
    assert [d.dashboard_id for d in lineage.dashboards] == [10, 20]


def test_reused_dashboard_recomputes_meta_from_fresh_mapping():
    # Previous snapshot carries STALE meta; the fresh dashboard_meta mapping must win even
    # though the dashboard itself is reused (the consumer taxonomy may have changed).
    previous = _prev_snapshot(
        [
            MetabaseDashboard(
                dashboard_id=10,
                name="stable",
                card_ids=[1],
                updated_at="v1",
                meta={"tier": "STALE"},
            )
        ]
    )
    recorded = build_recorded(
        cards=[_mbql_card(1)],
        dashboards=[{"id": 10, "name": "stable", "collection_id": 1, "updated_at": "v1"}],
        dashboard_details={"10": {"id": 10, "dashcards": [{"card_id": 1, "card": {"id": 1}}]}},
        database_metadata=_DB_META_2,
    )
    client = _spy_client(FakeSession(recorded))

    mapping = {"by_dashboard": {"10": {"tier": "fresh"}}}
    lineage = run_extract(_config(previous, dashboard_meta=mapping), client)

    dash = next(d for d in lineage.dashboards if d.dashboard_id == 10)
    assert dash.meta == {"tier": "fresh"}  # fresh mapping, not the previous snapshot's meta
    assert 10 not in client.fetched_dashboard_ids  # still reused (not refetched)


def test_reused_dashboard_reintersects_card_ids_against_scope():
    # Previous dashboard referenced cards 1 and 2, but card 2 is no longer in scope; reuse must
    # re-intersect so the dropped card is gone from the reused dashboard's card_ids.
    previous = _prev_snapshot(
        [
            MetabaseDashboard(
                dashboard_id=10, name="stable", card_ids=[1, 2], updated_at="v1", meta={}
            )
        ]
    )
    recorded = build_recorded(
        cards=[_mbql_card(1)],  # only card 1 is in scope now; card 2 is gone
        dashboards=[{"id": 10, "name": "stable", "collection_id": 1, "updated_at": "v1"}],
        dashboard_details={"10": {"id": 10, "dashcards": [{"card_id": 1, "card": {"id": 1}}]}},
        database_metadata=_DB_META_2,
    )
    client = _spy_client(FakeSession(recorded))

    lineage = run_extract(_config(previous), client)

    dash = next(d for d in lineage.dashboards if d.dashboard_id == 10)
    assert dash.card_ids == [1]  # card 2 re-intersected out
    assert 10 not in client.fetched_dashboard_ids  # reused, not refetched


def test_scope_change_disables_reuse_to_avoid_dropping_newly_in_scope_cards():
    # The previous snapshot was taken over --database-id 1 (scope {1}); this run widens to {1,2}.
    # A dashboard unchanged in Metabase (same updated_at) would otherwise be reused, but its
    # stored card_ids were filtered to the OLD scope — so a card newly in scope on that unedited
    # dashboard would be silently dropped. Reuse must be disabled on any scope mismatch and the
    # run must do a full refetch instead.
    previous = _prev_snapshot(
        [MetabaseDashboard(dashboard_id=10, name="stable", card_ids=[1], updated_at="v1", meta={})],
        database_ids=[1],  # previous scope differs from this run's [2]
    )
    recorded = build_recorded(
        cards=[_mbql_card(1)],
        dashboards=[{"id": 10, "name": "stable", "collection_id": 1, "updated_at": "v1"}],
        dashboard_details={"10": {"id": 10, "dashcards": [{"card_id": 1, "card": {"id": 1}}]}},
        database_metadata=_DB_META_2,
    )
    client = _spy_client(FakeSession(recorded))

    lineage = run_extract(_config(previous), client)  # this run scopes to [2]

    assert 10 in client.fetched_dashboard_ids  # refetched, NOT reused, despite matching updated_at
    assert [d.dashboard_id for d in lineage.dashboards] == [10]


def test_v1_previous_snapshot_forces_full_refetch():
    # A v1 previous snapshot's dashboards carry no updated_at (None); with no stamp to compare,
    # nothing is reused — every dashboard is refetched — and it must not error.
    previous = _prev_snapshot(
        [
            MetabaseDashboard(dashboard_id=10, name="a", card_ids=[1], updated_at=None, meta={}),
            MetabaseDashboard(dashboard_id=20, name="b", card_ids=[1], updated_at=None, meta={}),
        ],
        schema_version=1,
    )
    recorded = build_recorded(
        cards=[_mbql_card(1)],
        dashboards=[
            {"id": 10, "name": "a", "collection_id": 1, "updated_at": "v1"},
            {"id": 20, "name": "b", "collection_id": 1, "updated_at": "v1"},
        ],
        dashboard_details={
            "10": {"id": 10, "dashcards": [{"card_id": 1, "card": {"id": 1}}]},
            "20": {"id": 20, "dashcards": [{"card_id": 1, "card": {"id": 1}}]},
        },
        database_metadata=_DB_META_2,
    )
    client = _spy_client(FakeSession(recorded))

    lineage = run_extract(_config(previous), client)

    assert set(client.fetched_dashboard_ids) == {10, 20}  # nothing reused
    assert [d.dashboard_id for d in lineage.dashboards] == [10, 20]
