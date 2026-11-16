# Security

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS/Amazon Security via the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/).
Please do **not** create a public GitHub issue.

## Security model of the sample

- The CUR table and the CUR bucket are only read: the state machine role has `s3:GetObject` on the
  configured CUR prefix and read-only Glue access to the CUR database.
- The stack writes only to the artifacts bucket it creates (versioned, public access blocked, TLS 1.2+
  enforced, SSE-S3 or SSE-KMS) and to its own Glue database.
- Every value from `backfill.json` that ends up in SQL is validated against a strict pattern and escaped;
  every identifier is validated and quoted (`src/builder/config.py`, `src/builder/sql.py`).
- IAM roles are scoped to the stack's resources. Wildcard resources are used only for
  `cloudwatch:PutMetricData` (restricted by namespace), the CloudWatch Logs delivery actions used by Step Functions
  logging (no resource-level permissions), and `quicksight:CreateIngestion` on the account's datasets when
  `CidDatasetIds` is set (the function refreshes only the listed datasets). Athena can write only under `map/` and
  `athena-results/` of the artifacts bucket; `config/` and `overrides/`, which start a run, are read only for the stack.
- Optional controls, if your organization requires them: customer managed KMS key (`KmsKeyArn`), S3 server access
  logging (`AccessLogsBucketName`), IAM permissions boundary (`PermissionsBoundaryArn`), log retention.
- Step Functions logging excludes execution data, but the execution history in Step Functions keeps the inputs and
  outputs of each state (generated SQL and configuration: account IDs, tag keys, periods) for its retention period.
  Restrict `states:DescribeExecution` and `states:GetExecutionHistory` to the operators who run the backfill.
- Writes to `config/` and `overrides/` start a run. To make "only authorized operators upload configuration" an
  enforced control, add to the artifacts bucket a policy statement that denies `s3:PutObject` on those prefixes to
  every principal except your operator role.

This is sample code, for non-production usage. You should work with your security and legal teams to meet your
organizational security, regulatory and compliance requirements before deployment.
