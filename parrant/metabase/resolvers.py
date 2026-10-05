"""— the two card resolvers, converging on one warehouse anchor.

Both resolvers terminate at a warehouse ``database.schema.table.column`` and return the same
:class:`ResolvedCard`, so the extractor treats them uniformly:

* **MBQL** (query-builder cards) — column-precise from Metabase's structured Table/Field
  metadata graph. **No SQL parsing.**
* **native SQL** — expand ``{{#card}}`` / ``{{snippet}}`` / field-filter template tags
  transitively, then **REUSE** the product's SQLGlot engine
  (:class:`~parrant.parser.sql_parser.SQLColumnParser`) — no second lineage
  engine. Degrade safely to table grain on ``select *`` / unparseable / unknown-table SQL.

A :class:`CardResolver` carries the shared corpus/meta/parser and memoizes + cycle-guards
card→card recursion (``card__<id>`` sources and ``{{#id}}`` tags).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from parrant.metabase.pmbql import normalize_dataset_query
from parrant.metabase.warehouse_meta import CardCorpus, WarehouseMeta
from parrant.models.schema import MetabaseColumnRef
from parrant.parser.sql_parser import SQLColumnParser

# The roles a resolved column may carry (matches ``MetabaseColumnRef.role``).
Role = Literal["field", "breakout", "aggregation", "filter", "join", "order", "native"]

# Placeholder table token a ``{{#123}}`` card reference is rewritten to before parsing, so
# the SQL stays parseable and the injected relation is mappable back to the upstream card.
_CARD_TOKEN = "__card_{id}"
_CARD_TOKEN_RE = re.compile(r"^__card_(\d+)$")

_CARD_TAG_RE = re.compile(r"\{\{\s*#\s*(\d+)[^}]*\}\}")
_SNIPPET_TAG_RE = re.compile(r"\{\{\s*snippet:\s*([^}]+?)\s*\}\}", re.IGNORECASE)
_VAR_TAG_RE = re.compile(r"\{\{\s*([^#}][^}]*?)\s*\}\}")


@dataclass
class ResolvedCard:
    """The unified resolver output the extractor turns into a ``MetabaseCard``."""

    precision: str  # "column" | "table" | "none"
    columns: list[MetabaseColumnRef] = field(default_factory=list)
    table_relations: list[str] = field(default_factory=list)
    upstream_card_ids: list[int] = field(default_factory=list)
    snippet_ids: list[int] = field(default_factory=list)
    unresolved_reason: str | None = None


def _iter_field_refs(node: Any) -> list[list]:
    """Recursively collect every MBQL ``["field", <id|name>, opts]`` clause under ``node``."""
    found: list[list] = []
    if isinstance(node, list):
        if node and node[0] == "field":
            found.append(node)
        else:
            for item in node:
                found.extend(_iter_field_refs(item))
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_iter_field_refs(value))
    return found


class CardResolver:
    """Resolves a card to a :class:`ResolvedCard`, recursing into card sources safely."""

    def __init__(
        self,
        meta: WarehouseMeta,
        corpus: CardCorpus,
        dialect: str | None,
        parser: SQLColumnParser | None = None,
    ) -> None:
        self.meta = meta
        self.corpus = corpus
        self.dialect = dialect
        self.parser = parser or SQLColumnParser(dialect)
        self._cache: dict[int, ResolvedCard] = {}
        self._resolving: set[int] = set()

    # --- entry point ------------------------------------------------------
    def resolve_card(self, card: dict) -> ResolvedCard:
        """Resolve ``card`` to a :class:`ResolvedCard`.

        **Mutates ``card`` in place**: a pMBQL (MBQL 5) ``dataset_query`` — as returned by
        Metabase v0.57+ — is normalized to the legacy MBQL shape and written back to
        ``card["dataset_query"]`` at entry. This keeps all downstream resolution logic (and
        its recursion into upstream cards) legacy-only, and — critically — lets
        :func:`parrant.metabase.extract._to_card` read the corrected ``dataset_query["type"]``
        / ``["database"]`` (e.g. a native pMBQL card ends up ``type == "native"``). Legacy
        input passes through untouched (the normalizer is a no-op / idempotent).
        """
        card_id = card.get("id")
        if isinstance(card_id, int) and card_id in self._cache:
            return self._cache[card_id]
        raw_query = card.get("dataset_query")
        if isinstance(raw_query, dict):
            query = normalize_dataset_query(raw_query)
            card["dataset_query"] = query
        else:
            query = {}
        if query.get("type") == "native":
            resolved = self.resolve_native(query, card)
        elif query.get("type") == "query":
            resolved = self.resolve_mbql(query, card)
        else:
            resolved = ResolvedCard(precision="none", unresolved_reason="unknown_query_kind")
        if isinstance(card_id, int):
            self._cache[card_id] = resolved
        return resolved

    def _resolve_card_id(self, card_id: int) -> ResolvedCard:
        """Resolve a referenced card by id with a cycle guard (spec Q3)."""
        if card_id in self._resolving:
            return ResolvedCard(precision="table", unresolved_reason="cycle")
        card = self.corpus.card(card_id)
        if card is None:
            return ResolvedCard(precision="none", unresolved_reason="unknown_card")
        self._resolving.add(card_id)
        try:
            return self.resolve_card(card)
        finally:
            self._resolving.discard(card_id)

    # --- MBQL -------------------------------------------------------------
    def resolve_mbql(self, query: dict, card: dict) -> ResolvedCard:
        inner = query.get("query", query)
        acc = _MbqlAccumulator()
        self._resolve_mbql_query(inner, acc)
        columns = list(acc.columns.values())
        table_relations = sorted(acc.relations)
        if columns:
            precision = "column"
            reason = None
        elif table_relations:
            precision = "table"
            reason = "unknown_field" if acc.unknown_field else None
        else:
            precision = "none"
            reason = "unknown_table"
        return ResolvedCard(
            precision=precision,
            columns=columns,
            table_relations=table_relations,
            upstream_card_ids=sorted(acc.upstream_card_ids),
            snippet_ids=[],
            unresolved_reason=reason,
        )

    def _resolve_mbql_query(self, query: dict, acc: _MbqlAccumulator) -> None:
        """Resolve one (possibly nested) MBQL query into ``acc``.

        ``name_map`` (column name → (relation_key, column)) lets field-by-name refs — common
        when the source is a card / nested query — resolve against the source's output.
        """
        name_map: dict[str, tuple[str, str]] = {}
        source = query.get("source-table")
        if isinstance(source, str) and source.startswith("card__"):
            try:
                src_card_id = int(source.split("__", 1)[1])
            except ValueError:
                src_card_id = -1
            if src_card_id >= 0:
                acc.upstream_card_ids.add(src_card_id)
                sub = self._resolve_card_id(src_card_id)
                acc.relations.update(sub.table_relations)
                for ref in sub.columns:
                    acc.relations.add(ref.relation)
                    name_map[ref.column] = (ref.relation, ref.column)
        elif isinstance(source, int):
            key = self.meta.table(source)
            if key is not None:
                acc.relations.add(key)
        nested = query.get("source-query")
        if isinstance(nested, dict):
            self._resolve_mbql_query(nested, acc)

        # Each clause contributes fields with a distinct role.
        clause_roles: list[tuple[str, Role]] = [
            ("fields", "field"),
            ("breakout", "breakout"),
            ("aggregation", "aggregation"),
            ("filter", "filter"),
            ("order-by", "order"),
            ("expressions", "field"),
        ]
        for clause, role in clause_roles:
            self._collect_clause(query.get(clause), role, acc, name_map)

        for join in query.get("joins") or []:
            js = join.get("source-table")
            if isinstance(js, int):
                key = self.meta.table(js)
                if key is not None:
                    acc.relations.add(key)
            elif isinstance(js, str) and js.startswith("card__"):
                try:
                    jc = int(js.split("__", 1)[1])
                    acc.upstream_card_ids.add(jc)
                    sub = self._resolve_card_id(jc)
                    acc.relations.update(sub.table_relations)
                    for ref in sub.columns:
                        acc.relations.add(ref.relation)
                except ValueError:
                    pass
            self._collect_clause(join.get("condition"), "join", acc, name_map)
            self._collect_clause(join.get("fields"), "field", acc, name_map)

    def _collect_clause(
        self,
        node: Any,
        role: Role,
        acc: _MbqlAccumulator,
        name_map: dict[str, tuple[str, str]],
    ) -> None:
        if node is None:
            return
        for ref in _iter_field_refs(node):
            if len(ref) < 2:
                continue
            target = ref[1]
            if isinstance(target, int):
                resolved = self.meta.field(target)
                if resolved is None:
                    acc.unknown_field = True
                    continue
                relation_key, column = resolved
            elif isinstance(target, str):
                by_name = name_map.get(target.lower())
                if by_name is None:
                    acc.unknown_field = True
                    continue
                relation_key, column = by_name
            else:
                continue
            acc.relations.add(relation_key)
            key = (relation_key, column)
            # First-seen role wins (a projected field outranks a later filter mention).
            if key not in acc.columns:
                acc.columns[key] = MetabaseColumnRef(
                    relation=relation_key, column=column, role=role, confidence="high"
                )

    # --- native SQL -------------------------------------------------------
    def resolve_native(self, query: dict, card: dict) -> ResolvedCard:
        native = query.get("native") or {}
        sql = native.get("query") or ""
        tags = native.get("template-tags") or {}

        expanded, upstream_card_ids, snippet_ids, synthetic, dim_columns = self._expand_tags(
            sql, tags, visited_cards=set(), visited_snippets=set()
        )

        columns: dict[tuple[str, str], MetabaseColumnRef] = {}
        for ref in dim_columns:
            columns[(ref.relation, ref.column)] = ref
        table_relations: set[str] = set()
        reason: str | None = None

        try:
            result = self.parser.parse_column_lineage(expanded)
        except Exception:
            # Unparseable → cheap table extraction so table-grain reach is not lost.
            for name in self._extract_tables(expanded):
                key = self.meta.resolve_name(name)
                if key is not None:
                    table_relations.add(key)
            table_relations.update(self._synthetic_relations(synthetic))
            precision = "table" if table_relations else "none"
            return ResolvedCard(
                precision=precision,
                columns=list(columns.values()),
                table_relations=sorted(table_relations),
                upstream_card_ids=sorted(upstream_card_ids),
                snippet_ids=sorted(snippet_ids),
                unresolved_reason="parse_failed" if table_relations else "parse_failed",
            )

        # Projected columns → warehouse relation.column.
        for lineages in result.column_lineage.values():
            for lineage in lineages:
                for source in lineage.source_columns or set():
                    self._map_source_column(source, "native", synthetic, columns, table_relations)
        # Predicate-only columns → row-set reach (role=filter).
        for source in result.predicate_sources or set():
            self._map_source_column(source, "filter", synthetic, columns, table_relations)

        star = bool(result.star_sources)
        if star:
            reason = "select_star"
            for source in result.star_sources:
                table = source.rsplit(".", 1)[0] if "." in source else source
                token = _CARD_TOKEN_RE.match(table)
                if token:
                    table_relations.update(synthetic.get(int(token.group(1)), set()))
                    continue
                key = self.meta.resolve_name(table)
                if key is not None:
                    table_relations.add(key)
        table_relations.update(self._synthetic_relations(synthetic))
        # Any relation referenced by a resolved column is also (redundantly) a table hit.
        for ref in columns.values():
            table_relations.add(ref.relation)

        if columns and not star:
            precision = "column"
        elif columns or table_relations:
            precision = "table"
            if reason is None and not columns:
                reason = "unknown_table" if not table_relations else None
        else:
            precision = "none"
            reason = reason or "unknown_table"

        return ResolvedCard(
            precision=precision,
            columns=list(columns.values()),
            table_relations=sorted(table_relations),
            upstream_card_ids=sorted(upstream_card_ids),
            snippet_ids=sorted(snippet_ids),
            unresolved_reason=reason,
        )

    def _map_source_column(
        self,
        source: str,
        role: Role,
        synthetic: dict[int, set[str]],
        columns: dict[tuple[str, str], MetabaseColumnRef],
        table_relations: set[str],
    ) -> None:
        """Map a parser ``table.column`` source to a relation, or degrade to table grain."""
        if "." in source:
            table, column = source.rsplit(".", 1)
        else:
            table, column = "", source
        token = _CARD_TOKEN_RE.match(table)
        if token:
            # A column read through a card source → table-grain reach on the card's relations.
            table_relations.update(synthetic.get(int(token.group(1)), set()))
            return
        if not table:
            return
        key = self.meta.resolve_name(table)
        if key is None:
            return
        entry = (key, column.lower())
        if entry not in columns:
            columns[entry] = MetabaseColumnRef(
                relation=key, column=column.lower(), role=role, confidence="medium"
            )

    def _synthetic_relations(self, synthetic: dict[int, set[str]]) -> set[str]:
        out: set[str] = set()
        for relations in synthetic.values():
            out.update(relations)
        return out

    # --- template-tag expansion ------------------------------------------
    def _expand_tags(
        self,
        sql: str,
        tags: dict,
        visited_cards: set[int],
        visited_snippets: set[int],
    ) -> tuple[str, set[int], set[int], dict[int, set[str]], list[MetabaseColumnRef]]:
        """Substitute template tags so the SQL parses, returning expansion side-channels.

        Returns ``(expanded_sql, upstream_card_ids, snippet_ids, synthetic, dim_columns)``
        where ``synthetic`` maps a referenced card id → the warehouse relations it reads
        (so ``__card_<id>`` tokens resolve to table-grain reach) and ``dim_columns`` are the
        precisely-recovered field-filter columns (spec Q4).
        """
        upstream_card_ids: set[int] = set()
        snippet_ids: set[int] = set()
        synthetic: dict[int, set[str]] = {}
        dim_columns: list[MetabaseColumnRef] = []

        # Unwrap Metabase optional blocks [[ ... ]] so inner SQL/tags survive.
        expanded = sql.replace("[[", " ").replace("]]", " ")

        # Field-filter (dimension) tags: record the column precisely, replace with 1=1.
        for tag in tags.values():
            if tag.get("type") == "dimension":
                dim = tag.get("dimension")
                if isinstance(dim, list) and len(dim) >= 2 and isinstance(dim[1], int):
                    resolved = self.meta.field(dim[1])
                    if resolved is not None:
                        relation_key, column = resolved
                        dim_columns.append(
                            MetabaseColumnRef(
                                relation=relation_key,
                                column=column,
                                role="filter",
                                confidence="high",
                            )
                        )

        # Card references {{#123}} → a table token; inherit the card's relations.
        def _card_sub(match: re.Match[str]) -> str:
            card_id = int(match.group(1))
            upstream_card_ids.add(card_id)
            if card_id not in visited_cards:
                sub = self._resolve_card_id(card_id)
                relations: set[str] = set(sub.table_relations)
                for ref in sub.columns:
                    relations.add(ref.relation)
                synthetic[card_id] = relations
            return _CARD_TOKEN.format(id=card_id)

        expanded = _CARD_TAG_RE.sub(_card_sub, expanded)

        # Snippet references {{snippet: name}} → inline the snippet content (one level of
        # transitive expansion, cycle-guarded).
        def _snippet_sub(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            snippet = self.corpus.snippet_by_name(name)
            if snippet is None:
                return "(1=1)"
            sid = snippet.get("id")
            child_visited = visited_snippets
            if isinstance(sid, int):
                snippet_ids.add(sid)
                if sid in visited_snippets:
                    return "(1=1)"
                child_visited = visited_snippets | {sid}
            content = snippet.get("content") or ""
            inner, sub_cards, sub_snips, sub_syn, sub_dims = self._expand_tags(
                content, {}, visited_cards, child_visited
            )
            upstream_card_ids.update(sub_cards)
            snippet_ids.update(sub_snips)
            synthetic.update(sub_syn)
            dim_columns.extend(sub_dims)
            return f" {inner} "

        expanded = _SNIPPET_TAG_RE.sub(_snippet_sub, expanded)

        # Remaining variables (text/number/date/dimension placeholders) → safe literals.
        def _var_sub(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            tag = tags.get(name) or {}
            if tag.get("type") == "dimension":
                return "1=1"
            return "null"

        expanded = _VAR_TAG_RE.sub(_var_sub, expanded)
        return expanded, upstream_card_ids, snippet_ids, synthetic, dim_columns

    def _extract_tables(self, sql: str) -> set[str]:
        """Cheap table extraction for the parse-failed degrade path (sqlglot table walk)."""
        try:
            from sqlglot import exp, parse_one

            parsed = parse_one(sql, dialect=self.dialect)
        except Exception:
            return set()
        names: set[str] = set()
        for table in parsed.find_all(exp.Table):
            parts = [p for p in (table.catalog, table.db, table.name) if p]
            if parts:
                names.add(".".join(parts))
        return names


@dataclass
class _MbqlAccumulator:
    relations: set[str] = field(default_factory=set)
    columns: dict[tuple[str, str], MetabaseColumnRef] = field(default_factory=dict)
    upstream_card_ids: set[int] = field(default_factory=set)
    unknown_field: bool = False
