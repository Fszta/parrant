"""Unit tests for diff-driven impact.

These exercise the pure pieces — changeset derivation, severity-aware
aggregation, report assembly and Markdown rendering — with lightweight stubs, so
they run without dbt artifacts on disk.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import pytest

from parrant.lineage.changeset import (
    ChangeKind,
    ChangesetBuilder,
    ColumnChange,
    build_changeset_report,
    build_git_changeset,
    scope_changes_to_models,
)
from parrant.models.schema import SemanticChangeKind
from parrant.lineage.display.markdown import render_changeset_markdown
from parrant.lineage.service import LineageService


# --- stubs -----------------------------------------------------------------


@dataclass
class _Col:
    data_type: Optional[str]


@dataclass
class _Model:
    columns: Dict[str, _Col]
    # Optional so existing stubs stay minimal; the missing-compiled-SQL indeterminate
    # pathway only applies to real model nodes, which always carry a resource_type.
    resource_type: Optional[str] = None


@dataclass
class _Lin:
    """Stand-in for ColumnLineage (the per-column derivation signature source)."""

    source_columns: Set[str]
    transformation_type: str
    sql_expression: str


@dataclass
class _LinCol:
    """A column that also carries parsed per-column lineage, enabling a precise diff."""

    data_type: Optional[str] = None
    lineage: List[_Lin] = field(default_factory=list)


class _FakeRegistry:
    """Minimal stand-in for ModelRegistry used by ChangesetBuilder."""

    def __init__(
        self,
        models: Dict[str, _Model],
        compiled: Optional[Dict[str, str]] = None,
        catalog_backed: Optional[set] = None,
    ):
        self._models = models
        self._compiled = compiled or {}
        # Default: every model is catalog-backed (structural column diffs are trusted).
        # Pass an explicit set to simulate catalog-missing (manifest-only) models.
        self._catalog_backed = catalog_backed if catalog_backed is not None else set(models)

    def get_models(self) -> Dict[str, _Model]:
        return self._models

    def is_catalog_backed(self, model_name: str) -> bool:
        return model_name in self._catalog_backed

    def get_compiled_sql(self, model_name: str) -> str:
        if model_name not in self._compiled:
            raise ValueError(f"no compiled sql for {model_name}")
        return self._compiled[model_name]


class _FakeService:
    """Stub exposing get_column_impact, used to drive get_changeset_impact."""

    def __init__(self, impacts: Dict[tuple, dict]):
        self._impacts = impacts

    def get_column_impact(self, model: str, column: str) -> dict:
        if (model, column) not in self._impacts:
            raise ValueError(f"no impact for {model}.{column}")
        return self._impacts[(model, column)]


def _impact(models, columns, exposures):
    return {
        "summary": {
            "affected_models": len(models),
            "affected_columns": len(columns),
            "affected_exposures": len(exposures),
            "critical_count": sum(1 for c in columns if c["severity"] == "critical"),
            "low_impact_count": sum(1 for c in columns if c["severity"] != "critical"),
        },
        "affected_models": models,
        "affected_columns": columns,
        "affected_exposures": exposures,
    }


# --- ChangeKind ------------------------------------------------------------


def test_change_kind_priority_ordering():
    order = [
        ChangeKind.ADDED,
        ChangeKind.LOGIC_CHANGED,
        ChangeKind.TYPE_CHANGED,
        ChangeKind.REMOVED,
    ]
    priorities = [k.priority for k in order]
    assert priorities == sorted(priorities), "priority must increase with severity"


# --- ChangesetBuilder ------------------------------------------------------


def test_builder_detects_added_removed_and_type_change():
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int"), "b": _Col("text")})},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("bigint"), "c": _Col("text")})},
    )
    changes = ChangesetBuilder(base, head).build()
    by_col = {(c.column): c.kind for c in changes}

    assert by_col["a"] == ChangeKind.TYPE_CHANGED
    assert by_col["b"] == ChangeKind.REMOVED
    assert by_col["c"] == ChangeKind.ADDED
    # type_changed carries a base -> head detail string
    a_change = next(c for c in changes if c.column == "a")
    assert a_change.detail == "int -> bigint"


def test_builder_new_and_removed_models():
    base = _FakeRegistry({"gone": _Model({"x": _Col("int")})})
    head = _FakeRegistry({"fresh": _Model({"y": _Col("int")})})
    changes = ChangesetBuilder(base, head).build()

    kinds = {(c.model, c.column): c.kind for c in changes}
    assert kinds[("gone", "x")] == ChangeKind.REMOVED
    assert kinds[("fresh", "y")] == ChangeKind.ADDED


def test_builder_logic_change_flags_all_columns_and_dedups_with_type_change():
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int"), "b": _Col("text")})},
        compiled={"m": "select 1 as a, 'x' as b"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("bigint"), "b": _Col("text")})},
        compiled={"m": "select 2 as a, 'y' as b"},
    )
    changes = ChangesetBuilder(base, head).build()
    kinds = {c.column: c.kind for c in changes}

    # 'b' only differs by logic; 'a' differs by both type and logic -> keep the
    # higher-severity type_changed.
    assert kinds["b"] == ChangeKind.LOGIC_CHANGED
    assert kinds["a"] == ChangeKind.TYPE_CHANGED


def test_builder_ignores_cosmetic_sql_changes():
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int")})},
        compiled={"m": "select 1 as a  -- old comment"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("int")})},
        compiled={"m": "select    1 as a\n-- new comment"},
    )
    assert ChangesetBuilder(base, head).build() == []


def test_builder_detects_change_next_to_dash_comment_inside_string_literal():
    # The logic gate must be string-literal-aware: a `--` INSIDE a quoted string is data,
    # not a comment. A naive regex comment-strip removes everything after it — including
    # the real `* 1` -> `* 100` change on the same line — and silently reports SAFE.
    base = _FakeRegistry(
        {"m": _Model({"env": _Col("text"), "amt": _Col("int")})},
        compiled={"m": "select 'ref -- prod' as env, amount * 1 as amt from t"},
    )
    head = _FakeRegistry(
        {"m": _Model({"env": _Col("text"), "amt": _Col("int")})},
        compiled={"m": "select 'ref -- prod' as env, amount * 100 as amt from t"},
    )
    changes = ChangesetBuilder(base, head).build()
    assert changes, "a real change hidden behind a '--' inside a string literal must be detected"
    assert {c.kind for c in changes} == {ChangeKind.LOGIC_CHANGED}


def test_builder_detects_change_between_block_comment_markers_inside_string_literals():
    # Same soundness hole with `/* */`: markers living INSIDE two string literals make a
    # naive regex strip swallow every real token between them (here `1 as n` -> `2 as n`).
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("text"), "n": _Col("int"), "b": _Col("text")})},
        compiled={"m": "select 'open /*' as a, 1 as n, '*/ close' as b from t"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("text"), "n": _Col("int"), "b": _Col("text")})},
        compiled={"m": "select 'open /*' as a, 2 as n, '*/ close' as b from t"},
    )
    changes = ChangesetBuilder(base, head).build()
    assert changes, "a real change between '/*' and '*/' string contents must be detected"
    assert {c.kind for c in changes} == {ChangeKind.LOGIC_CHANGED}


def test_builder_comment_only_edit_is_not_a_logic_change():
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int")})},
        compiled={"m": "select 1 as a -- old note\n/* block v1 */ from t"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("int")})},
        compiled={"m": "select 1 as a -- new note\n/* block v2 */ from t"},
    )
    assert ChangesetBuilder(base, head).build() == []


def test_builder_whitespace_only_edit_is_not_a_logic_change():
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int")})},
        compiled={"m": "select 1 as a from t"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("int")})},
        compiled={"m": "select   1 as a\n\tfrom t"},
    )
    assert ChangesetBuilder(base, head).build() == []


def test_builder_missing_head_sql_on_model_is_indeterminate_not_silent_safe():
    # A MODEL whose compiled SQL is unavailable on the head side cannot be proven
    # unchanged. Silently returning "no logic change" is a false SAFE: the change is
    # invisible AND the model would not even rebuild itself. It must surface as an
    # explicit indeterminate logic change on every head column.
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int"), "b": _Col("text")}, resource_type="model")},
        compiled={"m": "select 1 as a, 'x' as b"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("int"), "b": _Col("text")}, resource_type="model")},
        compiled={},  # head compiled SQL unavailable
    )
    changes = ChangesetBuilder(base, head).build()
    assert {c.column for c in changes} == {"a", "b"}
    for change in changes:
        assert change.kind == ChangeKind.LOGIC_CHANGED
        assert change.semantic == SemanticChangeKind.INDETERMINATE
        assert change.logic_diff_status == "indeterminate"
        assert change.reason


def test_builder_missing_base_sql_on_model_is_indeterminate_not_silent_safe():
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("int")}, resource_type="model")},
        compiled={},  # base compiled SQL unavailable
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("int")}, resource_type="model")},
        compiled={"m": "select 1 as a"},
    )
    changes = ChangesetBuilder(base, head).build()
    assert [c.logic_diff_status for c in changes] == ["indeterminate"]
    assert changes[0].to_dict()["logic_diff_status"] == "indeterminate"


def test_builder_missing_sql_on_non_model_nodes_stays_silent():
    # Sources / seeds / snapshots legitimately have no compiled logic to diff — missing
    # SQL there is NOT indeterminate.
    for resource_type in ("source", "seed", "snapshot"):
        base = _FakeRegistry({"n": _Model({"a": _Col("int")}, resource_type=resource_type)})
        head = _FakeRegistry({"n": _Model({"a": _Col("int")}, resource_type=resource_type)})
        assert ChangesetBuilder(base, head).build() == [], resource_type


def test_builder_logic_change_is_per_column_when_lineage_is_available():
    # base: a, b, c are all plain pass-throughs of an upstream column.
    base = _FakeRegistry(
        {
            "m": _Model(
                {
                    "a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")]),
                    "b": _LinCol("text", [_Lin({"up.b"}, "direct", "up.b")]),
                    "c": _LinCol("text", [_Lin({"up.c"}, "direct", "up.c")]),
                }
            )
        },
        compiled={"m": "select up.a as a, up.b as b, up.c as c from up"},
    )
    # head: only `a`'s derivation changed (now a coalesce); b and c are untouched.
    head = _FakeRegistry(
        {
            "m": _Model(
                {
                    "a": _LinCol(
                        "text", [_Lin({"up.a", "up.z"}, "derived", "coalesce(up.a, up.z)")]
                    ),
                    "b": _LinCol("text", [_Lin({"up.b"}, "direct", "up.b")]),
                    "c": _LinCol("text", [_Lin({"up.c"}, "direct", "up.c")]),
                }
            )
        },
        compiled={"m": "select coalesce(up.a, up.z) as a, up.b as b, up.c as c from up"},
    )
    changes = ChangesetBuilder(base, head).build()
    logic = {c.column for c in changes if c.kind == ChangeKind.LOGIC_CHANGED}
    # Precisely `a` — NOT the whole model. This is what stops an edit to one column from
    # flooding downstream with every unrelated pass-through.
    assert logic == {"a"}, logic


def test_builder_logic_change_flags_all_columns_when_lineage_missing():
    # No per-column lineage on the stub columns -> can't diff precisely, so fall back to
    # the conservative model-level behaviour (flag every output column).
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("text"), "b": _Col("text")})},
        compiled={"m": "select 1 as a, 2 as b"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("text"), "b": _Col("text")})},
        compiled={"m": "select 9 as a, 8 as b"},
    )
    changes = ChangesetBuilder(base, head).build()
    logic = {c.column for c in changes if c.kind == ChangeKind.LOGIC_CHANGED}
    assert logic == {"a", "b"}, logic


def test_builder_join_predicate_only_change_fails_safe_indeterminate():
    # Regression for #129: the compiled SQL changes ONLY in a JOIN `ON` predicate. Every
    # output column's per-column lineage is byte-identical on both sides (order_id/amount are
    # plain pass-throughs; country_code is still `direct` from stg_customers.country_code), so
    # `_logic_changed_columns` attributes the change to NO column and returns {}. Before the fix
    # this yielded total_changes: 0 — a false negative at full confidence on a model whose
    # compiled SQL provably changed (country_code now resolves to a different customer row for
    # every order whose customer_key is null). The backstop must flag every head output column
    # INDETERMINATE (fail-safe) instead of falling silent.
    base_cols = {
        "order_id": _LinCol("int", [_Lin({"stg_orders.order_id"}, "direct", "orders.order_id")]),
        "amount": _LinCol("numeric", [_Lin({"stg_orders.amount"}, "direct", "orders.amount")]),
        "country_code": _LinCol(
            "text", [_Lin({"stg_customers.country_code"}, "direct", "customers.country_code")]
        ),
    }
    # Head lineage is identical — the join key lives in the ON clause, never in the signature.
    head_cols = {name: _LinCol(col.data_type, list(col.lineage)) for name, col in base_cols.items()}
    select = (
        "select orders.order_id as order_id, orders.amount as amount, "
        "customers.country_code as country_code "
        "from stg_orders as orders left join stg_customers as customers"
    )
    base = _FakeRegistry(
        {"mart_orders": _Model(base_cols)},
        compiled={"mart_orders": f"{select} on orders.customer_key = customers.customer_key"},
    )
    head = _FakeRegistry(
        {"mart_orders": _Model(head_cols)},
        compiled={
            "mart_orders": f"{select} on coalesce(orders.customer_key, orders.fallback_key) "
            "= customers.customer_key"
        },
    )
    changes = ChangesetBuilder(base, head).build()

    # Before the fix this was empty; now the model's SQL change is reported, not dropped.
    assert changes, "join-predicate-only change must not be reported as total_changes: 0"
    logic = {c.column for c in changes if c.kind == ChangeKind.LOGIC_CHANGED}
    assert logic == {"order_id", "amount", "country_code"}, logic
    for change in changes:
        assert change.kind == ChangeKind.LOGIC_CHANGED
        assert change.semantic == SemanticChangeKind.INDETERMINATE
        assert change.reason is not None and "could not be attributed" in change.reason


def test_builder_statement_level_cosmetic_change_is_still_silent():
    # Counterpart to the #129 backstop: when the ONLY compiled-SQL difference is cosmetic at the
    # statement level (identifier case here), the statement-level AST compare proves EQUIVALENT,
    # so the backstop must NOT fire — no false positive. `_logic_changed` (a string compare)
    # fires on the case difference, but nothing real changed.
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "UP.A")])})},
        compiled={"m": "select UP.A as a from UP"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")])})},
        compiled={"m": "select up.a as a from up"},
    )
    assert ChangesetBuilder(base, head).build() == []


def test_structural_diff_available_requires_catalog_on_both_sides():
    base = _FakeRegistry({"m": _Model({"a": _Col("int")})})
    head = _FakeRegistry({"m": _Model({"a": _Col("int")})})
    # Both sides catalog-backed -> add/removed/type_changed detection is trustworthy.
    assert ChangesetBuilder(base, head).structural_diff_available() is True

    # A side with no catalog-backed model (e.g. catalog.json absent on that side) means
    # structural checks were skipped, regardless of which side is missing it.
    no_catalog = _FakeRegistry({"m": _Model({"a": _Col("int")})}, catalog_backed=set())
    assert ChangesetBuilder(base, no_catalog).structural_diff_available() is False
    assert ChangesetBuilder(no_catalog, head).structural_diff_available() is False


# --- AST semantic-diff integration (Package B) -----------------------------


def test_builder_suppresses_cosmetic_column_change():
    # `a`'s derivation differs only by whitespace — a cosmetic edit the OLD string compare
    # would flag, but the AST canonical form collapses to equal. The compiled SQL genuinely
    # differs (the spacing), so the model-level gate DOES fire; the per-column AST diff is what
    # suppresses `a`. This is the headline proof. (Whitespace — not redundant parens — because
    # canonicalization no longer runs the NULL-unsound ``simplify`` pass that folded parens.)
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a", "up.b"}, "derived", "up.a + up.b")])})},
        compiled={"m": "select up.a + up.b as a from up"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a", "up.b"}, "derived", "up.a  +  up.b")])})},
        compiled={"m": "select up.a  +  up.b as a from up"},
    )
    changes = ChangesetBuilder(base, head).build()
    logic = {c.column for c in changes if c.kind == ChangeKind.LOGIC_CHANGED}
    assert logic == set(), "cosmetic-only change must not be flagged (AST beats string compare)"


def test_builder_suppresses_identifier_case_only_change():
    # base `UP.A`, head `up.a` — unquoted identifier case folds to one form; not a change.
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "UP.A")])})},
        compiled={"m": "select UP.A as a from up"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")])})},
        compiled={"m": "select up.a as a from up"},
    )
    changes = ChangesetBuilder(base, head).build()
    logic = {c.column for c in changes if c.kind == ChangeKind.LOGIC_CHANGED}
    assert logic == set(), "identifier-case-only change must not be flagged"


def test_builder_flags_meaning_change_and_tags_it():
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")])})},
        compiled={"m": "select up.a as a from up"},
    )
    head = _FakeRegistry(
        {
            "m": _Model(
                {"a": _LinCol("text", [_Lin({"up.a", "up.z"}, "derived", "coalesce(up.a, up.z)")])}
            )
        },
        compiled={"m": "select coalesce(up.a, up.z) as a from up"},
    )
    changes = ChangesetBuilder(base, head).build()
    a_change = next(c for c in changes if c.column == "a")
    assert a_change.kind == ChangeKind.LOGIC_CHANGED
    assert a_change.semantic == SemanticChangeKind.MEANING_CHANGED


def test_builder_tags_unparseable_head_as_indeterminate():
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")])})},
        compiled={"m": "select up.a as a from up"},
    )
    # Head expression is junk (fails to parse) -> conservative-breaking, not a proven change.
    head = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "derived", "up.a +")])})},
        compiled={"m": "select up.a + as a from up"},
    )
    changes = ChangesetBuilder(base, head).build()
    a_change = next(c for c in changes if c.column == "a")
    assert a_change.kind == ChangeKind.LOGIC_CHANGED
    assert a_change.semantic == SemanticChangeKind.INDETERMINATE


def test_builder_no_lineage_fallback_tags_all_indeterminate():
    # No per-column lineage anywhere -> flag every head column, each INDETERMINATE
    # (matches the pre-existing "flag all" fallback, now carrying an honest tag).
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("text"), "b": _Col("text")})},
        compiled={"m": "select 1 as a, 2 as b"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("text"), "b": _Col("text")})},
        compiled={"m": "select 9 as a, 8 as b"},
    )
    changes = ChangesetBuilder(base, head).build()
    semantics = {c.column: c.semantic for c in changes if c.kind == ChangeKind.LOGIC_CHANGED}
    assert semantics == {
        "a": SemanticChangeKind.INDETERMINATE,
        "b": SemanticChangeKind.INDETERMINATE,
    }


def test_git_changeset_fallback_tags_indeterminate(monkeypatch):
    from parrant.lineage import changeset

    monkeypatch.setattr(
        changeset,
        "_git_changed_sql_files",
        lambda ref, repo_dir=None, git_head="HEAD": ["models/orders.sql"],
    )
    registry = _PathRegistry({"orders": _PathModel({"id": _Col("int")}, "models/orders.sql")})
    changes = build_git_changeset(registry, "origin/main")
    assert len(changes) == 1
    (change,) = changes
    assert change.kind == ChangeKind.LOGIC_CHANGED
    assert change.semantic == SemanticChangeKind.INDETERMINATE
    assert change.detail == "models/orders.sql"


def test_column_change_to_dict_carries_semantic_key():
    # Structural changes leave semantic=None; the key is present (superset) but None.
    structural = ColumnChange("m", "a", ChangeKind.TYPE_CHANGED, detail="int -> bigint")
    assert structural.to_dict() == {
        "model": "m",
        "column": "a",
        "kind": "type_changed",
        "detail": "int -> bigint",
        "semantic": None,
    }
    # A logic change carries the classification value.
    logic = ColumnChange(
        "m", "a", ChangeKind.LOGIC_CHANGED, semantic=SemanticChangeKind.MEANING_CHANGED
    )
    assert logic.to_dict()["semantic"] == "meaning_changed"


def test_to_dict_no_explain_key_on_structural_change():
    # Structural add/remove/type entries carry no reason, so no `explain` block is attached
    # (keeps them lean + JSON backward-compatible).
    structural = ColumnChange("m", "a", ChangeKind.TYPE_CHANGED, detail="int -> bigint")
    assert "explain" not in structural.to_dict()


def test_to_dict_explain_block_on_meaning_change():
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")])})},
        compiled={"m": "select up.a as a from up"},
    )
    head = _FakeRegistry(
        {
            "m": _Model(
                {"a": _LinCol("text", [_Lin({"up.a", "up.z"}, "derived", "coalesce(up.a, up.z)")])}
            )
        },
        compiled={"m": "select coalesce(up.a, up.z) as a from up"},
    )
    a_change = next(c for c in ChangesetBuilder(base, head).build() if c.column == "a")
    explain = a_change.to_dict()["explain"]
    assert isinstance(explain, dict)
    assert explain["reason"] == "expression meaning changed"
    assert explain["base"] == "up.a"
    assert explain["head"] == "coalesce(up.a, up.z)"


def test_to_dict_explain_block_on_indeterminate_unparseable():
    base = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "direct", "up.a")])})},
        compiled={"m": "select up.a as a from up"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _LinCol("text", [_Lin({"up.a"}, "derived", "up.a +")])})},
        compiled={"m": "select up.a + as a from up"},
    )
    a_change = next(c for c in ChangesetBuilder(base, head).build() if c.column == "a")
    explain = a_change.to_dict()["explain"]
    assert a_change.semantic == SemanticChangeKind.INDETERMINATE
    assert "did not parse" in explain["reason"]


def test_to_dict_explain_fail_safe_when_no_lineage():
    # No per-column lineage anywhere -> fail-safe INDETERMINATE with the "no per-column
    # lineage available to diff" reason.
    base = _FakeRegistry(
        {"m": _Model({"a": _Col("text")})},
        compiled={"m": "select 1 as a"},
    )
    head = _FakeRegistry(
        {"m": _Model({"a": _Col("text")})},
        compiled={"m": "select 9 as a"},
    )
    a_change = next(c for c in ChangesetBuilder(base, head).build() if c.column == "a")
    explain = a_change.to_dict()["explain"]
    assert "no per-column lineage available to diff" in explain["reason"]
    assert explain["base"] is None and explain["head"] is None


# --- get_changeset_impact (severity-aware aggregation) ---------------------


def test_get_changeset_impact_dedups_and_keeps_highest_severity():
    # Two changed columns both hit downstream node ('dm', 'dc'): once low, once
    # critical. The aggregated node must keep 'critical' and count it once.
    low = {
        "model": "dm",
        "column": "dc",
        "severity": "low_impact",
        "transformation_type": "direct",
        "sql_expression": "dm.dc",
    }
    crit = {
        "model": "dm",
        "column": "dc",
        "severity": "critical",
        "transformation_type": "derived",
        "sql_expression": "sum(dm.dc)",
    }
    other = {
        "model": "dm2",
        "column": "z",
        "severity": "low_impact",
        "transformation_type": "renamed",
        "sql_expression": "dm2.z",
    }

    exposure = {
        "name": "dash",
        "type": "dashboard",
        "url": None,
        "description": None,
        "depends_on_models": ["dm"],
    }

    impacts = {
        ("src", "col1"): _impact([{"name": "dm"}], [low, other], [exposure]),
        ("src", "col2"): _impact([{"name": "dm"}], [crit], [exposure]),
    }
    changes = [
        ColumnChange("src", "col1", ChangeKind.LOGIC_CHANGED),
        ColumnChange("src", "col2", ChangeKind.TYPE_CHANGED),
    ]

    result = LineageService.get_changeset_impact(_FakeService(impacts), changes)

    assert result["summary"]["affected_columns"] == 2  # (dm,dc) deduped + (dm2,z)
    assert result["summary"]["critical_count"] == 1
    assert result["summary"]["affected_exposures"] == 1  # deduped by name
    assert result["summary"]["unresolved_changes"] == 0
    dedup = {(c["model"], c["column"]): c["severity"] for c in result["affected_columns"]}
    assert dedup[("dm", "dc")] == "critical"


def test_get_changeset_impact_reports_unresolved():
    changes = [ColumnChange("ghost", "col", ChangeKind.REMOVED)]
    result = LineageService.get_changeset_impact(_FakeService({}), changes)

    assert result["summary"]["unresolved_changes"] == 1
    assert result["by_change"][0]["resolved"] is False


def test_removed_column_resolved_against_base_service():
    base_impact = _impact(
        [{"name": "dm"}],
        [
            {
                "model": "dm",
                "column": "dc",
                "severity": "low_impact",
                "transformation_type": "direct",
                "sql_expression": "dm.dc",
            }
        ],
        [],
    )
    head = _FakeService({})  # column is gone from head -> would be unresolved
    base = _FakeService({("src", "removed_col"): base_impact})

    changes = [ColumnChange("src", "removed_col", ChangeKind.REMOVED)]
    result = LineageService.get_changeset_impact(head, changes, base_service=base)

    assert result["summary"]["unresolved_changes"] == 0
    assert result["summary"]["affected_columns"] == 1


# --- report + markdown -----------------------------------------------------


def test_build_changeset_report_shape():
    changes = [
        ColumnChange("m", "a", ChangeKind.TYPE_CHANGED),
        ColumnChange("m", "b", ChangeKind.ADDED),
    ]
    aggregated = _impact([], [], [])
    aggregated["by_change"] = []
    report = build_changeset_report("two-manifest", changes, aggregated)

    assert report["changeset"]["source"] == "two-manifest"
    assert report["changeset"]["total_changes"] == 2
    assert report["changeset"]["by_kind"] == {"type_changed": 1, "added": 1}
    # aggregated impact keys are merged in at top level
    assert "summary" in report and "affected_columns" in report


def test_markdown_empty_changeset():
    report = build_changeset_report("two-manifest", [], {**_impact([], [], []), "by_change": []})
    md = render_changeset_markdown(report)
    assert "No column changes" in md
    # attribution footer present even on the empty report
    assert "github.com/Fszta/parrant" in md


def test_markdown_explain_renders_reason_and_expressions():
    aggregated = _impact([], [], [])
    aggregated["by_change"] = [
        {
            "model": "s",
            "column": "c",
            "kind": "logic_changed",
            "detail": None,
            "semantic": "meaning_changed",
            "explain": {
                "reason": "expression meaning changed",
                "base": "up.a",
                "head": "coalesce(up.a, up.z)",
            },
            "resolved": True,
        }
    ]
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    # explain=False -> the compact semantic reason shows by default (the default gate explains
    # itself) but NOT the base→head expression trace.
    default_md = render_changeset_markdown(report)
    assert "expression meaning changed" in default_md
    assert "coalesce(up.a, up.z)" not in default_md
    # explain=True -> reason and the base→head expression line both appear.
    md = render_changeset_markdown(report, explain=True)
    assert "expression meaning changed" in md
    assert "up.a" in md and "coalesce(up.a, up.z)" in md
    assert "→" in md


def test_markdown_appends_attribution_footer():
    columns = [
        {
            "model": "dm",
            "column": "dc",
            "severity": "critical",
            "transformation_type": "derived",
            "sql_expression": "sum(x)",
        }
    ]
    aggregated = _impact([{"name": "dm"}], columns, [])
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    md = render_changeset_markdown(report)
    # one subtle, linked credit line at the end of the comment body
    assert "— lineage by [parrant](https://github.com/Fszta/parrant)" in md
    assert md.count("github.com/Fszta/parrant") == 1


def test_markdown_lists_exposures_first_and_blast_table():
    columns = [
        {
            "model": "dm",
            "column": "dc",
            "severity": "critical",
            "transformation_type": "derived",
            "sql_expression": "sum(x)",
        }
    ]
    exposures = [
        {
            "name": "Finance Dashboard",
            "type": "dashboard",
            "url": "https://x",
            "description": None,
            "depends_on_models": ["dm"],
        }
    ]
    aggregated = _impact([{"name": "dm"}], columns, exposures)
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    md = render_changeset_markdown(report)

    # verdict banner leads, fusing severity + blast radius
    assert "Review required" in md
    assert md.index("Review required") < md.index("Column-level impact") + 200
    # business-facing exposures section present
    assert "Business-facing exposures" in md
    assert "Finance Dashboard" in md
    # criticality-first: the output-changes section comes before exposures.
    assert "Check these — their output changes" in md
    assert md.index("Check these — their output changes") < md.index("Business-facing exposures")
    # the review section is a scannable table with plain-language tags
    assert "| Model | What changes | How |" in md
    assert "`dm`" in md and "`dc`" in md and "value recomputed" in md
    # the derived expression is reachable behind a per-model fold, not cluttering the table
    assert "Show new logic" in md and "sum(x)" in md


def test_markdown_renders_partial_confidence_and_coverage_warning():
    aggregated = _impact([{"name": "dm"}], [], [])
    aggregated["by_change"] = []
    # Confidence + coverage are attached by the service/CLI, not build_changeset_report.
    aggregated["confidence"] = {
        "reachable_models": 187,
        "resolved_models": 1,
        "unanalyzable_models": 186,
        "no_column_info": 186,
        "parse_failed": 0,
        "level": "partial",
    }
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.TYPE_CHANGED)], aggregated
    )
    report["coverage"] = {
        "models_in_manifest": 1217,
        "parsed_ok": 176,
        "not_in_catalog_count": 1031,
        "parse_failed": 0,
        "skipped_no_sql": 0,
        "complete": False,
    }
    md = render_changeset_markdown(report)

    assert "Confidence:" in md and "partial" in md
    assert "186 of 187" in md
    # The diagnosis must be honest: no column-level info, NOT a claim that the models
    # "haven't been built" (a built model can still be absent from the catalog).
    assert "no column-level information" in md
    assert "haven't been built in the warehouse yet" not in md
    # the "this is a floor, not a count" caveat is surfaced in the verdict banner
    assert "lower bound" in md
    # coverage stated in plain words with the real counts
    assert "Parser reached" in md and "176" in md and "1,217" in md


def test_markdown_full_confidence_is_quiet_without_coverage_warning():
    aggregated = _impact([{"name": "dm"}], [], [])
    aggregated["by_change"] = []
    aggregated["confidence"] = {
        "reachable_models": 5,
        "resolved_models": 5,
        "level": "full",
    }
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.TYPE_CHANGED)], aggregated
    )
    report["coverage"] = {"complete": True}
    md = render_changeset_markdown(report)

    assert "Confidence:" in md and "full" in md
    # A complete project must not emit the scary partial-coverage note.
    assert "Coverage is partial" not in md


def test_markdown_folds_passthrough_columns_and_omits_empty_critical_section():
    columns = [
        {
            "model": "dm",
            "column": "c1",
            "severity": "low_impact",
            "transformation_type": "direct",
            "sql_expression": "dm.c1",
        }
    ]
    aggregated = _impact([{"name": "dm"}], columns, [])
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    md = render_changeset_markdown(report)

    # Pass-throughs are low-risk: folded into a <details>, not shown as a wall of rows.
    assert "passes through unchanged" in md
    assert "<details>" in md and "`c1`" in md
    # With no derived or row-set impact, the review section is omitted entirely.
    assert "Check these — their output changes" not in md


def test_markdown_renders_filter_section_for_row_set_impact():
    columns = [
        {
            "model": "orders_flag_rate",
            "column": "(row-set)",
            "severity": "filter",
            "transformation_type": "filter",
            "sql_expression": "order_status = 'flagged'",
        }
    ]
    aggregated = _impact([{"name": "orders_flag_rate"}], columns, [])
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest",
        [ColumnChange("orders", "order_status", ChangeKind.LOGIC_CHANGED)],
        aggregated,
    )
    md = render_changeset_markdown(report)

    # Filter/join-only consumers appear in the output-changes table with plain tags,
    # with the predicate condition available in the per-model fold.
    assert "Check these — their output changes" in md
    assert "`orders_flag_rate`" in md
    assert "rows kept may change" in md and "filtered/joined on this column" in md
    assert "order_status = 'flagged'" in md
    # A row-set impact is not a pass-through.
    assert "passes through unchanged" not in md


def test_markdown_routes_exposure_to_its_owner():
    """Each affected exposure is routed to the owner who must sign off (dbt owner.name)."""
    exposures = [
        {
            "name": "Finance Dashboard",
            "type": "dashboard",
            "url": "https://x",
            "description": None,
            "owner": {"name": "Jane Doe", "email": "jane@example.com"},
            "depends_on_models": ["dm"],
        },
        {
            "name": "Billing API",
            "type": "application",
            "url": None,
            "description": None,
            "owner": {"email": "platform@example.com"},  # name absent → falls back to email
            "depends_on_models": ["dm"],
        },
    ]
    aggregated = _impact([{"name": "dm"}], [], exposures)
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    md = render_changeset_markdown(report)

    assert "owner: **Jane Doe**" in md
    assert "owner: **platform@example.com**" in md  # email fallback when name absent


def test_markdown_omits_owner_clause_when_no_owner_declared():
    exposures = [
        {
            "name": "Ownerless Dashboard",
            "type": "dashboard",
            "url": "https://x",
            "description": None,
            "owner": None,
            "depends_on_models": ["dm"],
        }
    ]
    aggregated = _impact([{"name": "dm"}], [], exposures)
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    md = render_changeset_markdown(report)

    assert "Ownerless Dashboard" in md
    assert "owner:" not in md


def test_markdown_renders_block_verdict_and_provable_breaks():
    """A provable break drives a ⛔ BLOCK banner + a compiler-style diagnostics section."""
    aggregated = _impact([{"name": "dm"}], [], [])
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("orders", "customer_id", ChangeKind.REMOVED)], aggregated
    )
    report["provable_breaks"] = [
        {
            "break_kind": "break_test",
            "change_model": "orders",
            "change_column": "customer_id",
            "change_kind": "removed",
            "test_name": "not_null",
            "test_unique_id": "test.pkg.not_null_orders_customer_id",
            "resource_path": "models/marts/_orders.yml",
            "via_reference": False,
        }
    ]
    report["verdict"] = "block"
    md = render_changeset_markdown(report)

    assert "Blocked" in md and "provable break" in md
    # The block banner outranks the heuristic safe/review banner.
    assert "Looks safe" not in md
    assert "### ⛔ Provable breaks (1)" in md
    # Compiler-style diagnostic names the exact test, change, and file to fix.
    assert "`error[BREAK-TEST]`" in md
    assert "removing `orders.customer_id`" in md
    assert "**not_null**" in md
    assert "`models/marts/_orders.yml`" in md


def test_markdown_without_breaks_is_unchanged_safe_banner():
    """No provable_breaks key → falls back to the existing safe/review banner (compatibility)."""
    aggregated = _impact([{"name": "dm"}], [], [])
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    md = render_changeset_markdown(report)

    assert "Provable breaks" not in md
    assert "Looks safe" in md


def test_markdown_notes_skipped_structural_checks_without_catalog():
    aggregated = _impact([{"name": "dm"}], [], [])
    aggregated["by_change"] = []
    report = build_changeset_report(
        "two-manifest", [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)], aggregated
    )
    report["structural_checks_available"] = False
    md = render_changeset_markdown(report)
    # One honest line telling the reviewer add/removed/type_changed were not checked.
    assert "Structural checks (type/added/removed) skipped" in md
    assert "dbt docs generate" in md


def test_markdown_omits_structural_note_when_catalog_present():
    aggregated = _impact([{"name": "dm"}], [], [])
    aggregated["by_change"] = []
    change = [ColumnChange("s", "c", ChangeKind.LOGIC_CHANGED)]

    with_catalog = build_changeset_report("two-manifest", change, aggregated)
    with_catalog["structural_checks_available"] = True
    assert "Structural checks" not in render_changeset_markdown(with_catalog)

    # Absent flag (older/other report shapes) also stays quiet — no false alarm.
    default = build_changeset_report("two-manifest", change, aggregated)
    assert "Structural checks" not in render_changeset_markdown(default)


def test_markdown_empty_changeset_warns_when_structural_skipped():
    # A clean "no changes" verdict must not hide the fact that add/removed/type_changed
    # were never checked when a catalog.json was missing on a side.
    report = build_changeset_report("two-manifest", [], {**_impact([], [], []), "by_change": []})
    report["structural_checks_available"] = False
    md = render_changeset_markdown(report)
    assert "No column changes" in md
    assert "Structural checks (type/added/removed) skipped" in md


# --- git scope filter ------------------------------------------------------


@dataclass
class _PathModel:
    columns: Dict[str, _Col]
    resource_path: Optional[str]


class _PathRegistry:
    def __init__(self, models: Dict[str, _PathModel]):
        self._models = models

    def get_models(self) -> Dict[str, _PathModel]:
        return self._models


def _registry_with_paths():
    return _PathRegistry(
        {
            "orders": _PathModel({"id": _Col("int")}, "models/orders.sql"),
            "customers": _PathModel({"id": _Col("int")}, "models/customers.sql"),
        }
    )


def test_git_changed_models_maps_files_to_models(monkeypatch):
    from parrant.lineage import changeset

    monkeypatch.setattr(
        changeset,
        "_git_changed_sql_files",
        lambda ref, repo_dir=None, git_head="HEAD": ["models/orders.sql", "macros/helper.sql"],
    )
    changed = changeset.git_changed_models(_registry_with_paths(), "origin/main")
    # orders maps to a model; the macro file has no model and is ignored.
    assert changed == {"orders"}


def test_scope_changes_to_models_filters():
    changes = [
        ColumnChange("orders", "id", ChangeKind.TYPE_CHANGED),
        ColumnChange("customers", "id", ChangeKind.LOGIC_CHANGED),
    ]
    scoped = scope_changes_to_models(changes, {"orders"})
    assert [(c.model, c.column) for c in scoped] == [("orders", "id")]


def test_scope_changes_empty_when_no_overlap():
    changes = [ColumnChange("customers", "id", ChangeKind.LOGIC_CHANGED)]
    assert scope_changes_to_models(changes, {"orders"}) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# --- override resolution -------------------------------------------------

from parrant.lineage.changeset import (  # noqa: E402
    OverrideResolution,
    resolve_overrides,
)
from parrant.models.schema import OverrideVerb  # noqa: E402


def _lc(model, column):
    return ColumnChange(model=model, column=column, kind=ChangeKind.LOGIC_CHANGED)


def test_resolve_overrides_column_scope_attaches():
    sql = 'select\n  -- lineage:allow-change column=total reason="ok"\n  total\n'
    changes = [_lc("orders", "total"), _lc("orders", "other")]
    resolved, stale, warnings = resolve_overrides({"orders": sql}, changes)
    assert stale == [] and warnings == []
    by_col = {c.column: c for c in resolved}
    assert by_col["total"].override is not None
    assert by_col["total"].override.verb is OverrideVerb.ALLOW_CHANGE
    assert by_col["other"].override is None


def test_resolve_overrides_model_scope_attaches_to_all():
    sql = '-- lineage:allow-change reason="whole model intended"\nselect total, other from t\n'
    changes = [_lc("orders", "total"), _lc("orders", "other")]
    resolved, stale, _warn = resolve_overrides({"orders": sql}, changes)
    assert stale == []
    assert all(c.override is not None and c.override.scope == "model" for c in resolved)


def test_resolve_overrides_model_scope_with_no_changes_is_stale():
    sql = '-- lineage:allow-change reason="nothing here"\nselect a from t\n'
    resolved, stale, _warn = resolve_overrides({"orders": sql}, [])
    assert resolved == []
    assert len(stale) == 1
    assert stale[0]["scope"] == "model"


def test_resolve_overrides_explicit_column_not_in_changeset_is_stale():
    sql = 'select\n  -- lineage:allow-change column=ghost reason="dead"\n  total\n'
    changes = [_lc("orders", "total")]
    resolved, stale, _warn = resolve_overrides({"orders": sql}, changes)
    assert resolved[0].override is None
    assert len(stale) == 1
    assert stale[0]["column"] == "ghost"


def test_resolve_overrides_warnings_prefixed_with_model():
    sql = 'select\n  -- lineage:allow-foo reason="x"\n  total\n'
    _resolved, _stale, warnings = resolve_overrides({"orders": sql}, [_lc("orders", "total")])
    assert len(warnings) == 1
    assert warnings[0].startswith("orders:")


def test_resolve_overrides_missing_sql_is_noop():
    resolved, stale, warnings = resolve_overrides({"orders": None}, [_lc("orders", "total")])
    assert resolved[0].override is None
    assert stale == [] and warnings == []


def test_resolve_overrides_case_insensitive_column_match():
    sql = 'select\n  -- lineage:allow-change column=TOTAL reason="ok"\n  total\n'
    resolved, stale, _warn = resolve_overrides({"orders": sql}, [_lc("orders", "total")])
    assert resolved[0].override is not None
    assert stale == []


def test_changeset_builder_honor_overrides_flag():
    # A logic change whose head compiled SQL carries a pragma is tagged; --no-overrides skips it.
    base = _FakeRegistry(
        {"orders": _Model({"total": _LinCol(lineage=[_Lin({"a"}, "derived", "a + b")])})},
        compiled={"orders": "select a + b as total"},
    )
    head_sql = (
        'select\n  -- lineage:allow-change column=total reason="intended"\n  a + c as total\n'
    )
    head = _FakeRegistry(
        {"orders": _Model({"total": _LinCol(lineage=[_Lin({"a"}, "derived", "a + c")])})},
        compiled={"orders": head_sql},
    )
    on = ChangesetBuilder(base, head, honor_overrides=True).build()
    assert len(on) == 1 and on[0].override is not None

    off = ChangesetBuilder(base, head, honor_overrides=False).build()
    assert len(off) == 1 and off[0].override is None


def test_build_git_changeset_collects_stale_and_warnings():
    class _GitReg:
        def __init__(self):
            self._m = {"orders": _Model({"total": _Col("int")})}
            self._m["orders"].resource_path = "models/orders.sql"

        def get_models(self):
            return self._m

        def get_compiled_sql(self, name):
            return 'select\n  -- lineage:allow-foo reason="x"\n  -- lineage:allow-change column=ghost reason="dead"\n  total\n'

    reg = _GitReg()
    import parrant.lineage.changeset as cs

    orig = cs.git_changed_models
    cs.git_changed_models = lambda head, git_base, repo_dir=None, git_head="HEAD": {"orders"}
    try:
        collect = OverrideResolution()
        changes = cs.build_git_changeset(reg, "origin/main", collect=collect)
    finally:
        cs.git_changed_models = orig
    assert len(changes) == 1
    assert len(collect.warnings) == 1  # unknown verb
    assert len(collect.stale) == 1  # column=ghost not in changeset
