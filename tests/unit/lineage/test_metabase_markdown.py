"""Unit tests for cross-boundary (Metabase) exposure rendering in the Markdown report (F4).

A dashboard reached PAST dbt's edge must name WHICH column(s) of the board the change hits,
so a reviewer goes straight to the field instead of hunting the whole dashboard. A table-grain
reach (no proven column) must stay honest — tagged ``(table-grain)`` with no invented column.
"""

from parrant.lineage.display.markdown import render_changeset_markdown


def _report_with_exposures(exposures):
    return {
        "changeset": {"total_changes": 1, "by_kind": {"logic_changed": 1}},
        "summary": {
            "affected_models": 1,
            "affected_columns": 1,
            "affected_exposures": len(exposures),
        },
        "by_change": [{"model": "dim_accounts", "column": "balance", "kind": "logic_changed"}],
        "affected_columns": [],
        "affected_exposures": exposures,
    }


def test_column_precise_dashboard_names_the_affected_field():
    md = render_changeset_markdown(
        _report_with_exposures(
            [
                {
                    "name": "metabase.dashboard.55",
                    "type": "dashboard",
                    "url": "https://mb/dashboard/55",
                    "source": "metabase",
                    "precision": "column",
                    "via_cards": [128],
                    "via_columns": [
                        {
                            "model": "dim_accounts",
                            "column": "balance",
                            "card_id": 128,
                            "role": "field",
                        },
                    ],
                    "meta": {},
                }
            ]
        )
    )
    assert "via **Metabase**" in md
    assert "(table-grain)" not in md
    # The exact field is named so the reviewer can go straight to it.
    assert "affects `dim_accounts.balance`" in md


def test_table_grain_dashboard_names_no_column():
    md = render_changeset_markdown(
        _report_with_exposures(
            [
                {
                    "name": "metabase.dashboard.88",
                    "type": "dashboard",
                    "source": "metabase",
                    "precision": "table",
                    "via_cards": [900],
                    "via_columns": [],
                    "meta": {},
                }
            ]
        )
    )
    assert "via **Metabase** (table-grain)" in md
    # Honest degradation: no fabricated column.
    assert "affects" not in md


def test_multiple_columns_are_deduped_and_capped():
    via = [
        {"model": "dim_accounts", "column": f"c{i}", "card_id": 128, "role": "field"}
        for i in range(6)
    ]
    # A duplicate (same model.column via another card) must collapse to one entry.
    via.append({"model": "dim_accounts", "column": "c0", "card_id": 129, "role": "filter"})
    md = render_changeset_markdown(
        _report_with_exposures(
            [
                {
                    "name": "metabase.dashboard.55",
                    "type": "dashboard",
                    "source": "metabase",
                    "precision": "column",
                    "via_cards": [128, 129],
                    "via_columns": via,
                    "meta": {},
                }
            ]
        )
    )
    # 6 distinct columns, capped at 4 inline + "+2 more".
    assert "+2 more" in md
    assert "`dim_accounts.c0`" in md
