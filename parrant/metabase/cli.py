"""— the ``metabase-extract`` CLI subcommand (credentialed).

Wired into ``cli/main.py:main()`` as an additive dispatch branch, mirroring the existing
``impact`` subcommand. Credentials come from env/flags and live only here + the client;
they are never persisted into the artifact (only the non-secret base URL is stamped).
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from parrant.artifacts.manifest import ManifestReader
from parrant.metabase.artifact import dump_metabase_lineage, load_metabase_lineage
from parrant.metabase.client import MetabaseClient
from parrant.metabase.extract import ExtractConfig, coverage_ratio, run_extract


def _extractor_version() -> str:
    try:
        return version("parrant")
    except PackageNotFoundError:  # pragma: no cover - installed in normal use
        return "0.0.0"


def _resolve_dialect(manifest: str | None, adapter: str | None) -> str | None:
    if adapter:
        return adapter
    # ``--manifest`` is optional only when ``--adapter`` supplies the dialect directly; the
    # command validates that at least one is present before we get here.
    assert manifest is not None
    reader = ManifestReader(manifest)
    reader.load()
    return reader.get_adapter()


def _load_dashboard_meta(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


@click.command()
@click.option("--metabase-url", envvar="METABASE_URL", required=True)
@click.option("--metabase-api-key", envvar="METABASE_API_KEY")
@click.option("--metabase-username", envvar="METABASE_USERNAME")
@click.option("--metabase-password", envvar="METABASE_PASSWORD")
@click.option(
    "--database-id",
    "database_ids",
    multiple=True,
    type=int,
    required=True,
    help="Restrict to these Metabase database id(s) (the warehouse dbt targets).",
)
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    help="dbt manifest.json — supplies the SQL dialect for the native resolver. Optional when "
    "--adapter is given (the adapter then supplies the dialect and the manifest is not read).",
)
@click.option(
    "--adapter",
    help="SQL dialect for the native resolver (e.g. snowflake). Overrides the manifest adapter; "
    "when set, --manifest may be omitted.",
)
@click.option("--output", "-o", default="metabase_lineage.json")
@click.option("--include-archived", is_flag=True, default=False)
@click.option(
    "--dashboard-meta-file",
    type=click.Path(exists=True),
    help='JSON mapping dashboards to consumer meta: {"by_collection": {...}, '
    '"by_dashboard": {...}}. The tool never hardcodes a taxonomy (tier/owner).',
)
@click.option(
    "--previous",
    type=click.Path(),
    help="Previous metabase_lineage.json to reuse unchanged dashboards from (incremental). "
    "Download the last snapshot from your artifact store and pass it here. A missing path is "
    "treated as a cold start (full extract), so a scheduled job can pass it unconditionally.",
)
@click.option(
    "--max-workers",
    type=int,
    default=8,
    help="Concurrency for dashboard detail fetches.",
)
@click.option(
    "--timeout",
    type=int,
    default=120,
    help="Per-request HTTP timeout (seconds). The bulk /api/card list can be large on big "
    "instances (tens of MB, ~90s), so the default is generous.",
)
@click.option(
    "--fail-under",
    type=float,
    help="Exit non-zero if (column + table) coverage ratio < this value.",
)
def metabase_extract(
    metabase_url: str,
    metabase_api_key: str | None,
    metabase_username: str | None,
    metabase_password: str | None,
    database_ids: tuple[int, ...],
    manifest: str | None,
    adapter: str | None,
    output: str,
    include_archived: bool,
    dashboard_meta_file: str | None,
    previous: str | None,
    max_workers: int,
    timeout: int,
    fail_under: float | None,
) -> None:
    """Snapshot Metabase card→column and card→dashboard lineage into an offline artifact."""
    if not manifest and not adapter:
        raise click.UsageError("Provide --manifest or --adapter to supply the SQL dialect.")
    try:
        dialect = _resolve_dialect(manifest, adapter)
        client = MetabaseClient(
            base_url=metabase_url,
            api_key=metabase_api_key,
            username=metabase_username,
            password=metabase_password,
            timeout=timeout,
        )
        previous_lineage = load_metabase_lineage(previous)
        config = ExtractConfig(
            metabase_base_url=metabase_url,
            database_ids=list(database_ids),
            extractor_version=_extractor_version(),
            dialect=dialect,
            include_archived=include_archived,
            dashboard_meta=_load_dashboard_meta(dashboard_meta_file),
            previous=previous_lineage,
            max_workers=max_workers,
        )
        lineage = run_extract(config, client)
        dump_metabase_lineage(lineage, output)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    cov = lineage.coverage
    ratio = coverage_ratio(cov)
    incremental = ""
    if previous_lineage is not None:
        # Reused = dashboards carried over unchanged from the previous snapshot (same id and
        # matching, non-null ``updated_at``) that survived into the new snapshot.
        prev_stamps = {
            d.dashboard_id: d.updated_at
            for d in previous_lineage.dashboards
            if d.updated_at is not None
        }
        reused = sum(
            1
            for d in lineage.dashboards
            if d.updated_at is not None and prev_stamps.get(d.dashboard_id) == d.updated_at
        )
        incremental = f" (incremental: {reused} dashboards reused)"
    click.echo(
        f"Wrote {output}: {cov.cards_total} cards "
        f"({cov.cards_resolved_column} column-precise, {cov.cards_resolved_table_only} "
        f"table-only, {cov.cards_unresolved} unresolved), "
        f"{cov.dashboards_total} dashboards. Coverage {ratio:.0%}.{incremental}",
        err=True,
    )
    if fail_under is not None and ratio < fail_under:
        click.echo(f"Coverage {ratio:.0%} is below --fail-under {fail_under:.0%}.", err=True)
        sys.exit(1)


def main(args: list | None = None, prog_name: str | None = None) -> None:
    """Entry point used by ``cli/main.py``'s dispatch branch."""
    metabase_extract.main(args=args, prog_name=prog_name)
