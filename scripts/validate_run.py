# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validate the last run of the stack: invariants plus an independent recomputation.

  V1 totals        rows and cost per month: view = CUR (from reports/latest-summary.json)
  V2 native kept   no (tag key, native value) loses rows in the view
  V3 provenance    every derived value exists in the native history of the same resource and key
  V4 scope         rows with the marker are inside the config scope (override rows excepted)
  V5 oracle        the mapping equals an independent recomputation of next_seen; also reports how much
                   was rebuilt from a later month (tagged later) vs an earlier month only (tag removed)
  V6 spot check    month-by-month native vs view values for 5 pseudo-random rebuilt resources

Everything is read from the stack (outputs and Lambda environment). Read only on the data: Athena
writes only query results to the artifacts bucket. Typical scan: a few times the CUR columns
resource_tags / resource ID / period / cost.

Usage:
  python3 scripts/validate_run.py --stack-name cur-cost-tags-backfill --region us-east-1 [--profile p]
Requires boto3 (pip install -r requirements.txt). Exit code 1 if a check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from builder.checks import build_checks  # noqa: E402
from builder.config import parse_config  # noqa: E402
from builder.sql import Names  # noqa: E402


def run_query(athena, sql: str, workgroup: str, timeout_s: int = 1800):
    """Run one Athena query and wait for it, polling with a bounded backoff (1 s → 10 s, up to timeout_s)."""
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup)["QueryExecutionId"]
    deadline, delay = time.monotonic() + timeout_s, 1.0
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.monotonic() > deadline:
            athena.stop_query_execution(QueryExecutionId=qid)
            raise RuntimeError(f"query {qid} still {q['Status']['State']} after {timeout_s} s: stopped")
        # Intentional: Athena has no waiter for query completion, so the script polls GetQueryExecution.
        time.sleep(delay)  # nosemgrep
        delay = min(delay * 2, 10.0)
    if q["Status"]["State"] != "SUCCEEDED":
        raise RuntimeError(f"query {qid} {q['Status']['State']}: {q['Status'].get('StateChangeReason')}")
    gb = q.get("Statistics", {}).get("DataScannedInBytes", 0) / 1e9
    header, rows = [], []
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for row in page["ResultSet"]["Rows"]:
            values = [d.get("VarCharValue", "") for d in row["Data"]]
            if not header:
                header = values
            else:
                rows.append(values)
    return header, rows, gb


def first_row(header, rows) -> dict[str, str]:
    return dict(zip(header, rows[0])) if rows else {}


def num(d: dict[str, str], k: str) -> float:
    return float(d.get(k) or 0)


def verdict(name: str, ok: bool, detail: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<15} {detail}")
    return ok


def main() -> int:
    import boto3

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    athena, lam = session.client("athena"), session.client("lambda")
    outputs = {o["OutputKey"]: o["OutputValue"]
               for o in cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0].get("Outputs", [])}
    env = lam.get_function_configuration(FunctionName=outputs["BuilderFunctionName"])["Environment"]["Variables"]
    names = Names(
        cur_database=env["CUR_DATABASE"], cur_table=env["CUR_TABLE"], work_database=env["WORK_DATABASE"],
        period_column=env.get("PERIOD_COLUMN", "billing_period"), cost_column=env.get("COST_COLUMN", "line_item_unblended_cost"),
    )
    workgroup = outputs["AthenaWorkGroup"]
    summary = json.loads(s3.get_object(Bucket=outputs["ArtifactsBucket"], Key="reports/latest-summary.json")["Body"].read())
    cfg = parse_config(summary["config"])
    queries = build_checks(cfg, names)
    results, scanned = [], 0.0

    print(f"Run {summary['runTable']} | {names.cur} -> {names.work(names.cur_view)} | keys {list(cfg.tag_keys)} | "
          f"{cfg.period_start}..{cfg.period_end} | strategy {cfg.value_strategy}\n")

    periods = summary.get("periods", [])
    bad = [p["billing_period"] for p in periods if p["row_delta"] != 0 or abs(p["cost_delta"]) > 0.01]
    results.append(verdict("V1 totals", bool(summary.get("ok")) and not bad, f"{len(periods)} months, differing: {bad or 'none'}"))

    h, r, gb = run_query(athena, queries["native_kept"], workgroup)
    scanned += gb
    d = first_row(h, r)
    results.append(verdict("V2 native kept", num(d, "values_losing_rows") == 0,
                           f"{d.get('native_values_checked')} native values, {d.get('values_losing_rows')} losing rows; "
                           f"native rows {d.get('native_rows')}, found in view {d.get('native_rows_found_in_view')}"))

    h, r, gb = run_query(athena, queries["provenance"], workgroup)
    scanned += gb
    d = first_row(h, r)
    results.append(verdict("V3 provenance", num(d, "values_not_in_resource_history") == 0,
                           f"{d.get('derived_rows')} derived mapping rows, "
                           f"{d.get('values_not_in_resource_history')} with a value the resource never had"))

    h, r, gb = run_query(athena, queries["scope"], workgroup)
    scanned += gb
    d = first_row(h, r)
    outside_ok = num(d, "outside_config_scope") == 0 or num(d, "override_rows") > 0
    results.append(verdict("V4 scope", outside_ok and num(d, "wrong_line_item_type") == 0 and num(d, "without_resource_id") == 0,
                           f"{d.get('marker_rows')} marker rows: outside config scope {d.get('outside_config_scope')} "
                           f"(override rows {d.get('override_rows')}, allowed outside), wrong type {d.get('wrong_line_item_type')}, "
                           f"no resource id {d.get('without_resource_id')}"))

    if cfg.value_strategy == "next_seen":
        h, r, gb = run_query(athena, queries["oracle"], workgroup)
        scanned += gb
        d = first_row(h, r)
        ok = num(d, "different_value") == 0 and num(d, "missing_in_map") == 0 and num(d, "extra_in_map") == 0
        results.append(verdict("V5 oracle", ok,
                               f"expected {d.get('expected')}, same {d.get('same_value')}, different {d.get('different_value')}, "
                               f"missing {d.get('missing_in_map')}, extra {d.get('extra_in_map')}"))
        print(f"        info            value from a LATER month (tagged later): {d.get('gaps_value_from_later_month')} gaps, "
              f"cost {d.get('cost_value_from_later_month')}")
        print(f"        info            value from an EARLIER month only (tag removed or lost later): "
              f"{d.get('gaps_value_from_earlier_only')} gaps, cost {d.get('cost_value_from_earlier_only')}")
    else:
        print(f"  SKIP  V5 oracle       implemented for next_seen (strategy is {cfg.value_strategy})")

    h, r, gb = run_query(athena, queries["spot"], workgroup)
    scanned += gb
    print(f"\n  V6 spot check on {cfg.tag_keys[0]} ('-' = empty; in_view must equal native where native is set)")
    widths = [max(len(x), *(len(row[i]) for row in r)) if r else len(x) for i, x in enumerate(h)]
    print("    " + "  ".join(x.ljust(w) for x, w in zip(h, widths)))
    for row in r:
        print("    " + "  ".join(v.ljust(w) for v, w in zip(row, widths)))

    print(f"\n{sum(results)}/{len(results)} checks passed · Athena scanned {scanned:.2f} GB")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
