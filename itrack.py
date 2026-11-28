#!/usr/bin/env python3
"""itrack.py — Thin CLI entrypoint for the unified iTrack issue tracker."""

from __future__ import annotations

import sys

from itrack_core.cli import run_cli


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    sys.exit(main())
