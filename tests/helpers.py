"""Shared test helpers, workspace/daemon managers, synthetic file generators, and surface introspectors for iTrack."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import importlib.util
import inspect
import json
import math
import os
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# ---------------------------------------------------------------------------
# Repository Footprint & Canonical Contract Constants
# ---------------------------------------------------------------------------

TESTS_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = TESTS_DIR.parent
ITRACK_PY: Path = REPO_ROOT / "itrack.py"
ITRACK_SERVER_PY: Path = REPO_ROOT / "itrack-server.py"
ITRACK_REQUIREMENTS: Path = REPO_ROOT / "itrack-requirements.txt"

EXPECTED_ROOT_ENTRIES: set[str] = {
    "itrack.py",
    "itrack-server.py",
    "itrack-requirements.txt",
    "tests",
}

CATEGORIES: list[str] = [
    "epic",
    "story",
    "task",
    "subtask",
    "bug",
    "feature",
    "improvement",
    "refactor",
    "spike",
    "incident",
    "security",
    "chore",
    "docs",
]

STATES: list[str] = [
    "triage",
    "backlog",
    "todo",
    "ready",
    "in-progress",
    "in-review",
    "in-qa",
    "on-hold",
    "blocked",
    "done",
    "cancelled",
    "archived",
]

PRIORITIES: list[str] = [
    "critical",
    "high",
    "medium",
    "low",
    "trivial",
]

PRIORITY_RANKS: dict[str, int] = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "trivial": 1,
}

SEVERITIES: list[str] = [
    "sev1",
    "sev2",
    "sev3",
    "sev4",
    "sev5",
]

SEVERITY_RANKS: dict[str, int] = {
    "sev1": 5,
    "sev2": 4,
    "sev3": 3,
    "sev4": 2,
    "sev5": 1,
}

RESOLUTIONS: list[str] = [
    "fixed",
    "wont-fix",
    "duplicate",
    "cannot-reproduce",
    "by-design",
    "obsolete",
]

LINK_TYPES: list[str] = [
    "blocks",
    "blocked-by",
    "relates-to",
    "duplicates",
    "parent-of",
    "clones",
]

PRESET_TABS: list[str] = [
    "active",
    "open",
    "in-progress",
    "blocked",
    "completed",
    "cancelled",
    "archived",
    "all",
]

SWIMLANES: list[str] = [
    "none",
    "category",
    "priority",
    "assignee",
    "sprint",
]

UI_VIEWS: list[str] = [
    "issues",
    "board",
    "sprints",
    "milestones",
    "views",
    "analytics",
    "tags",
    "settings",
]

UI_MODALS: list[str] = [
    "create-issue",
    "issue-detail",
    "delete-confirm",
    "link-issue",
    "log-work",
    "sprint-rollover",
    "image-lightbox",
    "attachment-preview",
    "link-attachment",
    "save-view",
]

IQL_OPERATORS: list[str] = [
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "~",
    "!~",
    "IN",
    "NOT IN",
    "IS EMPTY",
    "IS NOT EMPTY",
    "AND",
    "OR",
    "NOT",
    "ORDER BY",
]

EXIT_CODES: dict[int, str] = {
    0: "Success",
    1: "Validation / Usage / Invalid value error",
    2: "Git-root pre-flight failure",
    3: "Workspace not initialized",
    4: "Resource not found",
    5: "Conflict / Referential / Collision error",
}

FORBIDDEN_ARTIFACT_NAMES: set[str] = {
    "__pycache__",
    ".pytest_cache",
    ".coverage",
    "htmlcov",
    "test-results",
    "itrack-issues",
    "requirements.txt",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    ".coveragerc",
}


# ---------------------------------------------------------------------------
# Clean-Root Enforcement
# ---------------------------------------------------------------------------


def assert_clean_repo_root(repo_root: Path = REPO_ROOT) -> None:
    """Assert Law 1 Clean-Root compliance at repo_root and inside tests/."""
    root = Path(repo_root)
    actual = set(os.listdir(root)) - {".git"}
    if actual != EXPECTED_ROOT_ENTRIES:
        unexpected = sorted(actual - EXPECTED_ROOT_ENTRIES)
        missing = sorted(EXPECTED_ROOT_ENTRIES - actual)
        raise AssertionError(
            f"Clean-Root violation in {root}! "
            f"Expected strictly {sorted(EXPECTED_ROOT_ENTRIES)}; "
            f"unexpected={unexpected}, missing={missing}"
        )

    bytecode_disabled = os.environ.get("PYTHONDONTWRITEBYTECODE") == "1" or sys.dont_write_bytecode
    polluted: list[str] = []
    tests_dir = root / "tests"
    if tests_dir.exists():
        for dirpath, dirnames, filenames in os.walk(tests_dir):
            rel_dir = Path(dirpath).relative_to(root)
            for d in list(dirnames):
                if d in FORBIDDEN_ARTIFACT_NAMES or (bytecode_disabled and d == "__pycache__"):
                    polluted.append(str(rel_dir / d))
            for fn in filenames:
                if (
                    fn in FORBIDDEN_ARTIFACT_NAMES
                    or fn.startswith(".coverage")
                    or (bytecode_disabled and fn.endswith((".pyc", ".pyo")))
                ):
                    polluted.append(str(rel_dir / fn))

    if polluted:
        raise AssertionError(
            f"Clean-Root cache/artifact violation inside {root}: {sorted(polluted)}"
        )


# ---------------------------------------------------------------------------
# Git Repository, Worktree & Daemon Lifecycle Helpers
# ---------------------------------------------------------------------------


def init_git_repo(path: Path | str) -> Path:
    """Initialize an isolated Git repository (supporting spaces & Unicode in path)."""
    repo = Path(path)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@itrack.local"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "iTrack Test Harness"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def create_git_worktree(
    base_repo: Path | str,
    worktree_path: Path | str,
    branch: str | None = None,
) -> Path:
    """Create a real Git worktree (where .git in worktree_path is a file, not a directory)."""
    base = Path(base_repo)
    wt = Path(worktree_path)
    wt.parent.mkdir(parents=True, exist_ok=True)

    # Ensure base_repo has at least one commit so `git worktree add` succeeds cleanly.
    head_check = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(base),
        capture_output=True,
        text=True,
    )
    if head_check.returncode != 0:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-q", "-m", "Initial empty commit"],
            cwd=str(base),
            check=True,
            capture_output=True,
            text=True,
        )

    cmd = ["git", "worktree", "add", "-q"]
    if branch:
        cmd.extend(["-b", branch, str(wt)])
    else:
        cmd.extend(["--detach", str(wt)])

    subprocess.run(cmd, cwd=str(base), check=True, capture_output=True, text=True)
    git_marker = wt / ".git"
    assert git_marker.exists() and git_marker.is_file(), (
        f"Expected {git_marker} to be a git worktree file"
    )
    return wt


def _is_pid_running(pid: int) -> bool:
    """Return True if pid is alive and not a zombie ('Z') process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            raw = stat_path.read_text(encoding="utf-8", errors="replace")
            rparen = raw.rfind(")")
            if rparen != -1:
                fields = raw[rparen + 2 :].split()
                if fields and fields[0] == "Z":
                    return False
        except OSError:
            return False
    return True


def _is_port_open(host: str, port: int) -> bool:
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex((host, port)) == 0


def find_itrack_daemons(tmp_prefix: str | Path | None = None) -> list[dict[str, Any]]:
    """Scan /proc for running itrack-server.py processes (optionally scoped to tmp_prefix)."""
    my_pid = os.getpid()
    prefix_str: str | None = None
    prefix_resolved: str | None = None
    if tmp_prefix is not None:
        prefix_str = str(tmp_prefix)
        try:
            prefix_resolved = str(Path(tmp_prefix).resolve())
        except OSError:
            prefix_resolved = prefix_str

    results: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return results

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            cmdline_bytes = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmdline_bytes:
            continue
        argv = [part.decode("utf-8", errors="replace") for part in cmdline_bytes.split(b"\x00") if part]
        if not any("itrack-server.py" in token for token in argv):
            continue
        if not _is_pid_running(pid):
            continue

        root_arg: str | None = None
        port_arg: int = 0
        for idx, tok in enumerate(argv):
            if tok == "--root" and idx + 1 < len(argv):
                root_arg = argv[idx + 1]
            elif tok.startswith("--root="):
                root_arg = tok.split("=", 1)[1]
            elif tok == "--port" and idx + 1 < len(argv):
                try:
                    port_arg = int(argv[idx + 1])
                except ValueError:
                    pass

        if prefix_str is not None:
            matched = False
            if root_arg:
                try:
                    root_resolved = str(Path(root_arg).resolve())
                except OSError:
                    root_resolved = root_arg
                if (
                    root_arg.startswith(prefix_str)
                    or (prefix_resolved and root_arg.startswith(prefix_resolved))
                    or root_resolved.startswith(prefix_str)
                    or (prefix_resolved and root_resolved.startswith(prefix_resolved))
                ):
                    matched = True
            if not matched:
                continue

        results.append(
            {
                "pid": pid,
                "argv": argv,
                "repo_root": root_arg,
                "port": port_arg,
            }
        )
    return results


def find_headless_browsers() -> list[dict[str, Any]]:
    """Scan /proc for running headless Chromium / Playwright browser processes."""
    my_pid = os.getpid()
    found: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return found

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            cmdline_bytes = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmdline_bytes:
            continue
        argv = [part.decode("utf-8", errors="replace") for part in cmdline_bytes.split(b"\x00") if part]
        joined = " ".join(argv)
        if (
            ("chromium" in joined or "headless_shell" in joined or "chrome-linux" in joined)
            and "--headless" in joined
            and _is_pid_running(pid)
        ):
            found.append({"pid": pid, "argv": argv})
    return found


def _terminate_pid_gracefully(pid: int, graceful_timeout: float = 3.0) -> None:
    """Send SIGTERM so Python/coverage atexit handlers flush shards, escalating to SIGKILL only if stuck."""
    if not _is_pid_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.monotonic() + graceful_timeout
    while time.monotonic() < deadline:
        if not _is_pid_running(pid):
            return
        time.sleep(0.04)

    if _is_pid_running(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return
        kill_deadline = time.monotonic() + 1.0
        while time.monotonic() < kill_deadline and _is_pid_running(pid):
            time.sleep(0.03)


def cleanup_repo_daemon(repo_dir: Path | str, timeout: float = 5.0) -> None:
    """Gracefully shut down a workspace's daemon via /api/v1/shutdown, reap PID, and verify port release."""
    repo = Path(repo_dir)
    try:
        repo_resolved = repo.resolve()
    except OSError:
        repo_resolved = repo

    daemon_json = repo / "itrack-issues" / ".runtime" / "daemon.json"
    pids: set[int] = set()
    ports: set[int] = set()
    base_url: str | None = None

    if daemon_json.exists():
        try:
            meta = json.loads(daemon_json.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                if meta.get("pid"):
                    pids.add(int(meta["pid"]))
                if meta.get("port"):
                    ports.add(int(meta["port"]))
                if meta.get("base_url"):
                    base_url = str(meta["base_url"]).rstrip("/")
        except Exception:
            pass

    for proc in find_itrack_daemons():
        proc_root = proc.get("repo_root")
        if not proc_root:
            continue
        try:
            proc_resolved = Path(proc_root).resolve()
        except OSError:
            proc_resolved = Path(proc_root)
        if proc_resolved == repo_resolved or Path(proc_root) == repo:
            pids.add(int(proc["pid"]))
            if proc.get("port"):
                ports.add(int(proc["port"]))

    if base_url:
        try:
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                client.post(f"{base_url}/api/v1/shutdown")
        except Exception:
            pass

    # Allow brief moment for /api/v1/shutdown graceful uvicorn exit before sending SIGTERM
    if pids:
        wait_until = time.monotonic() + min(1.2, timeout * 0.4)
        while time.monotonic() < wait_until:
            if all(not _is_pid_running(pid) for pid in pids):
                break
            time.sleep(0.04)

    for pid in pids:
        _terminate_pid_gracefully(pid, graceful_timeout=max(1.5, timeout * 0.6))

    for port in ports:
        if port > 0:
            port_deadline = time.monotonic() + 1.5
            while time.monotonic() < port_deadline and _is_port_open("127.0.0.1", port):
                time.sleep(0.04)

    if daemon_json.exists():
        try:
            daemon_json.unlink(missing_ok=True)
        except OSError:
            pass


def reap_all_test_daemons(tmp_prefix: str | Path = "/tmp") -> list[int]:
    """Find and gracefully reap any stray itrack-server.py processes whose --root is under tmp_prefix."""
    daemons = find_itrack_daemons(tmp_prefix=tmp_prefix)
    reaped: list[int] = []
    seen_roots: set[str] = set()

    for d in daemons:
        pid = int(d["pid"])
        reaped.append(pid)
        rroot = d.get("repo_root")
        if rroot and rroot not in seen_roots:
            seen_roots.add(rroot)
            cleanup_repo_daemon(rroot)

    for d in daemons:
        pid = int(d["pid"])
        if _is_pid_running(pid):
            _terminate_pid_gracefully(pid, graceful_timeout=2.0)

    return sorted(set(reaped))


# ---------------------------------------------------------------------------
# Telemetry / Surface Event Logging
# ---------------------------------------------------------------------------


def record_surface_event(event: dict[str, Any]) -> None:
    """Append a flock-synchronized JSONL event to ITRACK_SURFACE_LOG if configured."""
    log_path_str = os.environ.get("ITRACK_SURFACE_LOG", "").strip()
    if not log_path_str:
        return
    log_path = Path(log_path_str)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
    with open(log_path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_surface_events(log_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read all JSONL surface telemetry events from ITRACK_SURFACE_LOG."""
    target = Path(log_path) if log_path is not None else Path(os.environ.get("ITRACK_SURFACE_LOG", ""))
    if not str(target) or not target.exists():
        return []
    events: list[dict[str, Any]] = []
    with open(target, "r", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        events.append(obj)
                except json.JSONDecodeError:
                    continue
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return events


def harvest_browser_telemetry(page: Any) -> dict[str, list[str]]:
    """Extract visited UI views, modals, and actions from Playwright page & sessionStorage into ITRACK_SURFACE_LOG."""
    empty: dict[str, list[str]] = {"views": [], "modals": [], "actions": []}
    if page is None:
        return empty
    try:
        if page.is_closed():
            return empty
        data = page.evaluate(
            """() => {
                const out = { views: new Set(), modals: new Set(), actions: new Set() };
                const merge = (src) => {
                    if (!src || typeof src !== 'object') return;
                    for (const k of ['views', 'modals', 'actions']) {
                        const vals = src[k];
                        if (vals) {
                            for (const item of Array.from(vals)) {
                                if (typeof item === 'string' && item) out[k].add(item);
                            }
                        }
                    }
                };
                try {
                    const saved = sessionStorage.getItem('__itrack_accum_telemetry');
                    if (saved) merge(JSON.parse(saved));
                } catch (e) {}
                try {
                    if (typeof window.getItrackTelemetry === 'function') {
                        merge(window.getItrackTelemetry());
                    }
                } catch (e) {}
                try {
                    if (window.__itrackTelemetry) {
                        merge(window.__itrackTelemetry);
                    }
                } catch (e) {}
                return {
                    views: Array.from(out.views).sort(),
                    modals: Array.from(out.modals).sort(),
                    actions: Array.from(out.actions).sort(),
                };
            }"""
        )
        if isinstance(data, dict):
            result = {
                "views": sorted(set(data.get("views") or [])),
                "modals": sorted(set(data.get("modals") or [])),
                "actions": sorted(set(data.get("actions") or [])),
            }
            record_surface_event(
                {
                    "kind": "ui_telemetry",
                    "views": result["views"],
                    "modals": result["modals"],
                    "actions": result["actions"],
                }
            )
            return result
    except Exception:
        pass
    return empty


# ---------------------------------------------------------------------------
# CLI Runner & ITrackWorkspace Helper
# ---------------------------------------------------------------------------


@dataclass
class CLIResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float = 0.0

    @property
    def rc(self) -> int:
        return self.returncode

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"Expected valid JSON on stdout for {self.args}, got:\n"
                f"STDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
            ) from exc


def run_itrack_cli(
    *args: Any,
    cwd: Path | str | None = None,
    stdin: str | bytes | None = None,
    expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
    env_extra: dict[str, str | None] | None = None,
    timeout: float = 30.0,
    script_path: Path = ITRACK_PY,
) -> CLIResult:
    """Invoke `python -B itrack.py ...` as a subprocess and return CLIResult."""
    str_args = [str(a) for a in args]
    cmd = [sys.executable, "-B", str(script_path), *str_args]

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    env.setdefault("COLUMNS", "140")
    if env_extra:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)

    # Record early-exit global flags (-h/--help/-V/--version) so argparse SystemExit(0) never hides them.
    if env.get("ITRACK_SURFACE_LOG"):
        early_flags: list[str] = []
        for tok in str_args:
            if tok in ("-h", "--help"):
                early_flags.append("--help")
            elif tok in ("-V", "--version"):
                early_flags.append("--version")
            elif tok == "--json":
                early_flags.append("--json")
            elif tok == "--root" or tok.startswith("--root="):
                early_flags.append("--root")
        if early_flags:
            record_surface_event({"kind": "cli_flag_hint", "flags": sorted(set(early_flags))})

    run_cwd = str(cwd) if cwd is not None else str(REPO_ROOT)
    t0 = time.monotonic()
    if isinstance(stdin, bytes):
        proc = subprocess.run(
            cmd,
            cwd=run_cwd,
            input=stdin,
            capture_output=True,
            env=env,
            timeout=timeout,
        )
        stdout_str = proc.stdout.decode("utf-8", errors="replace")
        stderr_str = proc.stderr.decode("utf-8", errors="replace")
    else:
        proc = subprocess.run(
            cmd,
            cwd=run_cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        stdout_str = proc.stdout
        stderr_str = proc.stderr

    elapsed = time.monotonic() - t0
    res = CLIResult(
        args=str_args,
        returncode=proc.returncode,
        stdout=stdout_str,
        stderr=stderr_str,
        duration_sec=elapsed,
    )

    if expect_rc is not None:
        if isinstance(expect_rc, int):
            allowed = {expect_rc}
        else:
            allowed = set(expect_rc)
        if res.returncode not in allowed:
            raise AssertionError(
                f"CLI command failed exit-code assertion!\n"
                f"  Args: {str_args}\n"
                f"  CWD:  {run_cwd}\n"
                f"  Expected RC: {sorted(allowed)}, Actual RC: {res.returncode}\n"
                f"  STDOUT:\n{res.stdout}\n"
                f"  STDERR:\n{res.stderr}"
            )
    return res


class ITrackWorkspace:
    """High-level test wrapper around an isolated Git + iTrack workspace."""

    def __init__(self, root: Path | str, script_path: Path = ITRACK_PY) -> None:
        self.root: Path = Path(root)
        self.repo_root: Path = self.root
        self.script_path: Path = Path(script_path)

    @property
    def path(self) -> Path:
        return self.root

    @property
    def issues_root(self) -> Path:
        return self.root / "itrack-issues"

    @property
    def issues_dir(self) -> Path:
        return self.issues_root / "issues"

    @property
    def runtime_dir(self) -> Path:
        return self.issues_root / ".runtime"

    @property
    def daemon_json_path(self) -> Path:
        return self.runtime_dir / "daemon.json"

    def issue_dir(self, issue_name: str) -> Path:
        return self.issues_dir / issue_name

    def cli(
        self,
        *args: Any,
        stdin: str | bytes | None = None,
        expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
        env_extra: dict[str, str | None] | None = None,
        cwd: Path | str | None = None,
        use_root_flag: bool = False,
        timeout: float = 30.0,
    ) -> CLIResult:
        full_args: list[Any] = []
        if use_root_flag:
            full_args.extend(["--root", str(self.root)])
        full_args.extend(args)
        return run_itrack_cli(
            *full_args,
            cwd=cwd if cwd is not None else self.root,
            stdin=stdin,
            expect_rc=expect_rc,
            env_extra=env_extra,
            timeout=timeout,
            script_path=self.script_path,
        )

    def cli_json(
        self,
        *args: Any,
        stdin: str | bytes | None = None,
        expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
        env_extra: dict[str, str | None] | None = None,
        cwd: Path | str | None = None,
        use_root_flag: bool = False,
        timeout: float = 30.0,
    ) -> Any:
        str_args = [str(a) for a in args]
        if "--json" not in str_args:
            str_args.append("--json")
        res = self.cli(
            *str_args,
            stdin=stdin,
            expect_rc=expect_rc,
            env_extra=env_extra,
            cwd=cwd,
            use_root_flag=use_root_flag,
            timeout=timeout,
        )
        return res.json()

    def get_base_url(self) -> str | None:
        if not self.daemon_json_path.exists():
            return None
        try:
            meta = json.loads(self.daemon_json_path.read_text(encoding="utf-8"))
            base_url = str(meta.get("base_url") or "").rstrip("/")
            if not base_url:
                return None
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                resp = client.get(f"{base_url}/api/v1/health")
                if resp.status_code == 200:
                    return base_url
        except Exception:
            return None
        return None

    def ensure_server(self, port: int = 0) -> str:
        existing = self.get_base_url()
        if existing and port == 0:
            return existing

        start_args: list[str] = ["server", "start"]
        if port:
            start_args.extend(["--port", str(port)])
        data = self.cli_json(*start_args)
        base_url = ""
        if isinstance(data, dict):
            base_url = str(data.get("base_url") or data.get("url") or "").rstrip("/")
        if not base_url:
            base_url = self.get_base_url() or ""
        assert base_url, f"Failed to obtain daemon base_url for workspace {self.root}"
        return base_url

    def api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        base_url = self.ensure_server()
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = f"{base_url}/{path.lstrip('/')}"
        req_timeout = kwargs.pop("timeout", 15.0)
        with httpx.Client(timeout=req_timeout, trust_env=False) as client:
            return client.request(method.upper(), url, **kwargs)

    def stop_server(self) -> None:
        cleanup_repo_daemon(self.root)

    def git_status_porcelain(self) -> str:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.root),
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()


# ---------------------------------------------------------------------------
# Standards-Compliant Synthetic Asset / File Generators
# ---------------------------------------------------------------------------


def sha256_file(path: Path | str) -> str:
    """Compute hex SHA-256 digest of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_bytes_and_hash(path: Path | str, payload: bytes) -> tuple[Path, str]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target, hashlib.sha256(payload).hexdigest()


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def make_png(
    path: Path | str,
    width: int = 32,
    height: int = 32,
    color: tuple[int, int, int] = (37, 99, 235),
) -> tuple[Path, str]:
    """Generate a valid RGB PNG image decodable in Chromium (`naturalWidth == width > 0`)."""
    assert width > 0 and height > 0, "PNG dimensions must be positive"
    r, g, b = (color[0] & 0xFF, color[1] & 0xFF, color[2] & 0xFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((r, g, b)) * width
    raw_scanlines = row * height
    idat = zlib.compress(raw_scanlines, level=9)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    return _write_bytes_and_hash(path, png_bytes)


def make_svg(
    path: Path | str,
    label: str = "diagram",
    width: int = 120,
    height: int = 60,
) -> tuple[Path, str]:
    """Generate a valid standalone SVG image decodable in Chromium (`naturalWidth == width > 0`)."""
    safe_label = html.escape(label, quote=True)
    svg_text = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" rx="6" fill="#1e293b"/>'
        f'<text x="{width // 2}" y="{height // 2 + 4}" fill="#f8fafc" '
        f'font-family="monospace" font-size="12" text-anchor="middle">{safe_label}</text>'
        f"</svg>\n"
    )
    return _write_bytes_and_hash(path, svg_text.encode("utf-8"))


def make_jpg(
    path: Path | str,
    width: int = 16,
    height: int = 16,
    color: tuple[int, int, int] = (220, 38, 38),
) -> tuple[Path, str]:
    """Generate a valid Baseline DCT (SOF0) JFIF JPEG image decodable in Chromium."""
    assert width > 0 and height > 0, "JPEG dimensions must be positive"
    r, g, b = (color[0] & 0xFF, color[1] & 0xFF, color[2] & 0xFF)
    y_val = max(0, min(255, round(0.299 * r + 0.587 * g + 0.114 * b)))
    cb_val = max(0, min(255, round(128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b)))
    cr_val = max(0, min(255, round(128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b)))

    # Quantization table 0: Q[0]=8 so DC = 8*(P-128) quantizes cleanly to (P-128) in [-128..127].
    qtable = bytes([8] + [16] * 63)
    dqt_payload = b"\x00" + qtable
    dqt = b"\xff\xdb" + struct.pack(">H", len(dqt_payload) + 2) + dqt_payload

    # APP0 JFIF 1.01 segment
    app0_payload = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload

    # SOF0 (Baseline DCT, 8-bit, 3 components Y/Cb/Cr at 1x1 sampling)
    sof0_payload = struct.pack(
        ">BHHBBBBBBBBBB",
        8,
        height,
        width,
        3,
        1,
        0x11,
        0,  # Y
        2,
        0x11,
        0,  # Cb
        3,
        0x11,
        0,  # Cr
    )
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload

    # DHT:
    # - DC table 0 (class 0, id 0): 9 symbols (0..8), all length 4 -> canonical codes 0000..1000
    dc_counts = bytes([0, 0, 0, 9] + [0] * 12)
    dc_syms = bytes(range(9))
    dht_dc_payload = b"\x00" + dc_counts + dc_syms
    dht_dc = b"\xff\xc4" + struct.pack(">H", len(dht_dc_payload) + 2) + dht_dc_payload

    # - AC table 0 (class 1, id 0): 1 symbol (0x00 EOB), length 1 -> code 0
    ac_counts = bytes([1] + [0] * 15)
    ac_syms = b"\x00"
    dht_ac_payload = b"\x10" + ac_counts + ac_syms
    dht_ac = b"\xff\xc4" + struct.pack(">H", len(dht_ac_payload) + 2) + dht_ac_payload

    # SOS header (3 components referencing DC table 0 / AC table 0)
    sos_payload = bytes([3, 1, 0x00, 2, 0x00, 3, 0x00, 0, 63, 0])
    sos = b"\xff\xda" + struct.pack(">H", len(sos_payload) + 2) + sos_payload

    # MSB-first entropy bit writer with 0xFF -> 0xFF 0x00 byte stuffing
    ecs = bytearray()
    bit_buf = 0
    bit_count = 0

    def put_bits(val: int, nbits: int) -> None:
        nonlocal bit_buf, bit_count
        for shift in range(nbits - 1, -1, -1):
            bit_buf = (bit_buf << 1) | ((val >> shift) & 1)
            bit_count += 1
            if bit_count == 8:
                ecs.append(bit_buf)
                if bit_buf == 0xFF:
                    ecs.append(0x00)
                bit_buf = 0
                bit_count = 0

    mcu_count = math.ceil(width / 8) * math.ceil(height / 8)
    targets = [y_val - 128, cb_val - 128, cr_val - 128]
    prev_dc = [0, 0, 0]

    for _ in range(mcu_count):
        for ch in range(3):
            diff = targets[ch] - prev_dc[ch]
            prev_dc[ch] = targets[ch]
            if diff == 0:
                put_bits(0, 4)  # DC symbol 0 (4-bit code 0000)
            else:
                ssss = abs(diff).bit_length()
                put_bits(ssss, 4)  # DC symbol ssss (4-bit code == ssss)
                mag = diff if diff > 0 else (diff + (1 << ssss) - 1)
                put_bits(mag, ssss)
            put_bits(0, 1)  # AC EOB (1-bit code 0)

    if bit_count > 0:
        put_bits((1 << (8 - bit_count)) - 1, 8 - bit_count)

    jpg_bytes = b"\xff\xd8" + app0 + dqt + sof0 + dht_dc + dht_ac + sos + bytes(ecs) + b"\xff\xd9"
    return _write_bytes_and_hash(path, jpg_bytes)


def make_webp(
    path: Path | str,
    width: int = 1,
    height: int = 1,
    color: tuple[int, int, int] = (16, 185, 129),
) -> tuple[Path, str]:
    """Generate a valid RIFF WEBP lossless VP8L image decodable in Chromium."""
    assert 1 <= width <= 16384 and 1 <= height <= 16384, "Invalid WebP dimensions"
    r, g, b = (color[0] & 0xFF, color[1] & 0xFF, color[2] & 0xFF)
    hdr32 = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)

    # LSB-first VP8L bitstream writer
    stream = bytearray()
    cur_byte = 0
    used_bits = 0

    def write_lsb(val: int, nbits: int) -> None:
        nonlocal cur_byte, used_bits
        for i in range(nbits):
            cur_byte |= ((val >> i) & 1) << used_bits
            used_bits += 1
            if used_bits == 8:
                stream.append(cur_byte)
                cur_byte = 0
                used_bits = 0

    write_lsb(0, 1)  # transform_present = 0
    write_lsb(0, 1)  # color_cache_present = 0
    write_lsb(0, 1)  # meta_prefix_present = 0

    # 5 single-symbol simple prefix codes: Green, Red, Blue, Alpha, Distance
    for sym in (g, r, b, 255):
        write_lsb(1, 1)  # is_simple = 1
        write_lsb(0, 1)  # num_symbols - 1 = 0 (1 symbol -> 0 bits per pixel)
        write_lsb(1, 1)  # is_first_8bits = 1
        write_lsb(sym, 8)

    # Distance prefix code: single 1-bit symbol 0
    write_lsb(1, 1)  # is_simple = 1
    write_lsb(0, 1)  # num_symbols - 1 = 0
    write_lsb(0, 1)  # is_first_8bits = 0
    write_lsb(0, 1)  # symbol0 = 0

    if used_bits > 0:
        stream.append(cur_byte)

    vp8l_payload = b"\x2f" + struct.pack("<I", hdr32) + bytes(stream)
    pad = b"\x00" if (len(vp8l_payload) % 2 == 1) else b""
    riff_size = 4 + 8 + len(vp8l_payload) + len(pad)
    webp_bytes = (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WEBP"
        + b"VP8L"
        + struct.pack("<I", len(vp8l_payload))
        + vp8l_payload
        + pad
    )
    return _write_bytes_and_hash(path, webp_bytes)


def make_pdf(path: Path | str, text: str = "iTrack Specification") -> tuple[Path, str]:
    """Generate a valid %PDF-1.4 document with exact xref byte offsets and %%EOF."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1", errors="replace")
    )
    stream_body = b"BT /F1 14 Tf 50 740 Td (" + escaped + b") Tj ET\n"

    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream_body)} >>\nstream\n".encode("ascii")
            + stream_body
            + b"endstream\nendobj\n"
        ),
    ]

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(buf))
        buf.extend(obj)

    xref_offset = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for off in offsets:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    buf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return _write_bytes_and_hash(path, bytes(buf))


def make_zip(
    path: Path | str,
    files: dict[str, str | bytes] | None = None,
) -> tuple[Path, str]:
    """Generate a deterministic valid ZIP archive on disk."""
    entries = (
        {"trace.log": "ERROR line 1\n", "meta.json": '{"ok": true}'}
        if files is None
        else files
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in entries.items():
            info = zipfile.ZipInfo(filename=arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            raw = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(info, raw)
    return target, sha256_file(target)


def make_json_file(path: Path | str, data: Any = None) -> tuple[Path, str]:
    """Generate a UTF-8 JSON file on disk."""
    payload_obj = {"service": "itrack", "ok": True, "items": [1, 2, 3]} if data is None else data
    encoded = (json.dumps(payload_obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return _write_bytes_and_hash(path, encoded)


def make_text_file(
    path: Path | str,
    text: str = "Sample iTrack text attachment\nLine 2\n",
) -> tuple[Path, str]:
    """Generate a UTF-8 plain-text file on disk."""
    return _write_bytes_and_hash(path, text.encode("utf-8"))


def make_zero_byte_file(path: Path | str) -> tuple[Path, str]:
    """Generate a 0-byte empty file on disk."""
    return _write_bytes_and_hash(path, b"")


def make_random_binary(
    path: Path | str,
    size_bytes: int = 65536,
    seed: int | None = 42,
) -> tuple[Path, str]:
    """Generate a non-UTF8 binary file of exact size_bytes."""
    assert size_bytes >= 0, "size_bytes must be non-negative"
    if size_bytes == 0:
        return make_zero_byte_file(path)
    rng = random.Random(seed) if seed is not None else random.Random()
    raw = bytearray(rng.randbytes(size_bytes))
    header = b"\x80\xff\x00\xfe"
    for idx in range(min(len(header), size_bytes)):
        raw[idx] = header[idx]
    return _write_bytes_and_hash(path, bytes(raw))


# ---------------------------------------------------------------------------
# Dynamic CLI / API / SPA Introspection & Surface Coverage Audit
# ---------------------------------------------------------------------------


def load_module_from_path(module_name: str, file_path: Path | str) -> types.ModuleType:
    """Import a Python file (including hyphenated `itrack-server.py`) without writing bytecode."""
    prev_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        fpath = Path(file_path).resolve()
        spec = importlib.util.spec_from_file_location(module_name, str(fpath))
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to create module spec for {fpath}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = prev_flag


def load_itrack_cli_module() -> types.ModuleType:
    return load_module_from_path("_itrack_cli_introspect", ITRACK_PY)


def load_itrack_server_module() -> types.ModuleType:
    return load_module_from_path("_itrack_server_introspect", ITRACK_SERVER_PY)


def _canonical_flag_for_action(action: argparse.Action) -> str | None:
    if not action.option_strings:
        return None
    long_opts = [opt for opt in action.option_strings if opt.startswith("--")]
    if long_opts:
        return long_opts[0]
    return action.option_strings[0]


def introspect_cli_surface(itrack_py_module: Any = None) -> dict[str, Any]:
    """Introspect `build_parser()` in `itrack.py` for all canonical subcommands and option flags."""
    mod = itrack_py_module if itrack_py_module is not None else load_itrack_cli_module()
    if isinstance(mod, argparse.ArgumentParser):
        parser = mod
    else:
        builder = (
            getattr(mod, "build_parser", None)
            or getattr(mod, "create_parser", None)
            or getattr(mod, "build_arg_parser", None)
        )
        if builder is None:
            raise AttributeError("CLI module does not define build_parser()")
        parser = builder()

    option_map: dict[str, str] = {}
    global_flags: set[str] = {"--json", "--root", "--version", "--help"}

    for act in parser._actions:
        canon = _canonical_flag_for_action(act)
        if canon:
            global_flags.add(canon)
            for opt in act.option_strings:
                option_map[opt] = canon

    leaf_commands: list[str] = []
    group_commands: list[str] = []
    command_flags: dict[str, list[str]] = {}
    all_flags: set[str] = set(global_flags)

    def walk_parser(cur_parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        sub_actions = [
            a for a in cur_parser._actions if isinstance(a, argparse._SubParsersAction)
        ]
        local_flags: set[str] = set()
        for act in cur_parser._actions:
            if isinstance(act, argparse._SubParsersAction):
                continue
            canon = _canonical_flag_for_action(act)
            if not canon:
                continue
            for opt in act.option_strings:
                option_map[opt] = canon
            all_flags.add(canon)
            if canon not in ("--help",):
                local_flags.add(canon)

        if not sub_actions:
            if prefix:
                cmd_name = " ".join(prefix)
                leaf_commands.append(cmd_name)
                command_flags[cmd_name] = sorted(local_flags - {"--json", "--root", "--version", "--help"})
            return

        if prefix:
            group_commands.append(" ".join(prefix))

        for sub_act in sub_actions:
            # _choices_actions lists canonical subparser names (excluding aliases) in order.
            canonical_names: list[str] = [ca.dest for ca in sub_act._choices_actions]
            seen_parsers: set[int] = set()
            for name in canonical_names:
                child = sub_act.choices.get(name)
                if child is None or id(child) in seen_parsers:
                    continue
                seen_parsers.add(id(child))
                walk_parser(child, (*prefix, name))
            # Fallback if a subparser had no entry in _choices_actions:
            for name, child in sub_act.choices.items():
                if id(child) not in seen_parsers:
                    seen_parsers.add(id(child))
                    walk_parser(child, (*prefix, name))

    walk_parser(parser, ())

    option_map.setdefault("-h", "--help")
    option_map.setdefault("--help", "--help")
    option_map.setdefault("-V", "--version")
    option_map.setdefault("--version", "--version")
    option_map.setdefault("--json", "--json")
    option_map.setdefault("--root", "--root")

    return {
        "commands": sorted(set(leaf_commands)),
        "group_commands": sorted(set(group_commands)),
        "all_commands": sorted(set(leaf_commands) | set(group_commands)),
        "command_flags": {k: command_flags[k] for k in sorted(command_flags)},
        "global_flags": sorted(global_flags),
        "all_flags": sorted(all_flags),
        "option_map": option_map,
    }


def introspect_api_routes(itrack_server_module: Any = None) -> list[tuple[str, str]]:
    """Introspect FastAPI app in `itrack-server.py` and return sorted `(method, path)` routes on `/api/v1/*`."""
    mod = itrack_server_module if itrack_server_module is not None else load_itrack_server_module()

    routes_set: set[tuple[str, str]] = set()
    with tempfile.TemporaryDirectory(prefix="itrack_api_introspect_") as tmpdir:
        app_obj = getattr(mod, "app", None)
        if app_obj is None and hasattr(mod, "create_app"):
            sig = inspect.signature(mod.create_app)
            if len(sig.parameters) == 0:
                app_obj = mod.create_app()
            else:
                app_obj = mod.create_app(Path(tmpdir))

        if app_obj is None:
            raise RuntimeError("Could not locate FastAPI `app` or `create_app` in itrack-server.py")

        for route in getattr(app_obj, "routes", []):
            path = getattr(route, "path", "")
            if not path.startswith("/api/v1/"):
                continue
            methods = getattr(route, "methods", None) or set()
            for method in methods:
                m_up = method.upper()
                if m_up in ("HEAD", "OPTIONS"):
                    continue
                routes_set.add((m_up, path))

    return sorted(routes_set)


def normalize_route_shape(path: str) -> str:
    """Normalize parameter placeholders (`/api/v1/issues/{issue_ref}` -> `/api/v1/issues/{}`) for parity matching."""
    clean = path.split("?", 1)[0].rstrip("/")
    return re.sub(r"\{[^/{}]+\}", "{}", clean)


def _extract_js_api_paths(spa_html: str) -> tuple[list[str], list[str]]:
    """Scan JS/HTML string & template literals for `/api/v1/...` paths, normalizing `${...}` -> `{param}`."""
    raw_found: set[str] = set()
    norm_found: set[str] = set()

    n = len(spa_html)
    i = 0
    while i < n:
        ch = spa_html[i]
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            buf: list[str] = []
            while j < n:
                c = spa_html[j]
                if c == "\\" and j + 1 < n:
                    buf.append(spa_html[j : j + 2])
                    j += 2
                    continue
                if quote == "`" and c == "$" and j + 1 < n and spa_html[j + 1] == "{":
                    # Consume balanced ${...} expression
                    depth = 1
                    k = j + 2
                    while k < n and depth > 0:
                        if spa_html[k] == "\\" and k + 1 < n:
                            k += 2
                            continue
                        if spa_html[k] == "{":
                            depth += 1
                        elif spa_html[k] == "}":
                            depth -= 1
                        k += 1
                    buf.append("{param}")
                    j = k
                    continue
                if c == quote:
                    break
                buf.append(c)
                j += 1

            literal = "".join(buf)
            if "/api/v1/" in literal:
                for m in re.finditer(r"/api/v1/[A-Za-z0-9_.\-/{}?&=]+", literal):
                    candidate = m.group(0)
                    raw_found.add(candidate)
                    base = candidate.split("?", 1)[0].rstrip("/")
                    if base and base != "/api/v1":
                        norm_found.add(base)
            i = j + 1
        else:
            i += 1

    return sorted(norm_found), sorted(raw_found)


def introspect_spa_surface(spa_html: str | None = None) -> dict[str, Any]:
    """Introspect SPA_HTML for declared views, modals, and fetched `/api/v1/*` endpoints."""
    if spa_html is None:
        server_mod = load_itrack_server_module()
        spa_html = getattr(server_mod, "SPA_HTML", "")

    views = set(re.findall(r'(?:^|[\s<])data-view=["\']([a-z][a-z0-9-]*)["\']', spa_html))
    if not views:
        views = set(re.findall(r'(?:^|[\s<])id=["\']view-([a-z][a-z0-9-]*)["\']', spa_html))
    for v in re.findall(r"""(?:switchView|openView|showView)\(\s*['"]([a-z][a-z0-9-]*)['"]\s*\)""", spa_html):
        views.add(v)

    modals = set(re.findall(r'(?:^|[\s<])data-modal=["\']([a-z][a-z0-9-]*)["\']', spa_html))
    if not modals:
        modals = set(re.findall(r'(?:^|[\s<])id=["\']modal-([a-z][a-z0-9-]*)["\']', spa_html))
    for m in re.findall(r"""(?:openModal|showModal)\(\s*['"]([a-z][a-z0-9-]*)['"]\s*\)""", spa_html):
        modals.add(m)

    endpoints, raw_endpoints = _extract_js_api_paths(spa_html)
    return {
        "views": sorted(views),
        "modals": sorted(modals),
        "endpoints": endpoints,
        "raw_endpoints": raw_endpoints,
        "endpoint_shapes": sorted({normalize_route_shape(ep) for ep in endpoints}),
    }


def compute_surface_audit(
    log_path: Path | str | None = None,
    cli_mod: Any = None,
    server_mod: Any = None,
) -> dict[str, Any]:
    """Compare recorded ITRACK_SURFACE_LOG telemetry against 100% of API, CLI, and UI surfaces."""
    s_mod = server_mod if server_mod is not None else load_itrack_server_module()
    c_mod = cli_mod if cli_mod is not None else load_itrack_cli_module()

    expected_routes = set(introspect_api_routes(s_mod))
    cli_surface = introspect_cli_surface(c_mod)
    expected_commands = set(cli_surface["commands"])
    expected_flags = set(cli_surface["all_flags"])
    option_map: dict[str, str] = cli_surface["option_map"]

    spa_surface = introspect_spa_surface(getattr(s_mod, "SPA_HTML", ""))
    expected_views = set(UI_VIEWS)
    expected_modals = set(UI_MODALS)

    events = read_surface_events(log_path)

    hit_routes: set[tuple[str, str]] = set()
    hit_commands: set[str] = set()
    hit_flags: set[str] = set()
    hit_views: set[str] = set()
    hit_modals: set[str] = set()

    for ev in events:
        kind = ev.get("kind")
        if kind == "api_route":
            method = str(ev.get("method") or "").upper()
            route = str(ev.get("route") or ev.get("path") or "")
            if method and route:
                hit_routes.add((method, route))
        elif kind == "cli_exec":
            cmd = str(ev.get("command") or "").strip()
            if cmd:
                hit_commands.add(cmd)
            for fl in ev.get("flags") or []:
                fl_str = str(fl)
                hit_flags.add(option_map.get(fl_str, fl_str))
        elif kind == "cli_flag_hint":
            for fl in ev.get("flags") or []:
                fl_str = str(fl)
                hit_flags.add(option_map.get(fl_str, fl_str))
        elif kind == "ui_view":
            v = str(ev.get("view") or "").strip()
            if v:
                hit_views.add(v)
        elif kind == "ui_modal":
            m = str(ev.get("modal") or "").strip()
            if m:
                hit_modals.add(m)

        if isinstance(ev.get("views"), list):
            for v in ev["views"]:
                if isinstance(v, str) and v:
                    hit_views.add(v)
        if isinstance(ev.get("modals"), list):
            for m in ev["modals"]:
                if isinstance(m, str) and m:
                    hit_modals.add(m)

    matched_routes = expected_routes & hit_routes
    matched_commands = expected_commands & hit_commands
    matched_flags = expected_flags & hit_flags
    matched_views = expected_views & hit_views
    matched_modals = expected_modals & hit_modals

    backend_shapes = {normalize_route_shape(path) for _, path in expected_routes}
    orphan_spa_endpoints = sorted(
        ep for ep in spa_surface["endpoints"] if normalize_route_shape(ep) not in backend_shapes
    )

    def _pct(done: int, total: int) -> float:
        return 100.0 if total == 0 else (100.0 * done / total)

    return {
        "api_routes": {
            "expected": sorted(expected_routes),
            "hit": sorted(matched_routes),
            "missing": sorted(expected_routes - matched_routes),
            "pct": _pct(len(matched_routes), len(expected_routes)),
        },
        "cli_commands": {
            "expected": sorted(expected_commands),
            "hit": sorted(matched_commands),
            "missing": sorted(expected_commands - matched_commands),
            "pct": _pct(len(matched_commands), len(expected_commands)),
        },
        "cli_flags": {
            "expected": sorted(expected_flags),
            "hit": sorted(matched_flags),
            "missing": sorted(expected_flags - matched_flags),
            "pct": _pct(len(matched_flags), len(expected_flags)),
        },
        "ui_views": {
            "expected": sorted(expected_views),
            "hit": sorted(matched_views),
            "missing": sorted(expected_views - matched_views),
            "pct": _pct(len(matched_views), len(expected_views)),
        },
        "ui_modals": {
            "expected": sorted(expected_modals),
            "hit": sorted(matched_modals),
            "missing": sorted(expected_modals - matched_modals),
            "pct": _pct(len(matched_modals), len(expected_modals)),
        },
        "spa_surface": spa_surface,
        "orphan_spa_endpoints": orphan_spa_endpoints,
    }
