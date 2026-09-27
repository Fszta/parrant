import re
import logging
from dataclasses import dataclass, field
from sqlglot import parse_one, exp
from typing import Dict, List, Set, Optional, Any, Callable, Literal, Tuple, cast
from parrant.models.schema import (
    ColumnLineage,
    OverrideDirective,
    OverrideVerb,
    SQLParseResult,
    UnresolvedColumnEdge,
)
from parrant.parser.sql_parser_utils import (
    get_table_aliases,
    get_lateral_flatten_aliases,
    get_flatten_alias_nodes,
    get_table_context,
    get_all_tables_from_select,
    get_final_selects,
    split_qualified_name,
    strip_sql_comments,
)

logging.getLogger("sqlglot").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# override pragma parsing (`-- lineage:allow-change` / `-- lineage:allow-break`).
#
# sqlglot comment<->node association is unreliable, so we scan the RAW SQL TEXT line by
# line rather than the AST. The pragma lives in the HEAD model's own SQL (diffable,
# reviewed like any code). A malformed / reasonless / unknown-verb pragma is DROPPED and
# reported as a loud warning — it must never alter the ruling silently (audit invariant).
# ---------------------------------------------------------------------------

_OVERRIDE_LINE_RE = re.compile(
    r"--\s*lineage:allow-(?P<verb>[a-z0-9-]+)\b(?P<args>.*)$", re.IGNORECASE
)
# reason="..." or reason='...' — the quoted value is extracted FIRST so a ``column=`` that
# happens to sit inside the reason text is not mis-read as the target column arg.
_REASON_RE = re.compile(r"""reason\s*=\s*(?P<q>["'])(?P<val>.*?)(?P=q)""", re.IGNORECASE)
_COLUMN_ARG_RE = re.compile(r"""column\s*=\s*"?(?P<val>[A-Za-z_][\w]*)"?""", re.IGNORECASE)
_SELECT_WORD_RE = re.compile(r"\bselect\b", re.IGNORECASE)
# ` as <alias>` in a SELECT-list line (the projected output name).
_ALIAS_AS_RE = re.compile(r"\bas\s+\"?(?P<a>[A-Za-z_]\w*)\"?", re.IGNORECASE)
# last bare identifier before an optional trailing comma (fallback alias extraction).
_TRAILING_IDENT_RE = re.compile(r'(?:"(?P<q>[^"]+)"|(?P<b>[A-Za-z_]\w*))\s*$')


def _extract_select_alias(line: str) -> Optional[str]:
    """Best-effort pull of the projected column name from a SELECT-list line.

    Prefers the token after a case-insensitive `` as `` (the explicit alias); else the last
    bare identifier before an optional trailing comma, stripping quotes. Lowercased. Returns
    ``None`` when nothing looks like a column (the caller then treats the pragma as an
    unresolved => stale override rather than silently excusing the wrong column).
    """
    # Drop any trailing line comment so `x as y  -- note` doesn't confuse extraction.
    code = line.split("--", 1)[0].strip().rstrip(",").strip()
    if not code:
        return None
    m = _ALIAS_AS_RE.search(code)
    if m:
        return m.group("a").lower()
    m2 = _TRAILING_IDENT_RE.search(code)
    if m2:
        token = m2.group("q") or m2.group("b")
        if token:
            return token.lower()
    return None


def _adjacent_column(lines: List[str], pragma_idx: int) -> Optional[str]:
    """Scan forward from the pragma line to the next non-blank, non-comment line and extract
    its projected column via :func:`_extract_select_alias`. Best-effort (``None`` => stale)."""
    for j in range(pragma_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("--"):
            continue
        return _extract_select_alias(lines[j])
    return None


def parse_override_directives(sql: str) -> Tuple[List[OverrideDirective], List[str]]:
    """Parse ``-- lineage:allow-(change|break) ...`` pragmas from raw head SQL.

    Returns ``(directives, warnings)``. A pragma is DROPPED (contributing a human warning
    string, never a directive) when its verb is unknown or its ``reason=`` is missing/empty —
    so a malformed override can never silently change the ruling. Scope resolution:
      * explicit ``column=<ident>`` => ``column`` scope;
      * else if the pragma line is BEFORE the first line containing a ``select`` keyword =>
        ``model`` scope (``column=None``, excuses every changed column of the model);
      * else line-adjacency: the projected column of the next code line => ``column`` scope
        (``column`` may be ``None`` when adjacency can't resolve => the caller marks it stale).
    """
    lines = sql.splitlines()
    first_select_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if _SELECT_WORD_RE.search(line):
            first_select_idx = i
            break

    directives: List[OverrideDirective] = []
    warnings: List[str] = []
    for i, line in enumerate(lines):
        m = _OVERRIDE_LINE_RE.search(line)
        if not m:
            continue
        line_no = i + 1
        verb_suffix = m.group("verb").lower()
        args = m.group("args")
        try:
            verb = OverrideVerb("allow-" + verb_suffix)
        except ValueError:
            warnings.append(
                f"line {line_no}: unknown override verb 'allow-{verb_suffix}' — pragma ignored"
            )
            continue
        reason_match = _REASON_RE.search(args)
        if reason_match is None or not reason_match.group("val").strip():
            warnings.append(
                f"line {line_no}: 'allow-{verb_suffix}' pragma has no non-empty reason= "
                "— pragma ignored (an override must always be justified)"
            )
            continue
        reason = reason_match.group("val").strip()
        # Look for column= OUTSIDE the quoted reason span (so reason="see column=x" is safe).
        args_wo_reason = args[: reason_match.start()] + args[reason_match.end() :]
        column_match = _COLUMN_ARG_RE.search(args_wo_reason)
        column: Optional[str]
        scope: Literal["column", "model"]
        if column_match:
            column = column_match.group("val").lower()
            scope = "column"
        elif first_select_idx is not None and i < first_select_idx:
            column = None
            scope = "model"
        else:
            column = _adjacent_column(lines, i)
            scope = "column"
        directives.append(
            OverrideDirective(
                verb=verb,
                column=column,
                reason=reason,
                scope=scope,
                source_line=line_no,
            )
        )
    return directives, warnings


@dataclass
class ParserContext:
    """Context object containing parser state and dependencies."""

    aliases: Dict[str, str]
    table_context: str
    cte_sources: Dict[str, Dict[str, str]]
    cte_to_model: Optional[Dict[str, str]]
    cte_transformation_types: Dict[str, Dict[str, str]] = field(default_factory=dict)
    cte_sql_expressions: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)
    cte_base_tables: Dict[str, Set[str]] = field(default_factory=dict)
    # Additional per-column sources contributed by non-left UNION branches of a CTE.
    # cte_sources holds a single primary source per column; these are merged in on top
    # so a CTE built from a UNION is not reduced to only its left-most branch.
    cte_extra_sources: Dict[str, Dict[str, Set[str]]] = field(default_factory=dict)
    column_definitions: Optional[Dict[str, Any]] = None


class CTEHandler:
    def extract_cte_model_mappings_from_parsed(self, parsed: Any) -> Dict[str, str]:
        mappings = {}
        for cte in parsed.find_all(exp.CTE):
            cte_name = cte.alias
            select = cte.this.find(exp.Select)
            if select:
                base_table = get_table_context(select)
                if base_table:
                    mappings[cte_name] = base_table
        return mappings

    def trace_base_tables(
        self,
        table: str,
        cte_to_model: Optional[Dict[str, str]],
        cte_sources: Dict[str, Dict[str, str]],
        star_sources: Set[str],
    ) -> None:
        if cte_to_model is None:
            if table not in cte_sources:
                star_sources.add(table)
            return

        trace_table = table
        trace_visited = set()

        while trace_table in cte_to_model and trace_table not in trace_visited:
            trace_visited.add(trace_table)
            next_table = cte_to_model[trace_table]
            if next_table == trace_table or next_table == trace_table.split(".")[-1]:
                star_sources.add(trace_table)
                break
            trace_table = next_table
        else:
            if trace_table not in cte_sources:
                star_sources.add(trace_table)


class StarExpressionHandler:
    def __init__(self) -> None:
        self._cte_handler: Optional[CTEHandler] = None

    def is_star_expression(self, expr: Any) -> bool:
        return isinstance(expr, exp.Star) or (
            isinstance(expr, exp.Column) and getattr(expr, "is_star", False)
        )

    def get_star_source_table(self, expr: Any, aliases: Dict[str, str], table_context: str) -> str:
        if isinstance(expr, exp.Column) and expr.table:
            star_table_alias = str(expr.table)
            return aliases.get(star_table_alias, star_table_alias)
        else:
            return table_context

    def get_excluded_columns(self, star_expr: exp.Star) -> List[str]:
        excluded = []
        if hasattr(star_expr, "args") and "except" in star_expr.args:
            except_clause = star_expr.args["except"]
            if except_clause:
                for col_expr in except_clause:
                    if isinstance(col_expr, exp.Column):
                        col_name = (
                            str(col_expr.this) if hasattr(col_expr, "this") else str(col_expr)
                        )
                        # Strip SQL comments from excluded column names
                        col_name = strip_sql_comments(col_name)
                        excluded.append(col_name)
        return excluded

    def get_cte_transformation_info(
        self, context: ParserContext, cte_name: str, col_name: str
    ) -> tuple[str, Optional[str]]:
        trans_type = context.cte_transformation_types.get(cte_name, {}).get(col_name, "direct")
        sql_expr = context.cte_sql_expressions.get(cte_name, {}).get(col_name)
        return trans_type, sql_expr

    def expand_from_join_tables(
        self,
        select: Any,
        all_tables: List[str],
        excluded_col_names: Set[str],
        context: ParserContext,
        columns: Dict[str, List[ColumnLineage]],
        star_sources: Set[str],
    ) -> None:
        for join_table in all_tables:
            if join_table in context.cte_sources:
                if len(context.cte_sources[join_table]) > 0:
                    for col_name, col_source in sorted(context.cte_sources[join_table].items()):
                        if col_name.lower() not in excluded_col_names:
                            trans_type, sql_expr = self.get_cte_transformation_info(
                                context, join_table, col_name
                            )
                            columns[col_name.lower()] = [
                                ColumnLineage(
                                    source_columns={col_source},
                                    transformation_type=cast(
                                        Literal["direct", "renamed", "derived"], trans_type
                                    ),
                                    sql_expression=sql_expr,
                                )
                            ]
                if join_table in context.cte_base_tables:
                    star_sources.update(context.cte_base_tables[join_table])
                if self._cte_handler:
                    self._cte_handler.trace_base_tables(
                        join_table, context.cte_to_model, context.cte_sources, star_sources
                    )
            elif context.cte_to_model and join_table in context.cte_to_model:
                star_sources.add(context.cte_to_model[join_table])

    def expand_from_cte(
        self,
        source_table: str,
        excluded_col_names: Set[str],
        context: ParserContext,
        columns: Dict[str, List[ColumnLineage]],
        star_sources: Set[str],
    ) -> bool:
        if source_table in context.cte_sources:
            if len(context.cte_sources[source_table]) > 0:
                for col_name, col_source in sorted(context.cte_sources[source_table].items()):
                    if col_name.lower() not in excluded_col_names:
                        trans_type, sql_expr = self.get_cte_transformation_info(
                            context, source_table, col_name
                        )
                        columns[col_name.lower()] = [
                            ColumnLineage(
                                source_columns={col_source},
                                transformation_type=cast(
                                    Literal["direct", "renamed", "derived"], trans_type
                                ),
                                sql_expression=sql_expr,
                            )
                        ]

            if source_table in context.cte_base_tables:
                star_sources.update(context.cte_base_tables[source_table])

            if self._cte_handler:
                self._cte_handler.trace_base_tables(
                    source_table, context.cte_to_model, context.cte_sources, star_sources
                )
            return True
        return False


class ExpressionAnalyzer:
    def __init__(self, parser: "SQLColumnParser") -> None:
        self.parser = parser
        self._handlers: Dict[type, Callable[[Any, ParserContext, bool], List[ColumnLineage]]] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self.register_handler(exp.Alias, self._handle_alias)
        self.register_handler(exp.Column, self._handle_column)

    def register_handler(
        self, expr_type: type, handler: Callable[[Any, ParserContext, bool], List[ColumnLineage]]
    ) -> None:
        self._handlers[expr_type] = handler

    def analyze(
        self, expr: Any, context: ParserContext, is_aliased: bool = False
    ) -> List[ColumnLineage]:
        expr_type = type(expr)
        if expr_type in self._handlers:
            return self._handlers[expr_type](expr, context, is_aliased)
        return self._default_handler(expr, context)

    def _handle_alias(
        self, expr: exp.Alias, context: ParserContext, is_aliased: bool
    ) -> List[ColumnLineage]:
        return self.analyze(expr.this, context, is_aliased=True)

    def _handle_column(
        self, expr: exp.Column, context: ParserContext, is_aliased: bool
    ) -> List[ColumnLineage]:
        col_name = (
            str(expr.this).lower() if hasattr(expr, "this") and expr.this else str(expr).lower()
        )
        # Strip SQL comments that might be included in the column name
        col_name = strip_sql_comments(col_name)

        forward_result = self.parser._handle_forward_reference(expr, col_name, context)
        if forward_result is not None:
            return forward_result

        return self.parser._analyze_column_reference(expr, col_name, context, is_aliased)

    def _default_handler(self, expr: Any, context: ParserContext) -> List[ColumnLineage]:
        source_cols = self.parser._extract_source_columns(expr, context)
        normalized_source_cols = self.parser._normalize_source_columns(source_cols)
        return [
            ColumnLineage(
                source_columns=normalized_source_cols,
                transformation_type="derived",
                sql_expression=str(expr),
            )
        ]


class SQLColumnParser:
    def __init__(self, dialect: Optional[str] = None):
        self.dialect = dialect
        self._cte_handler = CTEHandler()
        self._star_handler = StarExpressionHandler()
        self._star_handler._cte_handler = self._cte_handler
        self._expression_analyzer = ExpressionAnalyzer(self)

    def parse_column_lineage(self, sql: str) -> SQLParseResult:
        parsed = parse_one(sql, dialect=self.dialect)
        cte_to_model = self._cte_handler.extract_cte_model_mappings_from_parsed(parsed)

        cte_transformation_types: Dict[str, Dict[str, str]] = {}
        cte_sql_expressions: Dict[str, Dict[str, Optional[str]]] = {}
        cte_base_tables: Dict[str, Set[str]] = {}
        cte_extra_sources: Dict[str, Dict[str, Set[str]]] = {}

        aliases = get_table_aliases(parsed)
        for cte in parsed.find_all(exp.CTE):
            cte_base_tables[cte.alias] = set()

        cte_sources = self._build_cte_sources(
            parsed,
            cte_to_model,
            cte_transformation_types,
            cte_sql_expressions,
            cte_base_tables,
            cte_extra_sources,
        )

        columns: Dict[str, List[ColumnLineage]] = {}
        star_sources: Set[str] = set()
        # Unresolved-edge markers collected during this parse (see UnresolvedColumnEdge).
        # ``model`` is left empty; the registry stamps the real node name.
        markers: List[UnresolvedColumnEdge] = []
        flatten_aliases = get_lateral_flatten_aliases(parsed)
        # flatten pseudo-alias -> the REAL upstream source columns of the expression it unnests
        # (e.g. `flatten(payload:items) f` -> {`raw_events.payload`}). Lets a downstream
        # `f.value:id` be attributed to the flattened column instead of the phantom `f` alias.
        flatten_alias_sources = self._build_flatten_alias_sources(
            parsed,
            cte_to_model,
            cte_sources,
            cte_transformation_types,
            cte_sql_expressions,
            cte_base_tables,
            cte_extra_sources,
        )
        # A `table(flatten(...))` alias is a flatten pseudo-relation too, but is not an
        # `exp.Lateral`; fold its alias in so the phantom-token detection recognises it.
        flatten_aliases |= set(flatten_alias_sources.keys())

        final_selects = get_final_selects(parsed)
        if not final_selects:
            selects_to_process: List[Any] = list(parsed.find_all(exp.Select))
        else:
            selects_to_process = list(final_selects)
            # `select * from <cte>`: expand the CTE's own SELECT(s). Using
            # get_final_selects on the CTE body pulls in *every* union branch,
            # not just the left-most one.
            if len(final_selects) == 1:
                final_select = final_selects[0]
                if len(final_select.expressions) == 1:
                    expr = final_select.expressions[0]
                    if self._star_handler.is_star_expression(expr):
                        from_clause = final_select.find(exp.From)
                        if from_clause:
                            table = from_clause.find(exp.Table)
                            if table:
                                table_name = str(table.name).lower()
                                for cte in parsed.find_all(exp.CTE):
                                    if cte.alias.lower() == table_name:
                                        cte_selects = get_final_selects(cte.this)
                                        if cte_selects:
                                            selects_to_process = cte_selects
                                        break

        for select in selects_to_process:
            table_context = get_table_context(select)

            column_definitions = {}
            for expr in select.expressions:
                col_name = expr.alias_or_name.lower()
                # Strip SQL comments that might be included in the column name
                col_name = strip_sql_comments(col_name)
                column_definitions[col_name] = expr

            context = ParserContext(
                aliases=aliases,
                table_context=table_context,
                cte_sources=cte_sources,
                cte_to_model=cte_to_model,
                cte_transformation_types=cte_transformation_types,
                cte_sql_expressions=cte_sql_expressions,
                cte_base_tables=cte_base_tables,
                cte_extra_sources=cte_extra_sources,
                column_definitions=column_definitions,
            )

            for expr in select.expressions:
                if self._star_handler.is_star_expression(expr):
                    # ``select * rename (old as new)``: parrant only reads ``except`` today, so a
                    # renamed output column is silently dropped from the expansion and its edge
                    # is unresolved. Declare it rather than leave a silent gap.
                    if isinstance(expr, exp.Star):
                        self._emit_star_rename_markers(expr, markers)
                    excluded_columns = (
                        self._star_handler.get_excluded_columns(expr)
                        if isinstance(expr, exp.Star)
                        else []
                    )
                    excluded_col_names = {col.lower() for col in excluded_columns}
                    source_table = self._star_handler.get_star_source_table(
                        expr, context.aliases, context.table_context
                    )

                    all_tables = get_all_tables_from_select(select)
                    if len(all_tables) > 1 and not isinstance(expr, exp.Column):
                        self._star_handler.expand_from_join_tables(
                            select,
                            all_tables,
                            excluded_col_names,
                            context,
                            columns,
                            star_sources,
                        )
                        continue

                    if self._star_handler.expand_from_cte(
                        source_table,
                        excluded_col_names,
                        context,
                        columns,
                        star_sources,
                    ):
                        continue

                    if context.cte_to_model and source_table in context.cte_to_model:
                        base_table = context.cte_to_model[source_table]
                        star_sources.add(base_table)
                        continue

                    self._cte_handler.trace_base_tables(
                        source_table,
                        context.cte_to_model,
                        context.cte_sources,
                        star_sources,
                    )
                    continue

                target_col = expr.alias_or_name.lower()
                # Strip SQL comments that might be included in the column name
                target_col = strip_sql_comments(target_col)
                lineage = self._expression_analyzer.analyze(expr, context)
                # Merge rather than overwrite: when processing multiple UNION branch
                # SELECTs, each branch contributes its own sources for the same output
                # column, so keep every distinct branch's lineage.
                if target_col in columns:
                    for lin in lineage:
                        if lin not in columns[target_col]:
                            columns[target_col].append(lin)
                else:
                    columns[target_col] = list(lineage)

        # Strip phantom/pivot-literal source tokens and declare them as unresolved edges,
        # instead of laundering them into confident-but-wrong source columns. The model-level
        # edge (star_sources / any real remaining token) is preserved — only the fabricated
        # column-grain token is removed.
        self._declare_phantom_edges(columns, flatten_aliases, flatten_alias_sources, markers)

        predicate_lineage = self._extract_predicate_lineage(
            parsed,
            cte_to_model,
            cte_sources,
            cte_transformation_types,
            cte_sql_expressions,
            cte_base_tables,
        )

        return SQLParseResult(
            column_lineage=columns,
            star_sources=star_sources,
            predicate_sources=set(predicate_lineage.keys()),
            predicate_lineage=predicate_lineage,
            unresolved_edges=self._dedupe_markers(markers),
        )

    @staticmethod
    def _dedupe_markers(markers: List[UnresolvedColumnEdge]) -> List[UnresolvedColumnEdge]:
        """Order-stable de-duplication of markers (same column/reason/detail collapse to one)."""
        seen: Set[Tuple[str, str, Optional[str]]] = set()
        unique: List[UnresolvedColumnEdge] = []
        for marker in markers:
            key = (marker.column, marker.reason, marker.detail)
            if key not in seen:
                seen.add(key)
                unique.append(marker)
        return unique

    def _emit_star_rename_markers(
        self, star_expr: exp.Star, markers: List[UnresolvedColumnEdge]
    ) -> None:
        """Declare each ``select * rename (old as new)`` output as an unresolved edge.

        The star expander reads only ``except`` (see StarExpressionHandler.get_excluded_columns),
        so a ``rename`` modifier's output columns never make it into the lineage — a silent gap.
        We can't correctly re-attribute them from the star alone, so we mark them ``star_rename``
        (the ``new`` output name, with ``old->new`` detail) rather than leave them unresolved and
        unflagged.
        """
        rename = star_expr.args.get("rename") if hasattr(star_expr, "args") else None
        if not rename:
            return
        for item in rename:
            new_name = getattr(item, "alias", None)
            old_expr = getattr(item, "this", None)
            if not new_name:
                continue
            old_name = str(getattr(old_expr, "name", old_expr) or "").strip().strip('"')
            detail = f"{old_name} -> {new_name}" if old_name else str(new_name)
            markers.append(
                UnresolvedColumnEdge(
                    column=str(new_name).lower(),
                    reason="star_rename",
                    detail=detail,
                )
            )

    def _declare_phantom_edges(
        self,
        columns: Dict[str, List[ColumnLineage]],
        flatten_aliases: Set[str],
        flatten_alias_sources: Dict[str, Set[str]],
        markers: List[UnresolvedColumnEdge],
    ) -> None:
        """Resolve or declare fabricated source tokens on every resolved column.

        Two constructs are detectable from the SQL alone (no registry needed):

        * ``phantom_alias`` — the token's qualifier is a ``lateral flatten`` / table-function
          pseudo-alias (``f.value``): ``f`` is no real upstream. When the flattened expression is
          traceable to real upstream columns (``flatten_alias_sources``), those columns REPLACE the
          phantom token — the flatten unnests them, so they are its genuine source (a real edge).
          Only when the flattened expression cannot be traced (e.g. ``flatten([1, 2, 3])``, or a
          non-flatten ``lateral (subquery)``) do we fall back to the ``phantom_alias`` marker,
          preserving the honesty of the un-resolvable tail rather than fabricating.
        * ``pivot_output`` — the token's column part is a quoted pivot literal (``"'name'"``):
          a Snowflake ``pivot`` synthesizes these column names; they exist in no upstream node.

        Fail-safe: only fabricated tokens are dropped/re-attributed; every genuinely-resolved token
        (qualified by a real relation, or unqualified) is kept, so a legitimate edge is never
        removed. If a token is dropped without a replacement, the model-level edge still survives
        via ``star_sources`` and the column carries an unresolved marker instead of a lie.
        """
        for out_col, lineages in columns.items():
            for lineage in lineages:
                kept: Set[str] = set()
                for token in lineage.source_columns:
                    reason = self._phantom_token_reason(token, flatten_aliases)
                    if reason is None:
                        kept.add(token)
                        continue
                    if reason == "phantom_alias":
                        resolved = self._resolve_flatten_token(token, flatten_alias_sources)
                        if resolved:
                            # The flattened expression traces to real upstream columns: attribute
                            # the downstream column to them instead of the phantom alias.
                            kept.update(resolved)
                            continue
                    markers.append(
                        UnresolvedColumnEdge(column=out_col, reason=reason, detail=token)
                    )
                lineage.source_columns = kept

    @staticmethod
    def _resolve_flatten_token(token: str, flatten_alias_sources: Dict[str, Set[str]]) -> Set[str]:
        """Return the real upstream columns a flatten-qualified token (``f.value``) derives from.

        ``f`` is looked up in the flatten-alias -> flattened-source map. An empty result means the
        flattened expression was not traceable (the caller then keeps the honest marker)."""
        table_part, _ = split_qualified_name(token)
        qualifier = table_part.strip().strip('"').lower() if table_part else ""
        return set(flatten_alias_sources.get(qualifier, set()))

    def _build_flatten_alias_sources(
        self,
        parsed: Any,
        cte_to_model: Optional[Dict[str, str]],
        cte_sources: Dict[str, Dict[str, str]],
        cte_transformation_types: Dict[str, Dict[str, str]],
        cte_sql_expressions: Dict[str, Dict[str, Optional[str]]],
        cte_base_tables: Dict[str, Set[str]],
        cte_extra_sources: Dict[str, Dict[str, Set[str]]],
    ) -> Dict[str, Set[str]]:
        """Map each flatten pseudo-alias to the real upstream columns of the expression it unnests.

        For every ``flatten(<expr>) alias`` in the query, resolve ``<expr>``'s columns through the
        SAME CTE/alias machinery used for projections, evaluated in the flatten's own SELECT scope
        (so an unqualified ``flatten(order_ids)`` binds to that SELECT's ``FROM`` table). The result
        is what a downstream ``alias.value`` truly derives from.

        Nested flatten (``flatten(outer.value) inner`` where ``outer`` is itself a flatten alias)
        is expanded transitively: a raw source that is qualified by another flatten alias is
        replaced by that alias's resolved sources, with a visited-set guard against cycles. An
        alias whose expression traces to nothing real (a literal array, an untraceable path) maps
        to the empty set, which keeps the honest ``phantom_alias`` marker downstream.
        """
        raw_sources: Dict[str, Set[str]] = {}
        for alias, flattened_expr, select in get_flatten_alias_nodes(parsed):
            if select is None:
                raw_sources.setdefault(alias, set())
                continue
            # A flatten frequently unnests a column the SAME select derives on the fly
            # (``flatten(parse_json(raw):items)`` projected as ``arr``, then ``flatten(arr)``).
            # Populate column_definitions so forward-reference resolution follows such a derived
            # column to its REAL upstream source instead of fabricating a same-name column on the
            # base relation.
            column_definitions: Dict[str, Any] = {}
            for projected in select.expressions:
                projected_name = strip_sql_comments(projected.alias_or_name).lower()
                column_definitions[projected_name] = projected
            context = ParserContext(
                aliases=get_table_aliases(select),
                table_context=get_table_context(select),
                cte_sources=cte_sources,
                cte_to_model=cte_to_model,
                cte_transformation_types=cte_transformation_types,
                cte_sql_expressions=cte_sql_expressions,
                cte_base_tables=cte_base_tables,
                cte_extra_sources=cte_extra_sources,
                column_definitions=column_definitions,
            )
            resolved = self._extract_source_columns(flattened_expr, context)
            # Same alias reused in two scopes: union rather than clobber (over-inclusive is
            # honest here — both are genuine flattened sources; dropping one would hide an edge).
            raw_sources.setdefault(alias, set()).update(resolved)

        expanded: Dict[str, Set[str]] = {}
        for alias in raw_sources:
            expanded[alias] = self._expand_flatten_sources(alias, raw_sources, set())
        return expanded

    def _expand_flatten_sources(
        self,
        alias: str,
        raw_sources: Dict[str, Set[str]],
        visiting: Set[str],
    ) -> Set[str]:
        """Transitively resolve a flatten alias's sources, replacing nested-flatten qualifiers."""
        if alias in visiting:
            return set()
        visiting.add(alias)
        out: Set[str] = set()
        for token in raw_sources.get(alias, set()):
            table_part, _ = split_qualified_name(token)
            qualifier = table_part.strip().strip('"').lower() if table_part else ""
            if qualifier and qualifier in raw_sources and qualifier != alias:
                out.update(self._expand_flatten_sources(qualifier, raw_sources, visiting))
            else:
                out.add(token)
        visiting.discard(alias)
        return out

    @staticmethod
    def _phantom_token_reason(
        token: str, flatten_aliases: Set[str]
    ) -> Optional[Literal["pivot_output", "phantom_alias"]]:
        """Classify a source token as a fabricated edge, or ``None`` if it is genuine.

        Returns ``"pivot_output"`` for a quoted pivot literal, ``"phantom_alias"`` for a
        flatten/table-function-qualified token, else ``None`` (keep the token).
        """
        table_part, col = split_qualified_name(token)
        qualifier = table_part.strip().strip('"').lower() if table_part else ""
        col_clean = (col or "").strip().strip('"')
        # A pivot output column is projected as a quoted string literal (e.g. "'name'").
        if len(col_clean) >= 2 and col_clean.startswith("'") and col_clean.endswith("'"):
            return "pivot_output"
        if qualifier and qualifier in flatten_aliases:
            return "phantom_alias"
        return None

    def _extract_predicate_lineage(
        self,
        parsed: Any,
        cte_to_model: Optional[Dict[str, str]],
        cte_sources: Dict[str, Dict[str, str]],
        cte_transformation_types: Dict[str, Dict[str, str]],
        cte_sql_expressions: Dict[str, Dict[str, Optional[str]]],
        cte_base_tables: Dict[str, Set[str]],
    ) -> Dict[str, str]:
        """Resolve upstream columns referenced only in predicate clauses, with the condition.

        Column-value lineage is built from the projected ``SELECT`` list, so a column a
        model *filters or joins on* but never projects (e.g. ``where status = 'flagged'``
        driving a ``count(*)``) is invisible to it — yet changing that column's logic
        changes this model's row-set, and therefore its output. We walk the ``WHERE`` /
        ``JOIN ON`` / ``HAVING`` / ``QUALIFY`` conditions of every (sub)select and resolve
        each column reference through the same CTE/alias machinery used for projections, so
        a predicate on a CTE that wraps an upstream model resolves to that model's column.
        The returned map is ``upstream_column -> predicate condition text`` (the "why").
        """
        conditions_by_source: Dict[str, Set[str]] = {}

        for select in parsed.find_all(exp.Select):
            context = ParserContext(
                aliases=get_table_aliases(select),
                table_context=get_table_context(select),
                cte_sources=cte_sources,
                cte_to_model=cte_to_model,
                cte_transformation_types=cte_transformation_types,
                cte_sql_expressions=cte_sql_expressions,
                cte_base_tables=cte_base_tables,
                column_definitions={},
            )

            conditions: List[Any] = []
            for key in ("where", "having", "qualify"):
                wrapper = select.args.get(key)
                if wrapper is not None:
                    # WHERE/HAVING/QUALIFY wrap the boolean condition in `.this`.
                    conditions.append(getattr(wrapper, "this", wrapper))
            for join in select.args.get("joins", []) or []:
                on_condition = join.args.get("on")
                if on_condition is not None:
                    conditions.append(on_condition)

            for condition in conditions:
                try:
                    condition_text = strip_sql_comments(condition.sql(dialect=self.dialect))
                except Exception:
                    condition_text = ""
                for column_ref in condition.find_all(exp.Column):
                    if self._star_handler.is_star_expression(column_ref):
                        continue
                    try:
                        for lineage in self._expression_analyzer.analyze(column_ref, context):
                            for source in lineage.source_columns or set():
                                conditions_by_source.setdefault(source, set())
                                if condition_text:
                                    conditions_by_source[source].add(condition_text)
                    except Exception:
                        # A predicate we can't resolve is skipped rather than failing the
                        # whole parse — predicate lineage is best-effort.
                        continue

        return {
            source: " ; ".join(sorted(conditions))
            for source, conditions in conditions_by_source.items()
        }

    def _extract_cte_model_mappings(self, sql: str) -> Dict[str, str]:
        """Extract mappings from CTE names to model names (legacy method using regex)."""
        mappings = {}
        # Pattern to handle:
        # - SQLite: from main."stg_transactions"
        # - DuckDB: from "test"."main"."stg_transactions"
        # - Snowflake: from test.main.stg_transactions
        pattern = r'(\w+)\s+as\s*\(\s*select\b.*?\bfrom\s+(["\w\.]+(?:\."[^"]+"|[^"\s]+))\s*\)'
        matches = re.findall(pattern, sql, re.IGNORECASE | re.DOTALL)

        for cte_name, full_table_ref in matches:
            parts = re.findall(r'"([^"]+)"|([^"\s\.]+)', full_table_ref)
            model_name = next(name for pair in reversed(parts) for name in pair if name)
            mappings[cte_name] = model_name

        return mappings

    def _normalize_table_ref(self, column: str, aliases: Dict[str, str], table_context: str) -> str:
        column = strip_sql_comments(column)
        table_part, col = split_qualified_name(column)
        if not table_part:
            return f"{table_context}.{col}" if table_context else col
        table = aliases.get(table_part, table_part)
        return f"{table}.{col}"

    def _build_cte_sources(
        self,
        parsed: Any,
        cte_to_model: Optional[Dict[str, str]],
        cte_transformation_types: Dict[str, Dict[str, str]],
        cte_sql_expressions: Dict[str, Dict[str, Optional[str]]],
        cte_base_tables: Dict[str, Set[str]],
        cte_extra_sources: Dict[str, Dict[str, Set[str]]],
    ) -> Dict[str, Dict[str, str]]:
        cte_sources: Dict[str, Dict[str, str]] = {}

        for cte in parsed.find_all(exp.CTE):
            cte_name = cte.alias
            cte_sources[cte_name] = {}
            cte_transformation_types[cte_name] = {}
            cte_sql_expressions[cte_name] = {}
            cte_extra_sources.setdefault(cte_name, {})

            # A CTE body may be a UNION: process *every* branch SELECT so all branches'
            # sources are captured, not just the left-most one.
            for select in get_final_selects(cte.this):
                table_context = get_table_context(select)
                aliases = get_table_aliases(select)

                column_definitions = {}
                for expr in select.expressions:
                    col_name = expr.alias_or_name
                    # Strip SQL comments that might be included in the column name
                    col_name = strip_sql_comments(col_name)
                    column_definitions[col_name.lower()] = expr

                context = ParserContext(
                    aliases=aliases,
                    table_context=table_context,
                    cte_sources=cte_sources,
                    cte_to_model=cte_to_model,
                    cte_transformation_types=cte_transformation_types,
                    cte_sql_expressions=cte_sql_expressions,
                    cte_base_tables=cte_base_tables,
                    cte_extra_sources=cte_extra_sources,
                    column_definitions=column_definitions,
                )

                for expr in select.expressions:
                    col_name = expr.alias_or_name
                    # Strip SQL comments that might be included in the column name
                    col_name = strip_sql_comments(col_name)

                    if self._star_handler.is_star_expression(expr):
                        from_table = self._resolve_star_from_table_in_cte(
                            expr, select, context.aliases, context.table_context
                        )
                        excluded_columns = (
                            self._star_handler.get_excluded_columns(expr)
                            if isinstance(expr, exp.Star)
                            else []
                        )
                        excluded_col_names = {col.lower() for col in excluded_columns}

                        if from_table in cte_sources:
                            self._copy_cte_columns_with_exclusions(
                                from_table,
                                cte_name,
                                excluded_col_names,
                                context,
                            )
                        elif cte_to_model and from_table in cte_to_model:
                            base_table = cte_to_model[from_table]
                            cte_base_tables[cte_name].add(base_table)
                        else:
                            if from_table not in cte_sources:
                                cte_base_tables[cte_name].add(from_table)
                    else:
                        lineage_list = self._expression_analyzer.analyze(expr, context)
                        if lineage_list:
                            lineage = lineage_list[0]
                            if col_name not in cte_sources[cte_name]:
                                self._store_column_lineage_in_cte(
                                    cte_name,
                                    col_name,
                                    lineage,
                                    context,
                                )
                            else:
                                # Later UNION branch for a column already seen: keep its
                                # sources as extras so no branch is dropped.
                                extras = cte_extra_sources[cte_name].setdefault(col_name, set())
                                extras.update(lineage.source_columns or set())

        return cte_sources

    def _resolve_star_from_table_in_cte(
        self,
        expr: Any,
        select: Any,
        aliases: Dict[str, str],
        table_context: str,
    ) -> str:
        if isinstance(expr, exp.Column) and expr.table:
            star_table_alias = str(expr.table)
            from_table = aliases.get(star_table_alias, star_table_alias)
            if from_table == star_table_alias:
                for join in select.find_all(exp.Join):
                    if join.alias and join.alias == star_table_alias:
                        if hasattr(join, "this"):
                            join_table = join.this
                            if isinstance(join_table, exp.Table):
                                from_table = str(join_table.name).lower()
                            else:
                                from_table = str(join_table).lower()
                        break
                from_clause = select.find(exp.From)
                if from_clause:
                    table_expr = from_clause.find(exp.Table)
                    if table_expr and table_expr.alias == star_table_alias:
                        from_table = (
                            str(table_expr.name).lower()
                            if hasattr(table_expr, "name")
                            else from_table
                        )
            return from_table.lower() if from_table else table_context
        else:
            return table_context

    def _copy_cte_columns_with_exclusions(
        self,
        from_table: str,
        cte_name: str,
        excluded_col_names: Set[str],
        context: ParserContext,
    ) -> None:
        if from_table in context.cte_sources:
            for src_col_name, src_col_source in sorted(context.cte_sources[from_table].items()):
                if src_col_name.lower() not in excluded_col_names:
                    context.cte_sources[cte_name][src_col_name] = src_col_source
                    if from_table in context.cte_transformation_types:
                        context.cte_transformation_types[cte_name][src_col_name] = (
                            context.cte_transformation_types[from_table].get(src_col_name, "direct")
                        )
                    else:
                        context.cte_transformation_types[cte_name][src_col_name] = "direct"
                    if from_table in context.cte_sql_expressions:
                        context.cte_sql_expressions[cte_name][src_col_name] = (
                            context.cte_sql_expressions[from_table].get(src_col_name)
                        )
                    else:
                        context.cte_sql_expressions[cte_name][src_col_name] = None
            if from_table in context.cte_base_tables:
                context.cte_base_tables[cte_name].update(context.cte_base_tables[from_table])

    def _store_column_lineage_in_cte(
        self,
        cte_name: str,
        col_name: str,
        lineage: ColumnLineage,
        context: ParserContext,
    ) -> None:
        if lineage.source_columns:
            sorted_sources = sorted(lineage.source_columns)
            context.cte_sources[cte_name][col_name] = sorted_sources[0]
        else:
            if context.table_context:
                context.cte_sources[cte_name][col_name] = f"{context.table_context}.*"
            else:
                context.cte_sources[cte_name][col_name] = col_name
        context.cte_transformation_types[cte_name][col_name] = lineage.transformation_type
        context.cte_sql_expressions[cte_name][col_name] = lineage.sql_expression

    def _resolve_column_source(
        self,
        column: str,
        table: str,
        cte_sources: Dict[str, Dict[str, str]],
        cte_to_model: Optional[Dict[str, str]] = None,
    ) -> str:
        column = strip_sql_comments(column)
        table_part, col_name = split_qualified_name(column)
        if table_part:
            table = table_part

        col_name_lower = col_name.lower() if col_name else col_name

        if table in cte_sources:
            if col_name in cte_sources[table]:
                return cte_sources[table][col_name]
            if col_name_lower in cte_sources[table]:
                return cte_sources[table][col_name_lower]
            for key in sorted(cte_sources[table].keys()):
                if key.lower() == col_name_lower:
                    return cte_sources[table][key]

        if table and cte_to_model and table in cte_to_model:
            base_table = self._resolve_base_table(table, cte_to_model)
            return f"{base_table}.{col_name_lower}"
        elif table:
            return f"{table}.{col_name_lower}"
        return column

    def _resolve_base_table(self, table: str, cte_to_model: Dict[str, str]) -> str:
        """Follow cte_to_model transitively until reaching a table that is not a CTE.

        A single cte_to_model lookup can land on another CTE alias (e.g. a chain of
        star-passthrough CTEs), which would otherwise leak an internal CTE name into the
        lineage as if it were an upstream model. Walk to the ultimate base table, mirroring
        CTEHandler.trace_base_tables. A visited set and the self-reference guards prevent
        infinite loops on recursive/self-referential mappings.
        """
        current = table
        visited: Set[str] = set()
        while current in cte_to_model and current not in visited:
            visited.add(current)
            next_table = cte_to_model[current]
            if next_table == current or next_table == current.split(".")[-1]:
                break
            current = next_table
        return current

    def _handle_forward_reference(
        self,
        expr: exp.Column,
        col_name: str,
        context: ParserContext,
    ) -> Optional[List[ColumnLineage]]:
        is_qualified = bool(expr.table)
        if (
            not is_qualified
            and context.column_definitions
            and col_name in context.column_definitions
        ):
            forward_expr = context.column_definitions[col_name]
            if forward_expr != expr:
                forward_sources = self._extract_source_columns(
                    forward_expr,
                    context,
                    visited_forward_refs={col_name},
                )
                return [
                    ColumnLineage(
                        source_columns=forward_sources,
                        transformation_type="derived",
                        sql_expression=str(expr),
                    )
                ]
        return None

    def _analyze_column_reference(
        self,
        expr: exp.Column,
        col_name: str,
        context: ParserContext,
        is_aliased: bool,
    ) -> List[ColumnLineage]:
        source_col = self._normalize_table_ref(
            strip_sql_comments(str(expr)), context.aliases, context.table_context
        )
        table_part, col = split_qualified_name(source_col)
        table = table_part if table_part else context.table_context
        resolved_source = self._resolve_column_source(
            source_col, table, context.cte_sources, context.cte_to_model
        )

        trans_type = "direct"
        sql_expr = None
        if table in context.cte_sources and col_name in context.cte_sources[table]:
            trans_type = context.cte_transformation_types.get(table, {}).get(col_name, "direct")
            sql_expr = context.cte_sql_expressions.get(table, {}).get(col_name)
        elif is_aliased:
            trans_type = "renamed"

        resolved_table, resolved_col = split_qualified_name(resolved_source)
        if resolved_table:
            resolved_source = f"{resolved_table}.{resolved_col.lower()}"
        elif resolved_col:
            resolved_source = resolved_col.lower()

        source_columns = {resolved_source}
        # Merge in sources contributed by non-left UNION branches of the referenced CTE,
        # so a reference to a union CTE column carries every branch's source.
        source_columns.update(
            self._normalize_extra_cte_sources(context.cte_extra_sources.get(table, {}), col_name)
        )

        return [
            ColumnLineage(
                source_columns=source_columns,
                transformation_type=cast(Literal["direct", "renamed", "derived"], trans_type),
                sql_expression=sql_expr,
            )
        ]

    def _normalize_extra_cte_sources(
        self, extras_for_table: Dict[str, Set[str]], col_name: str
    ) -> Set[str]:
        normalized: Set[str] = set()
        for extra in extras_for_table.get(col_name, set()):
            extra_table, extra_col = split_qualified_name(extra)
            if extra_table:
                normalized.add(f"{extra_table}.{extra_col.lower()}")
            elif extra_col:
                normalized.add(extra_col.lower())
        return normalized

    def _normalize_source_columns(self, source_cols: Set[str]) -> Set[str]:
        """Normalize source columns, ensuring all are cleaned of comments and lowercase."""
        normalized = set()
        for s in source_cols:
            s = strip_sql_comments(s)
            table_part, col_part = split_qualified_name(s)
            if table_part:
                normalized.add(f"{table_part}.{col_part.lower()}")
            else:
                normalized.add(col_part.lower() if col_part else s)
        return normalized

    def _handle_forward_reference_in_extraction(
        self,
        col: exp.Column,
        col_name: str,
        context: ParserContext,
        visited_forward_refs: Set[str],
    ) -> Optional[Set[str]]:
        is_qualified = bool(col.table)
        if (
            not is_qualified
            and context.column_definitions
            and col_name in context.column_definitions
            and col_name not in visited_forward_refs
        ):
            forward_expr = context.column_definitions[col_name]
            if forward_expr != col:
                visited_forward_refs.add(col_name)
                forward_cols = self._extract_source_columns(
                    forward_expr,
                    context,
                    visited_forward_refs,
                )
                visited_forward_refs.remove(col_name)
                return forward_cols
        return None

    def _extract_source_columns(
        self,
        expr: Any,
        context: ParserContext,
        visited_forward_refs: Optional[Set[str]] = None,
    ) -> Set[str]:
        if visited_forward_refs is None:
            visited_forward_refs = set()

        columns = set()
        all_columns = list(expr.find_all(exp.Column))
        all_columns.sort(key=lambda c: str(c).lower())
        for col in all_columns:
            col_name = (
                str(col.this).lower() if hasattr(col, "this") and col.this else str(col).lower()
            )
            # Strip SQL comments that might be included in the column name
            col_name = strip_sql_comments(col_name)

            forward_cols = self._handle_forward_reference_in_extraction(
                col,
                col_name,
                context,
                visited_forward_refs,
            )
            if forward_cols is not None:
                columns.update(forward_cols)
                continue

            source_col_raw = strip_sql_comments(str(col))
            source_col = self._normalize_table_ref(
                source_col_raw, context.aliases, context.table_context
            )
            table_part, _ = split_qualified_name(source_col)
            table = table_part if table_part else context.table_context
            resolved = self._resolve_column_source(
                source_col, table, context.cte_sources, context.cte_to_model
            )
            columns.add(resolved)
            columns.update(
                self._normalize_extra_cte_sources(
                    context.cte_extra_sources.get(table, {}), col_name
                )
            )
        return columns
