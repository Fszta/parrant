"""Per-model resolution status + aggregate summary (display/emission only).

``build_resolution`` retains the reachable partition parrant already computes for the
confidence block, labelling every reachable model with a ``status`` (resolved:
``catalog_backed``/``parsed``; unanalyzable: ``no_column_info``/``parse_failed``/
``unresolved``) and a coarse, advisory ``reason``. These tests lock the status inference,
the reason taxonomy, the aggregate roll-up, and — most importantly — that the per-model
statuses reconcile exactly with the confidence counts and the rebuild/skippable sets.
"""

from parrant.lineage.changeset import ChangesetBuilder
from parrant.lineage.service import build_resolution
from parrant.models.schema import Column, ColumnLineage, Model
from tests.unit.test_lineage_provider import InMemoryProvider, _model, _service_on


def _col(name: str) -> Column:
    return Column(name=name, model_name="m", data_type="int")


def _resolution_provider() -> InMemoryProvider:
    """One provider carrying a model per resolution outcome we want to assert."""
    catalog_model = _model("catalog_backed", {"id": _col("id")})
    parsed_model = _model("parsed", {"id": _col("id")})
    star_cte = _model("star_cte", {}, metadata={"star_sources": ["some_cte"]})
    star_mod = _model("star_mod", {}, metadata={"star_sources": ["src"]})
    missing = _model("missing", {})
    py_model = _model("py_model", {}, language="python")
    broke = _model("broke", {})
    models: dict[str, Model] = {
        "catalog_backed": catalog_model,
        "parsed": parsed_model,
        "star_cte": star_cte,
        "star_mod": star_mod,
        "missing": missing,
        "py_model": py_model,
        "broke": broke,
    }
    return InMemoryProvider(
        models,
        # Only the two column-carrying models plus "missing" would default to catalog-backed;
        # pin the set so "parsed" and "missing" are explicitly NOT catalog-backed.
        catalog_backed={"catalog_backed"},
        parse_failed={"broke"},
        compiled={
            "star_cte": "select * from some_cte",
            "star_mod": "select * exclude (secret) from src",
        },
    )


def _statuses(reachable):
    provider = _resolution_provider()
    service = _service_on(provider)
    partition = service._partition_reachable(set(reachable))
    per_model, summary = build_resolution(provider, partition, set())
    return per_model, summary


def test_catalog_backed_model_is_resolved_with_no_reason() -> None:
    per_model, _ = _statuses({"catalog_backed"})
    assert per_model["catalog_backed"] == {"status": "catalog_backed", "reason": None}


def test_columns_without_catalog_entry_are_parsed() -> None:
    per_model, _ = _statuses({"parsed"})
    assert per_model["parsed"] == {"status": "parsed", "reason": None}


def test_star_off_cte_is_no_column_info() -> None:
    per_model, _ = _statuses({"star_cte"})
    assert per_model["star_cte"] == {"status": "no_column_info", "reason": "star_off_cte"}


def test_star_modifier_is_detected() -> None:
    per_model, _ = _statuses({"star_mod"})
    assert per_model["star_mod"] == {"status": "no_column_info", "reason": "star_modifier"}


def test_model_absent_from_catalog_is_missing_catalog() -> None:
    per_model, _ = _statuses({"missing"})
    assert per_model["missing"] == {"status": "no_column_info", "reason": "missing_catalog"}


def test_python_model_is_unresolved() -> None:
    per_model, _ = _statuses({"py_model"})
    assert per_model["py_model"] == {"status": "unresolved", "reason": "python_model"}


def test_parse_failed_model_is_unsupported_sql() -> None:
    per_model, _ = _statuses({"broke"})
    assert per_model["broke"] == {"status": "parse_failed", "reason": "unsupported_sql"}


def test_every_reachable_model_has_exactly_one_status() -> None:
    reachable = {"catalog_backed", "parsed", "star_cte", "star_mod", "missing", "py_model", "broke"}
    per_model, summary = _statuses(reachable)
    # Exactly the reachable set, once each.
    assert set(per_model) == reachable
    resolved = {"catalog_backed", "parsed"}
    for name, entry in per_model.items():
        is_resolved = entry["status"] in ("catalog_backed", "parsed")
        assert is_resolved == (name in resolved)
        # Resolved models carry no reason; unanalyzable ones always carry a coarse reason.
        assert (entry["reason"] is None) == is_resolved
    assert summary["reachable"] == len(reachable)


def test_top_reasons_are_ranked_by_count_then_name() -> None:
    reachable = {"star_cte", "star_mod", "missing", "py_model", "broke"}
    _, summary = _statuses(reachable)
    top = summary["top_reasons"]
    # Every unanalyzable model contributes exactly one reason; deterministic ordering.
    counts = [(entry["reason"], entry["count"]) for entry in top]
    assert sum(c for _, c in counts) == len(reachable)
    ordering = sorted(counts, key=lambda kv: (-kv[1], kv[0]))
    assert counts == ordering


def test_reason_is_advisory_and_never_changes_status() -> None:
    # Stripping every reason leaves the status map byte-identical: reason is never load-bearing.
    reachable = {"catalog_backed", "star_cte", "py_model", "broke"}
    per_model, _ = _statuses(reachable)
    stripped = {name: entry["status"] for name, entry in per_model.items()}
    assert stripped == {
        "catalog_backed": "catalog_backed",
        "star_cte": "no_column_info",
        "py_model": "unresolved",
        "broke": "parse_failed",
    }


def test_rebuild_forced_by_nonresolution_counts_only_unresolved_rebuilds() -> None:
    provider = _resolution_provider()
    service = _service_on(provider)
    reachable = {"catalog_backed", "star_cte", "py_model", "broke"}
    partition = service._partition_reachable(reachable)
    # A rebuild set spanning one resolved and two unanalyzable models.
    rebuild = {"catalog_backed", "star_cte", "py_model"}
    _, summary = build_resolution(provider, partition, rebuild)
    # catalog_backed is resolved -> excluded; star_cte + py_model are not -> counted.
    assert summary["rebuild_forced_by_nonresolution"] == 2
    assert summary["rebuild_forced_by_nonresolution"] <= len(rebuild)


def _recon_graph(stg_expr: str) -> InMemoryProvider:
    """stg.id (root) fans out to a resolved ``mart`` and an unanalyzable ``blind`` model."""
    stg = _model(
        "stg",
        {
            "id": Column(
                name="id",
                model_name="stg",
                data_type="int",
                lineage=[
                    ColumnLineage(
                        source_columns={"raw.id"},
                        transformation_type="derived",
                        sql_expression=stg_expr,
                    )
                ],
            )
        },
        downstream={"mart", "blind"},
    )
    mart = _model(
        "mart",
        {
            "id": Column(
                name="id",
                model_name="mart",
                data_type="int",
                lineage=[ColumnLineage(source_columns={"stg.id"}, transformation_type="direct")],
            )
        },
        upstream={"stg"},
    )
    blind = _model("blind", {}, upstream={"stg"})
    return InMemoryProvider(
        {"stg": stg, "mart": mart, "blind": blind},
        dialect="snowflake",
        downstream={"stg": {"mart", "blind"}, "mart": set(), "blind": set()},
        compiled={
            "stg": f"select {stg_expr} as id from raw",
            "mart": "select id from stg",
            "blind": "select * from some_cte",
        },
        catalog_backed={"stg", "mart"},
    )


def test_resolution_reconciles_with_confidence_and_selection() -> None:
    # End-to-end through the service: statuses aggregate exactly to the confidence counts and
    # rebuild_forced_by_nonresolution reconstructs from the emitted rebuild set.
    base_provider = _recon_graph("raw.id")
    head_provider = _recon_graph("raw.id * 2")
    changes = ChangesetBuilder(base_provider, head_provider).build()
    report = _service_on(head_provider).get_changeset_impact(
        changes, base_service=_service_on(base_provider)
    )

    confidence = report["confidence"]
    selection = report["selection"]
    resolution = report["resolution"]
    summary = report["resolution_summary"]

    # Total and per-status reconciliation with the confidence partition.
    assert summary["reachable"] == confidence["reachable_models"] == len(resolution)
    assert summary["no_column_info"] + summary["unresolved"] == confidence["no_column_info"]
    assert summary["parse_failed"] == confidence["parse_failed"]
    total = (
        summary["catalog_backed"]
        + summary["parsed"]
        + summary["no_column_info"]
        + summary["parse_failed"]
        + summary["unresolved"]
    )
    assert total == summary["reachable"]

    # Reconciliation with the rebuild set.
    unanalyzable = {
        name
        for name, entry in resolution.items()
        if entry["status"] not in ("catalog_backed", "parsed")
    }
    forced = len(set(selection["rebuild_models"]) & unanalyzable)
    assert summary["rebuild_forced_by_nonresolution"] == forced
    assert summary["rebuild_forced_by_nonresolution"] <= len(selection["rebuild_models"])
