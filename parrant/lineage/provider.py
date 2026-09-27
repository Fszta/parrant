"""The lineage seam — the stable interface between lineage *production* and *consumption*.

Lineage *computation* ("column X feeds column Y") is a commodity the engine layer
(dbt Fusion / SDF / a warehouse's ACCESS_HISTORY) will eventually own. Everything the
product actually invests in — semantic change categorization, the policy gate, PII /
cross-boundary reachability, the explorer — lives *above* lineage and must not depend on
*how* lineage is computed. This module declares that boundary as two ``typing.Protocol``s
so today's SQLGlot engine (:class:`~parrant.artifacts.registry.ModelRegistry`)
can be swapped for a Fusion or warehouse backend as a one-adapter change.

Structural (``Protocol``) typing is deliberate: the existing ``ModelRegistry`` already
satisfies the surface, so the seam is a *retype*, not a refactor, and a test fake needs no
base class. Every method is typed purely in terms of existing ``models.schema`` types, so no
consumer's data handling changes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Set, runtime_checkable

from parrant.models.schema import (
    Column,
    ColumnLineage,
    Coverage,
    Exposure,
    Model,
    TestNode,
)


@runtime_checkable
class LineageProvider(Protocol):
    """A swappable backend that produces a project's column-level lineage graph.

    The single stable contract every consumer above the lineage-production line
    (traversal, impact, changeset, verdict, explorer) depends on — never on *how*
    lineage is computed. Today's implementation is the SQLGlot engine (``ModelRegistry``
    via :class:`~parrant.lineage.sqlglot_provider.SqlglotLineageProvider`); a
    Fusion-artifact adapter or a warehouse "authoritative mode" can slot in behind the
    same methods.

    Contract:
      * The graph is **immutable after** :meth:`load` — consumers only read.
      * Model and column names are **lowercased** keys (matches today's registry).
      * Lineage is **materialized onto** ``Model.columns[c].lineage``;
        :meth:`get_column_lineage` is a convenience accessor over the same data.
      * Capability methods (:meth:`get_filter_dependents`, :meth:`get_compiled_sql`,
        :meth:`is_catalog_backed`) degrade **honestly** — an empty / "authoritative"
        answer, never an exception — when a backend cannot compute them.
    """

    # --- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        """Build the graph from the backend's inputs. Raise if already loaded."""

    @property
    def is_loaded(self) -> bool:
        """Whether :meth:`load` has completed and the graph is queryable."""

    # --- model / graph access ---------------------------------------------
    def get_models(self) -> Dict[str, Model]:
        """All nodes (models, snapshots, seeds, sources) keyed by lowercased name."""

    def get_model(self, model_name: str) -> Model:
        """One node by name (case-insensitive). Raise ``ModelNotFoundError`` if absent."""

    def get_manifest_downstream(self) -> Dict[str, Set[str]]:
        """Model-level child map over the *whole* DAG, incl. nodes with no column info.

        Distinct from per-column edges: this is the reachability frontier the impact
        confidence signal is measured against. Every provider must supply model-level
        edges even when it cannot supply column-level ones for some nodes.
        """

    # --- column lineage (the core product) --------------------------------
    def get_column_lineage(self, model_name: str, column_name: str) -> List[ColumnLineage]:
        """Per-column upstream edges for ``model.column`` (case-insensitive).

        Each :class:`ColumnLineage` carries ``source_columns`` (``model.col`` refs),
        ``transformation_type`` (``direct|renamed|derived``) and, when the backend can
        provide it, the defining ``sql_expression``. Empty list when the column is a
        root/literal or the backend has no edge for it. Convenience over
        ``get_model(model).columns[column].lineage``; both must agree.
        """

    def get_column(self, model_name: str, column_name: str) -> Optional[Column]:
        """Column truth (name, ``data_type``, description, lineage) or ``None`` if unknown.

        ``data_type`` may be ``None`` when the backend lacks catalog/compiler column truth
        for that node (see :meth:`is_catalog_backed`) — the column is still traversable.
        """

    # --- row-set / predicate lineage (capability) -------------------------
    def get_filter_dependents(self, source_column: str) -> Set[str]:
        """Models that reference ``source_column`` ONLY in a predicate (WHERE/JOIN/HAVING/QUALIFY).

        Row-set dependents that value-lineage misses. Capability method: a backend that
        does not compute predicate lineage returns an empty set (never raises), and the
        impact report is correspondingly a lower bound.
        """

    # --- provenance / quality signals -------------------------------------
    def get_dialect(self) -> Optional[str]:
        """The SQL dialect used to canonicalize expressions, or ``None`` if unknown.

        Consumed by the AST semantic-diff (:mod:`~parrant.lineage.changeset`)
        so canonicalization matches the source warehouse. A backend that emits
        pre-canonicalized lineage may return ``None`` (sqlglot default) without harm.
        """

    def get_coverage(self) -> Coverage:
        """How completely the backend's inputs cover the project (manifest vs catalog vs
        parsed-ok), so the report can be honest about gaps. See :class:`Coverage`."""

    def is_catalog_backed(self, model_name: str) -> bool:
        """Whether this node's column truth (names + types) is authoritative.

        Today: has a real ``catalog.json`` entry (vs. columns recovered from parsing SQL,
        types unknown). Gates trustworthy structural (add/remove/type) diffs in changeset.
        A compiler-grade backend (Fusion) returns ``True`` for every built node.
        """

    def get_parse_failed_models(self) -> Set[str]:
        """Nodes whose lineage the backend could not compute (had input, failed to derive).

        Feeds the ``partial`` confidence level. A backend with no notion of parse failure
        returns an empty set. Distinct from :meth:`get_opaque_models` — a genuine failure,
        not a deliberate choice not to analyze.
        """

    def get_opaque_models(self) -> Set[str]:
        """Nodes the backend deliberately does NOT column-analyze (unparseable SQL).

        Semantic views chief among them; generally any node whose compiled SQL the backend
        cannot parse. Column lineage is withheld, but MODEL-level reach through them is still
        preserved from the manifest dependency graph, so a change to an upstream reaches (and
        rebuilds) them at model grain. Classified ``opaque`` — a *choice*, not a *failure* —
        so they do not drag the coverage floor. A backend with no notion of opaqueness returns
        an empty set.
        """

    # --- raw SQL (leaky capability) ---------------------------------------
    def get_compiled_sql(self, model_name: str) -> Optional[str]:
        """The model's compiled SQL, or ``None`` when the backend has none.

        Leaky: raw SQL is a *production input*, exposed only because ``changeset`` uses a
        whole-model SQL diff to gate "did logic change?". Consumers must treat ``None`` as
        "cannot use the SQL-diff gate" and fall back to the per-column signature diff.
        """


@runtime_checkable
class ProjectMetadataProvider(Protocol):
    """dbt-object metadata that is **independent of the lineage engine**.

    Test declarations and exposures come from ``manifest.json`` and do not change when the
    lineage backend changes. Kept as a *sibling* protocol so the lineage seam stays purely
    about lineage; today one object (the registry) satisfies both, but a future Fusion
    lineage provider can be paired with the *same* metadata provider unchanged.
    """

    def get_exposures(self) -> Dict[str, Exposure]:
        """All exposures keyed by name."""

    def get_exposure(self, exposure_name: str) -> Exposure:
        """One exposure by name. Raise if absent."""

    def get_column_tests(self, model: str, column: str) -> List[TestNode]:
        """dbt tests targeting ``model.column`` (case-insensitive)."""

    def get_tests_referencing(self, model: str, column: str) -> List[TestNode]:
        """relationships tests whose *referenced* (parent) side is ``model.column``."""

    def get_model_tests(self, model: str) -> List[TestNode]:
        """Every test that breaks if ``model`` is removed wholesale."""

    def get_test_unique_ids(self) -> Set[str]:
        """All test ``unique_id``s present (verdict confirms a base test survived in head)."""

    def get_unattributable_test_count(self) -> int:
        """Tests that could not be attributed to a (model, column) — coverage honesty."""

    # --- arbitrary dbt meta (metadata-agnostic access) --------------------
    def get_model_dbt_meta(self, model: str) -> Dict[str, Any]:
        """Arbitrary user-authored dbt ``meta`` on a model (case-insensitive), or ``{}``.

        Manifest-sourced and independent of the lineage engine, exactly like exposures and
        tests. Every meta key is exposed generically — nothing (``critical``/``pii``/…) is
        privileged; the policy layer above decides what any key means. Merged
        ``config.meta`` over top-level ``meta`` per dbt precedence. Absent meta ⇒ ``{}``.
        """

    def get_column_dbt_meta(self, model: str, column: str) -> Dict[str, Any]:
        """Arbitrary user-authored dbt ``meta`` on a column (case-insensitive), or ``{}``.

        Same contract as :meth:`get_model_dbt_meta`, scoped to one column. Absent ⇒ ``{}``.
        """

    def get_model_config(self, model: str) -> Dict[str, Any]:
        """The node's resolved dbt ``config`` dict for a model (case-insensitive), or ``{}``.

        Manifest-sourced and metadata-agnostic, exactly like :meth:`get_model_dbt_meta`:
        every key (``materialized``, ``grants``, ``tags``, …) is exposed generically, none
        privileged, values surfaced raw. Absent config ⇒ ``{}`` — never guessed.
        """


@runtime_checkable
class LineageAndMetadataProvider(LineageProvider, ProjectMetadataProvider, Protocol):
    """A backend that supplies **both** column lineage and dbt-object metadata.

    Python's type system has no intersection operator, so this explicit combined protocol
    is how consumers that need both facets — the service (traversal + exposures), the
    verdict classifier (lineage graph + test declarations) — name a single type. Today one
    object (the registry, via ``SqlglotLineageProvider``) satisfies it; a future Fusion
    lineage provider paired with a manifest metadata provider would be composed to satisfy
    it too.
    """
