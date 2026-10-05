"""The policy-free rebuild selection derived purely from the lineage diff.

``build_selection`` answers "which models must CI rebuild?" from the diff alone, fail-closed:
every edited model, every model reached by a non-additive change, and every reachable model
parrant could not analyze lands in ``rebuild_models``; ``skippable_models`` is the reachable
complement and is earned only at full confidence with nothing truncated. These tests lock the
honesty invariants (partition, the breaking contract, the widening branch, the empty-selector
sentinel, and determinism).
"""

from typing import Any

from parrant.lineage.changeset import ChangesetBuilder
from parrant.lineage.service import build_selection
from tests.unit.test_lineage_provider import _service_on, _two_model_graph


def _confidence(
    *,
    level: str = "full",
    no_column_info_models: list[str] | None = None,
    parse_failed_models: list[str] | None = None,
    partial_edges_models: list[str] | None = None,
    no_column_info_truncated: bool = False,
    parse_failed_truncated: bool = False,
) -> dict[str, Any]:
    return {
        "level": level,
        "no_column_info_models": no_column_info_models or [],
        "parse_failed_models": parse_failed_models or [],
        "partial_edges_models": partial_edges_models or [],
        "no_column_info_truncated": no_column_info_truncated,
        "parse_failed_truncated": parse_failed_truncated,
    }


def _change(
    *,
    kind: str,
    semantic: str | None,
    reached: list[str] | None = None,
    resolved: bool = True,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": kind, "semantic": semantic, "resolved": resolved}
    if reached is not None:
        entry["reached_models"] = [
            {"name": name, "mechanism": "direct_passthrough"} for name in reached
        ]
    return entry


def _assert_partition(selection: dict[str, Any], universe: set[str]) -> None:
    """Every model in the universe has exactly one disposition — the headline invariant."""
    rebuild = set(selection["rebuild_models"])
    skippable = set(selection["skippable_models"])
    assert rebuild | skippable == universe
    assert rebuild & skippable == set()
    # Deterministic: emitted lists are sorted and the selector is their space-join.
    assert selection["rebuild_models"] == sorted(selection["rebuild_models"])
    assert selection["skippable_models"] == sorted(selection["skippable_models"])
    if selection["has_rebuild"]:
        assert selection["rebuild_selector"].split() == selection["rebuild_models"]
    else:
        assert selection["rebuild_selector"] == ""
    # The sentinel and the selector agree, always.
    assert selection["has_rebuild"] == (len(selection["rebuild_models"]) > 0)
    assert (selection["rebuild_selector"] == "") == (not selection["has_rebuild"])


def test_additive_only_full_confidence_puts_downstream_in_skippable() -> None:
    changed = {"parent"}
    reachable = {"child"}
    by_change = [_change(kind="added", semantic=None, reached=["child"])]
    selection = build_selection(reachable, changed, by_change, _confidence())

    assert selection["has_rebuild"] is True
    assert selection["widened_to_all_reachable"] is False
    # The edited model always rebuilds; the additive-only reach is skippable.
    assert selection["rebuild_models"] == ["parent"]
    assert selection["skippable_models"] == ["child"]
    _assert_partition(selection, changed | reachable)


def test_meaning_changed_reach_puts_downstream_in_rebuild() -> None:
    changed = {"orders"}
    reachable = {"derived"}
    by_change = [_change(kind="logic_changed", semantic="meaning_changed", reached=["derived"])]
    selection = build_selection(reachable, changed, by_change, _confidence())

    assert selection["rebuild_models"] == ["derived", "orders"]
    assert selection["skippable_models"] == []
    assert selection["widened_to_all_reachable"] is False
    _assert_partition(selection, changed | reachable)


def test_indeterminate_reach_is_always_rebuilt_never_skippable() -> None:
    changed = {"orders"}
    reachable = {"derived", "sibling"}
    by_change = [
        _change(kind="logic_changed", semantic="indeterminate", reached=["derived"]),
        # A purely additive reach onto a different model stays skippable alongside it.
        _change(kind="added", semantic=None, reached=["sibling"]),
    ]
    selection = build_selection(reachable, changed, by_change, _confidence())

    assert "derived" in selection["rebuild_models"]
    assert "derived" not in selection["skippable_models"]
    assert selection["skippable_models"] == ["sibling"]
    _assert_partition(selection, changed | reachable)


def test_partial_confidence_widens_to_all_reachable() -> None:
    changed = {"seed"}
    reachable = {"a", "b", "c"}
    # Even a provably-additive reach cannot be skipped once confidence is partial.
    by_change = [_change(kind="added", semantic=None, reached=["a"])]
    selection = build_selection(reachable, changed, by_change, _confidence(level="partial"))

    assert selection["widened_to_all_reachable"] is True
    assert selection["skippable_models"] == []
    assert selection["rebuild_models"] == sorted(changed | reachable)
    _assert_partition(selection, changed | reachable)


def test_truncated_display_list_widens_even_at_full_level() -> None:
    changed = {"seed"}
    reachable = {"a", "b"}
    by_change = [_change(kind="added", semantic=None, reached=["a"])]
    # Level is "full" but a display list was truncated -> we cannot prove anything safe to skip.
    conf = _confidence(level="full", no_column_info_truncated=True)
    selection = build_selection(reachable, changed, by_change, conf)

    assert selection["widened_to_all_reachable"] is True
    assert selection["skippable_models"] == []
    assert selection["rebuild_models"] == sorted(changed | reachable)
    _assert_partition(selection, changed | reachable)


def test_unanalyzable_reachable_models_are_always_rebuilt() -> None:
    changed = {"seed"}
    reachable = {"blind", "parsed_ok"}
    # "blind" carries no column info; at full confidence it must still be rebuilt (fail-closed),
    # while "parsed_ok" reached only additively stays skippable.
    by_change = [_change(kind="added", semantic=None, reached=["parsed_ok"])]
    conf = _confidence(level="full", no_column_info_models=["blind"])
    selection = build_selection(reachable, changed, by_change, conf)

    assert "blind" in selection["rebuild_models"]
    assert selection["skippable_models"] == ["parsed_ok"]
    _assert_partition(selection, changed | reachable)


def test_partial_edges_model_is_always_rebuilt_even_at_full_level() -> None:
    # A model carrying an unresolved-edge marker is folded into the rebuild set exactly like an
    # unanalyzable model — a marker may only ADD to the build (fail-safe). Pinned at level "full"
    # to isolate the explicit fold-in from the level-driven widen: even reached only additively,
    # the marker model must NOT be skippable.
    changed = {"seed"}
    reachable = {"phantom", "clean"}
    by_change = [_change(kind="added", semantic=None, reached=["phantom", "clean"])]
    conf = _confidence(level="full", partial_edges_models=["phantom"])
    selection = build_selection(reachable, changed, by_change, conf)

    assert "phantom" in selection["rebuild_models"]
    assert "phantom" not in selection["skippable_models"]
    # The genuinely-clean additive reach is still skippable — the marker only adds, never widens
    # here (widening is the level="partial" path, exercised separately).
    assert selection["skippable_models"] == ["clean"]
    _assert_partition(selection, changed | reachable)


def test_partial_edges_reach_widens_when_level_is_partial() -> None:
    # The real propagation path: a reachable marker-carrying model drives confidence to "partial" (done in
    # _impact_confidence); build_selection then widens the whole reachable universe — the safe
    # over-build — so nothing is skipped alongside the phantom-edge model.
    changed = {"seed"}
    reachable = {"phantom", "a", "b"}
    by_change = [_change(kind="added", semantic=None, reached=["a"])]
    conf = _confidence(level="partial", partial_edges_models=["phantom"])
    selection = build_selection(reachable, changed, by_change, conf)

    assert selection["widened_to_all_reachable"] is True
    assert selection["skippable_models"] == []
    assert selection["rebuild_models"] == sorted(changed | reachable)
    _assert_partition(selection, changed | reachable)


def test_no_impact_reports_no_rebuild_and_empty_selector() -> None:
    selection = build_selection(set(), set(), [], _confidence())

    assert selection["has_rebuild"] is False
    assert selection["rebuild_models"] == []
    assert selection["rebuild_selector"] == ""
    assert selection["skippable_models"] == []
    assert selection["confidence_level"] == "full"
    _assert_partition(selection, set())


def test_unresolved_change_still_rebuilds_its_own_model() -> None:
    # An unresolved change contributes no reach, but its edited model is in changed_models and so
    # is always rebuilt — the diff never silently drops a model it could not fan out.
    changed = {"orphan"}
    reachable: set[str] = set()
    by_change = [_change(kind="removed", semantic=None, resolved=False)]
    selection = build_selection(reachable, changed, by_change, _confidence())

    assert selection["rebuild_models"] == ["orphan"]
    assert selection["has_rebuild"] is True
    _assert_partition(selection, changed | reachable)


def test_unresolved_change_widens_the_selection() -> None:
    # A change parrant could not analyze (impact resolution raised) has an UNKNOWN reach: its
    # downstream cone is invisible to the fold, so nothing in the reachable universe may be
    # proven safe to skip. The selection must widen, exactly like non-full confidence.
    changed = {"orphan"}
    reachable = {"downstream_a", "downstream_b"}
    by_change = [
        _change(kind="removed", semantic=None, resolved=False),
        _change(kind="added", semantic=None, reached=["downstream_a"]),
    ]
    selection = build_selection(reachable, changed, by_change, _confidence())

    assert selection["widened_to_all_reachable"] is True
    assert selection["skippable_models"] == []
    assert set(selection["rebuild_models"]) == changed | reachable
    _assert_partition(selection, changed | reachable)


def test_selection_is_wired_into_get_changeset_impact() -> None:
    # End-to-end through the service: a real logic change on mart.id surfaces a selection block
    # with the edited model in the rebuild set.
    base_provider = _two_model_graph("stg.id")
    head_provider = _two_model_graph("stg.id * 2")
    changes = ChangesetBuilder(base_provider, head_provider).build()
    head_service = _service_on(head_provider)
    base_service = _service_on(base_provider)

    aggregated = head_service.get_changeset_impact(changes, base_service=base_service)
    selection = aggregated["selection"]

    assert selection is not None
    assert selection["has_rebuild"] is True
    assert "mart" in selection["rebuild_models"]
    universe = set(selection["rebuild_models"]) | set(selection["skippable_models"])
    _assert_partition(selection, universe)
