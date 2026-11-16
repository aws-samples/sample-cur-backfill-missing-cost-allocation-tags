# Athena test with synthetic data

End-to-end test with known answers: a synthetic CUR with the three scenarios of the design document, the
override precedence rules and a few control rows. Use a test account.

1. Run the statements in `01_fixture.sql`, one at a time.
2. Generate the SQL over the synthetic CUR:
   ```bash
   python3 scripts/render_sql.py --config config/backfill.example.json \
     --columns tests/athena/fake_columns.json \
     --cur-database cctb_test --cur-table cur2_fake --work-database cctb_test \
     --location s3://<test-bucket>/cctb/map/run1/ --out build/sql-test
   ```
   In the generated `01_build_map.sql`, the table name is `tag_backfill_map_rpreview`.
3. Run in Athena, in order: `01_build_map.sql`, `02_staging_view.sql`, `03_reconcile.sql`, `04_unresolved.sql`,
   `05_overrides_report.sql`.
4. Run `02_expected.sql` and compare with the tables below.

The example config covers 2026-06 → 2026-09, region `eu-south-1`, keys `user_cost_center` and `user_project`.

## Expected result (`02_expected.sql`)

| resource_id | usage_start | cost_center | project | marker |
|---|---|---|---|---|
| (empty, Tax) | 2026-07-01 | — | — | — |
| bkt-bbb | 2026-06-10 | — | alpha | derived |
| bkt-bbb | 2026-07-10 | — | alpha | — |
| bkt-bbb | 2026-08-10 | — | alpha | — |
| bkt-bbb | 2026-09-10 | — | beta | — |
| bkt-bbb | 2026-10-10 | — | beta | — |
| i-aaa | 2026-06-10 | CC-123 | — | derived |
| i-aaa | 2026-07-10 | CC-123 | — | derived |
| i-aaa | 2026-08-10 | CC-123 | — | derived |
| i-aaa | 2026-09-05 | CC-123 | — | derived |
| i-aaa | 2026-09-20 | CC-123 | — | — |
| i-aaa | 2026-10-05 | CC-123 | — | — |
| i-ddd | 2026-07-10 | — | — | — |
| i-ddd | 2026-09-10 | CC-999 | — | — |
| i-fff | 2026-05-10 | — | gamma | override |
| i-fff | 2026-06-10 | — | gamma | override |
| vol-ccc | 2026-06-10 | CC-123 | — | override |
| vol-ccc | 2026-07-10 | CC-123 | — | override |
| vol-ccc | 2026-08-10 | CC-123 | — | override |
| vol-eee | 2026-07-10 | CC-777 | — | override |
| vol-eee | 2026-08-10 | CC-777 | — | override |

Notes:
- `i-aaa` also fills the 5 September row (the same month the tag was applied): scenario A.
- `bkt-bbb` takes `alpha` in June, not `beta`: `next_seen` strategy, scenario B. The override `zzz` for
  August has no effect, because August already has a native value.
- `vol-ccc` is filled only thanks to the override: scenario C.
- `i-ddd` stays empty in July because it is in `eu-west-1`, outside the filters, and has no override.
- `vol-eee` is in `eu-west-1` too, but its override applies: config filters do not limit overrides.
- `i-fff` is filled in May, before the configured period: the override period wins.
- The Tax row has no resource ID and is never touched.

## Expected reports

- `03_reconcile.sql`: one row per month from 2026-05 to 2026-09 (May is included because an override filled
  it), with equal rows and costs between the CUR and the view.
- `04_unresolved.sql`: 12 rows, each with `total_gaps` = 12. These are the keys a resource never had:
  `user_project` for `i-aaa` from June to September and for `vol-ccc` from June to August, `user_cost_center`
  for `bkt-bbb` from June to September and for `i-fff` in June. This is not an error: the list is there to
  decide which overrides to add.
- `05_overrides_report.sql`:

  | status | overrides |
  |---|---|
  | applied | 3 |
  | period_start_after_end | 1 |
  | tag_key_not_in_config | 1 |
  | valid_not_applied | 1 |

Cleanup: `DROP TABLE cctb_test.tag_backfill_map_rpreview`, then delete the S3 files under the `--location` used
and the `cctb_test` database.
