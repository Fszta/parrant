"""An unresolved-edge marker degrades confidence and forces a rebuild.

These lock the propagation contract from the marker on ``model.metadata["unresolved_edges"]``
through to the selection decision:

* a reachable marker-carrying model is routed OUT of ``catalog_backed``/``parsed`` into the new
  ``partial_edges`` partition (``_partition_reachable``);
* it drops the change's ``confidence.level`` to ``partial`` (``_impact_confidence``), which widens
  the rebuild to the whole reachable universe — so it is rebuilt, never skipped;
* it is surfaced (``build_resolution``: status ``partial_edges`` + reason; summary ``unresolved`` /
  ``partial_edges`` counts; forced-by-nonresolution);
* **fail-safe only** — a change reaching only clean models stays ``full`` and does NOT widen (no
  over-trigger), and a marker can only add to the build, never shrink it or raise confidence.

The marker carries EMPTY ``source_columns`` on its column here on purpose: the parser drops phantom
tokens, so empty sources must not be read as "clean" — the marker is the signal.
"""

from parrant.lineage.service import build_resolution, build_selection
from parrant.models.schema import Column, ColumnLineage
from tests.unit.test_lineage_provider import InMemoryProvider, _model, _service_on


def _col(name: str, *, sources: set[str] | None = None) -> Column:
    lineage = (
        [ColumnLineage(source_columns=sources, transformation_type="direct")]
        if sources is not None
        else None
    )
    return Column(name=name, model_name="m", data_type="int", lineage=lineage)


def _graph(*, mark_phantom: bool) -> InMemoryProvider:
    """seed → {clean_mart, phantom_mart}. ``phantom_mart`` optionally carries a marker.

    Both marts have columns (so absent a marker they are ``catalog_backed`` = resolved). The
    marker'd column deliberately has EMPTY source_columns — the marker, not the sources, is what
    must keep the model out of ``skippable``.
    """
    seed = _model("seed", {"id": _col("id")}, downstream={"clean_mart", "phantom_mart"})
    clean_mart = _model("clean_mart", {"id": _col("id", sources={"seed.id"})}, upstream={"seed"})
    phantom_meta = (
        {
            "unresolved_edges": [
                {
                    "model": "phantom_mart",
                    "column": "value",
                    "reason": "phantom_alias",
                    "detail": "p.value",
                }
            ]
        }
        if mark_phantom
        else None
    )
    phantom_mart = _model(
        "phantom_mart",
        # column present but its source was dropped as phantom -> empty source_columns
        {"value": _col("value", sources=set())},
        upstream={"seed"},
        metadata=phantom_meta,
    )
    return InMemoryProvider(
        {"seed": seed, "clean_mart": clean_mart, "phantom_mart": phantom_mart},
        dialect="snowflake",
        downstream={
            "seed": {"clean_mart", "phantom_mart"},
            "clean_mart": set(),
            "phantom_mart": set(),
        },
        catalog_backed={"seed", "clean_mart", "phantom_mart"},
    )


def test_reached_marker_model_routes_to_partial_edges() -> None:
    service = _service_on(_graph(mark_phantom=True))
    partition = service._partition_reachable({"clean_mart", "phantom_mart"})

    assert partition.partial_edges == {"phantom_mart"}
    # And it is pulled OUT of the resolved (catalog_backed/parsed) partition.
    assert "phantom_mart" not in partition.catalog_backed
    assert "phantom_mart" not in partition.parsed
    assert "clean_mart" in partition.catalog_backed


def test_reached_marker_degrades_confidence_and_widens_rebuild() -> None:
    service = _service_on(_graph(mark_phantom=True))
    reachable = service._dag_reachable_models("seed")
    assert reachable == {"clean_mart", "phantom_mart"}

    confidence = service._impact_confidence(reachable, resolved_models=1)
    assert confidence["level"] == "partial"
    assert confidence["partial_edges"] == 1
    assert confidence["partial_edges_models"] == ["phantom_mart"]

    # Only an additive reach onto the clean mart — yet the marker widens everything.
    by_change = [
        {
            "kind": "added",
            "semantic": None,
            "resolved": True,
            "reached_models": [{"name": "clean_mart", "mechanism": "direct_passthrough"}],
        }
    ]
    selection = build_selection(reachable, {"seed"}, by_change, confidence)
    assert selection["widened_to_all_reachable"] is True
    assert "phantom_mart" in selection["rebuild_models"]
    assert selection["skippable_models"] == []
    assert "phantom_mart" not in selection["skippable_models"]


def test_marker_surfaced_in_resolution_summary() -> None:
    service = _service_on(_graph(mark_phantom=True))
    provider = service.registry
    reachable = {"clean_mart", "phantom_mart"}
    partition = service._partition_reachable(reachable)
    per_model, summary = build_resolution(provider, partition, {"phantom_mart"})

    assert per_model["phantom_mart"] == {"status": "partial_edges", "reason": "phantom_alias"}
    assert summary["partial_edges"] == 1
    assert summary["unresolved"] >= 1
    # It counts as a rebuild forced by non-resolution (not by a proven reaching change).
    assert summary["rebuild_forced_by_nonresolution"] == 1
    # Reconciliation still holds: cb + parsed + nci + pf + unresolved == reachable.
    total = (
        summary["catalog_backed"]
        + summary["parsed"]
        + summary["no_column_info"]
        + summary["parse_failed"]
        + summary["unresolved"]
    )
    assert total == summary["reachable"] == len(reachable)


def test_fail_safe_clean_reach_stays_full_and_does_not_widen() -> None:
    # The negative: identical graph with NO marker. A change reaching only clean models must stay
    # `full`, NOT widen, and leave the additive reach skippable — the marker is the ONLY trigger,
    # so there is no repo-wide over-widening.
    service = _service_on(_graph(mark_phantom=False))
    reachable = service._dag_reachable_models("seed")

    confidence = service._impact_confidence(reachable, resolved_models=2)
    assert confidence["level"] == "full"
    assert confidence["partial_edges"] == 0
    assert confidence["partial_edges_models"] == []

    by_change = [
        {
            "kind": "added",
            "semantic": None,
            "resolved": True,
            "reached_models": [
                {"name": "clean_mart", "mechanism": "direct_passthrough"},
                {"name": "phantom_mart", "mechanism": "direct_passthrough"},
            ],
        }
    ]
    selection = build_selection(reachable, {"seed"}, by_change, confidence)
    assert selection["widened_to_all_reachable"] is False
    # Additive-only reach at full confidence stays skippable — nothing forced.
    assert set(selection["skippable_models"]) == {"clean_mart", "phantom_mart"}

    _, summary = build_resolution(service.registry, service._partition_reachable(reachable), set())
    assert summary["partial_edges"] == 0
    assert summary["unresolved"] == 0
