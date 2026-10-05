"""The SQLGlot engine, adapted to the :mod:`lineage.provider` seam.

``SqlglotLineageProvider`` is the first — and today only — provider. Because the
``LineageProvider`` / ``ProjectMetadataProvider`` Protocols were defined to match the
existing ``ModelRegistry`` surface, this adapter is a *thin subclass*, not a rewrite:
``ModelRegistry`` keeps owning production (readers + ``SQLColumnParser`` + graph stitching)
verbatim, and this file only

  * adds the two convenience accessors the interface declares
    (:meth:`get_column_lineage`, :meth:`get_column`), and
  * softens :meth:`get_compiled_sql` to return ``None`` instead of raising, so a caller can
    use the null-signal fallback (a future Fusion/warehouse backend has no compiled SQL).

Overriding ``get_compiled_sql`` **here** (not in ``registry.py``) is deliberate: it keeps
the seam file-disjoint from ``registry.py`` — the one file the upcoming policy-engine work
most wants to edit — so the two efforts don't collide.
"""

from __future__ import annotations

from parrant.artifacts.exceptions import ModelNotFoundError
from parrant.artifacts.registry import ModelRegistry
from parrant.lineage.provider import LineageAndMetadataProvider
from parrant.models.schema import Column, ColumnLineage


class SqlglotLineageProvider(ModelRegistry):
    """The SQLGlot ``ModelRegistry`` exposed as a Lineage + ProjectMetadata provider.

    Inherits every production method verbatim from ``ModelRegistry`` (which is left
    untouched) and adds only the interface's convenience accessors plus the softened
    ``get_compiled_sql``. Being a subclass, it *is-a* ``ModelRegistry``, so any existing
    code or test that expects a registry keeps working.
    """

    def get_column_lineage(self, model_name: str, column_name: str) -> list[ColumnLineage]:
        """Per-column upstream edges for ``model.column`` (see the interface).

        Convenience over ``get_model(model).columns[column].lineage``; empty list when the
        model/column is unknown or the column has no parsed lineage.
        """
        column = self.get_column(model_name, column_name)
        if column is None:
            return []
        return list(column.lineage or [])

    def get_column(self, model_name: str, column_name: str) -> Column | None:
        """Column truth for ``model.column`` (case-insensitive), or ``None`` if unknown."""
        try:
            model = self.get_model(model_name)
        except ModelNotFoundError:
            return None
        columns = model.columns
        return columns.get(column_name) or columns.get(column_name.lower())

    def get_compiled_sql(self, model_name: str) -> str | None:  # type: ignore[override]
        """Compiled SQL for a model, or ``None`` when there is none.

        Softens ``ModelRegistry.get_compiled_sql`` (which raises ``ValueError`` /
        ``ModelNotFoundError``) to the interface's ``Optional[str]`` capability contract, so
        a caller can treat "no SQL" as "cannot use the SQL-diff gate" without a try/except.
        The ``[override]`` widening of the return type is intentional (see module docstring).
        """
        try:
            return super().get_compiled_sql(model_name)
        except (ValueError, ModelNotFoundError):
            return None


def build_sqlglot_provider(
    catalog_path: str,
    manifest_path: str,
    adapter_override: str | None = None,
) -> LineageAndMetadataProvider:
    """Construct and :meth:`load` a SQLGlot-backed provider, ready to query.

    The wiring seam: callers depend on the returned ``LineageAndMetadataProvider`` interface
    rather than naming the concrete class, so swapping the backend later is a one-line
    change here. Raises ``RegistryError`` if the artifacts fail to load.
    """
    provider = SqlglotLineageProvider(
        catalog_path, manifest_path, adapter_override=adapter_override
    )
    provider.load()
    return provider
