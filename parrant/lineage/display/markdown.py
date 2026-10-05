"""Human-readable Markdown rendering of a diff-driven impact report.

Same data as the ``--format json`` changeset report, shaped for a person to triage on a PR.

Reviewer-first layout (a reviewer's real question is *"should I worry?"*):
1. a one-line **verdict banner** fusing severity + blast radius + the confidence floor,
2. what changed, then the downstream models whose **output changes** (a scannable table),
3. business-facing exposures with **operational apps surfaced above dashboards** (a stale
   dashboard is a Monday fix; an automation acting on wrong data is an incident),
with per-expression folds (oversized SQL truncated) and low-risk pass-through folded away.
"""

from typing import Any

# Fold long dashboard lists so a huge blast radius stays scrollable.
_MAX_DASHBOARDS_INLINE = 8
# Cap the affected-column chain shown per cross-boundary dashboard: enough to
# be actionable ("go straight to `revenue`"), not a wall of columns on a wide `select *`.
_MAX_VIA_COLUMNS_INLINE = 4
# Truncate a single SQL expression past this many chars (a 2000-char JSON_OBJECT on one
# line forces horizontal scroll and makes the whole disclosure useless).
_MAX_SQL_CHARS = 400
# Cap the matched-reach sample shown per fired policy rule in the "why this verdict" section:
# enough to be concrete ("it reaches `marts.revenue`"), not a wall on a wide fan-out.
_MAX_REACH_INLINE = 3
# Cap the unanalyzable model names listed in the folded coverage-gap disclosure, so a huge
# blast radius stays readable. The JSON confidence block carries the complete lists; this
# cap is display-only and, when it elides names, is signalled with a "… +N more" line.
_UNANALYZABLE_NAME_CAP = 100

# Subtle attribution appended to every rendered report so an adopting repo's PRs
# passively surface where the analysis came from. Kept to one muted line.
_CREDIT_LINE = "<sub>— lineage by [parrant](https://github.com/Fszta/parrant)</sub>"

# Machine kinds → words a reviewer reads without a glossary.
_KIND_LABELS = {
    "logic_changed": "logic changed",
    "type_changed": "type changed",
    "added": "added",
    "removed": "removed",
}


# Honesty note when a catalog.json was missing on a side: add/removed/type_changed
# detection never ran, so a clean report is not proof those changes are absent.
_STRUCTURAL_SKIP_NOTE = (
    "Structural checks (type/added/removed) skipped — no `catalog.json` on both sides; "
    "run `dbt docs generate`."
)


def _structural_checks_skipped(report: dict[str, Any]) -> bool:
    return not report.get("structural_checks_available", True)


def _structural_diff_degraded_note(report: dict[str, Any]) -> str | None:
    """The honesty line for a degraded structural diff (identical/stale catalogs), or None.

    A degraded stamp means removed/type_changed columns may be INVISIBLE in this report even
    though structural checks nominally ran — a quiet run must not read as proof.
    """
    block = report.get("structural_diff")
    if not isinstance(block, dict) or block.get("status") != "degraded":
        return None
    reason = block.get("reason") or "base and head catalogs are not independent"
    return (
        f"⚠️ Structural diff degraded — {reason}. "
        "Removed/retyped columns may be missing from this report."
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind.replace("_", " "))


def _truncate_sql(raw: str) -> tuple[str, str]:
    """Return (sql_to_show, note). Oversized one-liners are head-elided with a pointer."""
    raw = raw.strip()
    if len(raw) <= _MAX_SQL_CHARS:
        return raw, ""
    head = raw[:_MAX_SQL_CHARS].rstrip()
    note = (
        f"_Expression truncated ({len(raw)} chars). "
        f"View the full form in the HTML explorer or `--format json`._"
    )
    return head + " …", note


def _format_break(b: dict[str, Any]) -> str:
    """One compiler-style diagnostic line for a provable break.

    ``error[BREAK-TEST]`` — <removing|renaming> `model.column` breaks the **<kind>** test,
    with the schema-file path the reviewer opens to fix it. A relationships test broken
    through its referenced parent key is called out so the fix location is unambiguous.
    """
    verb = "renaming" if b.get("change_kind") == "renamed" else "removing"
    node = f"`{b.get('change_model', '?')}.{b.get('change_column', '?')}`"
    test_name = b.get("test_name", "?")
    via = " (via its referenced key)" if b.get("via_reference") else ""
    path = b.get("resource_path")
    where = f" — `{path}`" if path else ""
    return f"- `error[BREAK-TEST]` {verb} {node} breaks the **{test_name}** test{via}{where}"


def _owner_suffix(exposure: dict[str, Any]) -> str:
    """A ' — owner: **Name**' clause routing the exposure to who must sign off.

    dbt stores an exposure ``owner`` as ``{name, email}``. Surfacing it turns blast
    radius into accountability — the reviewer knows who to ping without leaving the PR.
    Returns "" when no owner is declared.
    """
    owner = exposure.get("owner") or {}
    if not isinstance(owner, dict):
        return ""
    label = owner.get("name") or owner.get("email")
    return f" — owner: **{label}**" if label else ""


def _group_by_model(columns: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for column in columns:
        grouped.setdefault(column.get("model", "?"), []).append(column)
    return {model: grouped[model] for model in sorted(grouped)}


def _confidence_floor_clause(confidence: dict[str, Any]) -> str:
    """A short clause for the verdict banner when impact is a lower bound."""
    if not confidence or confidence.get("level") == "full":
        return ""
    n = confidence.get("unanalyzable_models", 0)
    if not n:
        return ""
    return f" Impact is a **lower bound** — {_plural(n, 'downstream model')} couldn't be analyzed."


def _confidence_reason_words(confidence: dict[str, Any]) -> str:
    """Plain-language reason models were unanalyzable, for the footer."""
    no_column_info = confidence.get("no_column_info", 0)
    parse_failed = confidence.get("parse_failed", 0)
    if no_column_info and parse_failed:
        return f" ({parse_failed} had unparseable SQL, {no_column_info} exposed no columns)"
    if parse_failed:
        return " (their SQL wouldn't parse)"
    if no_column_info:
        return " (they expose no column-level information)"
    return ""


def _capped_name_lines(names: list[str], label: str) -> tuple[list[str], bool]:
    """Render up to the display cap of sorted model names, with a "… +N more" line
    when the source list is longer. Returns the lines and whether names were elided."""
    if not names:
        return [], False
    ordered = sorted(names)
    shown = ordered[:_UNANALYZABLE_NAME_CAP]
    lines = [f"- {label}: " + ", ".join(f"`{name}`" for name in shown)]
    extra = len(ordered) - len(shown)
    truncated = extra > 0
    if truncated:
        lines.append(f"  - … +{extra} more")
    return lines, truncated


def _render_unanalyzable_names(confidence: dict[str, Any]) -> list[str]:
    """A folded ``<details>`` disclosure of the reachable models that couldn't be
    analyzed, capped for readability. Mutates ``confidence`` to set the display-only
    ``*_truncated`` flags True when it actually elided names (the JSON surface, which is
    emitted before any markdown render, always carries the complete lists and False
    flags). Returns an empty list when there are no unanalyzable models."""
    no_column_info = confidence.get("no_column_info_models") or []
    parse_failed = confidence.get("parse_failed_models") or []
    opaque = confidence.get("opaque_models") or []
    if not no_column_info and not parse_failed and not opaque:
        return []
    total = len(no_column_info) + len(parse_failed) + len(opaque)
    body: list[str] = []
    nci_lines, nci_truncated = _capped_name_lines(no_column_info, "No column info")
    pf_lines, pf_truncated = _capped_name_lines(parse_failed, "Parse failed")
    # Opaque is a deliberate choice, not a failure — labelled distinctly from the others.
    opaque_lines, opaque_truncated = _capped_name_lines(
        opaque, "Not column-analyzed by choice (e.g. semantic views)"
    )
    body.extend(nci_lines)
    body.extend(pf_lines)
    body.extend(opaque_lines)
    confidence["no_column_info_truncated"] = nci_truncated
    confidence["parse_failed_truncated"] = pf_truncated
    confidence["opaque_truncated"] = opaque_truncated
    return [
        "<details>",
        f"<summary>Models that couldn't be analyzed ({total})</summary>",
        "",
        *body,
        "",
        "</details>",
    ]


# Policy gate decision → a one-glyph severity marker (amber reserved for the one blocking
# band, per DESIGN.md's rationed-colour rule).
_POLICY_DECISION_MARKER = {
    "block": "⛔",
    "warn": "🟡",
    "allow": "🟢",
}
# Order decisions block-first so the reviewer reads the reason it blocked before anything else.
_POLICY_DECISION_ORDER = ["block", "warn", "allow"]


def _reach_sample(reach: list[str]) -> str:
    """A capped, deterministic ```a`, `b` +N more`` sample of the matched reach."""
    if not reach:
        return ""
    shown = ", ".join(f"`{name}`" for name in reach[:_MAX_REACH_INLINE])
    extra = len(reach) - _MAX_REACH_INLINE
    if extra > 0:
        shown += f" +{extra} more"
    return shown


def _proof_marker(hit: dict[str, Any]) -> str:
    """The load-bearing honesty distinction for one fired rule.

    A rule that fired because a fail-safe knob resolved an UNKNOWN (missing meta / an
    evaluation error) MUST NOT read like a proven match — a confident-looking block that was
    actually "undecided" is exactly the trust erosion this feature exists to prevent. So a
    ``fired_on_unknown`` hit gets a LOUD, visually-distinct marker; a proven one a quiet check.
    """
    if hit.get("fired_on_unknown"):
        cause = hit.get("unknown_cause")
        cause_txt = "evaluation error" if cause == "error" else "meta missing"
        return f"⚠️ **fired on a fail-safe default** ({cause_txt})"
    return "✓ proven match"


def _policy_hit_line(hit: dict[str, Any]) -> str:
    """One "why this verdict" row for a fired rule: the honesty marker (proven vs fail-safe),
    the rule id, the subject change, and a capped sample of the matched reach.

    Deliberately references node NAMES only — the blast radius already has its own section
    above, and re-listing it here would duplicate (and risk contradicting) that report.
    """
    rule_id = hit.get("rule_id", "?")
    model = hit.get("change_model")
    column = hit.get("change_column")
    if model and column:
        subject = f"`{model}.{column}`"
    elif model:
        subject = f"`{model}`"
    else:
        subject = "_changeset_"  # aggregate-scope rule: no single subject
    reach_sample = _reach_sample(hit.get("matched_reach") or [])
    reach_txt = f" → reaches {reach_sample}" if reach_sample else ""
    line = f"- {_proof_marker(hit)} — **{rule_id}** on {subject}{reach_txt}"
    # when an override capped this hit, show the decision delta so the audit trail is visible.
    if hit.get("overridden"):
        original = hit.get("original_decision") or "?"
        effective = hit.get("decision") or "?"
        reason = hit.get("override_reason") or ""
        line += f" — {original} → {effective} (overridden: {reason})"
    return line


def _override_applied_line(record: dict[str, Any]) -> str:
    """One line for a honored override: verb, subject, the severity delta, and the reason."""
    model = record.get("model", "?")
    column = record.get("column")
    node = f"`{model}.{column}`" if column else f"`{model}`"
    scope_txt = " (model-level)" if record.get("scope") == "model" else ""
    downgraded_from = record.get("downgraded_from") or "?"
    downgraded_to = record.get("downgraded_to") or "?"
    return (
        f"- `{record.get('verb', '?')}` on {node}{scope_txt} — "
        f"{downgraded_from} → {downgraded_to} (reason: {record.get('reason', '')})"
    )


def _render_overrides_section(report: dict[str, Any]) -> list[str]:
    """Render the override signals: malformed-pragma warnings (loud, unfolded, FIRST — a
    dropped pragma must be noticed), honored overrides with their severity delta, ineffective
    (no-op) overrides with a fix hint, and a folded list of stale overrides to prune.

    Returns ``[]`` (rendered nothing) unless at least one of these is present.
    """
    applied = report.get("overrides") or []
    ineffective = report.get("ineffective_overrides") or []
    stale = report.get("stale_overrides") or []
    warnings = report.get("override_warnings") or []
    if not (applied or ineffective or stale or warnings):
        return []

    out: list[str] = []
    # Warnings FIRST and UNFOLDED — the audit invariant (a reasonless/malformed pragma is
    # dropped, ruling unchanged) is only useful if the author actually notices it did nothing.
    if warnings:
        out.append(f"### ⚠️ Override pragmas IGNORED ({len(warnings)})")
        out.append("")
        out.append("These pragmas were malformed and had NO effect on the ruling — fix or remove:")
        out.append("")
        out += [f"- {warning}" for warning in warnings]
        out.append("")

    if applied:
        out.append(f"### ⚠️ Overrides applied ({len(applied)})")
        out.append("")
        out.append(
            "Each acknowledged change below was downgraded with a logged reason "
            "(source line is relative to the **compiled** SQL):"
        )
        out.append("")
        out += [_override_applied_line(record) for record in applied]
        out.append("")

    if ineffective:
        out.append(f"### ⚠️ Overrides with no effect ({len(ineffective)})")
        out.append("")
        out.append("These pragmas landed on a real change but changed nothing — fix or remove:")
        out.append("")
        for record in ineffective:
            model = record.get("model", "?")
            column = record.get("column")
            node = f"`{model}.{column}`" if column else f"`{model}`"
            hint = record.get("hint", "")
            out.append(
                f"- `{record.get('verb', '?')}` on {node} — {hint} "
                f"(reason: {record.get('reason', '')})"
            )
        out.append("")

    if stale:
        out.append(
            f"<details><summary>Stale overrides ({len(stale)}) — "
            f"no matching change, prune these</summary>"
        )
        out.append("")
        for record in stale:
            model = record.get("model", "?")
            column = record.get("column")
            node = f"`{model}.{column}`" if column else f"`{model}` (model scope)"
            out.append(
                f"- `{record.get('verb', '?')}` on {node} (reason: {record.get('reason', '')})"
            )
        out += ["", "</details>", ""]

    return out


def _render_policy_section(verdict: dict[str, Any]) -> list[str]:
    """Render the policy-engine verdict: fired rules grouped by decision (block first),
    the selective build/test sets, and the notify intents for the consumer's CI to route.

    Rendered ONLY when a policy is present (the caller guards on ``report['policy_verdict']``).
    Does NOT re-list the downstream blast radius — the impact section owns that.
    """
    decision = str(verdict.get("decision", "allow"))
    marker = _POLICY_DECISION_MARKER.get(decision, "🟢")
    hits = verdict.get("hits") or []
    out: list[str] = [f"### {marker} Policy verdict — {decision.upper()}", ""]

    if decision == "block":
        # A block must state its EXIT, not just the obstacle: reframe it as
        # "block-until". The gate re-runs on every push and clears itself once the change stops
        # tripping the rules below — so a reviewer sees the release path, not a dead end. This is
        # pure messaging over the existing verdict; no override input is consulted.
        out += [
            (
                "> **Blocked until the change stops tripping the rules below.** This gate re-runs on "
                "every push and clears itself — no manual override needed. Clear it by any of: "
                "reverting or proving-equivalent the breaking change; evolving the downstream model / "
                "schema to absorb it; or stopping it from reaching the flagged object."
            ),
            "",
        ]

    if not hits:
        out.append("No policy rule fired against this change.")
    else:
        # "Why this verdict" — one row per fired rule, grouped by contribution (block first).
        # The honesty marker on each row states whether the rule PROVED its match or fired on a
        # fail-safe default, so a fail-safe block never reads as a confident one.
        out += ["**Why this verdict** — the rules that fired, and on what:", ""]
        by_decision: dict[str, list[dict[str, Any]]] = {}
        for hit in hits:
            by_decision.setdefault(str(hit.get("decision", "allow")), []).append(hit)
        for band in _POLICY_DECISION_ORDER:
            band_hits = by_decision.get(band)
            if not band_hits:
                continue
            band_marker = _POLICY_DECISION_MARKER.get(band, "🟢")
            out.append(f"**{band_marker} {band.capitalize()} ({len(band_hits)})**")
            out.append("")
            out += [_policy_hit_line(hit) for hit in band_hits]
            out.append("")

    build_set = verdict.get("build_set") or []
    test_set = verdict.get("test_set") or []
    if build_set or test_set:
        if build_set:
            out.append(
                f"**Selective build set ({len(build_set)}):** "
                + ", ".join(f"`{name}`" for name in build_set)
            )
        if test_set:
            out.append(
                f"**Selective test set ({len(test_set)}):** "
                + ", ".join(f"`{name}`" for name in test_set)
            )
        out.append("")

    notifications = verdict.get("notifications") or []
    if notifications:
        out.append(f"**Notifications ({len(notifications)})** — routed by your CI:")
        out.append("")
        for note in notifications:
            channel = note.get("channel") or "?"
            target = note.get("target") or "?"
            message = note.get("message") or ""
            out.append(f"- `{channel}` → **{target}**: {message}")
        out.append("")

    # Honesty coverage footer — LOUD (not folded into a <sub>): anything the policy left
    # UNDECIDED (missing meta / unresolved reach) resolved via a fail-safe default and MUST NOT
    # be silently folded into a clean pass. Shown whenever either counter is > 0.
    unresolved = int(verdict.get("unresolved_reach_count", 0) or 0)
    skipped = int(verdict.get("skipped_missing_meta", 0) or 0)
    coverage_bits: list[str] = []
    if skipped:
        coverage_bits.append(f"{_plural(skipped, 'column')} undecided (missing meta)")
    if unresolved:
        reach_word = "reach" if unresolved == 1 else "reaches"
        coverage_bits.append(f"{unresolved} {reach_word} unresolved")
    if coverage_bits:
        out += [
            "> ⚠️ **Coverage:** " + ", ".join(coverage_bits) + " — these did NOT count as safe.",
            "",
        ]

    out += _render_unproven_conditions(verdict)

    return out


def _render_unproven_conditions(verdict: dict[str, Any]) -> list[str]:
    """Suppressed-unknown telemetry (additive section, issue #124): warn-rule conditions that a
    ``fail_closed`` knob would have fired were the rule blocking. An all-warn pilot policy must
    never read clean while block mode would fail closed here — so the count line is loud and the
    per-rule list is folded (visibility without warning-flood)."""
    unproven = verdict.get("unproven") or []
    if not unproven:
        return []
    out: list[str] = [
        "> ⚠️ **"
        + _plural(len(unproven), "unproven warn-rule condition")
        + " (unknown metadata under `fail_closed`)** — suppressed, not fired; block mode "
        "would fail closed here.",
        "",
        f"<details><summary>Unproven conditions ({len(unproven)})</summary>",
        "",
    ]
    for record in unproven:
        rule_id = record.get("rule_id", "?")
        model = record.get("change_model")
        column = record.get("change_column")
        if model and column:
            subject = f"`{model}.{column}`"
        elif model:
            subject = f"`{model}`"
        else:
            subject = "_changeset_"
        cause = record.get("unknown_cause")
        cause_txt = "evaluation error" if cause == "error" else "meta missing"
        leaves = record.get("unknown_leaves") or []
        leaves_txt = " — unknown: " + ", ".join(f"`{leaf}`" for leaf in leaves) if leaves else ""
        out.append(f"- **{rule_id}** on {subject} ({cause_txt}){leaves_txt}")
    out += ["", "</details>", ""]
    return out


def _truncate_expr(raw: str, limit: int = 120) -> str:
    """One-line, length-capped rendering of a defining expression for the explain view."""
    collapsed = " ".join(raw.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def render_changeset_markdown(report: dict[str, Any], explain: bool = False) -> str:
    """Render a changeset impact report (from ``build_changeset_report``) as Markdown.

    The compact semantic reason a column was flagged is shown by default, so the default gate
    explains itself. When ``explain`` is True, each changed column is additionally annotated
    with a compact ``base → head`` expression line (the fuller trace). The machine-readable
    JSON always carries the full explanation regardless of this flag.
    """
    changeset = report.get("changeset", {})
    summary = report.get("summary", {})

    out: list[str] = ["## Column-level impact of this change", ""]

    total_changes = changeset.get("total_changes", 0)
    if not total_changes:
        out.append(
            "✅ **No column changes detected** between base and head — nothing downstream to check."
        )
        if _structural_checks_skipped(report):
            out += ["", "<sub>" + _STRUCTURAL_SKIP_NOTE + "</sub>"]
        degraded_note = _structural_diff_degraded_note(report)
        if degraded_note:
            out += ["", "<sub>" + degraded_note + "</sub>"]
        out.append("")
        out.append(_CREDIT_LINE)
        out.append("")
        return "\n".join(out)

    by_change = report.get("by_change", [])
    changed_nodes = sorted({(c.get("model", "?"), c.get("column", "?")) for c in by_change})
    # (model, column) -> the explain block a logic change carried, so `--explain` can annotate
    # each changed column with WHY it was flagged. Structural changes carry no explain block.
    explain_by_node: dict[tuple[str, str], dict[str, Any]] = {}
    for change in by_change:
        block = change.get("explain")
        if isinstance(block, dict):
            explain_by_node[(change.get("model", "?"), change.get("column", "?"))] = block
    by_kind = changeset.get("by_kind", {})

    affected_columns = report.get("affected_columns", [])
    critical = [c for c in affected_columns if c.get("severity") == "critical"]
    filtered = [c for c in affected_columns if c.get("severity") == "filter"]
    passthrough = [c for c in affected_columns if c.get("severity") not in ("critical", "filter")]

    exposures = report.get("affected_exposures", [])
    apps = [e for e in exposures if (e.get("type") or "").lower() != "dashboard"]
    dashboards = [e for e in exposures if (e.get("type") or "").lower() == "dashboard"]

    derived_by_model = _group_by_model(critical)
    filter_by_model = {c.get("model", "?"): c for c in filtered}
    review_models = sorted(set(derived_by_model) | set(filter_by_model))
    confidence = report.get("confidence") or {}
    breaks = report.get("provable_breaks") or []

    # --- Verdict banner (the 5-second read) ----------------------------------------------
    # A provable break (a dbt test the change orphans) is the only BLOCK-worthy signal; it
    # outranks the heuristic review/safe banner below.
    if breaks:
        out.append(
            f"> ⛔ **Blocked — {_plural(len(breaks), 'provable break')}.** "
            f"This change orphans {_plural(len(breaks), 'dbt test')} that will fail on the "
            f"next `dbt build`." + _confidence_floor_clause(confidence)
        )
    elif review_models:
        if len(changed_nodes) == 1:
            subject = f"**`{changed_nodes[0][0]}.{changed_nodes[0][1]}`**"
        elif changed_nodes:
            subject = f"**{_plural(len(changed_nodes), 'column')}**"
        else:
            subject = f"**{_plural(total_changes, 'column')}**"
        reach_bits: list[str] = []
        if apps:
            reach_bits.append(f"**{_plural(len(apps), 'automation')}**")
        if dashboards:
            reach_bits.append(f"**{_plural(len(dashboards), 'dashboard')}**")
        reach = (" and reaches " + " + ".join(reach_bits)) if reach_bits else ""
        out.append(
            f"> 🔴 **Review required.** Changing {subject} recomputes logic in "
            f"**{_plural(len(review_models), 'model')}**{reach}."
            + _confidence_floor_clause(confidence)
        )
    else:
        out.append(
            "> 🟢 **Looks safe.** The change is only referenced downstream, not recomputed — "
            "no model's output logic changes." + _confidence_floor_clause(confidence)
        )
    out.append("")

    # --- What changed --------------------------------------------------------------------
    if len(by_kind) == 1 and total_changes == 1:
        kind_txt = _kind_label(next(iter(by_kind)))
    else:
        kind_txt = ", ".join(f"{_kind_label(k)}: {v}" for k, v in sorted(by_kind.items()))
    out.append(f"**Changed:** {_plural(total_changes, 'column')} — {kind_txt}")
    if changed_nodes:
        rows: list[str] = []
        for model, column in changed_nodes:
            rows.append(f"- `{model}.{column}`")
            # The compact semantic reason ("why was this flagged?") shows BY DEFAULT so the
            # free/default gate explains itself; ``--explain`` EXPANDS it with the base→head
            # expression trace. Structural changes carry no explain block (nothing to add).
            block = explain_by_node.get((model, column))
            if block is not None:
                reason = block.get("reason")
                if reason:
                    rows.append(f"  - _why:_ {reason}")
                if explain:
                    base = (block.get("base") or "").strip()
                    head = (block.get("head") or "").strip()
                    if base or head:
                        rows.append(f"  - `{_truncate_expr(base)}` → `{_truncate_expr(head)}`")
        if len(changed_nodes) <= 10:
            out += [""] + rows
        else:
            out += [
                "",
                f"<details><summary>Show {_plural(len(changed_nodes), 'changed column')}</summary>",
                "",
            ]
            out += rows + ["", "</details>"]
    out.append("")
    out.append(
        f"Reaches **{_plural(summary.get('affected_models', 0), 'model')}** · "
        f"**{_plural(summary.get('affected_columns', 0), 'column')}** · "
        f"**{_plural(summary.get('affected_exposures', 0), 'exposure')}** downstream."
    )
    out.append("")

    # --- ⛔ Provable breaks — the block-worthy diagnostics --------------------------------
    if breaks:
        out.append(f"### ⛔ Provable breaks ({len(breaks)})")
        out.append("")
        out.append(
            "These fail on the next `dbt build` — a dbt test now targets a column this "
            "change removed. Fix the test or restore the column."
        )
        out.append("")
        out += [_format_break(b) for b in breaks]
        out.append("")

    # --- 🔴 Output changes — the scannable table -----------------------------------------
    if review_models:
        out.append(f"### 🔴 Check these — their output changes ({len(review_models)})")
        out.append("")
        out.append("| Model | What changes | How |")
        out.append("|---|---|---|")
        folds: list[str] = []
        for model in review_models:
            what_bits: list[str] = []
            how_bits: list[str] = []
            derived_cols = sorted(
                derived_by_model.get(model, []), key=lambda c: c.get("column", "")
            )
            if derived_cols:
                what_bits.append(", ".join(f"`{c.get('column', '')}`" for c in derived_cols))
                how_bits.append("value recomputed")
                for col in derived_cols:
                    raw = (col.get("sql_expression") or "").strip()
                    if raw:
                        sql, note = _truncate_sql(raw)
                        folds += [
                            f"<details><summary>Show new logic — <code>{model}.{col.get('column', '')}</code></summary>",
                            "",
                            "```sql",
                            sql,
                            "```",
                        ]
                        if note:
                            folds += ["", note]
                        folds += ["</details>", ""]
            if model in filter_by_model:
                what_bits.append("rows kept may change")
                how_bits.append("filtered/joined on this column")
                cond = (filter_by_model[model].get("sql_expression") or "").strip()
                if cond:
                    sql, note = _truncate_sql(cond)
                    folds += [
                        f"<details><summary>Show filter — <code>{model}</code></summary>",
                        "",
                        "```sql",
                        sql,
                        "```",
                    ]
                    if note:
                        folds += ["", note]
                    folds += ["</details>", ""]
            out.append(f"| `{model}` | {'; '.join(what_bits)} | {' · '.join(how_bits)} |")
        out.append("")
        if folds:
            out += folds

    # --- ⚠️ Business-facing exposures (apps surfaced above dashboards) --------------------
    if exposures:
        rollup_bits: list[str] = []
        if dashboards:
            rollup_bits.append(_plural(len(dashboards), "dashboard"))
        if apps:
            rollup_bits.append(_plural(len(apps), "application"))
        out.append(f"### ⚠️ Business-facing exposures ({len(exposures)})")
        out.append("")

        def _fmt(exp: dict[str, Any]) -> str:
            name = exp.get("name", "?")
            url = exp.get("url")
            head = f"- **[{name}]({url})**" if url else f"- **{name}**"
            # Cross-boundary provenance: a dashboard reached PAST dbt's edge via the
            # Metabase artifact is tagged so a reviewer knows it's not a dbt-native exposure,
            # and whether the reach is column-precise or coarse (table-grain).
            tag = ""
            if exp.get("source") == "metabase":
                precision = exp.get("precision")
                grain = " (table-grain)" if precision == "table" else ""
                tag = f" — via **Metabase**{grain}"
                # F4: name WHICH column(s) of the dashboard the change hits, so the reviewer
                # goes straight to the field instead of hunting the whole board. Distinct
                # `model.column`, deterministically ordered, capped. Empty on a table-grain
                # reach (no proven column) — the "(table-grain)" tag already says why.
                via = exp.get("via_columns") or []
                cols = sorted({f"{v['model']}.{v['column']}" for v in via if isinstance(v, dict)})
                if cols:
                    shown = ", ".join(f"`{c}`" for c in cols[:_MAX_VIA_COLUMNS_INLINE])
                    extra = len(cols) - _MAX_VIA_COLUMNS_INLINE
                    if extra > 0:
                        shown += f" +{extra} more"
                    tag += f" · affects {shown}"
            return head + tag + _owner_suffix(exp)

        if apps:
            out.append(
                f"**{_plural(len(apps), 'application')}** — operational, may act on this data:"
            )
            out += [_fmt(e) for e in apps]
            out.append("")
        if dashboards:
            if len(dashboards) > _MAX_DASHBOARDS_INLINE:
                out.append(f"<details><summary>{_plural(len(dashboards), 'dashboard')}</summary>")
                out += [""] + [_fmt(e) for e in dashboards] + ["", "</details>"]
            else:
                if apps:
                    out.append(f"**{_plural(len(dashboards), 'dashboard')}:**")
                out += [_fmt(e) for e in dashboards]
        out.append("")

    # --- 🟢 Pass-through references (folded: low risk) -----------------------------------
    if passthrough:
        grouped = _group_by_model(passthrough)
        out.append(
            f"<details><summary>🟢 Safe — passes through unchanged "
            f"({_plural(len(passthrough), 'column')} in {_plural(len(grouped), 'model')})</summary>"
        )
        out.append("")
        for model, cols in grouped.items():
            names = ", ".join(
                f"`{c.get('column', '')}`" for c in sorted(cols, key=lambda c: c.get("column", ""))
            )
            out.append(f"- **`{model}`**: {names}")
        out += ["", "</details>", ""]

    # --- ⚠️ Overrides applied / ignored / stale -------------------------------------
    out += _render_overrides_section(report)

    # --- 🛡️ Policy verdict (only when a policy ran) --------------------------------------
    policy_verdict = report.get("policy_verdict")
    if isinstance(policy_verdict, dict):
        out += _render_policy_section(policy_verdict)

    # --- Footer: confidence + coverage (small, plain, honest) ----------------------------
    footer: list[str] = []
    if _structural_checks_skipped(report):
        footer.append(_STRUCTURAL_SKIP_NOTE)
    degraded_note = _structural_diff_degraded_note(report)
    if degraded_note:
        footer.append(degraded_note)
    # Break detection is a lower bound: tests it couldn't attribute to a column are never
    # checked, so a clean (SAFE/REVIEW) ruling is not proof no test breaks.
    unattributable = (report.get("verdict_coverage") or {}).get("unattributable_tests", 0)
    if unattributable:
        footer.append(
            f"Break detection skipped {_plural(unattributable, 'dbt test')} it couldn't tie "
            f"to a column (singular/custom tests); a clean ruling is a lower bound."
        )
    unanalyzable_disclosure: list[str] = []
    if confidence:
        if confidence.get("level") == "full":
            footer.append(
                f"**Confidence: full** — all {_plural(confidence.get('reachable_models', 0), 'downstream model')} "
                f"were analyzed."
            )
        else:
            n = confidence.get("unanalyzable_models", 0)
            partial_edges = confidence.get("partial_edges", 0)
            opaque = confidence.get("opaque", 0)
            reachable = confidence.get("reachable_models", 0)
            # Additional degradation clauses, kept distinct so the reviewer can tell a parser
            # *failure* apart from a deliberate *choice* not to analyze (opaque, e.g. semantic
            # views). Both are rebuilt rather than proven safe to skip.
            extra_clauses: list[str] = []
            if partial_edges:
                extra_clauses.append(f"{partial_edges} more carried unresolved column edges")
            if opaque:
                extra_clauses.append(
                    f"{opaque} more are not column-analyzed by choice "
                    f"(unparseable SQL, e.g. semantic views)"
                )
            extra = ("; " + "; ".join(extra_clauses)) if extra_clauses else ""
            if n:
                footer.append(
                    f"**Confidence: partial** — {n} of {_plural(reachable, 'downstream model')} "
                    f"couldn't be analyzed{_confidence_reason_words(confidence)}"
                    + extra
                    + ", so the impact above may be incomplete."
                )
            else:
                # Degraded purely by unresolved column edges and/or opaque nodes — analyzable at
                # most at the column-list level (or deliberately not at all), so these are rebuilt
                # rather than proven safe to skip, never dropped.
                joined = "; ".join(extra_clauses) if extra_clauses else "some downstream models"
                footer.append(
                    f"**Confidence: partial** — {joined} "
                    f"(of {_plural(reachable, 'downstream model')}), "
                    f"so they were rebuilt rather than proven safe to skip."
                )
            unanalyzable_disclosure = _render_unanalyzable_names(confidence)
    coverage = report.get("coverage")
    if coverage and not coverage.get("complete", False):
        footer.append(
            f"Parser reached {coverage.get('parsed_ok', 0):,} of "
            f"{coverage.get('models_in_manifest', 0):,} project models "
            f"({coverage.get('parse_failed', 0)} parse-failed)."
        )
    unresolved = summary.get("unresolved_changes", 0)
    if unresolved:
        footer.append(
            f"{_plural(unresolved, 'change')} could not be traced downstream (e.g. a removed column)."
        )
    # Cross-boundary (Metabase) reach honesty: a stale or coarse snapshot must read as such so a
    # fail-closed policy block driven by it is explainable, never a fabricated certainty.
    metabase = report.get("metabase")
    if isinstance(metabase, dict) and metabase.get("level") != "absent":
        reached = metabase.get("dashboards_reached", 0)
        table_only = metabase.get("cards_table_only", 0)
        bits = f"Metabase reach: {_plural(reached, 'dashboard')}"
        if table_only:
            bits += f" ({table_only} table-grain, not column-precise)"
        if metabase.get("stale"):
            age = metabase.get("snapshot_age_hours")
            age_txt = f" ({age:.0f}h old)" if isinstance(age, (int, float)) else ""
            bits += f" — ⚠️ snapshot is STALE{age_txt}; treat this reach as degraded"
        footer.append(bits + ".")
    if footer:
        out += ["<sub>" + " · ".join(footer) + "</sub>", ""]
    if unanalyzable_disclosure:
        out += unanalyzable_disclosure + [""]

    out.append(_CREDIT_LINE)
    out.append("")

    return "\n".join(out)
