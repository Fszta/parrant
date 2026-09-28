"""Regression tests for the manifest-seeded model universe.

Before this fix the registry built its model universe *only* from ``catalog.json``,
so any model absent from the catalog was invisible to impact analysis and, in the
two-manifest diff, misreported as removed or "not built in the warehouse yet". This
is the norm under a deferred / partial CI build (``dbt docs generate --defer`` after
building just the ``state:modified+`` cone) and for non-table relations such as
semantic views.

A deferred-CI run exercises TWO distinct shapes that the fix must both get right,
mirrored below:

Shape 1 — filter/join-only consumer (``orders_flag_rate``): reads
``orders`` but uses its columns ONLY in ``WHERE`` / join / aggregate predicates and
projects NO ``orders`` column value to its output (outputs are ``count(*)``-based
ratios plus ``reporting_month`` sourced from ``customers``). It carries no derived
*value* from the change, but a change to ``order_status``'s logic shifts which rows it
keeps — so it is surfaced as a distinct ``filter`` (row-set) impact, not as a derived
column and not silently dropped.

Shape 2 — projecting, genuinely catalog-absent consumer (``orders_fulfilment_flags``):
projects an ``orders`` column value to its output AND is absent from the head catalog
(deferred / not-built, so it is manifest-only). Before the fix it was dropped, mislabeled
"not built", and reported as ``removed``; after the fix it is reported as affected,
``removed`` is false, and it does not pollute the unanalyzable bucket.

A ``materialized: semantic_view`` relation (``orders_semantic_view``) is a distinct case: it
is column-OPAQUE by design — we do not trace its columns, but its model-level reach is kept
from the manifest dependency graph, so a change to ``orders`` still reaches and rebuilds it at
model grain. It is classified ``opaque`` (a deliberate choice), never a parse failure.

A genuinely unanalyzable relation (no catalog columns AND no parseable compiled SQL) is
honestly labelled ("no column-level information", never "not built").
"""

import json

from parrant.artifacts.registry import ModelRegistry
from parrant.lineage.changeset import ChangeKind, ChangesetBuilder
from parrant.lineage.display.markdown import _confidence_reason_words
from parrant.lineage.service import LineageService

# --- fixture helpers -------------------------------------------------------


def _catalog_node(name, columns, schema="s", database="d"):
    return {
        "unique_id": f"model.p.{name}",
        "metadata": {"name": name, "schema": schema, "database": database, "type": "BASE TABLE"},
        "columns": {c: {"name": c, "type": "TEXT"} for c in columns},
    }


def _manifest_node(
    name,
    compiled=None,
    depends_on=None,
    schema="s",
    database="d",
    language="sql",
    materialized="table",
):
    node = {
        "name": name,
        "unique_id": f"model.p.{name}",
        "resource_type": "model",
        "language": language,
        "schema": schema,
        "database": database,
        "config": {"materialized": materialized},
        "depends_on": {"nodes": [f"model.p.{d}" for d in (depends_on or [])]},
    }
    if compiled is not None:
        node["compiled_code"] = compiled
    return node


def _write(tmp_path, tag, catalog_nodes, manifest_nodes):
    catalog_path = tmp_path / f"{tag}_catalog.json"
    manifest_path = tmp_path / f"{tag}_manifest.json"
    catalog_path.write_text(
        json.dumps({"metadata": {"adapter_type": "snowflake"}, "nodes": catalog_nodes})
    )
    manifest_path.write_text(
        json.dumps({"metadata": {"adapter_type": "snowflake"}, "nodes": manifest_nodes})
    )
    return str(catalog_path), str(manifest_path)


# --- the analytics scenario ------------------------------------------------

_ORDERS_COLUMNS = [
    "customer_id",
    "order_status",
    "last_flagged_at",
    "order_cancel_requested_at",
    "order_cancelled_at",
    "order_cancel_reason",
]

# orders: a mart whose compiled SQL changes (a pure logic edit to order_status).
_ORDERS_BASE = (
    "select "
    "dim.customer_id as customer_id, "
    "dim.order_status as order_status, "
    "dim.last_flagged_at as last_flagged_at, "
    "dim.order_cancel_requested_at as order_cancel_requested_at, "
    "dim.order_cancelled_at as order_cancelled_at, "
    "dim.order_cancel_reason as order_cancel_reason "
    "from {db}.{schema}.dim_orders as dim"
)
_ORDERS_HEAD = (
    "select "
    "dim.customer_id as customer_id, "
    "coalesce(dim.order_status, dim.fallback_status) as order_status, "
    "dim.last_flagged_at as last_flagged_at, "
    "dim.order_cancel_requested_at as order_cancel_requested_at, "
    "dim.order_cancelled_at as order_cancelled_at, "
    "dim.order_cancel_reason as order_cancel_reason "
    "from {db}.{schema}.dim_orders as dim"
)

# Shape 1: orders_flag_rate. orders columns appear ONLY in the WHERE of the
# `suspended` CTE; the projected outputs are reporting_month (from customers) and
# two count(*)-based ratios. No orders column VALUE reaches an output column.
_FLAG_RATE = (
    "with "
    "customers as (select * from {db}.{schema}.customers), "
    "orders as (select * from {db}.{schema}.orders), "
    "suspended as ("
    "  select "
    "    date_trunc('month', customers.first_seen_at) as reporting_month, "
    "    count(*) as breached_count "
    "  from customers "
    "  inner join orders on orders.customer_id = customers.customer_id "
    "  where orders.order_status = 'Flagged' "
    "     or (orders.order_status in ('Cancelling', 'Cancelled') "
    "         and orders.order_cancel_reason in ('fraud')) "
    "  group by 1"
    "), "
    "cohort as ("
    "  select "
    "    date_trunc('month', customers.first_seen_at) as reporting_month, "
    "    count(*) as total_count "
    "  from customers "
    "  group by 1"
    "), "
    "final as ("
    "  select "
    "    cohort.reporting_month as reporting_month, "
    "    suspended.breached_count / nullif(cohort.total_count, 0) as m0_breach_rate, "
    "    suspended.breached_count / nullif(cohort.total_count, 0) as m3_breach_rate "
    "  from cohort "
    "  left join suspended on cohort.reporting_month = suspended.reporting_month"
    ") "
    "select * from final"
)

# Shape 2: orders_fulfilment_flags. Projects orders columns — including the CHANGED
# one (order_status) — so it is genuinely affected under per-column precision.
_FULFILMENT = (
    "with "
    "orders as (select * from {db}.{schema}.orders), "
    "final as ("
    "  select "
    "    orders.customer_id as customer_id, "
    "    orders.order_status as order_status, "
    "    true as is_otif "
    "  from orders"
    ") "
    "select * from final"
)

# A real semantic_view that projects the changed orders column (compiled to a SELECT).
_SEMANTIC_VIEW = (
    "with "
    "orders as (select * from {db}.{schema}.orders), "
    "final as ("
    "  select "
    "    orders.customer_id as customer_id, "
    "    orders.order_status as order_status "
    "  from orders"
    ") "
    "select * from final"
)


def _manifest_nodes(env, orders_sql, *, otif_in_manifest=True):
    """Build the manifest node set for one environment ('QA' head or 'spec' base)."""
    db = "ANALYTICS_QA" if env == "QA" else "ANALYTICS_PRD"
    prefix = "MR_PR_validate" if env == "QA" else "spec"

    def sch(layer):
        return f"{prefix}_{layer}"

    nodes = {
        "model.p.dim_orders": _manifest_node(
            "dim_orders", compiled="select 1 as order_status", schema=sch("star"), database=db
        ),
        "model.p.customers": _manifest_node(
            "customers",
            compiled="select 1 as customer_id, current_date as first_seen_at",
            schema=sch("star"),
            database=db,
        ),
        "model.p.orders": _manifest_node(
            "orders",
            compiled=orders_sql.format(db=db, schema=sch("marts")),
            depends_on=["dim_orders"],
            schema=sch("marts"),
            database=db,
        ),
        "model.p.orders_flag_rate": _manifest_node(
            "orders_flag_rate",
            # neutral relation refs -> identical text both envs, so it is never flagged
            # by its OWN logic diff; the only way it could surface is the orders fan-out.
            compiled=_FLAG_RATE.format(db="analytics", schema="marts"),
            depends_on=["orders", "customers"],
            schema=sch("metrics"),
            database=db,
        ),
        "model.p.orders_fulfilment_flags": _manifest_node(
            "orders_fulfilment_flags",
            compiled=_FULFILMENT.format(db=db, schema=sch("marts")),
            depends_on=["orders"],
            schema=sch("intermediate"),
            database=db,
        ),
        "model.p.orders_semantic_view": _manifest_node(
            "orders_semantic_view",
            compiled=_SEMANTIC_VIEW.format(db=db, schema=sch("marts")),
            depends_on=["orders"],
            schema=sch("marts"),
            database=db,
            materialized="semantic_view",
        ),
    }
    if not otif_in_manifest:
        del nodes["model.p.orders_fulfilment_flags"]
    return nodes


def _catalog_nodes(env, *, include_absent):
    """Catalog for one environment. When include_absent is False, the two projecting
    consumers (otif + semantic view) are omitted, modelling a deferred/partial build
    where they were NOT written to the catalog even though they are in the manifest."""
    prefix = "MR_PR_validate" if env == "QA" else "spec"
    db = "ANALYTICS_QA" if env == "QA" else "ANALYTICS_PRD"

    def sch(layer):
        return f"{prefix}_{layer}"

    nodes = {
        "model.p.dim_orders": _catalog_node("dim_orders", ["order_status"], sch("star"), db),
        "model.p.customers": _catalog_node(
            "customers", ["customer_id", "first_seen_at"], sch("star"), db
        ),
        "model.p.orders": _catalog_node("orders", _ORDERS_COLUMNS, sch("marts"), db),
        # breach_rate is in the built modified+ cone, so it IS catalogued in both envs.
        "model.p.orders_flag_rate": _catalog_node(
            "orders_flag_rate",
            ["reporting_month", "m0_breach_rate", "m3_breach_rate"],
            sch("metrics"),
            db,
        ),
    }
    if include_absent:
        nodes["model.p.orders_fulfilment_flags"] = _catalog_node(
            "orders_fulfilment_flags",
            ["customer_id", "order_status", "is_otif"],
            sch("intermediate"),
            db,
        )
        nodes["model.p.orders_semantic_view"] = _catalog_node(
            "orders_semantic_view",
            ["customer_id", "order_status"],
            sch("marts"),
            db,
        )
    return nodes


def _base(tmp_path):
    """Base (prod) service: everything built & catalogued in ANALYTICS_PRD.PRD_*."""
    cat, man = _write(
        tmp_path,
        "base",
        _catalog_nodes("spec", include_absent=True),
        _manifest_nodes("spec", _ORDERS_BASE),
    )
    return LineageService(cat, man, adapter="snowflake")


def _head(tmp_path):
    """Head (QA) service: orders + breach_rate built & catalogued in
    ANALYTICS_QA.MR_PR_validate_*; otif + semantic view are manifest-only (deferred /
    not built, so absent from the head catalog). Same unique_ids as base, divergent
    database + schema — exercising the multi-schema / multi-database divergence."""
    cat, man = _write(
        tmp_path,
        "head",
        _catalog_nodes("QA", include_absent=False),
        _manifest_nodes("QA", _ORDERS_HEAD),
    )
    return LineageService(cat, man, adapter="snowflake")


def _changeset_impact(tmp_path):
    base = _base(tmp_path)
    head = _head(tmp_path)
    changes = ChangesetBuilder(base.registry, head.registry).build()
    agg = head.get_changeset_impact(changes, base_service=base)
    return base, head, changes, agg


# --- Shape 1: filter/join-only consumer is a ROW-SET impact ----------------


def test_shape1_filter_join_only_consumer_is_flagged_as_row_set_impact(tmp_path):
    base, head, changes, agg = _changeset_impact(tmp_path)

    # breach_rate is catalog-backed (it was in the built cone) and fully analyzable.
    assert head.registry.is_catalog_backed("orders_flag_rate") is True
    breach = head.registry.get_model("orders_flag_rate")
    # No output column of breach_rate carries any orders.* VALUE — its dependency on
    # order_status is purely a predicate (WHERE), so it must not be a *derived* impact...
    all_sources = {
        src
        for col in breach.columns.values()
        for lineage in (col.lineage or [])
        for src in lineage.source_columns
    }
    assert not any(s.startswith("orders.") for s in all_sources), all_sources

    # ...but it IS a row-set impact: a change to order_status shifts which rows it keeps,
    # so it surfaces as a 'filter'-severity dependent (not derived, not pass-through).
    impact = head.get_column_impact("orders", "order_status")
    assert "orders_flag_rate" in {m["name"] for m in impact["affected_models"]}
    breach_cols = [c for c in impact["affected_columns"] if c["model"] == "orders_flag_rate"]
    assert breach_cols and all(c["severity"] == "filter" for c in breach_cols), breach_cols

    # And it rides through to the aggregated changeset blast radius, still not "removed".
    assert "orders_flag_rate" in {m["name"] for m in agg["affected_models"]}
    assert not any(c.model == "orders_flag_rate" and c.kind == ChangeKind.REMOVED for c in changes)
    # Being analyzable, it does not inflate the unanalyzable/confidence buckets.
    conf = agg["confidence"]
    assert "orders_flag_rate" not in conf["no_column_info_models"]
    assert "orders_flag_rate" not in conf["parse_failed_models"]


# --- Shape 2: projecting, catalog-absent consumer must be INCLUDED ----------


def test_shape2_projecting_catalog_absent_consumer_is_included(tmp_path):
    base, head, changes, agg = _changeset_impact(tmp_path)

    # otif is manifest-only in head (absent from the head catalog), across a divergent
    # database + schema from base — matched by unique_id/name all the same.
    assert head.registry.is_catalog_backed("orders_fulfilment_flags") is False
    otif = head.registry.get_model("orders_fulfilment_flags")
    assert otif.database == "ANALYTICS_QA" and otif.schema_name == "MR_PR_validate_intermediate"
    assert base.registry.get_model("orders_fulfilment_flags").database == "ANALYTICS_PRD"
    # Columns recovered from compiled SQL; customer_id traces to orders.
    assert otif.columns["customer_id"].lineage[0].source_columns == {"orders.customer_id"}

    affected = {m["name"] for m in agg["affected_models"]}
    assert "orders_fulfilment_flags" in affected
    # Affected via the CHANGED column (order_status), recovered from compiled SQL.
    assert ("orders_fulfilment_flags", "order_status") in {
        (c["model"], c["column"]) for c in agg["affected_columns"]
    }
    # Not misclassified as removed just because it left the head catalog.
    assert not any(
        c.model == "orders_fulfilment_flags" and c.kind == ChangeKind.REMOVED for c in changes
    )
    # It is analyzable via parsed SQL, so it does not land in any unanalyzable bucket.
    conf = agg["confidence"]
    assert "orders_fulfilment_flags" not in conf["no_column_info_models"]
    assert "orders_fulfilment_flags" not in conf["parse_failed_models"]


def test_shape2_semantic_view_is_opaque_with_model_grain_reach(tmp_path):
    """A ``materialized: semantic_view`` relation is column-OPAQUE: we deliberately do not
    trace its columns, but its MODEL-level reach is preserved, so a change to ``orders`` still
    reaches and rebuilds it at model grain. It is classified ``opaque`` (a choice), never a
    parse failure, and no column-level edge is fabricated for it."""
    base, head, changes, agg = _changeset_impact(tmp_path)

    sv = "orders_semantic_view"
    assert head.registry.is_catalog_backed(sv) is False
    confidence = agg["confidence"]
    selection = agg["selection"]
    # Opaque, not a failure, and folded into the rebuild set (model-grain reach preserved).
    assert sv in confidence["opaque_models"]
    assert sv not in confidence["parse_failed_models"]
    assert sv in selection["rebuild_models"]
    # No column-level edge is invented for an opaque node.
    assert sv not in {c["model"] for c in agg["affected_columns"]}
    assert not any(c.model == sv and c.kind == ChangeKind.REMOVED for c in changes)


# --- registry seeding / coverage -------------------------------------------


def test_manifest_only_model_is_seeded_and_parsed(tmp_path):
    """A model present in the manifest but absent from the catalog is registered and
    analyzed from its compiled SQL; coverage still honestly reports the catalog gap."""
    catalog = {"model.p.a": _catalog_node("a", ["id"])}
    manifest = {
        "model.p.a": _manifest_node("a", compiled="select 1 as id"),
        "model.p.b": _manifest_node(
            "b", compiled="select a.id as id from d.s.a as a", depends_on=["a"]
        ),
    }
    cat, man = _write(tmp_path, "only", catalog, manifest)
    registry = ModelRegistry(cat, man, adapter_override="snowflake")
    registry.load()

    models = registry.get_models()
    assert "b" in models  # seeded from the manifest despite being absent from the catalog
    assert list(models["b"].columns) == ["id"]  # columns recovered from compiled SQL
    assert models["b"].columns["id"].data_type is None  # type unknown, honestly
    assert registry.is_catalog_backed("b") is False

    coverage = registry.get_coverage()
    assert coverage.models_in_manifest == 2
    assert coverage.models_in_catalog == 1  # only 'a' is catalog-backed
    assert coverage.not_in_catalog_count == 1
    assert coverage.parsed_ok == 2  # both parsed; catalog gap != analyzability gap


def test_unanalyzable_model_is_labelled_honestly_not_as_unbuilt(tmp_path):
    """A reachable downstream with neither catalog columns nor parseable SQL (e.g. a
    semantic view whose body is not a SELECT, or a python model) is reported as
    unanalyzable via ``no_column_info`` and never claimed to be 'not built'."""
    catalog = {"model.p.a": _catalog_node("a", ["id"])}
    manifest = {
        "model.p.a": _manifest_node("a", compiled="select 1 as id"),
        # no compiled SELECT body to recover columns from.
        "model.p.opaque": _manifest_node("opaque", compiled=None, depends_on=["a"]),
    }
    cat, man = _write(tmp_path, "opaque", catalog, manifest)
    service = LineageService(cat, man, adapter="snowflake")

    impact = service.get_column_impact("a", "id")
    conf = impact["confidence"]
    assert conf["level"] == "partial"
    assert conf["no_column_info"] == 1
    assert "opaque" in conf["no_column_info_models"]
    assert conf["parse_failed"] == 0

    reason = _confidence_reason_words(conf)
    assert "no column-level information" in reason
    assert "haven't been built in the warehouse yet" not in reason


def test_model_absent_from_head_manifest_is_still_removed(tmp_path):
    """Guard the other direction: a model truly gone from the head *manifest* (not
    merely from the catalog) is still classified as removed."""
    base_catalog = {
        "model.p.a": _catalog_node("a", ["id"]),
        "model.p.gone": _catalog_node("gone", ["x"]),
    }
    base_manifest = {
        "model.p.a": _manifest_node("a", compiled="select 1 as id"),
        "model.p.gone": _manifest_node("gone", compiled="select 2 as x"),
    }
    head_catalog = {"model.p.a": _catalog_node("a", ["id"])}
    head_manifest = {"model.p.a": _manifest_node("a", compiled="select 1 as id")}

    bcat, bman = _write(tmp_path, "rmbase", base_catalog, base_manifest)
    hcat, hman = _write(tmp_path, "rmhead", head_catalog, head_manifest)
    base = LineageService(bcat, bman, adapter="snowflake")
    head = LineageService(hcat, hman, adapter="snowflake")

    changes = ChangesetBuilder(base.registry, head.registry).build()
    assert any(
        c.model == "gone" and c.column == "x" and c.kind == ChangeKind.REMOVED for c in changes
    )
