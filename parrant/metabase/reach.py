"""— the offline Metabase reach index: ``(dbt_model, column) -> cards -> dashboards``.

Built once from a loaded :class:`MetabaseLineage` artifact + the relation join
(:mod:`parrant.metabase.join`). Pure, offline, zero-credential — it imports ONLY
the pydantic schema, never the Metabase client — so the gate/impact path stays credential-free
.

**One reach model, not a bolt-on.** ``service.get_changeset_impact`` already computes, per
changed column, the downstream columns/models/exposures it reaches. This index answers "which
Metabase dashboards read any of those terminal nodes?" and the service appends each such
dashboard onto that SAME reach as an EXPOSURE-kind reached object (a Metabase dashboard *is* an
exposure — a BI artifact consuming the warehouse). No new ``ReachKind``;'s policy engine
already scans ``reach.kind: exposure`` over the object's ``meta.*``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from parrant.metabase.join import normalize_relation
from parrant.models.schema import (
    MetabaseDashboard,
    MetabaseLineage,
    MetabaseReachConfidence,
)

# The synthetic exposure name a reached Metabase dashboard surfaces under. The
# ``metabase.dashboard.`` prefix is how :class:`MetaIndex` recognises a dashboard-sourced
# exposure and resolves its ``meta`` from this index rather than the dbt manifest.
DASHBOARD_NAME_PREFIX = "metabase.dashboard."


def dashboard_reach_name(dashboard_id: int) -> str:
    """The synthetic exposure name for a Metabase dashboard (``metabase.dashboard.<id>``)."""
    return f"{DASHBOARD_NAME_PREFIX}{dashboard_id}"


class MetabaseReach:
    """Read-only index ``(dbt_model, column) -> cards -> dashboards``, built offline.

    Construct via :meth:`build`. No network, no registry mutation; every lookup is a dict read.
    Model and column keys are lowercased to match the registry/impact convention.
    """

    def __init__(
        self,
        column_cards: dict[tuple[str, str], dict[int, str]],
        model_cards: dict[str, set[int]],
        dashboards_by_card: dict[int, list[MetabaseDashboard]],
        dashboard_by_name: dict[str, MetabaseDashboard],
    ) -> None:
        self._column_cards = column_cards
        self._model_cards = model_cards
        self._dashboards_by_card = dashboards_by_card
        self._dashboard_by_name = dashboard_by_name

    @classmethod
    def build(cls, lineage: MetabaseLineage, relation_index: dict[str, str]) -> MetabaseReach:
        """Invert the artifact into the reach index using the dbt relation join.

        A card's column refs (column-precise) map ``(relation, column) -> (dbt_model, column)``;
        a card's ``table_relations`` map to a dbt model at *table* grain, used ONLY for cards
        that could not be resolved column-precise (``precision != "column"``) so a column-precise
        card never over-fires on a model-level match. Relations that don't join to
        any dbt model are dropped (honest: no guess).
        """
        column_cards: dict[tuple[str, str], dict[int, str]] = {}
        model_cards: dict[str, set[int]] = {}

        for card in lineage.cards:
            for ref in card.columns:
                model = relation_index.get(normalize_relation(ref.relation))
                if model is None:
                    continue
                # ``(dbt_model, warehouse_column) -> {card_id: role}``. The column stays a
                # first-class part of the reach so the chain ``changed column -> card field ->
                # dashboard`` survives to the report, and the ``role``
                # (field/filter/breakout/...) rides along for display. A column used in several
                # roles on one card keeps the first role seen — it is still one via-column edge.
                column_cards.setdefault((model, ref.column.lower()), {}).setdefault(
                    card.card_id, ref.role
                )

            if card.precision != "column":
                for relation in card.table_relations:
                    model = relation_index.get(normalize_relation(relation))
                    if model is None:
                        continue
                    model_cards.setdefault(model, set()).add(card.card_id)

        dashboards_by_card: dict[int, list[MetabaseDashboard]] = {}
        dashboard_by_name: dict[str, MetabaseDashboard] = {}
        for dashboard in lineage.dashboards:
            dashboard_by_name[dashboard_reach_name(dashboard.dashboard_id)] = dashboard
            for card_id in dashboard.card_ids:
                dashboards_by_card.setdefault(card_id, []).append(dashboard)

        return cls(column_cards, model_cards, dashboards_by_card, dashboard_by_name)

    # -- reach queries -------------------------------------------------------

    def reached_dashboards(
        self,
        columns: Iterable[tuple[str, str]],
        models: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Dashboards reached by any of ``columns`` (column-precise) or ``models`` (table grain).

        ``columns`` is the set of ``(model, column)`` the change touches — the changed column
        itself plus every downstream column the dbt reach already resolved; ``models`` is those
        models' names (for table-only cards). Returns one exposure-shaped entry per reached
        dashboard, deterministically ordered by dashboard id, ready to append onto
        ``affected_exposures`` and each change's ``reached_exposures``.
        """
        hits: dict[int, dict[str, Any]] = {}

        def _record(
            card_id: int,
            precision: str,
            matched: tuple[str, str] | None = None,
            role: str | None = None,
        ) -> None:
            for dashboard in self._dashboards_by_card.get(card_id, ()):
                entry = hits.setdefault(
                    dashboard.dashboard_id,
                    {
                        "dashboard": dashboard,
                        "via_cards": set(),
                        "precision": "table",
                        "via_columns": {},
                    },
                )
                entry["via_cards"].add(card_id)
                if precision == "column":
                    entry["precision"] = "column"
                    if matched is not None:
                        # Key by (model, column, card) so the same warehouse column reached
                        # through the same card dedupes; the role rides along for display. This
                        # is the column-precise chain F4 asks for — never dropped to card grain.
                        entry["via_columns"][(matched[0], matched[1], card_id)] = role or ""

        for model, column in columns:
            key = (model.lower(), column.lower())
            for card_id, role in self._column_cards.get(key, {}).items():
                _record(card_id, "column", matched=key, role=role)
        for model in models:
            for card_id in self._model_cards.get(model.lower(), ()):
                _record(card_id, "table")

        entries: list[dict[str, Any]] = []
        for dashboard_id in sorted(hits):
            info = hits[dashboard_id]
            dashboard: MetabaseDashboard = info["dashboard"]
            # The full column chain, deterministically ordered. Empty for a table-grain-only
            # reach (native ``select *`` etc.) — honest: we know the dashboard is reached but
            # not which column, and ``precision`` already says so.
            via_columns = [
                {"model": model, "column": column, "card_id": card_id, "role": role}
                for (model, column, card_id), role in sorted(info["via_columns"].items())
            ]
            entries.append(
                {
                    "name": dashboard_reach_name(dashboard_id),
                    "type": "dashboard",
                    "url": dashboard.url,
                    "source": "metabase",
                    "owner": None,
                    "meta": dict(dashboard.meta),
                    "via_cards": sorted(info["via_cards"]),
                    "via_columns": via_columns,
                    "precision": info["precision"],
                    "depends_on_models": [],
                }
            )
        return entries

    def dashboard_meta(self, name: str) -> dict[str, Any] | None:
        """Resolve a reached dashboard's meta for the policy ``reach.where`` clause.

        ``name`` is the synthetic ``metabase.dashboard.<id>``. Returns the dashboard's ``meta``
        merged with the reserved provenance keys (``source=metabase``, ``name``, ``url``) so a
        policy can match either ``meta.tier`` (consumer data) or ``meta.source`` (provenance).
        ``None`` when the name is not a known dashboard (the caller falls back to the registry).
        """
        dashboard = self._dashboard_by_name.get(name)
        if dashboard is None:
            return None
        merged: dict[str, Any] = {"source": "metabase", "name": dashboard.name}
        if dashboard.url is not None:
            merged["url"] = dashboard.url
        merged.update(dashboard.meta)
        return merged


def build_reach_confidence(
    lineage: MetabaseLineage | None,
    reached_entries: list[dict[str, Any]],
    max_age_hours: float = 24.0,
    now: datetime | None = None,
) -> MetabaseReachConfidence:
    """Summarise the honesty of the appended Metabase reach.

    Reports snapshot staleness (age of ``provenance.generated_at`` vs ``max_age_hours``) and how
    many reached dashboards are backed by a column-precise vs a table-only card. An absent
    snapshot is ``level="absent"``; a present-but-stale one is ``stale=True`` and ``partial``.
    """
    if lineage is None:
        return MetabaseReachConfidence(level="absent")

    generated_at = lineage.provenance.generated_at
    age_hours: float | None = None
    parsed = _parse_iso8601(generated_at)
    if parsed is not None:
        reference = now or datetime.now(timezone.utc)
        age_hours = max(0.0, (reference - parsed).total_seconds() / 3600.0)
    stale = age_hours is None or age_hours > max_age_hours

    column_precise = sum(1 for entry in reached_entries if entry.get("precision") == "column")
    table_only = len(reached_entries) - column_precise
    level: str
    if not reached_entries:
        level = "full" if not stale else "partial"
    else:
        level = "full" if (not stale and table_only == 0) else "partial"

    return MetabaseReachConfidence(
        snapshot_generated_at=generated_at,
        snapshot_age_hours=age_hours,
        stale=stale,
        dashboards_reached=len(reached_entries),
        cards_column_precise=column_precise,
        cards_table_only=table_only,
        level=level,  # type: ignore[arg-type]
    )


def _parse_iso8601(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``. ``None`` on failure."""
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
