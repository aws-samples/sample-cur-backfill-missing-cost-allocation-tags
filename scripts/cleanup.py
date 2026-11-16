# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Remove everything the stack created, including the retained artifacts bucket.

Dry run by default: it prints what would be deleted. Add --yes to delete.
Before running it, point CID back to the original CUR table (cid-cmd update), otherwise
the CID datasets fail when the view disappears.

Usage:
  python3 scripts/cleanup.py --stack-name cur-cost-tags-backfill --region us-east-1          # dry run
  python3 scripts/cleanup.py --stack-name cur-cost-tags-backfill --region us-east-1 --yes    # delete
Requires boto3 (pip install -r requirements.txt).
"""
from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError


def stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def empty_and_delete_bucket(s3, bucket: str) -> int:
    """Delete every object version and delete marker, then the bucket. Returns the number of versions."""
    deleted = 0
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        keys = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])]
        keys += [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i : i + 1000], "Quiet": True})
        deleted += len(keys)
    s3.delete_bucket(Bucket=bucket)
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    try:
        outputs = stack_outputs(cfn, args.stack_name)
    except ClientError as exc:
        print(f"STOP: cannot read stack {args.stack_name}: {exc}")
        return 1
    bucket = outputs.get("ArtifactsBucket")
    print(f"Stack:            {args.stack_name} ({args.region})")
    print(f"Artifacts bucket: {bucket} (retained by the stack, emptied and deleted by this script)")
    print(f"Glue database:    {outputs.get('BackfilledView', '?').split('.')[0]} (deleted with the stack, tables included)")
    if not args.yes:
        print("Dry run. Re-run with --yes to delete. Point CID back to the original CUR table first.")
        return 0

    cfn.delete_stack(StackName=args.stack_name)
    print("Deleting the stack...")
    cfn.get_waiter("stack_delete_complete").wait(StackName=args.stack_name)
    if bucket:
        count = empty_and_delete_bucket(s3, bucket)
        print(f"Deleted bucket {bucket} ({count} object versions).")
    print("Done. If you added policies by hand to the QuickSight role, remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
