# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda entry point used by the Step Functions state machine.

Actions (event["action"]):
  prepare  - concurrency guard, load config, read the CUR schema, render every SQL statement
  evaluate - read reconciliation, unresolved and overrides results, write reports, publish metrics, return ok
  publish  - after promotion: drop old run tables and (optionally) refresh the CID datasets
  render   - debug: return the SQL that a run would execute, without executing anything
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3

from .config import load_config
from .evaluate import evaluate as evaluate_results
from .evaluate import override_metrics, parse_overrides_report, parse_rows, parse_unresolved_csv, runs_to_delete
from .sql import Names, plan

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

RUN_PREFIX = "tag_backfill_map_r"

_athena = boto3.client("athena")
_glue = boto3.client("glue")
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")
_sfn = boto3.client("stepfunctions")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing environment variable {name}")
    return value


def _names() -> Names:
    return Names(
        cur_database=_env("CUR_DATABASE"),
        cur_table=_env("CUR_TABLE"),
        work_database=_env("WORK_DATABASE"),
        period_column=_env("PERIOD_COLUMN", "billing_period"),
        cost_column=_env("COST_COLUMN", "line_item_unblended_cost"),
    )


def _bucket() -> str:
    return _env("ARTIFACTS_BUCKET")


def _read_config(event: dict[str, Any]):
    if "config" in event and event.get("action") == "render":
        return load_config(json.dumps(event["config"]))
    key = _env("CONFIG_KEY", "config/backfill.json")
    body = _s3.get_object(Bucket=_bucket(), Key=key)["Body"].read().decode("utf-8")
    return load_config(body)


def _cur_columns(names: Names) -> list[str]:
    table = _glue.get_table(DatabaseName=names.cur_database, Name=names.cur_table)["Table"]
    cols = [c["Name"] for c in table.get("StorageDescriptor", {}).get("Columns", [])]
    cols += [c["Name"] for c in table.get("PartitionKeys", [])]
    return cols


def _split_s3(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an S3 URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


# ── prepare ──────────────────────────────────────────────────────────────────
def prepare(event: dict[str, Any]) -> dict[str, Any]:
    sm_arn, exec_arn = event.get("stateMachineArn"), event.get("executionArn")
    if sm_arn and exec_arn:
        running = _sfn.list_executions(stateMachineArn=sm_arn, statusFilter="RUNNING", maxResults=10)
        others = [e["executionArn"] for e in running.get("executions", []) if e["executionArn"] != exec_arn]
        if others:
            LOG.info("another execution is running, skipping: %s", others)
            return {"skip": True, "reason": "another execution is running"}

    cfg = _read_config(event)
    names = _names()
    columns = _cur_columns(names)
    run_table = RUN_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    location = f"s3://{_bucket()}/map/{run_table}/"
    statements = plan(cfg, names, columns, run_table, location)
    LOG.info("prepared run %s for %s..%s", run_table, cfg.period_start, cfg.period_end)
    return {
        "skip": False,
        "runTable": run_table,
        "location": location,
        "config": asdict(cfg),
        "sql": statements,
    }


# ── evaluate ─────────────────────────────────────────────────────────────────
def _query_rows(query_id: str) -> tuple[list[str], list[list[str | None]]]:
    header: list[str] = []
    rows: list[list[str | None]] = []
    for page in _athena.get_paginator("get_query_results").paginate(QueryExecutionId=query_id):
        for row in page["ResultSet"]["Rows"]:
            values = [d.get("VarCharValue") for d in row["Data"]]
            if not header:
                header = [v or "" for v in values]
            else:
                rows.append(values)
    return header, rows


def _copy_unresolved(query_id: str, run_table: str) -> dict[str, float | int]:
    output = _athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["ResultConfiguration"][
        "OutputLocation"
    ]
    src_bucket, src_key = _split_s3(output)
    body = _s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    for key in (f"reports/{run_table}/unresolved.csv", "reports/unresolved-latest.csv"):
        _s3.put_object(Bucket=_bucket(), Key=key, Body=body, ContentType="text/csv")
    return parse_unresolved_csv(body.decode("utf-8"))


def _put_metrics(evaluation, overrides: dict[str, int], unresolved: dict[str, float | int]) -> None:
    dims = [{"Name": "Stack", "Value": _env("STACK_NAME", "cur-cost-tags-backfill")}]
    data = {
        "RowDelta": abs(evaluation.row_delta),
        "CostDelta": abs(evaluation.cost_delta),
        "BackfilledRows": evaluation.rows_backfilled,
        "BackfilledCost": evaluation.cost_backfilled,
        "UnresolvedGaps": evaluation.unresolved_gaps,
        "UnresolvedGapCost": unresolved.get("total_gap_cost", 0),
        "ReconciliationOk": 1 if evaluation.ok else 0,
        **override_metrics(overrides),
    }
    _cw.put_metric_data(
        Namespace=_env("METRIC_NAMESPACE", "CURCostTagsBackfill"),
        MetricData=[{"MetricName": k, "Value": float(v), "Dimensions": dims} for k, v in data.items()],
    )


def evaluate(event: dict[str, Any]) -> dict[str, Any]:
    run_table = event["prepare"]["runTable"]
    header, rows = _query_rows(event["reconcileQueryId"])
    unresolved = _copy_unresolved(event["unresolvedQueryId"], run_table)
    result = evaluate_results(
        parse_rows(header, rows), int(unresolved["total_gaps"]), float(_env("COST_TOLERANCE", "0.01"))
    )
    overrides: dict[str, int] = {}
    if event.get("overridesReportQueryId"):
        overrides = parse_overrides_report(*_query_rows(event["overridesReportQueryId"]))
    summary = {
        "runTable": run_table,
        "evaluatedAt": datetime.now(timezone.utc).isoformat(),
        "config": event["prepare"]["config"],
        **result.to_dict(),
        "unresolved": unresolved,
        "overrides": overrides,
    }
    body = json.dumps(summary, indent=2).encode("utf-8")
    for key in (f"reports/{run_table}/summary.json", "reports/latest-summary.json"):
        _s3.put_object(Bucket=_bucket(), Key=key, Body=body, ContentType="application/json")
    _put_metrics(result, overrides, unresolved)
    LOG.info("evaluation ok=%s problems=%s unresolved=%s overrides=%s", result.ok, result.problems, unresolved, overrides)
    return {"ok": result.ok, "problems": result.problems, "unresolved": unresolved, "overrides": overrides}


# ── publish ──────────────────────────────────────────────────────────────────
def _delete_prefix(prefix: str) -> None:
    bucket = _bucket()
    for page in _s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            _s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})


def _cleanup(current: str) -> list[str]:
    names = _names()
    tables: list[str] = []
    for page in _glue.get_paginator("get_tables").paginate(DatabaseName=names.work_database, Expression=f"{RUN_PREFIX}.*"):
        tables += [t["Name"] for t in page["TableList"]]
    to_delete = runs_to_delete(tables, RUN_PREFIX, int(_env("KEEP_RUNS", "2")), current)
    for table in to_delete:
        _delete_prefix(f"map/{table}/")  # only objects written by this tool
        _glue.delete_table(DatabaseName=names.work_database, Name=table)
    return to_delete


def _refresh_cid(run_table: str) -> dict[str, str]:
    dataset_ids = [d.strip() for d in _env("CID_DATASET_IDS", "").split(",") if d.strip()]
    if not dataset_ids:
        return {}
    qs = boto3.client("quicksight")
    account = _env("TARGET_ACCOUNT_ID")
    status = {}
    for dataset_id in dataset_ids:
        try:
            qs.create_ingestion(AwsAccountId=account, DataSetId=dataset_id, IngestionId=f"{run_table}-{len(status)}")
            status[dataset_id] = "started"
        except Exception as exc:  # a failed refresh must not undo a good promotion
            LOG.warning("ingestion failed for %s: %s", dataset_id, exc)
            status[dataset_id] = f"failed: {exc}"
    return status


def publish(event: dict[str, Any]) -> dict[str, Any]:
    run_table = event["prepare"]["runTable"]
    deleted = _cleanup(run_table)
    refresh = _refresh_cid(run_table)
    return {"deletedRuns": deleted, "cidRefresh": refresh}


# ── entry point ──────────────────────────────────────────────────────────────
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    action = event.get("action")
    if action == "prepare":
        return prepare(event)
    if action == "evaluate":
        return evaluate(event)
    if action == "publish":
        return publish(event)
    if action == "render":
        cfg = _read_config(event)
        names = _names()
        return plan(cfg, names, _cur_columns(names), RUN_PREFIX + "preview", f"s3://{_bucket()}/map/preview/")
    raise ValueError(f"unknown action: {action!r}")
