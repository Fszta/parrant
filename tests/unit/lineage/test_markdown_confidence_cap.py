"""The markdown renderer caps the unanalyzable name list; the JSON stays complete.

The 100-name cap is a DISPLAY concern only. The renderer shows at most the cap, appends a
``… +N more`` line, sets the display-only ``*_truncated`` flag True — and never mutates the
completeness of the machine lists the JSON surface already emitted.
"""

from typing import Any

from parrant.lineage.display.markdown import render_changeset_markdown


def _report_with_unanalyzable(names: list[str]) -> dict[str, Any]:
    return {
        "summary": {"affected_models": 0, "affected_columns": 0},
        "changeset": {"total_changes": 1, "by_kind": {"logic_changed": 1}},
        "by_change": [{"model": "stg_orders", "column": "amount", "kind": "logic_changed"}],
        "affected_columns": [],
        "affected_exposures": [],
        "confidence": {
            "reachable_models": len(names),
            "resolved_models": 0,
            "unanalyzable_models": len(names),
            "no_column_info": len(names),
            "parse_failed": 0,
            "no_column_info_models": list(names),
            "parse_failed_models": [],
            "no_column_info_truncated": False,
            "parse_failed_truncated": False,
            "level": "partial",
        },
    }


def test_markdown_caps_names_with_more_suffix_and_sets_truncated():
    names = [f"m{i:03d}" for i in range(150)]
    report = _report_with_unanalyzable(names)

    rendered = render_changeset_markdown(report)

    # Exactly the cap of names is shown, the tail is summarised, and the last name is elided.
    shown = [n for n in names if f"`{n}`" in rendered]
    assert len(shown) == 100
    assert "+50 more" in rendered
    assert "`m149`" not in rendered
    # The folded disclosure is present and titled with the true total.
    assert "Models that couldn't be analyzed (150)" in rendered
    # The display-only truncation flag is set on the confidence view.
    assert report["confidence"]["no_column_info_truncated"] is True
    assert report["confidence"]["parse_failed_truncated"] is False
    # Rendering does not corrupt the machine list completeness.
    assert len(report["confidence"]["no_column_info_models"]) == 150


def test_markdown_does_not_truncate_or_flag_a_short_list():
    names = [f"m{i:02d}" for i in range(5)]
    report = _report_with_unanalyzable(names)

    rendered = render_changeset_markdown(report)

    assert all(f"`{n}`" in rendered for n in names)
    assert "more" not in rendered.split("Models that couldn't be analyzed")[1]
    assert report["confidence"]["no_column_info_truncated"] is False
    assert report["confidence"]["parse_failed_truncated"] is False
