"""Provable-break classifier: turn a changeset into a blockable verdict.

The blast radius answers "what does this change *reach*?". A reviewer's real question is
"will it *break* anything, and may I merge?". This module answers the blockable half of
that offline, from dbt artifacts alone: a column a change removes/renames that a dbt test
still targets will fail on the next ``dbt build`` — a deterministic, warehouse-free FAIL.

Only *provable* breaks drive a BLOCK verdict. Heuristic reach (derived recompute, exposures)
is REVIEW, and everything else is SAFE — so a team can flip ``--fail-on tests`` from warn to
block without fearing false positives. "Will the *value* be wrong" needs the warehouse and is
deliberately out of scope.
"""

from __future__ import annotations

from typing import Any

from parrant.lineage.changeset import ChangeKind, ColumnChange
from parrant.lineage.provider import LineageAndMetadataProvider, LineageProvider
from parrant.models.schema import BreakFinding, OverrideVerb, TestNode

# Change kinds that can orphan a test by making the column disappear. A rename is emitted by
# the changeset builder as REMOVED(old) + ADDED(new), so REMOVED already covers it.
_COLUMN_GONE_KINDS = (ChangeKind.REMOVED,)


def _column_missing_in_head(head: LineageProvider, model: str, column: str) -> bool:
    """True when ``model.column`` no longer exists in the head registry.

    This is the guard that keeps a break *provable*: we only flag a test as broken when the
    column it targets is genuinely absent from head — either the whole model was removed, or
    the column was dropped/renamed away. If head still exposes the column (e.g. a mis-derived
    change, or a removal the PR reverted), we do NOT flag it.
    """
    head_model = head.get_models().get(model.lower())
    if head_model is None:
        return True  # whole model gone → its columns are certainly gone
    return column.lower() not in {c.lower() for c in head_model.columns}


def classify_provable_breaks(
    changes: list[ColumnChange],
    head_registry: LineageAndMetadataProvider,
    base_registry: LineageAndMetadataProvider | None = None,
) -> list[BreakFinding]:
    """Return the dbt tests that a changeset provably breaks (BREAK-TEST).

    For each removed/renamed column we look up the tests that targeted it in the *base*
    registry (where the column and its tests both existed), then confirm the column is truly
    gone in *head*. Both sides of a ``relationships`` test are checked: dropping either the
    child column or the referenced parent key breaks it. Deduplicated by test per change.

    ``base_registry`` is the reliable source of pre-change tests. Without it (git-diff
    fallback, which only yields ``logic_changed``) there is nothing to prove, so no breaks
    are returned — an honest empty result rather than a guess.

    A base test is only flagged when it *still exists in head* (same ``unique_id``). dbt
    encodes a generic test's target column into its unique_id, so a rename that updates the
    test's yml produces a new id — the base id is gone and is correctly NOT flagged
    (removing a false block on the dominant "rename + update the test" refactor). A rename
    that leaves the yml stale keeps the old id in head, targeting a now-missing column, and
    IS flagged. Deleting a column together with its test also drops the id → not flagged.
    """
    source = base_registry or head_registry
    head_test_ids = head_registry.get_test_unique_ids()
    findings: list[BreakFinding] = []
    # Dedup across the whole changeset: a relationships test whose child column AND
    # referenced parent key are both removed in one PR must count once, not twice.
    seen: set[str] = set()

    def _emit(model: str, column: str, kind: str, test: TestNode, via_reference: bool) -> None:
        # Only a test that survives into head (still declared) can actually fail on build.
        if test.unique_id not in head_test_ids or test.unique_id in seen:
            return
        seen.add(test.unique_id)
        findings.append(
            BreakFinding(
                break_kind="break_test",
                change_model=model,
                change_column=column,
                change_kind=kind,
                test_name=test.test_name,
                test_unique_id=test.unique_id,
                resource_path=test.resource_path,
                via_reference=via_reference,
            )
        )

    # A model absent from head entirely takes ALL its tests down with it — including tests on
    # columns we couldn't recover from compiled SQL (incomplete column recovery would
    # otherwise make a dropped model read as SAFE). Handle these once per model.
    wholly_removed = {
        c.model
        for c in changes
        if c.kind in _COLUMN_GONE_KINDS and head_registry.get_models().get(c.model.lower()) is None
    }
    for model in wholly_removed:
        for test in source.get_model_tests(model):
            attached_here = (test.target_model or "").lower() == model.lower()
            # The column that's gone: the tested column for an attached test, or the parent
            # key for a relationships test that referenced this (now-removed) model.
            column = (test.target_column if attached_here else test.referenced_column) or model
            _emit(model, column, ChangeKind.REMOVED.value, test, via_reference=not attached_here)

    for change in changes:
        if change.kind not in _COLUMN_GONE_KINDS or change.model in wholly_removed:
            continue
        if not _column_missing_in_head(head_registry, change.model, change.column):
            # The column still exists in head — no orphaned test, nothing provable.
            continue

        for test in source.get_column_tests(change.model, change.column):
            _emit(change.model, change.column, change.kind.value, test, via_reference=False)
        # relationships tests whose *referenced* parent key is this removed column.
        for test in source.get_tests_referencing(change.model, change.column):
            _emit(change.model, change.column, change.kind.value, test, via_reference=True)

    return findings


def _has_meaning_shift(changes: list[ColumnChange] | None) -> bool:
    """True when any NON-overridden change carries a proven-or-unprovable meaning shift.

    An ``EQUIVALENT`` edit is never emitted as a change, so a *set* ``semantic`` is always
    either ``MEANING_CHANGED`` (the AST diff proved the derivation's meaning moved) or
    ``INDETERMINATE`` (it could not be proven safe) — both ``is_breaking``. Either warrants
    a human look, even when nothing downstream is recomputed. A change carrying ANY override
    pragma is excluded (the author acknowledged it), so it no longer lifts the ruling.
    """
    if not changes:
        return False
    return any(
        c.override is None and c.semantic is not None and c.semantic.is_breaking for c in changes
    )


def break_is_overridden(break_finding: BreakFinding, changes: list[ColumnChange] | None) -> bool:
    """True when the change matching this provable break carries an ``allow-break`` override.

    Fail-safe: only the hard ``allow-break`` verb can demote a break — ``allow-change`` never
    can. Matched case-insensitively on ``(model, column)``.
    """
    if not changes:
        return False
    model = break_finding.change_model.lower()
    column = break_finding.change_column.lower()
    for change in changes:
        if (
            change.model.lower() == model
            and change.column.lower() == column
            and change.override is not None
            and change.override.verb is OverrideVerb.ALLOW_BREAK
        ):
            return True
    return False


def unexcused_break_count(breaks: list[BreakFinding], changes: list[ColumnChange] | None) -> int:
    """Number of provable breaks NOT excused by an ``allow-break`` override.

    This is the count the CI gate (``--fail-on tests``) must read: an acknowledged break is
    demoted to REVIEW and must not keep the gate armed, or the override is cosmetic for the
    exact gate the feature exists to keep usable. With no overrides this equals ``len(breaks)``.
    """
    return sum(1 for b in breaks if not break_is_overridden(b, changes))


_REACHING_MECHANISMS = ("derived_recompute", "rowset_filter")


def _reaching_change_keys(by_change: list[dict[str, Any]] | None) -> set[tuple[str, str, str]]:
    """``(model, column, kind)`` keys of changes that reach a recompute/row-set column or an
    exposure — i.e. the changes that drive a blast-radius REVIEW."""
    keys: set[tuple[str, str, str]] = set()
    for entry in by_change or []:
        if not entry.get("resolved"):
            continue
        reaches = bool(entry.get("reached_exposures"))
        if not reaches:
            for reached in entry.get("reached_columns", []):
                if reached.get("mechanism") in _REACHING_MECHANISMS:
                    reaches = True
                    break
        if reaches:
            keys.add((str(entry.get("model")), str(entry.get("column")), str(entry.get("kind"))))
    return keys


def _change_reaches(change: ColumnChange, reaching_keys: set[tuple[str, str, str]]) -> bool:
    """Whether a change contributes to the REVIEW tier: it meaning-shifts, or it reaches a
    recompute/row-set column or an exposure (per ``by_change``)."""
    if change.semantic is not None and change.semantic.is_breaking:
        return True
    return (change.model, change.column, change.kind.value) in reaching_keys


def _all_reaching_overridden(
    changes: list[ColumnChange], by_change: list[dict[str, Any]] | None
) -> bool:
    """True only when EVERY change that drives a blast-radius review carries an override.

    Used to suppress the reach-review tier precisely: if any reaching change is un-acknowledged,
    the review stands. Returns False when nothing reaches (so the caller keeps ``summary``'s
    reach signal as the baseline rather than re-deriving it)."""
    reaching = _reaching_change_keys(by_change)
    if not reaching:
        return False
    override_by_key = {(c.model, c.column, c.kind.value): c.override for c in changes}
    for key in reaching:
        if override_by_key.get(key) is None:
            return False
    return True


def decide_verdict(
    breaks: list[BreakFinding],
    summary: dict[str, Any],
    changes: list[ColumnChange] | None = None,
    by_change: list[dict[str, Any]] | None = None,
) -> str:
    """Collapse breaks + blast-radius summary + semantic axis into a single ruling.

    - ``block``: at least one provable break with no ``allow-break`` override — objective
      enough to fail a gate.
    - ``review``: no *blocking* break, but *either* the change recomputes downstream logic,
      shifts a row-set, or reaches a business-facing exposure, *or* the semantic diff found
      a column whose meaning changed / couldn't be proven safe — a human should look. A break
      demoted by ``allow-break`` also floors the ruling here (never ``safe``).
    - ``safe``: nothing downstream is recomputed, no exposure is touched, and every changed
      column's derivation is a proven ``EQUIVALENT`` (or non-logic) edit.

    The semantic axis only ever lifts ``safe`` → ``review``; it never drives ``block``.

    ``changes`` is optional for backward compatibility; without it the semantic axis is
    invisible. ``by_change`` (also optional) enables precise per-change override suppression of
    the reach-review tier: it is used ONLY to test whether every reaching change is overridden,
    never to re-derive the baseline reach signal (which stays ``summary``). When ``by_change``
    is absent, the reach review can never be suppressed (fail-safe) — precise per-change capping
    is then the policy engine's job.
    """
    # (1) A break is demoted (not blocking) when its change carries an allow-break override.
    blocking_breaks = [b for b in breaks if not break_is_overridden(b, changes)]
    demoted = len(breaks) - len(blocking_breaks)
    if blocking_breaks:
        return "block"

    # (2) Blast-radius / semantic review. ``summary`` is the baseline reach signal; ``by_change``
    # only lets us suppress it when EVERY reaching change is acknowledged.
    reaches_review = bool(
        summary.get("critical_count", 0)
        or summary.get("filter_count", 0)
        or summary.get("affected_exposures", 0)
    )
    review = False
    if reaches_review:
        if (
            changes is not None
            and by_change is not None
            and _all_reaching_overridden(changes, by_change)
        ):
            review = False  # every reaching change acknowledged -> suppressed
        else:
            review = True
    if _has_meaning_shift(changes):
        review = True

    # (3) A demoted (allow-break'd) break floors the ruling at review, never safe.
    if demoted:
        return "review"
    return "review" if review else "safe"


def _applied_record(
    change: ColumnChange, downgraded_from: str, downgraded_to: str
) -> dict[str, Any]:
    """A unified honored-override record. Same shape as the policy path so the report's
    ``overrides`` block and the ``overrides_applied`` count never diverge across the two gates."""
    override = change.override
    assert override is not None
    return {
        "model": change.model,
        "column": change.column,
        "verb": override.verb.value,
        "reason": override.reason,
        "downgraded_from": downgraded_from,
        "downgraded_to": downgraded_to,
        "source_line": override.source_line,
        "scope": override.scope,
    }


def override_hint(change: ColumnChange, is_break: bool) -> str:
    """A pointed hint for an override that landed but changed nothing (a no-op)."""
    override = change.override
    assert override is not None
    if is_break and override.verb is OverrideVerb.ALLOW_CHANGE:
        return "allow-change cannot excuse a provable break — use allow-break to acknowledge it"
    if change.kind is ChangeKind.ADDED and override.verb is OverrideVerb.ALLOW_BREAK:
        return (
            "allow-break landed on an ADDED column; a rename break is on the REMOVED old "
            "column — use explicit column=<old_name> or model scope to excuse it"
        )
    return "matched a change that neither breaks nor reaches anything — safe to remove"


def ineffective_override_record(change: ColumnChange, is_break: bool) -> dict[str, Any]:
    """A unified no-op override record (same base skeleton + a ``hint``)."""
    override = change.override
    assert override is not None
    record = override.to_record()
    record["model"] = change.model
    record["column"] = change.column
    record["hint"] = override_hint(change, is_break)
    return record


def applied_overrides(
    changes: list[ColumnChange],
    breaks: list[BreakFinding],
    by_change: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Honored-override records for the DEFAULT (no-policy) gate — one per override that
    actually lowered its change's contribution. Overrides that changed nothing are skipped
    here and surface via :func:`ineffective_overrides` instead."""
    break_keys = {(b.change_model.lower(), b.change_column.lower()) for b in breaks}
    reaching = _reaching_change_keys(by_change)
    records: list[dict[str, Any]] = []
    for change in changes:
        if change.override is None:
            continue
        is_break = (change.model.lower(), change.column.lower()) in break_keys
        verb = change.override.verb
        if verb is OverrideVerb.ALLOW_BREAK and is_break:
            records.append(_applied_record(change, "block", "review"))
        elif verb is OverrideVerb.ALLOW_CHANGE and is_break:
            continue  # cannot silence a provable break -> ineffective (fail-safe)
        elif _change_reaches(change, reaching):
            records.append(_applied_record(change, "review", "safe"))
    return records


def ineffective_overrides(
    changes: list[ColumnChange],
    breaks: list[BreakFinding],
    by_change: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Override records that resolved to a REAL changed column but produced NO effect (the
    rename black-hole and friends). Distinct from stale overrides (no matching change at all),
    these must surface so the author isn't silently ignored."""
    break_keys = {(b.change_model.lower(), b.change_column.lower()) for b in breaks}
    reaching = _reaching_change_keys(by_change)
    records: list[dict[str, Any]] = []
    for change in changes:
        if change.override is None:
            continue
        is_break = (change.model.lower(), change.column.lower()) in break_keys
        verb = change.override.verb
        if verb is OverrideVerb.ALLOW_BREAK and is_break:
            continue  # effective
        if verb is OverrideVerb.ALLOW_CHANGE and is_break:
            records.append(ineffective_override_record(change, is_break))
            continue
        if _change_reaches(change, reaching):
            continue  # effective
        records.append(ineffective_override_record(change, is_break))
    return records
