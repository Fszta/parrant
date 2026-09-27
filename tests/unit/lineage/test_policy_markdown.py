"""Unit tests for the policy-verdict Markdown section.

The section renders ONLY when ``report['policy_verdict']`` is present, groups fired rules by
decision (block first), surfaces the selective build/test sets and notify intents, and never
re-lists the downstream blast radius (the impact section owns that).
"""

from parrant.lineage.display.markdown import render_changeset_markdown

_BASE_REPORT = {
    "changeset": {"total_changes": 1, "by_kind": {"logic_changed": 1}},
    "summary": {"affected_models": 1, "affected_columns": 1, "affected_exposures": 0},
    "by_change": [{"model": "stg_accounts", "column": "account_id", "kind": "logic_changed"}],
    "affected_columns": [],
    "affected_exposures": [],
}


def _report_with_policy(policy_verdict):
    report = dict(_BASE_REPORT)
    report["policy_verdict"] = policy_verdict
    return report


def test_no_policy_verdict_renders_no_policy_section():
    md = render_changeset_markdown(dict(_BASE_REPORT))
    assert "Policy verdict" not in md


def test_block_verdict_renders_section_block_first():
    verdict = {
        "decision": "block",
        "hits": [
            {
                "rule_id": "warn-rule",
                "decision": "warn",
                "change_model": "stg_x",
                "change_column": "c",
                "matched_reach": [],
                "actions": ["warn"],
            },
            {
                "rule_id": "pii-outside-allowlist",
                "decision": "block",
                "change_model": "stg_accounts",
                "change_column": "account_id",
                "matched_reach": ["mart_users", "mart_kyc"],
                "actions": ["block", "notify"],
            },
        ],
        "build_set": ["mart_users"],
        "test_set": ["mart_kyc"],
        "notifications": [
            {"channel": "slack", "target": "#data-governance", "message": "PII reaches reader"}
        ],
        "unresolved_reach_count": 0,
        "skipped_missing_meta": 0,
    }
    md = render_changeset_markdown(_report_with_policy(verdict))

    assert "Policy verdict — BLOCK" in md
    # Block band appears before the warn band.
    assert md.index("Block (1)") < md.index("Warn (1)")
    # Subject + matched reach names surface (by name only).
    assert "pii-outside-allowlist" in md
    assert "`stg_accounts.account_id`" in md
    assert "`mart_users`" in md
    # Build/test sets rendered.
    assert "Selective build set (1)" in md
    assert "Selective test set (1)" in md
    # Notification intent rendered for the consumer's CI to route.
    assert "#data-governance" in md
    assert "PII reaches reader" in md


def test_allow_verdict_with_no_hits_still_renders_section():
    verdict = {"decision": "allow", "hits": [], "build_set": [], "test_set": []}
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "Policy verdict — ALLOW" in md
    assert "No policy rule fired" in md


def test_block_verdict_states_how_to_clear():
    """Feedback F1: a block must carry its release path — framed as block-until, self-clearing
    on the next push, not a dead end."""
    verdict = {
        "decision": "block",
        "hits": [
            {
                "rule_id": "r",
                "decision": "block",
                "change_model": "m",
                "change_column": "c",
                "matched_reach": [],
                "actions": ["block"],
            }
        ],
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "Blocked until" in md
    assert "clears itself" in md


def test_allow_and_warn_have_no_block_until_note():
    for decision in ("allow", "warn"):
        verdict = {"decision": decision, "hits": []}
        md = render_changeset_markdown(_report_with_policy(verdict))
        assert "Blocked until" not in md


def _hit(rule_id="r", decision="block", **kwargs):
    hit = {
        "rule_id": rule_id,
        "decision": decision,
        "change_model": "m",
        "change_column": "c",
        "matched_reach": [],
        "actions": [decision],
    }
    hit.update(kwargs)
    return hit


def test_coverage_footer_present_when_undecided():
    """Honesty invariant: anything the policy left UNDECIDED (missing meta / unresolved reach)
    is stated LOUDLY and never folded into a clean pass."""
    verdict = {
        "decision": "block",
        "hits": [_hit()],
        "unresolved_reach_count": 2,
        "skipped_missing_meta": 1,
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "Coverage:" in md
    assert "1 column undecided (missing meta)" in md
    assert "2 reaches unresolved" in md
    assert "did NOT count as safe" in md


def test_coverage_footer_absent_when_fully_decided():
    verdict = {
        "decision": "block",
        "hits": [_hit()],
        "unresolved_reach_count": 0,
        "skipped_missing_meta": 0,
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "Coverage:" not in md
    assert "did NOT count as safe" not in md


def test_fired_on_unknown_hit_marked_distinct_from_proven():
    """The single most important line: a fail-safe-driven block MUST render visibly differently
    from a proven one — never presented as a proven match."""
    verdict = {
        "decision": "block",
        "hits": [
            _hit(rule_id="proven-block", fired_on_unknown=False),
            _hit(rule_id="failsafe-block", fired_on_unknown=True, unknown_cause="missing"),
        ],
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    proven_line = next(ln for ln in md.splitlines() if "proven-block" in ln)
    failsafe_line = next(ln for ln in md.splitlines() if "failsafe-block" in ln)
    # They must not render identically (strip the rule id to compare the treatment).
    assert proven_line.replace("proven-block", "X") != failsafe_line.replace("failsafe-block", "X")
    # The fail-safe row is LOUD and names the cause; the proven row is a quiet check.
    assert "fired on a fail-safe default" in failsafe_line
    assert "meta missing" in failsafe_line
    assert "proven match" in proven_line
    assert "fired on a fail-safe default" not in proven_line


def test_fired_on_unknown_error_cause_labeled():
    verdict = {
        "decision": "warn",
        "hits": [_hit(decision="warn", fired_on_unknown=True, unknown_cause="error")],
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "fired on a fail-safe default" in md
    assert "(evaluation error)" in md


def test_matched_reach_sample_capped():
    verdict = {
        "decision": "warn",
        "hits": [_hit(decision="warn", matched_reach=["a", "b", "c", "d", "e"])],
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "+2 more" in md


def test_why_this_verdict_heading_present():
    verdict = {"decision": "block", "hits": [_hit()]}
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "Why this verdict" in md


def test_unproven_conditions_render_honest_line_with_folded_list():
    """Issue #124 telemetry: suppressed warn-rule UNKNOWNs under fail_closed render an honest
    count line plus the folded per-rule list, so an all-warn pilot report never reads clean
    while block mode would fail closed."""
    verdict = {
        "decision": "allow",
        "hits": [],
        "unproven_count": 2,
        "unproven": [
            {
                "rule_id": "warn-pii",
                "change_model": "stg_accounts",
                "change_column": "ssn",
                "unknown_cause": "missing",
                "unknown_leaves": ["meta.pii"],
            },
            {
                "rule_id": "roles-subset",
                "change_model": "stg_accounts",
                "change_column": "roles",
                "unknown_cause": "error",
                "unknown_leaves": ["meta.roles"],
            },
        ],
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "2 unproven warn-rule conditions (unknown metadata under `fail_closed`)" in md
    # The list is folded but present, identifying rule, subject, leaf, and cause.
    assert "<details><summary>Unproven conditions (2)</summary>" in md
    assert "warn-pii" in md
    assert "`stg_accounts.ssn`" in md
    assert "meta.pii" in md
    assert "meta missing" in md
    assert "evaluation error" in md


def test_unproven_singular_line():
    verdict = {
        "decision": "allow",
        "hits": [],
        "unproven_count": 1,
        "unproven": [
            {
                "rule_id": "warn-pii",
                "change_model": "m",
                "change_column": "c",
                "unknown_cause": "missing",
                "unknown_leaves": ["meta.pii"],
            }
        ],
    }
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "1 unproven warn-rule condition (unknown metadata under `fail_closed`)" in md


def test_no_unproven_renders_no_unproven_section():
    verdict = {"decision": "allow", "hits": [], "unproven_count": 0, "unproven": []}
    md = render_changeset_markdown(_report_with_policy(verdict))
    assert "unproven warn-rule condition" not in md
    assert "Unproven conditions" not in md
