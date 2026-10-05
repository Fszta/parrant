"""``policy init`` — a manifest-introspecting scaffold that removes the blank-page adoption
cliff by writing a heavily-commented, safe-by-construction starter ``policy.yml`` INTO the
user's repo (code they own, not a tool-managed preset).

The scaffold is *manifest-aware* on purpose: it emits an
ENABLED rule only for a tool-owned signal the scan confirmed exists (column-targeted tests →
``provable-break-block``; exposures → ``exposure-guard``), and offers every discovered dbt-meta
key as a COMMENTED template prefixed with its REAL coverage. That is what keeps the generated
file green on day one instead of rage-blocking every PR — the exact failure the feature exists
to prevent.

This module is pure authoring: a read-only scan (:func:`scan_project`) plus a string-templated
YAML emitter (:func:`emit_policy_yaml`). It performs NO engine changes — it only writes YAML
the existing engine already understands. The emitter builds a list of lines rather than calling
``yaml.dump`` because PyYAML cannot emit comments, and the whole value of the scaffold is its
comments; the ENABLED subset is hand-verified valid YAML and round-tripped through
``parse_policy`` in the integration test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from parrant.lineage.service import LineageService
from parrant.models.schema import MetaKeyCoverage, PolicyInitScan

# The default output path is EXACTLY the one ``load_policy`` auto-resolves, so the generated
# file is picked up by ``impact``/``policy test`` with no ``--policy`` flag.
DEFAULT_OUTPUT_PATH = "./parrant.policy.yml"

# Readability cap: on a large manifest (e.g. 1000+ models) emitting a commented template for
# EVERY one-off meta key becomes a wall of noise that undercuts the "remove the blank page"
# goal. Show the most-covered keys; the numbers shown stay real, and the omitted-count note
# keeps the truncation honest.
_META_TEMPLATE_CAP = 12


def _flatten_meta_keys(meta: dict[str, Any], _prefix: str = "") -> list[str]:
    """Yield the dotted leaf-key paths (``a.b.c``) of a (possibly nested) meta dict.

    Nested dicts are recursed so a nested key still gets an accurate histogram entry AND a
    valid template key that matches the engine's dotted ``_dotted_get`` lookup. Only LEAF paths
    are yielded (an intermediate dict is never itself a template key), because a presence
    operator against an intermediate dict is rarely what an author means. An empty dict value is
    treated as a leaf so it still surfaces as a (present-but-empty) key rather than vanishing.
    """
    keys: list[str] = []
    for key, value in meta.items():
        dotted = f"{_prefix}{key}"
        if isinstance(value, dict) and value:
            keys.extend(_flatten_meta_keys(value, f"{dotted}."))
        else:
            keys.append(dotted)
    return keys


def _histogram(counts: dict[str, int], total: int) -> list[MetaKeyCoverage]:
    """Build the coverage rows, sorted most-covered-first then by key (stable, deterministic)."""
    rows = [MetaKeyCoverage(key=key, n_present=n, total=total) for key, n in counts.items()]
    rows.sort(key=lambda row: (-row.n_present, row.key))
    return rows


def scan_project(registry: Any) -> PolicyInitScan:
    """Read-only scan of a loaded registry into a :class:`PolicyInitScan` (the manifest scan).

    Iterates every model × column pair to count column-targeted generic tests and build the
    model- and column-meta coverage histograms; counts exposures. O(models × columns) via
    ``get_column_tests`` per pair — fine for the one-time-per-repo ``policy init`` command.
    Everything is derived from the registry accessors; nothing is guessed.
    """
    models = registry.get_models()
    total_models = len(models)
    total_columns = 0
    column_test_count = 0
    models_with_column_tests = 0
    model_meta_counts: dict[str, int] = {}
    column_meta_counts: dict[str, int] = {}
    models_with_grants = 0

    for name, model in models.items():
        model_has_column_test = False
        for column in model.columns:
            total_columns += 1
            tests = registry.get_column_tests(name, column)
            if tests:
                column_test_count += len(tests)
                model_has_column_test = True
            for key in _flatten_meta_keys(registry.get_column_dbt_meta(name, column)):
                column_meta_counts[key] = column_meta_counts.get(key, 0) + 1
        if model_has_column_test:
            models_with_column_tests += 1
        for key in _flatten_meta_keys(registry.get_model_dbt_meta(name)):
            model_meta_counts[key] = model_meta_counts.get(key, 0) + 1
        if _has_select_grant(registry.get_model_config(name)):
            models_with_grants += 1

    exposure_count = len(registry.get_exposures())

    return PolicyInitScan(
        total_models=total_models,
        total_columns=total_columns,
        column_test_count=column_test_count,
        models_with_column_tests=models_with_column_tests,
        exposure_count=exposure_count,
        models_with_grants=models_with_grants,
        model_meta_keys=_histogram(model_meta_counts, total_models),
        column_meta_keys=_histogram(column_meta_counts, total_columns),
    )


def _has_select_grant(config: dict[str, Any]) -> bool:
    """True when a model's resolved dbt ``config`` declares a non-empty ``grants.select``.

    Mirrors the engine's ``config.grants.select`` dotted lookup: the roles a model grants SELECT
    to. A missing ``grants`` block, a missing ``select`` key, or an empty/None reader list all
    count as no grant (nothing to over-expose), so the config-axis template is only offered when a
    real grant exists. Any non-list ``select`` (a bare string, say) is treated as present — the
    engine surfaces it raw and would flag it as a shape error, which is still worth a template.
    """
    grants = config.get("grants")
    if not isinstance(grants, dict):
        return False
    select = grants.get("select")
    if select is None:
        return False
    if isinstance(select, (list, tuple, set)):
        return len(select) > 0
    return True


# --- YAML emitter (string-templated; comments cannot survive yaml.dump) ------


def _header_lines() -> list[str]:
    """The top comment block: ownership, the ``policy test`` pointer, and the two footguns.

    Deliberately never writes the literal permissive-default token: this scaffold only ever uses
    the safe closed default, and naming the permissive mode as a copy-pasteable value would
    invite exactly the fail-open gate the honesty brand forbids (see the emit invariant)."""
    return [
        "# ============================================================================",
        "# parrant policy — generated by `parrant policy init`",
        "#",
        "# THIS FILE IS YOURS. It was scaffolded from your manifest + catalog, keyed only",
        "# to signals the scan confirmed exist. Edit it freely — it is code you own, not a",
        "# tool-managed preset that can silently change under you.",
        "#",
        "# Before you arm anything new:",
        "#   Run `parrant policy test --last 20` to replay this policy over your",
        "#   recent history and see what each rule WOULD have done — do that BEFORE you flip",
        "#   a `warn` rule to `block`, or uncomment any meta template below.",
        "#",
        "# Two footguns, stated plainly:",
        "#   * Presence operators — `is_true`, `is_false`, `exists`, `absent` — are SAFE on a",
        "#     missing meta key: they resolve FALSE (they just don't match). A model that never",
        "#     declared the key simply won't match, so a presence rule never over-fires.",
        "#   * Value comparisons — `eq`, `in`, `gt`, `matches`, … — resolve UNKNOWN on a missing",
        "#     key. Under `on_missing_meta: fail_closed` (below), a BLOCK rule then fires on",
        "#     EVERYTHING that lacks the key — the rage-block footgun. Prefer presence operators;",
        "#     only reach for a value comparison after `policy test` proves your key coverage.",
        "#",
        "# Fail-safe glossary (how an UNDECIDABLE rule resolves):",
        "#   * fail_closed — treat \"can't prove it's safe\" as unsafe: a BLOCK rule fires. This is",
        "#                   the safe default, and the only mode this scaffold uses.",
        "#   * skip        — drop the rule for that change entirely (no gate contribution).",
        "#   A third, permissive mode (never fires on UNKNOWN) exists but is deliberately NOT used",
        "#   here: a gate that opens whenever it is unsure is not a gate.",
        "# ============================================================================",
    ]


def _defaults_lines() -> list[str]:
    return [
        "defaults:",
        "  # Anything we cannot prove safe is treated as unsafe. Safe here because every ENABLED",
        "  # rule below is a tool-owned structural fact that never resolves UNKNOWN; this default",
        "  # only governs the meta templates, which ship commented-out.",
        "  on_missing_meta: fail_closed",
        "  # Same closed posture for a genuine operator/type error, kept as a SEPARATE knob so a",
        "  # type bug is never masked by the missing-meta default.",
        "  on_error: fail_closed",
    ]


def _comment_out(yaml_lines: list[str]) -> list[str]:
    """Comment out a block of 2-space-indented YAML lines, preserving relative indentation."""
    return [f"  # {line[2:]}" if line.startswith("  ") else f"# {line}" for line in yaml_lines]


# The raw (2-space-indented) YAML body of each tool-owned rule, kept separate from its
# explanatory comment so the disabled variant can comment JUST the YAML without doubling `#`.
_PROVABLE_BREAK_BLOCK_YAML = [
    "  - id: provable-break-block",
    "    scope: aggregate",
    "    predicate: { structural: { fact: provable_test_break } }",
    "    action: [{ type: block }]",
]
_EXPOSURE_GUARD_YAML = [
    "  - id: exposure-guard",
    "    predicate: { structural: { fact: touches_exposure } }",
    "    action: [{ type: warn }]",
]


def _provable_break_block_lines(enabled: bool) -> list[str]:
    """The ``provable-break-block`` rule. Safe-by-construction: ``provable_test_break`` is a pure
    structural fact (always TRUE/FALSE, never UNKNOWN) evaluated at aggregate scope, so this
    block can only ever fire on a real, offline-verifiable breakage — never on a fail-safe
    default. That is why it is armed on day one."""
    if enabled:
        return [
            "  # provable-break-block — a change that removes/renames a column a dbt test targets",
            "  # will fail the next `dbt build`. This is a PROVEN break (a structural fact, never",
            "  # UNKNOWN), so it is safe to block on day one: it fires only on a real breakage.",
        ] + _PROVABLE_BREAK_BLOCK_YAML
    return [
        "  # provable-break-block — NOT emitted: the scan found no column-targeted generic tests,",
        "  # so there is nothing to prove a break against yet. Add dbt tests and re-run",
        "  # `policy init` (or uncomment below once you have them):",
    ] + _comment_out(_PROVABLE_BREAK_BLOCK_YAML)


def _exposure_guard_lines(enabled: bool) -> list[str]:
    """The ``exposure-guard`` rule — WARN (not block) when a change reaches an exposure.

    ``touches_exposure`` CAN be UNKNOWN on unresolved reach, but this is a non-blocking WARN
    rule, and under ``fail_closed`` a non-blocking rule does NOT fire on UNKNOWN — so it never
    manufactures a spurious warning. Safe to enable whenever exposures exist."""
    if enabled:
        return [
            "  # exposure-guard — warn when a change reaches a dbt exposure (a dashboard /",
            "  # downstream consumer). WARN, not block: it flags for review without gating merge.",
        ] + _EXPOSURE_GUARD_YAML
    return [
        "  # exposure-guard — NOT emitted: the scan found no exposures to guard. Declare exposures",
        "  # in dbt and re-run `policy init` (or uncomment below once you have them):",
    ] + _comment_out(_EXPOSURE_GUARD_YAML)


def _meta_template_lines(row: MetaKeyCoverage, subject: str) -> list[str]:
    """A single COMMENTED, meta-keyed reach template for one discovered key.

    Prefixed by the REAL coverage from the scan and using a PRESENCE operator (``is_true``) —
    never a value-comparison, never enabled, never a block. ``is_true`` is a *total* operator
    (it resolves FALSE, never UNKNOWN, on a missing or falsy key), so uncommenting it as WARN is
    safe under ``fail_closed``. The template ``id`` is a fixed-shape literal derived only from
    the sanitized key, never interpolating raw meta into an enabled rule.
    """
    coverage = f"present on {row.n_present}/{row.total} {subject}s ({row.pct}%)"
    rule_id = f"reach-{_slug(row.key)}-{subject}"
    reach_kind = "model" if subject == "model" else "column"
    return [
        f"  # meta.{row.key} {coverage}. Uncomment to WARN when a change reaches a {subject}",
        f"  # flagged `{row.key}`. NOTE: `is_true` matches only when the key is PRESENT AND",
        f"  # truthy — a {subject} WITHOUT the key (or with `{row.key}: false`) simply won't",
        "  # match, so this never rage-blocks. For a string-valued key you may want `eq`/`in`",
        "  # instead — but only after `policy test` confirms coverage (a value comparison",
        "  # resolves UNKNOWN on models missing the key and can over-fire under fail_closed).",
        f"  # - id: {rule_id}",
        "  #   predicate:",
        "  #     reach:",
        f"  #       kind: {reach_kind}",
        f"  #       where: {{ meta: {{ key: {row.key}, op: is_true }} }}",
        "  #   action: [{ type: warn }]",
    ]


def _slug(key: str) -> str:
    """Sanitize a dotted meta key into a safe rule-id fragment (letters/digits/dashes only)."""
    out = []
    for char in key.lower():
        out.append(char if char.isalnum() else "-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "meta"


def _footer_lines() -> list[str]:
    return [
        "",
        "# --- Next steps -------------------------------------------------------------",
        "# 1. Run `parrant policy test --last 20` to see what these rules WOULD have",
        "#    done over your recent history (the enabled rules are already safe-by-construction).",
        "# 2. Uncomment a meta template above once its coverage is high enough to be useful.",
        "# 3. Flip a `warn` rule to `block` only after `policy test` shows it fires exclusively on",
        "#    real matches — not on fail-safe UNKNOWNs.",
        "# See the blessed copy-paste recipes: docs/decision-engine/policy-recipes.md",
    ]


def emit_policy_yaml(scan: PolicyInitScan) -> str:
    """Render a :class:`PolicyInitScan` into a heavily-commented, safe-by-construction policy.

    Structure: header footguns → ``version`` → ``defaults`` (closed) → ENABLED tool-owned rules
    (only those whose signal the scan confirmed) → COMMENTED meta templates with real coverage →
    footer. If neither tests nor exposures exist, emits ``rules: []`` plus an honest note that
    nothing could be safely auto-enabled — never a fake guard.

    Invariant: the string ``fail_open`` is never emitted (an open-when-unsure gate is not a gate).
    """
    lines: list[str] = []
    lines.extend(_header_lines())
    lines.append("")
    lines.append("version: 1")
    lines.append("")
    lines.extend(_defaults_lines())
    lines.append("")

    enabled_blocks: list[list[str]] = []
    if scan.tests_present:
        enabled_blocks.append(_provable_break_block_lines(enabled=True))
    if scan.exposures_present:
        enabled_blocks.append(_exposure_guard_lines(enabled=True))

    if enabled_blocks:
        lines.append("rules:")
        lines.append("  # --- Enabled: tool-owned structural signals (cannot silently no-op) ---")
        for block in enabled_blocks:
            lines.append("")
            lines.extend(block)
    else:
        lines.append(
            "# Nothing could be safely auto-enabled: the scan found no column-targeted generic"
        )
        lines.append(
            "# tests and no exposures. This is an HONEST empty policy, not a fake guard. Add dbt"
        )
        lines.append(
            "# tests / exposures and re-run `policy init`, or uncomment a template below once you"
        )
        lines.append("# have a real signal to gate on.")
        lines.append("rules: []")

    # Disabled tool-owned rules, shown commented so the user knows why they were withheld.
    disabled_blocks: list[list[str]] = []
    if not scan.tests_present:
        disabled_blocks.append(_provable_break_block_lines(enabled=False))
    if not scan.exposures_present:
        disabled_blocks.append(_exposure_guard_lines(enabled=False))
    for block in disabled_blocks:
        lines.append("")
        lines.extend(block)

    # Commented meta templates — one per discovered key (model keys then column keys), capped for
    # readability with an honest omitted-count note. All coverage numbers are real (from the scan).
    lines.extend(_meta_section_lines(scan))

    # Commented config-axis (PII over-grant) template — only when the scan found real dbt grants.
    lines.extend(_config_section_lines(scan))

    lines.extend(_footer_lines())

    text = "\n".join(lines) + "\n"
    # Honesty invariant, enforced in code: never emit an open-when-unsure default.
    assert "fail_open" not in text, "policy init must never emit a fail_open default"
    return text


def _meta_section_lines(scan: PolicyInitScan) -> list[str]:
    """Emit the commented meta-template section (empty when the scan found no meta keys)."""
    if not scan.model_meta_keys and not scan.column_meta_keys:
        return []
    lines: list[str] = [
        "",
        "  # --- Commented meta templates (uncomment AFTER `policy test`) ---------",
        "  # These are keyed to dbt `meta` the scan actually found. Each is prefixed with its real",
        "  # coverage so you can judge whether it is worth arming. All ship as WARN + presence-op,",
        "  # so uncommenting one can never rage-block.",
    ]
    lines.extend(_capped_templates(scan.model_meta_keys, "model"))
    lines.extend(_capped_templates(scan.column_meta_keys, "column"))
    return lines


def _config_section_lines(scan: PolicyInitScan) -> list[str]:
    """Emit the commented config-axis (PII over-grant) template, or nothing.

    Only offered when the scan found at least one model declaring ``config.grants.select`` — the
    signal that a grants-based rule is even expressible here. Ships COMMENTED and as WARN, exactly
    like the meta templates. It composes ``inferred_meta.pii`` (PII inherited over lineage) with
    ``config.grants.select not_subset_of [...]``. Safe-by-construction: ``not_subset_of`` is a set
    operator, and on the ``config`` axis a missing ``grants.select`` resolves to the EMPTY SET
    (present, not UNKNOWN), so a model that grants to nobody does not fire — no rage-block.
    """
    if not scan.grants_present:
        return []
    pct = round(100 * scan.models_with_grants / scan.total_models) if scan.total_models else 0
    coverage = f"declared on {scan.models_with_grants}/{scan.total_models} models ({pct}%)"
    return [
        "",
        "  # --- Commented config-axis template: PII over-grant guard --------------",
        f"  # `config.grants.select` {coverage}. This reads dbt's OWN resolved grants — no manifest",
        "  # patch. Uncomment to WARN when a change touches a PII column (inferred over lineage) on",
        "  # a model that grants SELECT to a role OUTSIDE your allowlist. Safe by construction:",
        "  # `not_subset_of` is a SET operator, and a model with NO grants resolves to the EMPTY SET",
        "  # -> it does NOT fire (only a model that actually over-grants matches).",
        "  # NOTE: grant role names are surfaced RAW (no env-suffix normalization) — list the exact",
        "  # role names your project grants. Seed `pii` on your sources first so `inferred_meta`",
        "  # resolves. See docs/decision-engine/policy-recipes.md (PII over-grant guard).",
        "  # - id: pii-over-grant-guard",
        "  #   scope: change",
        "  #   predicate:",
        "  #     all:",
        "  #       - inferred_meta: { key: pii, op: is_true }",
        "  #       - config: { key: grants.select, op: not_subset_of, value: [pii_reader] }",
        "  #   action: [{ type: warn }]",
    ]


def _capped_templates(rows: list[MetaKeyCoverage], subject: str) -> list[str]:
    lines: list[str] = []
    for row in rows[:_META_TEMPLATE_CAP]:
        lines.append("")
        lines.extend(_meta_template_lines(row, subject))
    omitted = len(rows) - _META_TEMPLATE_CAP
    if omitted > 0:
        lines.append("")
        lines.append(
            f"  # (+{omitted} more {subject}-meta key(s) not shown — the {_META_TEMPLATE_CAP} "
            "most-covered are above.)"
        )
    return lines


# --- orchestration -----------------------------------------------------------


def run_policy_init(
    manifest: str,
    catalog: str,
    adapter: str | None,
    output: str,
    force: bool,
    stdout: bool,
) -> str:
    """Build the head registry, scan it, emit the policy, and (unless ``stdout``) write the file.

    Returns the YAML text in every mode (so the CLI can echo it and tests can assert on it).
    With ``stdout`` the filesystem is never touched — the overwrite guard does not apply. Without
    it, refuses to clobber an existing ``output`` unless ``force`` (raising ``FileExistsError``),
    so a hand-edited policy is never silently overwritten.
    """
    service = LineageService(Path(catalog), Path(manifest), adapter=adapter)
    scan = scan_project(service.registry)
    text = emit_policy_yaml(scan)
    if stdout:
        return text
    if os.path.exists(output) and not force:
        raise FileExistsError(
            f"refusing to overwrite existing policy '{output}' — pass --force to replace it, "
            "or --stdout to print without writing."
        )
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


__all__ = [
    "DEFAULT_OUTPUT_PATH",
    "emit_policy_yaml",
    "run_policy_init",
    "scan_project",
]
