#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Package the Lambda and deploy the stack in the account/region where the CUR and CID live.
#
# Required:
#   AWS_REGION      same region as the CUR bucket and the CID dashboards
#   DEPLOY_BUCKET   existing S3 bucket (same region) where the Lambda package is uploaded
#   CUR_BUCKET      bucket where Data Exports delivers the CUR
# Optional (defaults in brackets):
#   STACK_NAME [cur-cost-tags-backfill]   RESOURCE_PREFIX [cctb]
#   CUR_DATABASE [cid_data_export]   CUR_TABLE [cur2]   CUR_PREFIX [cur2/]   CUR_KMS_KEY_ARN []
#   WORK_DATABASE [cur_backfill]   KMS_KEY_ARN []   ACCESS_LOGS_BUCKET []   PERMISSIONS_BOUNDARY_ARN []
#   LOG_RETENTION_DAYS [90]   NOTIFICATION_EMAIL []   QS_ROLE []   CID_DATASETS []
#   STACK_TAGS []   extra stack tags propagated to every taggable resource, e.g. "Environment=dev Owner=finops"
set -euo pipefail

: "${AWS_REGION:?set AWS_REGION}"
: "${DEPLOY_BUCKET:?set DEPLOY_BUCKET}"
: "${CUR_BUCKET:?set CUR_BUCKET}"
STACK_NAME="${STACK_NAME:-cur-cost-tags-backfill}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-cctb}"
CUR_DATABASE="${CUR_DATABASE:-cid_data_export}"
CUR_TABLE="${CUR_TABLE:-cur2}"
CUR_PREFIX="${CUR_PREFIX:-cur2/}"
WORK_DATABASE="${WORK_DATABASE:-cur_backfill}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-90}"
read -r -a EXTRA_TAGS <<< "${STACK_TAGS:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/build"

echo "== Account / region"
aws sts get-caller-identity --query '[Account,Arn]' --output text
echo "$AWS_REGION"

echo "== Package"
aws cloudformation package \
  --region "$AWS_REGION" \
  --template-file "$ROOT/cloudformation/template.yaml" \
  --s3-bucket "$DEPLOY_BUCKET" \
  --s3-prefix "$STACK_NAME" \
  --output-template-file "$ROOT/build/packaged.yaml"

echo "== Deploy $STACK_NAME"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$ROOT/build/packaged.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --tags "Application=CURCostTagsBackfill" ${EXTRA_TAGS[@]+"${EXTRA_TAGS[@]}"} \
  --parameter-overrides \
    "ResourcePrefix=$RESOURCE_PREFIX" \
    "CurDatabase=$CUR_DATABASE" \
    "CurTable=$CUR_TABLE" \
    "CurBucketName=$CUR_BUCKET" \
    "CurDataPrefix=$CUR_PREFIX" \
    "CurKmsKeyArn=${CUR_KMS_KEY_ARN:-}" \
    "WorkDatabase=$WORK_DATABASE" \
    "KmsKeyArn=${KMS_KEY_ARN:-}" \
    "AccessLogsBucketName=${ACCESS_LOGS_BUCKET:-}" \
    "PermissionsBoundaryArn=${PERMISSIONS_BOUNDARY_ARN:-}" \
    "LogRetentionInDays=$LOG_RETENTION_DAYS" \
    "NotificationEmail=${NOTIFICATION_EMAIL:-}" \
    "QuickSightDataSourceRoleName=${QS_ROLE:-}" \
    "CidDatasetIds=${CID_DATASETS:-}"

echo "== Outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
