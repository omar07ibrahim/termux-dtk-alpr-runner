#!/usr/bin/env python3
"""Run the closed, two-pass FFmpeg RGB delivery evidence probe."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from alpr_runner.media_probe import (  # noqa: E402
    MediaProbeError,
    run_reproducible_probe,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify two byte-identical RGB24 deliveries through the pinned "
            "FFmpeg runtime and production process supervisor."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write the normalized, path-free evidence receipt to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.json:
        _write_error("error: select --json for the bounded public receipt\n")
        return 2
    try:
        result = run_reproducible_probe()
    except MediaProbeError as error:
        _write_error(f"error: {error}\n")
        return 1
    except Exception:
        _write_error("error: media probe failed safely\n")
        return 1
    payload = result.canonical_receipt()
    view = memoryview(payload)
    offset = 0
    try:
        while offset < len(view):
            written = os.write(1, view[offset:])
            if written <= 0:
                return 1
            offset += written
    except OSError:
        return 1
    return 0


def _write_error(message: str) -> None:
    try:
        os.write(2, message.encode("ascii", "strict"))
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
