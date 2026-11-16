-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Run after the rendered statements 01..05. Compare with the table in tests/athena/README.md.
SELECT line_item_resource_id AS resource_id,
       line_item_usage_start_date AS usage_start,
       element_at(resource_tags, 'user_cost_center') AS cost_center,
       element_at(resource_tags, 'user_project') AS project,
       element_at(resource_tags, 'user_tag_backfill') AS marker
FROM cctb_test.cur2_backfilled_staging
ORDER BY 1, 2;
