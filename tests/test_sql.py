# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import dataclasses
import re

import pytest

from builder import sql
from builder.config import parse_config

RUN = "tag_backfill_map_r20261013t101500"
LOC = "s3://amzn-s3-demo-artifacts-bucket/map/tag_backfill_map_r20261013t101500/"


def statements(cfg, names, columns):
    return sql.plan(cfg, names, columns, RUN, LOC)


def test_lit_escapes_quotes():
    assert sql.lit("a'b") == "'a''b'"


@pytest.mark.parametrize("bad", ["a-b", "1abc", "a b", 'a"b', ""])
def test_ident_rejects_bad_names(bad):
    with pytest.raises(ValueError):
        sql.ident(bad)


def test_plan_has_all_statements(cfg, names, columns):
    plan = statements(cfg, names, columns)
    assert list(plan) == ["build_map", "staging_view", "reconcile", "unresolved", "overrides_report", "promote_map", "promote_view"]


def test_row_filter(cfg, names):
    f = sql.row_filter(cfg, names)
    assert "c.\"billing_period\" BETWEEN '2026-06' AND '2026-09'" in f
    assert "c.product_region_code IN ('eu-south-1')" in f
    assert "line_item_usage_account_id IN" not in f  # no accounts configured
    assert "c.line_item_resource_id <> ''" in f


def test_resource_patterns_become_like(example_raw, names):
    cfg = parse_config({**example_raw, "resource_id_patterns": ["i-*", "vol-0abc"]})
    f = sql.row_filter(cfg, names)
    assert "c.line_item_resource_id LIKE 'i-%'" in f
    assert "c.line_item_resource_id LIKE 'vol-0abc'" in f


def test_next_seen_uses_following_then_previous_value(cfg, names, columns):
    build = statements(cfg, names, columns)["build_map"]
    assert "FILTER (WHERE o.billing_period >= g.billing_period)" in build
    assert "FILTER (WHERE o.billing_period <  g.billing_period)" in build
    assert build.startswith(f'CREATE TABLE "cur_backfill"."{RUN}"')
    assert f"external_location = '{LOC}'" in build


@pytest.mark.parametrize("strategy, fragment", [("latest", "max_by(o.tag_value, o.last_ts)"), ("first_seen", "min_by(o.tag_value, o.first_ts)")])
def test_other_strategies(example_raw, names, columns, strategy, fragment):
    cfg = parse_config({**example_raw, "value_strategy": strategy})
    assert fragment in statements(cfg, names, columns)["build_map"]


def test_csv_only_has_no_discovery_join(example_raw, names, columns):
    cfg = parse_config({**example_raw, "value_strategy": "csv_only"})
    build = statements(cfg, names, columns)["build_map"]
    assert "FROM gaps WHERE false" in build
    assert "min_by" not in build


def test_overrides_native_forces_values(example_raw, names, columns):
    cfg = parse_config({**example_raw, "csv_overrides_native": True})
    build = statements(cfg, names, columns)["build_map"]
    assert "true AS is_forced" in build
    assert "element_at(c.resource_tags, s.tag_key)" not in build  # forced: every month, not only gaps
    default = statements(parse_config(example_raw), names, columns)["build_map"]
    assert "false AS is_forced" in default
    assert "AND coalesce(element_at(c.resource_tags, s.tag_key), '') = ''" in default


def _ov_cte(build):
    return build.split("ov AS (", 1)[1].split("ov_final AS (", 1)[0]


def test_overrides_use_their_own_period(cfg, names, columns):
    build = statements(cfg, names, columns)["build_map"]
    assert 'FROM "cur_backfill"."overrides"' in build
    # empty period = config period, but no clamp: an override may extend beyond the config period
    assert "coalesce(nullif(trim(period_start), ''), '2026-06') AS ps" in build
    assert "coalesce(nullif(trim(period_end), ''), '2026-09') AS pe" in build
    assert "greatest(" not in build and "least(" not in build
    ov = _ov_cte(build)
    assert 'c."billing_period" BETWEEN s.ps AND s.pe' in ov
    assert "BETWEEN '2026-06' AND '2026-09'" not in ov


def test_overrides_ignore_config_filters(example_raw, names, columns):
    cfg = parse_config(
        {**example_raw, "accounts": ["123456789012"], "regions": ["eu-south-1"], "product_codes": ["AmazonEC2"], "resource_id_patterns": ["i-*"]}
    )
    ov = _ov_cte(statements(cfg, names, columns)["build_map"])
    assert "line_item_line_item_type IN (" in ov  # line item types still apply
    assert "c.line_item_resource_id <> ''" in ov
    for fragment in ("product_region_code", "line_item_product_code IN", "LIKE 'i-%'", "IN ('123456789012')"):
        assert fragment not in ov


def test_overrides_validation_status(cfg, names, columns):
    build = statements(cfg, names, columns)["build_map"]
    for status in ("missing_account_or_resource", "tag_key_not_in_config", "empty_value", "invalid_period", "period_start_after_end"):
        assert f"'{status}'" in build
    assert "WHERE status = 'valid'" in build
    assert "regexp_like(ps, '^[0-9]{4}-(0[1-9]|1[0-2])$')" in build


def test_overrides_report(cfg, names, columns):
    report = statements(cfg, names, columns)["overrides_report"]
    assert 'FROM "cur_backfill"."overrides"' in report
    assert f'FROM "cur_backfill"."{RUN}"' in report
    assert "WHERE source = 'override'" in report
    assert "m.billing_period BETWEEN o.ps AND o.pe" in report
    assert "'valid_not_applied'" in report and "'applied'" in report


def test_views_keep_every_column_in_order(cfg, names, columns):
    plan = statements(cfg, names, columns)
    for key, view in (("staging_view", "cur2_backfilled_staging"), ("promote_view", "cur2_backfilled")):
        text = plan[key]
        assert text.startswith(f'CREATE OR REPLACE VIEW "cur_backfill"."{view}" AS')
        select = text.split("FROM \"cid_data_export\".\"cur2\" c", 1)[0]
        found = re.findall(r'^\s+c\."([a-z0-9_]+)"', select, flags=re.M)
        expected = [c for c in columns if c != "resource_tags"]
        assert found == expected
        assert select.count('AS "resource_tags"') == 1


def test_staging_reads_run_table_and_final_reads_map_view(cfg, names, columns):
    plan = statements(cfg, names, columns)
    assert f'FROM "cur_backfill"."{RUN}"' in plan["staging_view"]
    assert 'FROM "cur_backfill"."tag_backfill_map"' in plan["promote_view"]
    assert plan["promote_map"].startswith('CREATE OR REPLACE VIEW "cur_backfill"."tag_backfill_map"')


def test_native_values_win_and_marker(cfg, names, columns):
    view = statements(cfg, names, columns)["promote_view"]
    concat = view.split("ELSE map_concat(", 1)[1]
    order = [concat.index("m.tags_derived"), concat.index("m.tags_ov"), concat.index("map_filter(coalesce(c.resource_tags"), concat.index("m.tags_force")]
    assert order == sorted(order), "map_concat order must be: derived, override, native, forced (last wins)"
    assert "MAP(ARRAY['user_tag_backfill'], ARRAY[m.source])" in view


def test_view_scopes_derived_but_not_overrides(cfg, names, columns):
    view = statements(cfg, names, columns)["promote_view"]
    join = view.split(") m\n", 1)[1]
    assert "line_item_line_item_type IN (" in join
    assert "BETWEEN '2026-06' AND '2026-09'" not in join  # config scope is not in the join
    assert "product_region_code" not in join
    # derived values are gated by the config scope per row, overrides are not
    assert "THEN m.tags_derived ELSE" in view
    assert "c.product_region_code IN ('eu-south-1')" in view.split("THEN m.tags_derived", 1)[0]


def test_reconcile_and_unresolved(cfg, names, columns):
    plan = statements(cfg, names, columns)
    assert 'FROM "cur_backfill"."cur2_backfilled_staging"' in plan["reconcile"]
    assert "count_if(element_at(resource_tags, 'user_tag_backfill') IS NOT NULL)" in plan["reconcile"]
    # months filled only by overrides outside the config period are reconciled too
    # (expected SQL text of the generator, not a query that is executed: B608 does not apply)
    assert f'"billing_period" IN (SELECT DISTINCT billing_period FROM "cur_backfill"."{RUN}")' in plan["reconcile"]  # nosec B608
    assert f'LEFT JOIN "cur_backfill"."{RUN}" m' in plan["unresolved"]
    assert "WHERE m.account_id IS NULL" in plan["unresolved"]
    # totals over every gap, computed before ORDER BY / LIMIT
    assert "count(*) OVER () AS total_gaps" in plan["unresolved"]
    assert "sum(g.gap_cost) OVER () AS total_gap_cost" in plan["unresolved"]
    assert plan["unresolved"].rstrip().endswith("LIMIT 10000")


def test_missing_columns_are_rejected(cfg, names, columns):
    with pytest.raises(ValueError, match="missing required columns"):
        statements(cfg, names, [c for c in columns if c != "resource_tags"])


def test_custom_names(cfg, names, columns):
    custom = dataclasses.replace(names, cost_column="line_item_net_unblended_cost")
    assert 'sum("line_item_net_unblended_cost")' in statements(cfg, custom, columns)["reconcile"]


def test_bad_location(cfg, names):
    with pytest.raises(ValueError):
        sql.build_map_ctas(cfg, names, RUN, "s3://amzn-s3-demo-bucket/it's/")


def test_statements_parse_as_trino(cfg, names, columns, example_raw):
    sqlglot = pytest.importorskip("sqlglot")
    variants = [cfg, parse_config({**example_raw, "csv_overrides_native": True, "accounts": ["123456789012"], "resource_id_patterns": ["i-*"]})]
    for variant in variants:
        for key, text in statements(variant, names, columns).items():
            try:
                sqlglot.parse_one(text, read="trino")
            except sqlglot.errors.ParseError as exc:  # pragma: no cover - shows which statement broke
                pytest.fail(f"{key} does not parse: {exc}")


def test_athena_fixture_columns_render(cfg):
    import json
    from pathlib import Path

    fake = json.loads((Path(__file__).parent / "athena" / "fake_columns.json").read_text())
    test_names = sql.Names(cur_database="cctb_test", cur_table="cur2_fake", work_database="cctb_test")
    plan = sql.plan(cfg, test_names, fake, RUN, LOC)
    assert 'FROM "cctb_test"."cur2_fake" c' in plan["staging_view"]
    assert 'FROM "cctb_test"."overrides"' in plan["build_map"]
