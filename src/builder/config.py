# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Load and validate the backfill configuration (config/backfill.json).

Every value that ends up inside generated SQL is validated here against a strict
pattern, and is additionally escaped by ``sql.lit``. Unknown keys are rejected so
that a typo in the JSON fails loudly instead of silently widening the scope.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ACCOUNT_RE = re.compile(r"^\d{12}$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")
PRODUCT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
# Tag keys as they appear in the CUR 2.0 resource_tags map (e.g. user_cost_center).
# Quotes and backslashes are never allowed.
TAG_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-./@+= ]{1,256}$")
# Resource IDs or LIKE patterns; '*' is accepted as a friendlier alias for '%'.
RESOURCE_PATTERN_RE = re.compile(r"^[A-Za-z0-9:/_\-.%*@+=]{1,1024}$")

LINE_ITEM_TYPE_RE = re.compile(r"^[A-Za-z]{1,64}$")
# Line item types seen in CUR 2.0. Only used for documentation and warnings: new types
# keep appearing (e.g. FlatRateSubscription, DistributorDiscount), so validation is by pattern.
KNOWN_LINE_ITEM_TYPES = frozenset(
    {
        "Usage",
        "DiscountedUsage",
        "SavingsPlanCoveredUsage",
        "FlatRateSubscription",
        "Fee",
        "RIFee",
        "SavingsPlanRecurringFee",
        "SavingsPlanUpfrontFee",
        "SavingsPlanNegation",
        "Tax",
        "Credit",
        "Refund",
        "BundledDiscount",
        "EdpDiscount",
        "PrivateRateDiscount",
        "SppDiscount",
        "DistributorDiscount",
    }
)
DEFAULT_LINE_ITEM_TYPES = ("Usage", "DiscountedUsage", "SavingsPlanCoveredUsage")

# next_seen : first value applied at or after the gap month (fallback: last value before it)
# latest    : the most recent value observed for the resource
# first_seen: the first value ever observed for the resource
# csv_only  : no discovery, only values from overrides/*.csv
STRATEGIES = frozenset({"next_seen", "latest", "first_seen", "csv_only"})

ALLOWED_KEYS = frozenset(
    {
        "period_start",
        "period_end",
        "tag_keys",
        "accounts",
        "regions",
        "product_codes",
        "resource_id_patterns",
        "line_item_types",
        "value_strategy",
        "csv_overrides_native",
        "marker_key",
    }
)


class ConfigError(ValueError):
    """Raised when backfill.json is invalid."""


@dataclass(frozen=True)
class BackfillConfig:
    period_start: str
    period_end: str
    tag_keys: tuple[str, ...]
    accounts: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    product_codes: tuple[str, ...] = ()
    resource_id_patterns: tuple[str, ...] = ()
    line_item_types: tuple[str, ...] = DEFAULT_LINE_ITEM_TYPES
    value_strategy: str = "next_seen"
    csv_overrides_native: bool = False
    marker_key: str = "user_tag_backfill"


def _str_list(raw: dict[str, Any], key: str, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    value = raw.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{key}' must be a list of strings")
    items = tuple(dict.fromkeys(v.strip() for v in value if v.strip()))  # dedupe, keep order
    if pattern is not None:
        bad = [v for v in items if not pattern.fullmatch(v)]
        if bad:
            raise ConfigError(f"'{key}' contains invalid values: {bad}")
    return items


def parse_config(raw: Any) -> BackfillConfig:
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be a JSON object")
    unknown = sorted(set(raw) - ALLOWED_KEYS)
    if unknown:
        raise ConfigError(f"unknown keys: {unknown}")

    start = raw.get("period_start")
    end = raw.get("period_end")
    for name, value in (("period_start", start), ("period_end", end)):
        if not isinstance(value, str) or not PERIOD_RE.fullmatch(value):
            raise ConfigError(f"'{name}' must be a string in the form YYYY-MM")
    if start > end:
        raise ConfigError("'period_start' must not be after 'period_end'")

    tag_keys = _str_list(raw, "tag_keys", TAG_KEY_RE)
    if not tag_keys:
        raise ConfigError("'tag_keys' must contain at least one tag key")

    line_item_types = _str_list(raw, "line_item_types", LINE_ITEM_TYPE_RE) or DEFAULT_LINE_ITEM_TYPES

    strategy = raw.get("value_strategy", "next_seen")
    if strategy not in STRATEGIES:
        raise ConfigError(f"'value_strategy' must be one of {sorted(STRATEGIES)}")

    overrides_native = raw.get("csv_overrides_native", False)
    if not isinstance(overrides_native, bool):
        raise ConfigError("'csv_overrides_native' must be true or false")

    marker = raw.get("marker_key", "user_tag_backfill")
    if not isinstance(marker, str) or not TAG_KEY_RE.fullmatch(marker):
        raise ConfigError("'marker_key' is not a valid tag key")
    if marker in tag_keys:
        raise ConfigError("'marker_key' must not be one of the 'tag_keys'")

    return BackfillConfig(
        period_start=start,
        period_end=end,
        tag_keys=tag_keys,
        accounts=_str_list(raw, "accounts", ACCOUNT_RE),
        regions=_str_list(raw, "regions", REGION_RE),
        product_codes=_str_list(raw, "product_codes", PRODUCT_RE),
        resource_id_patterns=_str_list(raw, "resource_id_patterns", RESOURCE_PATTERN_RE),
        line_item_types=line_item_types,
        value_strategy=strategy,
        csv_overrides_native=overrides_native,
        marker_key=marker,
    )


def load_config(text: str) -> BackfillConfig:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"backfill.json is not valid JSON: {exc}") from exc
    return parse_config(raw)
