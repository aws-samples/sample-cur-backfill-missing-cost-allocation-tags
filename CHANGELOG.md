# Changelog

All notable changes to this project are documented in this file. Versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-16

### Added
- CloudFormation stack: Step Functions state machine, Lambda builder, Athena workgroup, Glue database, artifacts bucket,
  EventBridge triggers (schedule, config upload, CUR schema change), CloudWatch alarms and SNS topic.
- Tag reconstruction from the CUR history (`next_seen`, `latest`, `first_seen`) and CSV overrides with their own period.
- Reconciliation gate (rows and costs per month) before promoting the view read by Cloud Intelligence Dashboards.
- Reports: `summary.json` (reconciliation, override status counts) and `unresolved.csv`.
- Enterprise options: `ResourcePrefix` naming, `Application`/`Version` tags plus stack tags, customer managed KMS key,
  S3 server access logging, IAM permissions boundary, configurable log retention, Step Functions error logging.
- `scripts/cleanup.py` to remove the stack and the retained artifacts bucket.
- `scripts/validate_run.py`: post-run validation (totals, native values kept, provenance, scope, independent
  recomputation of `next_seen`, spot check), PASS/FAIL per check.
- Total unresolved gaps and their cost in `summary.json` and in the `UnresolvedGaps` / `UnresolvedGapCost` metrics,
  even when `unresolved.csv` is capped at 10,000 rows.
- Athena end-to-end test with known answers extended to the override precedence rules and to the overrides report.
- Documentation: architecture and state machine diagrams in `docs/` (PNG and SVG, AWS Architecture Icons) and a
  "Design notes" section in the README.
- CloudFormation template in `cloudformation/template.yaml`.
- `requirements.txt` (runtime of the operator scripts: boto3) and `requirements-dev.txt` (includes
  `requirements.txt` plus test and scan tools). The Lambda function has no third-party dependencies.
- `pyproject.toml`: project metadata (dependencies read from `requirements.txt`) and pytest configuration.
- `NOTICE` file.

### Security
- The state machine role (Athena) can write only under `map/` and `athena-results/` of the artifacts bucket;
  `config/` and `overrides/` are no longer writable by the stack.
- Documented the `quicksight:CreateIngestion` wildcard and the Step Functions execution history in `SECURITY.md`.
