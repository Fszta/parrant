from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
import hashlib
import json
import logging

from parrant.artifacts.catalog import CatalogReader
from parrant.artifacts.manifest import ManifestReader
from parrant.models.schema import (
    ArtifactStamp,
    ArtifactStamps,
    Model,
    Column,
    SQLParseResult,
    ColumnLineage,
    Exposure,
    Coverage,
    TestNode,
)
from parrant.artifacts.exceptions import (
    ModelNotFoundError,
    RegistryNotLoadedError,
    RegistryError,
)
from parrant.parser import SQLColumnParser

logger = logging.getLogger(__name__)


# Resource types that can carry column lineage; coverage is measured against these.
_MODEL_LIKE_RESOURCE_TYPES = frozenset({"model", "snapshot", "seed"})

# Cap on the failed/skipped name lists surfaced in Coverage.
_COVERAGE_NAME_CAP = 25


@dataclass
class ParseStats:
    """Column-lineage parse outcome tallies."""

    parsed_ok: int = 0
    parse_failed: int = 0
    # Nodes whose compiled SQL the parser could not read and that we deliberately do NOT
    # analyze at the column level (semantic views chief among them; generally any unparseable
    # node). Kept OUT of ``parse_failed`` on purpose: opaque is a choice, not a failure, so it
    # neither drags the coverage floor nor reads as a parser bug. Model-level reach through
    # them is preserved from the manifest dependency graph.
    opaque: int = 0
    skipped_no_sql: int = 0
    failed_model_names: List[str] = field(default_factory=list)
    opaque_model_names: List[str] = field(default_factory=list)
    skipped_model_names: List[str] = field(default_factory=list)


@dataclass
class RegistryState:
    """Immutable state of the registry."""

    models: Dict[str, Model]
    exposures: Dict[str, Exposure]
    is_loaded: bool = False


class ModelRegistry:
    def __init__(
        self,
        catalog_path: str,
        manifest_path: str,
        adapter_override: Optional[str] = None,
    ):
        self._catalog_reader = CatalogReader(catalog_path)
        self._manifest_reader = ManifestReader(manifest_path)
        self._state = RegistryState(models={}, exposures={}, is_loaded=False)
        self._sql_parser: Optional[SQLColumnParser] = None
        self._dialect: Optional[str] = None
        self._adapter_override: Optional[str] = adapter_override
        self._parse_stats: ParseStats = ParseStats()
        # Names of model-like nodes that have a real catalog entry (data types known).
        # A manifest node absent from this set is "catalog-missing": still analyzable via
        # its compiled SQL, but with unknown column types.
        self._catalog_backed_model_names: set = set()
        # Lazily-built reverse index: upstream column -> models that reference it ONLY in a
        # predicate (filter/join), i.e. a row-set dependency rather than a value one.
        self._filter_dependents: Optional[Dict[str, set]] = None
        # Reverse index built at load time: (model, column) -> tests targeting that column.
        # Keys are lowercased to match the codebase's case-insensitive model/column naming.
        self._column_tests: Dict[Tuple[str, str], List[TestNode]] = {}
        # Reverse index for the *referenced* side of relationships tests: (model, column) ->
        # relationships tests pointing AT that column via ``to=``/``field=``. Removing this
        # parent key breaks the child's relationships test just as surely as removing the
        # child column does, so it is a distinct provable-break lookup.
        self._referenced_tests: Dict[Tuple[str, str], List[TestNode]] = {}
        # Tests we could not attribute to a (model, column) pair — kept for coverage honesty
        # (counted, never guessed at). See :meth:`get_unattributable_test_count`.
        self._unattributable_tests: List[TestNode] = []
        # Every test node's unique_id present in this manifest. Lets the verdict classifier
        # confirm a base test STILL EXISTS in head before flagging it broken — so a rename
        # that updates the test's yml (new unique_id) is not a false break.
        self._test_unique_ids: Set[str] = set()
        # model (lowercased) -> every test that breaks if the whole model is removed: those
        # attached to it AND relationships tests referencing it. Column-level recovery can
        # miss a model's tested columns, but a wholly-removed model breaks all of its tests.
        self._model_tests: Dict[str, List[TestNode]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._state.is_loaded

    def _initialize_models(self) -> Dict[str, Model]:
        """Initialize the model universe from the *manifest*, enriched by the catalog.

        The manifest is the source of truth for which models exist: it lists every
        node dbt knows about, regardless of whether the relation has been built in
        the warehouse. The catalog is only an *enrichment* source that supplies real
        column names and data types for relations that were built and profiled.

        Seeding from the catalog alone (the previous behaviour) made every model
        absent from ``catalog.json`` invisible — a common case under a deferred /
        partial CI build (``dbt docs generate --defer`` after building only the
        ``state:modified+`` cone) and for non-table relations such as semantic views.
        Those models were then silently dropped from impact and, in the two-manifest
        diff, misreported as removed. Here we register every manifest model-like node;
        columns for catalog-missing nodes are recovered later from their compiled SQL
        (see :meth:`_apply_column_lineage`).
        """
        try:
            catalog_models = self._catalog_reader.get_models_nodes()
        except Exception as e:
            raise RegistryError(f"Failed to initialize models: {e}")

        models: Dict[str, Model] = {}
        catalog_backed: set = set()

        # 1) Seed the universe from manifest model-like nodes (model/snapshot/seed).
        for node_id, node in self._manifest_reader.manifest.get("nodes", {}).items():
            if node.get("resource_type") not in _MODEL_LIKE_RESOURCE_TYPES:
                continue
            name = (node.get("name") or node_id.split(".")[-1]).lower()
            if name in models:
                continue

            catalog_model = catalog_models.get(name)
            if (
                catalog_model is not None
                and catalog_model.resource_type in _MODEL_LIKE_RESOURCE_TYPES
            ):
                # Built & profiled: reuse the catalog Model (real column types).
                models[name] = catalog_model
                catalog_backed.add(name)
            else:
                # Present in the manifest but absent from the catalog. Register it now;
                # its output columns are derived from compiled SQL during lineage parsing.
                models[name] = Model(
                    name=name,
                    schema=node.get("schema") or "main",
                    database=node.get("database") or "main",
                    columns={},
                    resource_type=node.get("resource_type"),
                    unique_id=node_id,
                    metadata={"catalog_missing": True},
                )

        # 2) Add sources (catalog-only inputs, not manifest ``nodes``) so upstream
        #    references still resolve.
        for name, model in catalog_models.items():
            if model.resource_type == "source" and name not in models:
                models[name] = model

        self._catalog_backed_model_names = catalog_backed

        if not models:
            raise RegistryError("No models found in manifest or catalog")
        return models

    def _apply_dependencies(self, models: Dict[str, Model]) -> None:
        """Apply upstream and downstream dependencies to models."""
        try:
            upstream_deps = self._manifest_reader.get_model_upstream()
            downstream_deps = self._manifest_reader.get_model_downstream()
            model_exposures = self._manifest_reader.get_model_exposures()

            manifest_sources = self._manifest_reader.manifest.get("sources", {})
            for source_id, source_node in manifest_sources.items():
                source_name = source_node.get("source_name")
                source_identifier = (
                    source_node.get("identifier", "").lower()
                    if source_node.get("identifier")
                    else source_node.get("name", "").lower()
                )

                source_model = models.get(source_identifier)
                if source_model and source_model.resource_type == "source" and source_name:
                    source_model.source_name = source_name.lower()

            for model_name, model in models.items():
                model.upstream = upstream_deps.get(model_name, set())
                model.downstream = downstream_deps.get(model_name, set())
                if model_name in model_exposures:
                    model.downstream.update(model_exposures[model_name])
                model.language = self._manifest_reader.get_model_language(model_name)
                model.resource_path = self._manifest_reader.get_model_resource_path(model_name)

                node = self._manifest_reader._find_node(model_name)
                if node:
                    model.tags = node.get("tags", [])
        except Exception as e:
            raise RegistryError(f"Failed to apply dependencies: {e}")

    def _apply_descriptions(self, models: Dict[str, Model]) -> None:
        """Populate model and column descriptions from the dbt-authored docs.

        The *manifest* is the primary source: dbt records the docs a person wrote in
        ``schema.yml`` at ``nodes.<id>.description`` (model) and
        ``nodes.<id>.columns.<name>.description`` (column). The catalog's column
        ``description`` is the warehouse comment (often empty), so the manifest wins
        and the catalog value — already loaded onto the column — is kept only as a
        fallback. An empty/None manifest description never clobbers an existing value.

        Runs *after* lineage parsing so columns materialised from compiled SQL for
        catalog-missing models (see :meth:`_apply_column_lineage`) are covered too.
        """
        for model_name, model in models.items():
            node = self._manifest_reader._find_node(model_name)
            if not node:
                continue

            manifest_model_desc = node.get("description")
            if manifest_model_desc:
                model.description = manifest_model_desc

            manifest_columns = node.get("columns", {}) or {}
            desc_by_column = {
                name.lower(): (col_data or {}).get("description")
                for name, col_data in manifest_columns.items()
            }
            for col_name, column in model.columns.items():
                manifest_desc = desc_by_column.get(col_name)
                if manifest_desc:
                    column.description = manifest_desc

    def _apply_meta(self, models: Dict[str, Model]) -> None:
        """Attach arbitrary dbt ``meta`` from the manifest onto models and columns.

        User-authored meta (ANY key) is namespaced under ``Model.metadata["dbt_meta"]``
        so it stays disjoint from the tool-internal flags already stored there
        (``catalog_missing``, ``star_sources``) — a consumer meta key named
        ``star_sources`` must never corrupt the star-reference pass. Column meta lands on
        ``Column.metadata``. Meta is merged ``config.meta`` over top-level ``meta`` (dbt
        precedence) in the manifest reader. No key is privileged; empty meta is left unset.

        Runs *after* lineage parsing so columns materialised from compiled SQL for
        catalog-missing models are covered too.
        """
        for model_name, model in models.items():
            model_meta = self._manifest_reader.get_model_meta(model_name)
            if model_meta:
                model.metadata = model.metadata or {}
                model.metadata["dbt_meta"] = model_meta

            # The node's resolved dbt ``config`` (grants/materialized/tags/enabled/schema/…),
            # namespaced under ``dbt_config`` so it stays disjoint from the tool-internal flags
            # and from ``dbt_meta``. Model-grained only (dbt config is a model-level notion).
            model_config = self._manifest_reader.get_model_config(model_name)
            if model_config:
                model.metadata = model.metadata or {}
                model.metadata["dbt_config"] = model_config

            column_meta = self._manifest_reader.get_column_meta(model_name)
            if not column_meta:
                continue
            for col_name, column in model.columns.items():
                col_meta = column_meta.get(col_name)
                if col_meta:
                    column.metadata = col_meta

    def _load_exposures(self) -> Dict[str, Exposure]:
        """Load exposures from manifest."""
        exposures = {}
        exposure_data = self._manifest_reader.get_exposures()
        exposure_deps = self._manifest_reader.get_exposure_dependencies()

        for exposure_id, exp_data in exposure_data.items():
            exposure_name = exp_data.get("name")
            if not exposure_name:
                continue

            depends_on_models = exposure_deps.get(exposure_name, set())

            exposure = Exposure(
                name=exposure_name,
                type=exp_data.get("type", "dashboard"),
                url=exp_data.get("url"),
                description=exp_data.get("description"),
                owner=exp_data.get("owner"),
                unique_id=exposure_id,
                depends_on_models=depends_on_models,
                resource_path=exp_data.get("original_file_path"),
                metadata=exp_data.get("meta", {}),
            )
            exposures[exposure_name] = exposure

        return exposures

    def _build_test_index(self) -> None:
        """Build the reverse index (model, column) -> tests from the manifest test nodes.

        A test is only indexed under a (model, column) pair when *both* the target model
        and the target column are known. Tests missing either are set aside as
        unattributable and merely counted (never guessed at) — they surface in the
        coverage honesty signal rather than silently disappearing.
        """
        self._column_tests = {}
        self._referenced_tests = {}
        self._unattributable_tests = []
        self._test_unique_ids = set()
        self._model_tests = {}

        def _attach_to_model(model_name: Optional[str], test: TestNode) -> None:
            if model_name is None:
                return
            bucket = self._model_tests.setdefault(model_name.lower(), [])
            if all(t.unique_id != test.unique_id for t in bucket):
                bucket.append(test)

        for test in self._manifest_reader.get_tests():
            self._test_unique_ids.add(test.unique_id)
            # Every test that a whole-model removal would break: attached to the model, or a
            # relationships test pointing at it from elsewhere.
            _attach_to_model(test.target_model, test)
            _attach_to_model(test.referenced_model, test)
            # A relationships test also depends on its *referenced* (parent) column; index
            # that side too so a change to the parent key can be found. Both sides may be
            # unknown independently.
            if test.referenced_model is not None and test.referenced_column is not None:
                ref_key = (test.referenced_model.lower(), test.referenced_column.lower())
                self._referenced_tests.setdefault(ref_key, []).append(test)

            if test.target_model is None or test.target_column is None:
                self._unattributable_tests.append(test)
                continue
            key = (test.target_model.lower(), test.target_column.lower())
            self._column_tests.setdefault(key, []).append(test)

    def get_column_tests(self, model: str, column: str) -> List[TestNode]:
        """Return the dbt tests targeting ``model.column`` (case-insensitive).

        Returns an empty list for an unknown (model, column) pair or one with no tests.
        """
        return list(self._column_tests.get((model.lower(), column.lower()), []))

    def get_tests_referencing(self, model: str, column: str) -> List[TestNode]:
        """Return relationships tests whose *referenced* (parent) side is ``model.column``.

        These break when the parent key is removed/renamed, distinct from the tests that
        target the column directly (:meth:`get_column_tests`). Case-insensitive; empty when
        nothing references it.
        """
        return list(self._referenced_tests.get((model.lower(), column.lower()), []))

    def get_model_tests(self, model: str) -> List[TestNode]:
        """Every test that breaks if ``model`` is removed wholesale (case-insensitive).

        Includes tests attached to the model and relationships tests referencing it — used
        for whole-model removals, where incomplete column recovery would otherwise miss
        tests on columns we couldn't reconstruct from compiled SQL.
        """
        return list(self._model_tests.get(model.lower(), []))

    def get_test_unique_ids(self) -> Set[str]:
        """All dbt test unique_ids present in this manifest.

        The verdict classifier intersects a base test against this head set to confirm the
        test survived the change: a rename that updated the test's yml yields a *new*
        unique_id, so the base one is absent here and is correctly not flagged as broken.
        """
        return set(self._test_unique_ids)

    def get_unattributable_test_count(self) -> int:
        """Number of test nodes whose (model, column) target could not be attributed.

        These are kept out of the reverse index but counted here so later coverage
        reporting can stay honest about what the index does and does not cover.
        """
        return len(self._unattributable_tests)

    def get_unattributable_tests(self) -> List[TestNode]:
        """The test nodes whose (model, column) target could not be attributed."""
        return list(self._unattributable_tests)

    def _process_lineage(self, models: Dict[str, Model]) -> None:
        """Process and apply column lineage to models."""
        logger = logging.getLogger(__name__)

        if self._sql_parser is None:
            raise RegistryError("SQL parser not initialized. Call load() first.")

        successful_parses = 0
        opaque_parses = 0
        skipped_models = 0
        opaque_model_names = []
        skipped_model_names = []

        # First pass: Process explicit column references
        for model_name, model in models.items():
            if model.language != "sql":
                continue

            sql = self._manifest_reader.get_compiled_sql(model_name)
            if not sql:
                skipped_models += 1
                skipped_model_names.append(model_name)
                continue

            # Nodes we deliberately do NOT column-analyze, detected by shape even when the parser
            # would silently accept them into a structure with no usable column lineage (a
            # ``CREATE SEMANTIC VIEW`` is the motivating case: some parser versions raise on it,
            # others parse it into an opaque command). Treating it as opaque up front — rather
            # than as a node that "parsed" into zero columns — keeps its model-level reach and
            # keeps it out of the resolved/blind buckets.
            if self._is_opaque_node(model_name, sql):
                opaque_parses += 1
                opaque_model_names.append(model_name)
                self._mark_opaque(model)
                continue

            try:
                parse_result = self._sql_parser.parse_column_lineage(sql)
                self._apply_column_lineage(model, parse_result, models)
                successful_parses += 1
            except Exception as e:
                # The parser could not read this compiled SQL (a semantic view, or generally any
                # node sqlglot cannot parse). We deliberately do NOT column-analyze it: mark it
                # OPAQUE rather than a parse failure. Column-level lineage is withheld, but the
                # node's MODEL-level reach is preserved from the manifest dependency graph
                # (``depends_on.nodes`` — already materialized onto ``model.upstream`` and the
                # manifest downstream map), so a change to an upstream still reaches and rebuilds
                # it at model grain.
                opaque_parses += 1
                opaque_model_names.append(model_name)
                self._mark_opaque(model)
                logger.info(
                    f"Model {model_name} is opaque to the column parser "
                    f"({type(e).__name__}); model-level reach preserved from the manifest"
                )
                continue

        self._parse_stats = ParseStats(
            parsed_ok=successful_parses,
            parse_failed=0,
            opaque=opaque_parses,
            skipped_no_sql=skipped_models,
            failed_model_names=[],
            opaque_model_names=opaque_model_names,
            skipped_model_names=skipped_model_names,
        )

        logger.info(
            f"SQL parsing summary: {successful_parses} successful, "
            f"{opaque_parses} opaque (unparseable, not column-analyzed), "
            f"{skipped_models} skipped (no SQL)"
        )

        if opaque_model_names:
            logger.info(
                f"Opaque models ({len(opaque_model_names)}): {', '.join(opaque_model_names)}"
            )

        # Second pass: Process star references
        try:
            self._process_star_references(models)
        except Exception as e:
            logger.error(f"Failed to process star references: {e}", exc_info=True)

    def _is_opaque_node(self, model_name: str, sql: str) -> bool:
        """Whether a node should be treated as column-opaque by its shape, not a parse outcome.

        A semantic view is the motivating case: it has no relational column projection to trace,
        and depending on the parser version it either fails to parse or parses into an opaque
        command with no usable lineage. We detect it from the dbt materialization or the compiled
        SQL shape so it is classified ``opaque`` up front. The general parse-exception fallback in
        :meth:`_process_lineage` still catches any *other* unparseable node.
        """
        config = self._manifest_reader.get_model_config(model_name) or {}
        materialized = str(config.get("materialized") or "").lower()
        if "semantic" in materialized:
            return True
        head = (sql or "").lstrip().lower()
        return head.startswith("create") and "semantic view" in head

    def _mark_opaque(self, model: Model) -> None:
        """Flag a node as opaque and (re)synthesize its MODEL-level upstream edges.

        An opaque node has no column-level lineage (we chose not to parse it), but its
        model-grain dependencies must still hold so downstream reach flows *through* it. Those
        edges come from the manifest's ``depends_on.nodes`` — dbt's own dependency graph, which
        a semantic view's ``TABLES`` clause resolves to ``ref()``s at compile time. They are
        already materialized onto ``model.upstream`` by :meth:`_apply_dependencies`; here we
        re-affirm them from the manifest so the reach is guaranteed even if the upstream set was
        empty, and stamp ``metadata["opaque"]`` so consumers can classify the node.
        """
        model.metadata = model.metadata or {}
        model.metadata["opaque"] = True
        manifest_upstream = self._manifest_reader.get_model_upstream().get(model.name, set())
        if manifest_upstream:
            model.upstream = set(model.upstream or set()) | set(manifest_upstream)

    def _apply_column_lineage(
        self, model: Model, parse_result: SQLParseResult, models: Dict[str, Model]
    ) -> None:
        """Apply parsed lineage to model columns.

        For a catalog-missing model (present in the manifest but absent from the
        catalog) we have no authoritative column list, so we materialise its output
        columns from the parsed final ``SELECT``. Data types are left ``None`` — the
        column is known and traversable, its type merely unknown. Catalog-backed
        models keep the catalog as the authority and only receive lineage on columns
        that already exist there.
        """
        catalog_missing = bool(model.metadata and model.metadata.get("catalog_missing"))
        for col_name, lineage in parse_result.column_lineage.items():
            if col_name not in model.columns:
                if not catalog_missing:
                    continue
                model.columns[col_name] = Column(
                    name=col_name,
                    model_name=model.name,
                    data_type=None,
                )
            model.columns[col_name].lineage = lineage

        model.predicate_sources = set(parse_result.predicate_sources or set())
        model.predicate_lineage = dict(parse_result.predicate_lineage or {})

        if parse_result.star_sources:
            model.metadata = model.metadata or {}
            model.metadata["star_sources"] = list(parse_result.star_sources)

        self._declare_unresolved_edges(model, parse_result, models)

    def _declare_unresolved_edges(
        self, model: Model, parse_result: SQLParseResult, models: Dict[str, Model]
    ) -> None:
        """Finalize the model's unresolved-edge markers and stamp them onto its metadata.

        Two sources are merged (see UnresolvedColumnEdge / UNRESOLVED_EDGES_PLAN.md §4a):

        * **Parser markers** (``phantom_alias`` / ``pivot_output`` / ``star_rename``) — detectable
          from the SQL alone. They arrive with ``model == ""``; we stamp the real node name.
        * **Registry markers** (``unexpandable_star``) — a source token pulled through a
          ``select *`` off a relation that is a *star base* (``metadata["star_sources"]``) yet is
          NOT one of this model's declared upstream dependencies (``model.upstream``). The parser
          cannot tell such a leaked base relation from a real source (both are bare relations); the
          declared-dependency set is the ground truth that only exists here in the registry. When
          the relation IS a declared upstream (the normal staging case, and this same model under a
          full production manifest) the token is genuine and kept — so a legitimate edge is never
          dropped (fail-safe).

        * **Registry markers** (``fabricated_column``) — the DOMINANT phantom on macro-inlined
          models (measured on the full prod catalog): a token qualified by a REAL declared upstream
          relation that IS catalog-backed, but naming a column that does NOT exist in that
          relation's catalog columns (e.g. ``stg_a.owner_id`` where
          ``owner_id`` is a pivot/derived output mis-attributed onto the base relation).
          Both ``phantom_alias`` and ``unexpandable_star`` miss it because the *relation* is real.
          We can only prove absence when the upstream is catalog-backed; if it is NOT catalog-backed
          (a source, or a deferred/unbuilt relation) we cannot prove the column is absent and SKIP
          (never fabricate a marker — that would over-widen). A token whose column DOES exist in the
          immediate upstream's catalog (a legitimate rename/passthrough) is kept, no marker.

        The finalized set is stored on ``model.metadata["unresolved_edges"]`` as a list of dicts,
        mirroring ``star_sources`` — the complete, uncapped machine surface the resolution/confidence pass consumes.
        """
        markers: List[Dict[str, Any]] = []

        # 1. Parser markers: stamp the model name.
        for edge in parse_result.unresolved_edges:
            record = edge.model_dump()
            record["model"] = model.name
            markers.append(record)

        # 2. Registry-detected phantom edges. Both checks need registry ground truth (the
        #    declared-dependency set and the upstreams' catalog columns) that the parser lacks.
        star_sources = set()
        if model.metadata and model.metadata.get("star_sources"):
            star_sources = {str(s).lower() for s in model.metadata["star_sources"]}
        declared_upstreams = {str(u).lower() for u in (model.upstream or set())}
        # A leaked ``select *`` base that is not a declared upstream (disjoint from declared).
        phantom_bases = star_sources - declared_upstreams

        for col_name, column in model.columns.items():
            for lineage in column.lineage or []:
                kept: Set[str] = set()
                for token in lineage.source_columns:
                    reason = self._registry_phantom_reason(
                        token, phantom_bases, declared_upstreams, models
                    )
                    if reason is not None:
                        markers.append(
                            {
                                "model": model.name,
                                "column": col_name,
                                "reason": reason,
                                "detail": token,
                            }
                        )
                    else:
                        kept.add(token)
                lineage.source_columns = kept

        if markers:
            model.metadata = model.metadata or {}
            model.metadata["unresolved_edges"] = self._dedupe_edge_records(markers)

    def _registry_phantom_reason(
        self,
        token: str,
        phantom_bases: Set[str],
        declared_upstreams: Set[str],
        models: Dict[str, Model],
    ) -> Optional[str]:
        """Classify a source token against registry ground truth, or ``None`` to keep it.

        * ``unexpandable_star`` — qualifier is a leaked ``select *`` base, not a declared upstream.
        * ``fabricated_column`` — qualifier is a declared, catalog-backed upstream, but the column
          does not exist in that upstream's catalog columns (a same-named output mis-attributed
          onto a real relation). Fail-safe: only fires when absence is PROVABLE (upstream
          catalog-backed); a column that exists upstream (legit rename/passthrough) is kept.
        """
        qualifier = self._token_qualifier(token)
        if not qualifier:
            return None
        if qualifier in phantom_bases:
            return "unexpandable_star"
        if qualifier in declared_upstreams and self.is_catalog_backed(qualifier):
            upstream = models.get(qualifier)
            if upstream is not None and upstream.columns:
                column_name = self._token_column(token)
                upstream_columns = {name.lower() for name in upstream.columns}
                if column_name and column_name not in upstream_columns:
                    return "fabricated_column"
        return None

    @staticmethod
    def _token_qualifier(token: str) -> str:
        """The lowercased, unquoted table-qualifier of a source token (``p.value`` -> ``p``)."""
        if "." not in token:
            return ""
        return token.rsplit(".", 1)[0].strip().strip('"').lower()

    @staticmethod
    def _token_column(token: str) -> str:
        """The lowercased, unquoted column part of a source token (``a.b`` -> ``b``)."""
        tail = token.rsplit(".", 1)[-1] if "." in token else token
        return tail.strip().strip('"').lower()

    @staticmethod
    def _dedupe_edge_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Order-stable de-dup of marker dicts on (column, reason, detail)."""
        seen: Set[Tuple[Any, Any, Any]] = set()
        unique: List[Dict[str, Any]] = []
        for record in records:
            key = (record.get("column"), record.get("reason"), record.get("detail"))
            if key not in seen:
                seen.add(key)
                unique.append(record)
        return unique

    def _process_star_references(self, models: Dict[str, Model]) -> None:
        """Process star references between models."""
        for model in models.values():
            if not model.metadata or "star_sources" not in model.metadata:
                continue

            for source_name in model.metadata["star_sources"]:
                if source_name not in models:
                    continue

                self._apply_star_columns(model, source_name, models[source_name])

    def _apply_star_columns(self, target: Model, source_name: str, source: Model) -> None:
        """Apply star columns from source to target model.

        For a catalog-MISSING target (present in the manifest but absent from the catalog,
        so no authoritative column list) a ``select *`` passthrough would otherwise recover
        NO columns at all: parsing a pure ``select *`` yields only a model-level star source
        and never an explicit projection, and this method historically only *attached*
        lineage to columns that already existed on the target. That silently dropped the
        entire column-lineage edge for star-passthrough intermediates — e.g. an SCD-1 collapse
        ``select * from <scd_1 cte>`` — whenever they were deferred out of the catalog (a
        common partial/deferred CI build). Every downstream inferred-meta / PII fold that
        walks through such a node then hits an empty upstream and resolves to UNKNOWN,
        silently disarming a fail-closed PII policy. So when the target is catalog-missing we
        MATERIALIZE the star source's columns onto it (types unknown), mirroring the
        catalog-missing branch of :meth:`_apply_column_lineage`.
        """
        catalog_missing = bool(target.metadata and target.metadata.get("catalog_missing"))
        for col_name, source_col in source.columns.items():
            if col_name not in target.columns:
                if not catalog_missing:
                    continue
                target.columns[col_name] = Column(
                    name=col_name,
                    model_name=target.name,
                    data_type=None,
                )

            target_col = target.columns[col_name]
            if not target_col.lineage:
                target_col.lineage = []

            star_lineage = ColumnLineage(
                source_columns={f"{source_name}.{col_name}"},
                transformation_type="direct",
            )

            if not any(
                existing.source_columns == star_lineage.source_columns
                for existing in target_col.lineage
            ):
                target_col.lineage.append(star_lineage)

    def load(self) -> None:
        """Load and initialize the registry."""
        if self.is_loaded:
            raise RegistryError("Registry has already been loaded")

        try:
            self._catalog_reader.load()
            self._manifest_reader.load()

            # Ensure the dialect is set before initializing the parser
            self._dialect = self._adapter_override or self._manifest_reader.get_adapter()

            if self._adapter_override:
                logger.info(f"Using adapter override from CLI: {self._adapter_override}")
            elif self._dialect:
                logger.info(f"Detected dialect: {self._dialect}")
            else:
                logger.warning("No dialect detected, the sql parser will be less accurate")

            self._sql_parser = SQLColumnParser(dialect=self._dialect)

            models = self._initialize_models()
            self._apply_dependencies(models)
            self._process_lineage(models)
            self._apply_descriptions(models)
            self._apply_meta(models)
            exposures = self._load_exposures()
            self._build_test_index()
            self._state = RegistryState(models=models, exposures=exposures, is_loaded=True)
        except Exception as e:
            raise RegistryError(f"Failed to load registry: {e}")

    def get_models(self) -> Dict[str, Model]:
        """Get all models in the registry."""
        if not self.is_loaded:
            raise RegistryNotLoadedError("Registry must be loaded before accessing models")
        return self._state.models

    def get_dialect(self) -> Optional[str]:
        """Return the resolved SQL dialect (adapter), or ``None`` when unknown.

        Public accessor over the dialect the registry already computes at load time
        (adapter override or manifest adapter). Consumed by ``ChangesetBuilder`` so the
        AST semantic-diff canonicalizes expressions with the right dialect rules.
        """
        return self._dialect

    def get_artifact_stamps(self) -> ArtifactStamps:
        """Identity stamps (``generated_at`` / ``invocation_id``) of the loaded artifacts.

        Read from each artifact's ``metadata`` block; a missing block yields empty stamps
        (no identity claim). Consumed by the two-manifest diff to detect a stale or shared
        catalog and stamp the report ``structural_diff: degraded`` instead of staying silent.
        """
        catalog_meta = (self._catalog_reader.catalog or {}).get("metadata") or {}
        manifest_meta = (self._manifest_reader.manifest or {}).get("metadata") or {}
        return ArtifactStamps(
            catalog=ArtifactStamp(
                generated_at=catalog_meta.get("generated_at"),
                invocation_id=catalog_meta.get("invocation_id"),
            ),
            manifest=ArtifactStamp(
                generated_at=manifest_meta.get("generated_at"),
                invocation_id=manifest_meta.get("invocation_id"),
            ),
        )

    def get_catalog_fingerprint(self) -> Optional[str]:
        """Content fingerprint (sha256 over canonical JSON) of the loaded ``catalog.json``.

        Byte-formatting-insensitive: two catalogs with the same CONTENT (the real signal —
        e.g. one prod catalog mounted on both diff sides) fingerprint equal even if
        re-serialized. ``None`` when no catalog content is loaded (no identity claim).
        """
        content = self._catalog_reader.catalog
        if not content:
            return None
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_model(self, model_name: str) -> Model:
        """Get a specific model by name."""
        if not self.is_loaded:
            raise RegistryNotLoadedError("Registry must be loaded before accessing models")

        model = self._state.models.get(model_name.lower())
        if model is None:
            raise ModelNotFoundError(f"Model '{model_name}' not found")
        return model

    def get_model_dbt_meta(self, model: str) -> Dict[str, Any]:
        """Arbitrary user-authored dbt ``meta`` for a model (case-insensitive).

        Reads the meta namespaced under ``Model.metadata["dbt_meta"]`` by
        :meth:`_apply_meta`, kept disjoint from the tool-internal flags. Returns an empty
        dict for an unknown model or one with no declared meta — meta is absent, never
        guessed. Metadata-agnostic: every key is exposed generically, none privileged.
        """
        model_obj = self._state.models.get(model.lower())
        if model_obj is None or not model_obj.metadata:
            return {}
        return dict(model_obj.metadata.get("dbt_meta") or {})

    def get_model_config(self, model: str) -> Dict[str, Any]:
        """The node's resolved dbt ``config`` dict for a model (case-insensitive).

        Reads the config namespaced under ``Model.metadata["dbt_config"]`` by
        :meth:`_apply_meta`, kept disjoint from ``dbt_meta`` and the tool-internal flags.
        Returns an empty dict for an unknown model or one with no config — config is absent,
        never guessed. Metadata-agnostic: every key (``grants``, ``materialized``, ``tags``, …)
        is exposed generically, none privileged. Values are surfaced RAW (no normalization).
        """
        model_obj = self._state.models.get(model.lower())
        if model_obj is None or not model_obj.metadata:
            return {}
        return dict(model_obj.metadata.get("dbt_config") or {})

    def get_column_dbt_meta(self, model: str, column: str) -> Dict[str, Any]:
        """Arbitrary user-authored dbt ``meta`` for a column (case-insensitive).

        Reads ``Column.metadata`` populated by :meth:`_apply_meta`. Returns an empty dict
        for an unknown model/column or one with no meta.
        """
        model_obj = self._state.models.get(model.lower())
        if model_obj is None:
            return {}
        col = model_obj.columns.get(column) or model_obj.columns.get(column.lower())
        if col is None or not col.metadata:
            return {}
        return dict(col.metadata)

    def get_exposures(self) -> Dict[str, Exposure]:
        """Get all exposures in the registry."""
        if not self.is_loaded:
            raise RegistryNotLoadedError("Registry must be loaded before accessing exposures")
        return self._state.exposures

    def get_exposure(self, exposure_name: str) -> Exposure:
        """Get a specific exposure by name."""
        if not self.is_loaded:
            raise RegistryNotLoadedError("Registry must be loaded before accessing exposures")

        exposure = self._state.exposures.get(exposure_name)
        if exposure is None:
            raise ValueError(f"Exposure '{exposure_name}' not found")
        return exposure

    def _count_manifest_models(self) -> int:
        """Count model-like nodes (model/snapshot/seed) declared in the manifest."""
        count = 0
        for node in self._manifest_reader.manifest.get("nodes", {}).values():
            if node.get("resource_type") in _MODEL_LIKE_RESOURCE_TYPES:
                count += 1
        return count

    def get_coverage(self) -> Coverage:
        """Report how completely the loaded artifacts cover the project."""
        if not self.is_loaded:
            raise RegistryNotLoadedError("Registry must be loaded before accessing coverage")

        models_in_manifest = self._count_manifest_models()
        # The universe is now manifest-seeded, so counting every model-like node in the
        # registry would always equal the manifest count. Coverage is about *catalog*
        # completeness, so count only the nodes actually backed by a catalog entry.
        models_in_catalog = len(self._catalog_backed_model_names)
        not_in_catalog_count = max(models_in_manifest - models_in_catalog, 0)

        stats = self._parse_stats
        # ``opaque`` nodes are intentionally not column-analyzed (unparseable SQL, e.g. semantic
        # views). They are NOT a coverage failure, so they are excluded from the ``complete``
        # denominator — only genuine parse failures, missing SQL, and catalog gaps count.
        complete = (
            not_in_catalog_count == 0 and stats.parse_failed == 0 and stats.skipped_no_sql == 0
        )

        return Coverage(
            models_in_manifest=models_in_manifest,
            models_in_catalog=models_in_catalog,
            parsed_ok=stats.parsed_ok,
            parse_failed=stats.parse_failed,
            skipped_no_sql=stats.skipped_no_sql,
            not_in_catalog_count=not_in_catalog_count,
            failed_models=sorted(stats.failed_model_names)[:_COVERAGE_NAME_CAP],
            skipped_models=sorted(stats.skipped_model_names)[:_COVERAGE_NAME_CAP],
            opaque=stats.opaque,
            opaque_models=sorted(stats.opaque_model_names)[:_COVERAGE_NAME_CAP],
            complete=complete,
        )

    def get_unparsed_models(self) -> set:
        """Names of models whose SQL failed to parse, is opaque, or was missing."""
        return (
            set(self._parse_stats.failed_model_names)
            | set(self._parse_stats.opaque_model_names)
            | set(self._parse_stats.skipped_model_names)
        )

    def get_parse_failed_models(self) -> set:
        """Names of models whose compiled SQL was present but genuinely failed to parse.

        Opaque nodes (unparseable SQL we deliberately do NOT column-analyze, e.g. semantic
        views) are NOT parse failures and are reported separately by :meth:`get_opaque_models`.
        """
        return set(self._parse_stats.failed_model_names)

    def get_opaque_models(self) -> set:
        """Names of nodes we deliberately do not column-analyze (unparseable SQL).

        Semantic views chief among them; generally any node the parser cannot read. These are
        classified ``opaque`` (a choice), distinct from ``parse_failed`` (a failure). Their
        model-level reach is preserved from the manifest dependency graph.
        """
        return set(self._parse_stats.opaque_model_names)

    def is_catalog_backed(self, model_name: str) -> bool:
        """Whether a model has a real catalog entry (known column types)."""
        return model_name.lower() in self._catalog_backed_model_names

    def get_manifest_downstream(self) -> Dict[str, set]:
        """Manifest-level downstream child map, covering every model (not just catalog ones)."""
        return self._manifest_reader.get_model_downstream()

    def get_filter_dependents(self, source_column: str) -> set:
        """Models that reference ``source_column`` ONLY in a predicate (filter/join/having).

        These are row-set dependents: a change to ``source_column``'s logic changes which
        rows they keep, and therefore their aggregates — a real impact that column-value
        lineage misses. A model that also *projects* ``source_column`` is excluded here (it
        is already reported as a value impact), so this stays the purely-predicate set.
        """
        if self._filter_dependents is None:
            index: Dict[str, set] = {}
            for name, model in self.get_models().items():
                projected: set = set()
                for column in model.columns.values():
                    for lineage in column.lineage or []:
                        projected |= {s.lower() for s in (lineage.source_columns or set())}
                for src in model.predicate_sources or set():
                    key = src.lower()
                    if key in projected:
                        continue
                    index.setdefault(key, set()).add(name)
            self._filter_dependents = index
        return set(self._filter_dependents.get(source_column.lower(), set()))

    def _check_loaded(self) -> None:
        """Verify registry is loaded before operations"""
        if not self._state.models:
            raise RegistryNotLoadedError("Registry must be loaded before accessing models")

    def _find_compiled_sql(self, model_name: str) -> Optional[str]:
        """Find compiled SQL for a model from manifest or target file."""
        self._check_loaded()
        model_name_lower = model_name.lower()
        model = self._state.models.get(model_name_lower)
        if model is None:
            raise ModelNotFoundError(f"Model '{model_name}' not found in registry")

        # Find in manifest (meaning node has been executed)
        manifest_sql = self._manifest_reader.get_compiled_sql(model_name)
        if manifest_sql:
            model.compiled_sql = manifest_sql
            return manifest_sql

        # If not in manifest, try to read from compiled target file
        compiled_path = self._manifest_reader.get_model_path(model_name)
        if compiled_path:
            try:
                with open(compiled_path, "r") as f:
                    compiled_sql = f.read()
                model.compiled_sql = compiled_sql
                return compiled_sql
            except (FileNotFoundError, IOError):
                pass

        return None

    def get_compiled_sql(self, model_name: str) -> str:
        """Get compiled SQL for a model, trying manifest first then target file."""
        self._check_loaded()
        model_name_lower = model_name.lower()
        model = self._state.models.get(model_name_lower)
        if model is None:
            raise ModelNotFoundError(f"Model '{model_name}' not found in registry")

        if model.compiled_sql:
            return model.compiled_sql

        compiled_sql = self._find_compiled_sql(model_name)
        if compiled_sql:
            return compiled_sql

        raise ValueError(f"No compiled SQL found for model '{model_name}'")
