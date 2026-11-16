# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CLI: validate a backfill.json without AWS.  python3 -m builder.validate path/to/backfill.json"""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from .config import ConfigError, load_config


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -m builder.validate <backfill.json>", file=sys.stderr)
        return 2
    try:
        cfg = load_config(Path(argv[1]).read_text())
    except (ConfigError, OSError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print("VALID")
    for key, value in asdict(cfg).items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
