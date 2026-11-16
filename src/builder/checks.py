# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SQL of the post-run validation (scripts/validate_run.py). Pure: no AWS calls.

The checks do not reuse the statements of the run: V2-V4 test invariants directly on the CUR and on
the published view, and V5 recomputes the ``next_seen`` choice with a window function instead of the
``min_by`` / ``max_by`` aggregates used by the mapping. Same security model as ``sql.py`` (validated
values escaped by ``lit``, identifiers checked by ``ident``), hence the ``# nosec B608`` markers.
"""
from __future__ import annotations

from .config import BackfillConfig
from .sql import Names, lit, lit_list, row_filter


def build_checks(cfg: BackfillConfig, names: Names) -> dict[str, str]:
    cur, view, mapping = names.cur, names.work(names.cur_view), names.work(names.map_view)
    per, cost = names.period, names.cost
    period = f"{per} BETWEEN {lit(cfg.period_start)} AND {lit(cfg.period_end)}"
    keys = lit_list(cfg.tag_keys)
    marker = lit(cfg.marker_key)
    types = lit_list(cfg.line_item_types)
    account_filter = f" AND c.line_item_usage_account_id IN ({lit_list(cfg.accounts)})" if cfg.accounts else ""
    key0 = lit(cfg.tag_keys[0])

    native_kept = f"""
WITH c AS (
  SELECT t.k, element_at(x.resource_tags, t.k) AS v, count(*) AS n
  FROM {cur} x CROSS JOIN UNNEST(ARRAY[{keys}]) AS t(k)
  WHERE x.{period} AND coalesce(element_at(x.resource_tags, t.k), '') <> ''
  GROUP BY 1, 2
), w AS (
  SELECT t.k, element_at(x.resource_tags, t.k) AS v, count(*) AS n
  FROM {view} x CROSS JOIN UNNEST(ARRAY[{keys}]) AS t(k)
  WHERE x.{period} AND coalesce(element_at(x.resource_tags, t.k), '') <> ''
  GROUP BY 1, 2
)
SELECT count(*) AS native_values_checked,
       count_if(coalesce(w.n, 0) < c.n) AS values_losing_rows,
       sum(c.n) AS native_rows,
       sum(least(coalesce(w.n, 0), c.n)) AS native_rows_found_in_view
FROM c LEFT JOIN w ON w.k = c.k AND w.v = c.v"""  # nosec B608

    provenance = f"""
WITH nat AS (
  SELECT DISTINCT c.line_item_usage_account_id AS a, c.line_item_resource_id AS r, x.k, x.v
  FROM {cur} c CROSS JOIN UNNEST(c.resource_tags) AS x(k, v)
  WHERE c.line_item_resource_id <> '' AND x.v <> '' AND x.k IN ({keys}){account_filter}
)
SELECT count(*) AS derived_rows,
       count_if(n.a IS NULL) AS values_not_in_resource_history
FROM {mapping} m
LEFT JOIN nat n ON n.a = m.account_id AND n.r = m.resource_id AND n.k = m.tag_key AND n.v = m.tag_value
WHERE m.source = 'derived'"""  # nosec B608

    scope = f"""
SELECT count(*) AS marker_rows,
       count_if(NOT ({row_filter(cfg, names)})) AS outside_config_scope,
       count_if(c.line_item_line_item_type NOT IN ({types})) AS wrong_line_item_type,
       count_if(coalesce(c.line_item_resource_id, '') = '') AS without_resource_id,
       count_if(element_at(c.resource_tags, {marker}) = 'override') AS override_rows
FROM {view} c
WHERE element_at(c.resource_tags, {marker}) IS NOT NULL"""  # nosec B608

    oracle = f"""
WITH g AS (
  SELECT c.line_item_usage_account_id AS a, c.line_item_resource_id AS r, t.k, c.{per} AS p,
         sum(c.{cost}) AS cost
  FROM {cur} c CROSS JOIN UNNEST(ARRAY[{keys}]) AS t(k)
  WHERE {row_filter(cfg, names)}
    AND coalesce(element_at(c.resource_tags, t.k), '') = ''
  GROUP BY 1, 2, 3, 4
), o AS (
  SELECT c.line_item_usage_account_id AS a, c.line_item_resource_id AS r, x.k, x.v, c.{per} AS p,
         min(c.line_item_usage_start_date) AS f, max(c.line_item_usage_start_date) AS l
  FROM {cur} c CROSS JOIN UNNEST(c.resource_tags) AS x(k, v)
  WHERE c.line_item_resource_id <> '' AND x.v <> '' AND x.k IN ({keys}){account_filter}
    AND c.line_item_resource_id IN (SELECT r FROM g)
  GROUP BY 1, 2, 3, 4, 5
), cand AS (
  SELECT g.a, g.r, g.k, g.p, g.cost, o.v, o.p >= g.p AS later,
         row_number() OVER (
           PARTITION BY g.a, g.r, g.k, g.p
           ORDER BY CASE WHEN o.p >= g.p THEN 0 ELSE 1 END,
                    CASE WHEN o.p >= g.p THEN to_unixtime(o.f) ELSE -to_unixtime(o.l) END,
                    o.v) AS rn
  FROM g JOIN o ON o.a = g.a AND o.r = g.r AND o.k = g.k
), e AS (
  SELECT a, r, k, p, cost, v, later FROM cand WHERE rn = 1
), m AS (
  SELECT account_id AS a, resource_id AS r, tag_key AS k, billing_period AS p, tag_value AS v
  FROM {mapping} WHERE source = 'derived'
)
SELECT count_if(e.a IS NOT NULL) AS expected,
       count_if(e.a IS NOT NULL AND m.a IS NOT NULL AND e.v = m.v) AS same_value,
       count_if(e.a IS NOT NULL AND m.a IS NOT NULL AND e.v <> m.v) AS different_value,
       count_if(e.a IS NOT NULL AND m.a IS NULL) AS missing_in_map,
       count_if(e.a IS NULL) AS extra_in_map,
       count_if(e.later) AS gaps_value_from_later_month,
       round(sum(CASE WHEN e.later THEN e.cost ELSE 0 END), 2) AS cost_value_from_later_month,
       count_if(NOT e.later) AS gaps_value_from_earlier_only,
       round(sum(CASE WHEN NOT e.later THEN e.cost ELSE 0 END), 2) AS cost_value_from_earlier_only
FROM e FULL OUTER JOIN m ON m.a = e.a AND m.r = e.r AND m.k = e.k AND m.p = e.p"""  # nosec B608

    spot = f"""
WITH s AS (
  SELECT r FROM (
    SELECT DISTINCT line_item_resource_id AS r FROM {view}
    WHERE {period} AND element_at(resource_tags, {marker}) IS NOT NULL
  ) ORDER BY xxhash64(to_utf8(r)) LIMIT 5
), n AS (
  SELECT line_item_resource_id AS r, {per} AS p,
         array_join(array_sort(array_distinct(array_agg(coalesce(nullif(element_at(resource_tags, {key0}), ''), '-')))), '|') AS native
  FROM {cur} WHERE line_item_resource_id IN (SELECT r FROM s) AND line_item_line_item_type IN ({types})
  GROUP BY 1, 2
), w AS (
  SELECT line_item_resource_id AS r, {per} AS p,
         array_join(array_sort(array_distinct(array_agg(coalesce(nullif(element_at(resource_tags, {key0}), ''), '-')))), '|') AS in_view,
         count_if(element_at(resource_tags, {marker}) IS NOT NULL) AS rows_marked,
         count(*) AS rows_total
  FROM {view} WHERE line_item_resource_id IN (SELECT r FROM s) AND line_item_line_item_type IN ({types})
  GROUP BY 1, 2
)
SELECT substr(n.r, 1, 32) AS resource, n.p AS month, n.native, w.in_view, w.rows_marked, w.rows_total
FROM n JOIN w ON w.r = n.r AND w.p = n.p
ORDER BY 1, 2"""  # nosec B608
    return {"native_kept": native_kept, "provenance": provenance, "scope": scope, "oracle": oracle, "spot": spot}
