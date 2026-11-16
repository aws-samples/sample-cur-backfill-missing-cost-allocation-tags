# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
from pathlib import Path

import pytest

from builder.config import parse_config
from builder.sql import Names

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def columns():
    return json.loads((ROOT / "tests" / "fixtures" / "cur2_columns.json").read_text())


@pytest.fixture
def names():
    return Names(cur_database="cid_data_export", cur_table="cur2", work_database="cur_backfill")


@pytest.fixture
def example_raw():
    return json.loads((ROOT / "config" / "backfill.example.json").read_text())


@pytest.fixture
def cfg(example_raw):
    return parse_config(example_raw)
