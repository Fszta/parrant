"""The confidence block's unanalyzable name lists are COMPLETE in machine output.

A fail-closed consumer force-rebuilds "every model parrant couldn't analyze" from
``no_column_info_models`` / ``parse_failed_models``. If those lists were capped, the
consumer would silently drop models past the cap on exactly the widest-blindness changes.
These tests lock the honesty invariant: in machine output the lists are the full sets and
``len(list) == count`` always, with the display-only ``*_truncated`` flags False.
"""

from parrant.models.schema import Column, Model
from tests.unit.test_lineage_provider import InMemoryProvider, _model, _service_on


def _root_with_blind_downstream(n_blind: int, parse_failed_count: int = 0) -> InMemoryProvider:
    """A root model with a column feeding ``n_blind`` column-less downstream models.

    The first ``parse_failed_count`` blind models are marked parse-failed; the rest carry
    no column information. Every blind model is reachable from the root via the manifest
    downstream map, so all land in the coverage gap.
    """
    root = _model(
        "root",
        {"id": Column(name="id", model_name="root", data_type="int")},
    )
    models: dict[str, Model] = {"root": root}
    blind_names = [f"d{i:03d}" for i in range(n_blind)]
    for name in blind_names:
        models[name] = _model(name, {})

    downstream = {"root": set(blind_names)}
    for name in blind_names:
        downstream[name] = set()

    parse_failed = set(blind_names[:parse_failed_count])
    return InMemoryProvider(
        models,
        downstream=downstream,
        parse_failed=parse_failed,
    )


def test_no_column_info_list_is_complete_and_matches_count():
    provider = _root_with_blind_downstream(150)
    service = _service_on(provider)

    confidence = service.get_column_impact("root", "id")["confidence"]

    assert confidence["level"] == "partial"
    # The complete list is emitted (no cap), and it equals the count exactly.
    assert confidence["no_column_info"] == 150
    assert len(confidence["no_column_info_models"]) == 150
    assert len(confidence["no_column_info_models"]) == confidence["no_column_info"]
    # Sorted and complete: model d101+ is present, not silently dropped.
    assert confidence["no_column_info_models"] == sorted(confidence["no_column_info_models"])
    assert "d149" in confidence["no_column_info_models"]
    # Machine output is never truncated.
    assert confidence["no_column_info_truncated"] is False
    assert confidence["parse_failed_truncated"] is False


def test_parse_failed_list_is_complete_and_matches_count():
    provider = _root_with_blind_downstream(150, parse_failed_count=20)
    service = _service_on(provider)

    confidence = service.get_column_impact("root", "id")["confidence"]

    assert confidence["level"] == "partial"
    assert confidence["parse_failed"] == 20
    assert len(confidence["parse_failed_models"]) == 20
    assert len(confidence["parse_failed_models"]) == confidence["parse_failed"]
    # The remaining 130 are no-column-info; both lists are complete.
    assert confidence["no_column_info"] == 130
    assert len(confidence["no_column_info_models"]) == 130
    # The derived force-build floor reconstructs exactly from the emitted lists.
    unanalyzable = set(confidence["no_column_info_models"]) | set(confidence["parse_failed_models"])
    assert len(unanalyzable) == confidence["unanalyzable_models"] == 150
    assert confidence["no_column_info_truncated"] is False
    assert confidence["parse_failed_truncated"] is False
