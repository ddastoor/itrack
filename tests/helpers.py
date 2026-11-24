"""Shared test fixtures and helpers for itrack unit, CLI, API, concurrency, and Playwright UI-CLI parity tests."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any

import httpx

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
ITRACK_PY = REPO_ROOT / "itrack.py"
ITRACK_SERVER_PY = REPO_ROOT / "itrack-server.py"
CHROME_CANDIDATES = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def load_module_from_path(module_name: str, file_path: Path) -> types.ModuleType:
    """Dynamically load a hyphenated or script Python file without creating __pycache__."""
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(file_path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load module spec from {file_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = prev


def init_git_repo(path: Path) -> Path:
    """Initialize an isolated temporary Git repository with local user identity."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "testuser"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "testuser@example.com"], cwd=str(path), check=True)
    return path


def cleanup_repo_daemon(repo_dir: Path) -> None:
    """Ensure any background itrack-server.py associated with repo_dir is cleanly terminated."""
    daemon_json = repo_dir / "itrack-issues" / ".runtime" / "daemon.json"
    pid: int | None = None
    base_url: str | None = None
    if daemon_json.exists():
        try:
            info = json.loads(daemon_json.read_text(encoding="utf-8"))
            pid = info.get("pid")
            base_url = info.get("base_url")
        except Exception:
            pass

    if base_url:
        try:
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                client.post(f"{base_url}/api/v1/shutdown")
        except Exception:
            pass

    if pid:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                pid = None
                break
            except PermissionError:
                break
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                pass


def run_itrack_cli(
    repo_dir: Path,
    *args: str,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke itrack.py in a subprocess with bytecode generation disabled."""
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-B", str(ITRACK_PY), *args],
        cwd=str(cwd or repo_dir),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def run_itrack_json(
    repo_dir: Path,
    *args: str,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    expect_rc: int | None = 0,
) -> tuple[int, Any, str]:
    """Invoke itrack.py with --json (unless already present) and parse stdout JSON."""
    final_args = list(args)
    if "--json" not in final_args:
        final_args.append("--json")
    proc = run_itrack_cli(repo_dir, *final_args, cwd=cwd, env_extra=env_extra)
    if expect_rc is not None and proc.returncode != expect_rc:
        raise AssertionError(
            f"Expected returncode {expect_rc}, got {proc.returncode} for args={final_args}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    parsed = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, parsed, proc.stderr


def find_chrome_executable() -> str | None:
    """Locate system Chrome/Chromium binary if available."""
    for candidate in CHROME_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class IsolatedRepoTestCase(unittest.TestCase):
    """Base TestCase providing a fresh Git repository and guaranteed daemon cleanup."""

    repo_dir: Path
    _tmp_dir: str

    def setUp(self) -> None:
        super().setUp()
        self._tmp_dir = tempfile.mkdtemp(prefix="itrack_test_")
        self.repo_dir = init_git_repo(Path(self._tmp_dir))

    def tearDown(self) -> None:
        try:
            cleanup_repo_daemon(self.repo_dir)
        finally:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        super().tearDown()

    def cli(self, *args: str, cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return run_itrack_cli(self.repo_dir, *args, cwd=cwd, env_extra=env_extra)

    def cli_json(
        self,
        *args: str,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        expect_rc: int | None = 0,
    ) -> Any:
        _, data, _ = run_itrack_json(self.repo_dir, *args, cwd=cwd, env_extra=env_extra, expect_rc=expect_rc)
        return data
