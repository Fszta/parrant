import re
from typing import Any

from sqlglot import exp


def strip_sql_comments(text: str) -> str:
    """Remove SQL comments from a string.

    Removes both /* ... */ and -- style comments.
    Normalizes whitespace (multiple spaces become single space).
    """
    if not text:
        return text

    # Remove /* ... */ style comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

    # Remove -- style comments (everything after -- until end of line)
    text = re.sub(r"--.*?$", "", text, flags=re.MULTILINE)

    # Normalize whitespace (multiple spaces/tabs/newlines become single space)
    text = re.sub(r"\s+", " ", text)

    # Clean up any extra whitespace that might be left
    return text.strip()


def get_table_aliases(parsed: Any) -> dict[str, str]:
    aliases = {}
    for table in parsed.find_all((exp.Table, exp.From, exp.Join)):
        if table.alias:
            aliases[table.alias] = str(table.name).lower()
    return aliases


def get_lateral_flatten_aliases(parsed: Any) -> set:
    """Return the aliases of every ``lateral flatten`` / table-function in the query.

    A Snowflake ``lateral flatten(x) p`` (and any table-valued function used the same way)
    parses to an ``exp.Lateral`` carrying an alias (``p``) but is **not** a real table — it is
    absent from :func:`get_table_aliases` (which only sees ``exp.Table``/``From``/``Join``). The
    parser therefore cannot resolve ``p.value`` to any upstream node and would otherwise mint the
    phantom token ``p.value``. These aliases are the ground truth for "this qualifier is a
    flatten pseudo-relation, not an upstream" — a column source qualified by one is a
    ``phantom_alias`` unresolved edge.

    Aliases are lowercased to match the parser's case-insensitive qualifier handling.
    """
    aliases: set = set()
    for lateral in parsed.find_all(exp.Lateral):
        alias = lateral.alias
        if alias:
            aliases.add(str(alias).lower())
    return aliases


def _enclosing_select(node: Any) -> Any | None:
    """Walk up the parent chain to the SELECT that owns this node (``None`` if unattached)."""
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


def _flatten_input_expression(explode: Any) -> Any | None:
    """Return the expression being unnested by a ``flatten`` (an ``exp.Explode``).

    The flattened value is either passed positionally (``flatten(x)`` -> ``explode.this == x``)
    or by keyword (``flatten(input => x, outer => true)`` -> ``explode.this`` is the ``input``
    ``Kwarg``). Snowflake's ``flatten`` names the flattened value ``input``; any other keyword
    (``path``, ``outer``, ``recursive``, ``mode``) is a modifier, not the source, so we select
    the ``input`` Kwarg specifically and fall back to the first Kwarg's value.
    """
    inner = getattr(explode, "this", None)
    if inner is None:
        return None
    if isinstance(inner, exp.Kwarg):
        candidates = [inner]
        candidates.extend(explode.args.get("expressions") or [])
        for kwarg in candidates:
            if not isinstance(kwarg, exp.Kwarg):
                continue
            key = kwarg.this
            key_name = str(getattr(key, "this", key)).lower()
            if key_name == "input":
                return kwarg.expression
        return inner.expression
    return inner


def get_flatten_alias_nodes(parsed: Any) -> list[tuple]:
    """Return ``(alias, flattened_expression, enclosing_select)`` for each ``flatten`` in the query.

    Covers the two shapes Snowflake ``flatten`` parses into: ``lateral flatten(...) a`` — an
    ``exp.Lateral`` wrapping an ``exp.Explode`` — and ``table(flatten(...)) a`` — an
    ``exp.TableFromRows`` wrapping an ``exp.Explode``. Each carries the flatten pseudo-alias
    (``a``) and the expression it unnests; the enclosing SELECT is returned so the flattened
    expression's columns can be resolved with that scope's table/alias context.

    Aliases are lowercased to match the parser's case-insensitive qualifier handling. A node with
    no alias, or whose inner is not an ``Explode`` (e.g. a ``lateral (subquery)``), is skipped —
    only genuine flatten table-functions are returned.
    """
    nodes: list[tuple] = []
    for holder in list(parsed.find_all(exp.Lateral)) + list(parsed.find_all(exp.TableFromRows)):
        alias = holder.alias
        if not alias:
            continue
        explode = holder.this
        if not isinstance(explode, exp.Explode):
            continue
        flattened_expr = _flatten_input_expression(explode)
        if flattened_expr is None:
            continue
        nodes.append((str(alias).lower(), flattened_expr, _enclosing_select(holder)))
    return nodes


def get_table_context(select: Any) -> str:
    from_clause = select.find(exp.From)
    if from_clause:
        table = from_clause.find(exp.Table)
        if table:
            return str(table.name).lower()

        subquery = from_clause.find(exp.Subquery)
        if subquery:
            subquery_select = subquery.find(exp.Select)
            if subquery_select:
                return get_table_context(subquery_select)
    return ""


def get_all_tables_from_select(select: Any) -> list[str]:
    tables = []
    from_clause = select.find(exp.From)
    if from_clause:
        table = from_clause.find(exp.Table)
        if table:
            tables.append(str(table.name).lower())

    for join in select.find_all(exp.Join):
        if hasattr(join, "this"):
            join_table = join.this
            if isinstance(join_table, exp.Table) or hasattr(join_table, "name"):
                tables.append(str(join_table.name).lower())

    return tables


def get_final_select(parsed: Any) -> Any | None:
    query = parsed
    while hasattr(query, "this") and query.this:
        query = query.this

    if isinstance(query, exp.Select):
        return query

    if isinstance(query, exp.Query):
        return query.this if isinstance(query.this, exp.Select) else None

    return None


def get_final_selects(parsed: Any) -> list[Any]:
    """Return every top-level branch SELECT to process.

    For a ``UNION`` / ``UNION ALL`` (including chained/nested unions), returns the
    SELECT of *every* branch so per-column lineage from all branches can be merged —
    otherwise only the left-most branch is traced and downstream blast radius is
    under-reported. For a plain query, returns the single final SELECT.
    """
    query = parsed
    # Unwrap outer wrappers (e.g. a Subquery/paren) until we reach a Select or Union,
    # so a union nested inside a wrapper is still flattened into its branches.
    while (
        hasattr(query, "this")
        and query.this is not None
        and not isinstance(query, (exp.Select, exp.Union))
    ):
        query = query.this

    if isinstance(query, exp.Union):
        selects: list[Any] = []
        for side in (query.this, query.expression):
            selects.extend(get_final_selects(side))
        return selects

    if isinstance(query, exp.Select):
        return [query]

    if isinstance(query, exp.Query):
        return [query.this] if isinstance(query.this, exp.Select) else []

    return []


def split_qualified_name(qualified_name: str) -> tuple[str, str]:
    """Split a qualified name into table and column parts, stripping SQL comments."""
    if "." not in qualified_name:
        return ("", strip_sql_comments(qualified_name))
    qualified_name = strip_sql_comments(qualified_name)
    parts = qualified_name.split(".")
    table_part = ".".join(parts[:-1])
    column_part = strip_sql_comments(parts[-1])
    return (table_part, column_part)
