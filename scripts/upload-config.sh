#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Validate backfill.json locally, then upload it (and optional override CSVs).
# Each upload triggers a run of the state machine through EventBridge.
#
# Usage: scripts/upload-config.sh <artifacts-bucket> <backfill.json> [overrides.csv ...]
set -euo pipefail

BUCKET="${1:?artifacts bucket (stack output ArtifactsBucket)}"
CONFIG="${2:?path to backfill.json}"
shift 2

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$ROOT/src" python3 -m builder.validate "$CONFIG"

for csv in "$@"; do
  head -1 "$csv" | grep -qx 'account_id,resource_id,tag_key,tag_value,period_start,period_end' \
    || { echo "STOP: $csv must start with the header account_id,resource_id,tag_key,tag_value,period_start,period_end"; exit 1; }
  aws s3 cp "$csv" "s3://$BUCKET/overrides/$(basename "$csv")"
done

aws s3 cp "$CONFIG" "s3://$BUCKET/config/backfill.json" --content-type application/json
echo "Uploaded. Follow the run in Step Functions; results in s3://$BUCKET/reports/latest-summary.json"
