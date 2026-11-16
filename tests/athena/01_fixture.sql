-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- Synthetic CUR with the three scenarios of the design document, plus a control row.
-- Run each statement separately in Athena (engine v3), in a test account.
-- Timestamps carry milliseconds: Athena views cannot store timestamp(0), and CUR 2.0 uses timestamp(3).

CREATE DATABASE IF NOT EXISTS cctb_test;

CREATE OR REPLACE VIEW cctb_test.cur2_fake AS
SELECT * FROM (
  VALUES
  -- Scenario A: EC2 created in June, tagged CostCenter=CC-123 on 15 September
  ('123456789012', 'i-aaa', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-06-10 00:00:00.000', 10.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-06'),
  ('123456789012', 'i-aaa', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-07-10 00:00:00.000', 10.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-07'),
  ('123456789012', 'i-aaa', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-08-10 00:00:00.000', 10.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-08'),
  ('123456789012', 'i-aaa', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-09-05 00:00:00.000', 10.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-09'),
  ('123456789012', 'i-aaa', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-09-20 00:00:00.000', 10.0, MAP(ARRAY['user_cost_center'], ARRAY['CC-123']), '2026-09'),
  ('123456789012', 'i-aaa', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-10-05 00:00:00.000', 10.0, MAP(ARRAY['user_cost_center'], ARRAY['CC-123']), '2026-10'),
  -- Scenario B: S3 bucket created in June, Project=alpha from July, beta from September
  ('123456789012', 'bkt-bbb', 'Usage', 'AmazonS3', 'eu-south-1', TIMESTAMP '2026-06-10 00:00:00.000', 5.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-06'),
  ('123456789012', 'bkt-bbb', 'Usage', 'AmazonS3', 'eu-south-1', TIMESTAMP '2026-07-10 00:00:00.000', 5.0, MAP(ARRAY['user_project'], ARRAY['alpha']), '2026-07'),
  ('123456789012', 'bkt-bbb', 'Usage', 'AmazonS3', 'eu-south-1', TIMESTAMP '2026-08-10 00:00:00.000', 5.0, MAP(ARRAY['user_project'], ARRAY['alpha']), '2026-08'),
  ('123456789012', 'bkt-bbb', 'Usage', 'AmazonS3', 'eu-south-1', TIMESTAMP '2026-09-10 00:00:00.000', 5.0, MAP(ARRAY['user_project'], ARRAY['beta']), '2026-09'),
  ('123456789012', 'bkt-bbb', 'Usage', 'AmazonS3', 'eu-south-1', TIMESTAMP '2026-10-10 00:00:00.000', 5.0, MAP(ARRAY['user_project'], ARRAY['beta']), '2026-10'),
  -- Scenario C: EBS volume deleted in August, never tagged (needs an override)
  ('123456789012', 'vol-ccc', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-06-10 00:00:00.000', 2.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-06'),
  ('123456789012', 'vol-ccc', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-07-10 00:00:00.000', 2.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-07'),
  ('123456789012', 'vol-ccc', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-08-10 00:00:00.000', 2.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-08'),
  -- Control: outside the configured regions, must stay empty in July
  ('123456789012', 'i-ddd', 'Usage', 'AmazonEC2', 'eu-west-1', TIMESTAMP '2026-07-10 00:00:00.000', 1.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-07'),
  ('123456789012', 'i-ddd', 'Usage', 'AmazonEC2', 'eu-west-1', TIMESTAMP '2026-09-10 00:00:00.000', 1.0, MAP(ARRAY['user_cost_center'], ARRAY['CC-999']), '2026-09'),
  -- Override precedence 1: outside the configured regions, never tagged; the override applies anyway
  ('123456789012', 'vol-eee', 'Usage', 'AmazonEC2', 'eu-west-1', TIMESTAMP '2026-07-10 00:00:00.000', 4.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-07'),
  ('123456789012', 'vol-eee', 'Usage', 'AmazonEC2', 'eu-west-1', TIMESTAMP '2026-08-10 00:00:00.000', 4.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-08'),
  -- Override precedence 2: May is before the configured period; the override period (May-June) wins
  ('123456789012', 'i-fff', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-05-10 00:00:00.000', 6.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-05'),
  ('123456789012', 'i-fff', 'Usage', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-06-10 00:00:00.000', 6.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-06'),
  -- Control: a Tax row without resource ID, never touched
  ('123456789012', '', 'Tax', 'AmazonEC2', 'eu-south-1', TIMESTAMP '2026-07-01 00:00:00.000', 3.0, CAST(MAP() AS MAP(VARCHAR, VARCHAR)), '2026-07')
) AS t(line_item_usage_account_id, line_item_resource_id, line_item_line_item_type, line_item_product_code,
       product_region_code, line_item_usage_start_date, line_item_unblended_cost, resource_tags, billing_period);

CREATE OR REPLACE VIEW cctb_test.overrides AS
SELECT * FROM (
  VALUES
  -- applied (scenario C)
  ('123456789012', 'vol-ccc', 'user_cost_center', 'CC-123', '', ''),
  -- applied: resource outside the configured regions
  ('123456789012', 'vol-eee', 'user_cost_center', 'CC-777', '', ''),
  -- applied: period starts before the configured period
  ('123456789012', 'i-fff', 'user_project', 'gamma', '2026-05', '2026-06'),
  -- valid_not_applied: bkt-bbb already has user_project in August (native values win)
  ('123456789012', 'bkt-bbb', 'user_project', 'zzz', '2026-08', '2026-08'),
  -- tag_key_not_in_config
  ('123456789012', 'i-aaa', 'user_unknown', 'x', '', ''),
  -- period_start_after_end
  ('123456789012', 'i-aaa', 'user_project', 'p', '2026-09', '2026-06')
) AS t(account_id, resource_id, tag_key, tag_value, period_start, period_end);
