# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import pytest

from builder.config import DEFAULT_LINE_ITEM_TYPES, ConfigError, load_config, parse_config


def test_example_is_valid(cfg):
    assert cfg.period_start == "2026-06"
    assert cfg.tag_keys == ("user_cost_center", "user_project")
    assert cfg.value_strategy == "next_seen"
    assert cfg.marker_key == "user_tag_backfill"


def test_defaults():
    cfg = parse_config({"period_start": "2026-01", "period_end": "2026-01", "tag_keys": ["user_team"]})
    assert cfg.line_item_types == DEFAULT_LINE_ITEM_TYPES
    assert cfg.value_strategy == "next_seen"
    assert cfg.csv_overrides_native is False
    assert cfg.accounts == ()


def test_dedupes_and_strips():
    cfg = parse_config(
        {"period_start": "2026-01", "period_end": "2026-02", "tag_keys": [" user_a ", "user_a", "user_b"]}
    )
    assert cfg.tag_keys == ("user_a", "user_b")


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"period_start": "2026-13"}, "period_start"),
        ({"period_start": "2026-10", "period_end": "2026-09"}, "after"),
        ({"tag_keys": []}, "tag_keys"),
        ({"tag_keys": ["user_x'; DROP TABLE t; --"]}, "tag_keys"),
        ({"accounts": ["12345"]}, "accounts"),
        ({"regions": ["Europe"]}, "regions"),
        ({"product_codes": ["Amazon EC2'"]}, "product_codes"),
        ({"resource_id_patterns": ["i-'1"]}, "resource_id_patterns"),
        ({"line_item_types": ["Usage", "Bad Type'"]}, "line_item_types"),
        ({"value_strategy": "random"}, "value_strategy"),
        ({"csv_overrides_native": "yes"}, "csv_overrides_native"),
        ({"marker_key": "user_project"}, "marker_key"),
        ({"tag_key": ["typo"]}, "unknown keys"),
    ],
)
def test_invalid(example_raw, patch, message):
    raw = {**example_raw, **patch}
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_invalid_json():
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config("{not json")


def test_not_an_object():
    with pytest.raises(ConfigError):
        parse_config(["period_start"])


def test_new_line_item_types_are_accepted(example_raw):
    cfg = parse_config({**example_raw, "line_item_types": ["Usage", "FlatRateSubscription"]})
    assert cfg.line_item_types == ("Usage", "FlatRateSubscription")
