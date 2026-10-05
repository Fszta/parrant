"""— the extract pipeline: fetch → resolve → normalize → coverage-stamp.

All network I/O is behind :class:`~parrant.metabase.client.MetabaseClient`, and
the SQL dialect for the native resolver is supplied by the caller (from the dbt manifest /
``--adapter``). The pipeline itself is pure w.r.t. those inputs, so it runs end-to-end in a
unit test against a fake client with zero live Metabase.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from parrant.metabase.client import MetabaseClient
from parrant.metabase.resolvers import CardResolver, ResolvedCard
from parrant.metabase.warehouse_meta import CardCorpus, WarehouseMeta
from parrant.models.schema import (
    MetabaseCard,
    MetabaseCoverage,
    MetabaseDashboard,
    MetabaseLineage,
    MetabaseProvenance,
    MetabaseRelation,
)

# Capacity for the honesty id samples in coverage.
_SAMPLE_CAP = 25


@dataclass
class ExtractConfig:
    """Inputs to :func:`run_extract` that are not the network client itself."""

    metabase_base_url: str
    database_ids: list[int]
    extractor_version: str
    dialect: str | None = None
    include_archived: bool = False
    # Consumer-configurable dashboard meta mapping (spec Q8 /): the tool
    # never hardcodes an org's taxonomy. Shape:
    #   {"by_collection": {<collection_id>: {...}}, "by_dashboard": {<dashboard_id>: {...}}}
    dashboard_meta: dict[str, dict[Any, dict[str, Any]]] = field(default_factory=dict)
    # A previously-loaded snapshot for incremental reuse. When provided, dashboards whose
    # Metabase ``updated_at`` matches the previous snapshot are reused rather than refetched
    # (the N+1 detail fetch is the expensive part at 500+ dashboards). ``None`` = full extract.
    previous: MetabaseLineage | None = None
    # Concurrency for the dashboard detail fan-out (``client.get_dashboards``).
    max_workers: int = 8


def build_dashboard_meta_resolver(
    mapping: dict[str, dict[Any, dict[str, Any]]],
) -> Callable[[dict], dict[str, Any]]:
    """Turn the consumer mapping into ``dashboard_dict -> meta`` (per-dashboard overrides
    per-collection). Absent → ``{}``; the taxonomy is entirely the consumer's."""
    by_collection = {str(k): v for k, v in (mapping.get("by_collection") or {}).items()}
    by_dashboard = {str(k): v for k, v in (mapping.get("by_dashboard") or {}).items()}

    def resolve(dashboard: dict) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        collection_id = dashboard.get("collection_id")
        if collection_id is not None and str(collection_id) in by_collection:
            meta.update(by_collection[str(collection_id)])
        dashboard_id = dashboard.get("id")
        if dashboard_id is not None and str(dashboard_id) in by_dashboard:
            meta.update(by_dashboard[str(dashboard_id)])
        return meta

    return resolve


def _dashcard_card_ids(dashboard: dict) -> list[int]:
    """Collect card ids from a dashboard's ``dashcards`` (or legacy ``ordered_cards``)."""
    entries = dashboard.get("dashcards")
    if entries is None:
        entries = dashboard.get("ordered_cards") or []
    ids: list[int] = []
    for entry in entries:
        card_id = entry.get("card_id")
        if not isinstance(card_id, int):
            card = entry.get("card") or {}
            card_id = card.get("id")
        if isinstance(card_id, int) and card_id not in ids:
            ids.append(card_id)
    return ids


def _person(obj: Any) -> str | None:
    """A human identifier from a Metabase ``creator`` / ``last-edit-info`` object.

    Prefers email (actionable for ownership routing), falls back to a display name; ``None``
    when nothing usable is present."""
    if not isinstance(obj, dict):
        return None
    email = obj.get("email")
    if email:
        return str(email)
    name = " ".join(str(x) for x in (obj.get("first_name"), obj.get("last_name")) if x).strip()
    return name or None


def _collection_name(obj: Any) -> str | None:
    """The name of an embedded Metabase ``collection`` object, if present."""
    if isinstance(obj, dict):
        name = obj.get("name")
        return str(name) if name else None
    return None


def _to_card(card: dict, resolved: ResolvedCard, base_url: str) -> MetabaseCard:
    query = card.get("dataset_query") or {}
    kind = "native" if query.get("type") == "native" else "mbql"
    return MetabaseCard(
        card_id=card["id"],
        name=card.get("name") or "",
        query_kind=kind,  # type: ignore[arg-type]
        precision=resolved.precision,  # type: ignore[arg-type]
        collection_id=card.get("collection_id"),
        archived=bool(card.get("archived", False)),
        database_id=query.get("database"),
        columns=resolved.columns,
        table_relations=resolved.table_relations,
        upstream_card_ids=resolved.upstream_card_ids,
        snippet_ids=resolved.snippet_ids,
        unresolved_reason=resolved.unresolved_reason,
        updated_at=card.get("updated_at"),
        url=f"{base_url.rstrip('/')}/question/{card['id']}",
        description=card.get("description"),
        collection_name=_collection_name(card.get("collection")),
        creator=_person(card.get("creator")),
        last_edited_by=_person(card.get("last-edit-info")),
    )


def run_extract(config: ExtractConfig, client: MetabaseClient) -> MetabaseLineage:
    """Fetch from Metabase, resolve every card, and assemble the snapshot artifact."""
    # 1. Fetch corpus + warehouse metadata.
    raw_cards = client.list_cards(include_archived=config.include_archived)
    snippets = client.list_snippets()
    metadatas = [client.database_metadata(db_id) for db_id in config.database_ids]
    meta = WarehouseMeta.from_database_metadata(metadatas)
    corpus = CardCorpus(raw_cards, snippets)

    # 2. Resolve cards. Scope to the configured connection(s): a card whose query targets a
    # foreign Metabase database pollutes the artifact and drags down coverage, so skip it. A
    # malformed card with no ``database`` (db is None) keeps the old behavior of being resolved.
    resolver = CardResolver(meta, corpus, config.dialect)
    scoped_db_ids = set(config.database_ids)
    cards: list[MetabaseCard] = []
    included_card_ids: set[int] = set()
    used_relations: set[str] = set()
    for raw_card in raw_cards:
        if not isinstance(raw_card.get("id"), int):
            continue
        if raw_card.get("archived") and not config.include_archived:
            continue
        query = raw_card.get("dataset_query") or {}
        db = query.get("database")
        if db is not None and db not in scoped_db_ids:
            continue
        resolved = resolver.resolve_card(raw_card)
        card = _to_card(raw_card, resolved, config.metabase_base_url)
        cards.append(card)
        included_card_ids.add(card.card_id)
        for ref in card.columns:
            used_relations.add(ref.relation)
        used_relations.update(card.table_relations)

    # 3. Attach dashboards (with consumer-supplied meta), concurrent + incremental. Fetch
    # detail only for dashboards new-or-changed since the previous snapshot; reuse the rest.
    meta_resolver = build_dashboard_meta_resolver(config.dashboard_meta)
    shells = client.list_dashboards()
    # Reuse is only sound when the previous snapshot was taken over the SAME connection scope:
    # a reused dashboard's ``card_ids`` were already filtered to the previous run's in-scope
    # cards, so re-intersecting can only shrink them. If ``--database-id`` widened (or otherwise
    # changed) since the previous run, a card that newly entered scope on an unedited dashboard
    # (same ``updated_at``) would be silently dropped. Guard against that by disabling reuse
    # entirely on a scope mismatch — the run degrades to a full (but correct) refetch.
    prev_scope_matches = config.previous is not None and set(
        config.previous.provenance.database_ids
    ) == set(config.database_ids)
    prev_by_id: dict[int, MetabaseDashboard] = (
        {d.dashboard_id: d for d in config.previous.dashboards}
        if config.previous is not None and prev_scope_matches
        else {}
    )

    # Decide reuse vs fetch per shell; a shell is reusable only when both its and the previous
    # snapshot's ``updated_at`` are present and equal (a missing stamp forces a refetch).
    shells_by_id: dict[int, dict] = {}
    fetch_ids: list[int] = []
    reused_ids: set[int] = set()
    for shell in shells:
        shell_id = shell.get("id")
        if not isinstance(shell_id, int):
            continue
        shells_by_id[shell_id] = shell
        prev = prev_by_id.get(shell_id)
        shell_updated = shell.get("updated_at")
        if (
            prev is not None
            and prev.updated_at is not None
            and shell_updated is not None
            and shell_updated == prev.updated_at
        ):
            reused_ids.add(shell_id)
        else:
            fetch_ids.append(shell_id)

    details = client.get_dashboards(fetch_ids, max_workers=config.max_workers) if fetch_ids else {}

    dashboards: list[MetabaseDashboard] = []
    for dashboard_id, shell in shells_by_id.items():
        if dashboard_id in reused_ids:
            # Reused = unchanged since the previous snapshot, so carry its asset metadata
            # forward rather than re-fetching detail.
            prev = prev_by_id[dashboard_id]
            raw_card_ids = list(prev.card_ids)
            name = shell.get("name") or prev.name or ""
            description = prev.description
            collection_name = prev.collection_name
            creator = prev.creator
            last_edited_by = prev.last_edited_by
        else:
            detail = details[dashboard_id]
            raw_card_ids = _dashcard_card_ids(detail)
            name = detail.get("name") or shell.get("name") or ""
            description = detail.get("description")
            collection_name = _collection_name(detail.get("collection"))
            creator = _person(detail.get("creator"))
            last_edited_by = _person(detail.get("last-edit-info"))
        # Intersect with the scoped cards (order-preserved); this also drops cards that left
        # the connection since the previous snapshot. Meta is ALWAYS recomputed from the shell
        # (the consumer's mapping may have changed even when the dashboard itself did not).
        card_ids = [cid for cid in raw_card_ids if cid in included_card_ids]
        if not card_ids:
            continue
        dashboards.append(
            MetabaseDashboard(
                dashboard_id=dashboard_id,
                name=name,
                collection_id=shell.get("collection_id"),
                url=f"{config.metabase_base_url.rstrip('/')}/dashboard/{dashboard_id}",
                card_ids=card_ids,
                meta=meta_resolver(shell),
                updated_at=shell.get("updated_at"),
                description=description,
                collection_name=collection_name,
                creator=creator,
                last_edited_by=last_edited_by,
            )
        )
    dashboards.sort(key=lambda d: d.dashboard_id)

    # 4. Relations actually referenced (de-duplicated), + coverage + provenance.
    relations: dict[str, MetabaseRelation] = {
        key: rel for key, rel in meta.relations.items() if key in used_relations
    }
    coverage = _build_coverage(cards, dashboards, len(snippets))
    provenance = MetabaseProvenance(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        metabase_base_url=config.metabase_base_url,
        metabase_version=client.server_version(),
        database_ids=list(config.database_ids),
        extractor_version=config.extractor_version,
        dbt_adapter=config.dialect,
    )
    return MetabaseLineage(
        provenance=provenance,
        coverage=coverage,
        relations=relations,
        cards=cards,
        dashboards=dashboards,
    )


def _build_coverage(
    cards: list[MetabaseCard], dashboards: list[MetabaseDashboard], snippets_total: int
) -> MetabaseCoverage:
    column = sum(1 for c in cards if c.precision == "column")
    table_only = sum(1 for c in cards if c.precision == "table")
    unresolved = sum(1 for c in cards if c.precision == "none")
    unresolved_ids = [c.card_id for c in cards if c.precision == "none"][:_SAMPLE_CAP]
    table_only_ids = [c.card_id for c in cards if c.precision == "table"][:_SAMPLE_CAP]
    return MetabaseCoverage(
        cards_total=len(cards),
        cards_resolved_column=column,
        cards_resolved_table_only=table_only,
        cards_unresolved=unresolved,
        dashboards_total=len(dashboards),
        snippets_total=snippets_total,
        unresolved_card_ids=unresolved_ids,
        table_only_card_ids=table_only_ids,
    )


def coverage_ratio(coverage: MetabaseCoverage) -> float:
    """(column + table_only) / total — the resolved-reach ratio for ``--fail-under``."""
    if coverage.cards_total == 0:
        return 1.0
    resolved = coverage.cards_resolved_column + coverage.cards_resolved_table_only
    return resolved / coverage.cards_total
