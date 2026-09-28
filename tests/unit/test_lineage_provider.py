"""Conformance proof for the:mod:`lineage.provider` seam.

Two things are proven here:

1. The concrete SQLGlot adapter (``SqlglotLineageProvider``) and a *non-registry*
   in-memory fake both satisfy the ``LineageProvider`` / ``ProjectMetadataProvider`` /
   ``LineageAndMetadataProvider`` Protocols at runtime (``isinstance``) and — implicitly,
   via mypy on this file — structurally.
2. The real consumers (``ChangesetBuilder``, ``classify_provable_breaks``, and
   ``LineageService`` traversal/impact) drive off the fake with **no registry involved**.
   That is the durable guard that the product layers depend on the interface, not on how
   lineage is computed — the whole point of the seam and the regression net for a future
   Fusion/warehouse backend swap.

The fake is deliberately hand-written (not a ``ModelRegistry`` subclass) so a real backend
that shares no code with today's engine is what exercises the consumers.
"""

from __future__ import annotations

from typing import Any

from parrant.artifacts.exceptions import ModelNotFoundError
from parrant.lineage.changeset import ChangeKind, ChangesetBuilder
from parrant.lineage.provider import (
    LineageAndMetadataProvider,
    LineageProvider,
    ProjectMetadataProvider,
)
from parrant.lineage.service import LineageService
from parrant.lineage.sqlglot_provider import SqlglotLineageProvider
from parrant.lineage.verdict import classify_provable_breaks
from parrant.models.schema import (
    Column,
    ColumnLineage,
    Coverage,
    Exposure,
    Model,
    TestNode,
)


class InMemoryProvider:
    """A minimal, from-scratch backend implementing the whole seam over plain dicts.

    Shares no code with ``ModelRegistry`` — it stands in for a hypothetical Fusion/warehouse
    provider. Only what the interface declares is implemented.
    """

    def __init__(
        self,
        models: dict[str, Model],
        *,
        exposures: dict[str, Exposure] | None = None,
        dialect: str | None = None,
        downstream: dict[str, set[str]] | None = None,
        compiled: dict[str, str] | None = None,
        column_tests: dict[tuple, list[TestNode]] | None = None,
        catalog_backed: set[str] | None = None,
        parse_failed: set[str] | None = None,
        opaque: set[str] | None = None,
    ) -> None:
        self._models = {name.lower(): model for name, model in models.items()}
        self._exposures = exposures or {}
        self._dialect = dialect
        self._downstream = downstream or {}
        self._compiled = {name.lower(): sql for name, sql in (compiled or {}).items()}
        self._column_tests = column_tests or {}
        self._catalog_backed = (
            {n.lower() for n in catalog_backed} if catalog_backed is not None else set(self._models)
        )
        self._parse_failed = {n.lower() for n in (parse_failed or set())}
        self._opaque = {n.lower() for n in (opaque or set())}
        self._loaded = False

    # --- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            raise RuntimeError("already loaded")
        self._loaded = True

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # --- model / graph access ---------------------------------------------
    def get_models(self) -> dict[str, Model]:
        return self._models

    def get_model(self, model_name: str) -> Model:
        model = self._models.get(model_name.lower())
        if model is None:
            raise ModelNotFoundError(f"Model '{model_name}' not found")
        return model

    def get_manifest_downstream(self) -> dict[str, set[str]]:
        return self._downstream

    # --- column lineage ----------------------------------------------------
    def get_column_lineage(self, model_name: str, column_name: str) -> list[ColumnLineage]:
        column = self.get_column(model_name, column_name)
        return list(column.lineage or []) if column is not None else []

    def get_column(self, model_name: str, column_name: str) -> Column | None:
        try:
            model = self.get_model(model_name)
        except ModelNotFoundError:
            return None
        return model.columns.get(column_name) or model.columns.get(column_name.lower())

    # --- capabilities ------------------------------------------------------
    def get_filter_dependents(self, source_column: str) -> set[str]:
        return set()

    def get_dialect(self) -> str | None:
        return self._dialect

    def get_coverage(self) -> Coverage:
        return Coverage(
            models_in_manifest=len(self._models),
            models_in_catalog=len(self._catalog_backed),
            parsed_ok=len(self._models),
            parse_failed=len(self._parse_failed),
            skipped_no_sql=0,
            not_in_catalog_count=max(len(self._models) - len(self._catalog_backed), 0),
            opaque=len(self._opaque),
            opaque_models=sorted(self._opaque),
            complete=not self._parse_failed,
        )

    def is_catalog_backed(self, model_name: str) -> bool:
        return model_name.lower() in self._catalog_backed

    def get_parse_failed_models(self) -> set[str]:
        return set(self._parse_failed)

    def get_opaque_models(self) -> set[str]:
        return set(self._opaque)

    def get_compiled_sql(self, model_name: str) -> str | None:
        return self._compiled.get(model_name.lower())

    # --- metadata ----------------------------------------------------------
    def get_exposures(self) -> dict[str, Exposure]:
        return self._exposures

    def get_exposure(self, exposure_name: str) -> Exposure:
        exposure = self._exposures.get(exposure_name)
        if exposure is None:
            raise ValueError(f"Exposure '{exposure_name}' not found")
        return exposure

    def get_column_tests(self, model: str, column: str) -> list[TestNode]:
        return list(self._column_tests.get((model.lower(), column.lower()), []))

    def get_tests_referencing(self, model: str, column: str) -> list[TestNode]:
        return []

    def get_model_tests(self, model: str) -> list[TestNode]:
        found: list[TestNode] = []
        for (test_model, _column), tests in self._column_tests.items():
            if test_model == model.lower():
                found.extend(tests)
        return found

    def get_test_unique_ids(self) -> set[str]:
        return {t.unique_id for tests in self._column_tests.values() for t in tests}

    def get_unattributable_test_count(self) -> int:
        return 0

    def get_model_dbt_meta(self, model: str) -> dict[str, Any]:
        return {}

    def get_column_dbt_meta(self, model: str, column: str) -> dict[str, Any]:
        return {}

    def get_model_config(self, model: str) -> dict[str, Any]:
        return {}


# --- fixtures --------------------------------------------------------------


def _model(name: str, columns: dict[str, Column], **kwargs) -> Model:
    return Model(
        name=name, schema="main", database="main", resource_type="model", columns=columns, **kwargs
    )


def _two_model_graph(mart_expr: str) -> InMemoryProvider:
    """stg.id  --direct-->  mart.id (derived from ``mart_expr``)."""
    stg = _model(
        "stg",
        {"id": Column(name="id", model_name="stg", data_type="int")},
        downstream={"mart"},
    )
    mart = _model(
        "mart",
        {
            "id": Column(
                name="id",
                model_name="mart",
                data_type="int",
                lineage=[
                    ColumnLineage(
                        source_columns={"stg.id"},
                        transformation_type="derived",
                        sql_expression=mart_expr,
                    )
                ],
            )
        },
        upstream={"stg"},
    )
    return InMemoryProvider(
        {"stg": stg, "mart": mart},
        dialect="snowflake",
        downstream={"stg": {"mart"}, "mart": set()},
        compiled={"stg": "select id from raw", "mart": f"select {mart_expr} as id from stg"},
    )


def _service_on(provider: LineageAndMetadataProvider) -> LineageService:
    """A LineageService driven by an arbitrary provider (bypasses path-based __init__)."""
    service = LineageService.__new__(LineageService)
    service.registry = provider
    service._coverage = provider.get_coverage()
    return service


# --- conformance -----------------------------------------------------------


def test_sqlglot_provider_is_a_conforming_provider():
    # Construct (not load): __init__ only wires the readers, so no artifacts are touched,
    # yet every interface member is present for the runtime isinstance checks.
    provider = SqlglotLineageProvider("catalog.json", "manifest.json")
    assert isinstance(provider, LineageProvider)
    assert isinstance(provider, ProjectMetadataProvider)
    assert isinstance(provider, LineageAndMetadataProvider)


def test_in_memory_fake_satisfies_the_protocols():
    fake = _two_model_graph("stg.id")
    assert isinstance(fake, LineageProvider)
    assert isinstance(fake, ProjectMetadataProvider)
    assert isinstance(fake, LineageAndMetadataProvider)


def test_static_typing_accepts_the_fake() -> None:
    # The ``-> None`` annotation makes mypy type-check this body: assigning the fake to the
    # interface types proves structural conformance at type-check time, not just at runtime.
    fake: LineageAndMetadataProvider = _two_model_graph("stg.id")
    lineage: LineageProvider = fake
    metadata: ProjectMetadataProvider = fake
    assert lineage.get_dialect() == "snowflake"
    assert metadata.get_unattributable_test_count() == 0


# --- consumers driven by the fake -----------------------------------------


def test_changeset_builder_runs_against_the_fake():
    base = _two_model_graph("stg.id")
    head = _two_model_graph("stg.id * 2")  # mart.id derivation changed
    changes = ChangesetBuilder(base, head).build()
    logic = {(c.model, c.column, c.kind) for c in changes}
    assert ("mart", "id", ChangeKind.LOGIC_CHANGED) in logic


def test_service_impact_runs_against_the_fake():
    provider = _two_model_graph("stg.id")
    service = _service_on(provider)
    impact = service.get_column_impact("stg", "id")
    affected = {m["name"] for m in impact["affected_models"]}
    assert "mart" in affected
    # confidence is computed off get_manifest_downstream + get_parse_failed_models
    assert impact["confidence"]["level"] == "full"


def test_verdict_runs_against_the_fake():
    # A removed column that a dbt test still targets is a provable break — proven with a
    # non-registry provider on both sides.
    test = TestNode(
        unique_id="test.demo.not_null_mart_id",
        test_name="not_null",
        target_model="mart",
        target_column="id",
    )
    base = InMemoryProvider(
        {"mart": _model("mart", {"id": Column(name="id", model_name="mart", data_type="int")})},
        column_tests={("mart", "id"): [test]},
    )
    # head: the whole model (and its column) is gone, but the test declaration survives.
    head = InMemoryProvider({}, column_tests={("mart", "id"): [test]})

    from parrant.lineage.changeset import ColumnChange

    breaks = classify_provable_breaks([ColumnChange("mart", "id", ChangeKind.REMOVED)], head, base)
    assert [b.test_unique_id for b in breaks] == ["test.demo.not_null_mart_id"]
