# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure evaluation of the reconciliation results (no AWS calls)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

RECONCILE_COLUMNS = (
    "billing_period",
    "rows_cur",
    "rows_view",
    "cost_cur",
    "cost_view",
    "rows_backfilled",
    "cost_backfilled",
)


@dataclass
class PeriodResult:
    billing_period: str
    rows_cur: int
    rows_view: int
    cost_cur: float
    cost_view: float
    rows_backfilled: int
    cost_backfilled: float

    @property
    def row_delta(self) -> int:
        return self.rows_view - self.rows_cur

    @property
    def cost_delta(self) -> float:
        return self.cost_view - self.cost_cur


@dataclass
class Evaluation:
    ok: bool
    row_delta: int
    cost_delta: float
    rows_backfilled: int
    cost_backfilled: float
    unresolved_gaps: int
    periods: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _num(value: str | None, cast):
    if value is None or value == "":
        return cast(0)
    return cast(float(value)) if cast is int else cast(value)


def parse_rows(header: Sequence[str], rows: Iterable[Sequence[str | None]]) -> list[PeriodResult]:
    """Turn Athena GetQueryResults rows (strings) into typed results."""
    index = {name: i for i, name in enumerate(header)}
    missing = [c for c in RECONCILE_COLUMNS if c not in index]
    if missing:
        raise ValueError(f"reconciliation result is missing columns: {missing}")
    out = []
    for row in rows:
        get = lambda c: row[index[c]]  # noqa: E731
        out.append(
            PeriodResult(
                billing_period=get("billing_period") or "",
                rows_cur=_num(get("rows_cur"), int),
                rows_view=_num(get("rows_view"), int),
                cost_cur=_num(get("cost_cur"), float),
                cost_view=_num(get("cost_view"), float),
                rows_backfilled=_num(get("rows_backfilled"), int),
                cost_backfilled=_num(get("cost_backfilled"), float),
            )
        )
    return out


def evaluate(periods: list[PeriodResult], unresolved_gaps: int, cost_tolerance: float) -> Evaluation:
    """The backfill may move cost between tag values, never change the totals."""
    problems = []
    for p in periods:
        if p.row_delta != 0:
            problems.append(f"{p.billing_period}: row count differs by {p.row_delta}")
        if abs(p.cost_delta) > cost_tolerance:
            problems.append(f"{p.billing_period}: cost differs by {p.cost_delta:.6f}")
    if not periods:
        problems.append("reconciliation returned no rows for the configured period")
    return Evaluation(
        ok=not problems,
        row_delta=sum(p.row_delta for p in periods),
        cost_delta=round(sum(p.cost_delta for p in periods), 6),
        rows_backfilled=sum(p.rows_backfilled for p in periods),
        cost_backfilled=round(sum(p.cost_backfilled for p in periods), 6),
        unresolved_gaps=unresolved_gaps,
        periods=[{**asdict(p), "row_delta": p.row_delta, "cost_delta": p.cost_delta} for p in periods],
        problems=problems,
    )


def runs_to_delete(table_names: Iterable[str], prefix: str, keep: int, current: str) -> list[str]:
    """Old run tables to drop: keep the newest ``keep`` (always including ``current``)."""
    runs = sorted((t for t in table_names if t.startswith(prefix)), reverse=True)
    kept = [current] + [t for t in runs if t != current][: max(keep - 1, 0)]
    return [t for t in runs if t not in kept]


OVERRIDE_STATUSES = (
    "applied",
    "valid_not_applied",
    "missing_account_or_resource",
    "tag_key_not_in_config",
    "empty_value",
    "invalid_period",
    "period_start_after_end",
)


def parse_overrides_report(header: Sequence[str], rows: Iterable[Sequence[str | None]]) -> dict[str, int]:
    """Override counts per status; every known status is present (0 when absent)."""
    index = {name: i for i, name in enumerate(header)}
    if "status" not in index or "overrides" not in index:
        raise ValueError("overrides report is missing the status/overrides columns")
    counts = {status: 0 for status in OVERRIDE_STATUSES}
    for row in rows:
        status = row[index["status"]] or ""
        counts[status] = counts.get(status, 0) + _num(row[index["overrides"]], int)
    return counts


def override_metrics(counts: dict[str, int]) -> dict[str, int]:
    """Applied / valid but without effect / invalid (ignored)."""
    applied = counts.get("applied", 0)
    not_applied = counts.get("valid_not_applied", 0)
    invalid = sum(v for k, v in counts.items() if k not in ("applied", "valid_not_applied"))
    return {"OverridesApplied": applied, "OverridesNotApplied": not_applied, "OverridesInvalid": invalid}


def parse_unresolved_csv(text: str) -> dict[str, float | int]:
    """Totals of the unresolved report (Athena CSV with total_gaps / total_gap_cost on every row).

    ``listed`` is the number of rows in the (possibly truncated) list; ``total_gaps`` and
    ``total_gap_cost`` cover every unresolved gap.
    """
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return {"listed": 0, "total_gaps": 0, "total_gap_cost": 0.0}
    first = rows[0]
    total = _num(first.get("total_gaps"), int) if "total_gaps" in first else len(rows)
    cost = _num(first.get("total_gap_cost"), float) if "total_gap_cost" in first else sum(
        _num(r.get("gap_cost"), float) for r in rows
    )
    return {"listed": len(rows), "total_gaps": total, "total_gap_cost": round(cost, 6)}
