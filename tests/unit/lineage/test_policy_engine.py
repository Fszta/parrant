"""Tests for the metadata-agnostic policy engine.

Covered:
  * the two examples as PURE CONFIG fixtures (PII allowlist, breaking->critical);
  * the operator set (subset/intersects/numeric/regex/is_true...);
  * predicate composition (all/any/not) and Kleene UNKNOWN propagation;
  * action combination (most-severe-wins gate, set-union build/test, notify dedup);
  * FAIL-SAFE: INDETERMINATE/absent semantic -> breaking; unknown reach + fail_closed ->
    not silently safe; missing meta key -> documented fail_closed/fail_open/skip defaults;
  * backward-compat: legacy --fail-on tests reproduced by a one-line structural rule.
"""

import os

from parrant.lineage.changeset import ChangeKind, ColumnChange
from parrant.lineage.policy import (
    MetaIndex,
    PolicyEngine,
    build_impact_view,
    evaluate_policy,
    load_policy,
    parse_policy,
)
from parrant.models.schema import (
    ActionKind,
    BreakFinding,
    GateDecision,
    SemanticChangeKind,
)

_POLICY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "resources", "policies")


def _fixture(name):
    return load_policy(os.path.join(_POLICY_DIR, name))


# --- fakes ------------------------------------------------------------------


class _FakeExposure:
    def __init__(self, name, type_="dashboard", owner=None, metadata=None):
        self.name = name
        self.type = type_
        self.owner = owner or {}
        self.metadata = metadata or {}


class FakeRegistry:
    """A hand-written stand-in for the registry meta accessors."""

    def __init__(self, model_meta=None, column_meta=None, exposures=None):
        self._model_meta = model_meta or {}
        self._column_meta = column_meta or {}
        self._exposures = exposures or {}

    def get_model_dbt_meta(self, model):
        return dict(self._model_meta.get(model, {}))

    def get_column_dbt_meta(self, model, column):
        return dict(self._column_meta.get((model, column), {}))

    def get_exposures(self):
        return self._exposures


def _impact(*entries):
    """Build a get_changeset_impact-shaped dict from resolved by_change entries."""
    return {"by_change": list(entries)}


def _resolved(model, column, kind="logic_changed", models=None, columns=None, exposures=None):
    return {
        "model": model,
        "column": column,
        "kind": kind,
        "resolved": True,
        "reached_models": models or [],
        "reached_columns": columns or [],
        "reached_exposures": exposures or [],
    }


def _unresolved(model, column, kind="removed"):
    return {"model": model, "column": column, "kind": kind, "resolved": False}


def _engine(policy, registry, impact, breaks=None):
    return PolicyEngine(policy, MetaIndex(registry), build_impact_view(impact), breaks or [])


def _change(model="accounts", column="ssn", kind=ChangeKind.LOGIC_CHANGED, semantic=None):
    return ColumnChange(model, column, kind, semantic=semantic)


# --- Example (a): PII allowlist ---------------------------------------


def test_pii_reaching_non_allowlisted_reader_blocks():
    registry = FakeRegistry(
        model_meta={"analyst_mart": {"readable_by": ["ANALYST", "DATA_ANALYST"]}},
        column_meta={("accounts", "ssn"): {"pii": True}},
    )
    impact = _impact(
        _resolved(
            "accounts",
            "ssn",
            models=[{"name": "analyst_mart", "mechanism": "direct_passthrough"}],
        )
    )
    engine = _engine(_fixture("pii_allowlist.yml"), registry, impact)
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.blocks()
    assert verdict.fired_rules == 1
    # The notify action fired with interpolated message.
    assert verdict.notifications[0].target == "#data-governance"
    assert "accounts.ssn" in verdict.notifications[0].message


def test_pii_reaching_only_allowlisted_readers_does_not_block():
    registry = FakeRegistry(
        model_meta={"compliance_mart": {"readable_by": ["COMPLIANCE"]}},
        column_meta={("accounts", "ssn"): {"pii": True}},
    )
    impact = _impact(
        _resolved(
            "accounts",
            "ssn",
            models=[{"name": "compliance_mart", "mechanism": "direct_passthrough"}],
        )
    )
    engine = _engine(_fixture("pii_allowlist.yml"), registry, impact)
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0


def test_non_pii_column_never_fires_pii_rule():
    registry = FakeRegistry(
        model_meta={"analyst_mart": {"readable_by": ["ANALYST"]}},
        column_meta={("accounts", "created_at"): {}},
    )
    impact = _impact(
        _resolved(
            "accounts",
            "created_at",
            models=[{"name": "analyst_mart", "mechanism": "direct_passthrough"}],
        )
    )
    engine = _engine(_fixture("pii_allowlist.yml"), registry, impact)
    verdict = engine.evaluate(
        [_change(column="created_at", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.decision is GateDecision.ALLOW


# --- Example (b): breaking -> critical --------------------------------


def test_breaking_change_reaching_critical_blocks_and_builds():
    registry = FakeRegistry(
        model_meta={
            "critical_mart": {"critical": True},
            "other_mart": {"critical": False},
        }
    )
    impact = _impact(
        _resolved(
            "orders",
            "amount",
            models=[
                {"name": "critical_mart", "mechanism": "derived_recompute"},
                {"name": "other_mart", "mechanism": "direct_passthrough"},
            ],
        )
    )
    engine = _engine(_fixture("breaking_reaches_critical.yml"), registry, impact)
    verdict = engine.evaluate(
        [_change("orders", "amount", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.blocks()
    # build-set is mechanism-filtered to recompute/rowset: only critical_mart, not other_mart.
    assert verdict.build_set == ["critical_mart"]


def test_equivalent_change_reaching_critical_does_not_block():
    registry = FakeRegistry(model_meta={"critical_mart": {"critical": True}})
    impact = _impact(
        _resolved(
            "orders",
            "amount",
            models=[{"name": "critical_mart", "mechanism": "derived_recompute"}],
        )
    )
    engine = _engine(_fixture("breaking_reaches_critical.yml"), registry, impact)
    # A proven-equivalent refactor is NOT breaking -> the rule must not fire.
    verdict = engine.evaluate([_change("orders", "amount", semantic=SemanticChangeKind.EQUIVALENT)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.build_set == []


# --- operator set -----------------------------------------------------------


def _one_meta_rule(key, op, value=None, action="block"):
    cond = {"key": key, "op": op}
    if value is not None:
        cond["value"] = value
    return parse_policy(
        {
            "version": 1,
            "rules": [{"id": "r", "predicate": {"meta": cond}, "action": [{"type": action}]}],
        }
    )


def _fire(policy, model_meta):
    registry = FakeRegistry(model_meta={"m": model_meta})
    impact = _impact(_resolved("m", "c"))
    engine = _engine(policy, registry, impact)
    return engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])


def test_operator_eq_and_ne():
    assert _fire(_one_meta_rule("tier", "eq", "gold"), {"tier": "gold"}).blocks()
    assert not _fire(_one_meta_rule("tier", "eq", "gold"), {"tier": "silver"}).blocks()
    assert _fire(_one_meta_rule("tier", "ne", "gold"), {"tier": "silver"}).blocks()


def test_operator_in_not_in():
    assert _fire(_one_meta_rule("tier", "in", ["gold", "plat"]), {"tier": "gold"}).blocks()
    assert _fire(_one_meta_rule("tier", "not_in", ["gold"]), {"tier": "silver"}).blocks()


def test_operator_subset_not_subset_superset_intersects():
    assert _fire(
        _one_meta_rule("roles", "subset_of", ["A", "B", "C"]), {"roles": ["A", "B"]}
    ).blocks()
    assert _fire(
        _one_meta_rule("roles", "not_subset_of", ["A", "B"]), {"roles": ["A", "X"]}
    ).blocks()
    assert _fire(_one_meta_rule("roles", "superset_of", ["A"]), {"roles": ["A", "B"]}).blocks()
    assert _fire(_one_meta_rule("roles", "intersects", ["X", "A"]), {"roles": ["A", "B"]}).blocks()


def test_operator_numeric_and_matches_and_bool():
    assert _fire(_one_meta_rule("rows", "gt", 100), {"rows": 500}).blocks()
    assert not _fire(_one_meta_rule("rows", "gt", 100), {"rows": 50}).blocks()
    assert _fire(_one_meta_rule("rows", "le", 100), {"rows": 100}).blocks()
    assert _fire(_one_meta_rule("name", "matches", "dim_.*"), {"name": "dim_accounts"}).blocks()
    assert _fire(_one_meta_rule("flag", "is_true"), {"flag": True}).blocks()
    assert _fire(_one_meta_rule("flag", "is_false"), {"flag": False}).blocks()
    assert _fire(_one_meta_rule("flag", "exists"), {"flag": False}).blocks()
    assert _fire(_one_meta_rule("flag", "absent"), {"other": 1}).blocks()


# --- predicate composition --------------------------------------------------


def test_any_or_semantics():
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {
                        "any": [
                            {"meta": {"key": "a", "op": "is_true"}},
                            {"meta": {"key": "b", "op": "is_true"}},
                        ]
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    assert _fire(policy, {"a": False, "b": True}).blocks()
    assert not _fire(policy, {"a": False, "b": False}).blocks()


def test_not_negation():
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {"not": {"meta": {"key": "a", "op": "is_true"}}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    assert _fire(policy, {"a": False}).blocks()
    assert not _fire(policy, {"a": True}).blocks()


# --- action combination -----------------------------------------------------


def test_most_severe_wins_block_over_warn():
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "w",
                    "predicate": {"meta": {"key": "a", "op": "is_true"}},
                    "action": [{"type": "warn"}],
                },
                {
                    "id": "b",
                    "predicate": {"meta": {"key": "a", "op": "is_true"}},
                    "action": [{"type": "block"}],
                },
            ],
        }
    )
    verdict = _fire(policy, {"a": True})
    assert verdict.decision is GateDecision.BLOCK
    assert verdict.fired_rules == 2  # no first-match short-circuit


def test_warn_only_yields_warn_decision():
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "w",
                    "predicate": {"meta": {"key": "a", "op": "is_true"}},
                    "action": [{"type": "warn"}],
                },
            ],
        }
    )
    assert _fire(policy, {"a": True}).decision is GateDecision.WARN


def test_build_and_test_sets_union_across_rules():
    registry = FakeRegistry(model_meta={"m1": {}, "m2": {}})
    impact = _impact(
        _resolved(
            "src",
            "c",
            models=[
                {"name": "m1", "mechanism": "derived_recompute"},
                {"name": "m2", "mechanism": "rowset_filter"},
            ],
        )
    )
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "build",
                    "predicate": {"change": {"field": "breaking", "op": "is_true"}},
                    "action": [
                        {"type": "add-to-build-set", "include": "both"},
                        {"type": "warn"},
                    ],
                },
                {
                    "id": "test",
                    "predicate": {"change": {"field": "breaking", "op": "is_true"}},
                    "action": [{"type": "add-to-test-set", "include": "reached"}],
                },
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("src", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    # include=both -> reached m1,m2 plus the subject model src.
    assert verdict.build_set == ["m1", "m2", "src"]
    assert verdict.test_set == ["m1", "m2"]


def test_notifications_dedup_by_channel_target_message():
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    rule = {
        "id": "n",
        "predicate": {"change": {"field": "breaking", "op": "is_true"}},
        "action": [
            {"type": "notify", "channel": "slack", "target": "#x", "message": "same"},
            {"type": "notify", "channel": "slack", "target": "#x", "message": "same"},
        ],
    }
    policy = parse_policy({"version": 1, "rules": [rule]})
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert len(verdict.notifications) == 1


# --- fail-safe: semantic ----------------------------------------------------


def test_absent_semantic_is_treated_as_breaking():
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {"change": {"field": "breaking", "op": "is_true"}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    # semantic=None -> breaking (fail-safe).
    verdict = engine.evaluate([_change("m", "c", semantic=None)])
    assert verdict.blocks()


def test_indeterminate_semantic_is_breaking():
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {"change": {"field": "breaking", "op": "is_true"}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.INDETERMINATE)])
    assert verdict.blocks()


def test_equivalent_semantic_is_not_breaking():
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {"change": {"field": "breaking", "op": "is_true"}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.EQUIVALENT)])
    assert verdict.decision is GateDecision.ALLOW


# --- fail-safe: unknown reach -----------------------------------------------


def test_unknown_reach_fail_closed_blocks():
    """A removed column whose reach is unresolved must NOT silently pass a blocking rule."""
    registry = FakeRegistry(model_meta={})
    impact = _impact(_unresolved("accounts", "ssn", kind="removed"))
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {
                        "reach": {
                            "kind": "model",
                            "where": {"meta": {"key": "critical", "op": "is_true"}},
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("accounts", "ssn", kind=ChangeKind.REMOVED)])
    assert verdict.blocks()
    assert verdict.unresolved_reach_count == 1


def test_unknown_reach_fail_open_does_not_block():
    registry = FakeRegistry(model_meta={})
    impact = _impact(_unresolved("accounts", "ssn", kind="removed"))
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {
                        "reach": {
                            "kind": "model",
                            "where": {"meta": {"key": "critical", "op": "is_true"}},
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("accounts", "ssn", kind=ChangeKind.REMOVED)])
    assert verdict.decision is GateDecision.ALLOW


# --- fail-safe: missing meta key --------------------------------------------


def test_missing_meta_fail_closed_blocking_rule_fires():
    """A reached mart that forgot to declare readable_by is treated as exposing to everyone."""
    registry = FakeRegistry(
        model_meta={"mystery_mart": {}},  # readable_by absent
        column_meta={("accounts", "ssn"): {"pii": True}},
    )
    impact = _impact(
        _resolved(
            "accounts",
            "ssn",
            models=[{"name": "mystery_mart", "mechanism": "direct_passthrough"}],
        )
    )
    engine = _engine(_fixture("pii_allowlist.yml"), registry, impact)
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.blocks()  # missing readable_by under fail_closed -> blind risk -> block


def test_missing_meta_fail_open_does_not_fire():
    registry = FakeRegistry(model_meta={"m": {}})  # 'a' absent
    impact = _impact(_resolved("m", "c"))
    # A value operator on a missing key is genuinely UNKNOWN -> fail_open must not fire.
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {"meta": {"key": "a", "op": "eq", "value": 1}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW


def test_missing_meta_skip_records_skipped():
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "skip"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {"meta": {"key": "a", "op": "eq", "value": 1}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.skipped_missing_meta == 1


def test_missing_meta_fail_closed_nonblocking_rule_does_not_fire():
    """The asymmetry: fail_closed biases BLOCKING rules to fire but not warn-only rules."""
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    # eq on a missing key -> UNKNOWN; a warn-only rule under fail_closed must NOT fire.
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {"meta": {"key": "a", "op": "eq", "value": 1}},
                    "action": [{"type": "warn"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact)
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW  # no spurious warning manufactured


# --- fail-safe: operator/type mismatch --------------------------------------


def test_type_mismatch_blocking_rule_fails_closed():
    # subset_of against a scalar -> UNKNOWN_ERROR -> blocking rule under (default) fail_closed
    # fires. This is the DEFAULT-config baseline; it must stay green after the on_error split.
    registry = FakeRegistry(model_meta={"m": {"roles": "ADMIN"}})
    impact = _impact(_resolved("m", "c"))
    policy = _one_meta_rule("roles", "subset_of", ["A", "B"])
    verdict = _engine(policy, registry, impact).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.blocks()


def test_on_error_honored_independently_of_on_missing_meta():
    """Regression (fail-safe defect): the two knobs are INDEPENDENT.

    With ``on_missing_meta: fail_open`` + ``on_error: fail_closed`` a genuine operator/type
    mismatch resolves via ``on_error`` and BLOCKS, while a genuinely missing key resolves via
    ``on_missing_meta`` and ALLOWs — proving one knob no longer swallows the other.
    """
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open", "on_error": "fail_closed"},
            "rules": [
                {
                    "id": "roles-subset",
                    "predicate": {"meta": {"key": "roles", "op": "subset_of", "value": ["A", "B"]}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    # (a) roles is a scalar -> subset_of type mismatch -> UNKNOWN_ERROR -> on_error fail_closed.
    err_verdict = _engine(
        policy, FakeRegistry(model_meta={"m": {"roles": "ADMIN"}}), _impact(_resolved("m", "c"))
    ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert err_verdict.blocks()  # genuine type error blocks under on_error fail_closed

    # (b) roles absent -> UNKNOWN_MISSING -> on_missing_meta fail_open -> no fire.
    miss_verdict = _engine(
        policy, FakeRegistry(model_meta={"m": {}}), _impact(_resolved("m", "c"))
    ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert miss_verdict.decision is GateDecision.ALLOW  # missing key still fail_open


def test_rule_level_on_error_overrides_default():
    """A per-rule ``on_error`` overrides the policy default, mirroring ``on_missing_meta``."""
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_error": "fail_closed"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {"meta": {"key": "roles", "op": "subset_of", "value": ["A", "B"]}},
                    "action": [{"type": "block"}],
                    "on_error": "fail_open",
                }
            ],
        }
    )
    # scalar roles -> type mismatch; the rule-level on_error fail_open wins over default closed.
    verdict = _engine(
        policy, FakeRegistry(model_meta={"m": {"roles": "ADMIN"}}), _impact(_resolved("m", "c"))
    ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW


def test_on_error_fail_open_still_blocks_on_missing_meta_fail_closed():
    """The mirror image: on_error fail_open must NOT swallow a missing-meta fail_closed block."""
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_closed", "on_error": "fail_open"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {"meta": {"key": "readable_by", "op": "eq", "value": "x"}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    # readable_by absent -> UNKNOWN_MISSING -> on_missing_meta fail_closed -> blocking rule fires.
    verdict = _engine(
        policy, FakeRegistry(model_meta={"m": {}}), _impact(_resolved("m", "c"))
    ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.blocks()


# --- fired_on_unknown trust signal on RuleHit ---------------------------


def test_fired_on_unknown_true_on_missing_meta_fail_closed_block():
    """A blocking rule firing via a fail-safe UNKNOWN (missing meta) is marked fired_on_unknown."""
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_closed"},
            "rules": [
                {
                    "id": "naive",
                    "predicate": {"meta": {"key": "pii", "op": "eq", "value": True}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    # pii absent -> eq on a missing key -> UNKNOWN_MISSING -> blocking rule fires via fail-safe.
    verdict = _engine(
        policy, FakeRegistry(model_meta={"m": {}}), _impact(_resolved("m", "c"))
    ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.blocks()
    assert len(verdict.hits) == 1
    assert verdict.hits[0].fired_on_unknown is True
    assert verdict.hits[0].unknown_cause == "missing"


def test_fired_on_unknown_error_cause_on_type_mismatch():
    """A blocking rule firing via a type error (UNKNOWN_ERROR) records unknown_cause='error'."""
    policy = _one_meta_rule("roles", "subset_of", ["A", "B"])  # subset_of on a scalar -> error
    verdict = _engine(
        policy, FakeRegistry(model_meta={"m": {"roles": "ADMIN"}}), _impact(_resolved("m", "c"))
    ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.blocks()
    assert verdict.hits[0].fired_on_unknown is True
    assert verdict.hits[0].unknown_cause == "error"


def test_fired_on_unknown_false_on_proven_true_match():
    """A rule that fired on a proven TRUE match leaves fired_on_unknown False."""
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "proven",
                    "predicate": {"meta": {"key": "pii", "op": "is_true"}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    registry = FakeRegistry(column_meta={("m", "c"): {"pii": True}})
    verdict = _engine(policy, registry, _impact(_resolved("m", "c"))).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.blocks()
    assert verdict.hits[0].fired_on_unknown is False
    assert verdict.hits[0].unknown_cause is None


def test_skip_and_fail_open_produce_no_fired_hit():
    """Neither skip nor fail_open should manufacture a fired hit on UNKNOWN."""
    for knob in ("skip", "fail_open"):
        policy = parse_policy(
            {
                "version": 1,
                "defaults": {"on_missing_meta": knob},
                "rules": [
                    {
                        "id": "r",
                        "predicate": {"meta": {"key": "pii", "op": "eq", "value": True}},
                        "action": [{"type": "block"}],
                    }
                ],
            }
        )
        verdict = _engine(
            policy, FakeRegistry(model_meta={"m": {}}), _impact(_resolved("m", "c"))
        ).evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
        assert verdict.hits == []


def test_builtin_semantic_default_hit_is_not_fired_on_unknown():
    """A builtin semantic-default hit is always proven, never fired_on_unknown."""
    policy = parse_policy({"version": 1, "defaults": {"on_indeterminate": "block"}, "rules": []})
    verdict = _engine(policy, FakeRegistry(), _impact(_resolved("m", "c"))).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.INDETERMINATE)]
    )
    assert verdict.blocks()
    assert verdict.hits[0].rule_id == "builtin:on_indeterminate"
    assert verdict.hits[0].fired_on_unknown is False


# --- reach: mechanism filter + exposures + min_count ------------------------


def test_reach_mechanism_filter_restricts_scan():
    registry = FakeRegistry(model_meta={"recompute_mart": {"critical": True}})
    impact = _impact(
        _resolved(
            "src",
            "c",
            models=[
                {"name": "recompute_mart", "mechanism": "direct_passthrough"},
            ],
        )
    )
    # Only look at derived_recompute reach; the critical mart is reached by passthrough here.
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {
                        "reach": {
                            "kind": "model",
                            "mechanism": ["derived_recompute"],
                            "where": {"meta": {"key": "critical", "op": "is_true"}},
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    verdict = _engine(policy, registry, impact).evaluate(
        [_change("src", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    # No derived_recompute reach -> reach FALSE -> rule does not fire.
    assert verdict.decision is GateDecision.ALLOW


def test_reach_exposure_where_matches_exposure_meta():
    registry = FakeRegistry(
        exposures={"revenue_dash": _FakeExposure("revenue_dash", metadata={"audience": "public"})}
    )
    impact = _impact(_resolved("m", "c", exposures=[{"name": "revenue_dash"}]))
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "r",
                    "predicate": {
                        "reach": {
                            "kind": "exposure",
                            "where": {"meta": {"key": "audience", "op": "eq", "value": "public"}},
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    verdict = _engine(policy, registry, impact).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.blocks()


def test_reach_min_count_requires_multiple_matches():
    registry = FakeRegistry(model_meta={"m1": {"critical": True}, "m2": {"critical": False}})
    impact = _impact(
        _resolved(
            "src",
            "c",
            models=[
                {"name": "m1", "mechanism": "direct_passthrough"},
                {"name": "m2", "mechanism": "direct_passthrough"},
            ],
        )
    )
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open"},
            "rules": [
                {
                    "id": "r",
                    "predicate": {
                        "reach": {
                            "kind": "model",
                            "min_count": 2,
                            "where": {"meta": {"key": "critical", "op": "is_true"}},
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    # Only one critical mart -> min_count 2 not met -> no fire.
    verdict = _engine(policy, registry, impact).evaluate(
        [_change("src", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.decision is GateDecision.ALLOW


# --- reach: matched_reach / {reach.count} precision (regression) -------------


def _exec_reach_policy():
    """A block+notify rule whose reach `where` matches only executive metabase dashboards."""
    return parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open"},
            "rules": [
                {
                    "id": "exec",
                    "predicate": {
                        "reach": {
                            "kind": "exposure",
                            "where": {
                                "all": [
                                    {"meta": {"key": "source", "op": "eq", "value": "metabase"}},
                                    {"meta": {"key": "tier", "op": "eq", "value": "executive"}},
                                ]
                            },
                            "min_count": 1,
                        }
                    },
                    "action": [
                        {"type": "block"},
                        {
                            "type": "notify",
                            "channel": "slack",
                            "target": "#data-exec-alerts",
                            "message": "breaks {reach.count} executive dashboard(s)",
                        },
                    ],
                }
            ],
        }
    )


def test_reach_matched_reach_lists_only_where_satisfying_objects():
    """The killer bug: when a reach `where` fires, matched_reach + {reach.count} must report ONLY
    the objects that actually satisfied the where — not every reached object of that kind. Here 1
    of 4 reached exposures is executive; the other three lack the meta key (UNKNOWN under
    fail_open) and must not be reported, even though the block correctly stands on the one match."""
    registry = FakeRegistry(
        exposures={
            "exec_dash": _FakeExposure(
                "exec_dash", metadata={"source": "metabase", "tier": "executive"}
            ),
            # Reached but untagged -> where is UNKNOWN_MISSING (fail_open); NOT a match.
            "ops_dash": _FakeExposure("ops_dash", metadata={}),
            "api_endpoint": _FakeExposure("api_endpoint", metadata={}),
            "tiering_report": _FakeExposure("tiering_report", metadata={}),
        }
    )
    impact = _impact(
        _resolved(
            "m",
            "c",
            exposures=[
                {"name": "exec_dash"},
                {"name": "ops_dash"},
                {"name": "api_endpoint"},
                {"name": "tiering_report"},
            ],
        )
    )
    verdict = _engine(_exec_reach_policy(), registry, impact).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    # Decision is unchanged: exactly one executive dashboard is reached -> BLOCK.
    assert verdict.blocks()
    hit = next(h for h in verdict.hits if h.rule_id == "exec")
    assert hit.matched_reach == ["exec_dash"]  # only the satisfying object, not all four
    assert verdict.notifications[0].message == "breaks 1 executive dashboard(s)"


def test_reach_matched_reach_lists_all_when_multiple_satisfy():
    """The filter must not over-prune: every object that satisfies the where is listed/counted.
    ops_dash is present-but-non-matching (tier != executive) and must be excluded too."""
    registry = FakeRegistry(
        exposures={
            "exec_a": _FakeExposure("exec_a", metadata={"source": "metabase", "tier": "executive"}),
            "exec_b": _FakeExposure("exec_b", metadata={"source": "metabase", "tier": "executive"}),
            "ops_dash": _FakeExposure("ops_dash", metadata={"source": "metabase", "tier": "ops"}),
        }
    )
    impact = _impact(
        _resolved(
            "m",
            "c",
            exposures=[{"name": "exec_a"}, {"name": "exec_b"}, {"name": "ops_dash"}],
        )
    )
    verdict = _engine(_exec_reach_policy(), registry, impact).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.blocks()
    hit = next(h for h in verdict.hits if h.rule_id == "exec")
    assert sorted(hit.matched_reach) == ["exec_a", "exec_b"]
    assert verdict.notifications[0].message == "breaks 2 executive dashboard(s)"


# --- backward-compat: provable break as one-line rule -----------------------


def test_provable_break_rule_reproduces_legacy_fail_on_tests():
    registry = FakeRegistry(model_meta={})
    impact = _impact(_unresolved("accounts", "ssn", kind="removed"))
    breaks = [
        BreakFinding(
            break_kind="break_test",
            change_model="accounts",
            change_column="ssn",
            change_kind="removed",
            test_name="not_null",
            test_unique_id="test.p.not_null_accounts_ssn",
        )
    ]
    engine = _engine(_fixture("provable_break.yml"), registry, impact, breaks=breaks)
    verdict = engine.evaluate([_change("accounts", "ssn", kind=ChangeKind.REMOVED)])
    assert verdict.blocks()
    assert verdict.hits[0].rule_id == "provable-break-blocks"


def test_provable_break_rule_silent_when_no_break():
    registry = FakeRegistry(model_meta={})
    impact = _impact(_resolved("m", "c"))
    engine = _engine(_fixture("provable_break.yml"), registry, impact, breaks=[])
    verdict = engine.evaluate([_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW


# --- aggregate scope --------------------------------------------------------


def test_aggregate_scope_any_provable_break_anywhere_blocks():
    registry = FakeRegistry(model_meta={})
    impact = _impact(_resolved("a", "x"), _resolved("b", "y"))
    breaks = [
        BreakFinding(
            break_kind="break_test",
            change_model="b",
            change_column="y",
            change_kind="removed",
            test_name="unique",
            test_unique_id="test.p.u",
        )
    ]
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "agg",
                    "scope": "aggregate",
                    "predicate": {"structural": {"fact": "provable_test_break"}},
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    engine = _engine(policy, registry, impact, breaks=breaks)
    verdict = engine.evaluate(
        [
            _change("a", "x", semantic=SemanticChangeKind.MEANING_CHANGED),
            _change("b", "y", kind=ChangeKind.REMOVED),
        ]
    )
    assert verdict.blocks()
    assert verdict.fired_rules == 1  # aggregate fires once, not per subject


# --- top-level convenience --------------------------------------------------


def test_evaluate_policy_convenience_builds_indexes():
    registry = FakeRegistry(
        model_meta={"analyst_mart": {"readable_by": ["ANALYST"]}},
        column_meta={("accounts", "ssn"): {"pii": True}},
    )
    impact = _impact(
        _resolved(
            "accounts",
            "ssn",
            models=[{"name": "analyst_mart", "mechanism": "direct_passthrough"}],
        )
    )
    verdict = evaluate_policy(
        [_change(semantic=SemanticChangeKind.MEANING_CHANGED)],
        impact,
        registry,
        _fixture("pii_allowlist.yml"),
    )
    assert verdict.blocks()
    assert ActionKind.BLOCK in verdict.hits[0].actions


# --- built-in semantic-severity knobs ---------------------------------------


def _defaults_policy(on_meaning_changed=None, on_indeterminate=None):
    defaults = {}
    if on_meaning_changed is not None:
        defaults["on_meaning_changed"] = on_meaning_changed
    if on_indeterminate is not None:
        defaults["on_indeterminate"] = on_indeterminate
    return parse_policy({"version": 1, "defaults": defaults, "rules": []})


def test_semantic_knobs_block_meaning_changed_warn_indeterminate():
    # No hand-written rules: the gate is driven entirely by the two knobs, tuned independently.
    policy = _fixture("semantic_severity_defaults.yml")
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate(
        [
            _change("a", "x", semantic=SemanticChangeKind.MEANING_CHANGED),
            _change("b", "y", semantic=SemanticChangeKind.INDETERMINATE),
        ]
    )
    assert verdict.decision is GateDecision.BLOCK  # most-severe-wins across the two
    ids = {h.rule_id: h.decision for h in verdict.hits}
    assert ids["builtin:on_meaning_changed"] is GateDecision.BLOCK
    assert ids["builtin:on_indeterminate"] is GateDecision.WARN


def test_semantic_knobs_are_independent_warn_only():
    # Warn on meaning_changed, say nothing about indeterminate: an indeterminate change alone
    # then contributes nothing from this axis (independent knobs).
    policy = _defaults_policy(on_meaning_changed="warn")
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.INDETERMINATE)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.hits == []


def test_semantic_knobs_unset_by_default_contribute_nothing():
    policy = _defaults_policy()  # neither knob set
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.hits == []


def test_semantic_knobs_allow_value_adds_no_hit():
    policy = _defaults_policy(on_meaning_changed="allow")
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.hits == []


def test_semantic_knobs_ignore_structural_changes():
    # A removed/added column carries no semantic; the knobs must not fire on it.
    policy = _fixture("semantic_severity_defaults.yml")
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate([_change("a", "x", kind=ChangeKind.REMOVED)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.hits == []


def test_semantic_knobs_ignore_equivalent():
    policy = _fixture("semantic_severity_defaults.yml")
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.EQUIVALENT)])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.hits == []


def test_semantic_knobs_fold_into_user_rules_most_severe_wins():
    # A user rule warns; the meaning_changed knob blocks; the gate is BLOCK.
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_meaning_changed": "block"},
            "rules": [
                {
                    "id": "warn-any-logic",
                    "predicate": {
                        "change": {"field": "kind", "op": "eq", "value": "logic_changed"}
                    },
                    "action": [{"type": "warn"}],
                }
            ],
        }
    )
    engine = _engine(policy, FakeRegistry(), _impact())
    verdict = engine.evaluate([_change(semantic=SemanticChangeKind.MEANING_CHANGED)])
    assert verdict.decision is GateDecision.BLOCK
    assert any(h.rule_id == "builtin:on_meaning_changed" for h in verdict.hits)
    assert any(h.rule_id == "warn-any-logic" for h in verdict.hits)


# --- override caps -------------------------------------------------------

from parrant.lineage.policy import (
    applied_policy_overrides,
    ineffective_policy_overrides,
)
from parrant.models.schema import OverrideDirective, OverrideVerb


def _ov(verb, column, reason="ack"):
    return OverrideDirective(verb=verb, column=column, reason=reason, scope="column", source_line=2)


def _break(model="orders", column="customer_id"):
    return BreakFinding(
        break_kind="break_test",
        change_model=model,
        change_column=column,
        change_kind="removed",
        test_name="not_null",
        test_unique_id="test.pkg.x",
    )


def test_allow_break_caps_provable_break_block_to_warn():
    policy = _fixture("provable_break.yml")
    change = ColumnChange(
        "orders",
        "customer_id",
        ChangeKind.REMOVED,
        override=_ov(OverrideVerb.ALLOW_BREAK, "customer_id"),
    )
    engine = _engine(policy, FakeRegistry(), _impact(), breaks=[_break()])
    verdict = engine.evaluate([change])
    assert verdict.decision is GateDecision.WARN
    capped = [h for h in verdict.hits if h.overridden]
    assert len(capped) == 1
    assert capped[0].original_decision is GateDecision.BLOCK
    assert capped[0].override_reason == "ack"


def test_allow_change_cannot_cap_provable_break_block():
    # HEADLINE FAIL-SAFE for the policy path: the soft verb must NOT silence a break block.
    policy = _fixture("provable_break.yml")
    change = ColumnChange(
        "orders",
        "customer_id",
        ChangeKind.REMOVED,
        override=_ov(OverrideVerb.ALLOW_CHANGE, "customer_id"),
    )
    engine = _engine(policy, FakeRegistry(), _impact(), breaks=[_break()])
    verdict = engine.evaluate([change])
    assert verdict.decision is GateDecision.BLOCK
    assert not any(h.overridden for h in verdict.hits)


def test_allow_change_caps_non_break_block_to_allow():
    # A block from a NON-break rule (removed-column governance) is soft-capped by allow-change.
    policy = _fixture("block_on_removed.yml")
    change = ColumnChange(
        "orders",
        "customer_id",
        ChangeKind.REMOVED,
        override=_ov(OverrideVerb.ALLOW_CHANGE, "customer_id"),
    )
    engine = _engine(policy, FakeRegistry(), _impact(), breaks=[])  # no provable break
    verdict = engine.evaluate([change])
    assert verdict.decision is GateDecision.ALLOW
    assert any(h.overridden and h.original_decision is GateDecision.BLOCK for h in verdict.hits)


def test_applied_policy_overrides_shape_matches_default_gate():
    policy = _fixture("block_on_removed.yml")
    change = ColumnChange(
        "orders",
        "customer_id",
        ChangeKind.REMOVED,
        override=_ov(OverrideVerb.ALLOW_CHANGE, "customer_id"),
    )
    verdict = _engine(policy, FakeRegistry(), _impact(), breaks=[]).evaluate([change])
    records = applied_policy_overrides(verdict, [change])
    assert len(records) == 1
    r = records[0]
    assert {
        "model",
        "column",
        "verb",
        "reason",
        "downgraded_from",
        "downgraded_to",
        "source_line",
        "scope",
    } <= set(r.keys())
    assert r["verb"] == "allow-change"
    assert r["downgraded_from"] == "block"
    assert r["downgraded_to"] == "allow"
    assert r["source_line"] == 2


def test_ineffective_policy_overrides_surfaces_allow_change_on_break():
    policy = _fixture("provable_break.yml")
    change = ColumnChange(
        "orders",
        "customer_id",
        ChangeKind.REMOVED,
        override=_ov(OverrideVerb.ALLOW_CHANGE, "customer_id"),
    )
    verdict = _engine(policy, FakeRegistry(), _impact(), breaks=[_break()]).evaluate([change])
    # It capped nothing (block stayed) -> not in applied, but IS in ineffective with a hint.
    assert applied_policy_overrides(verdict, [change]) == []
    ineff = ineffective_policy_overrides(verdict, [change], breaks=[_break()])
    assert len(ineff) == 1
    assert "allow-break" in ineff[0]["hint"]


# --- suppressed-unknown telemetry + warn-on-unknown knob (issue #124) ---------


def _warn_pii_policy(defaults=None):
    """A warn-only `meta.pii eq true` rule — UNKNOWN on any untagged column (the #124 repro)."""
    doc = {
        "version": 1,
        "defaults": defaults or {"on_missing_meta": "fail_closed"},
        "rules": [
            {
                "id": "warn-pii",
                "predicate": {"meta": {"key": "pii", "op": "eq", "value": True}},
                "action": [{"type": "warn"}],
            }
        ],
    }
    return parse_policy(doc)


def test_warn_rule_unknown_under_fail_closed_surfaces_unproven():
    """Issue #124: a warn rule whose predicate stays UNKNOWN under fail_closed is suppressed
    (no spurious warn) but the suppression MUST surface as an unproven record — otherwise an
    all-warn pilot policy systematically understates what block mode will do."""
    registry = FakeRegistry(model_meta={"m": {}})  # no `pii` meta anywhere
    impact = _impact(_resolved("m", "c"))
    verdict = _engine(_warn_pii_policy(), registry, impact).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    # decision surface unchanged: still ALLOW, still zero fired rules.
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0
    assert verdict.hits == []
    # ...but the suppressed firing is now visible.
    assert verdict.unproven_count == 1
    (record,) = verdict.unproven
    assert record.rule_id == "warn-pii"
    assert record.change_model == "m"
    assert record.change_column == "c"
    assert record.unknown_cause == "missing"
    assert "meta.pii" in record.unknown_leaves


def test_unproven_error_cause_from_type_mismatch():
    """A warn rule suppressed by an UNKNOWN_ERROR under `on_error: fail_closed` records the
    error cause, mirroring the missing/error split of the fired_on_unknown marker."""
    policy = parse_policy(
        {
            "version": 1,
            "defaults": {"on_missing_meta": "fail_open", "on_error": "fail_closed"},
            "rules": [
                {
                    "id": "roles-subset",
                    "predicate": {"meta": {"key": "roles", "op": "subset_of", "value": ["A"]}},
                    "action": [{"type": "warn"}],
                }
            ],
        }
    )
    registry = FakeRegistry(model_meta={"m": {"roles": "ADMIN"}})  # scalar -> type mismatch
    verdict = _engine(policy, registry, _impact(_resolved("m", "c"))).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.unproven_count == 1
    assert verdict.unproven[0].unknown_cause == "error"
    assert "meta.roles" in verdict.unproven[0].unknown_leaves


def test_unproven_not_recorded_under_fail_open_or_skip():
    """Only a suppressed *fail_closed* firing is unproven: fail_open / skip resolve the UNKNOWN
    by design and keep their existing surfaces (nothing / skipped_missing_meta)."""
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    change = _change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)

    open_verdict = _engine(
        _warn_pii_policy({"on_missing_meta": "fail_open"}), registry, impact
    ).evaluate([change])
    assert open_verdict.unproven_count == 0
    assert open_verdict.unproven == []

    skip_verdict = _engine(
        _warn_pii_policy({"on_missing_meta": "skip"}), registry, impact
    ).evaluate([change])
    assert skip_verdict.unproven_count == 0
    assert skip_verdict.skipped_missing_meta == 1


def test_unproven_not_recorded_for_blocking_rule():
    """A blocking rule under fail_closed FIRES on UNKNOWN (existing behavior) — it is a hit
    marked fired_on_unknown, never an unproven record (no double-count)."""
    verdict = _fire(_one_meta_rule("pii", "eq", True), {})
    assert verdict.blocks()
    assert verdict.unproven_count == 0
    assert verdict.unproven == []


def test_unproven_not_recorded_on_proven_false():
    """A predicate that resolves to a proven FALSE is not unproven — nothing was suppressed."""
    registry = FakeRegistry(column_meta={("m", "c"): {"pii": False}})
    verdict = _engine(_warn_pii_policy(), registry, _impact(_resolved("m", "c"))).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.unproven_count == 0
    assert verdict.unproven == []


def test_warn_rules_fire_on_unknown_knob_fires_warn_hit():
    """The opt-in knob (defaults.warn_rules_fire_on_unknown) makes fail_closed apply to
    non-blocking rules too: the warn rule fires on UNKNOWN as a normal warn hit marked
    fired_on_unknown, and nothing lands in unproven."""
    policy = _warn_pii_policy(
        {"on_missing_meta": "fail_closed", "warn_rules_fire_on_unknown": True}
    )
    registry = FakeRegistry(model_meta={"m": {}})
    verdict = _engine(policy, registry, _impact(_resolved("m", "c"))).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert verdict.decision is GateDecision.WARN
    assert verdict.fired_rules == 1
    (hit,) = verdict.hits
    assert hit.rule_id == "warn-pii"
    assert hit.decision is GateDecision.WARN
    assert hit.fired_on_unknown is True
    assert hit.unknown_cause == "missing"
    assert verdict.unproven_count == 0
    assert verdict.unproven == []


def test_warn_rules_fire_on_unknown_knob_never_escalates_to_block():
    """The knob only fires the rule at its declared severity — the gate decision can become
    WARN, never BLOCK, so `--fail-on policy` exit behavior is untouched."""
    policy = _warn_pii_policy(
        {"on_missing_meta": "fail_closed", "warn_rules_fire_on_unknown": True}
    )
    registry = FakeRegistry(model_meta={"m": {}})
    verdict = _engine(policy, registry, _impact(_resolved("m", "c"))).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    assert not verdict.blocks()


def test_knob_off_verdict_identical_except_unproven():
    """Default-off regression lock: apart from the additive unproven telemetry, the verdict is
    identical to the pre-#124 one (decision, hits, counters)."""
    registry = FakeRegistry(model_meta={"m": {}})
    impact = _impact(_resolved("m", "c"))
    verdict = _engine(_warn_pii_policy(), registry, impact).evaluate(
        [_change("m", "c", semantic=SemanticChangeKind.MEANING_CHANGED)]
    )
    dumped = verdict.model_dump(mode="json")
    dumped.pop("unproven", None)
    dumped.pop("unproven_count", None)
    assert dumped == {
        "decision": "allow",
        "hits": [],
        "build_set": [],
        "test_set": [],
        "notifications": [],
        "evaluated_rules": 1,
        "fired_rules": 0,
        "unresolved_reach_count": 0,
        "skipped_missing_meta": 0,
    }
