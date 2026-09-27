"""Unit tests for the provable-break classifier (BREAK-TEST) and the verdict collapse.

Lightweight fakes stand in for ModelRegistry so these run without dbt artifacts: a fake
exposes only what the classifier calls — ``get_models`` (head-side column existence) and the
two test lookups (``get_column_tests`` / ``get_tests_referencing``).
"""

from dataclasses import dataclass, field

from parrant.lineage.changeset import ChangeKind, ColumnChange
from parrant.lineage.verdict import classify_provable_breaks, decide_verdict
from parrant.models.schema import BreakFinding, SemanticChangeKind, TestNode


@dataclass
class _Model:
    columns: dict[str, object]


@dataclass
class _FakeRegistry:
    """Stand-in for ModelRegistry covering only the classifier's call surface."""

    models: dict[str, _Model] = field(default_factory=dict)
    column_tests: dict[tuple[str, str], list[TestNode]] = field(default_factory=dict)
    referenced_tests: dict[tuple[str, str], list[TestNode]] = field(default_factory=dict)
    # Test unique_ids present in THIS manifest. On a head registry this is what lets the
    # classifier confirm a base test survived the change (see the rename-with-yml-update case).
    test_ids: set = field(default_factory=set)
    model_tests: dict[str, list[TestNode]] = field(default_factory=dict)

    def get_models(self) -> dict[str, _Model]:
        return self.models

    def get_column_tests(self, model: str, column: str) -> list[TestNode]:
        return list(self.column_tests.get((model.lower(), column.lower()), []))

    def get_tests_referencing(self, model: str, column: str) -> list[TestNode]:
        return list(self.referenced_tests.get((model.lower(), column.lower()), []))

    def get_model_tests(self, model: str) -> list[TestNode]:
        return list(self.model_tests.get(model.lower(), []))

    def get_test_unique_ids(self) -> set:
        return set(self.test_ids)


def _head_missing(model: str, remaining_cols, test_ids=()) -> _FakeRegistry:
    """A head registry where ``model`` exists but only has ``remaining_cols`` (the changed
    column dropped), carrying ``test_ids`` (tests still declared in head)."""
    return _FakeRegistry(
        models={model: _Model(columns={c: object() for c in remaining_cols})},
        test_ids=set(test_ids),
    )


def _test(uid: str, name: str, target=("orders", "customer_id"), ref=None) -> TestNode:
    return TestNode(
        unique_id=uid,
        test_name=name,
        target_model=target[0] if target else None,
        target_column=target[1] if target else None,
        referenced_model=ref[0] if ref else None,
        referenced_column=ref[1] if ref else None,
        resource_path="models/marts/_orders.yml",
    )


def _removed(model="orders", column="customer_id") -> ColumnChange:
    return ColumnChange(model, column, ChangeKind.REMOVED)


# --- classify_provable_breaks ----------------------------------------------


def test_removed_column_with_stale_test_is_a_provable_break():
    """Column removed but its test left in head (stale yml) → the test will fail → break."""
    t = _test("test.pkg.not_null_orders_customer_id", "not_null")
    base = _FakeRegistry(column_tests={("orders", "customer_id"): [t]})
    head = _head_missing("orders", ["id"], test_ids={t.unique_id})  # test still declared in head

    breaks = classify_provable_breaks([_removed()], head, base)

    assert len(breaks) == 1
    assert breaks[0].test_name == "not_null"
    assert breaks[0].change_kind == "removed"
    assert breaks[0].via_reference is False
    assert breaks[0].code() == "BREAK-TEST"


def test_rename_with_test_updated_is_not_a_break():
    """The dominant safe refactor: rename the column AND update its test's yml in one PR.

    The old column is gone in head, but the base test's unique_id is NOT in head (dbt minted
    a new id for the renamed target), so it must NOT be flagged — the classic false BLOCK.
    """
    old = _test("test.pkg.not_null_orders_customer_id", "not_null")
    base = _FakeRegistry(column_tests={("orders", "customer_id"): [old]})
    # head has the renamed column and a NEW test id targeting it; the old id is absent.
    head = _head_missing("orders", ["client_id"], test_ids={"test.pkg.not_null_orders_client_id"})

    assert classify_provable_breaks([_removed()], head, base) == []


def test_column_and_test_both_deleted_is_not_a_break():
    """Deleting a column together with its yml test drops the test id → nothing fails."""
    t = _test("test.pkg.not_null_orders_customer_id", "not_null")
    base = _FakeRegistry(column_tests={("orders", "customer_id"): [t]})
    head = _head_missing("orders", ["id"], test_ids=set())  # test removed too

    assert classify_provable_breaks([_removed()], head, base) == []


def test_wholly_removed_model_breaks_tests_on_unrecovered_columns():
    """A dropped model breaks ALL its tests — even tests on columns column-recovery missed.

    Real analytics case: the changeset only lists columns recovered from compiled SQL, which
    can miss the model's tested keys. Because the model is gone from head entirely, every test
    attached to it must still be flagged via get_model_tests.
    """
    t = _test("test.pkg.unique_m_pk", "unique", target=("m", "pk"))
    # Base knows the test is attached to m; the changeset only saw an unrelated recovered col.
    base = _FakeRegistry(model_tests={"m": [t]})
    head = _FakeRegistry(models={}, test_ids={t.unique_id})  # model m absent from head

    breaks = classify_provable_breaks([_removed("m", "some_recovered_col")], head, base)

    assert len(breaks) == 1
    assert breaks[0].test_name == "unique"
    assert breaks[0].change_column == "pk"  # the tested key, not the recovered column


def test_no_break_when_column_still_present_in_head():
    """A removal the head reverted (column still there) must NOT be flagged — provable only."""
    t = _test("test.pkg.not_null_orders_customer_id", "not_null")
    base = _FakeRegistry(column_tests={("orders", "customer_id"): [t]})
    head = _FakeRegistry(
        models={"orders": _Model(columns={"customer_id": object()})}, test_ids={t.unique_id}
    )

    assert classify_provable_breaks([_removed()], head, base) == []


def test_relationships_break_via_referenced_parent_key():
    """Dropping the parent key a relationships test points at breaks it (referenced side)."""
    t = _test(
        "test.pkg.relationships_payments_customer",
        "relationships",
        target=("payments", "customer_id"),
        ref=("customers", "id"),
    )
    base = _FakeRegistry(referenced_tests={("customers", "id"): [t]})
    head = _head_missing("customers", ["name"], test_ids={t.unique_id})  # id gone, test stays

    breaks = classify_provable_breaks([_removed("customers", "id")], head, base)

    assert len(breaks) == 1
    assert breaks[0].via_reference is True
    assert breaks[0].test_name == "relationships"


def test_logic_change_is_not_a_provable_break():
    t = _test("test.pkg.not_null_orders_customer_id", "not_null")
    base = _FakeRegistry(column_tests={("orders", "customer_id"): [t]})
    head = _FakeRegistry(models={"orders": _Model(columns={"customer_id": object()})})

    change = ColumnChange("orders", "customer_id", ChangeKind.LOGIC_CHANGED)
    assert classify_provable_breaks([change], head, base) == []


def test_no_base_registry_proves_nothing():
    """Git-diff fallback (no base) yields no provable breaks rather than guessing."""
    head = _FakeRegistry(models={"orders": _Model(columns={"id": object()})})
    assert classify_provable_breaks([_removed()], head, None) == []


def test_whole_model_removed_breaks_its_tests():
    t = _test("test.pkg.unique_orders_customer_id", "unique")
    base = _FakeRegistry(column_tests={("orders", "customer_id"): [t]}, model_tests={"orders": [t]})
    head = _FakeRegistry(models={}, test_ids={t.unique_id})  # model gone, test still declared

    breaks = classify_provable_breaks([_removed()], head, base)
    assert len(breaks) == 1 and breaks[0].test_name == "unique"


def test_a_single_test_is_not_double_counted_per_change():
    """A test indexed on both sides for the same change is emitted once."""
    t = _test(
        "test.pkg.relationships_self",
        "relationships",
        target=("orders", "customer_id"),
        ref=("orders", "customer_id"),
    )
    base = _FakeRegistry(
        column_tests={("orders", "customer_id"): [t]},
        referenced_tests={("orders", "customer_id"): [t]},
    )
    head = _FakeRegistry(models={"orders": _Model(columns={})}, test_ids={t.unique_id})

    breaks = classify_provable_breaks([_removed()], head, base)
    assert len(breaks) == 1


# --- decide_verdict ---------------------------------------------------------


def _finding() -> BreakFinding:
    return BreakFinding(
        break_kind="break_test",
        change_model="orders",
        change_column="customer_id",
        change_kind="removed",
        test_name="not_null",
        test_unique_id="test.pkg.x",
    )


def test_verdict_block_when_any_provable_break():
    assert decide_verdict([_finding()], {"affected_exposures": 0}) == "block"


def test_verdict_review_when_reach_without_break():
    assert decide_verdict([], {"critical_count": 2}) == "review"
    assert decide_verdict([], {"affected_exposures": 1}) == "review"
    assert decide_verdict([], {"filter_count": 3}) == "review"


def test_verdict_safe_when_nothing_recomputes():
    assert (
        decide_verdict([], {"critical_count": 0, "filter_count": 0, "affected_exposures": 0})
        == "safe"
    )


_NO_REACH = {"critical_count": 0, "filter_count": 0, "affected_exposures": 0}


def _logic_change(semantic: SemanticChangeKind) -> ColumnChange:
    return ColumnChange(
        model="orders", column="total", kind=ChangeKind.LOGIC_CHANGED, semantic=semantic
    )


def test_verdict_meaning_changed_lifts_safe_to_review():
    changes = [_logic_change(SemanticChangeKind.MEANING_CHANGED)]
    assert decide_verdict([], _NO_REACH, changes) == "review"


def test_verdict_indeterminate_lifts_safe_to_review():
    changes = [_logic_change(SemanticChangeKind.INDETERMINATE)]
    assert decide_verdict([], _NO_REACH, changes) == "review"


def test_verdict_equivalent_stays_safe():
    # EQUIVALENT is normally never emitted, but if it appears it must not escalate.
    changes = [_logic_change(SemanticChangeKind.EQUIVALENT)]
    assert decide_verdict([], _NO_REACH, changes) == "safe"


def test_verdict_structural_change_without_semantic_stays_safe():
    # A removed/added column carries no semantic; the semantic axis leaves it untouched here
    # (its own severity is conveyed elsewhere, not by this axis).
    changes = [ColumnChange(model="orders", column="total", kind=ChangeKind.REMOVED)]
    assert decide_verdict([], _NO_REACH, changes) == "safe"


def test_verdict_meaning_shift_never_blocks():
    # BLOCK stays reserved for provable breaks; a meaning shift can only reach review.
    changes = [_logic_change(SemanticChangeKind.MEANING_CHANGED)]
    assert decide_verdict([], _NO_REACH, changes) != "block"


def test_verdict_omitting_changes_is_backward_compatible():
    # Legacy 2-arg calls keep the pre-semantic behavior.
    assert decide_verdict([], _NO_REACH) == "safe"


# --- overrides: decide_verdict + applied/ineffective override records ----

from parrant.lineage.verdict import (
    applied_overrides,
    ineffective_overrides,
    unexcused_break_count,
)
from parrant.models.schema import OverrideDirective, OverrideVerb


def _directive(verb, column=None, scope="column", reason="because"):
    return OverrideDirective(verb=verb, column=column, reason=reason, scope=scope, source_line=2)


def _removed_change(model="orders", column="customer_id", override=None):
    return ColumnChange(model=model, column=column, kind=ChangeKind.REMOVED, override=override)


def _logic_change_ov(semantic, override=None, model="orders", column="total"):
    return ColumnChange(
        model=model,
        column=column,
        kind=ChangeKind.LOGIC_CHANGED,
        semantic=semantic,
        override=override,
    )


def test_allow_break_demotes_lone_break_to_review():
    ov = _directive(OverrideVerb.ALLOW_BREAK, column="customer_id")
    change = _removed_change(override=ov)
    # A single provable break, acknowledged by allow-break -> floors at review, never safe.
    assert decide_verdict([_finding()], _NO_REACH, [change]) == "review"


def test_allow_change_cannot_downgrade_a_provable_break():
    # HEADLINE FAIL-SAFE: the soft verb must NOT silence a break -> stays block.
    ov = _directive(OverrideVerb.ALLOW_CHANGE, column="customer_id")
    change = _removed_change(override=ov)
    assert decide_verdict([_finding()], _NO_REACH, [change]) == "block"


def test_allow_break_beats_allow_change_precedence_stays_review():
    # Two pragmas on the same break column: hard (allow-break) wins -> demoted to review,
    # never safe, never block.
    from parrant.lineage.changeset import resolve_overrides

    hard = 'select\n  -- lineage:allow-change column=customer_id reason="soft"\n  -- lineage:allow-break column=customer_id reason="hard"\n  x\n'
    change = _removed_change()
    resolved, _stale, _warn = resolve_overrides({"orders": hard}, [change])
    assert resolved[0].override.verb is OverrideVerb.ALLOW_BREAK
    assert decide_verdict([_finding()], _NO_REACH, resolved) == "review"


def test_unexcused_break_count_drops_with_allow_break():
    ov = _directive(OverrideVerb.ALLOW_BREAK, column="customer_id")
    change = _removed_change(override=ov)
    assert unexcused_break_count([_finding()], [change]) == 0
    # allow-change does NOT excuse a break.
    change2 = _removed_change(override=_directive(OverrideVerb.ALLOW_CHANGE, column="customer_id"))
    assert unexcused_break_count([_finding()], [change2]) == 1
    # no changes / no override -> count unchanged.
    assert unexcused_break_count([_finding()], None) == 1


def test_reach_review_suppressed_only_when_all_reaching_overridden():
    ov = _directive(OverrideVerb.ALLOW_CHANGE, column="total")
    change = _logic_change_ov(SemanticChangeKind.EQUIVALENT, override=ov)
    by_change = [
        {
            "model": "orders",
            "column": "total",
            "kind": "logic_changed",
            "resolved": True,
            "reached_columns": [{"model": "m", "column": "c", "mechanism": "derived_recompute"}],
            "reached_exposures": [],
        }
    ]
    summary = {"critical_count": 1, "filter_count": 0, "affected_exposures": 0}
    # Every reaching change is overridden -> review suppressed to safe.
    assert decide_verdict([], summary, [change], by_change=by_change) == "safe"


def test_reach_review_never_suppressed_in_fallback_without_by_change():
    # by_change absent -> reach review CANNOT be suppressed (fail-safe), even with an override.
    ov = _directive(OverrideVerb.ALLOW_CHANGE, column="total")
    change = _logic_change_ov(SemanticChangeKind.EQUIVALENT, override=ov)
    summary = {"critical_count": 0, "filter_count": 0, "affected_exposures": 1}
    assert decide_verdict([], summary, [change], by_change=None) == "review"


def test_reach_review_stands_when_one_reaching_change_unacknowledged():
    ack = _logic_change_ov(
        SemanticChangeKind.EQUIVALENT,
        override=_directive(OverrideVerb.ALLOW_CHANGE, column="total"),
        column="total",
    )
    unack = _logic_change_ov(SemanticChangeKind.EQUIVALENT, override=None, column="other")
    by_change = [
        {
            "model": "orders",
            "column": "total",
            "kind": "logic_changed",
            "resolved": True,
            "reached_columns": [{"model": "m", "column": "c", "mechanism": "rowset_filter"}],
            "reached_exposures": [],
        },
        {
            "model": "orders",
            "column": "other",
            "kind": "logic_changed",
            "resolved": True,
            "reached_columns": [{"model": "m", "column": "d", "mechanism": "derived_recompute"}],
            "reached_exposures": [],
        },
    ]
    summary = {"critical_count": 2, "filter_count": 0, "affected_exposures": 0}
    assert decide_verdict([], summary, [ack, unack], by_change=by_change) == "review"


def test_verdict_parity_no_override_matches_legacy():
    # With no pragma present, threading by_change must not change the string vs legacy.
    change = _logic_change_ov(SemanticChangeKind.MEANING_CHANGED, override=None)
    by_change = [
        {
            "model": "orders",
            "column": "total",
            "kind": "logic_changed",
            "resolved": True,
            "reached_columns": [{"model": "m", "column": "c", "mechanism": "derived_recompute"}],
            "reached_exposures": [],
        },
    ]
    summary = {"critical_count": 1, "filter_count": 0, "affected_exposures": 0}
    assert decide_verdict([], summary, [change]) == "review"
    assert decide_verdict([], summary, [change], by_change=by_change) == "review"


def test_applied_overrides_records_break_demotion():
    ov = _directive(OverrideVerb.ALLOW_BREAK, column="customer_id")
    change = _removed_change(override=ov)
    records = applied_overrides([change], [_finding()], by_change=[])
    assert len(records) == 1
    assert records[0]["downgraded_from"] == "block"
    assert records[0]["downgraded_to"] == "review"
    assert records[0]["verb"] == "allow-break"
    assert records[0]["reason"] == "because"


def test_applied_overrides_skips_allow_change_on_break():
    ov = _directive(OverrideVerb.ALLOW_CHANGE, column="customer_id")
    change = _removed_change(override=ov)
    assert applied_overrides([change], [_finding()], by_change=[]) == []
    # ...and it surfaces as ineffective with a "use allow-break" hint.
    ineff = ineffective_overrides([change], [_finding()], by_change=[])
    assert len(ineff) == 1
    assert "allow-break" in ineff[0]["hint"]


def test_ineffective_override_on_added_column_gets_rename_hint():
    ov = _directive(OverrideVerb.ALLOW_BREAK, column="amount_eur")
    change = ColumnChange(model="orders", column="amount_eur", kind=ChangeKind.ADDED, override=ov)
    ineff = ineffective_overrides([change], [], by_change=[])
    assert len(ineff) == 1
    assert "REMOVED old column" in ineff[0]["hint"]


def test_applied_overrides_records_reach_suppression():
    ov = _directive(OverrideVerb.ALLOW_CHANGE, column="total")
    change = _logic_change_ov(SemanticChangeKind.MEANING_CHANGED, override=ov)
    records = applied_overrides([change], [], by_change=[])
    assert len(records) == 1
    assert records[0]["downgraded_from"] == "review"
    assert records[0]["downgraded_to"] == "safe"
