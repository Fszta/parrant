"""— join the Metabase artifact's warehouse relations back to dbt models.

Offline, zero-credential by construction: this module imports ONLY the lineage provider
protocol (for typing) and reads ``Model`` fields. It never imports the Metabase client, so
the gate path that builds a relation index carries no credentials.

**The join key is the warehouse relation.** dbt's authoritative physical identifier is the
manifest node ``relation_name`` (already adapter-cased + quoted, e.g.
``"ANALYTICS"."MARTS"."DIM_ACCOUNTS"``); ``Model.name`` can differ from the physical table via
``alias``/``identifier``, so we PREFER ``relation_name`` (supplied by an optional resolver) and
fall back to ``(database, schema, name)``. Both sides normalize to ``db.schema.table`` —
lowercased, quotes/brackets stripped — so a Snowflake ``"ANALYTICS"."MARTS"."DIM"`` and a
Metabase-side ``analytics.marts.dim`` meet.
"""

from __future__ import annotations

from collections.abc import Callable

from parrant.lineage.provider import LineageProvider


def normalize_relation(raw: str) -> str:
    """Normalize a warehouse relation to ``db.schema.table``.

    Lowercased, with quotes (``"``), backticks (BigQuery) and brackets (T-SQL) stripped and
    surrounding whitespace trimmed. Idempotent: an already-normalized artifact key passes
    through unchanged.
    """
    cleaned = (
        raw.replace('"', "").replace("`", "").replace("[", "").replace("]", "").strip().lower()
    )
    return cleaned


def build_relation_index(
    provider: LineageProvider,
    get_relation_name: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """Build ``normalized db.schema.table -> dbt model name`` (lowercased model keys).

    Prefer the manifest ``relation_name`` (via ``get_relation_name(model_name)``, when
    supplied); always also index the model's ``(database, schema_name, name)`` as a fallback.
    Additionally index the bare ``schema.table`` when — and only when — it is UNAMBIGUOUS (a
    Metabase instance pointing at a single database omits the db component); a ``schema.table``
    owned by two different models is dropped rather than guessed.

    Pure and offline: reads only ``provider.get_models()`` plus the optional resolver.
    """
    full: dict[str, str] = {}
    schema_table_owners: dict[str, set[str]] = {}

    for name, model in provider.get_models().items():
        keys: set[str] = set()
        if get_relation_name is not None:
            relation_name = get_relation_name(name)
            if relation_name:
                keys.add(normalize_relation(relation_name))
        database = getattr(model, "database", None)
        schema = getattr(model, "schema_name", None)
        table = getattr(model, "name", None)
        if database and schema and table:
            keys.add(normalize_relation(f"{database}.{schema}.{table}"))

        for key in keys:
            # First writer wins for an exact key; a later duplicate does not clobber it.
            full.setdefault(key, name)
            parts = key.split(".")
            if len(parts) == 3:
                schema_table_owners.setdefault(".".join(parts[1:]), set()).add(name)

    for schema_table, owners in schema_table_owners.items():
        # Only an unambiguous schema.table becomes a fallback, and never over an exact key.
        if len(owners) == 1 and schema_table not in full:
            full[schema_table] = next(iter(owners))

    return full
