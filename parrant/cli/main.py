import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from parrant.lineage.changeset import (
    ChangesetBuilder,
    ColumnChange,
    OverrideResolution,
    build_changeset_report,
    build_git_changeset,
    git_changed_models,
    scope_changes_to_models,
)
from parrant.lineage.display import DotDisplay, JsonDisplay, TextDisplay
from parrant.lineage.display.base import LineageStaticDisplay
from parrant.lineage.display.html.explore import LineageExplorer
from parrant.lineage.display.markdown import render_changeset_markdown
from parrant.lineage.service import LineageSelector, LineageService
from parrant.lineage.verdict import (
    applied_overrides,
    break_is_overridden,
    classify_provable_breaks,
    decide_verdict,
    ineffective_overrides,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")


@click.command()
@click.version_option(package_name="parrant", message="%(version)s")
@click.option(
    "--select",
    help="Select models/columns to generate lineage for. Format: [+]model_name[.column_name][+]\n"
    "Examples:\n"
    "  stg_accounts.account_id+  (downstream lineage)\n"
    "  +stg_accounts.account_id  (upstream lineage)\n"
    "  stg_accounts.account_id   (both directions)",
)
@click.option(
    "--explore",
    is_flag=True,
    help="Start an interactive HTML server for exploring model and column lineage",
)
@click.option(
    "--catalog",
    type=click.Path(exists=True),
    default="target/catalog.json",
    help="Path to the dbt catalog file",
)
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    default="target/manifest.json",
    help="Path to the dbt manifest file",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["text", "dot", "json"]),
    default="text",
    help="Output format (text, dot graph, or machine-readable json)",
)
@click.option(
    "--output", "-o", default="lineage", help="Output file name for dot format (without extension)"
)
@click.option(
    "--port", "-p", default=8000, help="Port to run the HTML server (only used with --explore)"
)
@click.option(
    "--adapter",
    help="Override sqlglot dialect (e.g., tsql, snowflake, bigquery). If set, ignores adapter from manifest.",
)
@click.option(
    "--base-manifest",
    "base_manifest",
    type=click.Path(exists=True),
    help="(--explore only) Base manifest.json for a two-manifest diff. When provided, the "
    "explorer surfaces the change context: semantic categorization, the policy verdict "
    "(with --policy), and cross-boundary Metabase reach (with --metabase).",
)
@click.option(
    "--base-catalog",
    "base_catalog",
    type=click.Path(exists=True),
    help="(--explore only) Base catalog.json paired with --base-manifest. Defaults to a "
    "catalog.json next to --base-manifest when present.",
)
@click.option(
    "--git-base",
    "git_base",
    help="(--explore only) Git ref for a git-diff changeset when no --base-manifest is given.",
)
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(),
    help="(--explore only) Path to a policy.yml so the explorer can show the policy verdict.",
)
@click.option(
    "--metabase",
    "metabase_path",
    type=click.Path(),
    help="(--explore only) Path to a metabase_lineage.json so the explorer can surface "
    "cross-boundary dashboard reach. Consumed OFFLINE — no Metabase credentials.",
)
@click.option(
    "--no-overrides",
    "no_overrides",
    is_flag=True,
    default=False,
    help="(--explore only) Ignore `-- lineage:allow-*` override pragmas so the explorer "
    "shows the raw gate as if none were present.",
)
def cli(
    select: str,
    explore: bool,
    catalog: str,
    manifest: str,
    format: str,
    output: str,
    port: int,
    adapter: str | None,
    base_manifest: str | None,
    base_catalog: str | None,
    git_base: str | None,
    policy_path: str | None,
    metabase_path: str | None,
    no_overrides: bool,
) -> None:
    """Parrant - column-level lineage and change-impact for dbt (parry breaks, warrant safe)."""
    if not select and not explore:
        click.echo("Error: Either --select or --explore must be specified", err=True)
        sys.exit(1)

    if select and explore:
        click.echo("Error: Cannot use both --select and --explore at the same time", err=True)
        sys.exit(1)

    try:
        service = LineageService(Path(catalog), Path(manifest), adapter=adapter)

        if explore:
            click.echo(f"Starting explore mode server on port {port}...")
            lineage_explorer = LineageExplorer(port=port)
            lineage_explorer.set_lineage_service(service)
            # Static cross-boundary exploration: when a Metabase snapshot is supplied, build a
            # reach index so browsing ANY column shows the Metabase cards/dashboards that read
            # it — no changeset required (the changeset path below still adds change-scoped
            # reach on top when a diff is provided).
            if metabase_path:
                from parrant.metabase.artifact import load_metabase_lineage
                from parrant.metabase.join import build_relation_index
                from parrant.metabase.reach import MetabaseReach

                _mb_lineage = load_metabase_lineage(metabase_path)
                if _mb_lineage is not None:
                    _rel_index = build_relation_index(
                        service.registry, _relation_name_resolver(service.registry)
                    )
                    lineage_explorer.attach_metabase_static(
                        MetabaseReach.build(_mb_lineage, _rel_index),
                        {d.dashboard_id: d.name for d in _mb_lineage.dashboards},
                    )
            # when a changeset source (and optionally a policy / Metabase artifact) is
            # supplied, precompute the changeset report ONCE and hand it to the explorer so
            # every panel can surface the product signals. Absent => pure-explore mode.
            change_report = _build_explore_change_context(
                service,
                adapter=adapter,
                base_manifest=base_manifest,
                base_catalog=base_catalog,
                git_base=git_base,
                policy_path=policy_path,
                metabase_path=metabase_path,
                no_overrides=no_overrides,
            )
            if change_report is not None:
                lineage_explorer.set_change_context(change_report)
                click.echo(
                    "Change context loaded: "
                    f"{len(change_report.get('by_change', []))} changed column(s)"
                    + (
                        f", policy={change_report['policy_verdict'].get('decision')}"
                        if change_report.get("policy_verdict")
                        else ""
                    )
                )
            lineage_explorer.start()
            return

        selector = LineageSelector.from_string(select)
        model = service.registry.get_model(selector.model)

        if selector.column:
            if selector.column in model.columns:
                column = model.columns[selector.column]

                display: LineageStaticDisplay
                if format == "dot":
                    display = DotDisplay(output, registry=service.registry)
                    display.main_model = selector.model
                    display.main_column = selector.column
                elif format == "json":
                    display = JsonDisplay()
                else:
                    display = TextDisplay()

                display.display_column_info(column)
                if isinstance(display, JsonDisplay):
                    display.set_model_description(model.description)

                if selector.upstream:
                    upstream_refs = service._get_upstream_lineage(selector.model, selector.column)
                    display.display_upstream(upstream_refs)

                if selector.downstream:
                    downstream_refs = service._get_downstream_lineage(
                        selector.model, selector.column
                    )
                    display.display_downstream(downstream_refs)

                display.display_coverage(service.get_coverage())

                if format == "json" and isinstance(display, JsonDisplay):
                    # Impact analysis is the flagship capability; include it whenever
                    # downstream lineage was requested so the JSON is self-contained.
                    if selector.downstream:
                        display.set_impact(
                            service.get_column_impact(selector.model, selector.column)
                        )
                    display.save()

                if format == "dot":
                    display.save()
            else:
                ", ".join(model.columns.keys())
                click.echo(
                    f"Error: Column '{selector.column}' not found in model '{selector.model}'",
                    err=True,
                )
                sys.exit(1)
        else:
            model_info = service.get_model_info(selector)
            click.echo(f"\nModel: {model_info['name']}")
            click.echo(f"Schema: {model_info['schema']}")
            click.echo(f"Database: {model_info['database']}")
            click.echo(f"Columns: {', '.join(model_info['columns'])}")

            if model_info["upstream"]:
                click.echo("\nUpstream dependencies:")
                for upstream in model_info["upstream"]:
                    click.echo(f"  {upstream}")

            if model_info["downstream"]:
                click.echo("\nDownstream dependencies:")
                for downstream in model_info["downstream"]:
                    click.echo(f"  {downstream}")

    except Exception as e:
        click.echo(f"Error: {e!s}", err=True)
        sys.exit(1)


def _build_explore_change_context(
    head_service: LineageService,
    *,
    adapter: str | None,
    base_manifest: str | None,
    base_catalog: str | None,
    git_base: str | None,
    policy_path: str | None,
    metabase_path: str | None,
    no_overrides: bool = False,
) -> dict[str, Any] | None:
    """Assemble the changeset report the explorer surfaces, or ``None`` when no change
    source was supplied (pure-explore mode).

    Mirrors the ``impact`` command's report assembly but is self-contained so it never
    perturbs that gate path. Reuses the same library primitives (changeset build, changeset
    impact with optional Metabase reach, provable breaks, policy evaluation) so the explorer
    shows exactly what the CI gate would.
    """
    if not base_manifest and not git_base:
        return None

    honor_overrides = not no_overrides
    stale_overrides: list[dict[str, object]] = []
    override_warnings: list[str] = []

    base_service: LineageService | None = None
    if base_manifest:
        resolved_base_catalog = base_catalog
        if not resolved_base_catalog:
            candidate = Path(base_manifest).parent / "catalog.json"
            if candidate.exists():
                resolved_base_catalog = str(candidate)
        if not resolved_base_catalog:
            click.echo(
                "Error: --base-catalog is required for a two-manifest diff "
                "(no catalog.json found next to --base-manifest).",
                err=True,
            )
            sys.exit(1)
        base_service = LineageService(
            Path(resolved_base_catalog), Path(base_manifest), adapter=adapter
        )
        builder = ChangesetBuilder(
            base_service.registry, head_service.registry, honor_overrides=honor_overrides
        )
        changes = builder.build()
        stale_overrides = builder.stale_overrides
        override_warnings = builder.override_warnings
        source = "two-manifest"
    elif git_base:
        override_collector = OverrideResolution()
        changes = build_git_changeset(
            head_service.registry,
            git_base,
            honor_overrides=honor_overrides,
            collect=override_collector,
        )
        stale_overrides = override_collector.stale
        override_warnings = override_collector.warnings
        source = f"git-diff ({git_base})"
    else:
        # Unreachable given the guard above, but keeps the changeset source well-typed.
        return None

    # Policy + Metabase are optional overlays on the change context.
    from parrant.lineage.policy import evaluate_policy, load_policy

    policy = load_policy(policy_path)

    metabase_reach = None
    metabase_lineage = None
    if metabase_path:
        from parrant.metabase.artifact import load_metabase_lineage
        from parrant.metabase.join import build_relation_index
        from parrant.metabase.reach import MetabaseReach

        metabase_lineage = load_metabase_lineage(metabase_path)
        if metabase_lineage is not None:
            relation_index = build_relation_index(
                head_service.registry, _relation_name_resolver(head_service.registry)
            )
            metabase_reach = MetabaseReach.build(metabase_lineage, relation_index)

    aggregated = head_service.get_changeset_impact(
        changes, base_service=base_service, metabase=metabase_reach
    )
    report = build_changeset_report(source, changes, aggregated)
    report["coverage"] = head_service.get_coverage().model_dump()

    breaks = classify_provable_breaks(
        changes,
        head_service.registry,
        base_service.registry if base_service else None,
    )
    by_change_list = aggregated.get("by_change") if isinstance(aggregated, dict) else None
    summary_obj = report.get("summary", {})
    summary: dict[str, Any] = summary_obj if isinstance(summary_obj, dict) else {}
    report["verdict"] = decide_verdict(breaks, summary, changes, by_change=by_change_list)
    # Mirror the impact() gate: unexcused (blocking) breaks only; excused ones surface as
    # allow-break override records so the explorer shows the same signals as CI.
    blocking_breaks = [b for b in breaks if not break_is_overridden(b, changes)]
    report["provable_breaks"] = [b.model_dump() for b in blocking_breaks]
    summary["provable_break_count"] = len(blocking_breaks)

    if policy is not None:
        verdict = evaluate_policy(
            changes,
            aggregated,
            head_service.registry,
            policy,
            breaks,
            metabase_reach=metabase_reach,
        )
        report["policy_verdict"] = verdict.model_dump(mode="json")
        from parrant.lineage.policy import (
            applied_policy_overrides,
            ineffective_policy_overrides,
        )

        report["overrides"] = applied_policy_overrides(verdict, changes)
        report["ineffective_overrides"] = ineffective_policy_overrides(verdict, changes, breaks)
    else:
        report["overrides"] = applied_overrides(changes, breaks, by_change_list)
        report["ineffective_overrides"] = ineffective_overrides(changes, breaks, by_change_list)
    report["stale_overrides"] = stale_overrides
    report["override_warnings"] = override_warnings

    if metabase_lineage is not None:
        from parrant.metabase.reach import build_reach_confidence

        reached_dashboards = [
            exposure
            for exposure in aggregated.get("affected_exposures", [])
            if exposure.get("source") == "metabase"
        ]
        report["metabase"] = build_reach_confidence(
            metabase_lineage, reached_dashboards
        ).model_dump()

    return report


@click.command()
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    default="target/manifest.json",
    help="Path to the current (head) dbt manifest file",
)
@click.option(
    "--catalog",
    type=click.Path(exists=True),
    default="target/catalog.json",
    help="Path to the current (head) dbt catalog file",
)
@click.option(
    "--base-manifest",
    type=click.Path(exists=True),
    help="Path to the base (target-branch) manifest. Enables two-manifest diff.",
)
@click.option(
    "--base-catalog",
    type=click.Path(exists=True),
    help="Path to the base (target-branch) catalog. Defaults to catalog.json next "
    "to --base-manifest. Required for column-level (add/remove/type) diffing.",
)
@click.option(
    "--git-base",
    help="Fallback: diff changed .sql files against this git ref (e.g. main) when "
    "no base manifest is available. Reports touched models as logic changes.",
)
@click.option(
    "--scope-git",
    help="Restrict a two-manifest diff to models changed in `git diff <ref>...HEAD` "
    "(e.g. origin/main). Drops changes on models the branch didn't touch — useful "
    "when the base artifacts may be staler than the base ref.",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    help="Output format for the impact report",
)
@click.option("--adapter", help="Override sqlglot dialect (e.g., tsql, snowflake, bigquery).")
@click.option(
    "--explain",
    is_flag=True,
    default=False,
    help="Show why each column was flagged (the semantic reason and the base→head "
    "expressions). Affects the human report only; the JSON output always carries it.",
)
@click.option(
    "--no-overrides",
    "no_overrides",
    is_flag=True,
    default=False,
    help="Ignore `-- lineage:allow-change` / `-- lineage:allow-break` override pragmas and "
    "compute the RAW gate as if none were present (audit / measure override reliance).",
)
@click.option(
    "--ci",
    is_flag=True,
    help="CI mode: post/update a sticky impact comment on the PR and apply the "
    "--fail-on severity gate as an exit code.",
)
@click.option(
    "--fail-on",
    type=click.Choice(["none", "tests", "exposures", "critical", "any", "policy"]),
    default="none",
    help="Severity gate for --ci: fail (exit 1) when the impact reaches this level. "
    "'tests' fails only on a provable break (a dbt test the change orphans) — the "
    "safe level to block on. 'policy' fails when the metadata-agnostic policy engine "
    "returns a BLOCK verdict (needs a resolvable --policy). Defaults to 'none' "
    "(warn only, never block).",
)
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(exists=True),
    help="Path to a policy.yml for the metadata-agnostic policy engine. When resolvable "
    "(explicit path or ./parrant.policy.yml), the report gains a 'policy_verdict' "
    "block and --fail-on policy gates on it. A present-but-invalid file fails loudly.",
)
@click.option(
    "--metabase",
    "metabase_path",
    type=click.Path(exists=True),
    help="Path to a metabase_lineage.json artifact (from `metabase-extract`). When supplied, "
    "the impact reach is extended PAST dbt's edge: Metabase dashboards that read a changed "
    "column (directly or via its downstream) surface as exposure-kind reach, matchable by a "
    "policy `reach: {kind: exposure}` rule. Consumed OFFLINE — no Metabase credentials.",
)
@click.option(
    "--emit-selector",
    is_flag=True,
    default=False,
    help="Write has_rebuild and rebuild_selector to $GITHUB_OUTPUT for a selective dbt build. "
    "Additive; composes with --format json; posts no comment and never changes the exit code "
    "(gating stays --fail-on). The selector is space-joined dbt node names — the consumer "
    "validates them against `dbt ls` and treats any unresolved name as fail-closed.",
)
@click.option(
    "--github-token",
    envvar="GITHUB_TOKEN",
    help="GitHub token for posting the PR comment (defaults to $GITHUB_TOKEN).",
)
@click.option(
    "--repo",
    envvar="GITHUB_REPOSITORY",
    help="owner/name of the repo (defaults to $GITHUB_REPOSITORY).",
)
@click.option(
    "--pr-number",
    type=int,
    help="Pull request number (defaults to the GitHub Actions event payload).",
)
def impact(
    manifest: str,
    catalog: str,
    base_manifest: str | None,
    base_catalog: str | None,
    git_base: str | None,
    scope_git: str | None,
    format: str,
    adapter: str | None,
    explain: bool,
    no_overrides: bool,
    ci: bool,
    fail_on: str,
    policy_path: str | None,
    metabase_path: str | None,
    emit_selector: bool,
    github_token: str | None,
    repo: str | None,
    pr_number: int | None,
) -> None:
    """Diff-driven impact: assess the blast radius of a whole change (PR).

    Provide a base manifest/catalog for the reliable two-manifest diff, or a
    --git-base ref for the git-diff fallback. Add --ci to post the report as a
    sticky PR comment and gate the check with --fail-on.
    """
    try:
        head_service = LineageService(Path(catalog), Path(manifest), adapter=adapter)

        # Resolve the policy up front so a present-but-broken file fails LOUDLY (the
        # PolicyConfigError propagates to the outer handler -> exit 1) rather than being
        # mistaken for "no policy". None means no policy configured -> legacy behaviour.
        from parrant.lineage.policy import evaluate_policy, load_policy

        policy = load_policy(policy_path)

        # Cross-boundary Metabase reach: load the offline artifact and build the
        # relation-joined reach index up front. A present-but-invalid artifact fails LOUDLY
        # (MetabaseArtifactError -> outer handler -> exit 1); a missing/None one degrades to
        # dbt-only reach. This path imports ONLY the artifact reader + join/reach — never the
        # credentialed Metabase client (the offline guardrail is structural,).
        metabase_reach = None
        metabase_lineage = None
        if metabase_path:
            from parrant.metabase.artifact import load_metabase_lineage
            from parrant.metabase.join import build_relation_index
            from parrant.metabase.reach import MetabaseReach

            metabase_lineage = load_metabase_lineage(metabase_path)
            if metabase_lineage is not None:
                relation_index = build_relation_index(
                    head_service.registry, _relation_name_resolver(head_service.registry)
                )
                metabase_reach = MetabaseReach.build(metabase_lineage, relation_index)

        honor_overrides = not no_overrides
        # Override side-outputs (populated by the changeset build below): stale directives
        # (no matching change) and parse warnings (malformed pragmas). Empty under --no-overrides.
        stale_overrides: list[dict[str, object]] = []
        override_warnings: list[str] = []

        base_service: LineageService | None = None
        changes: list[ColumnChange]
        # Whether structural checks (added/removed/type_changed) could run. They need a
        # real catalog on both sides; the two-manifest path decides this from the builder
        # below. The git-diff fallback is a separate, self-evident coarse mode, so it is
        # left as-is (no catalog note).
        structural_checks_available = True

        if scope_git and not base_manifest:
            click.echo(
                "Error: --scope-git only applies to the two-manifest diff "
                "(--base-manifest); the --git-base fallback is already file-scoped.",
                err=True,
            )
            sys.exit(1)

        if base_manifest:
            resolved_base_catalog = base_catalog
            if not resolved_base_catalog:
                sibling = Path(base_manifest).parent / "catalog.json"
                if sibling.exists():
                    resolved_base_catalog = str(sibling)
            if not resolved_base_catalog:
                click.echo(
                    "Error: --base-catalog is required for two-manifest diff "
                    "(no catalog.json found next to --base-manifest).",
                    err=True,
                )
                sys.exit(1)

            base_service = LineageService(
                Path(resolved_base_catalog), Path(base_manifest), adapter=adapter
            )
            builder = ChangesetBuilder(
                base_service.registry, head_service.registry, honor_overrides=honor_overrides
            )
            changes = builder.build()
            stale_overrides = builder.stale_overrides
            override_warnings = builder.override_warnings
            structural_checks_available = builder.structural_diff_available()
            source = "two-manifest"

            if scope_git:
                # Intersect the precise two-manifest changeset with the models the
                # branch actually touched, so a stale base artifact can't leak
                # already-merged changes into the report.
                scoped_models = git_changed_models(head_service.registry, scope_git)
                changes = scope_changes_to_models(changes, scoped_models)
                source = f"two-manifest scoped to git-diff ({scope_git})"
        elif git_base:
            override_collector = OverrideResolution()
            changes = build_git_changeset(
                head_service.registry,
                git_base,
                honor_overrides=honor_overrides,
                collect=override_collector,
            )
            stale_overrides = override_collector.stale
            override_warnings = override_collector.warnings
            source = f"git-diff ({git_base})"
        else:
            click.echo(
                "Error: provide --base-manifest (two-manifest diff) or --git-base "
                "(git-diff fallback) to derive the changeset.",
                err=True,
            )
            sys.exit(1)

        if fail_on == "tests" and base_service is None:
            click.echo(
                "Warning: --fail-on tests needs a two-manifest diff (--base-manifest). The "
                "git-diff fallback only detects logic changes, so no provable break can be "
                "found and the gate will never fire.",
                err=True,
            )

        # Same warn-and-no-fire pattern as --fail-on tests above: asking to gate on the
        # policy engine without a resolvable policy can never block, so say so plainly.
        if fail_on == "policy" and policy is None:
            click.echo(
                "Warning: --fail-on policy needs a resolvable policy (--policy PATH or "
                "./parrant.policy.yml). None was found, so the gate will never fire.",
                err=True,
            )

        # surface malformed-pragma warnings LOUDLY on stderr (also rendered in the report).
        for warning in override_warnings:
            click.echo(f"Override warning: {warning}", err=True)

        aggregated = head_service.get_changeset_impact(
            changes, base_service=base_service, metabase=metabase_reach
        )
        report = build_changeset_report(source, changes, aggregated)
        report["coverage"] = head_service.get_coverage().model_dump()
        report["structural_checks_available"] = structural_checks_available

        # Provable breaks + the SAFE/REVIEW/BLOCK ruling. Base registry (when present) is the
        # reliable source of the tests that existed before the change.
        breaks = classify_provable_breaks(
            changes,
            head_service.registry,
            base_service.registry if base_service else None,
        )
        by_change_list = aggregated.get("by_change") if isinstance(aggregated, dict) else None
        summary_obj = report.get("summary", {})
        summary: dict[str, Any] = summary_obj if isinstance(summary_obj, dict) else {}
        report["verdict"] = decide_verdict(breaks, summary, changes, by_change=by_change_list)
        # a break excused by an allow-break override is DEMOTED — it must not keep the
        # gate armed. Split the breaks so report/gate reflect only the UNEXCUSED (blocking)
        # ones; excused breaks surface in the overrides section (block -> review). With no
        # override present this is byte-identical to the old ``[b for b in breaks]`` behavior.
        blocking_breaks = [b for b in breaks if not break_is_overridden(b, changes)]
        report["provable_breaks"] = [b.model_dump() for b in blocking_breaks]
        # Expose the (effective) count in the summary so the CI gate (--fail-on tests) reads it.
        summary["provable_break_count"] = len(blocking_breaks)
        # Honesty: break detection only sees catalog-backed models and tests it could
        # attribute to a column. Surface the blind spots so a SAFE ruling reads as a lower
        # bound, not a guarantee.
        report["verdict_coverage"] = {
            "unattributable_tests": head_service.registry.get_unattributable_test_count(),
        }

        # Policy engine: additive and metadata-agnostic. When a policy resolved, evaluate
        # the consumer's rules over the changeset + its reach + arbitrary dbt meta and attach the
        # full PolicyVerdict as report["policy_verdict"] (the flagship machine-facing surface).
        # decide_verdict above stays untouched as the no-policy fallback (backward compatible).
        if policy is not None:
            verdict = evaluate_policy(
                changes,
                aggregated,
                head_service.registry,
                policy,
                breaks,
                metabase_reach=metabase_reach,
            )
            # mode="json" so enums (GateDecision, ActionKind) serialize to their string values —
            # the report is JSON-dumped and also rendered by markdown, both of which expect plain
            # strings (a raw model_dump() leaves enum members and renders as "GateDecision.BLOCK").
            report["policy_verdict"] = verdict.model_dump(mode="json")
            # Override records come from the CAPPED hits under a policy (uniform shape).
            from parrant.lineage.policy import (
                applied_policy_overrides,
                ineffective_policy_overrides,
            )

            report["overrides"] = applied_policy_overrides(verdict, changes)
            report["ineffective_overrides"] = ineffective_policy_overrides(verdict, changes, breaks)
        else:
            # No policy: the default gate owns the override records.
            report["overrides"] = applied_overrides(changes, breaks, by_change_list)
            report["ineffective_overrides"] = ineffective_overrides(changes, breaks, by_change_list)
        report["stale_overrides"] = stale_overrides
        report["override_warnings"] = override_warnings

        # Cross-boundary honesty block: when the Metabase artifact was joined, attach
        # the reach-confidence (snapshot staleness + column-precise vs table-only) so a
        # fail-closed block driven by a stale/coarse snapshot reads as such.
        if metabase_lineage is not None:
            from parrant.metabase.reach import build_reach_confidence

            reached_dashboards = [
                exposure
                for exposure in aggregated.get("affected_exposures", [])
                if exposure.get("source") == "metabase"
            ]
            report["metabase"] = build_reach_confidence(
                metabase_lineage, reached_dashboards
            ).model_dump()

        if format == "json":
            click.echo(json.dumps(report, indent=2, sort_keys=False))
        else:
            click.echo(render_changeset_markdown(report, explain=explain))

    except Exception as e:
        click.echo(f"Error: {e!s}", err=True)
        sys.exit(1)

    # Selector emission is a pure side-channel to $GITHUB_OUTPUT: independent of --ci, it posts
    # no comment and never calls sys.exit, so the exit code stays owned by --fail-on. Placed after
    # the try/except so a hard render error (which already exited 1) never emits a green selector.
    if emit_selector:
        from parrant.lineage.ci import write_selector_outputs

        write_selector_outputs(report)

    # CI wiring is deliberately outside the try/except above: a failure to render
    # the report is a hard error, but the CI gate deciding to fail the check
    # (exit 1) is a normal outcome we don't want to mask as "Error: 1".
    if ci:
        _run_ci(report, fail_on, github_token, repo, pr_number, explain=explain)


@click.group(name="policy")
def policy_group() -> None:
    """Policy tooling: author and validate metadata-agnostic gate policies."""


@policy_group.command("test")
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    default="target/manifest.json",
    help="Path to the current (head) dbt manifest — the registry replayed against every point.",
)
@click.option(
    "--catalog",
    type=click.Path(exists=True),
    default="target/catalog.json",
    help="Path to the current (head) dbt catalog.",
)
@click.option("--adapter", help="Override sqlglot dialect (e.g., tsql, snowflake, bigquery).")
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(exists=True),
    required=True,
    help="Path to the candidate policy.yml to backtest. A present-but-invalid file fails loudly.",
)
@click.option(
    "--git-range",
    "git_range",
    help="Replay each commit in <base>..<head> as one changeset (default head = HEAD).",
)
@click.option(
    "--last",
    type=int,
    help="Sugar for --git-range HEAD~N..HEAD (replay the last N commits).",
)
@click.option(
    "--changesets",
    "changesets_dir",
    type=click.Path(exists=True, file_okay=False),
    help="Replay a directory of saved changeset JSONs (fixtures / CI corpora).",
)
@click.option(
    "--repo-dir",
    "repo_dir",
    type=click.Path(exists=True, file_okay=False),
    help="Git repo directory for git-range/last modes (defaults to the current directory). "
    "Independent of the manifest/catalog paths, which may live under target/.",
)
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["table", "json", "markdown"]),
    default="table",
    help="Output format (table for terminals, json/markdown for CI artifacts + agents).",
)
@click.option(
    "--fail-on",
    "fail_on",
    type=click.Choice(["none", "any-block", "regression"]),
    default="none",
    help="Exit-code gate. 'any-block': fail if any replayed PR would block. 'regression': fail "
    "if any rule's would-BLOCK count rose vs --baseline (which is then REQUIRED). Default none.",
)
@click.option(
    "--baseline",
    "baseline_path",
    type=click.Path(exists=True),
    help="A saved prior backtest JSON to compare against (required for --fail-on regression).",
)
def policy_test(
    manifest: str,
    catalog: str,
    adapter: str | None,
    policy_path: str,
    git_range: str | None,
    last: int | None,
    changesets_dir: str | None,
    repo_dir: str | None,
    fmt: str,
    fail_on: str,
    baseline_path: str | None,
) -> None:
    """Backtest a candidate policy over git history (or a saved changeset corpus).

    Builds the head registry ONCE and replays the policy against each point, reporting per rule
    what the gate WOULD have ruled — would-BLOCK / would-WARN PRs and, the headline trust column,
    how many firings were driven by a fail-safe UNKNOWN rather than a proven match. Deterministic,
    offline, and SEQUENTIAL (no dbt run, no warehouse). NOTE: the backtest has no per-commit before-state,
    so the provable-break and semantic meaning-change BLOCK tiers are NOT exercised (see the
    fidelity note in the output).
    """
    from parrant.lineage.backtest import run_backtest
    from parrant.lineage.display.backtest import (
        render_backtest_markdown,
        render_backtest_table,
    )
    from parrant.lineage.policy import load_policy
    from parrant.models.schema import BacktestReport

    # Exactly-one-source guard (loud), mirroring the mutually-exclusive discipline elsewhere.
    supplied = [git_range is not None or last is not None, changesets_dir is not None]
    if sum(1 for s in supplied if s) != 1:
        click.echo(
            "Error: provide exactly one change source: --git-range / --last (git history) "
            "or --changesets (a directory of saved changesets).",
            err=True,
        )
        sys.exit(1)
    if git_range is not None and last is not None:
        click.echo("Error: --git-range and --last are mutually exclusive.", err=True)
        sys.exit(1)

    # --fail-on regression is uncomputable without a baseline: fail loudly, never silently pass.
    if fail_on == "regression" and not baseline_path:
        click.echo(
            "Error: --fail-on regression requires --baseline (a saved prior run to compare "
            "against). A regression gate with no baseline validates nothing.",
            err=True,
        )
        sys.exit(1)

    report: BacktestReport | None = None
    baseline: BacktestReport | None = None
    try:
        # Resolve the policy up front so a broken file fails loudly (PolicyConfigError -> exit 1),
        # never treated as "no policy".
        policy = load_policy(policy_path)
        if policy is None:
            # required=True + exists=True means the path resolved; None would be a resolution bug.
            raise click.ClickException(f"could not load policy '{policy_path}'")

        if baseline_path:
            try:
                with open(baseline_path, "r", encoding="utf-8") as handle:
                    baseline = BacktestReport.model_validate_json(handle.read())
            except Exception as exc:
                raise click.ClickException(
                    f"could not parse --baseline '{baseline_path}' as a backtest report: {exc}"
                )

        head_service = LineageService(Path(catalog), Path(manifest), adapter=adapter)
        report = run_backtest(
            head_service,
            policy,
            git_range=git_range,
            last=last,
            changesets_dir=changesets_dir,
            repo_dir=repo_dir,
            baseline=baseline,
            policy_source=str(policy_path),
        )

        if fmt == "json":
            click.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=False))
        elif fmt == "markdown":
            click.echo(render_backtest_markdown(report))
        else:
            click.echo(render_backtest_table(report))
    except Exception as exc:
        click.echo(f"Error: {exc!s}", err=True)
        sys.exit(1)

    # The gate exit is OUTSIDE the try/except (mirrors impact's CI gate): a tripped gate is a
    # normal non-zero outcome, not an "Error".
    from parrant.lineage.backtest import backtest_exit_code as _exit_code

    assert report is not None  # the except above sys.exit(1)s, so we only get here on success
    code = _exit_code(report, fail_on, baseline)
    if code != 0:
        click.echo(f"Backtest gate '--fail-on {fail_on}' tripped — failing.", err=True)
    sys.exit(code)


@policy_group.command("init")
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    default="target/manifest.json",
    help="Path to the dbt manifest to scan (defaults to target/manifest.json — run dbt first).",
)
@click.option(
    "--catalog",
    type=click.Path(exists=True),
    default="target/catalog.json",
    help="Path to the dbt catalog to scan (defaults to target/catalog.json — run dbt first).",
)
@click.option("--adapter", help="Override sqlglot dialect (e.g., tsql, snowflake, bigquery).")
@click.option(
    "-o",
    "--output",
    default="./parrant.policy.yml",
    show_default=True,
    help="Where to write the generated policy (the path load_policy auto-resolves).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing policy file at --output instead of refusing.",
)
@click.option(
    "--stdout",
    is_flag=True,
    help="Print the generated policy to stdout instead of writing a file (never touches disk).",
)
def policy_init(
    manifest: str,
    catalog: str,
    adapter: str | None,
    output: str,
    force: bool,
    stdout: bool,
) -> None:
    """Scaffold a safe-by-construction starter policy from your manifest + catalog.

    Reads the artifacts once and emits a heavily-commented ``policy.yml`` keyed only to signals
    the scan confirmed exist: it enables ``provable-break-block`` when column-targeted tests are
    found and ``exposure-guard`` when exposures are found, and offers every discovered dbt-meta
    key as a COMMENTED template with real coverage. The result runs green on day one (no
    rage-block). Refuses to overwrite an existing file without ``--force``; ``--stdout`` prints
    instead of writing.
    """
    from parrant.lineage.policy_init import run_policy_init

    try:
        text = run_policy_init(
            manifest=manifest,
            catalog=catalog,
            adapter=adapter,
            output=output,
            force=force,
            stdout=stdout,
        )
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if stdout:
        click.echo(text)
    else:
        click.echo(
            f"Wrote {output}. It enables only safe-by-construction structural rules, so it runs "
            "green today — run `parrant policy test --last 20` before flipping any rule "
            "to block or uncommenting a meta template."
        )


def _relation_name_resolver(registry: Any):
    """Build a ``model_name -> manifest relation_name`` callable for the Metabase join.

    The SQLGlot provider IS-A ``ModelRegistry``, which owns the manifest reader; ``relation_name``
    is dbt's authoritative physical identifier (honours ``alias``/``identifier``), so we prefer it
    over ``Model.name`` for the warehouse join. Guarded: a backend without a manifest reader (or a
    node without ``relation_name``) yields ``None`` and the join falls back to the model's
    ``(database, schema, name)``.
    """
    reader = getattr(registry, "_manifest_reader", None)
    if reader is None or not hasattr(reader, "_find_node"):
        return None

    def _resolve(model_name: str) -> str | None:
        node = reader._find_node(model_name)
        if not node:
            return None
        relation_name = node.get("relation_name")
        return relation_name if isinstance(relation_name, str) else None

    return _resolve


def _run_ci(
    report: dict,
    fail_on_value: str,
    token: str | None,
    repo: str | None,
    pr_number: int | None,
    explain: bool = False,
) -> None:
    """Post the sticky PR comment (best-effort) and exit per the severity gate."""
    from parrant.lineage.ci import (
        FailOn,
        gate_exit_code,
        post_sticky_comment,
        resolve_context,
        write_github_outputs,
    )

    # Expose machine-readable results to the composite action (via $GITHUB_OUTPUT)
    # before anything else, so downstream workflow steps get them even if the gate
    # trips (non-zero exit) below.
    write_github_outputs(report)

    body = render_changeset_markdown(report, explain=explain)
    context = resolve_context(token, repo, pr_number)
    if context is None:
        click.echo(
            "CI mode: no PR context resolved (need a GitHub token, repo and PR "
            "number) — skipping the sticky comment.",
            err=True,
        )
    else:
        try:
            outcome = post_sticky_comment(context, body)
            click.echo(
                f"CI mode: {outcome} impact comment on {context.repo}#{context.pr_number}.",
                err=True,
            )
        except Exception as exc:
            # A comment-post failure (network, permissions) shouldn't crash the
            # gate — report it and still apply the severity policy.
            click.echo(f"CI mode: failed to post PR comment: {exc}", err=True)

    # Reconstruct the PolicyVerdict (dumped into the report) so the POLICY gate can read
    # .blocks(). Only consulted under --fail-on policy; every other gate ignores it.
    policy_verdict = None
    raw_verdict = report.get("policy_verdict")
    if isinstance(raw_verdict, dict):
        from parrant.models.schema import PolicyVerdict

        policy_verdict = PolicyVerdict.model_validate(raw_verdict)

    fail_on = FailOn(fail_on_value)
    exit_code = gate_exit_code(report.get("summary", {}), fail_on, policy_verdict)
    if exit_code != 0:
        click.echo(
            f"CI gate '--fail-on {fail_on.value}' tripped — failing the check.",
            err=True,
        )
    sys.exit(exit_code)


def main() -> None:
    # Keep `cli` fully backward-compatible (existing --select/--explore usage and
    # tests target it directly) while exposing `impact` as a subcommand.
    argv = sys.argv[1:]
    if argv and argv[0] == "impact":
        impact.main(args=argv[1:], prog_name="parrant impact")
    elif argv and argv[0] == "policy":
        # `policy test` backtests a candidate policy over git history. Dispatched on argv[0] only,
        # so it never shadows the `--policy` OPTION on the root `cli` / `impact` commands.
        policy_group.main(args=argv[1:], prog_name="parrant policy")
    elif argv and argv[0] == "metabase-extract":
        # Imported lazily so the credentialed Metabase client is only loaded when the
        # extract subcommand is actually invoked — the offline gate path never imports it.
        from parrant.metabase import cli as metabase_extract

        metabase_extract.main(args=argv[1:], prog_name="parrant metabase-extract")
    else:
        cli()


def main_legacy() -> None:
    """Entry point for the deprecated ``dbt-col-lineage`` alias.

    Emits a one-line rename notice to STDERR (never stdout — the CLI's JSON/markdown output on
    stdout feeds agents and CI, and must not be corrupted), then delegates to :func:`main`. The
    alias is kept for one major release so existing scripts/CI keep working during the migration.
    """
    print(
        "note: `dbt-col-lineage` has been renamed to `parrant`; the old command will be removed "
        "in a future release. Run `parrant` instead.",
        file=sys.stderr,
    )
    main()


if __name__ == "__main__":
    main()
