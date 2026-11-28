#!/usr/bin/env python3
"""itrack-server.py — Thin HTTP & Web UI server entrypoint for iTrack."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from itrack_core.api import create_app
from itrack_core.models import VERSION
from itrack_core.storage import resolve_data_dir


def build_server_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="itrack-server.py",
        description="iTrack Unified Web UI & REST API Server",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override backing data directory (default: ITRACK_DATA_DIR or ./.itrack_data).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    parser.add_argument("-V", "--version", action="version", version=f"itrack-server {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_server_parser()
    args = parser.parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir)
    app = create_app(data_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
