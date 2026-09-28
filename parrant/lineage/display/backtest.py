"""Render a :class:`BacktestReport` as a human table or a CI/agent Markdown artifact.

The per-rule aggregate is the trust instrument (see the design): each row shows would-BLOCK /
would-WARN PRs, total firings, and — the headline column — how many firings came from a fail-safe
UNKNOWN rather than a proven match. Two failure modes are flagged LOUDLY: a rule that never fired
(``matched_zero`` -> DEAD) and a rule that fired ONLY via fail-safe UNKNOWN (FAIL-SAFE) — a
blocking rule that never proved a real match. The totals surface coverage (N replayed / K
skipped) so a low-coverage run is never mistaken for a clean pass. JSON output is just
``json.dumps(report.model_dump(mode="json"))`` in the CLI — no renderer needed.
"""

from __future__ import annotations

from parrant.models.schema import BacktestReport, BacktestRuleStat


def _rule_flag(stat: BacktestRuleStat) -> str:
    """Loud diagnostic tag distinguishing DEAD (never fired) from FAIL-SAFE (only via UNKNOWN)."""
    if stat.matched_zero:
        return "DEAD (never fired)"
    if stat.fired_total > 0 and stat.fired_on_unknown == stat.fired_total:
        return "FAIL-SAFE ONLY (never a proven match)"
    if stat.fired_on_unknown > 0:
        return "partly fail-safe"
    return ""


def _totals_line(report: BacktestReport) -> str:
    evaluated = report.prs_replayed - report.prs_skipped
    return (
        f"{report.prs_replayed} PR(s) replayed ({evaluated} evaluated, "
        f"{report.prs_skipped} skipped) — {report.prs_would_block} would BLOCK, "
        f"{report.prs_would_warn} would WARN; avg blast radius {report.avg_blast_radius}"
    )


def render_backtest_table(report: BacktestReport) -> str:
    """A fixed-width text table for terminals — the per-rule aggregate + totals + fidelity note."""
    lines: list[str] = []
    lines.append(f"Policy backtest [{report.mode}] — policy: {report.policy_source}")
    if report.base or report.head:
        lines.append(f"Range: {report.base}..{report.head}")
    lines.append("")
    lines.append(_totals_line(report))
    lines.append("")

    if not report.rule_stats:
        lines.append("(no rules evaluated — empty range or a policy with no rules)")
    else:
        header = (
            f"{'rule_id':<28} {'BLOCK':>6} {'WARN':>6} {'fired':>6} " f"{'unknown':>8}  {'flag'}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for stat in report.rule_stats:
            block = str(stat.would_block_prs) if stat.would_block_prs else "-"
            warn = str(stat.would_warn_prs) if stat.would_warn_prs else "-"
            lines.append(
                f"{stat.rule_id:<28} {block:>6} {warn:>6} {stat.fired_total:>6} "
                f"{stat.fired_on_unknown:>8}  {_rule_flag(stat)}"
            )

    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in report.warnings:
            lines.append(f"  - {warning}")

    lines.append("")
    lines.append(f"Fidelity: {report.fidelity_note}")
    return "\n".join(lines)


def render_backtest_markdown(report: BacktestReport) -> str:
    """A Markdown report for a CI artifact / PR comment / agent over MCP."""
    lines: list[str] = []
    lines.append(f"## Policy backtest — `{report.policy_source}`")
    lines.append("")
    lines.append(f"- **Mode:** {report.mode}")
    if report.base or report.head:
        lines.append(f"- **Range:** `{report.base}..{report.head}`")
    lines.append(f"- {_totals_line(report)}")
    lines.append("")

    lines.append(
        "| rule_id | would-BLOCK PRs | would-WARN PRs | fired-total | fail-safe UNKNOWN | flag |"
    )
    lines.append("|---|---|---|---|---|---|")
    if not report.rule_stats:
        lines.append("| _(no rules)_ | | | | | |")
    else:
        for stat in report.rule_stats:
            block = str(stat.would_block_prs) if stat.would_block_prs else "–"
            warn = str(stat.would_warn_prs) if stat.would_warn_prs else "–"
            flag = _rule_flag(stat)
            flag_md = f"**{flag}**" if flag else ""
            lines.append(
                f"| {stat.rule_id} | {block} | {warn} | {stat.fired_total} | "
                f"{stat.fired_on_unknown} | {flag_md} |"
            )

    if report.warnings:
        lines.append("")
        lines.append("### Warnings")
        for warning in report.warnings:
            lines.append(f"- {warning}")

    lines.append("")
    lines.append(f"> **Fidelity:** {report.fidelity_note}")
    return "\n".join(lines)


__all__ = ["render_backtest_markdown", "render_backtest_table"]
