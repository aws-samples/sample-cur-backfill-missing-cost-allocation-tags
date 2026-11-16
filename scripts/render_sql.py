# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render the SQL of a run locally, without AWS, to review it or paste it in Athena.

Usage:
  python3 scripts/render_sql.py --config config/backfill.example.json \
      --columns tests/fixtures/cur2_columns.json --out build/sql
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from builder.config import load_config  # noqa: E402
from builder.sql import Names, plan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--columns", required=True, type=Path, help="JSON list of the CUR table columns (incl. partitions)")
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "sql")
    parser.add_argument("--cur-database", default="cid_data_export")
    parser.add_argument("--cur-table", default="cur2")
    parser.add_argument("--work-database", default="cur_backfill")
    parser.add_argument("--location", default="s3://<artifacts-bucket>/map/tag_backfill_map_rpreview/")
    args = parser.parse_args()

    cfg = load_config(args.config.read_text())
    names = Names(cur_database=args.cur_database, cur_table=args.cur_table, work_database=args.work_database)
    columns = json.loads(args.columns.read_text())
    statements = plan(cfg, names, columns, "tag_backfill_map_rpreview", args.location)

    args.out.mkdir(parents=True, exist_ok=True)
    for i, (key, text) in enumerate(statements.items(), start=1):
        path = args.out / f"{i:02d}_{key}.sql"
        path.write_text(text + "\n")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
