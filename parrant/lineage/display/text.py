import click

from parrant.models.schema import Column, ColumnLineage, Coverage

from .base import LineageStaticDisplay, format_coverage_line


class TextDisplay(LineageStaticDisplay):
    def display_column_info(self, column: Column) -> None:
        click.echo(f"\nColumn: {column.name}")
        click.echo(f"Type: {column.data_type}")
        if column.description:
            click.echo(f"Description: {column.description}")

    def display_upstream(self, refs: dict[str, dict[str, ColumnLineage] | set[str]]) -> None:
        if not refs:
            return

        click.echo("\nUpstream dependencies:")

        if "sources" in refs and isinstance(refs["sources"], set) and refs["sources"]:
            click.echo("  Sources:")
            for source in sorted(refs["sources"]):
                click.echo(f"    {source}")

        if "direct_refs" in refs and isinstance(refs["direct_refs"], set) and refs["direct_refs"]:
            click.echo("  Direct references:")
            for ref in sorted(refs["direct_refs"]):
                click.echo(f"    {ref}")

        for model_name, columns in refs.items():
            if model_name not in ("sources", "direct_refs") and isinstance(columns, dict):
                click.echo(f"  Model {model_name}:")
                for col_name in columns:
                    click.echo(f"    {col_name}")

    def display_downstream(self, refs: dict[str, dict[str, ColumnLineage] | set[str]]) -> None:
        if not refs:
            return

        click.echo("\nDownstream dependencies:")

        if "exposures" in refs and isinstance(refs["exposures"], set) and refs["exposures"]:
            click.echo("  Exposures:")
            for exposure in sorted(refs["exposures"]):
                click.echo(f"    {exposure}")

        if "sources" in refs and isinstance(refs["sources"], set) and refs["sources"]:
            click.echo("  Sources:")
            for source in sorted(refs["sources"]):
                click.echo(f"    {source}")

        if "direct_refs" in refs and isinstance(refs["direct_refs"], set) and refs["direct_refs"]:
            click.echo("  Direct references:")
            for ref in sorted(refs["direct_refs"]):
                click.echo(f"    {ref}")

        for model_name, columns in refs.items():
            if model_name not in ("exposures", "sources", "direct_refs") and isinstance(
                columns, dict
            ):
                click.echo(f"  Model {model_name}:")
                for col_name in columns:
                    click.echo(f"    {col_name}")

    def display_coverage(self, coverage: Coverage) -> None:
        click.echo("")
        click.echo(format_coverage_line(coverage))

    def save(self) -> None:
        """No-op for text display as output is immediate."""
