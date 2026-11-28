"""Isolated fixtures for unit, domain, storage, REST API, and CLI subprocess tests."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from itrack_core.api import create_app  # noqa: E402
from itrack_core.storage import IssueStore  # noqa: E402

PYTHON_BIN = sys.executable
ITRACK_CLI = REPO_ROOT / "itrack.py"


def make_png_bytes(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (37, 99, 235)) -> bytes:
    """Build a valid, deterministic RGB PNG image using Python stdlib only."""

    def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)

    raw_rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw_rows)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@dataclass
class CLIResult:
    """Result wrapper for deterministic subprocess invocations of `itrack.py`."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"Expected valid JSON on stdout for {self.args} (rc={self.returncode}).\n"
                f"STDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
            ) from exc


class CLIRunner:
    """Subprocess runner targeting `itrack.py` against an isolated data directory."""

    def __init__(self, default_data_dir: Path) -> None:
        self.default_data_dir = default_data_dir

    def run(
        self,
        *args: str,
        stdin_text: str | None = None,
        env_overrides: dict[str, str | None] | None = None,
        use_env_data_dir: bool = True,
        cwd: Path | None = None,
    ) -> CLIResult:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if use_env_data_dir:
            env["ITRACK_DATA_DIR"] = str(self.default_data_dir)
        else:
            env.pop("ITRACK_DATA_DIR", None)

        if env_overrides:
            for key, val in env_overrides.items():
                if val is None:
                    env.pop(key, None)
                else:
                    env[key] = val

        cmd = [PYTHON_BIN, "-B", str(ITRACK_CLI), *[str(a) for a in args]]
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            text=True,
            capture_output=True,
            cwd=str(cwd or REPO_ROOT),
            env=env,
            timeout=30,
        )
        return CLIResult(
            args=[str(a) for a in args],
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def run_ok(self, *args: str, stdin_text: str | None = None, **kwargs: Any) -> CLIResult:
        res = self.run(*args, stdin_text=stdin_text, **kwargs)
        assert res.returncode == 0, (
            f"Expected exit code 0 for {res.args}, got {res.returncode}.\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
        return res

    def run_json(self, *args: str, stdin_text: str | None = None, **kwargs: Any) -> Any:
        full_args = [*args, "--json"] if "--json" not in args else list(args)
        res = self.run_ok(*full_args, stdin_text=stdin_text, **kwargs)
        return res.json()


@pytest.fixture
def png_bytes() -> bytes:
    return make_png_bytes()


@pytest.fixture
def sample_png_file(tmp_path: Path, png_bytes: bytes) -> Path:
    img_path = tmp_path / "architecture_diagram.png"
    img_path.write_bytes(png_bytes)
    return img_path


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    isolated = tmp_path / "itrack_store"
    monkeypatch.setenv("ITRACK_DATA_DIR", str(isolated))
    return isolated


@pytest.fixture
def store(data_dir: Path) -> Iterator[IssueStore]:
    issue_store = IssueStore(data_dir)
    try:
        yield issue_store
    finally:
        issue_store.close()


@pytest.fixture
def api_client(data_dir: Path) -> Iterator[TestClient]:
    app = create_app(data_dir)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def cli_runner(data_dir: Path) -> CLIRunner:
    return CLIRunner(data_dir)
