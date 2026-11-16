# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import pytest

from builder.checks import build_checks
from builder.config import parse_config


def test_all_checks_present(cfg, names):
    assert list(build_checks(cfg, names)) == ["native_kept", "provenance", "scope", "oracle", "spot"]


def test_checks_read_cur_view_and_mapping(cfg, names):
    q = build_checks(cfg, names)
    assert 'FROM "cid_data_export"."cur2" x' in q["native_kept"]
    assert 'FROM "cur_backfill"."cur2_backfilled" x' in q["native_kept"]
    assert 'FROM "cur_backfill"."tag_backfill_map" m' in q["provenance"]
    assert "WHERE m.source = 'derived'" in q["provenance"]
    assert "element_at(c.resource_tags, 'user_tag_backfill') IS NOT NULL" in q["scope"]


def test_oracle_uses_a_window_function_not_the_builder_aggregates(cfg, names):
    oracle = build_checks(cfg, names)["oracle"]
    assert "row_number() OVER" in oracle
    assert "min_by" not in oracle and "max_by" not in oracle
    assert "FULL OUTER JOIN" in oracle


def test_account_filter_and_custom_columns(example_raw, names):
    import dataclasses

    cfg = parse_config({**example_raw, "accounts": ["123456789012"]})
    custom = dataclasses.replace(names, cost_column="line_item_net_unblended_cost")
    q = build_checks(cfg, custom)
    assert "c.line_item_usage_account_id IN ('123456789012')" in q["provenance"]
    assert 'sum(c."line_item_net_unblended_cost")' in q["oracle"]


def test_checks_parse_as_trino(cfg, names, example_raw):
    sqlglot = pytest.importorskip("sqlglot")
    variants = [cfg, parse_config({**example_raw, "accounts": ["123456789012"], "resource_id_patterns": ["i-*"]})]
    for variant in variants:
        for key, text in build_checks(variant, names).items():
            try:
                sqlglot.parse_one(text, read="trino")
            except sqlglot.errors.ParseError as exc:  # pragma: no cover
                pytest.fail(f"{key} does not parse: {exc}")
