#!/usr/bin/env python3
"""itrack.py — 100% Surface Git-Rooted CLI, Self-Teaching Manual & Auto-Daemon Client."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import getpass
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

import httpx
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

VERSION = "2.0.0"

CATEGORIES: list[str] = [
    "epic", "story", "task", "subtask", "bug", "feature", "improvement",
    "refactor", "spike", "incident", "security", "chore", "docs",
]

STATES: list[str] = [
    "triage", "backlog", "todo", "ready", "in-progress", "in-review",
    "in-qa", "on-hold", "blocked", "done", "cancelled", "archived",
]

PRIORITIES: list[str] = ["critical", "high", "medium", "low", "trivial"]
SEVERITIES: list[str] = ["sev1", "sev2", "sev3", "sev4", "sev5"]
RESOLUTIONS: list[str] = [
    "fixed", "wont-fix", "duplicate", "cannot-reproduce", "by-design", "obsolete",
]
LINK_TYPES: list[str] = [
    "blocks", "blocked-by", "relates-to", "duplicates", "parent-of", "clones",
]
PRESET_TABS: list[str] = [
    "active", "open", "in-progress", "blocked", "completed", "cancelled", "archived", "all",
]
SWIMLANES: list[str] = ["none", "category", "priority", "assignee", "sprint"]
IQL_OPERATORS: list[str] = [
    "=", "!=", ">", ">=", "<", "<=", "~", "!~",
    "IN", "NOT IN", "IS EMPTY", "IS NOT EMPTY",
    "AND", "OR", "NOT", "ORDER BY",
]

EXIT_CODE_DOCS: dict[int, str] = {
    0: "Success (including zero-arg manual, --help, --version)",
    1: "Validation / Usage / Invalid value error (HTTP 400, 422, or argparse error)",
    2: "Git-root pre-flight failure (.git missing or inside Git subdirectory)",
    3: "Workspace not initialized (itrack-issues/config.json missing; HTTP 503)",
    4: "Resource not found (issue, comment, attachment, link, tag, sprint, milestone, view, worklog; HTTP 404)",
    5: "Conflict / Referential / Collision error (rename collision, cycle, self-link, duplicate link; HTTP 409)",
}

STATE_STYLES: dict[str, str] = {
    "triage": "magenta",
    "backlog": "dim white",
    "todo": "cyan",
    "ready": "bright_cyan",
    "in-progress": "bold yellow",
    "in-review": "bold magenta",
    "in-qa": "bold blue",
    "on-hold": "yellow",
    "blocked": "bold white on red",
    "done": "bold green",
    "cancelled": "strike dim",
    "archived": "dim",
}

PRIORITY_STYLES: dict[str, str] = {
    "critical": "bold red",
    "high": "bold yellow",
    "medium": "cyan",
    "low": "blue",
    "trivial": "dim",
}

SEVERITY_STYLES: dict[str, str] = {
    "sev1": "bold red",
    "sev2": "red",
    "sev3": "yellow",
    "sev4": "cyan",
    "sev5": "dim",
}

stdout_console = Console(highlight=False)
stderr_console = Console(stderr=True, highlight=False)

KEY_RE = re.compile(r"^[A-Za-z]{2,10}-\d+$")


class CLIError(Exception):
    """Structured CLI error carrying explicit exit code."""

    def __init__(self, message: str, error_code: str = "cli_error", exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.exit_code = exit_code


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_status_to_exit_code(status_code: int) -> int:
    if status_code in (400, 422):
        return 1
    if status_code == 404:
        return 4
    if status_code == 409:
        return 5
    if status_code == 503:
        return 3
    return 1


def normalize_ref_str(ref: str) -> str:
    """Preserve exact spaces & case for issue/entity names; uppercase ID keys like itr-1 -> ITR-1."""
    s = ref.strip()
    if KEY_RE.fullmatch(s):
        return s.upper()
    return ref


def encode_ref(ref: str) -> str:
    """URL-encode an issue key or exact issue name (including spaces, slashes, Unicode)."""
    return urllib.parse.quote(normalize_ref_str(ref), safe="")


def normalize_hyphen_enum(val: str | None) -> str | None:
    if val is None:
        return None
    cleaned = re.sub(r"[\s_]+", "-", val.strip().lower())
    if cleaned == "canceled":
        return "cancelled"
    return cleaned


def flatten_csv_tokens(items: list[str] | None) -> list[str]:
    if not items:
        return []
    out: list[str] = []
    for raw in items:
        for part in raw.split(","):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out


def parse_issue_refs(items: list[str] | None, repo_root: Path) -> list[str]:
    """Parse --issues tokens supporting both space-separated names and comma-separated keys."""
    if not items:
        return []
    issues_dir = repo_root / "itrack-issues" / "issues"
    out: list[str] = []
    for raw in items:
        if "," in raw and not (issues_dir / raw).exists():
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            out.extend(normalize_ref_str(p) for p in parts)
        else:
            val = normalize_ref_str(raw)
            if val.strip():
                out.append(val)
    return out


def default_actor(repo_root: Path | None = None) -> str:
    env_user = os.environ.get("ITRACK_USER") or os.environ.get("ITRACK_AGENT")
    if env_user and env_user.strip():
        return env_user.strip()
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(repo_root) if repo_root and repo_root.exists() else None,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    try:
        return getpass.getuser()
    except Exception:
        return "user"


def emit_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_cli_error(err: CLIError, json_mode: bool) -> NoReturn:
    if json_mode:
        emit_json({
            "error": err.error_code,
            "message": err.message,
            "exit_code": err.exit_code,
        })
    else:
        sys.stderr.write(f"Error: {err.message}\n")
        sys.stderr.flush()
    sys.exit(err.exit_code)


def require_yes(yes_flag: bool, resource_desc: str) -> None:
    if not yes_flag:
        raise CLIError(
            f"Refusing to delete {resource_desc} without confirmation flag '--yes'.",
            error_code="confirmation_required",
            exit_code=1,
        )


# ---------------------------------------------------------------------------
# Telemetry Hook (ITRACK_SURFACE_LOG) & Parser-Aware Global Flag Extraction
# ---------------------------------------------------------------------------


def _canonical_flag_name(action: argparse.Action) -> str:
    for opt in action.option_strings:
        if opt.startswith("--"):
            return opt
    return action.option_strings[0]


def _get_subparsers_map(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def extract_global_flags_and_surface(
    argv: list[str],
    root_parser: argparse.ArgumentParser,
) -> tuple[bool, str | None, list[str], str, list[str]]:
    """Extract --json and --root from ANY position while tracking canonical command & flags."""
    json_mode = False
    root_arg: str | None = None
    remaining: list[str] = []
    cmd_parts: list[str] = []
    used_flags: set[str] = set()

    cur_parser = root_parser
    can_match_subcmd = True
    i = 0
    n = len(argv)

    while i < n:
        tok = argv[i]
        if tok == "--":
            remaining.extend(argv[i:])
            break
        if tok == "--json":
            json_mode = True
            used_flags.add("--json")
            i += 1
            continue
        if tok == "--root":
            used_flags.add("--root")
            if i + 1 >= n:
                raise CLIError("Option '--root' requires a path argument.", "invalid_arguments", 1)
            root_arg = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--root="):
            used_flags.add("--root")
            val = tok.split("=", 1)[1]
            if not val:
                raise CLIError("Option '--root' requires a non-empty path.", "invalid_arguments", 1)
            root_arg = val
            i += 1
            continue

        sub_map = _get_subparsers_map(cur_parser) if can_match_subcmd else {}
        if not tok.startswith("-") and tok in sub_map:
            cmd_parts.append(tok)
            cur_parser = sub_map[tok]
            remaining.append(tok)
            i += 1
            continue

        if tok.startswith("-") and tok != "-":
            opt_key = tok.split("=", 1)[0] if "=" in tok else tok
            action = cur_parser._option_string_actions.get(opt_key)
            if action is None:
                action = root_parser._option_string_actions.get(opt_key)
            if action is not None:
                used_flags.add(_canonical_flag_name(action))
                remaining.append(tok)
                i += 1
                if "=" not in tok:
                    if action.nargs is None:
                        # Consume 1 value token (even if it starts with '-' unless it is --json/--root?)
                        if i < n and argv[i] != "--":
                            remaining.append(argv[i])
                            i += 1
                    elif isinstance(action.nargs, int) and action.nargs > 0:
                        for _ in range(action.nargs):
                            if i < n and argv[i] != "--":
                                remaining.append(argv[i])
                                i += 1
                    elif action.nargs in ("+", "*"):
                        while i < n and not argv[i].startswith("-"):
                            remaining.append(argv[i])
                            i += 1
                    elif action.nargs == "?" and i < n and not argv[i].startswith("-"):
                        remaining.append(argv[i])
                        i += 1
                continue
            # Unrecognized flag: still record if --* and let argparse raise exit 1
            if opt_key.startswith("--"):
                used_flags.add(opt_key)
            remaining.append(tok)
            i += 1
            continue

        # Positional value argument
        can_match_subcmd = False
        remaining.append(tok)
        i += 1

    return json_mode, root_arg, remaining, " ".join(cmd_parts), sorted(used_flags)


def record_cli_surface(command_path: str, flags: list[str]) -> None:
    log_file = os.environ.get("ITRACK_SURFACE_LOG")
    if not log_file:
        return
    try:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"kind": "cli_exec", "command": command_path, "flags": flags}) + "\n"
        with open(p, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(entry)
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Law 2 Git-Root Pre-Flight (Exit 2) & Workspace Init Pre-Flight (Exit 3)
# ---------------------------------------------------------------------------


def detect_git_toplevel_hint(target_path: Path) -> str | None:
    probe_dir = target_path if target_path.is_dir() else target_path.parent
    if not probe_dir.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(probe_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            toplevel = Path(proc.stdout.strip()).resolve()
            if toplevel != target_path.resolve():
                return str(toplevel)
    except Exception:
        pass
    return None


def resolve_and_check_git_root(root_arg: str | None, json_mode: bool) -> Path:
    """Law 2: Verify target directory is a Git repository root (.git dir or worktree file)."""
    if root_arg is not None:
        raw_root = Path(os.path.abspath(os.path.expanduser(root_arg)))
    elif os.environ.get("ITRACK_ROOT"):
        raw_root = Path(os.path.abspath(os.path.expanduser(os.environ["ITRACK_ROOT"])))
    else:
        raw_root = Path.cwd()

    if raw_root.exists() and raw_root.is_dir():
        resolved_root = raw_root.resolve()
        if (raw_root / ".git").exists() or (resolved_root / ".git").exists():
            return resolved_root if (resolved_root / ".git").exists() else raw_root
    else:
        resolved_root = raw_root

    toplevel_hint = detect_git_toplevel_hint(raw_root)
    base_msg = (
        f"Error [Law 2 - Git-Root Check]: '{raw_root}' is not a Git repository root "
        f"(.git not found). Run from the root of a Git repository."
    )
    full_msg = f"{base_msg} Hint: cd {toplevel_hint}" if toplevel_hint else base_msg

    if json_mode:
        payload: dict[str, Any] = {
            "error": "not_git_root",
            "exit_code": 2,
            "message": full_msg,
            "path": str(raw_root),
        }
        if toplevel_hint:
            payload["hint_git_root"] = toplevel_hint
        emit_json(payload)
    else:
        sys.stderr.write(full_msg + "\n")
        sys.stderr.flush()
    sys.exit(2)


def ensure_workspace_initialized(repo_root: Path) -> None:
    """Exit 3 if itrack-issues/config.json is missing prior to `itrack init`."""
    cfg_path = repo_root / "itrack-issues" / "config.json"
    if not cfg_path.exists():
        raise CLIError(
            f"Workspace at '{repo_root}' is not initialized (itrack-issues/config.json missing). "
            "Run 'python itrack.py init' first.",
            error_code="not_initialized",
            exit_code=3,
        )


# ---------------------------------------------------------------------------
# Daemon Discovery, Flock-Guarded Cold-Start Spawner & HTTP Client
# ---------------------------------------------------------------------------


def get_runtime_dir(repo_root: Path) -> Path:
    return repo_root / "itrack-issues" / ".runtime"


def get_daemon_json_path(repo_root: Path) -> Path:
    return get_runtime_dir(repo_root) / "daemon.json"


def get_daemon_log_path(repo_root: Path) -> Path:
    return get_runtime_dir(repo_root) / "daemon.log"


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def read_daemon_json(repo_root: Path) -> dict[str, Any] | None:
    dpath = get_daemon_json_path(repo_root)
    if not dpath.exists():
        return None
    try:
        data = json.loads(dpath.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("base_url"):
            return data
    except Exception:
        return None
    return None


def probe_daemon(repo_root: Path) -> tuple[str, dict[str, Any]] | None:
    """Verify daemon.json PID liveness and /api/v1/health matching repo_root."""
    meta = read_daemon_json(repo_root)
    if not meta:
        return None
    pid = int(meta.get("pid") or 0)
    if pid > 0 and not is_pid_alive(pid):
        return None
    base_url = str(meta["base_url"]).rstrip("/")
    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            resp = client.get(f"{base_url}/api/v1/health")
            if resp.status_code != 200:
                return None
            data = resp.json()
            remote_root = data.get("repo_root")
            if not remote_root or Path(remote_root).resolve() != repo_root.resolve():
                return None
            return base_url, data
    except Exception:
        return None


def locate_server_script() -> Path:
    override = os.environ.get("ITRACK_SERVER_SCRIPT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "itrack-server.py"


def read_log_tail(repo_root: Path, lines: int = 30) -> str:
    log_path = get_daemon_log_path(repo_root)
    if not log_path.exists():
        return ""
    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(all_lines[-max(1, lines):])
    except Exception:
        return ""


def spawn_daemon(repo_root: Path, requested_port: int = 0) -> tuple[str, dict[str, Any]]:
    """Spawn `itrack-server.py` under exclusive flock on `.runtime/daemon-spawn.lock`."""
    if requested_port < 0 or requested_port > 65535:
        raise CLIError(f"Invalid port '{requested_port}': must be 0..65535.", "invalid_port", 1)

    server_script = locate_server_script()
    if not server_script.exists():
        raise CLIError(
            f"Server script not found at '{server_script}'.",
            error_code="server_script_missing",
            exit_code=1,
        )

    rdir = get_runtime_dir(repo_root)
    rdir.mkdir(parents=True, exist_ok=True)
    spawn_lock_path = rdir / "daemon-spawn.lock"

    with open(spawn_lock_path, "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            probed = probe_daemon(repo_root)
            if probed is not None:
                cur_port = int(probed[1].get("port") or urllib.parse.urlparse(probed[0]).port or 0)
                if requested_port == 0 or cur_port == requested_port:
                    return probed
                stop_daemon(repo_root)

            # Recover stale daemon.json if present
            djson = get_daemon_json_path(repo_root)
            if djson.exists():
                try:
                    djson.unlink()
                except OSError:
                    pass

            log_path = get_daemon_log_path(repo_root)
            cmd = [
                sys.executable,
                str(server_script),
                "--root",
                str(repo_root),
                "--host",
                "127.0.0.1",
                "--port",
                str(requested_port),
            ]
            with open(log_path, "a", encoding="utf-8") as log_f:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_f,
                    stderr=log_f,
                    start_new_session=True,
                    close_fds=True,
                    cwd=str(repo_root),
                )

            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                probed = probe_daemon(repo_root)
                if probed is not None:
                    return probed
                rc = proc.poll()
                if rc is not None:
                    tail = read_log_tail(repo_root, 25)
                    msg = f"itrack-server.py exited prematurely with code {rc}."
                    if tail:
                        msg = f"{msg}\n{tail}"
                    raise CLIError(msg, error_code="daemon_spawn_failed", exit_code=1)
                time.sleep(0.04)

            tail = read_log_tail(repo_root, 25)
            msg = "Timed out waiting for itrack-server.py to become healthy."
            if tail:
                msg = f"{msg}\n{tail}"
            raise CLIError(msg, error_code="daemon_timeout", exit_code=3)
        finally:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def ensure_daemon(repo_root: Path, requested_port: int = 0) -> tuple[str, dict[str, Any]]:
    probed = probe_daemon(repo_root)
    if probed is not None:
        cur_port = int(probed[1].get("port") or urllib.parse.urlparse(probed[0]).port or 0)
        if requested_port == 0 or cur_port == requested_port:
            return probed
    return spawn_daemon(repo_root, requested_port=requested_port)


def stop_daemon(repo_root: Path) -> dict[str, Any]:
    """Gracefully stop daemon via POST /api/v1/shutdown + SIGTERM fallback + stale cleanup."""
    meta = read_daemon_json(repo_root)
    probed = probe_daemon(repo_root)
    djson = get_daemon_json_path(repo_root)

    if probed is None and meta is None:
        return {"status": "not_running", "stopped": False, "stale_cleaned": False, "repo_root": str(repo_root)}

    pid = int((probed[1].get("pid") if probed else (meta or {}).get("pid")) or 0)
    base_url = probed[0] if probed else str((meta or {}).get("base_url", "")).rstrip("/")
    was_stale = probed is None and meta is not None

    if probed is not None and base_url:
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                client.post(f"{base_url}/api/v1/shutdown")
        except Exception:
            pass

    deadline = time.monotonic() + 3.5
    while time.monotonic() < deadline:
        if (not pid or not is_pid_alive(pid)) and probe_daemon(repo_root) is None:
            break
        time.sleep(0.04)

    if pid and is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        term_deadline = time.monotonic() + 2.0
        while time.monotonic() < term_deadline and is_pid_alive(pid):
            time.sleep(0.04)

    if djson.exists():
        try:
            djson.unlink()
        except OSError:
            pass

    return {
        "status": "stopped" if not was_stale else "stale_cleaned",
        "stopped": not was_stale,
        "stale_cleaned": was_stale,
        "pid": pid or None,
        "repo_root": str(repo_root),
    }


class ITrackClient:
    """Proxy-safe httpx client translating HTTP error codes to strict CLI exit codes."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ITrackClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _raise_api_error(self, resp: httpx.Response) -> NoReturn:
        detail: Any = None
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or payload
            else:
                detail = payload
        except Exception:
            detail = resp.text.strip() or f"HTTP {resp.status_code}"

        msg = json.dumps(detail) if isinstance(detail, (dict, list)) else str(detail)
        code_map = {
            400: "bad_request",
            404: "not_found",
            409: "conflict",
            422: "validation_error",
            503: "not_initialized",
        }
        raise CLIError(
            msg,
            error_code=code_map.get(resp.status_code, "api_error"),
            exit_code=http_status_to_exit_code(resp.status_code),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str, Any, str]]] | None = None,
    ) -> Any:
        clean_params = (
            {k: v for k, v in params.items() if v is not None}
            if params is not None
            else None
        )
        try:
            resp = self._client.request(
                method,
                path,
                params=clean_params,
                json=json_body,
                data=data,
                files=files,
            )
        except httpx.HTTPError as exc:
            raise CLIError(
                f"Failed communicating with itrack daemon ({self.base_url}): {exc}",
                error_code="connection_error",
                exit_code=3,
            ) from exc

        if resp.status_code >= 400:
            self._raise_api_error(resp)
        if resp.status_code == 204 or not resp.content:
            return {"ok": True}
        return resp.json()

    def download_bytes(self, path: str) -> tuple[bytes, httpx.Headers]:
        try:
            resp = self._client.get(path)
        except httpx.HTTPError as exc:
            raise CLIError(f"Download failed: {exc}", "connection_error", 3) from exc
        if resp.status_code >= 400:
            self._raise_api_error(resp)
        return resp.content, resp.headers


def validate_upload_paths(paths: list[str] | None, label: str = "File") -> list[Path]:
    if not paths:
        return []
    resolved: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists() or not p.is_file():
            raise CLIError(f"{label} not found: '{raw}'", "file_not_found", 1)
        resolved.append(p)
    return resolved


def open_multipart_files(
    field_specs: list[tuple[str, list[Path]]],
) -> tuple[list[tuple[str, tuple[str, Any, str]]], list[Any]]:
    multipart: list[tuple[str, tuple[str, Any, str]]] = []
    handles: list[Any] = []
    for field_name, paths in field_specs:
        for p in paths:
            mime, _ = mimetypes.guess_type(p.name)
            fh = open(p, "rb")
            handles.append(fh)
            multipart.append((field_name, (p.name, fh, mime or "application/octet-stream")))
    return multipart, handles


# ---------------------------------------------------------------------------
# Rich TUI Renderers (Tables, Panels, Progress Bars & Deleted Attachment UI)
# ---------------------------------------------------------------------------


def fmt_state(state: str | None) -> str:
    s = state or "-"
    st = STATE_STYLES.get(s, "white")
    return f"[{st}]{s}[/{st}]"


def fmt_priority(prio: str | None) -> str:
    p = prio or "-"
    st = PRIORITY_STYLES.get(p, "white")
    return f"[{st}]{p}[/{st}]"


def fmt_severity(sev: str | None) -> str:
    s = sev or "-"
    st = SEVERITY_STYLES.get(s, "white")
    return f"[{st}]{s}[/{st}]"


def make_bar(completed: int | float, total: int | float, width: int = 16) -> str:
    if total <= 0:
        return "[dim]" + ("░" * width) + "[/dim] 0%"
    ratio = max(0.0, min(1.0, float(completed) / float(total)))
    filled = int(round(ratio * width))
    pct = (float(completed) / float(total)) * 100.0
    color = "green" if pct <= 100.0 else "red"
    bar = f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (width - filled)}[/dim]"
    return f"{bar} {pct:.0f}%"


def render_time_bar(logged_minutes: int, estimate_minutes: int | None) -> str:
    if estimate_minutes and estimate_minutes > 0:
        return f"{make_bar(logged_minutes, estimate_minutes)} ({logged_minutes}m / {estimate_minutes}m)"
    return f"{logged_minutes}m logged (no estimate)"


def render_issues_table(data: dict[str, Any], title: str = "Issues") -> None:
    items = data.get("issues") if "issues" in data else (data.get("items") or data.get("results") or [])
    total = data.get("total", len(items))
    page = data.get("page", 1)
    total_pages = data.get("total_pages") or max(1, (int(total) + int(data.get("page_size", max(1, len(items) or 25))) - 1) // int(data.get("page_size", max(1, len(items) or 25))))

    t = Table(
        title=f"{title} ({len(items)} shown / {total} total — Page {page}/{total_pages})",
        box=box.ROUNDED,
    )
    t.add_column("Key", style="bold cyan", no_wrap=True)
    t.add_column("Category", style="magenta")
    t.add_column("State", no_wrap=True)
    t.add_column("Prio", no_wrap=True)
    t.add_column("Sev", no_wrap=True)
    t.add_column("Name", style="bold")
    t.add_column("Assignee", style="green")
    t.add_column("Due / Flags", style="yellow")

    for iss in items:
        flags: list[str] = []
        if iss.get("due_date"):
            flags.append(str(iss["due_date"]))
        if iss.get("is_overdue"):
            flags.append("[bold red]OVERDUE[/bold red]")
        if iss.get("is_blocked"):
            flags.append("[bold red]BLOCKED[/bold red]")
        if iss.get("archived"):
            flags.append("[dim]ARCHIVED[/dim]")
        t.add_row(
            str(iss.get("key", "")),
            str(iss.get("category", "")),
            fmt_state(iss.get("state")),
            fmt_priority(iss.get("priority")),
            fmt_severity(iss.get("severity")),
            str(iss.get("name", "")),
            str(iss.get("assignee") or "-"),
            " ".join(flags) if flags else "-",
        )
    stdout_console.print(t)


def render_comment_panel(
    cmt: dict[str, Any],
    deleted_map: dict[str, Any] | None = None,
    issue_att_map: dict[str, Any] | None = None,
) -> Panel:
    edits = cmt.get("edit_history") or []
    edited_badge = f" [dim](edited {len(edits)}x)[/dim]" if edits else ""
    header = (
        f"[bold cyan]{cmt.get('id')}[/bold cyan] by [bold green]{cmt.get('author') or 'user'}[/bold green] "
        f"at [dim]{cmt.get('created_at', '')}[/dim]{edited_badge}"
    )
    lines: list[str] = [cmt.get("body") or "[dim](empty comment)[/dim]"]

    embeds = cmt.get("embeds") or []
    if embeds:
        emb_str = ", ".join(f"{e.get('id')} ({e.get('filename')})" for e in embeds)
        lines.append(f"\n[magenta]Inline Embeds:[/magenta] {emb_str}")

    atts = cmt.get("attachments") or []
    if atts:
        att_str = ", ".join(f"{a.get('id')} ({a.get('filename')}, {a.get('size_bytes', 0)}B)" for a in atts)
        lines.append(f"[cyan]Comment Attachments:[/cyan] {att_str}")

    resolved_links = cmt.get("linked_attachments")
    linked_ids = cmt.get("linked_attachment_ids") or []
    if resolved_links:
        parts: list[str] = []
        for la in resolved_links:
            if la.get("deleted"):
                parts.append(f"[strike red]\\[deleted attachment: {la.get('id')} ({la.get('filename') or 'removed'})][/strike red]")
            else:
                parts.append(f"{la.get('id')} ({la.get('filename')})")
        lines.append("[yellow]Linked Issue Attachments:[/yellow] " + ", ".join(parts))
    elif linked_ids:
        parts = []
        dmap = deleted_map or {}
        amap = issue_att_map or {}
        for aid in linked_ids:
            if aid in dmap or (amap and aid not in amap):
                dinfo = dmap.get(aid) or {}
                parts.append(f"[strike red]\\[deleted attachment: {aid} ({dinfo.get('filename') or 'removed'})][/strike red]")
            else:
                ainfo = amap.get(aid) or {}
                parts.append(f"{aid} ({ainfo.get('filename', aid)})")
        lines.append("[yellow]Linked Issue Attachments:[/yellow] " + ", ".join(parts))

    return Panel("\n".join(lines), title=header, border_style="blue")


def render_issue_detail(issue: dict[str, Any]) -> None:
    badges: list[str] = [
        f"[bold magenta]{issue.get('category', 'task')}[/bold magenta]",
        fmt_state(issue.get("state")),
        f"prio:{fmt_priority(issue.get('priority'))}",
        f"sev:{fmt_severity(issue.get('severity'))}",
    ]
    if issue.get("resolution"):
        badges.append(f"[green]res:{issue['resolution']}[/green]")
    if issue.get("archived"):
        badges.append("[dim]ARCHIVED[/dim]")
    if issue.get("is_overdue"):
        badges.append("[bold white on red] OVERDUE [/bold white on red]")
    if issue.get("is_blocked"):
        blockers = ", ".join(issue.get("unresolved_blockers") or [])
        badges.append(f"[bold white on red] BLOCKED{f' ({blockers})' if blockers else ''} [/bold white on red]")

    logged = int(issue.get("logged_minutes") or 0)
    est = issue.get("estimate_minutes")
    tags_str = ", ".join(issue.get("tags") or []) or "-"
    comps_str = ", ".join(issue.get("components") or []) or "-"
    cfields = issue.get("custom_fields") or {}
    cfields_str = ", ".join(f"{k}={v}" for k, v in cfields.items()) if cfields else "-"

    meta_lines = [
        "  ".join(badges),
        f"[bold]Folder:[/bold] issues/{issue.get('folder') or issue.get('name')}",
        f"[bold]Assignee:[/bold] {issue.get('assignee') or '-'}   "
        f"[bold]Reporter:[/bold] {issue.get('reporter') or '-'}   "
        f"[bold]Parent:[/bold] {issue.get('parent') or '-'}",
        f"[bold]Sprint:[/bold] {issue.get('sprint') or '-'}   "
        f"[bold]Milestone:[/bold] {issue.get('milestone') or '-'}   "
        f"[bold]Points:[/bold] {issue.get('story_points') if issue.get('story_points') is not None else '-'}   "
        f"[bold]Due:[/bold] {issue.get('due_date') or '-'}",
        f"[bold]Tags:[/bold] {tags_str}   [bold]Components:[/bold] {comps_str}",
        f"[bold]Custom Fields:[/bold] {cfields_str}",
        f"[bold]Time Tracking:[/bold] {render_time_bar(logged, est)}",
        f"[dim]Created: {issue.get('created_at', '-')}  |  Updated: {issue.get('updated_at', '-')}[/dim]",
    ]
    stdout_console.print(
        Panel(
            "\n".join(meta_lines),
            title=f"[bold cyan]{issue.get('key')}[/bold cyan] — [bold]{issue.get('name')}[/bold]",
            border_style="cyan",
        )
    )

    desc = issue.get("description") or ""
    desc_embeds = issue.get("description_embeds") or []
    if desc or desc_embeds:
        body_str = desc if desc else "[dim](no text)[/dim]"
        if desc_embeds:
            emb_list = ", ".join(f"{e.get('id')} ({e.get('filename')})" for e in desc_embeds)
            body_str += f"\n\n[magenta]Description Embeds:[/magenta] {emb_list}"
        stdout_console.print(Panel(body_str, title="Description", border_style="dim"))

    acs = issue.get("acceptance_criteria") or []
    if acs:
        ac_prog = issue.get("ac_progress") or {
            "checked": sum(1 for a in acs if a.get("checked")),
            "total": len(acs),
        }
        ac_lines = [
            f"{make_bar(ac_prog.get('checked', 0), ac_prog.get('total', len(acs)))} "
            f"({ac_prog.get('checked', 0)}/{ac_prog.get('total', len(acs))} checked)"
        ]
        for idx, item in enumerate(acs, 1):
            mark = "[bold green]\\[x][/bold green]" if item.get("checked") else "[yellow]\\[ ][/yellow]"
            ac_lines.append(f"{idx}. {mark} [cyan]{item.get('id', f'AC-{idx}')}[/cyan]: {item.get('text', '')}")
        stdout_console.print(Panel("\n".join(ac_lines), title="Acceptance Criteria", border_style="green"))

    subtasks = issue.get("subtasks") or []
    if subtasks:
        sp = issue.get("subtask_progress") or {
            "completed": sum(1 for s in subtasks if s.get("completed") or s.get("state") == "done"),
            "total": len(subtasks),
        }
        st_lines = [
            f"{make_bar(sp.get('completed', 0), sp.get('total', len(subtasks)))} "
            f"({sp.get('completed', 0)}/{sp.get('total', len(subtasks))} completed)"
        ]
        for s in subtasks:
            st_lines.append(f"• [cyan]{s.get('key')}[/cyan] {fmt_state(s.get('state'))} — {s.get('name')}")
        stdout_console.print(Panel("\n".join(st_lines), title="Subtasks", border_style="magenta"))

    links = issue.get("links") or []
    if links:
        lt = Table(title="Linked Issues", box=box.SIMPLE)
        lt.add_column("ID", style="cyan")
        lt.add_column("Type", style="yellow")
        lt.add_column("Direction", style="dim")
        lt.add_column("Target", style="bold")
        for lnk in links:
            lt.add_row(
                str(lnk.get("id", "")),
                str(lnk.get("type", "")),
                str(lnk.get("direction", "outbound")),
                f"{lnk.get('target_key')} ({lnk.get('target_name', '')})",
            )
        stdout_console.print(lt)

    atts = issue.get("attachments") or []
    if atts:
        at = Table(title="Issue Attachments", box=box.SIMPLE)
        at.add_column("ID", style="cyan")
        at.add_column("Filename", style="bold")
        at.add_column("MIME", style="dim")
        at.add_column("Size", justify="right")
        for a in atts:
            at.add_row(
                str(a.get("id", "")),
                str(a.get("filename", "")),
                str(a.get("mime_type", "")),
                f"{a.get('size_bytes', 0)} B",
            )
        stdout_console.print(at)

    comments = issue.get("comments") or []
    if comments:
        deleted_map = issue.get("deleted_attachments") or {}
        att_map = {a.get("id"): a for a in atts if a.get("id")}
        for cmt in comments:
            stdout_console.print(render_comment_panel(cmt, deleted_map=deleted_map, issue_att_map=att_map))


# ---------------------------------------------------------------------------
# Workspace, Config, Server, UI, Stats, Tree, Search, Board & History
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def cmd_init(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    prefix_arg: str | None = None
    if args.prefix is not None:
        p = args.prefix.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", p):
            raise CLIError(
                f"Invalid project prefix '{args.prefix}': expected 2..10 alphanumeric chars starting with a letter.",
                error_code="invalid_prefix",
                exit_code=1,
            )
        prefix_arg = p

    title_arg: str | None = None
    if args.title is not None:
        t = args.title.strip()
        if not t or len(t) > 120:
            raise CLIError("Invalid project title: expected 1..120 non-whitespace characters.", "invalid_title", 1)
        title_arg = t

    issues_root = repo_root / "itrack-issues"
    runtime_dir = issues_root / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (issues_root / "issues").mkdir(parents=True, exist_ok=True)

    lock_path = runtime_dir / "storage.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            gitignore_path = issues_root / ".gitignore"
            if not gitignore_path.exists():
                gitignore_path.write_text(".runtime/\nindex.sqlite*\n*.tmp\n", encoding="utf-8")

            for fname, key in (
                ("tags.json", "tags"),
                ("sprints.json", "sprints"),
                ("milestones.json", "milestones"),
                ("views.json", "views"),
            ):
                fpath = issues_root / fname
                if not fpath.exists():
                    _atomic_write_json(fpath, {key: []})

            cfg_path = issues_root / "config.json"
            already_initialized = cfg_path.exists()
            if not already_initialized:
                ts = now_iso()
                default_cfg = {
                    "version": VERSION,
                    "project_prefix": prefix_arg or "ITR",
                    "project_title": title_arg or "iTrack Workspace",
                    "default_category": "task",
                    "default_state": "backlog",
                    "default_priority": "medium",
                    "default_severity": "sev3",
                    "wip_limits": {},
                    "preferences": {
                        "theme": "dark",
                        "default_swimlane": "none",
                        "page_size": 25,
                    },
                    "sequences": {
                        "issue": 0,
                        "comment": 0,
                        "attachment": 0,
                        "link": 0,
                        "worklog": 0,
                        "history": 0,
                        "sprint": 0,
                        "milestone": 0,
                        "view": 0,
                    },
                    "created_at": ts,
                    "updated_at": ts,
                }
                _atomic_write_json(cfg_path, default_cfg)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    base_url, health = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if already_initialized and (prefix_arg is not None or title_arg is not None):
            updates: dict[str, Any] = {}
            if prefix_arg is not None:
                updates["project_prefix"] = prefix_arg
            if title_arg is not None:
                updates["project_title"] = title_arg
            cfg = client.request("PATCH", "/api/v1/config", json_body=updates)
        else:
            cfg = client.request("GET", "/api/v1/config")

    result = {
        "initialized": True,
        "already_initialized": already_initialized,
        "repo_root": str(repo_root),
        "storage_dir": str(issues_root),
        "base_url": base_url,
        "pid": health.get("pid"),
        "config": cfg,
    }
    if json_mode:
        emit_json(result)
    else:
        stdout_console.print(
            Panel(
                f"Workspace: [bold cyan]{cfg.get('project_title', 'iTrack Workspace')}[/bold cyan] "
                f"([bold]{cfg.get('project_prefix', 'ITR')}[/bold])\n"
                f"Storage:   {issues_root}\n"
                f"Daemon:    {base_url} (PID {health.get('pid')})",
                title="itrack Initialized" if not already_initialized else "itrack Workspace Ready",
                border_style="green",
            )
        )


def cmd_status(_args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, health = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/status")
    if isinstance(data, dict):
        data.setdefault("base_url", base_url)
        data.setdefault("daemon", health)
    if json_mode:
        emit_json(data)
    else:
        stdout_console.print(
            Panel(
                f"Project: [bold cyan]{data.get('project_title', '-')}[/bold cyan] ({data.get('project_prefix', 'ITR')})\n"
                f"Root:    {data.get('repo_root', repo_root)}\n"
                f"Daemon:  {base_url} (PID {health.get('pid')})\n"
                f"Issues:  [bold]{data.get('total_issues', data.get('issue_count', 0))}[/bold]   "
                f"Sprints: {data.get('sprint_count', 0)}   "
                f"Milestones: {data.get('milestone_count', 0)}   "
                f"Tags: {data.get('tag_count', 0)}",
                title="Workspace Status",
                border_style="cyan",
            )
        )


def cmd_reindex(_args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        data = client.request("POST", "/api/v1/reindex")
    if json_mode:
        emit_json(data)
    else:
        stdout_console.print(
            f"[bold green]Reindexed workspace[/bold green] "
            f"({data.get('indexed_issues', data.get('total_issues', 0))} issues)."
        )


def _parse_config_value(key: str, raw_val: str) -> Any:
    s = raw_val.strip()
    if s.startswith(("{", "[")):
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise CLIError(f"Invalid JSON value for config key '{key}': {exc}", "invalid_config_value", 1) from exc
    if key in ("page_size", "preferences.page_size") or key.startswith("wip_limits."):
        try:
            return int(s)
        except ValueError as exc:
            raise CLIError(f"Expected integer for config key '{key}', got '{raw_val}'.", "invalid_config_value", 1) from exc
    if s.lower() in ("true", "false") and key not in ("project_title", "project_prefix"):
        return s.lower() == "true"
    if s.lower() in ("null", "none") and key.startswith("wip_limits."):
        return None
    return raw_val


def cmd_config(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if args.config_action == "show":
            cfg = client.request("GET", "/api/v1/config")
            if json_mode:
                emit_json(cfg)
            else:
                stdout_console.print(Panel(json.dumps(cfg, indent=2), title="Workspace Configuration", border_style="cyan"))
            return

        if args.config_action == "set":
            val = _parse_config_value(args.key, args.value)
            cfg = client.request("PATCH", "/api/v1/config", json_body={"key": args.key, "value": val})
            if json_mode:
                emit_json(cfg)
            else:
                stdout_console.print(f"[bold green]Updated config[/bold green] `{args.key}`.")
            return


def cmd_server(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.server_action
    if act == "status":
        meta = read_daemon_json(repo_root)
        probed = probe_daemon(repo_root)
        if probed is not None:
            base_url, health = probed
            res = {"running": True, "base_url": base_url, "stale_recovered": False, **health}
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Server running[/bold green] at [cyan]{base_url}[/cyan] (PID {health.get('pid')})"
                )
            return
        stale_cleaned = False
        djson = get_daemon_json_path(repo_root)
        if meta is not None and djson.exists():
            try:
                djson.unlink()
                stale_cleaned = True
            except OSError:
                pass
        res = {"running": False, "stale_recovered": stale_cleaned, "repo_root": str(repo_root)}
        if json_mode:
            emit_json(res)
        else:
            note = " (cleaned stale daemon.json)" if stale_cleaned else ""
            stdout_console.print(f"[yellow]Server stopped[/yellow]{note}")
        return

    if act == "start":
        base_url, health = ensure_daemon(repo_root, requested_port=args.port)
        res = {"running": True, "base_url": base_url, **health}
        if json_mode:
            emit_json(res)
        else:
            stdout_console.print(f"[bold green]Server active[/bold green] at [cyan]{base_url}[/cyan] (PID {health.get('pid')})")
        return

    if act == "stop":
        res = stop_daemon(repo_root)
        if json_mode:
            emit_json(res)
        else:
            stdout_console.print(f"[yellow]Server {res.get('status')}[/yellow].")
        return

    if act == "restart":
        stop_daemon(repo_root)
        base_url, health = spawn_daemon(repo_root, requested_port=args.port)
        res = {"running": True, "restarted": True, "base_url": base_url, **health}
        if json_mode:
            emit_json(res)
        else:
            stdout_console.print(
                f"[bold green]Server restarted[/bold green] at [cyan]{base_url}[/cyan] (PID {health.get('pid')})"
            )
        return


def cmd_ui(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    if args.port < 0 or args.port > 65535:
        raise CLIError(f"Invalid port '{args.port}': must be 0..65535.", "invalid_port", 1)

    if args.foreground:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        stop_daemon(repo_root)
        server_script = locate_server_script()
        cmd = [
            sys.executable,
            str(server_script),
            "--root",
            str(repo_root),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ]
        proc = subprocess.Popen(cmd, cwd=str(repo_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 12.0
            probed = None
            while time.monotonic() < deadline:
                probed = probe_daemon(repo_root)
                if probed is not None:
                    break
                if proc.poll() is not None:
                    raise CLIError("Foreground server exited prematurely.", "daemon_spawn_failed", 1)
                time.sleep(0.05)
            if probed is None:
                raise CLIError("Timed out starting foreground UI server.", "daemon_timeout", 3)
            base_url, health = probed
            opened = False
            if not args.no_browser:
                try:
                    opened = bool(webbrowser.open(base_url))
                except Exception:
                    opened = False
            if json_mode:
                emit_json({"url": base_url, "base_url": base_url, "pid": health.get("pid"), "foreground": True, "browser_opened": opened})
            else:
                stdout_console.print(f"itrack Web UI running in foreground at {base_url} (Ctrl+C to stop)")
                sys.stdout.flush()
            proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            stop_daemon(repo_root)
            if proc.poll() is None:
                proc.terminate()
        return

    base_url, health = ensure_daemon(repo_root, requested_port=args.port)
    # Verify live HTTP readiness of SPA root & health
    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r = hc.get(f"{base_url}/")
        if r.status_code >= 400:
            raise CLIError(f"UI endpoint returned HTTP {r.status_code}", "ui_error", 1)

    opened = False
    if not args.no_browser:
        try:
            opened = bool(webbrowser.open(base_url))
        except Exception:
            opened = False

    payload = {
        "url": base_url,
        "base_url": base_url,
        "port": health.get("port") or urllib.parse.urlparse(base_url).port,
        "pid": health.get("pid"),
        "browser_opened": opened,
    }
    if json_mode:
        emit_json(payload)
    else:
        stdout_console.print(f"itrack Web UI ready at [bold cyan]{base_url}[/bold cyan]")


def cmd_stats(_args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/stats")
    if json_mode:
        emit_json(data)
        return

    by_state = data.get("by_state") or {}
    by_cat = data.get("by_category") or {}
    by_prio = data.get("by_priority") or {}
    in_prog_count = data.get(
        "in_progress_issues",
        sum(int(by_state.get(s, 0)) for s in ("in-progress", "in-review", "in-qa")),
    )
    completed_count = data.get("completed_issues", data.get("done_issues", int(by_state.get("done", 0))))
    open_count = data.get(
        "open_issues",
        sum(int(by_state.get(s, 0)) for s in ("triage", "backlog", "todo", "ready", "on-hold", "blocked")),
    )
    summary = (
        f"Total Issues: [bold]{data.get('total_issues', 0)}[/bold]   "
        f"Open: [cyan]{open_count}[/cyan]   "
        f"In Progress: [yellow]{in_prog_count}[/yellow]   "
        f"Completed: [green]{completed_count}[/green]   "
        f"Blocked: [red]{data.get('blocked_issues', 0)}[/red]   "
        f"Overdue: [red]{data.get('overdue_issues', 0)}[/red]\n"
        f"Story Points: {data.get('completed_points', 0)}/{data.get('total_points', 0)}   "
        f"Time: {data.get('total_logged_minutes', 0)}m logged / {data.get('total_estimate_minutes', 0)}m est"
    )
    stdout_console.print(Panel(summary, title="Analytics Dashboard", border_style="cyan"))

    if by_state:
        st_table = Table(title="Issues by State", box=box.SIMPLE)
        st_table.add_column("State")
        st_table.add_column("Count", justify="right")
        for k, v in by_state.items():
            st_table.add_row(fmt_state(k), str(v))
        stdout_console.print(st_table)

    if by_cat or by_prio:
        cp_table = Table(title="Category & Priority Breakdown", box=box.SIMPLE)
        cp_table.add_column("Dimension")
        cp_table.add_column("Value")
        cp_table.add_column("Count", justify="right")
        for k, v in by_cat.items():
            cp_table.add_row("category", str(k), str(v))
        for k, v in by_prio.items():
            cp_table.add_row("priority", fmt_priority(k), str(v))
        stdout_console.print(cp_table)


def _populate_rich_tree(parent_node: Tree, nodes: list[dict[str, Any]]) -> None:
    for n in nodes:
        blk = " [bold red]\\[BLOCKED][/bold red]" if n.get("is_blocked") else ""
        label = (
            f"[bold cyan]{n.get('key')}[/bold cyan] {fmt_state(n.get('state'))} "
            f"[magenta]({n.get('category', 'task')})[/magenta] — {n.get('name')}{blk}"
        )
        branch = parent_node.add(label)
        children = n.get("children") or n.get("subtasks") or []
        if children:
            _populate_rich_tree(branch, children)


def cmd_tree(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    params = {"root": normalize_ref_str(args.issue)} if args.issue else None
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/tree", params=params)
    if json_mode:
        emit_json(data)
        return

    if isinstance(data, dict):
        roots = data.get("trees") or data.get("roots") or data.get("tree") or []
        if not roots and data.get("key"):
            roots = [data]
    elif isinstance(data, list):
        roots = data
    else:
        roots = []
    rich_tree = Tree("[bold cyan]Issue Hierarchy & Dependency Tree[/bold cyan]")
    _populate_rich_tree(rich_tree, roots)
    stdout_console.print(rich_tree)


def cmd_search(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    params: dict[str, Any] = {
        "page": args.page,
        "page_size": args.page_size,
        "sort": args.sort,
        "order": args.order.lower() if args.order else None,
    }
    if args.iql:
        params["iql"] = args.query
    else:
        params["q"] = args.query
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/search", params=params)
    if json_mode:
        emit_json(data)
    else:
        render_issues_table(data, title=f"Search Results: {args.query}")


def cmd_board(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    swimlane = args.swimlane.lower() if args.swimlane else "none"
    if swimlane not in SWIMLANES:
        raise CLIError(f"Invalid swimlane '{args.swimlane}'. Expected one of: {', '.join(SWIMLANES)}", "invalid_swimlane", 1)
    base_url, _ = ensure_daemon(repo_root)
    params = {
        "swimlane": swimlane,
        "category": normalize_hyphen_enum(args.category) if args.category else None,
        "priority": normalize_hyphen_enum(args.priority) if args.priority else None,
        "assignee": args.assignee,
        "sprint": args.sprint,
        "tag": args.tag,
        "q": args.query,
    }
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/board", params=params)
    if json_mode:
        emit_json(data)
        return

    lanes = data.get("swimlanes") or data.get("lanes") or []
    wip_limits = data.get("wip_limits") or {}
    if not lanes and "columns" in data:
        lanes = [{"name": "All Issues", "columns": data["columns"]}]

    for lane in lanes:
        lane_title = lane.get("name") or lane.get("key") or "Board"
        cols = lane.get("columns") or {}
        t = Table(title=f"Kanban Board — Swimlane: {lane_title} ({swimlane})", box=box.ROUNDED)
        col_items_list: list[tuple[str, list[dict[str, Any]]]] = []
        if isinstance(cols, list):
            for c in cols:
                st = str(c.get("state", ""))
                col_items_list.append((st, c.get("issues") or []))
        elif isinstance(cols, dict):
            for st, items in cols.items():
                col_items_list.append((str(st), items if isinstance(items, list) else (items.get("issues") or [])))

        for st, items in col_items_list:
            lim = wip_limits.get(st)
            wip_badge = f" [WIP {len(items)}/{lim}]" if lim else f" ({len(items)})"
            if lim and len(items) > int(lim):
                wip_badge = f" [bold red][WIP {len(items)}/{lim}!][/bold red]"
            t.add_column(f"{st}{wip_badge}")

        max_rows = max((len(items) for _, items in col_items_list), default=0)
        for idx in range(max_rows):
            row: list[str] = []
            for _, items in col_items_list:
                if idx < len(items):
                    card = items[idx]
                    row.append(f"[bold cyan]{card.get('key')}[/bold cyan] {card.get('name')}\n{fmt_priority(card.get('priority'))} {card.get('assignee') or ''}".strip())
                else:
                    row.append("")
            t.add_row(*row)
        stdout_console.print(t)


def cmd_history(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    ref = encode_ref(args.issue)
    with ITrackClient(base_url) as client:
        data = client.request("GET", f"/api/v1/issues/{ref}/history")
    if json_mode:
        emit_json(data)
        return

    entries = data.get("history") if isinstance(data, dict) else data
    entries = entries or []
    t = Table(title=f"History for {args.issue} ({len(entries)} events)", box=box.ROUNDED)
    t.add_column("ID", style="cyan")
    t.add_column("Timestamp", style="dim")
    t.add_column("Actor", style="green")
    t.add_column("Action", style="yellow")
    t.add_column("Diffs")
    for h in entries:
        diffs = h.get("diffs") or []
        diff_str = "; ".join(f"{d.get('field')}: {d.get('old')} -> {d.get('new')}" for d in diffs) or "-"
        t.add_row(
            str(h.get("id", "")),
            str(h.get("timestamp", "")),
            str(h.get("actor", "")),
            str(h.get("action", "")),
            diff_str,
        )
    stdout_console.print(t)


# ---------------------------------------------------------------------------
# Issue, Comment, Attachment, Link, Agile, View, Worklog & Bulk Handlers
# ---------------------------------------------------------------------------


def _resolve_text_source(
    inline_text: str | None,
    file_path: str | None,
    use_stdin: bool,
    label: str = "text",
) -> str | None:
    sources = sum([inline_text is not None, file_path is not None, bool(use_stdin)])
    if sources > 1:
        raise CLIError(f"Specify at most one {label} input flag.", "conflicting_input_flags", 1)
    if inline_text is not None:
        return inline_text
    if file_path is not None:
        p = Path(file_path).expanduser()
        if not p.exists() or not p.is_file():
            raise CLIError(f"{label.capitalize()} file not found: '{file_path}'", "file_not_found", 1)
        return p.read_text(encoding="utf-8")
    if use_stdin:
        return sys.stdin.read()
    return None


def _parse_kv_fields(items: list[str] | None) -> dict[str, str]:
    if not items:
        return {}
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise CLIError(f"Invalid --field '{raw}': expected KEY=VAL format.", "invalid_field", 1)
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k:
            raise CLIError(f"Invalid --field '{raw}': key cannot be empty.", "invalid_field", 1)
        out[k] = v
    return out


def _find_ac_index(acs: list[dict[str, Any]], token: str) -> int:
    s = token.strip()
    for idx, item in enumerate(acs):
        if str(item.get("id", "")).upper() == s.upper():
            return idx
    if re.fullmatch(r"\d+", s):
        pos = int(s) - 1
        if 0 <= pos < len(acs):
            return pos
        raise CLIError(f"Acceptance criterion index '{token}' out of range (1..{len(acs)}).", "invalid_ac_index", 1)
    raise CLIError(f"Acceptance criterion '{token}' not found.", "ac_not_found", 4)


def _next_ac_id(acs: list[dict[str, Any]]) -> str:
    max_num = 0
    for item in acs:
        m = re.fullmatch(r"AC-(\d+)", str(item.get("id", "")), re.IGNORECASE)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"AC-{max_num + 1}"


def cmd_issue(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.issue_action
    actor = default_actor(repo_root)
    base_url, _ = ensure_daemon(repo_root)

    with ITrackClient(base_url) as client:
        if act == "create":
            desc = _resolve_text_source(args.desc, args.desc_file, args.desc_stdin, label="description")
            embed_paths = validate_upload_paths(args.embed, label="Embed image")
            attach_paths = validate_upload_paths(args.attach, label="Attachment")
            cfields = _parse_kv_fields(args.field)
            tags = flatten_csv_tokens(args.tag)
            comps = flatten_csv_tokens(args.component)

            body: dict[str, Any] = {"name": args.name, "actor": actor}
            if args.category is not None:
                body["category"] = normalize_hyphen_enum(args.category)
            if args.state is not None:
                body["state"] = normalize_hyphen_enum(args.state)
            if args.priority is not None:
                body["priority"] = normalize_hyphen_enum(args.priority)
            if args.severity is not None:
                body["severity"] = normalize_hyphen_enum(args.severity)
            if args.resolution is not None:
                body["resolution"] = normalize_hyphen_enum(args.resolution)
            if desc is not None:
                body["description"] = desc
            if args.ac:
                body["acceptance_criteria"] = [
                    {"id": f"AC-{idx}", "text": txt, "checked": False}
                    for idx, txt in enumerate(args.ac, 1)
                ]
            if args.assignee is not None:
                body["assignee"] = args.assignee
            if args.reporter is not None:
                body["reporter"] = args.reporter
            if tags:
                body["tags"] = tags
            if comps:
                body["components"] = comps
            if args.parent is not None:
                body["parent"] = normalize_ref_str(args.parent)
            if args.sprint is not None:
                body["sprint"] = normalize_ref_str(args.sprint)
            if args.milestone is not None:
                body["milestone"] = normalize_ref_str(args.milestone)
            if args.points is not None:
                body["story_points"] = args.points
            if args.estimate is not None:
                body["estimate_minutes"] = args.estimate
            if args.due is not None:
                body["due_date"] = args.due
            if cfields:
                body["custom_fields"] = cfields

            issue = client.request("POST", "/api/v1/issues", json_body=body)
            ref = encode_ref(issue.get("key") or args.name)

            if embed_paths:
                mp_files, handles = open_multipart_files([("files", embed_paths)])
                try:
                    client.request(
                        "POST",
                        f"/api/v1/issues/{ref}/embeds",
                        files=[("uploaded_by", (None, actor))] + mp_files,
                    )
                finally:
                    for fh in handles:
                        fh.close()

            if attach_paths:
                mp_files, handles = open_multipart_files([("files", attach_paths)])
                try:
                    client.request(
                        "POST",
                        f"/api/v1/issues/{ref}/attachments",
                        files=[("uploaded_by", (None, actor))] + mp_files,
                    )
                finally:
                    for fh in handles:
                        fh.close()

            if embed_paths or attach_paths:
                issue = client.request("GET", f"/api/v1/issues/{ref}")

            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Created[/bold green] [bold cyan]{issue.get('key')}[/bold cyan]: "
                    f"[bold]{issue.get('name')}[/bold] ({issue.get('state')})"
                )
            return

        if act == "show":
            issue = client.request("GET", f"/api/v1/issues/{encode_ref(args.issue)}")
            if json_mode:
                emit_json(issue)
            else:
                render_issue_detail(issue)
            return

        if act == "list":
            params: dict[str, Any] = {
                "tab": normalize_hyphen_enum(args.tab) if args.tab else None,
                "iql": args.query,
                "category": normalize_hyphen_enum(args.category) if args.category else None,
                "state": normalize_hyphen_enum(args.state) if args.state else None,
                "priority": normalize_hyphen_enum(args.priority) if args.priority else None,
                "severity": normalize_hyphen_enum(args.severity) if args.severity else None,
                "resolution": normalize_hyphen_enum(args.resolution) if args.resolution else None,
                "assignee": args.assignee,
                "reporter": args.reporter,
                "tag": args.tag,
                "component": args.component,
                "parent": normalize_ref_str(args.parent) if args.parent else None,
                "sprint": normalize_ref_str(args.sprint) if args.sprint else None,
                "milestone": normalize_ref_str(args.milestone) if args.milestone else None,
                "overdue": True if args.overdue else None,
                "sort": args.sort,
                "order": args.order.lower() if args.order else None,
                "page": args.page,
                "page_size": args.page_size,
            }
            data = client.request("GET", "/api/v1/issues", params=params)
            if json_mode:
                emit_json(data)
            else:
                render_issues_table(data, title="Issues")
            return

        if act == "edit":
            if args.resolution is not None and args.clear_resolution:
                raise CLIError("Cannot specify both --resolution and --clear-resolution.", "conflicting_flags", 1)
            desc = _resolve_text_source(args.desc, args.desc_file, args.desc_stdin, label="description")
            if desc is not None and args.clear_desc:
                raise CLIError("Cannot specify both description text and --clear-desc.", "conflicting_flags", 1)
            if args.assignee is not None and args.clear_assignee:
                raise CLIError("Cannot specify both --assignee and --clear-assignee.", "conflicting_flags", 1)
            if args.reporter is not None and args.clear_reporter:
                raise CLIError("Cannot specify both --reporter and --clear-reporter.", "conflicting_flags", 1)
            if args.parent is not None and args.clear_parent:
                raise CLIError("Cannot specify both --parent and --clear-parent.", "conflicting_flags", 1)
            if args.sprint is not None and args.clear_sprint:
                raise CLIError("Cannot specify both --sprint and --clear-sprint.", "conflicting_flags", 1)
            if args.milestone is not None and args.clear_milestone:
                raise CLIError("Cannot specify both --milestone and --clear-milestone.", "conflicting_flags", 1)
            if args.points is not None and args.clear_points:
                raise CLIError("Cannot specify both --points and --clear-points.", "conflicting_flags", 1)
            if args.estimate is not None and args.clear_estimate:
                raise CLIError("Cannot specify both --estimate and --clear-estimate.", "conflicting_flags", 1)
            if args.due is not None and args.clear_due:
                raise CLIError("Cannot specify both --due and --clear-due.", "conflicting_flags", 1)

            embed_paths = validate_upload_paths(args.embed, label="Embed image")
            field_updates = _parse_kv_fields(args.field)

            cur_ref_raw = args.issue
            ref = encode_ref(cur_ref_raw)
            cur_issue: dict[str, Any] | None = None

            if args.name is not None:
                cur_issue = client.request(
                    "POST",
                    f"/api/v1/issues/{ref}/rename",
                    json_body={"new_name": args.name, "actor": actor},
                )
                cur_ref_raw = cur_issue.get("key") or args.name
                ref = encode_ref(cur_ref_raw)

            needs_current = any([
                args.add_ac, args.edit_ac, args.check_ac, args.uncheck_ac, args.remove_ac,
                args.add_tag, args.remove_tag,
                args.add_component, args.remove_component,
                field_updates, args.unset_field,
            ])
            if needs_current and cur_issue is None:
                cur_issue = client.request("GET", f"/api/v1/issues/{ref}")

            patch: dict[str, Any] = {}
            if args.category is not None:
                patch["category"] = normalize_hyphen_enum(args.category)
            if args.state is not None:
                patch["state"] = normalize_hyphen_enum(args.state)
            if args.priority is not None:
                patch["priority"] = normalize_hyphen_enum(args.priority)
            if args.severity is not None:
                patch["severity"] = normalize_hyphen_enum(args.severity)
            if args.clear_resolution:
                patch["resolution"] = None
            elif args.resolution is not None:
                patch["resolution"] = normalize_hyphen_enum(args.resolution)

            if args.clear_desc:
                patch["description"] = ""
            elif desc is not None:
                patch["description"] = desc

            if args.clear_assignee:
                patch["assignee"] = None
            elif args.assignee is not None:
                patch["assignee"] = args.assignee

            if args.clear_reporter:
                patch["reporter"] = None
            elif args.reporter is not None:
                patch["reporter"] = args.reporter

            if args.clear_parent:
                patch["parent"] = None
            elif args.parent is not None:
                patch["parent"] = normalize_ref_str(args.parent)

            if args.clear_sprint:
                patch["sprint"] = None
            elif args.sprint is not None:
                patch["sprint"] = normalize_ref_str(args.sprint)

            if args.clear_milestone:
                patch["milestone"] = None
            elif args.milestone is not None:
                patch["milestone"] = normalize_ref_str(args.milestone)

            if args.clear_points:
                patch["story_points"] = None
            elif args.points is not None:
                patch["story_points"] = args.points

            if args.clear_estimate:
                patch["estimate_minutes"] = None
            elif args.estimate is not None:
                patch["estimate_minutes"] = args.estimate

            if args.clear_due:
                patch["due_date"] = None
            elif args.due is not None:
                patch["due_date"] = args.due

            # Acceptance criteria mutations
            if args.clear_ac:
                acs: list[dict[str, Any]] = []
                if args.add_ac:
                    for txt in args.add_ac:
                        acs.append({"id": _next_ac_id(acs), "text": txt, "checked": False})
                patch["acceptance_criteria"] = acs
            elif any([args.add_ac, args.edit_ac, args.check_ac, args.uncheck_ac, args.remove_ac]):
                assert cur_issue is not None
                acs = [dict(x) for x in (cur_issue.get("acceptance_criteria") or [])]
                if args.edit_ac:
                    for target_tok, new_txt in args.edit_ac:
                        idx = _find_ac_index(acs, target_tok)
                        acs[idx]["text"] = new_txt
                if args.check_ac:
                    for target_tok in args.check_ac:
                        idx = _find_ac_index(acs, target_tok)
                        acs[idx]["checked"] = True
                if args.uncheck_ac:
                    for target_tok in args.uncheck_ac:
                        idx = _find_ac_index(acs, target_tok)
                        acs[idx]["checked"] = False
                if args.remove_ac:
                    indices_to_remove = sorted({_find_ac_index(acs, tok) for tok in args.remove_ac}, reverse=True)
                    for idx in indices_to_remove:
                        acs.pop(idx)
                if args.add_ac:
                    for txt in args.add_ac:
                        acs.append({"id": _next_ac_id(acs), "text": txt, "checked": False})
                patch["acceptance_criteria"] = acs

            # Tags mutations
            if args.clear_tags:
                tags_list: list[str] = flatten_csv_tokens(args.add_tag)
                patch["tags"] = tags_list
            elif args.add_tag or args.remove_tag:
                assert cur_issue is not None
                tags_list = list(cur_issue.get("tags") or [])
                for t in flatten_csv_tokens(args.add_tag):
                    if t not in tags_list:
                        tags_list.append(t)
                rem_tags = set(flatten_csv_tokens(args.remove_tag))
                tags_list = [t for t in tags_list if t not in rem_tags]
                patch["tags"] = tags_list

            # Components mutations
            if args.clear_components:
                comps_list: list[str] = flatten_csv_tokens(args.add_component)
                patch["components"] = comps_list
            elif args.add_component or args.remove_component:
                assert cur_issue is not None
                comps_list = list(cur_issue.get("components") or [])
                for c in flatten_csv_tokens(args.add_component):
                    if c not in comps_list:
                        comps_list.append(c)
                rem_comps = set(flatten_csv_tokens(args.remove_component))
                comps_list = [c for c in comps_list if c not in rem_comps]
                patch["components"] = comps_list

            # Custom fields mutations
            if args.clear_fields:
                cf_dict: dict[str, Any] = dict(field_updates)
                patch["custom_fields"] = cf_dict
                patch["replace_custom_fields"] = True
            elif field_updates or args.unset_field:
                assert cur_issue is not None
                cf_dict = dict(cur_issue.get("custom_fields") or {})
                if args.unset_field:
                    for k in flatten_csv_tokens(args.unset_field):
                        cf_dict.pop(k, None)
                cf_dict.update(field_updates)
                patch["custom_fields"] = cf_dict
                patch["replace_custom_fields"] = True

            if patch:
                patch["actor"] = actor
                cur_issue = client.request("PATCH", f"/api/v1/issues/{ref}", json_body=patch)

            if args.remove_embed:
                for att_id in flatten_csv_tokens(args.remove_embed):
                    cur_issue = client.request(
                        "DELETE",
                        f"/api/v1/issues/{ref}/embeds/{encode_ref(att_id)}",
                    )

            if embed_paths:
                mp_files, handles = open_multipart_files([("files", embed_paths)])
                try:
                    cur_issue = client.request(
                        "POST",
                        f"/api/v1/issues/{ref}/embeds",
                        files=[("uploaded_by", (None, actor))] + mp_files,
                    )
                finally:
                    for fh in handles:
                        fh.close()

            if cur_issue is None or "key" not in cur_issue:
                cur_issue = client.request("GET", f"/api/v1/issues/{ref}")

            if json_mode:
                emit_json(cur_issue)
            else:
                stdout_console.print(
                    f"[bold green]Updated[/bold green] [bold cyan]{cur_issue.get('key')}[/bold cyan]: "
                    f"[bold]{cur_issue.get('name')}[/bold]"
                )
            return

        if act == "state":
            body = {"state": normalize_hyphen_enum(args.state), "actor": actor}
            if args.resolution is not None:
                body["resolution"] = normalize_hyphen_enum(args.resolution)
            issue = client.request("PATCH", f"/api/v1/issues/{encode_ref(args.issue)}", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]State updated[/bold green] [cyan]{issue.get('key')}[/cyan] -> {fmt_state(issue.get('state'))}"
                )
            return

        if act == "rename":
            issue = client.request(
                "POST",
                f"/api/v1/issues/{encode_ref(args.issue)}/rename",
                json_body={"new_name": args.new_name, "actor": actor},
            )
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Renamed[/bold green] [cyan]{issue.get('key')}[/cyan] -> [bold]{issue.get('name')}[/bold]"
                )
            return

        if act == "clone":
            body = {"actor": actor}
            if args.name is not None:
                body["new_name"] = args.name
            issue = client.request("POST", f"/api/v1/issues/{encode_ref(args.issue)}/clone", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Cloned[/bold green] -> [bold cyan]{issue.get('key')}[/bold cyan]: {issue.get('name')}"
                )
            return

        if act == "archive":
            issue = client.request(
                "POST",
                f"/api/v1/issues/{encode_ref(args.issue)}/archive",
                json_body={"actor": actor},
            )
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(f"[yellow]Archived[/yellow] [cyan]{issue.get('key')}[/cyan]: {issue.get('name')}")
            return

        if act == "unarchive":
            issue = client.request(
                "POST",
                f"/api/v1/issues/{encode_ref(args.issue)}/unarchive",
                json_body={"actor": actor},
            )
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Unarchived[/bold green] [cyan]{issue.get('key')}[/cyan]: {issue.get('name')} ({issue.get('state')})"
                )
            return

        if act == "delete":
            require_yes(args.yes, f"issue '{args.issue}'")
            res = client.request("DELETE", f"/api/v1/issues/{encode_ref(args.issue)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted issue[/yellow] `{args.issue}`.")
            return

        if act == "context":
            data = client.request("GET", f"/api/v1/issues/{encode_ref(args.issue)}/context")
            if json_mode:
                emit_json(data)
            else:
                md_str = data.get("markdown") or json.dumps(data, indent=2)
                stdout_console.print(Panel(md_str, title=f"Agent Context Pack — {data.get('key', args.issue)}", border_style="cyan"))
            return


def cmd_comment(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.comment_action
    actor = default_actor(repo_root)
    base_url, _ = ensure_daemon(repo_root)
    ref = encode_ref(args.issue)

    with ITrackClient(base_url) as client:
        if act == "add":
            body_text = _resolve_text_source(args.body, args.body_file, args.stdin, label="comment body")
            embed_paths = validate_upload_paths(args.embed, label="Comment embed image")
            attach_paths = validate_upload_paths(args.attach, label="Comment attachment")
            linked_ids = [normalize_ref_str(x) for x in flatten_csv_tokens(args.link_attachment)]

            if body_text is None and not embed_paths and not attach_paths and not linked_ids:
                raise CLIError(
                    "Comment requires --body, --body-file, --stdin, --embed, --attach, or --link-attachment.",
                    error_code="empty_comment",
                    exit_code=1,
                )

            mp_files, handles = open_multipart_files([("embeds", embed_paths), ("attachments", attach_paths)])
            form_parts = [
                ("body", (None, body_text or "")),
                ("author", (None, args.author or actor)),
                ("linked_attachment_ids", (None, json.dumps(linked_ids))),
            ]
            try:
                cmt = client.request("POST", f"/api/v1/issues/{ref}/comments", files=form_parts + mp_files)
            finally:
                for fh in handles:
                    fh.close()

            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(render_comment_panel(cmt))
            return

        if act == "list":
            data = client.request("GET", f"/api/v1/issues/{ref}/comments")
            if json_mode:
                emit_json(data)
            else:
                cmts = data.get("comments") if isinstance(data, dict) else data
                for cmt in (cmts or []):
                    stdout_console.print(render_comment_panel(cmt))
                if not cmts:
                    stdout_console.print("[dim]No comments.[/dim]")
            return

        if act == "show":
            cmt = client.request("GET", f"/api/v1/issues/{ref}/comments/{encode_ref(args.comment_id)}")
            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(render_comment_panel(cmt))
            return

        if act == "edit":
            body_text = _resolve_text_source(args.body, args.body_file, args.stdin, label="comment body")
            add_embed_paths = validate_upload_paths(args.add_embed, label="Comment embed image")
            add_attach_paths = validate_upload_paths(args.add_attach, label="Comment attachment")
            rem_embeds = [normalize_ref_str(x) for x in flatten_csv_tokens(args.remove_embed)]
            rem_attach = [normalize_ref_str(x) for x in flatten_csv_tokens(args.remove_attach)]
            link_atts = [normalize_ref_str(x) for x in flatten_csv_tokens(args.link_attachment)]
            unlink_atts = [normalize_ref_str(x) for x in flatten_csv_tokens(args.unlink_attachment)]

            mp_files, handles = open_multipart_files([
                ("add_embeds", add_embed_paths),
                ("add_attachments", add_attach_paths),
            ])
            form_parts: list[tuple[str, tuple[Any, ...]]] = [
                ("actor", (None, args.actor or actor)),
                ("remove_embeds", (None, json.dumps(rem_embeds))),
                ("remove_attachments", (None, json.dumps(rem_attach))),
                ("link_attachments", (None, json.dumps(link_atts))),
                ("unlink_attachments", (None, json.dumps(unlink_atts))),
            ]
            if body_text is not None:
                form_parts.append(("body", (None, body_text)))
            try:
                cmt = client.request(
                    "PATCH",
                    f"/api/v1/issues/{ref}/comments/{encode_ref(args.comment_id)}",
                    files=form_parts + mp_files,
                )
            finally:
                for fh in handles:
                    fh.close()

            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(render_comment_panel(cmt))
            return

        if act == "embed-delete":
            form_parts = [
                ("actor", (None, actor)),
                ("remove_embeds", (None, json.dumps([normalize_ref_str(args.att_id)]))),
            ]
            cmt = client.request(
                "PATCH",
                f"/api/v1/issues/{ref}/comments/{encode_ref(args.comment_id)}",
                files=form_parts,
            )
            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(f"[yellow]Removed embedded image[/yellow] `{args.att_id}` from `{args.comment_id}`.")
            return

        if act == "attach-delete":
            form_parts = [
                ("actor", (None, actor)),
                ("remove_attachments", (None, json.dumps([normalize_ref_str(args.att_id)]))),
            ]
            cmt = client.request(
                "PATCH",
                f"/api/v1/issues/{ref}/comments/{encode_ref(args.comment_id)}",
                files=form_parts,
            )
            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(f"[yellow]Removed comment attachment[/yellow] `{args.att_id}` from `{args.comment_id}`.")
            return

        if act == "delete":
            require_yes(args.yes, f"comment '{args.comment_id}'")
            res = client.request("DELETE", f"/api/v1/issues/{ref}/comments/{encode_ref(args.comment_id)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted comment[/yellow] `{args.comment_id}`.")
            return


def cmd_attach(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.attach_action
    actor = default_actor(repo_root)
    base_url, _ = ensure_daemon(repo_root)
    ref = encode_ref(args.issue)

    with ITrackClient(base_url) as client:
        if act == "add":
            paths = validate_upload_paths(args.files, label="Attachment")
            mp_files, handles = open_multipart_files([("files", paths)])
            try:
                res = client.request(
                    "POST",
                    f"/api/v1/issues/{ref}/attachments",
                    files=[("uploaded_by", (None, args.by or actor))] + mp_files,
                )
            finally:
                for fh in handles:
                    fh.close()
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Uploaded {len(paths)} attachment(s)[/bold green] to `{args.issue}`.")
            return

        if act == "list":
            scope = (args.scope or "all").lower()
            if scope not in ("issue", "comments", "all"):
                raise CLIError(f"Invalid --scope '{args.scope}': expected issue, comments, or all.", "invalid_scope", 1)
            data = client.request("GET", f"/api/v1/issues/{ref}/attachments", params={"scope": scope})
            if json_mode:
                emit_json(data)
            else:
                items = data.get("attachments") if isinstance(data, dict) else data
                items = items or []
                t = Table(title=f"Attachments for {args.issue} (scope={scope}, {len(items)})", box=box.ROUNDED)
                t.add_column("ID", style="cyan")
                t.add_column("Scope", style="magenta")
                t.add_column("Filename", style="bold")
                t.add_column("MIME", style="dim")
                t.add_column("Size", justify="right")
                for a in items:
                    t.add_row(
                        str(a.get("id", "")),
                        str(a.get("scope", "")),
                        str(a.get("filename", "")),
                        str(a.get("mime_type", "")),
                        f"{a.get('size_bytes', 0)} B",
                    )
                stdout_console.print(t)
            return

        if act == "info":
            data = client.request("GET", f"/api/v1/issues/{ref}/attachments/{encode_ref(args.att_id)}")
            if json_mode:
                emit_json(data)
            else:
                preview = data.get("preview_text") or data.get("preview") or ""
                body = (
                    f"ID: [bold cyan]{data.get('id')}[/bold cyan]   Filename: [bold]{data.get('filename')}[/bold]\n"
                    f"Scope: {data.get('scope')}   MIME: {data.get('mime_type')}   Size: {data.get('size_bytes')} B\n"
                    f"SHA-256: [dim]{data.get('sha256')}[/dim]"
                )
                if preview:
                    body += f"\n\n[bold]Preview:[/bold]\n{preview}"
                stdout_console.print(Panel(body, title="Attachment Info", border_style="cyan"))
            return

        if act == "download":
            content, _ = client.download_bytes(
                f"/api/v1/issues/{ref}/attachments/{encode_ref(args.att_id)}/download"
            )
            out_path = Path(args.output).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(content)
            res = {"id": normalize_ref_str(args.att_id), "output": str(out_path), "size_bytes": len(content)}
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Downloaded[/bold green] `{args.att_id}` ({len(content)} B) -> `{out_path}`")
            return

        if act == "delete":
            require_yes(args.yes, f"attachment '{args.att_id}'")
            res = client.request("DELETE", f"/api/v1/issues/{ref}/attachments/{encode_ref(args.att_id)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted attachment[/yellow] `{args.att_id}`.")
            return


def cmd_link(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.link_action
    actor = default_actor(repo_root)
    base_url, _ = ensure_daemon(repo_root)
    ref = encode_ref(args.issue)

    with ITrackClient(base_url) as client:
        if act == "add":
            ltype = normalize_hyphen_enum(args.type)
            res = client.request(
                "POST",
                f"/api/v1/issues/{ref}/links",
                json_body={"type": ltype, "target": normalize_ref_str(args.target_issue), "actor": actor},
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Linked[/bold green] `{args.issue}` --\\[{ltype}]--> `{args.target_issue}`"
                )
            return

        if act == "list":
            data = client.request("GET", f"/api/v1/issues/{ref}/links")
            if json_mode:
                emit_json(data)
            else:
                links = data.get("links") if isinstance(data, dict) else data
                links = links or []
                t = Table(title=f"Links on {args.issue} ({len(links)})", box=box.ROUNDED)
                t.add_column("ID", style="cyan")
                t.add_column("Type", style="yellow")
                t.add_column("Direction", style="dim")
                t.add_column("Target", style="bold")
                for lnk in links:
                    t.add_row(
                        str(lnk.get("id", "")),
                        str(lnk.get("type", "")),
                        str(lnk.get("direction", "outbound")),
                        f"{lnk.get('target_key')} ({lnk.get('target_name', '')})",
                    )
                stdout_console.print(t)
            return

        if act == "remove":
            res = client.request("DELETE", f"/api/v1/issues/{ref}/links/{encode_ref(args.link_id)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Removed link[/yellow] `{args.link_id}`.")
            return


def cmd_tag(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.tag_action
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if act == "create":
            body = {"name": args.name}
            if args.color is not None:
                body["color"] = args.color
            if args.desc is not None:
                body["description"] = args.desc
            res = client.request("POST", "/api/v1/tags", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Created tag[/bold green] `{res.get('name', args.name)}`.")
            return

        if act == "list":
            data = client.request("GET", "/api/v1/tags")
            if json_mode:
                emit_json(data)
            else:
                tags = data.get("tags") if isinstance(data, dict) else data
                t = Table(title=f"Tags ({len(tags or [])})", box=box.ROUNDED)
                t.add_column("Name", style="bold cyan")
                t.add_column("Color", style="magenta")
                t.add_column("Description", style="dim")
                for tg in (tags or []):
                    t.add_row(str(tg.get("name", "")), str(tg.get("color", "")), str(tg.get("description") or "-"))
                stdout_console.print(t)
            return

        if act == "edit":
            body = {}
            if args.new_name is not None:
                body["new_name"] = args.new_name
            if args.color is not None:
                body["color"] = args.color
            if args.desc is not None:
                body["description"] = args.desc
            res = client.request("PATCH", f"/api/v1/tags/{urllib.parse.quote(args.name, safe='')}", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Updated tag[/bold green] `{args.name}`.")
            return

        if act == "delete":
            require_yes(args.yes, f"tag '{args.name}'")
            res = client.request("DELETE", f"/api/v1/tags/{urllib.parse.quote(args.name, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted tag[/yellow] `{args.name}`.")
            return


def cmd_sprint(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.sprint_action
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if act == "create":
            body: dict[str, Any] = {"name": args.name}
            if args.goal is not None:
                body["goal"] = args.goal
            if args.start is not None:
                body["start_date"] = args.start
            if args.end is not None:
                body["end_date"] = args.end
            res = client.request("POST", "/api/v1/sprints", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Created sprint[/bold green] [cyan]{res.get('id')}[/cyan]: {res.get('name')}"
                )
            return

        if act == "list":
            params = {"state": args.state.lower()} if args.state else None
            data = client.request("GET", "/api/v1/sprints", params=params)
            if json_mode:
                emit_json(data)
            else:
                sprints = data.get("sprints") if isinstance(data, dict) else data
                t = Table(title=f"Sprints ({len(sprints or [])})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Name", style="bold")
                t.add_column("State", style="yellow")
                t.add_column("Dates", style="dim")
                t.add_column("Goal")
                for sp in (sprints or []):
                    t.add_row(
                        str(sp.get("id", "")),
                        str(sp.get("name", "")),
                        str(sp.get("state", "")),
                        f"{sp.get('start_date') or '-'} -> {sp.get('end_date') or '-'}",
                        str(sp.get("goal") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "show":
            res = client.request("GET", f"/api/v1/sprints/{encode_ref(args.sprint)}")
            if json_mode:
                emit_json(res)
            else:
                sp = res.get("sprint") if isinstance(res, dict) and "sprint" in res else res
                stdout_console.print(
                    Panel(
                        f"Name:  [bold]{sp.get('name')}[/bold] ({sp.get('state')})\n"
                        f"Goal:  {sp.get('goal') or '-'}\n"
                        f"Dates: {sp.get('start_date') or '-'} -> {sp.get('end_date') or '-'}",
                        title=f"Sprint {sp.get('id')}",
                        border_style="cyan",
                    )
                )
                if isinstance(res, dict) and res.get("issues"):
                    render_issues_table({"issues": res["issues"], "total": len(res["issues"])}, title="Sprint Issues")
            return

        if act == "edit":
            body = {}
            if args.name is not None:
                body["name"] = args.name
            if args.goal is not None:
                body["goal"] = args.goal
            if args.start is not None:
                body["start_date"] = args.start
            if args.end is not None:
                body["end_date"] = args.end
            res = client.request("PATCH", f"/api/v1/sprints/{encode_ref(args.sprint)}", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Updated sprint[/bold green] `{args.sprint}`.")
            return

        if act == "start":
            body = {}
            if args.start is not None:
                body["start_date"] = args.start
            if args.end is not None:
                body["end_date"] = args.end
            res = client.request("POST", f"/api/v1/sprints/{encode_ref(args.sprint)}/start", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Started sprint[/bold green] `{args.sprint}`.")
            return

        if act == "complete":
            rollover_to: str | None = None
            if args.rollover and args.rollover.strip().lower() not in ("none", "null", "backlog"):
                rollover_to = normalize_ref_str(args.rollover)
            res = client.request(
                "POST",
                f"/api/v1/sprints/{encode_ref(args.sprint)}/complete",
                json_body={"rollover_to": rollover_to},
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Completed sprint[/bold green] `{args.sprint}`.")
            return

        if act == "assign":
            issues = parse_issue_refs(args.issues, repo_root)
            res = client.request(
                "POST",
                f"/api/v1/sprints/{encode_ref(args.sprint)}/assign",
                json_body={"issues": issues},
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Assigned {len(issues)} issue(s)[/bold green] to `{args.sprint}`.")
            return

        if act == "remove":
            issues = parse_issue_refs(args.issues, repo_root)
            res = client.request(
                "POST",
                f"/api/v1/sprints/{encode_ref(args.sprint)}/remove",
                json_body={"issues": issues},
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Removed {len(issues)} issue(s)[/yellow] from `{args.sprint}`.")
            return

        if act == "delete":
            require_yes(args.yes, f"sprint '{args.sprint}'")
            res = client.request("DELETE", f"/api/v1/sprints/{encode_ref(args.sprint)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted sprint[/yellow] `{args.sprint}`.")
            return


def cmd_milestone(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.milestone_action
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if act == "create":
            body: dict[str, Any] = {"name": args.name}
            if args.desc is not None:
                body["description"] = args.desc
            if args.due is not None:
                body["due_date"] = args.due
            res = client.request("POST", "/api/v1/milestones", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Created milestone[/bold green] [cyan]{res.get('id')}[/cyan]: {res.get('name')}"
                )
            return

        if act == "list":
            params = {"state": args.state.lower()} if args.state else None
            data = client.request("GET", "/api/v1/milestones", params=params)
            if json_mode:
                emit_json(data)
            else:
                items = data.get("milestones") if isinstance(data, dict) else data
                t = Table(title=f"Milestones ({len(items or [])})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Name", style="bold")
                t.add_column("State", style="yellow")
                t.add_column("Due Date", style="dim")
                t.add_column("Description")
                for m in (items or []):
                    t.add_row(
                        str(m.get("id", "")),
                        str(m.get("name", "")),
                        str(m.get("state", "")),
                        str(m.get("due_date") or "-"),
                        str(m.get("description") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "show":
            res = client.request("GET", f"/api/v1/milestones/{encode_ref(args.milestone)}")
            if json_mode:
                emit_json(res)
            else:
                m = res.get("milestone") if isinstance(res, dict) and "milestone" in res else res
                stdout_console.print(
                    Panel(
                        f"Name:  [bold]{m.get('name')}[/bold] ({m.get('state')})\n"
                        f"Due:   {m.get('due_date') or '-'}\n"
                        f"Desc:  {m.get('description') or '-'}",
                        title=f"Milestone {m.get('id')}",
                        border_style="cyan",
                    )
                )
            return

        if act == "edit":
            body = {}
            if args.name is not None:
                body["name"] = args.name
            if args.desc is not None:
                body["description"] = args.desc
            if args.due is not None:
                body["due_date"] = args.due
            res = client.request("PATCH", f"/api/v1/milestones/{encode_ref(args.milestone)}", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Updated milestone[/bold green] `{args.milestone}`.")
            return

        if act == "close":
            res = client.request("POST", f"/api/v1/milestones/{encode_ref(args.milestone)}/close")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Closed milestone[/bold green] `{args.milestone}`.")
            return

        if act == "reopen":
            res = client.request("POST", f"/api/v1/milestones/{encode_ref(args.milestone)}/reopen")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Reopened milestone[/bold green] `{args.milestone}`.")
            return

        if act == "delete":
            require_yes(args.yes, f"milestone '{args.milestone}'")
            res = client.request("DELETE", f"/api/v1/milestones/{encode_ref(args.milestone)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted milestone[/yellow] `{args.milestone}`.")
            return


def cmd_view(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.view_action
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if act == "create":
            body: dict[str, Any] = {"name": args.name, "query": args.query}
            if args.tab is not None:
                body["tab"] = normalize_hyphen_enum(args.tab)
            if args.sort is not None:
                body["sort"] = args.sort
            if args.order is not None:
                body["order"] = args.order.lower()
            if args.swimlane is not None:
                body["swimlane"] = args.swimlane.lower()
            if args.desc is not None:
                body["description"] = args.desc
            res = client.request("POST", "/api/v1/views", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Created view[/bold green] [cyan]{res.get('id')}[/cyan]: {res.get('name')}"
                )
            return

        if act == "list":
            data = client.request("GET", "/api/v1/views")
            if json_mode:
                emit_json(data)
            else:
                views = data.get("views") if isinstance(data, dict) else data
                t = Table(title=f"Saved Views ({len(views or [])})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Name", style="bold")
                t.add_column("Tab", style="magenta")
                t.add_column("Query", style="yellow")
                t.add_column("Description", style="dim")
                for v in (views or []):
                    t.add_row(
                        str(v.get("id", "")),
                        str(v.get("name", "")),
                        str(v.get("tab") or "all"),
                        str(v.get("query") or ""),
                        str(v.get("description") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "show":
            res = client.request("GET", f"/api/v1/views/{encode_ref(args.view)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(Panel(json.dumps(res, indent=2), title=f"Saved View {res.get('id', args.view)}", border_style="cyan"))
            return

        if act == "run":
            body = {}
            if args.page is not None:
                body["page"] = args.page
            if args.page_size is not None:
                body["page_size"] = args.page_size
            data = client.request("POST", f"/api/v1/views/{encode_ref(args.view)}/run", json_body=body)
            if json_mode:
                emit_json(data)
            else:
                render_issues_table(data, title=f"Saved View: {args.view}")
            return

        if act == "edit":
            body = {}
            if args.name is not None:
                body["name"] = args.name
            if args.query is not None:
                body["query"] = args.query
            if args.tab is not None:
                body["tab"] = normalize_hyphen_enum(args.tab)
            if args.sort is not None:
                body["sort"] = args.sort
            if args.order is not None:
                body["order"] = args.order.lower()
            if args.swimlane is not None:
                body["swimlane"] = args.swimlane.lower()
            if args.desc is not None:
                body["description"] = args.desc
            res = client.request("PATCH", f"/api/v1/views/{encode_ref(args.view)}", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold green]Updated view[/bold green] `{args.view}`.")
            return

        if act == "delete":
            require_yes(args.yes, f"view '{args.view}'")
            res = client.request("DELETE", f"/api/v1/views/{encode_ref(args.view)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted view[/yellow] `{args.view}`.")
            return


def cmd_worklog(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.worklog_action
    actor = default_actor(repo_root)
    base_url, _ = ensure_daemon(repo_root)
    ref = encode_ref(args.issue)

    with ITrackClient(base_url) as client:
        if act == "add":
            raw_dur = args.duration_or_minutes.strip()
            body: dict[str, Any] = {"author": args.author or actor}
            if re.fullmatch(r"-?\d+", raw_dur):
                body["minutes"] = int(raw_dur)
            else:
                body["duration"] = raw_dur
            if args.note is not None:
                body["note"] = args.note
            res = client.request("POST", f"/api/v1/issues/{ref}/worklogs", json_body=body)
            if json_mode:
                emit_json(res)
            else:
                wl = res.get("worklog") if isinstance(res, dict) and "worklog" in res else res
                stdout_console.print(
                    f"[bold green]Logged work[/bold green] [cyan]{wl.get('id', '')}[/cyan] "
                    f"({wl.get('minutes', raw_dur)}m) on `{args.issue}`."
                )
            return

        if act == "list":
            data = client.request("GET", f"/api/v1/issues/{ref}/worklogs")
            if json_mode:
                emit_json(data)
            else:
                wls = data.get("worklogs") if isinstance(data, dict) else data
                tp = data.get("time_progress") if isinstance(data, dict) else {}
                logged = int((tp or {}).get("logged_minutes", sum(int(w.get("minutes", 0)) for w in (wls or []))))
                est = (tp or {}).get("estimate_minutes")
                stdout_console.print(f"[bold]Time Progress:[/bold] {render_time_bar(logged, est)}")
                t = Table(title=f"Worklogs on {args.issue} ({len(wls or [])})", box=box.ROUNDED)
                t.add_column("ID", style="cyan")
                t.add_column("Author", style="green")
                t.add_column("Minutes", justify="right")
                t.add_column("Created", style="dim")
                t.add_column("Note")
                for w in (wls or []):
                    t.add_row(
                        str(w.get("id", "")),
                        str(w.get("author", "")),
                        str(w.get("minutes", 0)),
                        str(w.get("created_at", "")),
                        str(w.get("note") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "delete":
            require_yes(args.yes, f"worklog '{args.worklog_id}'")
            res = client.request("DELETE", f"/api/v1/issues/{ref}/worklogs/{encode_ref(args.worklog_id)}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted worklog[/yellow] `{args.worklog_id}`.")
            return


def cmd_bulk(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.bulk_action
    actor = default_actor(repo_root)
    issues = parse_issue_refs(args.issues, repo_root)
    if not issues and not args.query:
        raise CLIError("Specify target issues via --issues and/or --query.", "missing_bulk_targets", 1)

    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if act == "update":
            if args.assignee is not None and args.clear_assignee:
                raise CLIError("Cannot specify both --assignee and --clear-assignee.", "conflicting_flags", 1)
            if args.sprint is not None and args.clear_sprint:
                raise CLIError("Cannot specify both --sprint and --clear-sprint.", "conflicting_flags", 1)
            if args.milestone is not None and args.clear_milestone:
                raise CLIError("Cannot specify both --milestone and --clear-milestone.", "conflicting_flags", 1)
            if args.archive and args.unarchive:
                raise CLIError("Cannot specify both --archive and --unarchive.", "conflicting_flags", 1)

            updates: dict[str, Any] = {}
            if args.state is not None:
                updates["state"] = normalize_hyphen_enum(args.state)
            if args.priority is not None:
                updates["priority"] = normalize_hyphen_enum(args.priority)
            if args.severity is not None:
                updates["severity"] = normalize_hyphen_enum(args.severity)
            if args.category is not None:
                updates["category"] = normalize_hyphen_enum(args.category)
            if args.clear_assignee:
                updates["assignee"] = None
            elif args.assignee is not None:
                updates["assignee"] = args.assignee
            if args.add_tag:
                updates["add_tags"] = flatten_csv_tokens(args.add_tag)
            if args.remove_tag:
                updates["remove_tags"] = flatten_csv_tokens(args.remove_tag)
            if args.clear_sprint:
                updates["sprint"] = None
            elif args.sprint is not None:
                updates["sprint"] = normalize_ref_str(args.sprint)
            if args.clear_milestone:
                updates["milestone"] = None
            elif args.milestone is not None:
                updates["milestone"] = normalize_ref_str(args.milestone)
            if args.archive:
                updates["archived"] = True
            elif args.unarchive:
                updates["archived"] = False

            if not updates:
                raise CLIError("No update fields specified for bulk update.", "empty_bulk_updates", 1)

            payload: dict[str, Any] = {"updates": updates, "actor": actor}
            if issues:
                payload["issues"] = issues
            if args.query is not None:
                payload["query"] = args.query

            res = client.request("POST", "/api/v1/bulk/update", json_body=payload)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Bulk updated[/bold green] {res.get('updated_count', len(res.get('updated', [])))} issue(s)."
                )
            return

        if act == "delete":
            require_yes(args.yes, "bulk-matched issues")
            payload = {"actor": actor}
            if issues:
                payload["issues"] = issues
            if args.query is not None:
                payload["query"] = args.query
            res = client.request("POST", "/api/v1/bulk/delete", json_body=payload)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[yellow]Bulk deleted[/yellow] {res.get('deleted_count', len(res.get('deleted', [])))} issue(s)."
                )
            return


# ---------------------------------------------------------------------------
# Strict Exit-Code ArgumentParser & Complete Section 7 Subcommand Tree
# ---------------------------------------------------------------------------


class CLIArgumentParser(argparse.ArgumentParser):
    """ArgumentParser subclass exiting with code 1 (never 2) on parse/usage errors."""

    def error(self, message: str) -> NoReturn:
        raise CLIError(message, error_code="invalid_arguments", exit_code=1)


def build_parser() -> CLIArgumentParser:
    parser = CLIArgumentParser(
        prog="itrack.py",
        description="itrack — Git-rooted local-first CLI, Self-Teaching Manual & Web UI launcher.",
    )
    parser.add_argument("--json", action="store_true", help="Emit strictly valid JSON to stdout")
    parser.add_argument("--root", metavar="PATH", help="Target Git repository root (overrides ITRACK_ROOT and cwd)")
    parser.add_argument("-V", "--version", action="store_true", help="Print version and exit 0")

    sub = parser.add_subparsers(dest="command")

    # 2. init
    p_init = sub.add_parser("init", help="Initialize workspace under <repo_root>/itrack-issues/")
    p_init.add_argument("--prefix", help="Issue key prefix (default ITR)")
    p_init.add_argument("--title", help="Workspace project title (default 'iTrack Workspace')")
    p_init.set_defaults(handler=cmd_init)

    # 3. status
    sub.add_parser("status", help="Show workspace & daemon status summary").set_defaults(handler=cmd_status)

    # 4. reindex
    sub.add_parser("reindex", help="Rebuild index.sqlite + FTS5 from canonical on-disk JSON").set_defaults(handler=cmd_reindex)

    # 5. config
    p_cfg = sub.add_parser("config", help="View or update workspace configuration")
    csub = p_cfg.add_subparsers(dest="config_action", required=True)
    csub.add_parser("show", help="Show full workspace config")
    p_cset = csub.add_parser("set", help="Set a configuration key")
    p_cset.add_argument("key", help="Configuration key (e.g. project_title, default_state, default_priority, wip_limits)")
    p_cset.add_argument("value", help="Configuration value (string, number, boolean, or JSON)")
    p_cfg.set_defaults(handler=cmd_config)

    # 6. server
    p_srv = sub.add_parser("server", help="Manage per-repository background HTTP daemon")
    ssub = p_srv.add_subparsers(dest="server_action", required=True)
    p_sstart = ssub.add_parser("start", help="Start background daemon")
    p_sstart.add_argument("--port", type=int, default=0, help="TCP port (0 for ephemeral)")
    ssub.add_parser("stop", help="Gracefully stop background daemon")
    p_srestart = ssub.add_parser("restart", help="Restart background daemon")
    p_srestart.add_argument("--port", type=int, default=0, help="TCP port (0 for ephemeral)")
    ssub.add_parser("status", help="Show daemon liveness & health")
    p_srv.set_defaults(handler=cmd_server)

    # 7. ui
    p_ui = sub.add_parser("ui", help="Ensure server is running and launch offline Web UI")
    p_ui.add_argument("--port", type=int, default=0, help="Preferred TCP port")
    p_ui.add_argument("--no-browser", action="store_true", help="Do not open desktop browser")
    p_ui.add_argument("--foreground", action="store_true", help="Run UI server in foreground")
    p_ui.set_defaults(handler=cmd_ui)

    # 8. issue
    p_issue = sub.add_parser("issue", help="Complete issue CRUD, folder renames, lifecycle & agent context")
    isub = p_issue.add_subparsers(dest="issue_action", required=True)

    p_ic = isub.add_parser("create", help="Create an issue (creates folder issues/<name>/)")
    p_ic.add_argument("name", help="Exact issue name (spaces & Unicode supported)")
    p_ic.add_argument("--category", help="Issue category")
    p_ic.add_argument("--state", help="Initial workflow state")
    p_ic.add_argument("--priority", help="Priority level")
    p_ic.add_argument("--severity", help="Severity level")
    p_ic.add_argument("--resolution", help="Initial resolution")
    p_ic.add_argument("--desc", help="Rich Markdown description")
    p_ic.add_argument("--desc-file", metavar="PATH", help="Read description Markdown from file")
    p_ic.add_argument("--desc-stdin", action="store_true", help="Read description Markdown from stdin")
    p_ic.add_argument("--embed", nargs="+", action="extend", metavar="IMG", help="Inline embedded image(s) for description")
    p_ic.add_argument("--attach", nargs="+", action="extend", metavar="FILE", help="Whole-issue file attachment(s)")
    p_ic.add_argument("--ac", nargs="+", action="extend", metavar="TEXT", help="Acceptance criterion item(s)")
    p_ic.add_argument("--assignee", metavar="USER", help="Assignee username")
    p_ic.add_argument("--reporter", metavar="USER", help="Reporter username")
    p_ic.add_argument("--tag", nargs="+", action="extend", metavar="TAG", help="Tag(s)")
    p_ic.add_argument("--component", nargs="+", action="extend", metavar="COMP", help="Component(s)")
    p_ic.add_argument("--parent", metavar="ISSUE", help="Parent issue key or name")
    p_ic.add_argument("--sprint", metavar="SPRINT", help="Sprint ID or name")
    p_ic.add_argument("--milestone", metavar="MIL", help="Milestone ID or name")
    p_ic.add_argument("--points", type=float, metavar="FLOAT", help="Story points")
    p_ic.add_argument("--estimate", type=int, metavar="INT", help="Time estimate in minutes")
    p_ic.add_argument("--due", metavar="YYYY-MM-DD", help="Due date (YYYY-MM-DD)")
    p_ic.add_argument("--field", nargs="+", action="extend", metavar="KEY=VAL", help="Custom field key=value pair(s)")

    p_ishow = isub.add_parser("show", help="Show enriched issue details")
    p_ishow.add_argument("issue", help="Issue key (ITR-N) or exact issue name")

    p_ilist = isub.add_parser("list", help="Paginated & filtered issue list")
    p_ilist.add_argument("--tab", help="Preset filter tab")
    p_ilist.add_argument("--query", metavar="IQL", help="iQL filter query")
    p_ilist.add_argument("--category", metavar="CAT", help="Filter by category")
    p_ilist.add_argument("--state", metavar="STATE", help="Filter by state")
    p_ilist.add_argument("--priority", metavar="PRIO", help="Filter by priority")
    p_ilist.add_argument("--severity", metavar="SEV", help="Filter by severity")
    p_ilist.add_argument("--resolution", metavar="RES", help="Filter by resolution")
    p_ilist.add_argument("--assignee", metavar="USER", help="Filter by assignee")
    p_ilist.add_argument("--reporter", metavar="USER", help="Filter by reporter")
    p_ilist.add_argument("--tag", metavar="TAG", help="Filter by tag")
    p_ilist.add_argument("--component", metavar="COMP", help="Filter by component")
    p_ilist.add_argument("--parent", metavar="ISSUE", help="Filter by parent issue")
    p_ilist.add_argument("--sprint", metavar="SPRINT", help="Filter by sprint")
    p_ilist.add_argument("--milestone", metavar="MIL", help="Filter by milestone")
    p_ilist.add_argument("--overdue", action="store_true", help="Only overdue issues")
    p_ilist.add_argument("--sort", metavar="FIELD", help="Sort field")
    p_ilist.add_argument("--order", choices=["asc", "desc"], help="Sort direction (asc or desc)")
    p_ilist.add_argument("--page", type=int, metavar="INT", help="Page number (1-indexed)")
    p_ilist.add_argument("--page-size", type=int, metavar="INT", help="Page size (e.g. 10, 25, 50, 100)")

    p_iedit = isub.add_parser("edit", help="Edit or clear/unset any issue field")
    p_iedit.add_argument("issue", help="Issue key (ITR-N) or exact issue name")
    p_iedit.add_argument("--name", metavar="NEW_NAME", help="Rename issue & folder atomically")
    p_iedit.add_argument("--category", metavar="CAT", help="Set category")
    p_iedit.add_argument("--state", metavar="STATE", help="Set workflow state")
    p_iedit.add_argument("--priority", metavar="PRIO", help="Set priority")
    p_iedit.add_argument("--severity", metavar="SEV", help="Set severity")
    p_iedit.add_argument("--resolution", metavar="RES", help="Set resolution")
    p_iedit.add_argument("--clear-resolution", action="store_true", help="Clear resolution to null")
    p_iedit.add_argument("--desc", metavar="TEXT", help="Replace Markdown description")
    p_iedit.add_argument("--desc-file", metavar="PATH", help="Replace description from file")
    p_iedit.add_argument("--desc-stdin", action="store_true", help="Replace description from stdin")
    p_iedit.add_argument("--clear-desc", action="store_true", help="Clear description to empty")
    p_iedit.add_argument("--embed", nargs="+", action="extend", metavar="IMG", help="Add inline embedded image(s) to description")
    p_iedit.add_argument("--remove-embed", nargs="+", action="extend", metavar="ATT_ID", help="Delete embedded image(s) from description")
    p_iedit.add_argument("--add-ac", nargs="+", action="extend", metavar="TEXT", help="Append acceptance criteria item(s)")
    p_iedit.add_argument("--edit-ac", nargs=2, action="append", metavar=("ID_OR_IDX", "TEXT"), help="Edit acceptance criterion text")
    p_iedit.add_argument("--check-ac", nargs="+", action="extend", metavar="ID_OR_IDX", help="Check [x] acceptance criterion")
    p_iedit.add_argument("--uncheck-ac", nargs="+", action="extend", metavar="ID_OR_IDX", help="Uncheck [ ] acceptance criterion")
    p_iedit.add_argument("--remove-ac", nargs="+", action="extend", metavar="ID_OR_IDX", help="Remove acceptance criterion")
    p_iedit.add_argument("--clear-ac", action="store_true", help="Clear all acceptance criteria")
    p_iedit.add_argument("--assignee", metavar="USER", help="Set assignee")
    p_iedit.add_argument("--clear-assignee", action="store_true", help="Clear assignee")
    p_iedit.add_argument("--reporter", metavar="USER", help="Set reporter")
    p_iedit.add_argument("--clear-reporter", action="store_true", help="Clear reporter")
    p_iedit.add_argument("--add-tag", nargs="+", action="extend", metavar="TAG", help="Add tag(s)")
    p_iedit.add_argument("--remove-tag", nargs="+", action="extend", metavar="TAG", help="Remove tag(s)")
    p_iedit.add_argument("--clear-tags", action="store_true", help="Clear all tags")
    p_iedit.add_argument("--add-component", nargs="+", action="extend", metavar="COMP", help="Add component(s)")
    p_iedit.add_argument("--remove-component", nargs="+", action="extend", metavar="COMP", help="Remove component(s)")
    p_iedit.add_argument("--clear-components", action="store_true", help="Clear all components")
    p_iedit.add_argument("--parent", metavar="ISSUE", help="Set parent issue")
    p_iedit.add_argument("--clear-parent", action="store_true", help="Clear parent issue")
    p_iedit.add_argument("--sprint", metavar="SPRINT", help="Assign to sprint")
    p_iedit.add_argument("--clear-sprint", action="store_true", help="Remove from sprint")
    p_iedit.add_argument("--milestone", metavar="MIL", help="Assign to milestone")
    p_iedit.add_argument("--clear-milestone", action="store_true", help="Remove from milestone")
    p_iedit.add_argument("--points", type=float, metavar="FLOAT", help="Set story points")
    p_iedit.add_argument("--clear-points", action="store_true", help="Clear story points")
    p_iedit.add_argument("--estimate", type=int, metavar="INT", help="Set estimate minutes")
    p_iedit.add_argument("--clear-estimate", action="store_true", help="Clear estimate minutes")
    p_iedit.add_argument("--due", metavar="YYYY-MM-DD", help="Set due date")
    p_iedit.add_argument("--clear-due", action="store_true", help="Clear due date")
    p_iedit.add_argument("--field", nargs="+", action="extend", metavar="KEY=VAL", help="Set custom field(s)")
    p_iedit.add_argument("--unset-field", nargs="+", action="extend", metavar="KEY", help="Remove custom field key(s)")
    p_iedit.add_argument("--clear-fields", action="store_true", help="Clear all custom fields")

    p_istate = isub.add_parser("state", help="Transition issue workflow state")
    p_istate.add_argument("issue", help="Issue key or exact name")
    p_istate.add_argument("state", help="Target state")
    p_istate.add_argument("--resolution", metavar="RES", help="Optional resolution")

    p_irename = isub.add_parser("rename", help="Atomically rename issue and on-disk folder")
    p_irename.add_argument("issue", help="Current issue key or exact name")
    p_irename.add_argument("new_name", help="New exact issue name")

    p_iclone = isub.add_parser("clone", help="Clone an issue")
    p_iclone.add_argument("issue", help="Source issue key or exact name")
    p_iclone.add_argument("--name", metavar="NEW_NAME", help="Optional name for cloned issue")

    p_iarch = isub.add_parser("archive", help="Archive an issue")
    p_iarch.add_argument("issue", help="Issue key or exact name")

    p_iunarch = isub.add_parser("unarchive", help="Unarchive an issue")
    p_iunarch.add_argument("issue", help="Issue key or exact name")

    p_idel = isub.add_parser("delete", help="Permanently delete an issue and its folder")
    p_idel.add_argument("issue", help="Issue key or exact name")
    p_idel.add_argument("--yes", action="store_true", help="Confirm permanent deletion")

    p_ictx = isub.add_parser("context", help="Generate structured Agent Context Pack")
    p_ictx.add_argument("issue", help="Issue key or exact name")
    p_issue.set_defaults(handler=cmd_issue)

    # 9. comment
    p_cmt = sub.add_parser("comment", help="Manage rich Markdown comments, inline embeds & comment attachments")
    cmtsub = p_cmt.add_subparsers(dest="comment_action", required=True)

    p_cadd = cmtsub.add_parser("add", help="Add a rich comment to an issue")
    p_cadd.add_argument("issue", help="Issue key or exact name")
    p_cadd.add_argument("--body", metavar="TEXT", help="Markdown comment body")
    p_cadd.add_argument("--body-file", metavar="PATH", help="Read comment Markdown from file")
    p_cadd.add_argument("--stdin", action="store_true", help="Read comment Markdown from stdin")
    p_cadd.add_argument("--author", metavar="USER", help="Comment author")
    p_cadd.add_argument("--embed", nargs="+", action="extend", metavar="IMG", help="Inline embedded image(s)")
    p_cadd.add_argument("--attach", nargs="+", action="extend", metavar="FILE", help="Comment-scoped attachment(s)")
    p_cadd.add_argument("--link-attachment", nargs="+", action="extend", metavar="ATT_ID", help="Link existing whole-issue attachment ID(s)")

    p_clist = cmtsub.add_parser("list", help="List comments on an issue")
    p_clist.add_argument("issue", help="Issue key or exact name")

    p_cshow = cmtsub.add_parser("show", help="Show a single comment")
    p_cshow.add_argument("issue", help="Issue key or exact name")
    p_cshow.add_argument("comment_id", help="Comment ID (e.g. CMT-1)")

    p_cedit = cmtsub.add_parser("edit", help="Edit comment body, embeds, attachments, or linked attachments")
    p_cedit.add_argument("issue", help="Issue key or exact name")
    p_cedit.add_argument("comment_id", help="Comment ID")
    p_cedit.add_argument("--body", metavar="TEXT", help="Updated Markdown body")
    p_cedit.add_argument("--body-file", metavar="PATH", help="Read updated Markdown from file")
    p_cedit.add_argument("--stdin", action="store_true", help="Read updated Markdown from stdin")
    p_cedit.add_argument("--actor", metavar="USER", help="Editor username")
    p_cedit.add_argument("--add-embed", nargs="+", action="extend", metavar="IMG", help="Add inline embedded image(s)")
    p_cedit.add_argument("--remove-embed", nargs="+", action="extend", metavar="ATT_ID", help="Delete inline embedded image(s)")
    p_cedit.add_argument("--add-attach", nargs="+", action="extend", metavar="FILE", help="Add comment-scoped attachment(s)")
    p_cedit.add_argument("--remove-attach", nargs="+", action="extend", metavar="ATT_ID", help="Delete comment-scoped attachment(s)")
    p_cedit.add_argument("--link-attachment", nargs="+", action="extend", metavar="ATT_ID", help="Link whole-issue attachment ID(s)")
    p_cedit.add_argument("--unlink-attachment", nargs="+", action="extend", metavar="ATT_ID", help="Unlink whole-issue attachment ID(s)")

    p_cedel = cmtsub.add_parser("embed-delete", help="Delete a single inline embedded image from a comment")
    p_cedel.add_argument("issue", help="Issue key or exact name")
    p_cedel.add_argument("comment_id", help="Comment ID")
    p_cedel.add_argument("att_id", help="Embedded image attachment ID")

    p_cadel = cmtsub.add_parser("attach-delete", help="Delete a single comment-scoped attachment")
    p_cadel.add_argument("issue", help="Issue key or exact name")
    p_cadel.add_argument("comment_id", help="Comment ID")
    p_cadel.add_argument("att_id", help="Comment attachment ID")

    p_cdel = cmtsub.add_parser("delete", help="Delete a full comment and its owned files")
    p_cdel.add_argument("issue", help="Issue key or exact name")
    p_cdel.add_argument("comment_id", help="Comment ID")
    p_cdel.add_argument("--yes", action="store_true", help="Confirm comment deletion")
    p_cmt.set_defaults(handler=cmd_comment)

    # 10. attach
    p_att = sub.add_parser("attach", help="Manage whole-issue and unified attachments")
    attsub = p_att.add_subparsers(dest="attach_action", required=True)

    p_aadd = attsub.add_parser("add", help="Upload whole-issue attachment(s)")
    p_aadd.add_argument("issue", help="Issue key or exact name")
    p_aadd.add_argument("files", nargs="+", help="Local file path(s) to attach")
    p_aadd.add_argument("--by", metavar="USER", help="Uploader username")

    p_alist = attsub.add_parser("list", help="List attachments on an issue")
    p_alist.add_argument("issue", help="Issue key or exact name")
    p_alist.add_argument("--scope", choices=["issue", "comments", "all"], default="all", help="Attachment scope filter")

    p_ainfo = attsub.add_parser("info", help="Inspect attachment metadata & text preview")
    p_ainfo.add_argument("issue", help="Issue key or exact name")
    p_ainfo.add_argument("att_id", help="Attachment ID (e.g. ATT-1)")

    p_adl = attsub.add_parser("download", help="Download attachment bytes to disk")
    p_adl.add_argument("issue", help="Issue key or exact name")
    p_adl.add_argument("att_id", help="Attachment ID")
    p_adl.add_argument("-o", "--output", required=True, metavar="OUTPUT_PATH", help="Destination file path")

    p_adel = attsub.add_parser("delete", help="Delete whole-issue attachment")
    p_adel.add_argument("issue", help="Issue key or exact name")
    p_adel.add_argument("att_id", help="Attachment ID")
    p_adel.add_argument("--yes", action="store_true", help="Confirm attachment deletion")
    p_att.set_defaults(handler=cmd_attach)

    # 11. link
    p_lnk = sub.add_parser("link", help="Manage bidirectional issue dependency & relationship links")
    lnksub = p_lnk.add_subparsers(dest="link_action", required=True)

    p_ladd = lnksub.add_parser("add", help="Add bidirectional link between two issues")
    p_ladd.add_argument("issue", help="Source issue key or exact name")
    p_ladd.add_argument("type", help="Link type (blocks, blocked-by, relates-to, duplicates, parent-of, clones)")
    p_ladd.add_argument("target_issue", help="Target issue key or exact name")

    p_llist = lnksub.add_parser("list", help="List links on an issue")
    p_llist.add_argument("issue", help="Issue key or exact name")

    p_lrem = lnksub.add_parser("remove", help="Remove bidirectional link by ID")
    p_lrem.add_argument("issue", help="Issue key or exact name")
    p_lrem.add_argument("link_id", help="Link ID (e.g. LNK-1)")
    p_lnk.set_defaults(handler=cmd_link)

    # 12. tree
    p_tree = sub.add_parser("tree", help="Render parent/subtask & blocker hierarchy tree")
    p_tree.add_argument("issue", nargs="?", help="Optional root issue key or exact name")
    p_tree.set_defaults(handler=cmd_tree)

    # 13. search
    p_search = sub.add_parser("search", help="Search issues via FTS5 full-text or structured iQL")
    p_search.add_argument("query", help="Full-text query or iQL expression")
    p_search.add_argument("--iql", action="store_true", help="Evaluate query strictly as iQL")
    p_search.add_argument("--page", type=int, metavar="INT", help="Page number")
    p_search.add_argument("--page-size", type=int, metavar="INT", help="Page size")
    p_search.add_argument("--sort", metavar="FIELD", help="Sort field")
    p_search.add_argument("--order", choices=["asc", "desc"], help="Sort direction")
    p_search.set_defaults(handler=cmd_search)

    # 14. board
    p_board = sub.add_parser("board", help="Render Kanban board across workflow columns & swimlanes")
    p_board.add_argument("--swimlane", choices=SWIMLANES, default="none", help="Swimlane grouping")
    p_board.add_argument("--category", metavar="CAT", help="Filter by category")
    p_board.add_argument("--priority", metavar="PRIO", help="Filter by priority")
    p_board.add_argument("--assignee", metavar="USER", help="Filter by assignee")
    p_board.add_argument("--sprint", metavar="SPRINT", help="Filter by sprint")
    p_board.add_argument("--tag", metavar="TAG", help="Filter by tag")
    p_board.add_argument("--query", metavar="IQL", help="Filter by iQL or search query")
    p_board.set_defaults(handler=cmd_board)

    # 15. tag
    p_tag = sub.add_parser("tag", help="Manage colored project tags")
    tsub = p_tag.add_subparsers(dest="tag_action", required=True)
    p_tc = tsub.add_parser("create", help="Create a colored tag")
    p_tc.add_argument("name", help="Tag name")
    p_tc.add_argument("--color", metavar="HEX", help="Hex color (e.g. #ef4444)")
    p_tc.add_argument("--desc", metavar="TEXT", help="Tag description")
    tsub.add_parser("list", help="List tags")
    p_te = tsub.add_parser("edit", help="Edit or rename tag across workspace")
    p_te.add_argument("name", help="Current tag name")
    p_te.add_argument("--new-name", metavar="NAME", help="New tag name")
    p_te.add_argument("--color", metavar="HEX", help="New hex color")
    p_te.add_argument("--desc", metavar="TEXT", help="New description")
    p_td = tsub.add_parser("delete", help="Delete tag and scrub from issues")
    p_td.add_argument("name", help="Tag name")
    p_td.add_argument("--yes", action="store_true", help="Confirm tag deletion")
    p_tag.set_defaults(handler=cmd_tag)

    # 16. sprint
    p_spr = sub.add_parser("sprint", help="Manage Agile sprints & incomplete issue rollover")
    spsub = p_spr.add_subparsers(dest="sprint_action", required=True)
    p_sc = spsub.add_parser("create", help="Create a sprint")
    p_sc.add_argument("name", help="Sprint name")
    p_sc.add_argument("--goal", metavar="TEXT", help="Sprint goal")
    p_sc.add_argument("--start", metavar="YYYY-MM-DD", help="Start date")
    p_sc.add_argument("--end", metavar="YYYY-MM-DD", help="End date")
    p_sl = spsub.add_parser("list", help="List sprints")
    p_sl.add_argument("--state", choices=["planned", "active", "completed"], help="Filter by sprint state")
    p_ss = spsub.add_parser("show", help="Show sprint details and assigned issues")
    p_ss.add_argument("sprint", help="Sprint ID (SPR-1) or name")
    p_se = spsub.add_parser("edit", help="Edit sprint details")
    p_se.add_argument("sprint", help="Sprint ID or name")
    p_se.add_argument("--name", metavar="NAME", help="New sprint name")
    p_se.add_argument("--goal", metavar="TEXT", help="New sprint goal")
    p_se.add_argument("--start", metavar="YYYY-MM-DD", help="New start date")
    p_se.add_argument("--end", metavar="YYYY-MM-DD", help="New end date")
    p_sgo = spsub.add_parser("start", help="Start a planned sprint")
    p_sgo.add_argument("sprint", help="Sprint ID or name")
    p_sgo.add_argument("--start", metavar="YYYY-MM-DD", help="Start date")
    p_sgo.add_argument("--end", metavar="YYYY-MM-DD", help="End date")
    p_sdone = spsub.add_parser("complete", help="Complete active sprint with optional rollover")
    p_sdone.add_argument("sprint", help="Sprint ID or name")
    p_sdone.add_argument("--rollover", metavar="SPRINT_OR_NONE", help="Target sprint for incomplete issues (or none)")
    p_sasn = spsub.add_parser("assign", help="Assign issues to sprint")
    p_sasn.add_argument("sprint", help="Sprint ID or name")
    p_sasn.add_argument("issues", nargs="+", help="Issue keys or names")
    p_srem = spsub.add_parser("remove", help="Remove issues from sprint")
    p_srem.add_argument("sprint", help="Sprint ID or name")
    p_srem.add_argument("issues", nargs="+", help="Issue keys or names")
    p_sdel = spsub.add_parser("delete", help="Delete sprint")
    p_sdel.add_argument("sprint", help="Sprint ID or name")
    p_sdel.add_argument("--yes", action="store_true", help="Confirm sprint deletion")
    p_spr.set_defaults(handler=cmd_sprint)

    # 17. milestone
    p_mil = sub.add_parser("milestone", help="Manage release milestones")
    msub = p_mil.add_subparsers(dest="milestone_action", required=True)
    p_mc = msub.add_parser("create", help="Create a milestone")
    p_mc.add_argument("name", help="Milestone name")
    p_mc.add_argument("--desc", metavar="TEXT", help="Milestone description")
    p_mc.add_argument("--due", metavar="YYYY-MM-DD", help="Due date")
    p_ml = msub.add_parser("list", help="List milestones")
    p_ml.add_argument("--state", choices=["open", "closed", "all"], help="Filter by state")
    p_ms = msub.add_parser("show", help="Show milestone details")
    p_ms.add_argument("milestone", help="Milestone ID (MIL-1) or name")
    p_me = msub.add_parser("edit", help="Edit milestone")
    p_me.add_argument("milestone", help="Milestone ID or name")
    p_me.add_argument("--name", metavar="NAME", help="New milestone name")
    p_me.add_argument("--desc", metavar="TEXT", help="New description")
    p_me.add_argument("--due", metavar="YYYY-MM-DD", help="New due date")
    p_mclose = msub.add_parser("close", help="Close milestone")
    p_mclose.add_argument("milestone", help="Milestone ID or name")
    p_mreopen = msub.add_parser("reopen", help="Reopen milestone")
    p_mreopen.add_argument("milestone", help="Milestone ID or name")
    p_mdel = msub.add_parser("delete", help="Delete milestone")
    p_mdel.add_argument("milestone", help="Milestone ID or name")
    p_mdel.add_argument("--yes", action="store_true", help="Confirm milestone deletion")
    p_mil.set_defaults(handler=cmd_milestone)

    # 18. view
    p_vw = sub.add_parser("view", help="Manage named Saved Views")
    vsub = p_vw.add_subparsers(dest="view_action", required=True)
    p_vc = vsub.add_parser("create", help="Create a named Saved View")
    p_vc.add_argument("name", help="Saved view name")
    p_vc.add_argument("--query", required=True, metavar="IQL", help="Saved iQL or search query")
    p_vc.add_argument("--tab", metavar="TAB", help="Preset tab")
    p_vc.add_argument("--sort", metavar="FIELD", help="Sort field")
    p_vc.add_argument("--order", choices=["asc", "desc"], help="Sort direction")
    p_vc.add_argument("--swimlane", choices=SWIMLANES, help="Board swimlane")
    p_vc.add_argument("--desc", metavar="TEXT", help="View description")
    vsub.add_parser("list", help="List saved views")
    p_vs = vsub.add_parser("show", help="Show saved view definition")
    p_vs.add_argument("view", help="View ID (VIEW-1) or name")
    p_vr = vsub.add_parser("run", help="Execute saved view and return matching issues")
    p_vr.add_argument("view", help="View ID or name")
    p_vr.add_argument("--page", type=int, metavar="INT", help="Page number")
    p_vr.add_argument("--page-size", type=int, metavar="INT", help="Page size")
    p_ve = vsub.add_parser("edit", help="Edit saved view")
    p_ve.add_argument("view", help="View ID or name")
    p_ve.add_argument("--name", metavar="NAME", help="New view name")
    p_ve.add_argument("--query", metavar="IQL", help="New query")
    p_ve.add_argument("--tab", metavar="TAB", help="New tab")
    p_ve.add_argument("--sort", metavar="FIELD", help="New sort field")
    p_ve.add_argument("--order", choices=["asc", "desc"], help="New sort direction")
    p_ve.add_argument("--swimlane", choices=SWIMLANES, help="New swimlane")
    p_ve.add_argument("--desc", metavar="TEXT", help="New description")
    p_vd = vsub.add_parser("delete", help="Delete saved view")
    p_vd.add_argument("view", help="View ID or name")
    p_vd.add_argument("--yes", action="store_true", help="Confirm view deletion")
    p_vw.set_defaults(handler=cmd_view)

    # 19. worklog
    p_wl = sub.add_parser("worklog", help="Track logged work time on issues")
    wlsub = p_wl.add_subparsers(dest="worklog_action", required=True)
    p_wa = wlsub.add_parser("add", help="Log work duration (e.g. '1h 30m', '45m', or '90')")
    p_wa.add_argument("issue", help="Issue key or exact name")
    p_wa.add_argument("duration_or_minutes", help="Duration string or integer minutes")
    p_wa.add_argument("--author", metavar="USER", help="Worklog author")
    p_wa.add_argument("--note", metavar="TEXT", help="Worklog note")
    p_wl_list = wlsub.add_parser("list", help="List worklogs and time progress bar")
    p_wl_list.add_argument("issue", help="Issue key or exact name")
    p_wd = wlsub.add_parser("delete", help="Delete worklog entry")
    p_wd.add_argument("issue", help="Issue key or exact name")
    p_wd.add_argument("worklog_id", help="Worklog ID (e.g. WL-1)")
    p_wd.add_argument("--yes", action="store_true", help="Confirm worklog deletion")
    p_wl.set_defaults(handler=cmd_worklog)

    # 20. history
    p_hist = sub.add_parser("history", help="Show chronological audit trail & field diffs for an issue")
    p_hist.add_argument("issue", help="Issue key or exact name")
    p_hist.set_defaults(handler=cmd_history)

    # 21. bulk
    p_bulk = sub.add_parser("bulk", help="Batch update or delete issues by explicit list or iQL query")
    bsub = p_bulk.add_subparsers(dest="bulk_action", required=True)
    p_bu = bsub.add_parser("update", help="Bulk update fields across matching issues")
    p_bu.add_argument("--issues", nargs="+", action="extend", metavar="CSV_OR_LIST", help="Issue keys or names")
    p_bu.add_argument("--query", metavar="IQL", help="iQL selector query")
    p_bu.add_argument("--state", metavar="STATE", help="Set state")
    p_bu.add_argument("--priority", metavar="PRIO", help="Set priority")
    p_bu.add_argument("--severity", metavar="SEV", help="Set severity")
    p_bu.add_argument("--category", metavar="CAT", help="Set category")
    p_bu.add_argument("--assignee", metavar="USER", help="Set assignee")
    p_bu.add_argument("--clear-assignee", action="store_true", help="Clear assignee")
    p_bu.add_argument("--add-tag", nargs="+", action="extend", metavar="TAG", help="Add tag(s)")
    p_bu.add_argument("--remove-tag", nargs="+", action="extend", metavar="TAG", help="Remove tag(s)")
    p_bu.add_argument("--sprint", metavar="SPRINT", help="Assign to sprint")
    p_bu.add_argument("--clear-sprint", action="store_true", help="Clear sprint")
    p_bu.add_argument("--milestone", metavar="MIL", help="Assign to milestone")
    p_bu.add_argument("--clear-milestone", action="store_true", help="Clear milestone")
    p_bu.add_argument("--archive", action="store_true", help="Archive matched issues")
    p_bu.add_argument("--unarchive", action="store_true", help="Unarchive matched issues")

    p_bd = bsub.add_parser("delete", help="Bulk delete matching issues")
    p_bd.add_argument("--issues", nargs="+", action="extend", metavar="CSV_OR_LIST", help="Issue keys or names")
    p_bd.add_argument("--query", metavar="IQL", help="iQL selector query")
    p_bd.add_argument("--yes", action="store_true", help="Confirm bulk deletion")
    p_bulk.set_defaults(handler=cmd_bulk)

    # 22. stats
    sub.add_parser("stats", help="Show workspace analytics, velocity & distributions").set_defaults(handler=cmd_stats)

    return parser


# ---------------------------------------------------------------------------
# 100% Drift-Proof Self-Teaching Zero-Arg Manual (Programmatic AST Walk)
# ---------------------------------------------------------------------------


def collect_parser_catalog(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """Walk `build_parser()` recursively to extract 100% of subcommands, args & flags."""
    catalog: list[dict[str, Any]] = []

    def _walk(p: argparse.ArgumentParser, prefix: list[str]) -> None:
        pos_args: list[str] = []
        opt_entries: list[dict[str, Any]] = []
        sub_action: argparse._SubParsersAction | None = None

        for act in p._actions:
            if isinstance(act, argparse._SubParsersAction):
                sub_action = act
                continue
            if act.option_strings:
                opt_entries.append({
                    "flags": list(act.option_strings),
                    "metavar": act.metavar,
                    "help": act.help or "",
                    "required": bool(getattr(act, "required", False)),
                })
            else:
                pos_args.append(act.dest)

        cmd_path = " ".join(prefix)
        catalog.append({
            "command": cmd_path,
            "description": p.description or "",
            "positionals": pos_args,
            "options": opt_entries,
            "has_subcommands": sub_action is not None,
        })

        if sub_action is not None:
            seen_ids: set[int] = set()
            for sub_name, sub_parser in sub_action.choices.items():
                if id(sub_parser) in seen_ids:
                    continue
                seen_ids.add(id(sub_parser))
                _walk(sub_parser, prefix + [sub_name])

    _walk(parser, [])
    return catalog


def generate_manual_markdown(parser: argparse.ArgumentParser, repo_root: Path) -> str:
    catalog = collect_parser_catalog(parser)
    lines: list[str] = [
        f"# itrack v{VERSION} — Complete Self-Teaching Agent & Operator Manual",
        "",
        f"- **Repository Root**: `{repo_root}`",
        f"- **Canonical Storage**: `{repo_root}/itrack-issues/` (Git-tracked folder-per-issue JSON + gitignored `.runtime/` SQLite FTS5 index & daemon state)",
        "- **Dependencies & Setup**: Install runtime and test dependencies via `pip install -r itrack-requirements.txt`.",
        "- **Dual Issue Addressing**: Every `<issue>` parameter accepts **either** the immutable key (`ITR-1`) **or** the exact issue folder name with spaces (`\"OAuth2 Token Refresh Race\"`).",
        "- **Global Position-Independent Flags**: `--json` (pure JSON stdout), `--root <PATH>` (target Git root), `-V` / `--version`, `-h` / `--help`.",
        "",
        "## 1. Runnable Quickstart Workflow",
        "",
        "```bash",
        'python itrack.py init --prefix ITR --title "Platform Core"',
        'python itrack.py tag create security --color "#ef4444" --desc "Security items"',
        'python itrack.py sprint create "Sprint 1" --goal "Auth hardening" --start 2026-11-01 --end 2026-11-14',
        "python itrack.py sprint start SPR-1",
        'python itrack.py milestone create "v2.0" --desc "Major release" --due 2026-12-01',
        'python itrack.py issue create "OAuth2 Token Refresh Race" --category bug --state todo --priority critical --severity sev1 --desc "Race under concurrent 401s" --ac "Single refresh flight" --tag security --sprint SPR-1 --milestone MIL-1 --points 5 --estimate 120 --due 2026-11-30 --field env=prod',
        'python itrack.py issue create "Add Retry Backoff Tests" --category subtask --parent ITR-1 --priority high',
        'python itrack.py issue edit "OAuth2 Token Refresh Race" --state in-progress --assignee alice --check-ac AC-1',
        'python itrack.py comment add ITR-1 --body "Reproduced race condition under load" --author alice',
        "python itrack.py link add ITR-1 blocks ITR-2",
        'python itrack.py worklog add ITR-1 45m --author alice --note "Root cause analysis"',
        "python itrack.py issue list --tab active",
        'python itrack.py search "priority = critical AND state != done ORDER BY priority DESC" --iql',
        "python itrack.py board --swimlane priority",
        "python itrack.py tree ITR-1",
        'python itrack.py view create "Critical Active" --query "priority = critical" --tab active',
        "python itrack.py view run VIEW-1",
        "python itrack.py issue context ITR-1",
        "python itrack.py history ITR-1",
        "python itrack.py stats",
        "python itrack.py status",
        "python itrack.py server stop",
        "```",
        "",
        "## 2. Canonical Enums",
        "",
        f"- **Categories (13)**: {', '.join(f'`{x}`' for x in CATEGORIES)}",
        f"- **Workflow States (12)**: {', '.join(f'`{x}`' for x in STATES)} *(spaced inputs like `\"in progress\"` normalize automatically)*",
        f"- **Priorities (5)**: {', '.join(f'`{x}`' for x in PRIORITIES)}",
        f"- **Severities (5)**: {', '.join(f'`{x}`' for x in SEVERITIES)}",
        f"- **Resolutions (6)**: {', '.join(f'`{x}`' for x in RESOLUTIONS)}",
        f"- **Link Types (6)**: {', '.join(f'`{x}`' for x in LINK_TYPES)}",
        f"- **Preset Filter Tabs (8)**: {', '.join(f'`{x}`' for x in PRESET_TABS)}",
        f"- **Kanban Swimlanes (5)**: {', '.join(f'`{x}`' for x in SWIMLANES)}",
        "",
        "## 3. iQL (iTrack Query Language) & FTS5 Search",
        "",
        "- **Comparison & Match Operators**: `=`, `!=`, `>`, `>=`, `<`, `<=`, `~`, `!~`, `IN`, `NOT IN`, `IS EMPTY`, `IS NOT EMPTY`",
        "- **Logical & Ordering Clauses**: `AND`, `OR`, `NOT`, `ORDER BY` (`ASC` / `DESC`)",
        "- **Queryable Fields**: `key`, `name` (`title`), `category` (`type`), `state` (`status`), `priority`, `severity`, `resolution`, `assignee`, `reporter`, `tag` (`tags`), `component` (`components`), `parent`, `sprint`, `milestone`, `points` (`story_points`), `estimate` (`estimate_minutes`), `logged` (`logged_minutes`), `due` (`due_date`), `archived`, `blocked`, `overdue`, `text` (FTS5 across title, description, comments, and attachment names).",
        "",
        "## 4. Exit Codes",
        "",
    ]

    for code, desc in EXIT_CODE_DOCS.items():
        lines.append(f"- **`{code}`**: {desc}")

    lines.extend([
        "",
        "## 5. Complete CLI Subcommand & Flag Reference",
        "",
    ])

    for entry in catalog:
        cmd = entry["command"]
        heading = "`python itrack.py` (Global Options)" if not cmd else f"`python itrack.py {cmd}`"
        pos_str = " ".join(f"<{p}>" for p in entry["positionals"])
        sig = f"python itrack.py {cmd} {pos_str}".strip() if cmd else "python itrack.py [--json] [--root PATH] [-V|--version] [-h|--help]"
        lines.append(f"### {heading}")
        lines.append(f"- Usage: `{sig}`")
        for opt in entry["options"]:
            flag_str = ", ".join(f"`{f}`" for f in opt["flags"])
            req = " *(required)*" if opt["required"] else ""
            lines.append(f"  - {flag_str}{req}: {opt['help']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_manual_payload(parser: argparse.ArgumentParser, repo_root: Path) -> dict[str, Any]:
    catalog = collect_parser_catalog(parser)
    return {
        "name": "itrack",
        "version": VERSION,
        "repo_root": str(repo_root),
        "setup": "pip install -r itrack-requirements.txt",
        "enums": {
            "categories": CATEGORIES,
            "states": STATES,
            "priorities": PRIORITIES,
            "severities": SEVERITIES,
            "resolutions": RESOLUTIONS,
            "link_types": LINK_TYPES,
            "preset_tabs": PRESET_TABS,
            "swimlanes": SWIMLANES,
        },
        "iql_operators": IQL_OPERATORS,
        "exit_codes": {str(k): v for k, v in EXIT_CODE_DOCS.items()},
        "commands": [
            {
                "command": c["command"],
                "positionals": c["positionals"],
                "flags": [f for opt in c["options"] for f in opt["flags"]],
            }
            for c in catalog
        ],
        "markdown": generate_manual_markdown(parser, repo_root),
    }


def render_zero_arg_manual(parser: argparse.ArgumentParser, repo_root: Path, json_mode: bool) -> None:
    if json_mode:
        emit_json(build_manual_payload(parser, repo_root))
    else:
        sys.stdout.write(generate_manual_markdown(parser, repo_root))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    prelim_json = "--json" in raw_argv
    parser = build_parser()

    try:
        json_mode, root_arg, remaining, cmd_path, used_flags = extract_global_flags_and_surface(raw_argv, parser)
    except CLIError as err:
        record_cli_surface("", ["--json"] if prelim_json else [])
        handle_cli_error(err, prelim_json)

    record_cli_surface(cmd_path, used_flags)

    # Law 2: Enforce Git repository root (exits 2 if outside Git root or inside subdirectory).
    repo_root = resolve_and_check_git_root(root_arg, json_mode)

    # Zero-arg manual (does not require init and never spawns daemon).
    if not remaining:
        render_zero_arg_manual(parser, repo_root, json_mode)
        return

    # Handle -V / --version prior to subcommand validation & init check.
    if remaining in (["-V"], ["--version"]):
        if json_mode:
            emit_json({"name": "itrack", "version": VERSION})
        else:
            sys.stdout.write(f"itrack {VERSION}\n")
            sys.stdout.flush()
        return

    try:
        args = parser.parse_args(remaining)
        if getattr(args, "version", False):
            if json_mode:
                emit_json({"name": "itrack", "version": VERSION})
            else:
                sys.stdout.write(f"itrack {VERSION}\n")
                sys.stdout.flush()
            return

        if not getattr(args, "command", None):
            render_zero_arg_manual(parser, repo_root, json_mode)
            return

        # Exit 3 on any command other than zero-arg manual, --help, --version, and init
        if args.command != "init":
            ensure_workspace_initialized(repo_root)

        handler = getattr(args, "handler", None)
        if handler is None:
            render_zero_arg_manual(parser, repo_root, json_mode)
            return
        handler(args, repo_root, json_mode)
    except CLIError as err:
        handle_cli_error(err, json_mode)


if __name__ == "__main__":
    main()
