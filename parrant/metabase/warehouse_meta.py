"""support — in-memory warehouse metadata + the card/snippet corpus.

:class:`WarehouseMeta` turns Metabase's bulk ``GET /api/database/:id/metadata`` into fast
lookups both resolvers need: Table/Field **id** → warehouse relation/column (for MBQL) and
relation **name** → relation (for parsed native SQL). Everything normalizes to a lowercased,
unquoted ``database.schema.table`` key so the two resolvers converge on one anchor.

:class:`CardCorpus` holds every fetched card + snippet by id/name so the native resolver can
expand ``{{#card}}`` / ``{{snippet}}`` template tags transitively (with a cycle guard).
"""

from __future__ import annotations

from parrant.models.schema import MetabaseRelation


def normalize_ident(value: str) -> str:
    """Lowercase and strip quoting (``"`` and backticks) from one identifier part."""
    return value.strip().strip('"').strip("`").lower()


def relation_key(database: str, schema: str, table: str) -> str:
    """The normalized ``database.schema.table`` key used throughout the artifact."""
    return f"{normalize_ident(database)}.{normalize_ident(schema)}.{normalize_ident(table)}"


def _database_name(metadata: dict) -> str:
    """Best-effort warehouse database name for a ``/api/database/:id/metadata`` response.

    Prefers the connection detail (``details.db`` / ``details.dbname`` — the real warehouse
    database, e.g. Snowflake ``ANALYTICS``), falling back to the Metabase display ``name``.
    """
    details = metadata.get("details") or {}
    for candidate in (details.get("db"), details.get("dbname"), metadata.get("name")):
        if candidate:
            return str(candidate)
    return ""


class WarehouseMeta:
    """Resolved Metabase warehouse metadata across one or more databases."""

    def __init__(self) -> None:
        self.relations: dict[str, MetabaseRelation] = {}
        # Field id -> (relation_key, column_name)
        self._field_by_id: dict[int, tuple[str, str]] = {}
        # Table id -> relation_key
        self._table_by_id: dict[int, str] = {}
        # name lookups: "table", "schema.table", "db.schema.table" -> relation_key
        self._by_name: dict[str, str] = {}
        # name collisions on the bare-table key: once ambiguous, never guess (drop it)
        self._ambiguous_names: set = set()

    @classmethod
    def from_database_metadata(cls, metadatas: list[dict]) -> WarehouseMeta:
        """Build from a list of ``/api/database/:id/metadata`` response bodies."""
        meta = cls()
        for metadata in metadatas:
            meta._ingest(metadata)
        return meta

    def _ingest(self, metadata: dict) -> None:
        database = _database_name(metadata)
        for table in metadata.get("tables") or []:
            schema = table.get("schema") or ""
            table_name = table.get("name") or ""
            if not table_name:
                continue
            key = relation_key(database, schema, table_name)
            self.relations.setdefault(
                key,
                MetabaseRelation(
                    database=normalize_ident(database),
                    schema=normalize_ident(schema),
                    table=normalize_ident(table_name),
                ),
            )
            table_id = table.get("id")
            if isinstance(table_id, int):
                self._table_by_id[table_id] = key
            self._register_name(normalize_ident(table_name), key)
            self._register_name(f"{normalize_ident(schema)}.{normalize_ident(table_name)}", key)
            self._register_name(key, key)
            for field in table.get("fields") or []:
                field_id = field.get("id")
                column = field.get("name")
                if isinstance(field_id, int) and column:
                    self._field_by_id[field_id] = (key, normalize_ident(column))

    def _register_name(self, name: str, key: str) -> None:
        if name in self._ambiguous_names:
            return
        existing = self._by_name.get(name)
        if existing is not None and existing != key:
            # Ambiguous bare/short name across schemas/dbs — never guess which one.
            self._ambiguous_names.add(name)
            self._by_name.pop(name, None)
            return
        self._by_name[name] = key

    # --- id lookups (MBQL) ------------------------------------------------
    def field(self, field_id: int) -> tuple[str, str] | None:
        """``field_id`` → ``(relation_key, column)`` or ``None`` if unknown."""
        return self._field_by_id.get(field_id)

    def table(self, table_id: int) -> str | None:
        """``table_id`` → relation_key or ``None`` if unknown."""
        return self._table_by_id.get(table_id)

    # --- name lookups (native SQL) ----------------------------------------
    def resolve_name(self, raw_name: str) -> str | None:
        """Resolve a SQL table reference to a relation_key.

        Tries the fully-qualified name first, then ``schema.table``, then the bare table
        name. Ambiguous bare names (same table in several schemas) resolve to ``None`` —
        counted as unresolved, never guessed (spec Q7).
        """
        parts = [normalize_ident(p) for p in raw_name.split(".") if p]
        if not parts:
            return None
        candidates = [".".join(parts)]
        if len(parts) >= 2:
            candidates.append(".".join(parts[-2:]))
        candidates.append(parts[-1])
        for candidate in candidates:
            key = self._by_name.get(candidate)
            if key is not None:
                return key
        return None

    def relation(self, key: str) -> MetabaseRelation | None:
        return self.relations.get(key)


class CardCorpus:
    """Every fetched card + snippet, indexed for transitive template-tag expansion."""

    def __init__(self, cards: list[dict], snippets: list[dict]) -> None:
        self.cards_by_id: dict[int, dict] = {
            c["id"]: c for c in cards if isinstance(c.get("id"), int)
        }
        self.snippets_by_id: dict[int, dict] = {
            s["id"]: s for s in snippets if isinstance(s.get("id"), int)
        }
        self.snippets_by_name: dict[str, dict] = {
            str(s.get("name", "")).lower(): s for s in snippets if s.get("name")
        }

    def card(self, card_id: int) -> dict | None:
        return self.cards_by_id.get(card_id)

    def snippet_by_id(self, snippet_id: int) -> dict | None:
        return self.snippets_by_id.get(snippet_id)

    def snippet_by_name(self, name: str) -> dict | None:
        return self.snippets_by_name.get(name.lower())
