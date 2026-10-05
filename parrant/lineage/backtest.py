"""``policy test`` driver: replay a candidate policy over git history (or a saved
changeset corpus) and report what the gate WOULD have ruled.

The whole performance story is *load the head registry ONCE, replay many* (see the spec): the
caller builds one :class:`~parrant.lineage.service.LineageService` (the ~8.5s fixed
cost) and hands it in; every point reuses it. Inside the loop nothing is new — it reuses the
exact primitives the ``impact`` command runs today (``build_git_changeset`` /
``get_changeset_impact`` / ``classify_provable_breaks`` / ``evaluate_policy``). The only net-new
code is the point enumeration, the per-point loop, and the aggregation/report.

Honesty invariants (see :class:`BacktestReport.fidelity_note`): the backtest ships WITHOUT a per-commit
base manifest in every mode, so the provable-break and semantic meaning-change BLOCK tiers are
never exercised here — the note states that plainly and never names an unavailable flag. Every
git-diff change is ``indeterminate`` by construction; ``unmapped_changes`` and per-point parse
failures are surfaced rather than hidden. Execution is deliberately SEQUENTIAL for determinism;
the loop is structured so a bounded thread pool can wrap it later without touching the shared
read-only registry.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from typing import Any

from parrant.lineage.changeset import (
    ChangeKind,
    ColumnChange,
    changes_from_dicts,
    git_changed_models_and_unmapped,
    git_rev_list,
)
from parrant.lineage.service import LineageService
from parrant.lineage.verdict import classify_provable_breaks
from parrant.models.schema import (
    BacktestFiredHit,
    BacktestPointResult,
    BacktestReport,
    BacktestRuleStat,
    BacktestSelectionStats,
    GateDecision,
    Policy,
    PolicyVerdict,
    ResolutionReasonCount,
    SemanticChangeKind,
)

# The git empty-tree object: diffing a root commit (no parent) against this reads every file as
# added, so the root commit still replays as a full changeset rather than crashing.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Cap the per-point sample_reach so a huge blast radius does not bloat the report payload.
_SAMPLE_REACH_CAP = 20


def _git_diff_fidelity_note() -> str:
    return (
        "Git-diff mode: whole-file .sql diffs, so every change is classified `indeterminate` by "
        "construction. The provable-break and semantic meaning-change BLOCK tiers are NOT "
        "exercised in this version (there is no per-commit before-state); per-commit "
        "base-manifest validation of those tiers ships in a later release. Rules keyed on reach "
        "/ meta / change.kind / on_indeterminate DID fire and CAN block."
    )


def _changesets_fidelity_note() -> str:
    return (
        "Changesets mode: replayed saved changeset JSON against the HEAD registry. There is no "
        "base registry, so the provable-break BLOCK tier is NOT exercised; semantic "
        "meaning-change blocks fire only if a saved changeset already carried a `meaning_changed` "
        "classification. Treat block-tier coverage as partial."
    )


def _changes_for_models(head: LineageService, models: list[str]) -> list[ColumnChange]:
    """Expand a set of touched models into coarse ``logic_changed`` / ``INDETERMINATE`` changes.

    Mirrors :func:`build_git_changeset`'s inner loop but takes the already-computed model set, so
    the driver pays exactly ONE git subprocess per point (it derives matched + unmapped from the
    same diff) instead of shelling out twice.
    """
    head_models = head.registry.get_models()
    chosen: dict[tuple[str, str], ColumnChange] = {}
    for model_name in models:
        model = head_models[model_name]
        for column in sorted(model.columns):
            chosen[(model_name, column)] = ColumnChange(
                model_name,
                column,
                ChangeKind.LOGIC_CHANGED,
                detail=model.resource_path,
                semantic=SemanticChangeKind.INDETERMINATE,
            )
    return sorted(chosen.values(), key=lambda c: (c.model, c.column))


def _parent_ref(sha: str, repo_dir: str | None) -> str:
    """The first-parent ref to diff ``sha`` against, or the empty tree for a root commit.

    Squash-merge repos are linear (the spec's stated assumption), so first-parent ``^`` is the
    right base. A root commit has no parent, so we fall back to the empty tree hash.
    """
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return f"{sha}^"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _EMPTY_TREE


def _replay_point(
    head_service: LineageService,
    policy: Policy,
    changes: list[ColumnChange],
    ref: str,
    source: str,
    unmapped: int,
    parse_failures: list[str],
) -> tuple[BacktestPointResult, PolicyVerdict]:
    """Run the existing impact + breaks + policy pipeline for one point.

    In git-diff / changesets mode there is no base registry, so ``classify_provable_breaks``
    returns an honest empty list (the provable-break BLOCK tier cannot fire) and every change is
    ``INDETERMINATE`` by construction. Returns the point result and the raw verdict (the caller
    aggregates the verdict's hits across all points)."""
    aggregated = head_service.get_changeset_impact(changes)
    breaks = classify_provable_breaks(changes, head_service.registry, None)
    from parrant.lineage.policy import evaluate_policy

    verdict = evaluate_policy(changes, aggregated, head_service.registry, policy, breaks)

    summary = aggregated.get("summary", {}) if isinstance(aggregated, dict) else {}
    blast_radius = int(summary.get("affected_models", 0)) if isinstance(summary, dict) else 0
    affected = aggregated.get("affected_models", []) if isinstance(aggregated, dict) else []
    sample_reach = [str(m.get("name")) for m in affected][:_SAMPLE_REACH_CAP]

    fired = [
        BacktestFiredHit(
            rule_id=hit.rule_id,
            decision=hit.decision,
            fired_on_unknown=hit.fired_on_unknown,
        )
        for hit in verdict.hits
    ]
    widened, skippable_count, forced, reasons = _selection_facts(aggregated)
    point = BacktestPointResult(
        ref=ref,
        source=source,
        total_changes=len(changes),
        unmapped_changes=unmapped,
        parse_failures=list(parse_failures),
        decision=verdict.decision.value,
        blast_radius=blast_radius,
        fired=fired,
        sample_reach=sample_reach,
        selection_widened=widened,
        skippable_models_count=skippable_count,
        rebuild_forced_by_nonresolution=forced,
        resolution_reasons=reasons,
    )
    return point, verdict


def _selection_facts(
    aggregated: Any,
) -> tuple[bool | None, int | None, int | None, dict[str, int]]:
    """Extract the per-point selection/resolution facts for the widen-rate stats.

    Pure observation of the already-computed ``selection`` / ``resolution_summary`` blocks —
    nothing here influences selection or gating. Returns all-``None``/empty when the impact
    carried no selection block (e.g. a stub service without a registry), so such points are
    honestly excluded from the aggregate rather than counted as "not widened".
    """
    if not isinstance(aggregated, dict):
        return None, None, None, {}
    selection = aggregated.get("selection")
    if not isinstance(selection, dict):
        return None, None, None, {}
    widened = bool(selection.get("widened_to_all_reachable", False))
    skippable_count = len(selection.get("skippable_models") or [])

    forced: int | None = None
    reasons: dict[str, int] = {}
    summary = aggregated.get("resolution_summary")
    if isinstance(summary, dict):
        forced = int(summary.get("rebuild_forced_by_nonresolution", 0))
        for entry in summary.get("top_reasons") or []:
            if isinstance(entry, dict) and entry.get("reason") is not None:
                reasons[str(entry["reason"])] = int(entry.get("count", 0))
    return widened, skippable_count, forced, reasons


# Cap the aggregated top-reasons list so a reason-rich range stays a readable breakdown.
_TOP_REASONS_CAP = 10


def _aggregate_selection_stats(
    points: list[BacktestPointResult],
) -> BacktestSelectionStats | None:
    """Roll the per-point selection facts up into the range-wide widen-rate statistics.

    Only points that actually carried a selection block participate (``selection_widened is
    not None``); a range with none returns ``None`` so the report omits the surface instead of
    fabricating a 0% widen rate. Median is over ``skippable_models_count``; reasons are summed
    across points and ranked by frequency (ties broken alphabetically for determinism).
    """
    with_selection = [p for p in points if p.selection_widened is not None]
    if not with_selection:
        return None

    widened_points = sum(1 for p in with_selection if p.selection_widened)
    skippable_counts = [p.skippable_models_count or 0 for p in with_selection]
    forced_total = sum(p.rebuild_forced_by_nonresolution or 0 for p in with_selection)

    reason_totals: dict[str, int] = defaultdict(int)
    for point in with_selection:
        for reason, count in point.resolution_reasons.items():
            reason_totals[reason] += count
    top_reasons = [
        ResolutionReasonCount(reason=reason, count=count)
        for reason, count in sorted(reason_totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ][:_TOP_REASONS_CAP]

    return BacktestSelectionStats(
        points_with_selection=len(with_selection),
        widened_points=widened_points,
        widen_rate_pct=round(100.0 * widened_points / len(with_selection), 1),
        median_skippable_models=float(statistics.median(skippable_counts)),
        rebuild_forced_by_nonresolution_total=forced_total,
        top_reasons=top_reasons,
    )


def _aggregate_rule_stats(
    points_verdicts: list[tuple[BacktestPointResult, PolicyVerdict]],
    policy: Policy,
) -> list[BacktestRuleStat]:
    """Per-rule aggregate across the whole range.

    Rows: every ``policy.rules`` id (always present, so a rule that never fired reads as a
    ``matched_zero`` dead rule — the honesty signal), then any synthetic ``builtin:*`` rule that
    fired (excluded from the dead-rule check per spec). ``fired_on_unknown`` sums the fail-safe
    firings so a rule that blocks only via fail-safe UNKNOWN stands out even though it is not
    ``matched_zero``.
    """
    policy_ids = [rule.id for rule in policy.rules]
    policy_id_set = set(policy_ids)

    fired_total: dict[str, int] = defaultdict(int)
    fired_unknown: dict[str, int] = defaultdict(int)
    block_prs: dict[str, int] = defaultdict(int)
    warn_prs: dict[str, int] = defaultdict(int)
    extra_order: list[str] = []

    for _point, verdict in points_verdicts:
        block_here: set[str] = set()
        warn_here: set[str] = set()
        for hit in verdict.hits:
            rid = hit.rule_id
            fired_total[rid] += 1
            if hit.fired_on_unknown:
                fired_unknown[rid] += 1
            if hit.decision is GateDecision.BLOCK:
                block_here.add(rid)
            elif hit.decision is GateDecision.WARN:
                warn_here.add(rid)
            if rid not in policy_id_set and rid not in extra_order:
                extra_order.append(rid)
        for rid in block_here:
            block_prs[rid] += 1
        for rid in warn_here:
            warn_prs[rid] += 1

    stats: list[BacktestRuleStat] = []
    for rid in policy_ids + extra_order:
        total = fired_total.get(rid, 0)
        stats.append(
            BacktestRuleStat(
                rule_id=rid,
                would_block_prs=block_prs.get(rid, 0),
                would_warn_prs=warn_prs.get(rid, 0),
                fired_total=total,
                fired_on_unknown=fired_unknown.get(rid, 0),
                matched_zero=(rid in policy_id_set and total == 0),
            )
        )
    return stats


def _load_changesets_dir(path: str) -> list[tuple[str, list[ColumnChange]]]:
    """Read ``*.json`` from ``path`` and reconstruct each into ``(filename, changes)``.

    Accepts either a bare change-list, ``{"changes": [...]}``, or the full changeset report shape
    ``{"changeset": {"changes": [...]}}``. Files are read in sorted order for determinism.
    """
    if not os.path.isdir(path):
        raise RuntimeError(f"--changesets path is not a directory: '{path}'")
    out: list[tuple[str, list[ColumnChange]]] = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        full = os.path.join(path, name)
        with open(full, encoding="utf-8") as handle:
            data = json.load(handle)
        entries = _extract_change_entries(data)
        out.append((name, changes_from_dicts(entries)))
    return out


def _extract_change_entries(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("changeset"), dict):
            changeset = data["changeset"]
            if isinstance(changeset.get("changes"), list):
                return changeset["changes"]
        if isinstance(data.get("changes"), list):
            return data["changes"]
    raise RuntimeError(
        "changeset JSON must be a list of changes, {changes: [...]}, or "
        "{changeset: {changes: [...]}}"
    )


def _resolve_git_range(git_range: str | None, last: int | None) -> tuple[str, str]:
    """Resolve ``(base, head)`` from ``--git-range base..head`` or ``--last N`` sugar."""
    if last is not None:
        if last < 1:
            raise RuntimeError("--last must be a positive integer")
        return f"HEAD~{last}", "HEAD"
    assert git_range is not None
    if ".." not in git_range:
        raise RuntimeError(f"--git-range must be of the form <base>..<head>, got '{git_range}'")
    base, _, head = git_range.partition("..")
    base = base.strip()
    head = head.strip() or "HEAD"
    if not base:
        raise RuntimeError(f"--git-range needs a base ref, got '{git_range}'")
    return base, head


def run_backtest(
    head_service: LineageService,
    policy: Policy,
    *,
    git_range: str | None = None,
    last: int | None = None,
    changesets_dir: str | None = None,
    repo_dir: str | None = None,
    baseline: BacktestReport | None = None,
    policy_source: str = "<policy>",
    progress: bool = True,
) -> BacktestReport:
    """Replay ``policy`` over the chosen point source and assemble a :class:`BacktestReport`.

    Exactly one of ``git_range`` / ``last`` / ``changesets_dir`` must be supplied (the CLI is the
    primary guard; validated defensively here). ``head_service`` is the ONE pre-built head
    registry, reused for every point. ``baseline`` (when supplied) is attached as a per-rule
    ``baseline_delta`` for the regression gate.

    Execution is SEQUENTIAL by design (determinism); ``progress`` emits ``replaying i/N <ref>`` to
    stderr so a long ``--last 50`` run is observably alive.
    """
    sources = [git_range is not None or last is not None, changesets_dir is not None]
    if sum(1 for s in sources if s) != 1:
        raise RuntimeError(
            "provide exactly one change source: --git-range / --last (git) or --changesets (dir)"
        )

    if changesets_dir is not None:
        return _run_changesets(
            head_service, policy, changesets_dir, baseline, policy_source, progress
        )
    return _run_git(
        head_service, policy, git_range, last, repo_dir, baseline, policy_source, progress
    )


def _run_git(
    head_service: LineageService,
    policy: Policy,
    git_range: str | None,
    last: int | None,
    repo_dir: str | None,
    baseline: BacktestReport | None,
    policy_source: str,
    progress: bool,
) -> BacktestReport:
    base, head = _resolve_git_range(git_range, last)
    commits = git_rev_list(base, head, repo_dir)

    warnings: list[str] = [
        (
            "Merge/non-squash repos: first-parent diffs may double-count changes across a "
            "non-linear history (squash-merge repos are linear and unaffected)."
        )
    ]
    points_verdicts: list[tuple[BacktestPointResult, PolicyVerdict]] = []
    skipped = 0
    total = len(commits)
    for i, sha in enumerate(commits, start=1):
        short = sha[:12]
        if progress:
            print(f"replaying {i}/{total} {short}", file=sys.stderr)
        parent = _parent_ref(sha, repo_dir)
        source = f"git-diff ({short})"
        try:
            matched, unmapped_paths = git_changed_models_and_unmapped(
                head_service.registry, parent, sha, repo_dir
            )
            changes = _changes_for_models(head_service, sorted(matched))
            point, verdict = _replay_point(
                head_service, policy, changes, short, source, len(unmapped_paths), []
            )
        except Exception as exc:  # per-point failure must not abort the whole backtest
            skipped += 1
            msg = f"{short}: failed to replay ({exc})"
            warnings.append(msg)
            point = BacktestPointResult(
                ref=short,
                source=source,
                total_changes=0,
                decision="allow",
                parse_failures=[str(exc)],
            )
            points_verdicts.append((point, PolicyVerdict(decision=GateDecision.ALLOW)))
            continue
        points_verdicts.append((point, verdict))

    return _assemble_report(
        mode="git-diff",
        policy_source=policy_source,
        base=base,
        head=head,
        points_verdicts=points_verdicts,
        policy=policy,
        skipped=skipped,
        warnings=warnings,
        fidelity_note=_git_diff_fidelity_note(),
        baseline=baseline,
    )


def _run_changesets(
    head_service: LineageService,
    policy: Policy,
    changesets_dir: str,
    baseline: BacktestReport | None,
    policy_source: str,
    progress: bool,
) -> BacktestReport:
    corpus = _load_changesets_dir(changesets_dir)
    warnings: list[str] = []
    points_verdicts: list[tuple[BacktestPointResult, PolicyVerdict]] = []
    skipped = 0
    total = len(corpus)
    for i, (name, changes) in enumerate(corpus, start=1):
        if progress:
            print(f"replaying {i}/{total} {name}", file=sys.stderr)
        source = f"changeset ({name})"
        try:
            point, verdict = _replay_point(head_service, policy, changes, name, source, 0, [])
        except Exception as exc:
            skipped += 1
            warnings.append(f"{name}: failed to replay ({exc})")
            point = BacktestPointResult(
                ref=name,
                source=source,
                total_changes=len(changes),
                decision="allow",
                parse_failures=[str(exc)],
            )
            points_verdicts.append((point, PolicyVerdict(decision=GateDecision.ALLOW)))
            continue
        points_verdicts.append((point, verdict))

    return _assemble_report(
        mode="changesets",
        policy_source=policy_source,
        base=None,
        head=None,
        points_verdicts=points_verdicts,
        policy=policy,
        skipped=skipped,
        warnings=warnings,
        fidelity_note=_changesets_fidelity_note(),
        baseline=baseline,
    )


def _assemble_report(
    *,
    mode: str,
    policy_source: str,
    base: str | None,
    head: str | None,
    points_verdicts: list[tuple[BacktestPointResult, PolicyVerdict]],
    policy: Policy,
    skipped: int,
    warnings: list[str],
    fidelity_note: str,
    baseline: BacktestReport | None,
) -> BacktestReport:
    points = [p for p, _v in points_verdicts]
    rule_stats = _aggregate_rule_stats(points_verdicts, policy)

    prs_replayed = len(points)
    prs_would_block = sum(1 for p in points if p.decision == GateDecision.BLOCK.value)
    prs_would_warn = sum(1 for p in points if p.decision == GateDecision.WARN.value)

    # Average blast radius over the points that actually evaluated (exclude skipped ones so a
    # bad commit does not silently drag the average toward zero).
    evaluated = prs_replayed - skipped
    avg_blast = round(sum(p.blast_radius for p in points) / evaluated, 2) if evaluated > 0 else 0.0

    baseline_delta = _baseline_delta(rule_stats, baseline) if baseline is not None else None

    return BacktestReport(
        mode=mode,  # type: ignore[arg-type]
        policy_source=policy_source,
        base=base,
        head=head,
        prs_replayed=prs_replayed,
        prs_would_block=prs_would_block,
        prs_would_warn=prs_would_warn,
        prs_skipped=skipped,
        avg_blast_radius=avg_blast,
        rule_stats=rule_stats,
        points=points,
        fidelity_note=fidelity_note,
        warnings=warnings,
        baseline_delta=baseline_delta,
        selection_stats=_aggregate_selection_stats(points),
    )


def _baseline_delta(rule_stats: list[BacktestRuleStat], baseline: BacktestReport) -> dict[str, Any]:
    """Per-rule would-BLOCK delta vs a saved baseline (for the regression gate + report)."""
    base_block = {s.rule_id: s.would_block_prs for s in baseline.rule_stats}
    per_rule: dict[str, dict[str, int]] = {}
    for stat in rule_stats:
        prev = base_block.get(stat.rule_id, 0)
        per_rule[stat.rule_id] = {
            "baseline_would_block_prs": prev,
            "current_would_block_prs": stat.would_block_prs,
            "delta": stat.would_block_prs - prev,
        }
    return {
        "baseline_prs_would_block": baseline.prs_would_block,
        "current_prs_would_block": sum(1 for s in rule_stats if s.would_block_prs > 0),
        "per_rule": per_rule,
    }


def backtest_exit_code(
    report: BacktestReport,
    fail_on: str,
    baseline: BacktestReport | None = None,
) -> int:
    """Translate a report into a CI exit code.

    - ``none``       -> always 0.
    - ``any-block``  -> 1 when any replayed PR would block.
    - ``regression`` -> 1 when any rule's would-BLOCK count rose vs ``baseline``. A missing
      baseline is uncomputable and MUST fail loudly (raises), never silently pass — a green gate
      validating nothing is the worst CI footgun.
    """
    if fail_on == "none":
        return 0
    if fail_on == "any-block":
        return 1 if report.prs_would_block > 0 else 0
    if fail_on == "regression":
        if baseline is None:
            raise RuntimeError(
                "--fail-on regression requires --baseline (a saved prior run to compare against)"
            )
        base_block = {s.rule_id: s.would_block_prs for s in baseline.rule_stats}
        for stat in report.rule_stats:
            if stat.would_block_prs > base_block.get(stat.rule_id, 0):
                return 1
        return 0
    raise RuntimeError(f"unknown --fail-on value: '{fail_on}'")


__all__ = [
    "backtest_exit_code",
    "changes_from_dicts",
    "run_backtest",
]
