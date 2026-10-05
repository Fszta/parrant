"""Renderer tests: the per-rule aggregate table/markdown flag fail-safe + dead rules loudly,
and an empty range renders cleanly (no division-by-zero, no crash)."""

from parrant.lineage.display.backtest import (
    render_backtest_markdown,
    render_backtest_table,
)
from parrant.models.schema import (
    BacktestReport,
    BacktestRuleStat,
)


def _report(rule_stats=None, **kwargs):
    base = {
        "mode": "git-diff",
        "policy_source": "p.yml",
        "base": "HEAD~3",
        "head": "HEAD",
        "prs_replayed": 3,
        "prs_would_block": 1,
        "prs_would_warn": 1,
        "avg_blast_radius": 2.5,
        "rule_stats": rule_stats or [],
        "fidelity_note": "NOTE: block tiers not exercised.",
    }
    base.update(kwargs)
    return BacktestReport(**base)


def _stats():
    return [
        BacktestRuleStat(
            rule_id="exposure-guard",
            would_warn_prs=2,
            fired_total=14,
            fired_on_unknown=0,
        ),
        BacktestRuleStat(
            rule_id="naive-pii",
            would_block_prs=3,
            fired_total=9,
            fired_on_unknown=9,  # fires ONLY via fail-safe
        ),
        BacktestRuleStat(rule_id="dead-guard", fired_total=0, matched_zero=True),
    ]


def test_table_has_per_rule_columns_and_flags():
    out = render_backtest_table(_report(_stats()))
    assert "rule_id" in out
    assert "exposure-guard" in out
    assert "naive-pii" in out
    # pure fail-safe rule is loudly flagged
    assert "FAIL-SAFE ONLY" in out
    # dead rule flagged
    assert "DEAD" in out
    # totals + fidelity note present
    assert "3 PR(s) replayed" in out
    assert "block tiers not exercised" in out


def test_markdown_has_table_and_flags():
    out = render_backtest_markdown(_report(_stats()))
    assert "| rule_id |" in out
    assert "fail-safe UNKNOWN" in out
    assert "naive-pii" in out
    assert "FAIL-SAFE ONLY" in out
    assert "DEAD" in out
    assert "Fidelity" in out


def test_empty_range_renders_cleanly():
    empty = _report(
        rule_stats=[],
        prs_replayed=0,
        prs_would_block=0,
        prs_would_warn=0,
        avg_blast_radius=0.0,
    )
    table = render_backtest_table(empty)
    md = render_backtest_markdown(empty)
    assert "0 PR(s) replayed" in table
    assert "no rules" in table
    assert "0 PR(s) replayed" in md


def test_partly_fail_safe_rule_flagged_distinctly():
    stats = [
        BacktestRuleStat(rule_id="mixed", would_block_prs=1, fired_total=10, fired_on_unknown=3)
    ]
    out = render_backtest_table(_report(stats))
    assert "partly fail-safe" in out
