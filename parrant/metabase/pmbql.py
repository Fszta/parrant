"""— pMBQL (MBQL 5) → legacy MBQL normalizer.

Metabase v0.57+ returns card ``dataset_query`` bodies in **pMBQL** shape
(``{"lib/type": "mbql/query", "stages": [...]}``) from the bulk ``/api/card`` list.
The existing :class:`~parrant.metabase.resolvers.CardResolver` only understands the
**legacy** MBQL shape (``{"type": "native"|"query", ...}``). Rather than teach the resolver
a second dialect, :func:`normalize_dataset_query` rewrites a pMBQL ``dataset_query`` into the
legacy shape so *all* downstream logic (and its recursion into upstream cards) is unchanged.

The transform is a pure, structural rewrite:

* **envelope**  — ``{lib/type, database, stages}`` → legacy ``{type, database, native|query}``.
* **field refs** — pMBQL ``["field", <opts>, <id|name>]`` → legacy ``["field", <id|name>, <opts>]``
  (same for ``expression`` / ``aggregation`` refs); ``lib/uuid`` / ``effective-type`` noise
  stripped from ``<opts>``.
* **operator clauses** — pMBQL ``["op", <opts>, *args]`` → legacy ``["op", *args]`` (opts dropped;
  the legacy resolver only harvests field refs, never operator opts).
* **stage keys** — ``filters`` (plural) → ``filter`` (singular, ``["and", ...]`` if many);
  ``source-card`` → ``source-table: "card__<id>"``; joins folded to the legacy join shape.
* **multi-stage** — ``stages`` is innermost-first; folded into nested ``source-query``.
* **native template-tags** — a pMBQL list of tag objects → legacy dict keyed by tag ``name``.

:func:`normalize_dataset_query` is a **no-op on legacy input** (``type`` in ``{native, query}``)
and idempotent, so it is always safe to run at resolver entry.
"""

from __future__ import annotations

from typing import Any

# Ref heads whose ``[head, opts, target]`` pMBQL form reorders to legacy ``[head, target, opts]``.
_REF_HEADS = {"field", "expression", "aggregation"}
# Opts keys that are pMBQL-only noise the legacy shape never carries.
_OPTS_NOISE = ("lib/uuid", "effective-type")


def is_pmbql(query: Any) -> bool:
    """True iff ``query`` is a pMBQL ``dataset_query`` (and not already legacy)."""
    if not isinstance(query, dict):
        return False
    if query.get("type") in {"native", "query"}:
        return False
    return query.get("lib/type") == "mbql/query" or "stages" in query


def normalize_dataset_query(query: dict) -> dict:
    """Return ``query`` in legacy MBQL shape, converting from pMBQL if needed.

    A legacy query (``type`` in ``{"native", "query"}``) or any non-pMBQL dict is returned
    untouched — the function is a no-op for legacy input and idempotent.
    """
    if not is_pmbql(query):
        return query

    database = query.get("database")
    stages = query.get("stages") or []
    last = stages[-1] if stages else {}

    if isinstance(last, dict) and last.get("lib/type") == "mbql.stage/native":
        return {
            "type": "native",
            "database": database,
            "native": {
                "query": last.get("native") or "",
                "template-tags": _normalize_template_tags(last.get("template-tags")),
            },
        }

    return {"type": "query", "database": database, "query": _fold_stages(stages)}


def _fold_stages(stages: list[dict]) -> dict:
    """Fold innermost-first ``stages`` into legacy nested ``source-query`` form."""
    folded: dict | None = None
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        legacy = _normalize_stage(stage)
        if folded is not None:
            legacy["source-query"] = folded
        folded = legacy
    return folded or {}


def _normalize_stage(stage: dict) -> dict:
    """Convert one ``mbql.stage/mbql`` stage into a legacy query dict."""
    out: dict[str, Any] = {}
    _set_source(out, stage.get("source-table"), stage.get("source-card"))

    for key in ("breakout", "aggregation", "fields", "order-by", "expressions"):
        value = stage.get(key)
        if value is not None:
            out[key] = _normalize_node(value)

    filters = stage.get("filters")
    if filters:
        normalized = [_normalize_node(f) for f in filters]
        out["filter"] = normalized[0] if len(normalized) == 1 else ["and", *normalized]

    joins = stage.get("joins")
    if joins:
        out["joins"] = [_normalize_join(j) for j in joins if isinstance(j, dict)]

    return out


def _normalize_join(join: dict) -> dict:
    """Convert a pMBQL join to the legacy ``{source-table, condition, alias}`` shape."""
    out: dict[str, Any] = {}
    alias = join.get("alias") or join.get("join-alias")
    if alias:
        out["alias"] = alias

    # The joined source lives under the join's own (base) stage.
    for jstage in join.get("stages") or []:
        if isinstance(jstage, dict) and (
            jstage.get("source-table") is not None or jstage.get("source-card") is not None
        ):
            _set_source(out, jstage.get("source-table"), jstage.get("source-card"))
            break

    conditions = join.get("conditions")
    if conditions:
        normalized = [_normalize_node(c) for c in conditions]
        out["condition"] = normalized[0] if len(normalized) == 1 else ["and", *normalized]

    fields = join.get("fields")
    if fields is not None:
        out["fields"] = _normalize_node(fields)

    return out


def _set_source(out: dict[str, Any], source_table: Any, source_card: Any) -> None:
    """Write the legacy ``source-table`` key (``card__<id>`` for an upstream card)."""
    if isinstance(source_card, int):
        out["source-table"] = f"card__{source_card}"
    elif source_table is not None:
        out["source-table"] = source_table


def _normalize_node(node: Any) -> Any:
    """Recursively rewrite pMBQL clauses/refs to legacy shape.

    ``[head, opts, *rest]`` (``head`` a str, ``opts`` a dict) is a pMBQL clause: ref heads
    reorder to ``[head, target, opts]``; every other operator drops ``opts`` to ``[head, *rest]``.
    Lists of clauses and plain values recurse structurally.
    """
    if isinstance(node, list):
        if node and isinstance(node[0], str):
            head = node[0]
            if len(node) >= 2 and isinstance(node[1], dict):
                opts = _clean_opts(node[1])
                rest = [_normalize_node(item) for item in node[2:]]
                if head in _REF_HEADS:
                    if rest:
                        return [head, rest[0], opts, *rest[1:]]
                    return [head, opts]
                return [head, *rest]
            return [head, *[_normalize_node(item) for item in node[1:]]]
        return [_normalize_node(item) for item in node]
    if isinstance(node, dict):
        return {key: _normalize_node(value) for key, value in node.items()}
    return node


def _clean_opts(opts: dict) -> dict:
    """Strip pMBQL-only noise keys, preserving semantic opts (temporal-unit, join-alias, ...)."""
    return {key: value for key, value in opts.items() if key not in _OPTS_NOISE}


def _normalize_template_tags(tags: Any) -> dict[str, Any]:
    """Convert a pMBQL template-tags **list** into the legacy **dict** keyed by tag name.

    A dimension tag's ``dimension`` value is a pMBQL field ref → reorder it per the ref rule.
    An already-legacy dict is returned untouched (idempotent).
    """
    if isinstance(tags, dict):
        return tags
    if not isinstance(tags, list):
        return {}
    out: dict[str, Any] = {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        if not name:
            continue
        new_tag = dict(tag)
        if "dimension" in new_tag:
            new_tag["dimension"] = _normalize_node(new_tag["dimension"])
        out[str(name)] = new_tag
    return out
