# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from builder.evaluate import (
    RECONCILE_COLUMNS,
    evaluate,
    override_metrics,
    parse_overrides_report,
    parse_rows,
    parse_unresolved_csv,
    runs_to_delete,
)


def rows(*data):
    return parse_rows(list(RECONCILE_COLUMNS), data)


def test_balanced_run_is_ok():
    res = evaluate(rows(["2026-06", "100", "100", "50.5", "50.5", "40", "30.25"]), 3, 0.01)
    assert res.ok
    assert res.rows_backfilled == 40
    assert res.cost_backfilled == 30.25
    assert res.unresolved_gaps == 3


def test_row_difference_fails():
    res = evaluate(rows(["2026-06", "100", "101", "50", "50", "0", "0"]), 0, 0.01)
    assert not res.ok
    assert "row count" in res.problems[0]


def test_cost_tolerance():
    assert evaluate(rows(["2026-06", "1", "1", "10.000", "10.005", "0", "0"]), 0, 0.01).ok
    assert not evaluate(rows(["2026-06", "1", "1", "10.0", "10.5", "0", "0"]), 0, 0.01).ok


def test_missing_period_on_one_side():
    res = evaluate(rows(["2026-07", None, "5", None, "2.0", "0", "0"]), 0, 0.01)
    assert not res.ok


def test_empty_result_fails():
    assert not evaluate([], 0, 0.01).ok


def test_runs_to_delete_keeps_current_and_previous():
    tables = ["tag_backfill_map_r20261001t040000", "tag_backfill_map_r20261002t040000", "tag_backfill_map_r20261003t040000", "overrides"]
    out = runs_to_delete(tables, "tag_backfill_map_r", 2, "tag_backfill_map_r20261003t040000")
    assert out == ["tag_backfill_map_r20261001t040000"]
    assert runs_to_delete(tables, "tag_backfill_map_r", 1, "tag_backfill_map_r20261003t040000") == [
        "tag_backfill_map_r20261002t040000",
        "tag_backfill_map_r20261001t040000",
    ]


def test_overrides_report_counts_and_metrics():
    counts = parse_overrides_report(
        ["status", "overrides"],
        [["applied", "3"], ["valid_not_applied", "1"], ["tag_key_not_in_config", "2"], ["invalid_period", "1"]],
    )
    assert counts["applied"] == 3
    assert counts["empty_value"] == 0  # every known status is present
    assert override_metrics(counts) == {"OverridesApplied": 3, "OverridesNotApplied": 1, "OverridesInvalid": 3}


def test_overrides_report_empty_and_bad_header():
    assert override_metrics(parse_overrides_report(["status", "overrides"], [])) == {
        "OverridesApplied": 0,
        "OverridesNotApplied": 0,
        "OverridesInvalid": 0,
    }
    import pytest

    with pytest.raises(ValueError):
        parse_overrides_report(["status"], [])


def test_unresolved_csv_totals_beyond_the_listed_rows():
    text = (
        '"account_id","resource_id","tag_key","billing_period","gap_cost","total_gaps","total_gap_cost"\n'
        '"123456789012","vol-a","user_project","2026-06","10.5","25000","1234.5"\n'
        '"123456789012","vol-b","user_project","2026-06","2.0","25000","1234.5"\n'
    )
    assert parse_unresolved_csv(text) == {"listed": 2, "total_gaps": 25000, "total_gap_cost": 1234.5}


def test_unresolved_csv_empty_and_legacy_columns():
    assert parse_unresolved_csv('"account_id","gap_cost"\n') == {"listed": 0, "total_gaps": 0, "total_gap_cost": 0.0}
    legacy = '"account_id","gap_cost"\n"1","2.5"\n"2","1.5"\n'
    assert parse_unresolved_csv(legacy) == {"listed": 2, "total_gaps": 2, "total_gap_cost": 4.0}
