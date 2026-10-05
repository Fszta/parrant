"""Unit tests for the ``policy init`` scaffold: the safe-by-construction YAML emitter and the
pure scan helpers, exercised with synthetic :class:`PolicyInitScan` inputs (no artifacts).

Tests that need the built bundled project (``scan_project`` over real artifacts and the
``run_policy_init`` end-to-end orchestration) live in ``tests/integration/test_policy_init.py``,
which builds the project via the ``dbt_artifacts`` fixture — the unit tier stays artifact-free.
"""

import yaml

from parrant.lineage.policy import parse_policy
from parrant.lineage.policy_init import _flatten_meta_keys, _has_select_grant, emit_policy_yaml
from parrant.models.schema import MetaKeyCoverage, PolicyInitScan

# --- pure scan helpers -------------------------------------------------------


def test_flatten_meta_keys_dotted_nesting():
    keys = _flatten_meta_keys({"critical": True, "governance": {"tier": "gold", "owner": "x"}})
    assert set(keys) == {"critical", "governance.tier", "governance.owner"}


def test_flatten_meta_keys_empty():
    assert _flatten_meta_keys({}) == []


def test_has_select_grant_detects_real_grant():
    assert _has_select_grant({"grants": {"select": ["pii_reader", "analyst"]}}) is True
    # A non-list select (raw bare string) still counts as present — the engine surfaces it raw.
    assert _has_select_grant({"grants": {"select": "pii_reader"}}) is True


def test_has_select_grant_false_when_absent_empty_or_null():
    assert _has_select_grant({}) is False
    assert _has_select_grant({"materialized": "view"}) is False  # no grants block
    assert _has_select_grant({"grants": {}}) is False  # grants but no select
    assert _has_select_grant({"grants": {"select": []}}) is False  # empty reader set = no grant
    assert _has_select_grant({"grants": {"select": None}}) is False  # null = no grant


# --- MetaKeyCoverage.pct -----------------------------------------------------


def test_pct_guards_zero_total():
    assert MetaKeyCoverage(key="k", n_present=0, total=0).pct == 0


def test_pct_rounds():
    assert MetaKeyCoverage(key="critical", n_present=95, total=1163).pct == 8


# --- emitter: provable-break-block commented when no tests -------------------


def test_provable_break_block_commented_when_no_tests():
    scan = PolicyInitScan(total_models=5, column_test_count=0, exposure_count=1)
    text = emit_policy_yaml(scan)
    # The block rule must NOT be an armed list item...
    assert "\n  - id: provable-break-block" not in text
    # ...it is present only as a commented, withheld template with the honest reason.
    assert "provable-break-block — NOT emitted" in text
    assert "no column-targeted generic tests" in text
    # exposure-guard is still armed (exposures present).
    assert "\n  - id: exposure-guard" in text
    parse_policy(yaml.safe_load(text))  # must still be valid


# --- emitter: meta templates (synthetic scan) --------------------------------


def test_meta_template_commented_with_coverage_and_presence_op():
    scan = PolicyInitScan(
        total_models=1163,
        total_columns=1,
        column_test_count=10,
        exposure_count=0,
        model_meta_keys=[MetaKeyCoverage(key="critical", n_present=95, total=1163)],
    )
    text = emit_policy_yaml(scan)
    # Exact coverage string from the scan (not a placeholder).
    assert "meta.critical present on 95/1163 models (8%)" in text
    # A presence operator (is_true), never a value comparison, and always commented (never armed).
    assert "op: is_true" in text
    assert "\n  - id: reach-critical-model" not in text  # not armed
    assert "  # - id: reach-critical-model" in text  # commented template
    # Still valid + never fail_open.
    assert "fail_open" not in text
    parse_policy(yaml.safe_load(text))


def test_meta_template_dotted_key_slug():
    scan = PolicyInitScan(
        total_models=100,
        column_test_count=1,
        exposure_count=1,
        model_meta_keys=[MetaKeyCoverage(key="governance.tier", n_present=17, total=100)],
    )
    text = emit_policy_yaml(scan)
    assert "meta.governance.tier present on 17/100 models (17%)" in text
    # The dotted key survives verbatim in the where.meta.key (matches the engine's dotted lookup).
    assert "key: governance.tier, op: is_true" in text
    # The rule id is a sanitized slug (dots -> dashes).
    assert "reach-governance-tier-model" in text


def test_meta_template_cap_notes_omitted_keys():
    rows = [MetaKeyCoverage(key=f"k{i:02d}", n_present=20 - i, total=100) for i in range(20)]
    scan = PolicyInitScan(
        total_models=100, column_test_count=1, exposure_count=1, model_meta_keys=rows
    )
    text = emit_policy_yaml(scan)
    assert "more model-meta key(s) not shown" in text


# --- emitter: config-axis (PII over-grant) template --------------------------


def test_config_template_commented_with_coverage_when_grants_present():
    scan = PolicyInitScan(
        total_models=40,
        column_test_count=1,
        exposure_count=1,
        models_with_grants=10,
    )
    text = emit_policy_yaml(scan)
    # Real coverage from the scan (10/40 = 25%), never a placeholder.
    assert "`config.grants.select` declared on 10/40 models (25%)" in text
    # Commented (never armed), warn-first, composing inferred_meta.pii + config.grants.select.
    assert "  # - id: pii-over-grant-guard" in text
    assert "\n  - id: pii-over-grant-guard" not in text  # not armed
    assert "config: { key: grants.select, op: not_subset_of, value: [pii_reader] }" in text
    assert "inferred_meta: { key: pii, op: is_true }" in text
    assert "fail_open" not in text
    parse_policy(yaml.safe_load(text))  # still valid


def test_config_template_absent_when_no_grants():
    scan = PolicyInitScan(total_models=40, column_test_count=1, exposure_count=1)
    text = emit_policy_yaml(scan)
    assert "pii-over-grant-guard" not in text
    assert "config.grants.select" not in text


# --- emitter: honest empty (no tests, no exposures, no meta) -----------------


def test_emit_honest_empty_when_no_signal():
    scan = PolicyInitScan(total_models=3, total_columns=9)
    text = emit_policy_yaml(scan)
    assert "rules: []" in text
    assert "Nothing could be safely auto-enabled" in text
    assert "fail_open" not in text
    # An honest-empty policy must still parse and carry zero rules (does not rage-block).
    policy = parse_policy(yaml.safe_load(text))
    assert policy.rules == []
