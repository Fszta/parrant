"""Tests for the generic ``config`` predicate axis.

Covers ``MetaIndex.model_config`` / ``subject_config`` and the ``config.<dotted.key>`` predicate
leaf, which resolves a DOTTED key against a model's resolved dbt ``node.config`` (``grants.select``,
``materialized``, ``tags`` …) — mirroring the ``meta`` axis but with config-specific missing-path
semantics:

  * SET operators (``subset_of`` / ``not_subset_of`` / ``intersects`` / ``superset_of``): a missing
    dotted path is the proven EMPTY SET ``[]`` (present, NOT unknown) — the target
    "grants not over an allowlist" use case, where a model with no grants must NOT fire;
  * SCALAR operators (``eq`` / ``ne`` / numeric …): a missing path is UNKNOWN_MISSING, routed to
    ``on_missing_meta`` exactly like a missing ``meta`` key;
  * dotted traversal into nested config, and composition with ``inferred_meta`` for the offline
    PII-not-over-granted rule.

Mirrors the hand-written harness in ``test_policy_engine.py`` (FakeRegistry + by_change impact
dicts), extended with a ``get_model_config`` accessor and a ``get_column_lineage`` accessor (for the
combined ``inferred_meta`` + ``config`` case).
"""

import os

import pytest

from parrant.lineage.changeset import ChangeKind, ColumnChange
from parrant.lineage.policy import (
    MetaIndex,
    PolicyConfigError,
    PolicyEngine,
    build_impact_view,
    load_policy,
    parse_policy,
)
from parrant.models.schema import (
    ColumnLineage,
    GateDecision,
    SemanticChangeKind,
)

# --- fakes ------------------------------------------------------------------


class FakeRegistry:
    """Registry stand-in exposing the model ``config`` accessor (+ optional meta / lineage).

    ``model_config`` maps a model name to its resolved dbt ``node.config`` dict; a missing entry
    -> ``{}`` (no config). ``column_meta`` / ``column_lineage`` mirror the other harnesses so the
    combined ``inferred_meta`` + ``config`` case has a DAG and seeds to fold.
    """

    def __init__(self, model_config=None, column_meta=None, model_meta=None, column_lineage=None):
        self._model_config = model_config or {}
        self._column_meta = column_meta or {}
        self._model_meta = model_meta or {}
        self._column_lineage = column_lineage or {}

    def get_model_config(self, model):
        return dict(self._model_config.get(model, {}))

    def get_model_dbt_meta(self, model):
        return dict(self._model_meta.get(model, {}))

    def get_column_dbt_meta(self, model, column):
        return dict(self._column_meta.get((model, column), {}))

    def get_column_lineage(self, model, column):
        refs = self._column_lineage.get((model, column))
        if refs is None:
            return []
        return [ColumnLineage(source_columns=set(refs), transformation_type="renamed")]


def _impact(*entries):
    return {"by_change": list(entries)}


def _resolved(model, column, kind="logic_changed", models=None, exposures=None):
    return {
        "model": model,
        "column": column,
        "kind": kind,
        "resolved": True,
        "reached_models": models or [],
        "reached_columns": [],
        "reached_exposures": exposures or [],
    }


def _engine(policy, registry, impact=None):
    return PolicyEngine(policy, MetaIndex(registry), build_impact_view(impact or _impact()), [])


def _change(model="customers", column="email", kind=ChangeKind.LOGIC_CHANGED):
    return ColumnChange(model, column, kind, semantic=SemanticChangeKind.MEANING_CHANGED)


def _config_policy(key, op, value=None, action="block", defaults=None):
    predicate = {"config": {"key": key, "op": op}}
    if value is not None:
        predicate["config"]["value"] = value
    policy = {
        "version": 1,
        "rules": [{"id": "cfg", "predicate": predicate, "action": [{"type": action}]}],
    }
    if defaults is not None:
        policy["defaults"] = defaults
    return parse_policy(policy)


# --- SET operator: grants.select not_subset_of [allowlist] (the target case) ------


def test_grants_subset_of_allowlist_does_not_fire():
    """grants {loader} ⊆ [loader, transformer] -> not_subset_of is FALSE -> no fire."""
    registry = FakeRegistry(model_config={"customers": {"grants": {"select": ["loader"]}}})
    policy = _config_policy("grants.select", "not_subset_of", ["loader", "transformer"])
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0


def test_grants_outside_allowlist_fires():
    """grants {loader, reporter} ⊄ [loader, transformer] -> not_subset_of is TRUE -> block."""
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["loader", "reporter"]}}}
    )
    policy = _config_policy("grants.select", "not_subset_of", ["loader", "transformer"])
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.blocks()
    assert verdict.fired_rules == 1


def test_missing_grants_is_empty_set_not_unknown():
    """No grants declared -> missing path resolves to [] -> [] ⊄ X == FALSE -> no fire.

    This is the key case: a model that grants to nobody cannot over-expose, so it must NOT
    block even under the default fail_closed posture (the missing path is a PROVEN empty set,
    not an UNKNOWN that would route to on_missing_meta)."""
    registry = FakeRegistry(model_config={"customers": {"materialized": "table"}})  # no grants key
    policy = _config_policy(
        "grants.select", "not_subset_of", ["loader", "transformer"]
    )  # blocking, fail_closed
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0
    assert verdict.skipped_missing_meta == 0  # never routed through the missing-meta knob


def test_intersects_and_subset_of_sanity():
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["loader", "reporter"]}}}
    )
    # intersects [reporter] -> shares reporter -> TRUE
    v_int = _engine(_config_policy("grants.select", "intersects", ["reporter"]), registry).evaluate(
        [_change()]
    )
    assert v_int.blocks()
    # subset_of [loader, transformer, reporter] -> {loader, reporter} ⊆ -> TRUE
    v_sub = _engine(
        _config_policy("grants.select", "subset_of", ["loader", "transformer", "reporter"]),
        registry,
    ).evaluate([_change()])
    assert v_sub.blocks()
    # missing path + intersects -> [] shares nothing -> FALSE (empty set, no fire)
    empty = FakeRegistry(model_config={"customers": {}})
    v_missing = _engine(
        _config_policy("grants.select", "intersects", ["reporter"]), empty
    ).evaluate([_change()])
    assert v_missing.decision is GateDecision.ALLOW


# --- SCALAR operator: materialized eq incremental --------------------------


def test_scalar_eq_matches():
    registry = FakeRegistry(model_config={"customers": {"materialized": "incremental"}})
    verdict = _engine(_config_policy("materialized", "eq", "incremental"), registry).evaluate(
        [_change()]
    )
    assert verdict.blocks()


def test_scalar_eq_non_match_does_not_fire():
    registry = FakeRegistry(model_config={"customers": {"materialized": "table"}})
    verdict = _engine(_config_policy("materialized", "eq", "incremental"), registry).evaluate(
        [_change()]
    )
    assert verdict.decision is GateDecision.ALLOW


def test_scalar_missing_is_unknown_and_routes_to_on_missing_meta():
    """Missing scalar path -> UNKNOWN_MISSING -> on_missing_meta.

    Under fail_closed (default) a *blocking* rule fires on UNKNOWN; under skip it is dropped and
    counted in skipped_missing_meta. This proves the scalar side behaves like a missing meta key,
    NOT like the set-op empty set."""
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["loader"]}}}
    )  # no materialized
    # fail_closed (default) + blocking -> fires on UNKNOWN
    closed = _engine(_config_policy("materialized", "eq", "incremental"), registry).evaluate(
        [_change()]
    )
    assert closed.blocks()
    assert closed.hits[0].fired_on_unknown is True
    assert closed.hits[0].unknown_cause == "missing"
    # skip -> the rule drops for the subject and is counted
    skip_policy = _config_policy(
        "materialized", "eq", "incremental", defaults={"on_missing_meta": "skip"}
    )
    skipped = _engine(skip_policy, registry).evaluate([_change()])
    assert skipped.decision is GateDecision.ALLOW
    assert skipped.skipped_missing_meta == 1


# --- dotted traversal + tags -----------------------------------------------


def test_dotted_traversal_into_nested_config():
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["loader"], "insert": ["transformer"]}}}
    )
    # grants.insert is a distinct nested path
    verdict = _engine(
        _config_policy("grants.insert", "intersects", ["transformer"]), registry
    ).evaluate([_change()])
    assert verdict.blocks()


def test_tags_intersects():
    registry = FakeRegistry(model_config={"customers": {"tags": ["nightly", "finance"]}})
    hit = _engine(_config_policy("tags", "intersects", ["finance"]), registry).evaluate([_change()])
    assert hit.blocks()
    miss = _engine(_config_policy("tags", "intersects", ["hourly"]), registry).evaluate([_change()])
    assert miss.decision is GateDecision.ALLOW


# --- combined inferred_meta + config: the PII-not-over-granted rule ---------


def _pii_grants_policy():
    """all: [inferred_meta.pii is_true, config.grants.select not_subset_of [pii_reader]]."""
    return parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "pii-not-over-granted",
                    "predicate": {
                        "all": [
                            {"inferred_meta": {"key": "pii", "op": "is_true"}},
                            {
                                "config": {
                                    "key": "grants.select",
                                    "op": "not_subset_of",
                                    "value": ["pii_reader"],
                                }
                            },
                        ]
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )


def test_pii_over_granted_blocks():
    """PII (own meta) + grants to an analyst role outside the allowlist -> block."""
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["pii_reader", "analyst"]}}},
        column_meta={("customers", "email"): {"pii": True}},
    )
    verdict = _engine(_pii_grants_policy(), registry).evaluate([_change()])
    assert verdict.blocks()


def test_pii_granted_only_to_allowlist_does_not_block():
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["pii_reader"]}}},
        column_meta={("customers", "email"): {"pii": True}},
    )
    verdict = _engine(_pii_grants_policy(), registry).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW


def test_non_pii_over_granted_does_not_block():
    """Not PII -> the inferred_meta leaf is FALSE -> the AND short-circuits, no fire even though
    the model over-grants."""
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["analyst"]}}},
        column_meta={("customers", "created_at"): {"pii": False}},
    )
    verdict = _engine(_pii_grants_policy(), registry).evaluate([_change(column="created_at")])
    assert verdict.decision is GateDecision.ALLOW


def test_pii_but_no_grants_declared_does_not_block():
    """PII column on a model with NO grants -> the config leaf is [] ⊄ X == FALSE -> no fire.

    A PII model that grants to nobody is not over-exposed; the empty-set default keeps the rule
    from firing on it (the generic, correct default)."""
    registry = FakeRegistry(
        model_config={"customers": {"materialized": "table"}},  # no grants
        column_meta={("customers", "email"): {"pii": True}},
    )
    verdict = _engine(_pii_grants_policy(), registry).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW


# --- reach.where on config -------------------------------------------------


def test_reach_where_matches_config_on_reached_model():
    """A reach.where can match config on a reached model (grants of the downstream consumer)."""
    registry = FakeRegistry(
        model_config={"orders": {"grants": {"select": ["analyst"]}}},
    )
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "reaches-over-granted",
                    "predicate": {
                        "reach": {
                            "kind": "model",
                            "where": {
                                "config": {
                                    "key": "grants.select",
                                    "op": "not_subset_of",
                                    "value": ["pii_reader"],
                                }
                            },
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    impact = _impact(
        _resolved(
            "customers",
            "email",
            models=[{"name": "orders", "mechanism": "direct_passthrough"}],
        )
    )
    verdict = _engine(policy, registry, impact).evaluate([_change()])
    assert verdict.blocks()
    assert "orders" in verdict.hits[0].matched_reach


def test_config_axis_leaves_meta_untouched():
    """Regression: a ``config`` leaf never reads ``meta`` and vice-versa (disjoint axes)."""
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["loader"]}}},
        # a meta key that happens to collide on name but holds a different value
        model_meta={"customers": {"grants": {"select": ["meta_only_role"]}}},
    )
    # config reads node.config (loader), not the meta collision (meta_only_role)
    verdict = _engine(_config_policy("grants.select", "intersects", ["loader"]), registry).evaluate(
        [_change()]
    )
    assert verdict.blocks()
    # a meta rule on the same key reads meta (meta_only_role), proving they are independent
    meta_policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "meta",
                    "predicate": {
                        "meta": {
                            "key": "grants.select",
                            "op": "intersects",
                            "value": ["meta_only_role"],
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    assert _engine(meta_policy, registry).evaluate([_change()]).blocks()


# --- the shipped example fixture -------------------------------------------

_POLICY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "resources", "policies")


def test_shipped_pii_grants_allowlist_fixture_blocks_over_grant():
    """The shipped ``pii_grants_allowlist.yml`` example loads and fires end-to-end."""
    policy = load_policy(os.path.join(_POLICY_DIR, "pii_grants_allowlist.yml"))
    registry = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["pii_reader", "analyst"]}}},
        column_meta={("customers", "email"): {"pii": True}},
    )
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.blocks()
    assert verdict.notifications[0].target == "#data-governance"


# --- hardening: confirmed-safe edge paths ----------------------------------


def test_set_op_against_a_scalar_config_value_is_error_and_fails_closed():
    """config.grants.select is a STRING (not a list) fed to a SET operator -> UNKNOWN_ERROR ->
    on_error (fail_closed default) -> a blocking rule FIRES (fails safe, never a false pass).

    A present-but-wrong-shaped value is a genuine evaluation error, NOT the empty-set/absent
    default — that mapping is reserved for absent / null, never for a real scalar in the slot."""
    registry = FakeRegistry(model_config={"customers": {"grants": {"select": "loader"}}})
    policy = _config_policy("grants.select", "not_subset_of", ["loader", "transformer"])
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.blocks()
    assert verdict.hits[0].fired_on_unknown is True
    assert verdict.hits[0].unknown_cause == "error"


def test_intermediate_missing_hop_is_empty_set_not_unknown():
    """``grants`` present but ``select`` absent -> the dotted path is still missing at the last
    hop -> set op resolves to the EMPTY SET -> not_subset_of == FALSE -> no fire."""
    registry = FakeRegistry(model_config={"customers": {"grants": {"insert": ["loader"]}}})
    policy = _config_policy("grants.select", "not_subset_of", ["loader", "transformer"])
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0
    assert verdict.skipped_missing_meta == 0


def test_superset_of_positive_and_missing():
    """``superset_of`` (in _SET_OPERATORS) sanity: a proven superset fires; a missing path is the
    EMPTY SET, and [] is a superset of nothing but the empty set -> no fire."""
    granted = FakeRegistry(
        model_config={"customers": {"grants": {"select": ["loader", "transformer", "reporter"]}}}
    )
    # {loader, transformer, reporter} ⊇ [loader, reporter] -> TRUE
    v_pos = _engine(
        _config_policy("grants.select", "superset_of", ["loader", "reporter"]), granted
    ).evaluate([_change()])
    assert v_pos.blocks()
    # missing path -> [] ⊇ [loader] is FALSE -> no fire
    empty = FakeRegistry(model_config={"customers": {}})
    v_missing = _engine(_config_policy("grants.select", "superset_of", ["loader"]), empty).evaluate(
        [_change()]
    )
    assert v_missing.decision is GateDecision.ALLOW


def test_reach_where_config_on_reached_exposure_does_not_match():
    """A ``reach.where`` with a ``config`` leaf against a reached EXPOSURE (a non-model with no
    dbt config) resolves present=False -> the set-op empty-set default -> FALSE, so it never
    spuriously matches. The rule stays ALLOW."""
    registry = FakeRegistry()
    policy = parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": "reaches-over-granted-exposure",
                    "predicate": {
                        "reach": {
                            "kind": "exposure",
                            "where": {
                                "config": {
                                    "key": "grants.select",
                                    "op": "intersects",
                                    "value": ["reporter"],
                                }
                            },
                        }
                    },
                    "action": [{"type": "block"}],
                }
            ],
        }
    )
    impact = _impact(
        _resolved(
            "customers",
            "email",
            exposures=[{"name": "exec_dashboard"}],
        )
    )
    verdict = _engine(policy, registry, impact).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0


def test_predicate_with_two_axes_is_rejected():
    """The one-of validator rejects a leaf that sets ``config`` AND ``meta`` at the same level."""
    with pytest.raises(PolicyConfigError):
        parse_policy(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "two-axes",
                        "predicate": {
                            "config": {"key": "materialized", "op": "eq", "value": "table"},
                            "meta": {"key": "pii", "op": "is_true"},
                        },
                        "action": [{"type": "block"}],
                    }
                ],
            }
        )


# --- consistency: a present-but-null set value is the empty set ------------


def test_null_config_set_value_is_empty_set_no_fire():
    """``grants.select: null`` (explicitly nulled readers) -> set op treats None like absent ->
    EMPTY SET -> not_subset_of == FALSE -> no fire. Null readers = no readers = not exposed."""
    registry = FakeRegistry(model_config={"customers": {"grants": {"select": None}}})
    policy = _config_policy("grants.select", "not_subset_of", ["loader", "transformer"])
    verdict = _engine(policy, registry).evaluate([_change()])
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0
    assert verdict.skipped_missing_meta == 0


def test_null_config_scalar_value_is_unchanged():
    """The null->empty-set mapping is SET-operator only: a scalar op sees a present ``None`` value
    and evaluates it normally (None != 'incremental' -> FALSE), NOT as an empty set or UNKNOWN."""
    registry = FakeRegistry(model_config={"customers": {"materialized": None}})
    verdict = _engine(_config_policy("materialized", "eq", "incremental"), registry).evaluate(
        [_change()]
    )
    assert verdict.decision is GateDecision.ALLOW
    assert verdict.fired_rules == 0
    # present value -> NOT routed through the missing-meta knob (it is a concrete FALSE)
    assert verdict.skipped_missing_meta == 0
