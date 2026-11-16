# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure SQL generation for Athena engine v3 (Trino dialect).

No AWS calls here: every function returns a SQL string, so the whole module can
be unit-tested locally. Values coming from the configuration are validated in
``config.py`` and escaped again with ``lit``; identifiers go through ``ident``.

Security note: Athena does not support bind parameters for DDL/CTAS, so statements
are built as strings. Every value is validated against a strict pattern in
``config.py`` (unknown keys rejected) and escaped by ``lit``; every identifier
(database, table, column) is checked by ``ident`` against ``IDENT_RE``. The
``# nosec B608`` markers below refer to this note.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .config import BackfillConfig

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Columns of the CUR 2.0 table that the generated SQL relies on.
REQUIRED_COLUMNS = (
    "line_item_usage_account_id",
    "line_item_resource_id",
    "line_item_line_item_type",
    "line_item_product_code",
    "line_item_usage_start_date",
    "product_region_code",
    "resource_tags",
)

MAP_COLUMNS = ("account_id", "resource_id", "billing_period", "tag_key", "tag_value", "source", "is_forced")
EMPTY_MAP = "CAST(MAP() AS MAP(VARCHAR, VARCHAR))"


def lit(value: str) -> str:
    """SQL string literal with single quotes escaped."""
    return "'" + str(value).replace("'", "''") + "'"


def lit_list(values: Sequence[str]) -> str:
    return ", ".join(lit(v) for v in values)


def ident(name: str) -> str:
    """Double-quoted identifier, after a strict validation."""
    if not IDENT_RE.fullmatch(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return f'"{name}"'


def qualified(database: str, table: str) -> str:
    return f"{ident(database)}.{ident(table)}"


@dataclass(frozen=True)
class Names:
    """Database objects used by the tool."""

    cur_database: str
    cur_table: str
    work_database: str
    period_column: str = "billing_period"
    cost_column: str = "line_item_unblended_cost"
    overrides_table: str = "overrides"
    map_view: str = "tag_backfill_map"
    cur_view: str = "cur2_backfilled"
    staging_view: str = "cur2_backfilled_staging"

    @property
    def cur(self) -> str:
        return qualified(self.cur_database, self.cur_table)

    def work(self, obj: str) -> str:
        return qualified(self.work_database, obj)

    @property
    def period(self) -> str:
        return ident(self.period_column)

    @property
    def cost(self) -> str:
        return ident(self.cost_column)


def check_columns(columns: Sequence[str], names: Names) -> None:
    """Fail early if the CUR table does not have what the SQL needs."""
    present = set(columns)
    missing = [c for c in (*REQUIRED_COLUMNS, names.period_column, names.cost_column) if c not in present]
    if missing:
        raise ValueError(f"the CUR table is missing required columns: {missing}")
    for column in columns:
        ident(column)  # validates every column name used in the view


# ── Filters ──────────────────────────────────────────────────────────────────
def row_filter(cfg: BackfillConfig, names: Names, alias: str = "c") -> str:
    """Predicate selecting the CUR rows the backfill is allowed to touch."""
    p = f"{alias}."
    parts = [
        f"{p}{names.period} BETWEEN {lit(cfg.period_start)} AND {lit(cfg.period_end)}",
        f"{p}line_item_line_item_type IN ({lit_list(cfg.line_item_types)})",
        f"{p}line_item_resource_id <> ''",
    ]
    if cfg.accounts:
        parts.append(f"{p}line_item_usage_account_id IN ({lit_list(cfg.accounts)})")
    if cfg.regions:
        parts.append(f"{p}product_region_code IN ({lit_list(cfg.regions)})")
    if cfg.product_codes:
        parts.append(f"{p}line_item_product_code IN ({lit_list(cfg.product_codes)})")
    if cfg.resource_id_patterns:
        likes = " OR ".join(
            f"{p}line_item_resource_id LIKE {lit(pattern.replace('*', '%'))}" for pattern in cfg.resource_id_patterns
        )
        parts.append(f"({likes})")
    return "\n    AND ".join(parts)


def type_filter(cfg: BackfillConfig, names: Names, alias: str = "c") -> str:
    """Predicate that applies to overrides too: line item types and a non-empty resource ID."""
    p = f"{alias}."
    return f"{p}line_item_line_item_type IN ({lit_list(cfg.line_item_types)})\n    AND {p}line_item_resource_id <> ''"


# ── Common CTEs ──────────────────────────────────────────────────────────────
def gaps_cte(cfg: BackfillConfig, names: Names) -> str:
    """Resource-months (inside the filters) that have rows where a tag key is empty."""
    return f"""gaps AS (
  SELECT c.line_item_usage_account_id AS account_id,
         c.line_item_resource_id      AS resource_id,
         t.tag_key,
         c.{names.period}             AS billing_period,
         sum(c.{names.cost})          AS gap_cost
  FROM {names.cur} c
  CROSS JOIN UNNEST(ARRAY[{lit_list(cfg.tag_keys)}]) AS t(tag_key)
  WHERE {row_filter(cfg, names)}
    AND coalesce(element_at(c.resource_tags, t.tag_key), '') = ''
  GROUP BY 1, 2, 3, 4
)"""  # nosec B608


def obs_cte(cfg: BackfillConfig, names: Names) -> str:
    """Native tag values observed in the whole CUR history, per resource and month."""
    account_filter = (
        f"\n    AND c.line_item_usage_account_id IN ({lit_list(cfg.accounts)})" if cfg.accounts else ""
    )
    return f"""obs AS (
  SELECT c.line_item_usage_account_id AS account_id,
         c.line_item_resource_id      AS resource_id,
         x.tag_key,
         x.tag_value,
         c.{names.period}             AS billing_period,
         min(c.line_item_usage_start_date) AS first_ts,
         max(c.line_item_usage_start_date) AS last_ts
  FROM {names.cur} c
  CROSS JOIN UNNEST(c.resource_tags) AS x(tag_key, tag_value)
  WHERE c.line_item_resource_id <> ''
    AND x.tag_value <> ''
    AND x.tag_key IN ({lit_list(cfg.tag_keys)}){account_filter}
    AND c.line_item_resource_id IN (SELECT DISTINCT resource_id FROM gaps)
  GROUP BY 1, 2, 3, 4, 5
)"""  # nosec B608


def value_expression(strategy: str) -> str:
    if strategy == "next_seen":
        return (
            "coalesce(\n"
            "           min_by(o.tag_value, o.first_ts) FILTER (WHERE o.billing_period >= g.billing_period),\n"
            "           max_by(o.tag_value, o.last_ts)  FILTER (WHERE o.billing_period <  g.billing_period)\n"
            "         )"
        )
    if strategy == "latest":
        return "max_by(o.tag_value, o.last_ts)"
    if strategy == "first_seen":
        return "min_by(o.tag_value, o.first_ts)"
    raise ValueError(f"no value expression for strategy {strategy!r}")


def derived_cte(cfg: BackfillConfig) -> str:
    if cfg.value_strategy == "csv_only":
        return """derived AS (
  SELECT account_id, resource_id, billing_period, tag_key, CAST(NULL AS VARCHAR) AS tag_value
  FROM gaps WHERE false
)"""
    return f"""derived AS (
  SELECT g.account_id, g.resource_id, g.billing_period, g.tag_key,
         {value_expression(cfg.value_strategy)} AS tag_value
  FROM gaps g
  JOIN obs o
    ON  o.account_id  = g.account_id
    AND o.resource_id = g.resource_id
    AND o.tag_key     = g.tag_key
  GROUP BY 1, 2, 3, 4
)"""  # nosec B608


def overrides_ctes(cfg: BackfillConfig, names: Names) -> str:
    """Overrides from CSV, expanded to the resource-months they apply to.

    A valid override wins over the config scope: its own period (empty = config period) may extend
    beyond period_start/period_end, and the account/region/product/resource filters do not apply to
    it. Only ``line_item_types`` and a non-empty resource ID still apply. Rows with an invalid period,
    a key that is not in ``tag_keys`` or an empty value are ignored (see ``overrides_report``).
    """
    src = f"""ov_src AS (
  SELECT account_id, resource_id, tag_key, ps, pe, max(tag_value) AS tag_value
  FROM ({_ov_normalized(cfg, names)})
  WHERE status = 'valid'
  GROUP BY 1, 2, 3, 4, 5
)"""  # nosec B608
    gap_only = "" if cfg.csv_overrides_native else "\n    AND coalesce(element_at(c.resource_tags, s.tag_key), '') = ''"
    ov = f"""ov AS (
  SELECT DISTINCT c.line_item_usage_account_id AS account_id,
         c.line_item_resource_id AS resource_id,
         c.{names.period} AS billing_period,
         s.tag_key, s.tag_value
  FROM {names.cur} c
  JOIN ov_src s
    ON  s.account_id  = c.line_item_usage_account_id
    AND s.resource_id = c.line_item_resource_id
    AND c.{names.period} BETWEEN s.ps AND s.pe
  WHERE {type_filter(cfg, names)}
    AND c.{names.period} BETWEEN (SELECT min(ps) FROM ov_src) AND (SELECT max(pe) FROM ov_src)
    AND c.line_item_resource_id IN (SELECT resource_id FROM ov_src){gap_only}
)"""  # nosec B608
    return f"{src},\n{ov},\n{_ov_final('true' if cfg.csv_overrides_native else 'false')}"


PERIOD_SQL_RE = "^[0-9]{4}-(0[1-9]|1[0-2])$"


def _ov_normalized(cfg: BackfillConfig, names: Names) -> str:
    """Override rows, trimmed, with default periods and a validation status."""
    start, end = lit(cfg.period_start), lit(cfg.period_end)
    pattern = lit(PERIOD_SQL_RE)
    return f"""
    SELECT a AS account_id, r AS resource_id, k AS tag_key, v AS tag_value, ps, pe,
           CASE
             WHEN coalesce(a, '') = '' OR coalesce(r, '') = '' THEN 'missing_account_or_resource'
             WHEN coalesce(k, '') NOT IN ({lit_list(cfg.tag_keys)}) THEN 'tag_key_not_in_config'
             WHEN coalesce(v, '') = '' THEN 'empty_value'
             WHEN NOT regexp_like(ps, {pattern}) OR NOT regexp_like(pe, {pattern}) THEN 'invalid_period'
             WHEN ps > pe THEN 'period_start_after_end'
             ELSE 'valid'
           END AS status
    FROM (
      SELECT trim(account_id) AS a, trim(resource_id) AS r, trim(tag_key) AS k, trim(tag_value) AS v,
             coalesce(nullif(trim(period_start), ''), {start}) AS ps,
             coalesce(nullif(trim(period_end), ''), {end}) AS pe
      FROM {names.work(names.overrides_table)}
    )
  """  # nosec B608


def _ov_final(forced: str) -> str:
    return f"""ov_final AS (
  SELECT account_id, resource_id, billing_period, tag_key,
         max(tag_value) AS tag_value,
         'override' AS source,
         {forced} AS is_forced
  FROM ov
  GROUP BY 1, 2, 3, 4
)"""  # nosec B608


def map_select(cfg: BackfillConfig, names: Names) -> str:
    """Full mapping query: one row per account, resource, month and tag key."""
    return f"""WITH
{gaps_cte(cfg, names)},
{obs_cte(cfg, names)},
{derived_cte(cfg)},
{overrides_ctes(cfg, names)}
SELECT account_id, resource_id, billing_period, tag_key, tag_value, source, is_forced
FROM ov_final
UNION ALL
SELECT d.account_id, d.resource_id, d.billing_period, d.tag_key, d.tag_value,
       'derived' AS source, false AS is_forced
FROM derived d
LEFT JOIN ov_final f
  ON  f.account_id     = d.account_id
  AND f.resource_id    = d.resource_id
  AND f.billing_period = d.billing_period
  AND f.tag_key        = d.tag_key
WHERE d.tag_value IS NOT NULL
  AND f.account_id IS NULL"""  # nosec B608


# ── Statements executed by the state machine ─────────────────────────────────
def build_map_ctas(cfg: BackfillConfig, names: Names, run_table: str, location: str) -> str:
    if not location.startswith("s3://") or "'" in location:
        raise ValueError("invalid S3 location")
    return f"""CREATE TABLE {names.work(run_table)}
WITH (format = 'PARQUET', write_compression = 'SNAPPY', external_location = {lit(location)})
AS
{map_select(cfg, names)}"""  # nosec B608


def map_view(names: Names, run_table: str) -> str:
    cols = ", ".join(MAP_COLUMNS)
    return f"""CREATE OR REPLACE VIEW {names.work(names.map_view)} AS
SELECT {cols}
FROM {names.work(run_table)}"""  # nosec B608


def cur_view(cfg: BackfillConfig, names: Names, columns: Sequence[str], view_name: str, map_source: str) -> str:
    """View with the same columns, in the same order, as the CUR table.

    Only ``resource_tags`` changes. Derived values reach only rows inside the config
    scope; valid overrides reach every row of their resource and period that passes
    ``line_item_types``. Keys are filled only where empty: native values always win,
    unless an override is forced with ``csv_overrides_native``.
    """
    check_columns(columns, names)
    marker = lit(cfg.marker_key)
    # Derived values only reach rows inside the config scope; overrides reach every row that passes
    # type_filter (the join condition). Derived and override rows never share a key (see map_select).
    fill = f"""map_concat(
           CASE WHEN {row_filter(cfg, names)}
                THEN m.tags_derived ELSE {EMPTY_MAP} END,
           m.tags_ov)"""  # nosec B608
    select_cols = []
    for column in columns:
        if column == "resource_tags":
            select_cols.append(
                f"""  CASE
    WHEN m.account_id IS NULL THEN c.resource_tags
    WHEN cardinality(map_filter({fill}, (k, v) -> coalesce(element_at(c.resource_tags, k), '') = '')) = 0
         AND cardinality(m.tags_force) = 0 THEN c.resource_tags
    ELSE map_concat(
           {fill},
           map_filter(coalesce(c.resource_tags, {EMPTY_MAP}), (k, v) -> v IS NOT NULL AND v <> ''),
           m.tags_force,
           MAP(ARRAY[{marker}], ARRAY[m.source])
         )
  END AS "resource_tags\""""  # nosec B608
            )
        else:
            select_cols.append(f"  c.{ident(column)}")
    body = ",\n".join(select_cols)
    return f"""CREATE OR REPLACE VIEW {names.work(view_name)} AS
SELECT
{body}
FROM {names.cur} c
LEFT JOIN (
  SELECT account_id, resource_id, billing_period,
         max(source) AS source,
         coalesce(map_agg(tag_key, tag_value) FILTER (WHERE source = 'derived'), {EMPTY_MAP}) AS tags_derived,
         coalesce(map_agg(tag_key, tag_value) FILTER (WHERE source = 'override' AND NOT is_forced), {EMPTY_MAP}) AS tags_ov,
         coalesce(map_agg(tag_key, tag_value) FILTER (WHERE is_forced), {EMPTY_MAP}) AS tags_force
  FROM {names.work(map_source)}
  GROUP BY 1, 2, 3
) m
  ON  c.line_item_usage_account_id = m.account_id
  AND c.line_item_resource_id      = m.resource_id
  AND c.{names.period}             = m.billing_period
  AND {type_filter(cfg, names)}"""  # nosec B608


def reconcile(cfg: BackfillConfig, names: Names, view_name: str, map_source: str | None = None) -> str:
    """Per-month totals of the CUR table and of the view, plus backfill coverage.

    Covers the config period plus any month filled only by overrides outside it.
    """
    period = f"{names.period} BETWEEN {lit(cfg.period_start)} AND {lit(cfg.period_end)}"
    if map_source:
        period = f"({period} OR {names.period} IN (SELECT DISTINCT billing_period FROM {names.work(map_source)}))"  # nosec B608
    marker = lit(cfg.marker_key)
    return f"""SELECT coalesce(a.p, b.p) AS billing_period,
       coalesce(a.n, 0) AS rows_cur,
       coalesce(b.n, 0) AS rows_view,
       coalesce(a.c, 0) AS cost_cur,
       coalesce(b.c, 0) AS cost_view,
       coalesce(b.nb, 0) AS rows_backfilled,
       coalesce(b.cb, 0) AS cost_backfilled
FROM (
  SELECT {names.period} AS p, count(*) AS n, sum({names.cost}) AS c
  FROM {names.cur}
  WHERE {period}
  GROUP BY 1
) a
FULL OUTER JOIN (
  SELECT {names.period} AS p, count(*) AS n, sum({names.cost}) AS c,
         count_if(element_at(resource_tags, {marker}) IS NOT NULL) AS nb,
         sum(CASE WHEN element_at(resource_tags, {marker}) IS NOT NULL THEN {names.cost} ELSE 0 END) AS cb
  FROM {names.work(view_name)}
  WHERE {period}
  GROUP BY 1
) b ON a.p = b.p
ORDER BY 1"""  # nosec B608


def unresolved(cfg: BackfillConfig, names: Names, map_source: str, limit: int = 10000) -> str:
    """Gaps for which neither discovery nor overrides found a value (scenario C).

    Lists the ``limit`` most expensive gaps; ``total_gaps`` and ``total_gap_cost`` are computed over
    all of them (window functions run before ORDER BY / LIMIT), so the report is complete even when
    the list is truncated.
    """
    return f"""WITH
{gaps_cte(cfg, names)}
SELECT g.account_id, g.resource_id, g.tag_key, g.billing_period, g.gap_cost,
       count(*) OVER () AS total_gaps,
       sum(g.gap_cost) OVER () AS total_gap_cost
FROM gaps g
LEFT JOIN {names.work(map_source)} m
  ON  m.account_id     = g.account_id
  AND m.resource_id    = g.resource_id
  AND m.billing_period = g.billing_period
  AND m.tag_key        = g.tag_key
WHERE m.account_id IS NULL
ORDER BY g.gap_cost DESC
LIMIT {int(limit)}"""  # nosec B608


def overrides_report(cfg: BackfillConfig, names: Names, map_source: str) -> str:
    """Override rows grouped by status.

    ``applied``: valid and present in the mapping for at least one month of its period.
    ``valid_not_applied``: valid but with no effect (resource or period not in the CUR, line item
    types excluded, or the key is already populated and ``csv_overrides_native`` is false).
    Any other status is a validation error and the row is ignored. Identical rows count once.
    """
    return f"""SELECT CASE
         WHEN status <> 'valid' THEN status
         WHEN hits = 0 THEN 'valid_not_applied'
         ELSE 'applied'
       END AS status,
       count(*) AS overrides
FROM (
  SELECT o.account_id, o.resource_id, o.tag_key, o.tag_value, o.ps, o.pe, o.status,
         count(m.account_id) AS hits
  FROM ({_ov_normalized(cfg, names)}) o
  LEFT JOIN (
    SELECT DISTINCT account_id, resource_id, tag_key, billing_period
    FROM {names.work(map_source)}
    WHERE source = 'override'
  ) m
    ON  o.status        = 'valid'
    AND m.account_id    = o.account_id
    AND m.resource_id   = o.resource_id
    AND m.tag_key       = o.tag_key
    AND m.billing_period BETWEEN o.ps AND o.pe
  GROUP BY 1, 2, 3, 4, 5, 6, 7
)
GROUP BY 1
ORDER BY 1"""  # nosec B608


def plan(cfg: BackfillConfig, names: Names, columns: Sequence[str], run_table: str, location: str) -> dict[str, str]:
    """All statements of one run, in execution order.

    The staging view reads the run table directly, so reconciliation happens
    before anything visible to CID changes. Promotion only runs if it passes.
    """
    check_columns(columns, names)
    return {
        "build_map": build_map_ctas(cfg, names, run_table, location),
        "staging_view": cur_view(cfg, names, columns, names.staging_view, run_table),
        "reconcile": reconcile(cfg, names, names.staging_view, run_table),
        "unresolved": unresolved(cfg, names, run_table),
        "overrides_report": overrides_report(cfg, names, run_table),
        "promote_map": map_view(names, run_table),
        "promote_view": cur_view(cfg, names, columns, names.cur_view, names.map_view),
    }
