#!/usr/bin/env python3
"""
itrack-server.py — Local-First Folder-Per-Issue Tracker, Self-Healing SQLite/FTS5 Index,
iQL Engine & Air-Gapped Offline Web UI Daemon.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import uvicorn
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

VERSION = "2.0.0"

# ============================================================================
# Section 1: Canonical Enums, Ranks & Constants
# ============================================================================

CATEGORIES: List[str] = [
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

STATES: List[str] = [
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

PRIORITIES: List[str] = ["critical", "high", "medium", "low", "trivial"]
PRIORITY_RANKS: Dict[str, int] = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "trivial": 1,
}

SEVERITIES: List[str] = ["sev1", "sev2", "sev3", "sev4", "sev5"]
SEVERITY_RANKS: Dict[str, int] = {
    "sev1": 5,
    "sev2": 4,
    "sev3": 3,
    "sev4": 2,
    "sev5": 1,
}

RESOLUTIONS: List[str] = [
    "fixed",
    "wont-fix",
    "duplicate",
    "cannot-reproduce",
    "by-design",
    "obsolete",
]

LINK_TYPES: List[str] = [
    "blocks",
    "blocked-by",
    "relates-to",
    "duplicates",
    "parent-of",
    "clones",
]

INVERSE_LINK_TYPES: Dict[str, str] = {
    "blocks": "blocked-by",
    "blocked-by": "blocks",
    "relates-to": "relates-to",
    "duplicates": "duplicated-by",
    "duplicated-by": "duplicates",
    "parent-of": "child-of",
    "child-of": "parent-of",
    "clones": "cloned-from",
    "cloned-from": "clones",
}

PRESET_TABS: List[str] = [
    "active",
    "open",
    "in-progress",
    "blocked",
    "completed",
    "cancelled",
    "archived",
    "all",
]

SWIMLANES: List[str] = ["none", "category", "priority", "assignee", "sprint"]

TERMINAL_STATES: Set[str] = {"done", "cancelled", "archived"}

GITIGNORE_CONTENT = ".runtime/\nindex.sqlite*\n*.tmp\n"

IMAGE_EXTENSIONS: Set[str] = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
TEXT_PREVIEW_EXTENSIONS: Set[str] = {
    ".txt", ".log", ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".svg", ".xml", ".csv", ".sql",
    ".sh", ".bash", ".diff", ".patch", ".env", ".rst",
}


# ============================================================================
# Section 2: Normalization, Validation & Pre-Flight Helpers
# ============================================================================

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def _hyphenate(raw: str) -> str:
    s = raw.strip().lower().replace("_", "-")
    return re.sub(r"\s+", "-", s)


def normalize_category(val: Optional[str], default: Optional[str] = None) -> str:
    raw = _hyphenate(val if val is not None else (default or "task"))
    if raw not in CATEGORIES:
        raise ValueError(f"Invalid category '{val}'. Allowed: {', '.join(CATEGORIES)}.")
    return raw


def normalize_state(val: Optional[str], default: Optional[str] = None) -> str:
    raw = _hyphenate(val if val is not None else (default or "backlog"))
    if raw == "canceled":
        raw = "cancelled"
    if raw not in STATES:
        raise ValueError(f"Invalid state '{val}'. Allowed: {', '.join(STATES)}.")
    return raw


def normalize_priority(val: Optional[str], default: Optional[str] = None) -> Tuple[str, int]:
    raw = _hyphenate(val if val is not None else (default or "medium"))
    if raw not in PRIORITY_RANKS:
        raise ValueError(f"Invalid priority '{val}'. Allowed: {', '.join(PRIORITIES)}.")
    return raw, PRIORITY_RANKS[raw]


def normalize_severity(val: Optional[str], default: Optional[str] = None) -> Tuple[str, int]:
    raw = _hyphenate(val if val is not None else (default or "sev3"))
    if raw not in SEVERITY_RANKS:
        raise ValueError(f"Invalid severity '{val}'. Allowed: {', '.join(SEVERITIES)}.")
    return raw, SEVERITY_RANKS[raw]


def normalize_resolution(val: Optional[Any]) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "unset", "clear"):
        return None
    norm = _hyphenate(s)
    if norm == "wontfix":
        norm = "wont-fix"
    if norm not in RESOLUTIONS:
        raise ValueError(f"Invalid resolution '{val}'. Allowed: {', '.join(RESOLUTIONS)}.")
    return norm


def normalize_link_type(val: Optional[str]) -> str:
    if not val or not str(val).strip():
        raise ValueError(f"Link type is required. Allowed: {', '.join(LINK_TYPES)}.")
    norm = _hyphenate(str(val))
    if norm not in LINK_TYPES:
        raise ValueError(f"Invalid link type '{val}'. Allowed: {', '.join(LINK_TYPES)}.")
    return norm


def normalize_tab(val: Optional[str]) -> str:
    if not val or not str(val).strip():
        return "all"
    norm = _hyphenate(str(val))
    if norm == "canceled":
        norm = "cancelled"
    if norm not in PRESET_TABS:
        raise ValueError(f"Invalid tab '{val}'. Allowed: {', '.join(PRESET_TABS)}.")
    return norm


def normalize_swimlane(val: Optional[str]) -> str:
    if not val or not str(val).strip():
        return "none"
    norm = _hyphenate(str(val))
    if norm not in SWIMLANES:
        raise ValueError(f"Invalid swimlane '{val}'. Allowed: {', '.join(SWIMLANES)}.")
    return norm


def validate_issue_name(raw_name: Any) -> str:
    if raw_name is None or not isinstance(raw_name, str):
        raise ValueError("Issue name is required.")
    name = raw_name.strip()
    if not name:
        raise ValueError("Issue name cannot be empty.")
    if len(name) > 180:
        raise ValueError("Issue name must be at most 180 characters.")
    if name in (".", "..") or ".." in name:
        raise ValueError("Issue name cannot contain '..' path traversal sequences.")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Issue name cannot contain path separators ('/' or '\\') or null bytes.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("Issue name cannot contain control characters.")
    if name.startswith(".rename_tmp_"):
        raise ValueError("Issue name uses reserved prefix.")
    return name


def validate_calendar_date(val: Any, field_name: str = "date") -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "clear", ""):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"Invalid {field_name} '{val}'. Expected YYYY-MM-DD.")
    try:
        dt.date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar {field_name} '{val}': {exc}") from exc
    return s


def validate_story_points(val: Any) -> Optional[float]:
    if val is None or (isinstance(val, str) and val.strip().lower() in ("", "none", "null", "clear")):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid story_points '{val}'. Must be a non-negative number.") from exc
    if num < 0 or num != num or num in (float("inf"), float("-inf")):
        raise ValueError(f"story_points must be >= 0 (got {val}).")
    return round(num, 2)


def validate_estimate_minutes(val: Any) -> Optional[int]:
    if val is None or (isinstance(val, str) and val.strip().lower() in ("", "none", "null", "clear")):
        return None
    if isinstance(val, str) and re.search(r"[wdhm]", val.lower()):
        mins = parse_duration_minutes(None, val)
        return mins
    try:
        fv = float(val)
        if not fv.is_integer():
            raise ValueError("estimate_minutes must be an integer.")
        iv = int(fv)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid estimate_minutes '{val}'. Must be a non-negative integer.") from exc
    if iv < 0:
        raise ValueError(f"estimate_minutes must be >= 0 (got {val}).")
    return iv


def parse_duration_minutes(minutes: Optional[Any], duration: Optional[Any]) -> int:
    if minutes is not None and str(minutes).strip() != "":
        try:
            m_int = int(float(minutes))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid worklog minutes '{minutes}'.") from exc
        if m_int <= 0:
            raise ValueError("Worklog minutes must be > 0.")
        return m_int
    if duration is None or not str(duration).strip():
        raise ValueError("Provide positive 'minutes' or 'duration' (e.g. '1h 30m', '45m', '90').")
    s = str(duration).strip().lower()
    if re.fullmatch(r"[+-]?\d+", s):
        val = int(s)
        if val <= 0:
            raise ValueError("Worklog duration must be > 0.")
        return val
    tokens = re.findall(r"(\d+(?:\.\d+)?)\s*([wdhm])", s)
    if not tokens:
        raise ValueError(f"Invalid duration '{duration}'. Expected e.g. '1h 30m', '2d', '45m', or '90'.")
    rem = re.sub(r"(\d+(?:\.\d+)?)\s*([wdhm])", "", s).strip()
    if rem:
        raise ValueError(f"Unexpected text '{rem}' in duration '{duration}'.")
    mult = {"w": 5 * 8 * 60, "d": 8 * 60, "h": 60, "m": 1}
    total = int(round(sum(float(num) * mult[u] for num, u in tokens)))
    if total <= 0:
        raise ValueError("Worklog duration must be > 0 minutes.")
    return total


def sanitize_filename(raw_name: Optional[str]) -> Tuple[str, str]:
    """Returns (display_filename, safe_stored_suffix) preserving spaces safely."""
    raw = (raw_name or "attachment.bin").replace("\\", "/").split("/")[-1]
    raw = "".join(ch for ch in raw if ord(ch) >= 32 and ch not in ('\x00', '/', '\\'))
    display_name = raw.strip() or "attachment.bin"
    if display_name in (".", ".."):
        display_name = "attachment.bin"
    display_name = display_name[:160]
    safe_suffix = re.sub(r"[^A-Za-z0-9._ ()\[\]+-]", "_", display_name).strip(" .")
    if not safe_suffix or ".." in safe_suffix:
        safe_suffix = "attachment.bin"
    return display_name, safe_suffix[:160]


def is_image_file(filename: str, mime_type: str) -> bool:
    if mime_type and mime_type.lower().startswith("image/"):
        return True
    ext = Path(filename).suffix.lower()
    return ext in IMAGE_EXTENSIONS


def is_text_previewable(filename: str, mime_type: str) -> bool:
    m = (mime_type or "").lower()
    if m.startswith("text/") or m in (
        "application/json",
        "application/javascript",
        "application/xml",
        "image/svg+xml",
        "application/x-yaml",
    ):
        return True
    return Path(filename).suffix.lower() in TEXT_PREVIEW_EXTENSIONS


def detect_default_user(repo_root: Path) -> str:
    env_u = os.environ.get("ITRACK_USER")
    if env_u and env_u.strip():
        return env_u.strip()
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return os.environ.get("USER") or "user"


def resolve_raw_root_arg(cli_root: Optional[str]) -> Path:
    if cli_root:
        return Path(cli_root).expanduser()
    env_root = os.environ.get("ITRACK_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    return Path.cwd()


def check_and_resolve_git_root(raw_path: Path) -> Path:
    """
    Law 2: Accept a Git root directory, a Git worktree root (.git file), or a symlink
    pointing directly to a Git root. Reject non-Git dirs and Git subdirectories with exit code 2.
    """
    try:
        abs_raw = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path)
    except Exception:
        abs_raw = raw_path

    has_git_direct = False
    has_git_resolved = False
    try:
        has_git_direct = (abs_raw / ".git").exists()
    except Exception:
        has_git_direct = False
    try:
        resolved = abs_raw.resolve()
        has_git_resolved = (resolved / ".git").exists()
    except Exception:
        resolved = abs_raw
        has_git_resolved = False

    if has_git_direct or has_git_resolved:
        return resolved

    msg = (
        f"Error [Law 2 - Git-Root Check]: '{raw_path}' is not a Git repository root "
        f"(.git directory or worktree file not found at target root)."
    )
    try:
        if resolved.exists():
            proc = subprocess.run(
                ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                top = Path(proc.stdout.strip()).resolve()
                if top != resolved:
                    msg += f"\nHint: Run from repository root '{top}' (not a subdirectory)."
    except Exception:
        pass
    print(msg, file=sys.stderr, flush=True)
    sys.exit(2)


def default_config_dict(prefix: str = "ITR", title: str = "iTrack Workspace") -> Dict[str, Any]:
    clean_prefix = re.sub(r"[^A-Z0-9]", "", (prefix or "ITR").strip().upper()) or "ITR"
    if not clean_prefix[0].isalpha():
        clean_prefix = "ITR"
    clean_title = (title or "iTrack Workspace").strip() or "iTrack Workspace"
    ts = now_iso()
    return {
        "project_prefix": clean_prefix,
        "project_title": clean_title,
        "default_state": "backlog",
        "default_priority": "medium",
        "default_category": "task",
        "default_severity": "sev3",
        "default_assignee": None,
        "wip_limits": {},
        "preferences": {
            "theme": "dark",
            "page_size": 25,
            "default_swimlane": "none",
        },
        "next_issue_seq": 1,
        "next_att_seq": 1,
        "next_comment_seq": 1,
        "next_link_seq": 1,
        "next_worklog_seq": 1,
        "next_history_seq": 1,
        "next_sprint_seq": 1,
        "next_milestone_seq": 1,
        "next_view_seq": 1,
        "created_at": ts,
        "updated_at": ts,
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.{short_id()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
    os.replace(tmp, path)


def _atomic_write_json_standalone(path: Path, obj: Any) -> None:
    payload = (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


def init_workspace_on_disk(
    repo_root: Path,
    prefix: str = "ITR",
    title: str = "iTrack Workspace",
) -> Dict[str, Any]:
    """Idempotently initialize <repo_root>/itrack-issues/ layout and build index.sqlite."""
    issues_root = repo_root / "itrack-issues"
    runtime_dir = issues_root / ".runtime"
    issues_dir = issues_root / "issues"
    for d in (issues_root, runtime_dir, issues_dir):
        d.mkdir(parents=True, exist_ok=True)

    lock_path = runtime_dir / "storage.lock"
    with open(lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            gitignore_path = issues_root / ".gitignore"
            if not gitignore_path.exists() or gitignore_path.read_text(encoding="utf-8", errors="ignore") != GITIGNORE_CONTENT:
                _atomic_write_bytes(gitignore_path, GITIGNORE_CONTENT.encode("utf-8"))

            cfg_path = issues_root / "config.json"
            already_existed = cfg_path.exists()
            if not already_existed:
                cfg = default_config_dict(prefix=prefix, title=title)
                _atomic_write_json_standalone(cfg_path, cfg)
            else:
                with open(cfg_path, "r", encoding="utf-8") as cf:
                    cfg = json.load(cf)
                changed = False
                defaults = default_config_dict(prefix=cfg.get("project_prefix", prefix), title=cfg.get("project_title", title))
                for k, v in defaults.items():
                    if k not in cfg:
                        cfg[k] = v
                        changed = True
                if changed:
                    _atomic_write_json_standalone(cfg_path, cfg)

            for fname, root_key in (
                ("tags.json", "tags"),
                ("sprints.json", "sprints"),
                ("milestones.json", "milestones"),
                ("views.json", "views"),
            ):
                fpath = issues_root / fname
                if not fpath.exists():
                    _atomic_write_json_standalone(fpath, {root_key: []})
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    engine = StorageEngine(repo_root)
    engine.reindex()
    engine.close()
    return {
        "initialized": True,
        "already_initialized": already_existed,
        "repo_root": str(repo_root),
        "issues_root": str(issues_root),
        "config": cfg,
    }


def record_surface_event(event: Dict[str, Any]) -> None:
    log_path = os.environ.get("ITRACK_SURFACE_LOG")
    if not log_path:
        return
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


# ============================================================================
# Section 3: Recursive-Descent iQL Engine (Lexer -> AST -> SQLite + FTS5)
# ============================================================================

class IQLError(Exception):
    def __init__(self, message: str, position: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.position = position


class Token:
    __slots__ = ("kind", "value", "pos", "quoted")

    def __init__(self, kind: str, value: str, pos: int, quoted: bool = False) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos
        self.quoted = quoted


IQL_KEYWORDS: Set[str] = {
    "AND", "OR", "NOT", "IN", "IS", "EMPTY", "NULL", "ORDER", "BY", "ASC", "DESC",
}

FIELD_ALIASES: Dict[str, str] = {
    "key": "key",
    "id": "key",
    "seq": "seq",
    "name": "name",
    "title": "name",
    "summary": "name",
    "category": "category",
    "type": "category",
    "state": "state",
    "status": "state",
    "priority": "priority",
    "severity": "severity",
    "resolution": "resolution",
    "assignee": "assignee",
    "owner": "assignee",
    "reporter": "reporter",
    "creator": "reporter",
    "tag": "tags",
    "tags": "tags",
    "label": "tags",
    "labels": "tags",
    "component": "components",
    "components": "components",
    "parent": "parent",
    "epic": "parent",
    "sprint": "sprint",
    "milestone": "milestone",
    "points": "story_points",
    "story_points": "story_points",
    "estimate": "estimate_minutes",
    "estimate_minutes": "estimate_minutes",
    "logged": "logged_minutes",
    "logged_minutes": "logged_minutes",
    "due": "due_date",
    "due_date": "due_date",
    "created": "created_at",
    "created_at": "created_at",
    "updated": "updated_at",
    "updated_at": "updated_at",
    "archived": "archived",
    "blocked": "is_blocked",
    "is_blocked": "is_blocked",
    "overdue": "is_overdue",
    "is_overdue": "is_overdue",
    "description": "description",
    "body": "description",
    "ac": "ac_text",
    "acceptance_criteria": "ac_text",
    "comment": "comments_text",
    "comments": "comments_text",
    "attachment": "attachments_text",
    "attachments": "attachments_text",
    "text": "text",
    "q": "text",
}

ORDER_SQL_MAP: Dict[str, str] = {
    "key": "issues.seq",
    "seq": "issues.seq",
    "name": "LOWER(issues.name)",
    "category": "issues.category",
    "state": "issues.state",
    "priority": "issues.priority_rank",
    "severity": "issues.severity_rank",
    "resolution": "issues.resolution",
    "assignee": "LOWER(issues.assignee)",
    "reporter": "LOWER(issues.reporter)",
    "parent": "issues.parent",
    "sprint": "issues.sprint",
    "milestone": "issues.milestone",
    "story_points": "issues.story_points",
    "estimate_minutes": "issues.estimate_minutes",
    "logged_minutes": "issues.logged_minutes",
    "due_date": "issues.due_date",
    "created_at": "issues.created_at",
    "updated_at": "issues.updated_at",
}


class IQLLexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)

    def tokenize(self) -> List[Token]:
        tokens: List[Token] = []
        s = self.text
        n = self.n
        i = 0
        while i < n:
            ch = s[i]
            if ch in " \t\r\n":
                i += 1
                continue
            two = s[i : i + 2]
            if two in ("!=", ">=", "<=", "!~", "<>"):
                tokens.append(Token("OP", "!=" if two == "<>" else two, i))
                i += 2
                continue
            if ch in ("=", ">", "<", "~"):
                tokens.append(Token("OP", ch, i))
                i += 1
                continue
            if ch == "(":
                tokens.append(Token("LPAREN", ch, i))
                i += 1
                continue
            if ch == ")":
                tokens.append(Token("RPAREN", ch, i))
                i += 1
                continue
            if ch == ",":
                tokens.append(Token("COMMA", ch, i))
                i += 1
                continue
            if ch in ('"', "'"):
                quote = ch
                start = i
                i += 1
                buf: List[str] = []
                closed = False
                while i < n:
                    c = s[i]
                    if c == "\\" and i + 1 < n:
                        nxt = s[i + 1]
                        esc = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
                        buf.append(esc.get(nxt, nxt))
                        i += 2
                        continue
                    if c == quote:
                        i += 1
                        closed = True
                        break
                    buf.append(c)
                    i += 1
                if not closed:
                    raise IQLError(f"Unterminated string literal at position {start}", start)
                tokens.append(Token("STRING", "".join(buf), start, quoted=True))
                continue
            if ch.isalnum() or ch in "_@+-.:#":
                start = i
                i += 1
                while i < n and (s[i].isalnum() or s[i] in "_@+.:/#-"):
                    i += 1
                word = s[start:i]
                up = word.upper()
                if up in IQL_KEYWORDS:
                    tokens.append(Token("KW", up, start))
                else:
                    tokens.append(Token("WORD", word, start))
                continue
            raise IQLError(f"Unexpected character '{ch}' at position {i}", i)
        tokens.append(Token("EOF", "", n))
        return tokens


def resolve_iql_date(raw: str, date_only: bool = False, pos: Optional[int] = None) -> str:
    s = raw.strip()
    low = s.lower()
    now = dt.datetime.now(dt.timezone.utc)
    if low == "now":
        return now.strftime("%Y-%m-%d") if date_only else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if low == "today":
        return now.strftime("%Y-%m-%d") if date_only else now.strftime("%Y-%m-%dT00:00:00Z")
    if low == "yesterday":
        d = now - dt.timedelta(days=1)
        return d.strftime("%Y-%m-%d") if date_only else d.strftime("%Y-%m-%dT00:00:00Z")
    if low == "tomorrow":
        d = now + dt.timedelta(days=1)
        return d.strftime("%Y-%m-%d") if date_only else d.strftime("%Y-%m-%dT00:00:00Z")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([mhdw])", low)
    if m:
        amt = float(m.group(1))
        unit = m.group(2)
        delta = {
            "m": dt.timedelta(minutes=amt),
            "h": dt.timedelta(hours=amt),
            "d": dt.timedelta(days=amt),
            "w": dt.timedelta(weeks=amt),
        }[unit]
        t = now + delta
        return t.strftime("%Y-%m-%d") if date_only else t.strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            dt.date.fromisoformat(s)
        except ValueError as exc:
            raise IQLError(f"Invalid calendar date '{raw}'", pos) from exc
        return s if date_only else f"{s}T00:00:00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", s):
        return s[:10] if date_only else s.replace(" ", "T")
    raise IQLError(f"Invalid date expression '{raw}'", pos)


def build_fts_match_expr(raw: str, column: Optional[str] = None) -> Optional[str]:
    if not raw or not raw.strip():
        return None
    phrases: List[str] = []
    for pm in re.finditer(r'"([^"]+)"', raw):
        inner = pm.group(1).strip()
        if any(c.isalnum() for c in inner):
            safe = inner.replace('"', '""')
            phrases.append(f'"{safe}"')
    rem = re.sub(r'"[^"]+"', " ", raw)
    for tok in rem.split():
        sub_tokens = re.findall(r"[A-Za-z0-9_\u0080-\uffff]+(?:-[A-Za-z0-9_\u0080-\uffff]+)*", tok)
        for st in sub_tokens:
            safe_st = st.replace('"', '""')
            phrases.append(f'"{safe_st}"*')
    if not phrases:
        return None
    joined = " AND ".join(phrases)
    if column:
        return f"{column} : ({joined})"
    return joined


class IQLParser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.idx = 0

    def cur(self) -> Token:
        return self.tokens[self.idx]

    def peek_kw(self, kw: str) -> bool:
        t = self.cur()
        return t.kind == "KW" and t.value == kw

    def consume(self, kind: Optional[str] = None, val: Optional[str] = None) -> Token:
        t = self.cur()
        if kind and t.kind != kind:
            raise IQLError(f"Expected {kind} but got '{t.value or t.kind}' at pos {t.pos}", t.pos)
        if val and t.value != val:
            raise IQLError(f"Expected '{val}' but got '{t.value}' at pos {t.pos}", t.pos)
        self.idx += 1
        return t

    def _resolve_field(self, raw_field: str, pos: int) -> str:
        low = raw_field.lower()
        if low in FIELD_ALIASES:
            return FIELD_ALIASES[low]
        if low.startswith("cf.") or low.startswith("custom.") or low.startswith("custom_fields."):
            sub = raw_field.split(".", 1)[1].strip()
            if not sub:
                raise IQLError(f"Invalid custom field '{raw_field}' at pos {pos}", pos)
            return f"custom.{sub}"
        raise IQLError(f"Unknown iQL field '{raw_field}' at pos {pos}", pos)

    def parse(self) -> Dict[str, Any]:
        where_expr: Optional[Dict[str, Any]] = None
        order_by: List[Dict[str, str]] = []
        if self.cur().kind != "EOF" and not self.peek_kw("ORDER"):
            where_expr = self.parse_or()
        if self.peek_kw("ORDER"):
            self.consume("KW", "ORDER")
            self.consume("KW", "BY")
            order_by.append(self.parse_order_item())
            while self.cur().kind == "COMMA":
                self.consume("COMMA")
                order_by.append(self.parse_order_item())
        if self.cur().kind != "EOF":
            t = self.cur()
            raise IQLError(f"Unexpected token '{t.value}' at pos {t.pos}", t.pos)
        return {"where": where_expr, "order_by": order_by}

    def parse_order_item(self) -> Dict[str, str]:
        tok = self.cur()
        if tok.kind not in ("WORD", "KW", "STRING") or not tok.value:
            raise IQLError(f"Expected field in ORDER BY at pos {tok.pos}", tok.pos)
        self.idx += 1
        canonical = self._resolve_field(tok.value, tok.pos)
        if canonical not in ORDER_SQL_MAP:
            raise IQLError(f"Field '{tok.value}' is not sortable in ORDER BY", tok.pos)
        direction = "ASC"
        if self.peek_kw("ASC"):
            self.consume("KW", "ASC")
        elif self.peek_kw("DESC"):
            self.consume("KW", "DESC")
            direction = "DESC"
        return {"field": canonical, "direction": direction}

    def parse_or(self) -> Dict[str, Any]:
        items = [self.parse_and()]
        while self.peek_kw("OR"):
            self.consume("KW", "OR")
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else {"type": "or", "children": items}

    def parse_and(self) -> Dict[str, Any]:
        items = [self.parse_not()]
        while self.peek_kw("AND"):
            self.consume("KW", "AND")
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else {"type": "and", "children": items}

    def parse_not(self) -> Dict[str, Any]:
        if self.peek_kw("NOT"):
            self.consume("KW", "NOT")
            return {"type": "not", "child": self.parse_not()}
        if self.cur().kind == "LPAREN":
            lp = self.consume("LPAREN")
            if self.cur().kind == "RPAREN":
                raise IQLError(f"Empty parentheses at pos {lp.pos}", lp.pos)
            expr = self.parse_or()
            self.consume("RPAREN")
            return expr
        return self.parse_predicate()

    def _parse_val(self) -> Tuple[str, int]:
        t = self.cur()
        if t.kind in ("STRING", "WORD") or (t.kind == "KW" and t.value in ("EMPTY", "NULL")):
            self.idx += 1
            return t.value, t.pos
        raise IQLError(f"Expected value at pos {t.pos}, got '{t.value or t.kind}'", t.pos)

    def _validate_val(self, field: str, op: str, val: str, pos: int) -> None:
        if field == "category":
            try:
                normalize_category(val)
            except ValueError as e:
                raise IQLError(str(e), pos) from e
        elif field == "state":
            try:
                normalize_state(val)
            except ValueError as e:
                raise IQLError(str(e), pos) from e
        elif field == "priority":
            if val.lower() not in PRIORITY_RANKS and not re.fullmatch(r"[1-5]", val):
                raise IQLError(f"Invalid priority '{val}' in iQL", pos)
        elif field == "severity":
            if val.lower() not in SEVERITY_RANKS and not re.fullmatch(r"[1-5]", val):
                raise IQLError(f"Invalid severity '{val}' in iQL", pos)
        elif field == "resolution":
            if val.lower() not in ("none", "null") and _hyphenate(val) not in RESOLUTIONS:
                raise IQLError(f"Invalid resolution '{val}' in iQL", pos)
        elif field in ("story_points", "estimate_minutes", "logged_minutes", "seq"):
            try:
                float(val)
            except ValueError as e:
                raise IQLError(f"Numeric value required for '{field}', got '{val}'", pos) from e
        elif field in ("is_blocked", "is_overdue", "archived"):
            if val.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise IQLError(f"Boolean value required for '{field}', got '{val}'", pos)
        elif field in ("due_date", "created_at", "updated_at"):
            resolve_iql_date(val, date_only=(field == "due_date"), pos=pos)

    def parse_predicate(self) -> Dict[str, Any]:
        ftok = self.cur()
        if ftok.kind not in ("WORD", "STRING"):
            raise IQLError(f"Expected field name at pos {ftok.pos}", ftok.pos)
        self.idx += 1
        canonical = self._resolve_field(ftok.value, ftok.pos)

        if self.peek_kw("IS"):
            self.consume("KW", "IS")
            neg = False
            if self.peek_kw("NOT"):
                self.consume("KW", "NOT")
                neg = True
            t = self.cur()
            if t.kind == "KW" and t.value in ("EMPTY", "NULL"):
                self.idx += 1
                return {"type": "empty", "field": canonical, "negated": neg}
            raise IQLError(f"Expected EMPTY or NULL after IS at pos {t.pos}", t.pos)

        if self.peek_kw("NOT") or self.peek_kw("IN"):
            neg = False
            if self.peek_kw("NOT"):
                self.consume("KW", "NOT")
                neg = True
            self.consume("KW", "IN")
            self.consume("LPAREN")
            if self.cur().kind == "RPAREN":
                raise IQLError("IN list cannot be empty", self.cur().pos)
            vals: List[str] = []
            v, vp = self._parse_val()
            self._validate_val(canonical, "IN", v, vp)
            vals.append(v)
            while self.cur().kind == "COMMA":
                self.consume("COMMA")
                v, vp = self._parse_val()
                self._validate_val(canonical, "IN", v, vp)
                vals.append(v)
            self.consume("RPAREN")
            return {"type": "in", "field": canonical, "negated": neg, "values": vals}

        optok = self.cur()
        if optok.kind != "OP":
            raise IQLError(f"Expected operator at pos {optok.pos}, got '{optok.value or optok.kind}'", optok.pos)
        self.idx += 1
        v, vp = self._parse_val()
        self._validate_val(canonical, optok.value, v, vp)
        return {"type": "cmp", "field": canonical, "op": optok.value, "value": v}


def parse_iql(iql: str) -> Dict[str, Any]:
    tokens = IQLLexer(iql or "").tokenize()
    return IQLParser(tokens).parse()


class IQLCompiler:
    BLOCKED_SQL = """(
        issues.state IN ('blocked', 'on-hold')
        OR EXISTS (
            SELECT 1 FROM issue_links bl
            JOIN issues bi ON bi.key = bl.blocker_key
            WHERE bl.blocked_key = issues.key
              AND bi.state NOT IN ('done', 'cancelled', 'archived')
        )
    )"""

    def __init__(
        self,
        current_user: str = "user",
        sprint_map: Optional[Dict[str, str]] = None,
        milestone_map: Optional[Dict[str, str]] = None,
        active_sprint_ids: Optional[List[str]] = None,
    ) -> None:
        self.current_user = current_user
        self.sprint_map = {k.lower(): v for k, v in (sprint_map or {}).items()}
        self.milestone_map = {k.lower(): v for k, v in (milestone_map or {}).items()}
        self.active_sprint_ids = list(active_sprint_ids or [])

    def compile_where(self, node: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        if node is None:
            return "1 = 1", []
        nt = node["type"]
        if nt in ("and", "or"):
            joiner = " AND " if nt == "and" else " OR "
            parts: List[str] = []
            params: List[Any] = []
            for ch in node["children"]:
                s, p = self.compile_where(ch)
                parts.append(f"({s})")
                params.extend(p)
            return joiner.join(parts), params
        if nt == "not":
            s, p = self.compile_where(node["child"])
            return f"NOT ({s})", p
        if nt == "empty":
            return self._compile_empty(node["field"], node["negated"])
        if nt == "in":
            return self._compile_in(node["field"], node["negated"], node["values"])
        if nt == "cmp":
            return self._compile_cmp(node["field"], node["op"], node["value"])
        raise IQLError(f"Unsupported iQL node '{nt}'")

    def _compile_empty(self, field: str, neg: bool) -> Tuple[str, List[Any]]:
        if field == "tags":
            kw = "EXISTS" if neg else "NOT EXISTS"
            return f"{kw} (SELECT 1 FROM issue_tags it WHERE it.issue_key = issues.key)", []
        if field == "components":
            kw = "EXISTS" if neg else "NOT EXISTS"
            return f"{kw} (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key)", []
        if field in ("story_points", "estimate_minutes"):
            return (f"issues.{field} IS NOT NULL" if neg else f"issues.{field} IS NULL"), []
        if field == "logged_minutes":
            return ("issues.logged_minutes > 0" if neg else "issues.logged_minutes = 0"), []
        if field.startswith("custom."):
            cf = field.split(".", 1)[1]
            p = f"$.{cf}"
            if neg:
                return "(json_extract(issues.custom_fields_json, ?) IS NOT NULL AND json_extract(issues.custom_fields_json, ?) != '')", [p, p]
            return "(json_extract(issues.custom_fields_json, ?) IS NULL OR json_extract(issues.custom_fields_json, ?) = '')", [p, p]
        col = f"issues.{field}"
        if neg:
            return f"({col} IS NOT NULL AND {col} != '')", []
        return f"({col} IS NULL OR {col} = '')", []

    def _compile_in(self, field: str, neg: bool, values: List[str]) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        for v in values:
            op = "~" if field in ("text", "comments_text", "attachments_text", "ac_text") else "="
            sc, sp = self._compile_cmp(field, op, v)
            clauses.append(f"({sc})")
            params.extend(sp)
        joined = " OR ".join(clauses)
        return (f"NOT ({joined})" if neg else f"({joined})"), params

    def _compile_cmp(self, field: str, op: str, raw_val: str) -> Tuple[str, List[Any]]:
        sval = str(raw_val)
        if field == "archived":
            truthy = sval.lower() in ("true", "1", "yes")
            if op == "!=":
                truthy = not truthy
            return ("(issues.archived = 1 OR issues.state = 'archived')" if truthy else "(issues.archived = 0 AND issues.state != 'archived')"), []
        if field == "is_blocked":
            truthy = sval.lower() in ("true", "1", "yes")
            if op == "!=":
                truthy = not truthy
            return (self.BLOCKED_SQL if truthy else f"NOT {self.BLOCKED_SQL}"), []
        if field == "is_overdue":
            today = today_iso()
            ov_sql = "(issues.due_date IS NOT NULL AND issues.due_date < ? AND issues.state NOT IN ('done', 'cancelled', 'archived'))"
            truthy = sval.lower() in ("true", "1", "yes")
            if op == "!=":
                truthy = not truthy
            return (ov_sql if truthy else f"NOT {ov_sql}"), [today]
        if field == "state":
            st = normalize_state(sval)
            if op == "=":
                return "issues.state = ?", [st]
            if op == "!=":
                return "issues.state != ?", [st]
            if op == "~":
                return "issues.state LIKE ?", [f"%{st}%"]
            if op == "!~":
                return "issues.state NOT LIKE ?", [f"%{st}%"]
            raise IQLError(f"Operator '{op}' unsupported for state")
        if field == "category":
            cat = normalize_category(sval)
            if op == "=":
                return "issues.category = ?", [cat]
            if op == "!=":
                return "issues.category != ?", [cat]
            raise IQLError(f"Operator '{op}' unsupported for category")
        if field == "priority":
            rank = int(sval) if re.fullmatch(r"[1-5]", sval) else PRIORITY_RANKS[sval.lower()]
            return f"issues.priority_rank {op} ?", [rank]
        if field == "severity":
            rank = int(sval) if re.fullmatch(r"[1-5]", sval) else SEVERITY_RANKS[sval.lower()]
            return f"issues.severity_rank {op} ?", [rank]
        if field == "resolution":
            res = normalize_resolution(sval)
            if res is None:
                return ("issues.resolution IS NULL" if op == "=" else "issues.resolution IS NOT NULL"), []
            return (f"issues.resolution = ?" if op == "=" else "(issues.resolution IS NULL OR issues.resolution != ?)"), [res]
        if field in ("story_points", "estimate_minutes", "logged_minutes", "seq"):
            num = float(sval)
            if op == "!=":
                return f"(issues.{field} IS NULL OR issues.{field} != ?)", [num]
            return f"(issues.{field} IS NOT NULL AND issues.{field} {op} ?)", [num]
        if field in ("due_date", "created_at", "updated_at"):
            dt_str = resolve_iql_date(sval, date_only=(field == "due_date"))
            if op == "!=":
                return f"(issues.{field} IS NULL OR issues.{field} != ?)", [dt_str]
            return f"(issues.{field} IS NOT NULL AND issues.{field} {op} ?)", [dt_str]
        if field in ("assignee", "reporter"):
            u = self.current_user if sval.lower() == "@me" else sval
            if u.lower() in ("unassigned", "none", "null"):
                return (f"(issues.{field} IS NULL OR issues.{field} = '')" if op == "=" else f"(issues.{field} IS NOT NULL AND issues.{field} != '')"), []
            if op == "=":
                return f"LOWER(issues.{field}) = LOWER(?)", [u]
            if op == "!=":
                return f"(issues.{field} IS NULL OR LOWER(issues.{field}) != LOWER(?))", [u]
            if op == "~":
                return f"LOWER(issues.{field}) LIKE LOWER(?)", [f"%{u}%"]
            if op == "!~":
                return f"(issues.{field} IS NULL OR LOWER(issues.{field}) NOT LIKE LOWER(?))", [f"%{u}%"]
        if field == "tags":
            if op in ("=", "!="):
                kw = "EXISTS" if op == "=" else "NOT EXISTS"
                return f"{kw} (SELECT 1 FROM issue_tags it WHERE it.issue_key = issues.key AND LOWER(it.tag) = LOWER(?))", [sval]
            if op in ("~", "!~"):
                kw = "EXISTS" if op == "~" else "NOT EXISTS"
                return f"{kw} (SELECT 1 FROM issue_tags it WHERE it.issue_key = issues.key AND LOWER(it.tag) LIKE LOWER(?))", [f"%{sval}%"]
        if field == "components":
            if op in ("=", "!="):
                kw = "EXISTS" if op == "=" else "NOT EXISTS"
                return f"{kw} (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) = LOWER(?))", [sval]
            if op in ("~", "!~"):
                kw = "EXISTS" if op == "~" else "NOT EXISTS"
                return f"{kw} (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) LIKE LOWER(?))", [f"%{sval}%"]
        if field == "sprint":
            low = sval.lower()
            if low == "active":
                if not self.active_sprint_ids:
                    return ("1 = 1" if op == "!=" else "1 = 0"), []
                ph = ", ".join("?" for _ in self.active_sprint_ids)
                return (f"UPPER(issues.sprint) IN ({ph})" if op == "=" else f"(issues.sprint IS NULL OR UPPER(issues.sprint) NOT IN ({ph}))"), list(self.active_sprint_ids)
            if low in ("none", "null", "backlog", "unassigned"):
                return ("(issues.sprint IS NULL OR issues.sprint = '')" if op == "=" else "(issues.sprint IS NOT NULL AND issues.sprint != '')"), []
            sid = self.sprint_map.get(low, sval.upper())
            return ("UPPER(issues.sprint) = ?" if op == "=" else "(issues.sprint IS NULL OR UPPER(issues.sprint) != ?)"), [sid]
        if field == "milestone":
            low = sval.lower()
            if low in ("none", "null", "unassigned"):
                return ("(issues.milestone IS NULL OR issues.milestone = '')" if op == "=" else "(issues.milestone IS NOT NULL AND issues.milestone != '')"), []
            mid = self.milestone_map.get(low, sval.upper())
            return ("UPPER(issues.milestone) = ?" if op == "=" else "(issues.milestone IS NULL OR UPPER(issues.milestone) != ?)"), [mid]
        if field == "text":
            fts = build_fts_match_expr(sval)
            lk = f"%{sval}%"
            if fts:
                sql = (
                    "(issues.key IN (SELECT key FROM issues_fts WHERE issues_fts MATCH ?) "
                    "OR LOWER(issues.key) LIKE LOWER(?) "
                    "OR LOWER(issues.name) LIKE LOWER(?) "
                    "OR LOWER(issues.description) LIKE LOWER(?) "
                    "OR LOWER(issues.ac_text) LIKE LOWER(?) "
                    "OR LOWER(issues.comments_text) LIKE LOWER(?) "
                    "OR LOWER(issues.attachments_text) LIKE LOWER(?))"
                )
                prms: List[Any] = [fts, lk, lk, lk, lk, lk, lk]
            else:
                sql = (
                    "(LOWER(issues.key) LIKE LOWER(?) OR LOWER(issues.name) LIKE LOWER(?) "
                    "OR LOWER(issues.description) LIKE LOWER(?) OR LOWER(issues.comments_text) LIKE LOWER(?) "
                    "OR LOWER(issues.attachments_text) LIKE LOWER(?))"
                )
                prms = [lk, lk, lk, lk, lk]
            return (sql if op in ("~", "=") else f"NOT {sql}"), prms
        if field in ("name", "description", "ac_text", "comments_text", "attachments_text"):
            if op in ("~", "!~"):
                fts = build_fts_match_expr(sval, column=field)
                lk = f"%{sval}%"
                if fts:
                    sql = f"(issues.key IN (SELECT key FROM issues_fts WHERE issues_fts MATCH ?) OR LOWER(issues.{field}) LIKE LOWER(?))"
                    prms = [fts, lk]
                else:
                    sql = f"LOWER(issues.{field}) LIKE LOWER(?)"
                    prms = [lk]
                return (sql if op == "~" else f"NOT {sql}"), prms
            if op == "=":
                return f"LOWER(issues.{field}) = LOWER(?)", [sval]
            if op == "!=":
                return f"LOWER(issues.{field}) != LOWER(?)", [sval]
        if field.startswith("custom."):
            cf = field.split(".", 1)[1]
            p = f"$.{cf}"
            if op == "=":
                return "LOWER(CAST(json_extract(issues.custom_fields_json, ?) AS TEXT)) = LOWER(?)", [p, sval]
            if op == "!=":
                return "(json_extract(issues.custom_fields_json, ?) IS NULL OR LOWER(CAST(json_extract(issues.custom_fields_json, ?) AS TEXT)) != LOWER(?))", [p, p, sval]
            if op == "~":
                return "LOWER(CAST(json_extract(issues.custom_fields_json, ?) AS TEXT)) LIKE LOWER(?)", [p, f"%{sval}%"]
            if op == "!~":
                return "(json_extract(issues.custom_fields_json, ?) IS NULL OR LOWER(CAST(json_extract(issues.custom_fields_json, ?) AS TEXT)) NOT LIKE LOWER(?))", [p, p, f"%{sval}%"]
        if op == "=":
            return f"LOWER(issues.{field}) = LOWER(?)", [sval]
        if op == "!=":
            return f"(issues.{field} IS NULL OR LOWER(issues.{field}) != LOWER(?))", [sval]
        if op == "~":
            return f"LOWER(issues.{field}) LIKE LOWER(?)", [f"%{sval}%"]
        if op == "!~":
            return f"(issues.{field} IS NULL OR LOWER(issues.{field}) NOT LIKE LOWER(?))", [f"%{sval}%"]
        if op in (">", ">=", "<", "<="):
            return f"(issues.{field} IS NOT NULL AND LOWER(issues.{field}) {op} LOWER(?))", [sval]
        raise IQLError(f"Unsupported operator '{op}' for '{field}'")

    def compile_order_by(
        self,
        order_items: List[Dict[str, str]],
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> str:
        clauses: List[str] = []
        if sort_field and sort_field.strip():
            sf = sort_field.strip()
            d = (sort_order or "asc").strip().upper()
            if sf.startswith("-"):
                sf = sf[1:]
                d = "DESC"
            if d not in ("ASC", "DESC"):
                raise IQLError(f"Invalid sort order '{sort_order}'. Expected asc or desc.")
            canonical = FIELD_ALIASES.get(sf.lower(), sf.lower())
            if canonical not in ORDER_SQL_MAP:
                raise IQLError(f"Unknown sort field '{sort_field}'.")
            col = ORDER_SQL_MAP[canonical]
            if canonical in ("story_points", "estimate_minutes", "due_date", "assignee", "reporter", "sprint", "milestone", "resolution", "parent"):
                clauses.append(f"({col} IS NULL OR {col} = '') ASC, {col} {d}")
            else:
                clauses.append(f"{col} {d}")
        elif order_items:
            for it in order_items:
                f = it["field"]
                d = it["direction"]
                col = ORDER_SQL_MAP[f]
                if f in ("story_points", "estimate_minutes", "due_date", "assignee", "reporter", "sprint", "milestone", "resolution", "parent"):
                    clauses.append(f"({col} IS NULL OR {col} = '') ASC, {col} {d}")
                else:
                    clauses.append(f"{col} {d}")
        clauses.append("issues.seq ASC")
        return ", ".join(clauses)


# ============================================================================
# Section 4: StorageEngine — Self-Healing Hybrid Index & Folder-Per-Issue Core
# ============================================================================

class StorageEngine:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.issues_root = repo_root / "itrack-issues"
        self.issues_dir = self.issues_root / "issues"
        self.runtime_dir = self.issues_root / ".runtime"

        self.config_path = self.issues_root / "config.json"
        self.tags_path = self.issues_root / "tags.json"
        self.sprints_path = self.issues_root / "sprints.json"
        self.milestones_path = self.issues_root / "milestones.json"
        self.views_path = self.issues_root / "views.json"
        self.gitignore_path = self.issues_root / ".gitignore"

        self.lock_path = self.runtime_dir / "storage.lock"
        self.db_path = self.runtime_dir / "index.sqlite"
        self.stray_db_path = self.issues_root / "index.sqlite"
        self.daemon_path = self.runtime_dir / "daemon.json"

        self._rlock = threading.RLock()
        self._lock_depth = 0
        self._lock_fd: Optional[Any] = None
        self._lock_exclusive = False
        self._db: Optional[sqlite3.Connection] = None
        self.default_user = detect_default_user(repo_root)

        if self.is_initialized():
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.issues_dir.mkdir(parents=True, exist_ok=True)
            with self.write_lock():
                pass

    def is_initialized(self) -> bool:
        return self.config_path.exists()

    def require_initialized(self) -> None:
        if not self.is_initialized():
            raise HTTPException(
                status_code=503,
                detail="Workspace is not initialized (itrack-issues/config.json missing). Run 'itrack init' first.",
            )

    def close(self) -> None:
        with self._rlock:
            if self._db is not None:
                with contextlib.suppress(Exception):
                    self._db.close()
                self._db = None

    def _acquire_flock(self, exclusive: bool) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        f = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except Exception:
            f.close()
            raise
        self._lock_fd = f
        self._lock_exclusive = exclusive

    def _release_flock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_fd.close()
                self._lock_fd = None
                self._lock_exclusive = False

    @contextlib.contextmanager
    def write_lock(self) -> Iterator[None]:
        self.require_initialized()
        with self._rlock:
            upgraded_from_sh = False
            if self._lock_depth == 0:
                self._acquire_flock(exclusive=True)
            elif not self._lock_exclusive:
                # Safe lock upgrade: release SH then acquire EX
                self._release_flock()
                self._acquire_flock(exclusive=True)
                upgraded_from_sh = True
            self._lock_depth += 1
            try:
                self._ensure_healthy_index_locked(can_write=True)
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._release_flock()
                elif upgraded_from_sh:
                    pass

    @contextlib.contextmanager
    def read_lock(self) -> Iterator[None]:
        self.require_initialized()
        with self._rlock:
            if self._lock_depth == 0:
                self._acquire_flock(exclusive=False)
                if not self._check_index_quick_locked():
                    self._release_flock()
                    self._acquire_flock(exclusive=True)
                    self._ensure_healthy_index_locked(can_write=True)
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._release_flock()

    # ------------------------------------------------------------------------
    # Disk Atomic I/O & Signature Fingerprinting
    # ------------------------------------------------------------------------

    def _atomic_write_text(self, path: Path, content: str) -> None:
        _atomic_write_bytes(path, content.encode("utf-8"))

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        _atomic_write_json_standalone(path, data)

    def _read_json(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            if default is not None:
                return default
            raise FileNotFoundError(str(path))
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _compute_disk_signature(self) -> str:
        parts: List[str] = []
        for meta_p in (self.config_path, self.tags_path, self.sprints_path, self.milestones_path, self.views_path):
            if meta_p.exists():
                st = meta_p.stat()
                parts.append(f"{meta_p.name}:{st.st_mtime_ns}:{st.st_size}")
            else:
                parts.append(f"{meta_p.name}:0:0")

        if self.issues_dir.exists():
            entries = sorted(
                (e for e in os.scandir(self.issues_dir) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                key=lambda e: e.name,
            )
            parts.append(f"dirs:{len(entries)}")
            for ent in entries:
                dpath = Path(ent.path)
                ij = dpath / "issue.json"
                ij_sig = "0:0"
                if ij.exists():
                    ist = ij.stat()
                    ij_sig = f"{ist.st_mtime_ns}:{ist.st_size}"
                cdir = dpath / "comments"
                c_sigs: List[str] = []
                if cdir.exists():
                    for cent in sorted(os.scandir(cdir), key=lambda x: x.name):
                        if cent.is_dir(follow_symlinks=False) and not cent.name.startswith("."):
                            cj = Path(cent.path) / "comment.json"
                            if cj.exists():
                                cst = cj.stat()
                                c_sigs.append(f"{cent.name}:{cst.st_mtime_ns}:{cst.st_size}")
                parts.append(f"I:{ent.name}|{ij_sig}|C:{','.join(c_sigs)}")
        raw = "\n".join(parts).encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    # ------------------------------------------------------------------------
    # Self-Healing SQLite3 + FTS5 Lifecycle
    # ------------------------------------------------------------------------

    def _cleanup_stray_root_sqlite(self) -> None:
        for p in self.issues_root.glob("index.sqlite*"):
            with contextlib.suppress(Exception):
                if p.is_file():
                    p.unlink(missing_ok=True)

    def _wipe_runtime_sqlite(self) -> None:
        if self._db is not None:
            with contextlib.suppress(Exception):
                self._db.close()
            self._db = None
        for p in self.runtime_dir.glob("index.sqlite*"):
            with contextlib.suppress(Exception):
                if p.is_file():
                    p.unlink(missing_ok=True)
        self._cleanup_stray_root_sqlite()

    def _open_sqlite(self) -> sqlite3.Connection:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issues (
                key TEXT PRIMARY KEY,
                seq INTEGER NOT NULL,
                name TEXT NOT NULL,
                folder TEXT NOT NULL,
                category TEXT NOT NULL,
                state TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                priority TEXT NOT NULL,
                priority_rank INTEGER NOT NULL,
                severity TEXT NOT NULL,
                severity_rank INTEGER NOT NULL,
                resolution TEXT,
                description TEXT NOT NULL DEFAULT '',
                ac_text TEXT NOT NULL DEFAULT '',
                comments_text TEXT NOT NULL DEFAULT '',
                attachments_text TEXT NOT NULL DEFAULT '',
                assignee TEXT,
                reporter TEXT,
                parent TEXT,
                sprint TEXT,
                milestone TEXT,
                story_points REAL,
                estimate_minutes INTEGER,
                logged_minutes INTEGER NOT NULL DEFAULT 0,
                due_date TEXT,
                custom_fields_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_issues_seq ON issues(seq);
            CREATE INDEX IF NOT EXISTS idx_issues_name_low ON issues(LOWER(name));
            CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);
            CREATE INDEX IF NOT EXISTS idx_issues_category ON issues(category);
            CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority_rank);
            CREATE INDEX IF NOT EXISTS idx_issues_severity ON issues(severity_rank);
            CREATE INDEX IF NOT EXISTS idx_issues_assignee ON issues(LOWER(assignee));
            CREATE INDEX IF NOT EXISTS idx_issues_sprint ON issues(sprint);
            CREATE INDEX IF NOT EXISTS idx_issues_milestone ON issues(milestone);
            CREATE INDEX IF NOT EXISTS idx_issues_parent ON issues(parent);

            CREATE TABLE IF NOT EXISTS issue_tags (
                issue_key TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (issue_key, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_issue_tags_tag ON issue_tags(LOWER(tag));

            CREATE TABLE IF NOT EXISTS issue_components (
                issue_key TEXT NOT NULL,
                component TEXT NOT NULL,
                PRIMARY KEY (issue_key, component)
            );
            CREATE INDEX IF NOT EXISTS idx_issue_components_comp ON issue_components(LOWER(component));

            CREATE TABLE IF NOT EXISTS issue_links (
                id TEXT NOT NULL,
                blocker_key TEXT NOT NULL,
                blocked_key TEXT NOT NULL,
                PRIMARY KEY (id, blocker_key, blocked_key)
            );
            CREATE INDEX IF NOT EXISTS idx_issue_links_blocked ON issue_links(blocked_key);

            CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
                key,
                name,
                description,
                ac_text,
                comments_text,
                attachments_text
            );
            """
        )
        conn.commit()

    def _check_index_quick_locked(self) -> bool:
        if self.stray_db_path.exists():
            return False
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return False
        try:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._db = self._open_sqlite()
            qc = self._db.execute("PRAGMA quick_check(1);").fetchone()
            if not qc or str(qc[0]).lower() != "ok":
                return False
            # Also touch issues_fts to detect FTS shadow table corruption
            self._db.execute("SELECT count(*) FROM issues_fts;").fetchone()
            row = self._db.execute("SELECT value FROM meta WHERE key = 'disk_sig';").fetchone()
            if not row:
                return False
            return row["value"] == self._compute_disk_signature()
        except Exception:
            return False

    def _ensure_healthy_index_locked(self, can_write: bool = True) -> None:
        if self._check_index_quick_locked():
            return
        if not can_write:
            return
        try:
            self._cleanup_stray_root_sqlite()
            if self._db is None:
                self._db = self._open_sqlite()
            qc = self._db.execute("PRAGMA quick_check(1);").fetchone()
            if not qc or str(qc[0]).lower() != "ok":
                raise sqlite3.DatabaseError("quick_check failed")
            self._init_db_schema(self._db)
            self._rebuild_index_unlocked()
        except Exception:
            self._wipe_runtime_sqlite()
            self._db = self._open_sqlite()
            self._init_db_schema(self._db)
            self._rebuild_index_unlocked()

    def _update_disk_sig_in_db(self) -> None:
        assert self._db is not None
        sig = self._compute_disk_signature()
        self._db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('disk_sig', ?)",
            (sig,),
        )
        self._db.commit()

    def _load_raw_comments_from_dir(self, issue_dir: Path) -> List[Dict[str, Any]]:
        c_root = issue_dir / "comments"
        if not c_root.exists():
            return []
        comments: List[Dict[str, Any]] = []
        for entry in os.scandir(c_root):
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            c_json = Path(entry.path) / "comment.json"
            if c_json.exists():
                with contextlib.suppress(Exception):
                    comments.append(self._read_json(c_json))
        comments.sort(key=lambda c: (int(c.get("seq", 0)), str(c.get("created_at", "")), str(c.get("id", ""))))
        return comments

    def _index_issue_in_tx(self, cur: sqlite3.Cursor, issue: Dict[str, Any], issue_dir: Path) -> None:
        key = issue["key"]
        comments = self._load_raw_comments_from_dir(issue_dir)
        ac_text = "\n".join(str(item.get("text", "")) for item in issue.get("acceptance_criteria", []) if isinstance(item, dict))
        comments_text = "\n".join(str(c.get("body", "")) for c in comments)

        att_names: List[str] = []
        for a in issue.get("attachments", []):
            if isinstance(a, dict) and a.get("filename"):
                att_names.append(str(a["filename"]))
        for emb in issue.get("description_embeds", []):
            if isinstance(emb, dict) and emb.get("filename"):
                att_names.append(str(emb["filename"]))
        for c in comments:
            for emb in c.get("embeds", []):
                if isinstance(emb, dict) and emb.get("filename"):
                    att_names.append(str(emb["filename"]))
            for ca in c.get("attachments", []):
                if isinstance(ca, dict) and ca.get("filename"):
                    att_names.append(str(ca["filename"]))
        attachments_text = " ".join(att_names)

        prio, p_rank = normalize_priority(issue.get("priority"), "medium")
        sev, s_rank = normalize_severity(issue.get("severity"), "sev3")
        state = normalize_state(issue.get("state"), "backlog")
        archived = 1 if (issue.get("archived") or state == "archived") else 0

        cur.execute("DELETE FROM issue_tags WHERE issue_key = ?", (key,))
        cur.execute("DELETE FROM issue_components WHERE issue_key = ?", (key,))
        cur.execute("DELETE FROM issue_links WHERE blocker_key = ? OR blocked_key = ?", (key, key))
        cur.execute("DELETE FROM issues_fts WHERE key = ?", (key,))

        cur.execute(
            """
            INSERT OR REPLACE INTO issues (
                key, seq, name, folder, category, state, archived,
                priority, priority_rank, severity, severity_rank, resolution,
                description, ac_text, comments_text, attachments_text,
                assignee, reporter, parent, sprint, milestone,
                story_points, estimate_minutes, logged_minutes, due_date,
                custom_fields_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                int(issue.get("seq", 0)),
                issue["name"],
                issue_dir.name,
                issue.get("category", "task"),
                state,
                archived,
                prio,
                p_rank,
                sev,
                s_rank,
                issue.get("resolution"),
                issue.get("description", "") or "",
                ac_text,
                comments_text,
                attachments_text,
                issue.get("assignee"),
                issue.get("reporter"),
                issue.get("parent"),
                issue.get("sprint"),
                issue.get("milestone"),
                issue.get("story_points"),
                issue.get("estimate_minutes"),
                int(issue.get("logged_minutes") or 0),
                issue.get("due_date"),
                json.dumps(issue.get("custom_fields") or {}, sort_keys=True, ensure_ascii=False),
                issue.get("created_at") or now_iso(),
                issue.get("updated_at") or now_iso(),
            ),
        )

        for tg in issue.get("tags", []):
            cur.execute("INSERT OR IGNORE INTO issue_tags (issue_key, tag) VALUES (?, ?)", (key, tg))
        for cp in issue.get("components", []):
            cur.execute("INSERT OR IGNORE INTO issue_components (issue_key, component) VALUES (?, ?)", (key, cp))

        for lnk in issue.get("links", []):
            lt = lnk.get("type")
            lid = lnk.get("id", f"LNK-{short_id()}")
            tgt = lnk.get("target_key")
            if not tgt:
                continue
            if lt == "blocks":
                cur.execute("INSERT OR IGNORE INTO issue_links (id, blocker_key, blocked_key) VALUES (?, ?, ?)", (lid, key, tgt))
            elif lt == "blocked-by":
                cur.execute("INSERT OR IGNORE INTO issue_links (id, blocker_key, blocked_key) VALUES (?, ?, ?)", (lid, tgt, key))

        cur.execute(
            """
            INSERT INTO issues_fts (key, name, description, ac_text, comments_text, attachments_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, issue["name"], issue.get("description", "") or "", ac_text, comments_text, attachments_text),
        )

    def _rebuild_index_unlocked(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        assert self._db is not None
        cur = self._db.cursor()
        cur.execute("DELETE FROM issues;")
        cur.execute("DELETE FROM issue_tags;")
        cur.execute("DELETE FROM issue_components;")
        cur.execute("DELETE FROM issue_links;")
        cur.execute("DELETE FROM issues_fts;")

        count = 0
        max_issue_seq = 0
        max_att_seq = 0
        max_cmt_seq = 0
        max_lnk_seq = 0
        max_wl_seq = 0

        if self.issues_dir.exists():
            for entry in sorted(os.scandir(self.issues_dir), key=lambda e: e.name):
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                    continue
                idir = Path(entry.path)
                ij = idir / "issue.json"
                if not ij.exists():
                    continue
                try:
                    iss = self._read_json(ij)
                except Exception:
                    continue
                if not isinstance(iss, dict) or "key" not in iss:
                    continue
                if iss.get("name") != idir.name:
                    iss["name"] = idir.name
                    iss["folder"] = idir.name
                    self._atomic_write_json(ij, iss)
                m_k = re.search(r"-(\d+)$", str(iss["key"]))
                seq = int(iss.get("seq") or (int(m_k.group(1)) if m_k else (count + 1)))
                iss["seq"] = seq
                max_issue_seq = max(max_issue_seq, seq)

                for a in list(iss.get("attachments", [])) + list(iss.get("description_embeds", [])):
                    m_a = re.search(r"-(\d+)$", str(a.get("id", "")))
                    if m_a:
                        max_att_seq = max(max_att_seq, int(m_a.group(1)))
                for d_id in (iss.get("deleted_attachments") or {}).keys():
                    m_da = re.search(r"-(\d+)$", str(d_id))
                    if m_da:
                        max_att_seq = max(max_att_seq, int(m_da.group(1)))
                for lnk in iss.get("links", []):
                    m_l = re.search(r"-(\d+)$", str(lnk.get("id", "")))
                    if m_l:
                        max_lnk_seq = max(max_lnk_seq, int(m_l.group(1)))
                for wl in iss.get("worklogs", []):
                    m_w = re.search(r"-(\d+)$", str(wl.get("id", "")))
                    if m_w:
                        max_wl_seq = max(max_wl_seq, int(m_w.group(1)))

                for c in self._load_raw_comments_from_dir(idir):
                    m_c = re.search(r"-(\d+)$", str(c.get("id", "")))
                    if m_c:
                        max_cmt_seq = max(max_cmt_seq, int(m_c.group(1)))
                    for a in list(c.get("embeds", [])) + list(c.get("attachments", [])):
                        m_ca = re.search(r"-(\d+)$", str(a.get("id", "")))
                        if m_ca:
                            max_att_seq = max(max_att_seq, int(m_ca.group(1)))

                self._index_issue_in_tx(cur, iss, idir)
                count += 1

        # Re-add blocking links across all issues because each issue index clears links touching it
        cur.execute("DELETE FROM issue_links;")
        if self.issues_dir.exists():
            for entry in os.scandir(self.issues_dir):
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                    continue
                ij = Path(entry.path) / "issue.json"
                if not ij.exists():
                    continue
                with contextlib.suppress(Exception):
                    iss = self._read_json(ij)
                    key = iss["key"]
                    for lnk in iss.get("links", []):
                        lt = lnk.get("type")
                        lid = lnk.get("id", f"LNK-{short_id()}")
                        tgt = lnk.get("target_key")
                        if tgt and lt == "blocks":
                            cur.execute("INSERT OR IGNORE INTO issue_links (id, blocker_key, blocked_key) VALUES (?, ?, ?)", (lid, key, tgt))
                        elif tgt and lt == "blocked-by":
                            cur.execute("INSERT OR IGNORE INTO issue_links (id, blocker_key, blocked_key) VALUES (?, ?, ?)", (lid, tgt, key))

        self._db.commit()

        # Sync monotonic sequence counters in config.json if disk items had higher IDs (e.g. after git pull)
        if self.config_path.exists():
            cfg = self._read_json(self.config_path)
            max_spr = max((int(re.search(r"-(\d+)$", str(s.get("id", "0"))).group(1)) for s in self._read_json(self.sprints_path, {"sprints": []}).get("sprints", []) if re.search(r"-(\d+)$", str(s.get("id", "")))), default=0)
            max_mil = max((int(re.search(r"-(\d+)$", str(m.get("id", "0"))).group(1)) for m in self._read_json(self.milestones_path, {"milestones": []}).get("milestones", []) if re.search(r"-(\d+)$", str(m.get("id", "")))), default=0)
            max_vw = max((int(re.search(r"-(\d+)$", str(v.get("id", "0"))).group(1)) for v in self._read_json(self.views_path, {"views": []}).get("views", []) if re.search(r"-(\d+)$", str(v.get("id", "")))), default=0)
            dirty = False
            for k_seq, mval in (
                ("next_issue_seq", max_issue_seq),
                ("next_att_seq", max_att_seq),
                ("next_comment_seq", max_cmt_seq),
                ("next_link_seq", max_lnk_seq),
                ("next_worklog_seq", max_wl_seq),
                ("next_sprint_seq", max_spr),
                ("next_milestone_seq", max_mil),
                ("next_view_seq", max_vw),
            ):
                if mval >= int(cfg.get(k_seq, 1)):
                    cfg[k_seq] = mval + 1
                    dirty = True
            if dirty:
                self._atomic_write_json(self.config_path, cfg)

        self._update_disk_sig_in_db()
        dur_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {"status": "reindexed", "indexed_issues": count, "duration_ms": dur_ms}

    def reindex(self) -> Dict[str, Any]:
        with self.write_lock():
            return self._rebuild_index_unlocked()

    def _next_seq(self, cfg: Dict[str, Any], counter_key: str) -> int:
        val = int(cfg.get(counter_key, 1))
        cfg[counter_key] = val + 1
        cfg["updated_at"] = now_iso()
        return val

    def _append_history(
        self,
        issue_dir: Path,
        cfg: Dict[str, Any],
        actor: str,
        action: str,
        diffs: List[Dict[str, Any]],
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        h_seq = self._next_seq(cfg, "next_history_seq")
        entry = {
            "id": f"HST-{h_seq}",
            "timestamp": timestamp or now_iso(),
            "actor": actor or self.default_user,
            "action": action,
            "diffs": diffs,
        }
        h_path = issue_dir / "history.jsonl"
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(h_path, "a", encoding="utf-8") as hf:
            hf.write(line)
            hf.flush()
        return entry

    def _save_issue_unlocked(self, issue: Dict[str, Any], issue_dir: Path) -> None:
        assert self._db is not None
        issue["folder"] = issue_dir.name
        self._atomic_write_json(issue_dir / "issue.json", issue)
        cur = self._db.cursor()
        self._index_issue_in_tx(cur, issue, issue_dir)
        # Re-ensure peer blocking links pointing at this issue remain in issue_links
        for entry in os.scandir(self.issues_dir):
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith(".") or entry.name == issue_dir.name:
                continue
            p_json = Path(entry.path) / "issue.json"
            if p_json.exists():
                with contextlib.suppress(Exception):
                    peer = self._read_json(p_json)
                    pk = peer["key"]
                    for lnk in peer.get("links", []):
                        tgt = lnk.get("target_key")
                        if tgt == issue["key"]:
                            lt = lnk.get("type")
                            lid = lnk.get("id", f"LNK-{short_id()}")
                            if lt == "blocks":
                                cur.execute("INSERT OR IGNORE INTO issue_links (id, blocker_key, blocked_key) VALUES (?, ?, ?)", (lid, pk, tgt))
                            elif lt == "blocked-by":
                                cur.execute("INSERT OR IGNORE INTO issue_links (id, blocker_key, blocked_key) VALUES (?, ?, ?)", (lid, tgt, pk))
        self._db.commit()
        self._update_disk_sig_in_db()

    # ------------------------------------------------------------------------
    # Dual Addressing & Enrichment
    # ------------------------------------------------------------------------

    def _resolve_issue_unlocked(self, issue_ref: str) -> Tuple[Dict[str, Any], Path]:
        if issue_ref is None:
            raise HTTPException(status_code=404, detail="Issue reference is empty.")
        decoded = urllib.parse.unquote(str(issue_ref)).strip()
        if not decoded:
            raise HTTPException(status_code=404, detail="Issue reference is empty.")

        # 1. Exact folder match (supports spaces, symbols, Unicode)
        if "/" not in decoded and "\\" not in decoded and decoded not in (".", ".."):
            exact_dir = self.issues_dir / decoded
            if (exact_dir / "issue.json").exists():
                return self._read_json(exact_dir / "issue.json"), exact_dir

        ref_clean = decoded[1:] if (decoded.startswith("#") and re.fullmatch(r"#[A-Za-z][A-Za-z0-9]*-\d+", decoded)) else decoded

        # 2. SQLite index lookup by key (case-insensitive) or exact name
        if self._db is not None:
            row = self._db.execute(
                "SELECT folder FROM issues WHERE UPPER(key) = UPPER(?) OR name = ? LIMIT 1",
                (ref_clean, decoded),
            ).fetchone()
            if row:
                idir = self.issues_dir / row["folder"]
                if (idir / "issue.json").exists():
                    return self._read_json(idir / "issue.json"), idir

        # 3. Direct filesystem fallback scan
        if self.issues_dir.exists():
            for entry in os.scandir(self.issues_dir):
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                    continue
                ij = Path(entry.path) / "issue.json"
                if ij.exists():
                    with contextlib.suppress(Exception):
                        iss = self._read_json(ij)
                        if str(iss.get("key", "")).upper() == ref_clean.upper() or iss.get("name") == decoded:
                            return iss, Path(entry.path)

        raise HTTPException(status_code=404, detail=f"Issue '{decoded}' not found.")

    def _enrich_comment(self, raw_cmt: Dict[str, Any], issue: Dict[str, Any]) -> Dict[str, Any]:
        cmt = dict(raw_cmt)
        att_by_id = {a["id"].upper(): a for a in issue.get("attachments", []) if isinstance(a, dict) and "id" in a}
        del_by_id = {k.upper(): v for k, v in (issue.get("deleted_attachments") or {}).items()}
        linked_resolved: List[Dict[str, Any]] = []
        for lid in cmt.get("linked_attachment_ids", []):
            up = str(lid).upper()
            if up in att_by_id:
                item = dict(att_by_id[up])
                item["linked"] = True
                item["deleted"] = False
                linked_resolved.append(item)
            elif up in del_by_id:
                d = del_by_id[up]
                linked_resolved.append({
                    "id": d.get("id", up),
                    "filename": d.get("filename", up),
                    "linked": True,
                    "deleted": True,
                    "deleted_at": d.get("deleted_at"),
                })
            else:
                linked_resolved.append({
                    "id": up,
                    "filename": up,
                    "linked": True,
                    "deleted": True,
                })
        cmt["linked_attachments"] = linked_resolved
        return cmt

    def _enrich_issues_unlocked(self, items: List[Tuple[Dict[str, Any], Path]]) -> List[Dict[str, Any]]:
        if not items:
            return []
        assert self._db is not None
        rows = self._db.execute(
            "SELECT key, seq, name, state, priority, assignee, parent FROM issues ORDER BY seq ASC"
        ).fetchall()
        by_key: Dict[str, sqlite3.Row] = {r["key"]: r for r in rows}
        children_by_parent: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            if r["parent"]:
                children_by_parent.setdefault(r["parent"], []).append(r)

        link_rows = self._db.execute("SELECT blocker_key, blocked_key FROM issue_links").fetchall()
        blockers_of: Dict[str, List[str]] = {}
        for lr in link_rows:
            bk = lr["blocker_key"]
            b_row = by_key.get(bk)
            if b_row and b_row["state"] not in TERMINAL_STATES:
                if bk not in blockers_of.setdefault(lr["blocked_key"], []):
                    blockers_of[lr["blocked_key"]].append(bk)

        today = today_iso()
        out: List[Dict[str, Any]] = []
        for raw_iss, idir in items:
            iss = dict(raw_iss)
            key = iss["key"]
            state = iss.get("state", "backlog")
            due = iss.get("due_date")
            iss["is_overdue"] = bool(due and due < today and state not in TERMINAL_STATES)

            unres = list(blockers_of.get(key, []))
            for lnk in iss.get("links", []):
                if lnk.get("type") == "blocked-by":
                    tk = lnk.get("target_key")
                    t_row = by_key.get(tk) if tk else None
                    if tk and t_row and t_row["state"] not in TERMINAL_STATES and tk not in unres:
                        unres.append(tk)
            iss["unresolved_blockers"] = unres
            iss["is_blocked"] = (state in ("blocked", "on-hold")) or (len(unres) > 0 and state not in TERMINAL_STATES)

            subs_rows = children_by_parent.get(key, [])
            subs: List[Dict[str, Any]] = []
            sub_done = 0
            for sr in subs_rows:
                done = sr["state"] == "done"
                if done:
                    sub_done += 1
                subs.append({
                    "key": sr["key"],
                    "name": sr["name"],
                    "state": sr["state"],
                    "priority": sr["priority"],
                    "assignee": sr["assignee"],
                    "completed": done,
                })
            iss["subtasks"] = subs
            iss["subtask_progress"] = {
                "total": len(subs),
                "completed": sub_done,
                "pct": round(sub_done / len(subs) * 100.0, 1) if subs else 0.0,
            }

            acs = iss.get("acceptance_criteria") or []
            ac_checked = sum(1 for a in acs if isinstance(a, dict) and a.get("checked"))
            iss["ac_progress"] = {
                "total": len(acs),
                "checked": ac_checked,
                "pct": round(ac_checked / len(acs) * 100.0, 1) if acs else 0.0,
            }

            logged = sum(int(w.get("minutes", 0)) for w in (iss.get("worklogs") or []))
            iss["logged_minutes"] = logged
            est = iss.get("estimate_minutes")
            iss["time_progress"] = {
                "estimate_minutes": est,
                "logged_minutes": logged,
                "pct": round(min(100.0, logged / est * 100.0), 1) if (est and est > 0) else (100.0 if logged > 0 else 0.0),
            }

            raw_comments = self._load_raw_comments_from_dir(idir)
            iss["comments"] = [self._enrich_comment(c, iss) for c in raw_comments]
            iss["comment_count"] = len(iss["comments"])
            out.append(iss)
        return out

    def get_issue(self, issue_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            return self._enrich_issues_unlocked([(iss, idir)])[0]

    def issue_count(self) -> int:
        if not self.is_initialized():
            return 0
        with self.read_lock():
            assert self._db is not None
            row = self._db.execute("SELECT COUNT(*) AS c FROM issues").fetchone()
            return int(row["c"]) if row else 0

    # ------------------------------------------------------------------------
    # Config, Status, Stats & Hierarchy Tree
    # ------------------------------------------------------------------------

    def get_config(self) -> Dict[str, Any]:
        with self.read_lock():
            return self._read_json(self.config_path)

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise HTTPException(status_code=400, detail="Configuration update payload cannot be empty.")
        # Normalize {"key": k, "value": v} into flat/nested updates
        updates: Dict[str, Any] = {}
        if "key" in payload and "value" in payload and len(payload) <= 3:
            k_str = str(payload["key"]).strip()
            if not k_str:
                raise HTTPException(status_code=400, detail="Config key cannot be empty.")
            updates[k_str] = payload["value"]
        else:
            updates = dict(payload)

        with self.write_lock():
            cfg = self._read_json(self.config_path)
            for raw_k, val in updates.items():
                k = str(raw_k).strip()
                if k in ("project_prefix", "prefix"):
                    s = str(val or "").strip().upper()
                    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", s):
                        raise HTTPException(status_code=400, detail=f"Invalid project_prefix '{val}'. Must match ^[A-Z][A-Z0-9]{{0,9}}$.")
                    cfg["project_prefix"] = s
                elif k in ("project_title", "title", "project_name"):
                    s = str(val or "").strip()
                    if not s:
                        raise HTTPException(status_code=400, detail="project_title cannot be empty.")
                    cfg["project_title"] = s
                elif k == "default_state":
                    try:
                        cfg["default_state"] = normalize_state(str(val))
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                elif k == "default_priority":
                    try:
                        p, _ = normalize_priority(str(val))
                        cfg["default_priority"] = p
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                elif k == "default_category":
                    try:
                        cfg["default_category"] = normalize_category(str(val))
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                elif k == "default_severity":
                    try:
                        sv, _ = normalize_severity(str(val))
                        cfg["default_severity"] = sv
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                elif k == "default_assignee":
                    s = str(val).strip() if val is not None else ""
                    cfg["default_assignee"] = None if s.lower() in ("", "none", "null") else s
                elif k == "wip_limits":
                    if not isinstance(val, dict):
                        raise HTTPException(status_code=400, detail="wip_limits must be an object mapping state -> limit.")
                    clean_wip: Dict[str, int] = {}
                    for wk, wv in val.items():
                        try:
                            wst = normalize_state(str(wk))
                        except ValueError as e:
                            raise HTTPException(status_code=400, detail=str(e))
                        if wv is None or str(wv).strip() in ("", "none", "null", "0"):
                            continue
                        try:
                            lim = int(wv)
                        except (TypeError, ValueError):
                            raise HTTPException(status_code=400, detail=f"Invalid WIP limit '{wv}' for '{wst}'.")
                        if lim < 0:
                            raise HTTPException(status_code=400, detail="WIP limit must be >= 0.")
                        if lim > 0:
                            clean_wip[wst] = lim
                    cfg["wip_limits"] = clean_wip
                elif k.startswith("wip_limits."):
                    sub_st_raw = k.split(".", 1)[1]
                    try:
                        wst = normalize_state(sub_st_raw)
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                    cfg.setdefault("wip_limits", {})
                    if val is None or str(val).strip().lower() in ("", "none", "null", "0"):
                        cfg["wip_limits"].pop(wst, None)
                    else:
                        try:
                            lim = int(val)
                        except (TypeError, ValueError):
                            raise HTTPException(status_code=400, detail=f"Invalid WIP limit '{val}'.")
                        if lim < 0:
                            raise HTTPException(status_code=400, detail="WIP limit must be >= 0.")
                        cfg["wip_limits"][wst] = lim
                elif k == "preferences":
                    if not isinstance(val, dict):
                        raise HTTPException(status_code=400, detail="preferences must be an object.")
                    prefs = dict(cfg.get("preferences") or {})
                    prefs.update(val)
                    cfg["preferences"] = prefs
                elif k.startswith("preferences."):
                    pk = k.split(".", 1)[1].strip()
                    if not pk:
                        raise HTTPException(status_code=400, detail="Invalid preferences key.")
                    if pk == "theme" and str(val).lower() not in ("dark", "light"):
                        raise HTTPException(status_code=400, detail="preferences.theme must be 'dark' or 'light'.")
                    if pk == "page_size":
                        try:
                            ps = int(val)
                            if ps <= 0 or ps > 500:
                                raise ValueError()
                            val = ps
                        except ValueError:
                            raise HTTPException(status_code=400, detail="preferences.page_size must be 1..500.")
                    if pk == "default_swimlane":
                        try:
                            val = normalize_swimlane(str(val))
                        except ValueError as e:
                            raise HTTPException(status_code=400, detail=str(e))
                    cfg.setdefault("preferences", {})[pk] = val
                else:
                    raise HTTPException(status_code=400, detail=f"Unknown or read-only configuration key '{k}'.")

            cfg["updated_at"] = now_iso()
            self._atomic_write_json(self.config_path, cfg)
            self._update_disk_sig_in_db()
            return cfg

    def get_status(self) -> Dict[str, Any]:
        with self.read_lock():
            assert self._db is not None
            cfg = self._read_json(self.config_path)
            total = int(self._db.execute("SELECT COUNT(*) FROM issues").fetchone()[0])
            by_state_rows = self._db.execute("SELECT state, COUNT(*) AS c FROM issues GROUP BY state").fetchall()
            by_state = {r["state"]: int(r["c"]) for r in by_state_rows}
            active_cnt = sum(c for st, c in by_state.items() if st not in TERMINAL_STATES)
            sprints = self._read_json(self.sprints_path, {"sprints": []}).get("sprints", [])
            milestones = self._read_json(self.milestones_path, {"milestones": []}).get("milestones", [])
            tags = self._read_json(self.tags_path, {"tags": []}).get("tags", [])
            views = self._read_json(self.views_path, {"views": []}).get("views", [])
            active_sprints = [s for s in sprints if s.get("state") == "active"]
            return {
                "initialized": True,
                "repo_root": str(self.repo_root),
                "issues_root": str(self.issues_root),
                "project_prefix": cfg.get("project_prefix", "ITR"),
                "project_title": cfg.get("project_title", "iTrack Workspace"),
                "total_issues": total,
                "active_issues": active_cnt,
                "by_state": by_state,
                "tag_count": len(tags),
                "sprint_count": len(sprints),
                "active_sprints": active_sprints,
                "milestone_count": len(milestones),
                "view_count": len(views),
                "index_db": str(self.db_path),
                "version": VERSION,
            }

    def get_stats(self) -> Dict[str, Any]:
        with self.read_lock():
            assert self._db is not None
            folders = [r["folder"] for r in self._db.execute("SELECT folder FROM issues ORDER BY seq ASC").fetchall()]
            raw_pairs = [(self._read_json(self.issues_dir / f / "issue.json"), self.issues_dir / f) for f in folders if (self.issues_dir / f / "issue.json").exists()]
            issues = self._enrich_issues_unlocked(raw_pairs)

            by_state: Dict[str, int] = {st: 0 for st in STATES}
            by_category: Dict[str, int] = {c: 0 for c in CATEGORIES}
            by_priority: Dict[str, int] = {p: 0 for p in PRIORITIES}
            by_severity: Dict[str, int] = {s: 0 for s in SEVERITIES}
            by_assignee: Dict[str, int] = {}
            by_tag: Dict[str, int] = {}

            total_points = 0.0
            completed_points = 0.0
            total_estimate_minutes = 0
            total_logged_minutes = 0
            overdue_count = 0
            blocked_count = 0
            archived_count = 0

            for iss in issues:
                st = iss.get("state", "backlog")
                by_state[st] = by_state.get(st, 0) + 1
                cat = iss.get("category", "task")
                by_category[cat] = by_category.get(cat, 0) + 1
                pr = iss.get("priority", "medium")
                by_priority[pr] = by_priority.get(pr, 0) + 1
                sv = iss.get("severity", "sev3")
                by_severity[sv] = by_severity.get(sv, 0) + 1
                asg = iss.get("assignee") or "unassigned"
                by_assignee[asg] = by_assignee.get(asg, 0) + 1
                for tg in iss.get("tags", []):
                    by_tag[tg] = by_tag.get(tg, 0) + 1

                pts = float(iss.get("story_points") or 0.0)
                total_points += pts
                if st == "done":
                    completed_points += pts
                total_estimate_minutes += int(iss.get("estimate_minutes") or 0)
                total_logged_minutes += int(iss.get("logged_minutes") or 0)
                if iss.get("is_overdue"):
                    overdue_count += 1
                if iss.get("is_blocked"):
                    blocked_count += 1
                if iss.get("archived") or st == "archived":
                    archived_count += 1

            total = len(issues)
            done_count = by_state.get("done", 0)
            active_count = sum(1 for i in issues if i.get("state") not in TERMINAL_STATES)
            sprints = self._read_json(self.sprints_path, {"sprints": []}).get("sprints", [])
            milestones = self._read_json(self.milestones_path, {"milestones": []}).get("milestones", [])

            sprint_velocity: List[Dict[str, Any]] = []
            for sp in sprints:
                sid = sp["id"]
                sp_issues = [i for i in issues if i.get("sprint") == sid]
                sp_done = [i for i in sp_issues if i.get("state") == "done"]
                sprint_velocity.append({
                    "id": sid,
                    "name": sp.get("name", sid),
                    "state": sp.get("state", "planned"),
                    "total_issues": len(sp_issues),
                    "done_issues": len(sp_done),
                    "total_points": round(sum(float(i.get("story_points") or 0.0) for i in sp_issues), 2),
                    "completed_points": round(sum(float(i.get("story_points") or 0.0) for i in sp_done), 2),
                })

            return {
                "total_issues": total,
                "active_issues": active_count,
                "done_issues": done_count,
                "blocked_issues": blocked_count,
                "overdue_issues": overdue_count,
                "archived_issues": archived_count,
                "completion_rate_pct": round(done_count / total * 100.0, 1) if total else 0.0,
                "total_points": round(total_points, 2),
                "completed_points": round(completed_points, 2),
                "total_estimate_minutes": total_estimate_minutes,
                "total_logged_minutes": total_logged_minutes,
                "by_state": by_state,
                "by_category": by_category,
                "by_priority": by_priority,
                "by_severity": by_severity,
                "by_assignee": by_assignee,
                "by_tag": by_tag,
                "sprint_velocity": sprint_velocity,
                "milestone_count": len(milestones),
            }

    def get_tree(self, root_ref: Optional[str] = None) -> Dict[str, Any]:
        with self.read_lock():
            assert self._db is not None
            folders = [r["folder"] for r in self._db.execute("SELECT folder FROM issues ORDER BY seq ASC").fetchall()]
            raw_pairs = [(self._read_json(self.issues_dir / f / "issue.json"), self.issues_dir / f) for f in folders if (self.issues_dir / f / "issue.json").exists()]
            issues = self._enrich_issues_unlocked(raw_pairs)
            by_key = {i["key"]: i for i in issues}
            children_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
            for i in issues:
                p = i.get("parent")
                if p and p not in by_key:
                    p = None
                children_map.setdefault(p, []).append(i)

            def build_node(iss: Dict[str, Any], visited: Set[str]) -> Dict[str, Any]:
                k = iss["key"]
                visited.add(k)
                kids = [
                    build_node(ch, set(visited))
                    for ch in children_map.get(k, [])
                    if ch["key"] not in visited
                ]
                return {
                    "key": k,
                    "name": iss["name"],
                    "category": iss["category"],
                    "state": iss["state"],
                    "priority": iss["priority"],
                    "assignee": iss.get("assignee"),
                    "is_blocked": iss["is_blocked"],
                    "unresolved_blockers": iss["unresolved_blockers"],
                    "links": iss.get("links", []),
                    "children": kids,
                }

            if root_ref and str(root_ref).strip():
                root_iss_raw, _ = self._resolve_issue_unlocked(root_ref)
                root_iss = by_key[root_iss_raw["key"]]
                return {"root": root_iss["key"], "trees": [build_node(root_iss, set())]}

            roots = children_map.get(None, [])
            return {"root": None, "trees": [build_node(r, set()) for r in roots]}


    # ------------------------------------------------------------------------
    # Internal Reference & AC Helpers
    # ------------------------------------------------------------------------

    def _dedupe_strs(self, items: Optional[List[Any]]) -> List[str]:
        if not items:
            return []
        seen: Set[str] = set()
        out: List[str] = []
        for raw in items:
            s = str(raw).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    def _ensure_tags_registered_unlocked(self, tag_names: List[str]) -> None:
        if not tag_names:
            return
        doc = self._read_json(self.tags_path, {"tags": []})
        tags = list(doc.get("tags", []))
        existing = {t["name"].lower() for t in tags if isinstance(t, dict) and "name" in t}
        palette = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"]
        added = False
        ts = now_iso()
        for tname in tag_names:
            if tname.lower() not in existing:
                color = palette[len(tags) % len(palette)]
                tags.append({"name": tname, "color": color, "description": "", "created_at": ts, "updated_at": ts})
                existing.add(tname.lower())
                added = True
        if added:
            self._atomic_write_json(self.tags_path, {"tags": tags})

    def _find_sprint_unlocked(self, sprint_ref: str) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
        ref = urllib.parse.unquote(str(sprint_ref)).strip()
        sprints = list(self._read_json(self.sprints_path, {"sprints": []}).get("sprints", []))
        for idx, sp in enumerate(sprints):
            if str(sp.get("id", "")).upper() == ref.upper() or str(sp.get("name", "")).lower() == ref.lower():
                return sprints, idx, sp
        raise HTTPException(status_code=404, detail=f"Sprint '{ref}' not found.")

    def _find_milestone_unlocked(self, milestone_ref: str) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
        ref = urllib.parse.unquote(str(milestone_ref)).strip()
        milestones = list(self._read_json(self.milestones_path, {"milestones": []}).get("milestones", []))
        for idx, ms in enumerate(milestones):
            if str(ms.get("id", "")).upper() == ref.upper() or str(ms.get("name", "")).lower() == ref.lower():
                return milestones, idx, ms
        raise HTTPException(status_code=404, detail=f"Milestone '{ref}' not found.")

    def _find_view_unlocked(self, view_ref: str) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
        ref = urllib.parse.unquote(str(view_ref)).strip()
        views = list(self._read_json(self.views_path, {"views": []}).get("views", []))
        for idx, vw in enumerate(views):
            if str(vw.get("id", "")).upper() == ref.upper() or str(vw.get("name", "")).lower() == ref.lower():
                return views, idx, vw
        raise HTTPException(status_code=404, detail=f"Saved view '{ref}' not found.")

    def _check_other_issue_name_collision_unlocked(self, candidate_name: str, exclude_key: Optional[str] = None) -> Optional[str]:
        cand_low = candidate_name.lower()
        if self.issues_dir.exists():
            for ent in os.scandir(self.issues_dir):
                if not ent.is_dir(follow_symlinks=False) or ent.name.startswith("."):
                    continue
                if ent.name.lower() == cand_low:
                    ij = Path(ent.path) / "issue.json"
                    if ij.exists():
                        iss = self._read_json(ij)
                        if exclude_key and iss.get("key") == exclude_key:
                            continue
                        return iss.get("key", ent.name)
                    return ent.name
        return None

    def _check_parent_cycle_unlocked(self, child_key: str, candidate_parent_key: str) -> None:
        if child_key == candidate_parent_key:
            raise HTTPException(status_code=409, detail=f"Issue '{child_key}' cannot be its own parent.")
        assert self._db is not None
        rows = self._db.execute("SELECT key, parent FROM issues").fetchall()
        parent_of = {r["key"]: r["parent"] for r in rows}
        curr: Optional[str] = candidate_parent_key
        visited: Set[str] = set()
        while curr:
            if curr == child_key:
                raise HTTPException(
                    status_code=409,
                    detail=f"Setting parent of '{child_key}' to '{candidate_parent_key}' creates a hierarchy cycle.",
                )
            if curr in visited:
                break
            visited.add(curr)
            curr = parent_of.get(curr)

    def _parse_ac_input_list(self, raw_ac: Any) -> List[Dict[str, Any]]:
        if raw_ac is None:
            return []
        if not isinstance(raw_ac, list):
            raise ValueError("acceptance_criteria must be a list.")
        out: List[Dict[str, Any]] = []
        seq = 1
        for item in raw_ac:
            if isinstance(item, dict):
                txt = str(item.get("text", "")).strip()
                if not txt:
                    continue
                checked = bool(item.get("checked", False))
                acid = str(item.get("id") or f"AC-{seq}").strip()
                m = re.search(r"-(\d+)$", acid)
                if m:
                    seq = max(seq, int(m.group(1)))
                out.append({"id": acid, "text": txt, "checked": checked})
                seq += 1
            else:
                s = str(item).strip()
                if not s:
                    continue
                checked = False
                m_chk = re.match(r"^(?:-\s*)?\[([xX ])\]\s*(.+)$", s)
                if m_chk:
                    checked = m_chk.group(1).lower() == "x"
                    s = m_chk.group(2).strip()
                out.append({"id": f"AC-{seq}", "text": s, "checked": checked})
                seq += 1
        return out

    def _find_ac_index(self, ac_list: List[Dict[str, Any]], ref: Any) -> int:
        s = str(ref).strip()
        for i, item in enumerate(ac_list):
            if str(item.get("id", "")).upper() == s.upper():
                return i
        if re.fullmatch(r"\d+", s):
            idx_1 = int(s)
            if 1 <= idx_1 <= len(ac_list):
                return idx_1 - 1
        raise HTTPException(status_code=400, detail=f"Acceptance criterion '{ref}' not found.")

    # ------------------------------------------------------------------------
    # Issues Querying, Search, Pagination & Kanban Board
    # ------------------------------------------------------------------------

    def _make_compiler_unlocked(self) -> IQLCompiler:
        sprints = self._read_json(self.sprints_path, {"sprints": []}).get("sprints", [])
        milestones = self._read_json(self.milestones_path, {"milestones": []}).get("milestones", [])
        sprint_map: Dict[str, str] = {}
        active_sprints: List[str] = []
        for sp in sprints:
            sid = sp["id"]
            sprint_map[sid.lower()] = sid
            sprint_map[str(sp.get("name", "")).lower()] = sid
            if sp.get("state") == "active":
                active_sprints.append(sid)
        milestone_map: Dict[str, str] = {}
        for ms in milestones:
            mid = ms["id"]
            milestone_map[mid.lower()] = mid
            milestone_map[str(ms.get("name", "")).lower()] = mid
        return IQLCompiler(
            current_user=self.default_user,
            sprint_map=sprint_map,
            milestone_map=milestone_map,
            active_sprint_ids=active_sprints,
        )

    def _tab_sql_clause(self, tab: str) -> str:
        if tab == "active":
            return "(issues.state IN ('triage','backlog','todo','ready','in-progress','in-review','in-qa','on-hold','blocked') AND issues.archived = 0)"
        if tab == "open":
            return "(issues.state IN ('triage','backlog','todo','ready') AND issues.archived = 0)"
        if tab == "in-progress":
            return "(issues.state IN ('in-progress','in-review','in-qa') AND issues.archived = 0)"
        if tab == "blocked":
            return f"({IQLCompiler.BLOCKED_SQL} AND issues.state NOT IN ('done','cancelled','archived'))"
        if tab == "completed":
            return "(issues.state = 'done')"
        if tab == "cancelled":
            return "(issues.state = 'cancelled')"
        if tab == "archived":
            return "(issues.state = 'archived' OR issues.archived = 1)"
        return "1 = 1"

    def query_issues(
        self,
        tab: Optional[str] = None,
        q: Optional[str] = None,
        iql: Optional[str] = None,
        category: Optional[str] = None,
        state: Optional[str] = None,
        priority: Optional[str] = None,
        severity: Optional[str] = None,
        resolution: Optional[str] = None,
        assignee: Optional[str] = None,
        reporter: Optional[str] = None,
        tag: Optional[str] = None,
        component: Optional[str] = None,
        parent: Optional[str] = None,
        sprint: Optional[str] = None,
        milestone: Optional[str] = None,
        overdue: Optional[bool] = None,
        sort: Optional[str] = None,
        order: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        if page < 1:
            raise HTTPException(status_code=400, detail="page must be >= 1.")
        if page_size < 1 or page_size > 1000:
            raise HTTPException(status_code=400, detail="page_size must be between 1 and 1000.")

        try:
            eff_tab = normalize_tab(tab) if tab else "all"
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        with self.read_lock():
            assert self._db is not None
            compiler = self._make_compiler_unlocked()
            where_clauses: List[str] = [self._tab_sql_clause(eff_tab)]
            sql_params: List[Any] = []
            order_items: List[Dict[str, str]] = []

            if iql and iql.strip():
                try:
                    ast = parse_iql(iql)
                    w_sql, w_params = compiler.compile_where(ast["where"])
                    where_clauses.append(f"({w_sql})")
                    sql_params.extend(w_params)
                    order_items = ast["order_by"]
                except IQLError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid iQL: {e.message}")

            if q and q.strip():
                try:
                    # Auto-detect if q looks like valid structured iQL with operators
                    q_str = q.strip()
                    used_as_iql = False
                    if not iql and re.search(r"(\b(?:AND|OR|NOT|IN|IS|ORDER\s+BY)\b|[=!<>~])", q_str):
                        try:
                            q_ast = parse_iql(q_str)
                            qw_sql, qw_params = compiler.compile_where(q_ast["where"])
                            where_clauses.append(f"({qw_sql})")
                            sql_params.extend(qw_params)
                            if q_ast["order_by"] and not order_items:
                                order_items = q_ast["order_by"]
                            used_as_iql = True
                        except IQLError:
                            used_as_iql = False
                    if not used_as_iql:
                        ft_sql, ft_params = compiler._compile_cmp("text", "~", q_str)
                        where_clauses.append(f"({ft_sql})")
                        sql_params.extend(ft_params)
                except IQLError as e:
                    raise HTTPException(status_code=400, detail=str(e))

            filter_pairs: List[Tuple[str, Optional[str]]] = [
                ("category", category),
                ("state", state),
                ("priority", priority),
                ("severity", severity),
                ("resolution", resolution),
                ("assignee", assignee),
                ("reporter", reporter),
                ("tags", tag),
                ("components", component),
                ("sprint", sprint),
                ("milestone", milestone),
            ]
            for f_name, f_val in filter_pairs:
                if f_val is not None and str(f_val).strip() != "":
                    raw_s = str(f_val).strip()
                    try:
                        if "," in raw_s and f_name in ("category", "state", "priority", "severity", "tags", "components"):
                            vals = [x.strip() for x in raw_s.split(",") if x.strip()]
                            c_sql, c_params = compiler._compile_in(f_name, False, vals)
                        else:
                            c_sql, c_params = compiler._compile_cmp(f_name, "=", raw_s)
                    except (ValueError, IQLError) as e:
                        raise HTTPException(status_code=400, detail=str(e))
                    where_clauses.append(f"({c_sql})")
                    sql_params.extend(c_params)

            if parent is not None and str(parent).strip() != "":
                p_str = str(parent).strip()
                if p_str.lower() in ("none", "null"):
                    where_clauses.append("(issues.parent IS NULL OR issues.parent = '')")
                else:
                    try:
                        p_iss, _ = self._resolve_issue_unlocked(p_str)
                        p_key = p_iss["key"]
                    except HTTPException:
                        p_key = p_str.upper()
                    where_clauses.append("UPPER(issues.parent) = UPPER(?)")
                    sql_params.append(p_key)

            if overdue is not None:
                ov_sql, ov_params = compiler._compile_cmp("is_overdue", "=", "true" if overdue else "false")
                where_clauses.append(f"({ov_sql})")
                sql_params.extend(ov_params)

            try:
                order_sql = compiler.compile_order_by(order_items, sort_field=sort, sort_order=order)
            except IQLError as e:
                raise HTTPException(status_code=400, detail=e.message)

            full_where = " AND ".join(where_clauses)
            total = int(
                self._db.execute(f"SELECT COUNT(*) FROM issues WHERE {full_where}", sql_params).fetchone()[0]
            )
            total_pages = max(1, (total + page_size - 1) // page_size)
            offset = (page - 1) * page_size

            rows = self._db.execute(
                f"SELECT folder FROM issues WHERE {full_where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                [*sql_params, page_size, offset],
            ).fetchall()

            pairs: List[Tuple[Dict[str, Any], Path]] = []
            for r in rows:
                idir = self.issues_dir / r["folder"]
                if (idir / "issue.json").exists():
                    pairs.append((self._read_json(idir / "issue.json"), idir))
            issues = self._enrich_issues_unlocked(pairs)

            return {
                "issues": issues,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
                "tab": eff_tab,
            }

    def get_board(
        self,
        swimlane: Optional[str] = "none",
        category: Optional[str] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        sprint: Optional[str] = None,
        tag: Optional[str] = None,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            sw = normalize_swimlane(swimlane)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        res = self.query_issues(
            tab="all",
            q=q,
            category=category,
            priority=priority,
            assignee=assignee,
            sprint=sprint,
            tag=tag,
            sort="priority",
            order="desc",
            page=1,
            page_size=1000,
        )
        # Exclude archived from board unless explicitly searched
        issues = [i for i in res["issues"] if not i.get("archived") and i.get("state") != "archived"]
        cfg = self.get_config()
        wip_limits = cfg.get("wip_limits") or {}
        board_states = [s for s in STATES if s != "archived"]

        def build_columns(subset: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            cols: List[Dict[str, Any]] = []
            for st in board_states:
                col_items = [i for i in subset if i.get("state") == st]
                lim = wip_limits.get(st)
                pts = round(sum(float(i.get("story_points") or 0.0) for i in col_items), 2)
                cols.append({
                    "state": st,
                    "count": len(col_items),
                    "points": pts,
                    "wip_limit": lim,
                    "wip_exceeded": bool(lim is not None and len(col_items) > int(lim)),
                    "issues": col_items,
                })
            return cols

        columns = build_columns(issues)
        lanes: List[Dict[str, Any]] = []
        if sw == "none":
            lanes.append({"key": "all", "label": "All Issues", "count": len(issues), "columns": columns})
        elif sw == "category":
            present = [c for c in CATEGORIES if any(i.get("category") == c for i in issues)] or ["task"]
            for c in present:
                sub = [i for i in issues if i.get("category") == c]
                lanes.append({"key": c, "label": c, "count": len(sub), "columns": build_columns(sub)})
        elif sw == "priority":
            for p in PRIORITIES:
                sub = [i for i in issues if i.get("priority") == p]
                if sub or not issues:
                    lanes.append({"key": p, "label": p, "count": len(sub), "columns": build_columns(sub)})
        elif sw == "assignee":
            assignees = sorted({i.get("assignee") or "unassigned" for i in issues}) or ["unassigned"]
            for a in assignees:
                sub = [i for i in issues if (i.get("assignee") or "unassigned") == a]
                lanes.append({"key": a, "label": a, "count": len(sub), "columns": build_columns(sub)})
        elif sw == "sprint":
            sprints_list = self._read_json(self.sprints_path, {"sprints": []}).get("sprints", [])
            sp_names = {s["id"]: f"{s['id']} — {s.get('name', '')}" for s in sprints_list}
            sp_keys = sorted({i.get("sprint") or "backlog" for i in issues}) or ["backlog"]
            for sk in sp_keys:
                sub = [i for i in issues if (i.get("sprint") or "backlog") == sk]
                lanes.append({
                    "key": sk,
                    "label": "Backlog (No Sprint)" if sk == "backlog" else sp_names.get(sk, sk),
                    "count": len(sub),
                    "columns": build_columns(sub),
                })

        return {
            "swimlane": sw,
            "states": board_states,
            "wip_limits": wip_limits,
            "total_issues": len(issues),
            "columns": columns,
            "lanes": lanes,
        }

    # ------------------------------------------------------------------------
    # Issue Create, Edit/Clear, Rename, Clone, Archive, Delete, Context & History
    # ------------------------------------------------------------------------

    def create_issue(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            name = validate_issue_name(data.get("name") if "name" in data else data.get("title"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        with self.write_lock():
            if self._check_other_issue_name_collision_unlocked(name) is not None:
                raise HTTPException(status_code=400, detail=f"An issue named '{name}' already exists.")

            cfg = self._read_json(self.config_path)
            try:
                category = normalize_category(data.get("category"), cfg.get("default_category", "task"))
                state = normalize_state(data.get("state"), cfg.get("default_state", "backlog"))
                priority, p_rank = normalize_priority(data.get("priority"), cfg.get("default_priority", "medium"))
                severity, s_rank = normalize_severity(data.get("severity"), cfg.get("default_severity", "sev3"))
                resolution = normalize_resolution(data.get("resolution"))
                story_points = validate_story_points(data.get("story_points"))
                estimate_minutes = validate_estimate_minutes(data.get("estimate_minutes"))
                due_date = validate_calendar_date(data.get("due_date"), "due_date")
                ac_list = self._parse_ac_input_list(data.get("acceptance_criteria"))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            parent_key: Optional[str] = None
            if data.get("parent") and str(data["parent"]).strip().lower() not in ("", "none", "null"):
                p_iss, _ = self._resolve_issue_unlocked(str(data["parent"]))
                parent_key = p_iss["key"]

            sprint_id: Optional[str] = None
            if data.get("sprint") and str(data["sprint"]).strip().lower() not in ("", "none", "null", "backlog"):
                _, _, sp_obj = self._find_sprint_unlocked(str(data["sprint"]))
                sprint_id = sp_obj["id"]

            milestone_id: Optional[str] = None
            if data.get("milestone") and str(data["milestone"]).strip().lower() not in ("", "none", "null"):
                _, _, ms_obj = self._find_milestone_unlocked(str(data["milestone"]))
                milestone_id = ms_obj["id"]

            raw_cf = data.get("custom_fields")
            custom_fields: Dict[str, Any] = {}
            if raw_cf is not None:
                if not isinstance(raw_cf, dict):
                    raise HTTPException(status_code=400, detail="custom_fields must be an object.")
                for ck, cv in raw_cf.items():
                    ks = str(ck).strip()
                    if ks and cv is not None:
                        custom_fields[ks] = cv

            tags = self._dedupe_strs(data.get("tags"))
            components = self._dedupe_strs(data.get("components"))
            self._ensure_tags_registered_unlocked(tags)

            actor = str(data.get("actor") or data.get("reporter") or self.default_user).strip() or self.default_user
            assignee_raw = data.get("assignee") if "assignee" in data else cfg.get("default_assignee")
            assignee = None if (assignee_raw is None or str(assignee_raw).strip().lower() in ("", "none", "null", "unassigned")) else str(assignee_raw).strip()
            reporter_raw = data.get("reporter") if "reporter" in data else actor
            reporter = None if (reporter_raw is None or str(reporter_raw).strip().lower() in ("", "none", "null")) else str(reporter_raw).strip()

            seq = self._next_seq(cfg, "next_issue_seq")
            prefix = cfg.get("project_prefix", "ITR")
            key = f"{prefix}-{seq}"
            ts = now_iso()

            issue_dir = self.issues_dir / name
            (issue_dir / "attachments").mkdir(parents=True, exist_ok=True)
            (issue_dir / "comments").mkdir(parents=True, exist_ok=True)

            archived = state == "archived" or bool(data.get("archived", False))
            if archived and state != "archived":
                prev_st = state
                state = "archived"
            else:
                prev_st = None

            issue: Dict[str, Any] = {
                "key": key,
                "seq": seq,
                "name": name,
                "folder": name,
                "category": category,
                "state": state,
                "previous_state": prev_st,
                "archived": archived,
                "priority": priority,
                "priority_rank": p_rank,
                "severity": severity,
                "severity_rank": s_rank,
                "resolution": resolution,
                "description": str(data.get("description") or ""),
                "description_embeds": [],
                "acceptance_criteria": ac_list,
                "assignee": assignee,
                "reporter": reporter,
                "tags": tags,
                "components": components,
                "parent": parent_key,
                "sprint": sprint_id,
                "milestone": milestone_id,
                "story_points": story_points,
                "estimate_minutes": estimate_minutes,
                "logged_minutes": 0,
                "due_date": due_date,
                "custom_fields": custom_fields,
                "attachments": [],
                "deleted_attachments": {},
                "links": [],
                "worklogs": [],
                "cloned_from": data.get("cloned_from"),
                "created_at": ts,
                "updated_at": ts,
            }

            self._append_history(
                issue_dir,
                cfg,
                actor=actor,
                action="created",
                diffs=[
                    {"field": "name", "old": None, "new": name},
                    {"field": "category", "old": None, "new": category},
                    {"field": "state", "old": None, "new": state},
                    {"field": "priority", "old": None, "new": priority},
                    {"field": "severity", "old": None, "new": severity},
                ],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(issue, issue_dir)
            return self._enrich_issues_unlocked([(issue, issue_dir)])[0]

    def _rename_issue_unlocked(self, iss: Dict[str, Any], old_dir: Path, raw_new_name: Any, actor: str) -> Tuple[Dict[str, Any], Path]:
        try:
            new_name = validate_issue_name(raw_new_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        old_name = iss["name"]
        if new_name == old_name and old_dir.name == new_name:
            return iss, old_dir

        collider = self._check_other_issue_name_collision_unlocked(new_name, exclude_key=iss["key"])
        if collider is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot rename '{old_name}' to '{new_name}': issue '{collider}' already uses that name.",
            )

        target_dir = self.issues_dir / new_name
        case_only = old_name.lower() == new_name.lower()
        if not case_only and target_dir.exists():
            raise HTTPException(status_code=409, detail=f"Target folder '{new_name}' already exists.")

        tmp_dir: Optional[Path] = None
        moved = False
        try:
            if case_only:
                tmp_dir = self.issues_dir / f".rename_tmp_{uuid.uuid4().hex}"
                os.replace(old_dir, tmp_dir)
                os.replace(tmp_dir, target_dir)
            else:
                os.replace(old_dir, target_dir)
            moved = True

            ts = now_iso()
            iss["name"] = new_name
            iss["folder"] = new_name
            iss["updated_at"] = ts

            cfg = self._read_json(self.config_path)
            self._append_history(
                target_dir,
                cfg,
                actor=actor,
                action="renamed",
                diffs=[{"field": "name", "old": old_name, "new": new_name}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)

            # Update cached target_name in peer linked issues
            for ent in os.scandir(self.issues_dir):
                if not ent.is_dir(follow_symlinks=False) or ent.name.startswith(".") or ent.name == new_name:
                    continue
                p_json = Path(ent.path) / "issue.json"
                if p_json.exists():
                    peer = self._read_json(p_json)
                    changed_peer = False
                    for lnk in peer.get("links", []):
                        if lnk.get("target_key") == iss["key"] and lnk.get("target_name") != new_name:
                            lnk["target_name"] = new_name
                            changed_peer = True
                    if changed_peer:
                        self._atomic_write_json(p_json, peer)

            self._save_issue_unlocked(iss, target_dir)
            return iss, target_dir
        except Exception:
            if moved and target_dir.exists() and not old_dir.exists():
                with contextlib.suppress(Exception):
                    os.replace(target_dir, old_dir)
            elif tmp_dir is not None and tmp_dir.exists() and not old_dir.exists():
                with contextlib.suppress(Exception):
                    os.replace(tmp_dir, old_dir)
            raise

    def rename_issue(self, issue_ref: str, new_name: Any, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            eff_actor = str(actor or self.default_user).strip() or self.default_user
            iss, new_dir = self._rename_issue_unlocked(iss, idir, new_name, eff_actor)
            return self._enrich_issues_unlocked([(iss, new_dir)])[0]

    def update_issue(self, issue_ref: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            actor = str(patch.get("actor") or self.default_user).strip() or self.default_user

            if "name" in patch and patch["name"] is not None:
                iss, idir = self._rename_issue_unlocked(iss, idir, patch["name"], actor)

            diffs: List[Dict[str, Any]] = []

            def record(field: str, new_val: Any) -> None:
                old_val = iss.get(field)
                if old_val != new_val:
                    diffs.append({"field": field, "old": old_val, "new": new_val})
                    iss[field] = new_val

            try:
                if "category" in patch and patch["category"] is not None:
                    record("category", normalize_category(str(patch["category"])))

                if "state" in patch and patch["state"] is not None:
                    new_st = normalize_state(str(patch["state"]))
                    cur_st = iss.get("state")
                    if new_st == "archived" and cur_st != "archived":
                        iss["previous_state"] = cur_st
                        record("archived", True)
                    elif new_st != "archived" and iss.get("archived"):
                        record("archived", False)
                    record("state", new_st)

                if "priority" in patch and patch["priority"] is not None:
                    pr, prank = normalize_priority(str(patch["priority"]))
                    record("priority", pr)
                    iss["priority_rank"] = prank

                if "severity" in patch and patch["severity"] is not None:
                    sv, srank = normalize_severity(str(patch["severity"]))
                    record("severity", sv)
                    iss["severity_rank"] = srank

                if patch.get("clear_resolution"):
                    record("resolution", None)
                elif "resolution" in patch:
                    record("resolution", normalize_resolution(patch["resolution"]))

                if patch.get("clear_desc") or patch.get("clear_description"):
                    record("description", "")
                elif "description" in patch and patch["description"] is not None:
                    record("description", str(patch["description"]))

                if patch.get("clear_assignee"):
                    record("assignee", None)
                elif "assignee" in patch:
                    av = patch["assignee"]
                    record("assignee", None if (av is None or str(av).strip().lower() in ("", "none", "null", "unassigned")) else str(av).strip())

                if patch.get("clear_reporter"):
                    record("reporter", None)
                elif "reporter" in patch:
                    rv = patch["reporter"]
                    record("reporter", None if (rv is None or str(rv).strip().lower() in ("", "none", "null")) else str(rv).strip())

                # Tags
                cur_tags = list(iss.get("tags", []))
                if patch.get("clear_tags"):
                    cur_tags = []
                elif "tags" in patch and patch["tags"] is not None:
                    cur_tags = self._dedupe_strs(patch["tags"])
                if patch.get("add_tags"):
                    cur_tags = self._dedupe_strs(cur_tags + list(patch["add_tags"]))
                if patch.get("remove_tags"):
                    rem_t = {str(x).strip().lower() for x in patch["remove_tags"]}
                    cur_tags = [t for t in cur_tags if t.lower() not in rem_t]
                if cur_tags != iss.get("tags", []):
                    self._ensure_tags_registered_unlocked(cur_tags)
                    record("tags", cur_tags)

                # Components
                cur_comps = list(iss.get("components", []))
                if patch.get("clear_components"):
                    cur_comps = []
                elif "components" in patch and patch["components"] is not None:
                    cur_comps = self._dedupe_strs(patch["components"])
                if patch.get("add_components"):
                    cur_comps = self._dedupe_strs(cur_comps + list(patch["add_components"]))
                if patch.get("remove_components"):
                    rem_c = {str(x).strip().lower() for x in patch["remove_components"]}
                    cur_comps = [c for c in cur_comps if c.lower() not in rem_c]
                if cur_comps != iss.get("components", []):
                    record("components", cur_comps)

                # Parent
                if patch.get("clear_parent"):
                    record("parent", None)
                elif "parent" in patch:
                    pv = patch["parent"]
                    if pv is None or str(pv).strip().lower() in ("", "none", "null"):
                        record("parent", None)
                    else:
                        p_iss, _ = self._resolve_issue_unlocked(str(pv))
                        self._check_parent_cycle_unlocked(iss["key"], p_iss["key"])
                        record("parent", p_iss["key"])

                # Sprint
                if patch.get("clear_sprint"):
                    record("sprint", None)
                elif "sprint" in patch:
                    sv_raw = patch["sprint"]
                    if sv_raw is None or str(sv_raw).strip().lower() in ("", "none", "null", "backlog"):
                        record("sprint", None)
                    else:
                        _, _, sp_obj = self._find_sprint_unlocked(str(sv_raw))
                        record("sprint", sp_obj["id"])

                # Milestone
                if patch.get("clear_milestone"):
                    record("milestone", None)
                elif "milestone" in patch:
                    mv_raw = patch["milestone"]
                    if mv_raw is None or str(mv_raw).strip().lower() in ("", "none", "null"):
                        record("milestone", None)
                    else:
                        _, _, ms_obj = self._find_milestone_unlocked(str(mv_raw))
                        record("milestone", ms_obj["id"])

                # Points, Estimate, Due Date
                if patch.get("clear_points"):
                    record("story_points", None)
                elif "story_points" in patch:
                    record("story_points", validate_story_points(patch["story_points"]))

                if patch.get("clear_estimate"):
                    record("estimate_minutes", None)
                elif "estimate_minutes" in patch:
                    record("estimate_minutes", validate_estimate_minutes(patch["estimate_minutes"]))

                if patch.get("clear_due"):
                    record("due_date", None)
                elif "due_date" in patch:
                    record("due_date", validate_calendar_date(patch["due_date"], "due_date"))

                # Acceptance Criteria mutations
                cur_ac = [dict(a) for a in (iss.get("acceptance_criteria") or [])]
                ac_modified = False
                if patch.get("clear_ac"):
                    cur_ac = []
                    ac_modified = True
                elif "acceptance_criteria" in patch and patch["acceptance_criteria"] is not None:
                    cur_ac = self._parse_ac_input_list(patch["acceptance_criteria"])
                    ac_modified = True

                if patch.get("add_ac"):
                    max_ac_id = max(
                        (int(re.search(r"-(\d+)$", str(a.get("id", "0"))).group(1)) for a in cur_ac if re.search(r"-(\d+)$", str(a.get("id", "")))),
                        default=len(cur_ac),
                    )
                    for raw_txt in patch["add_ac"]:
                        s = str(raw_txt).strip()
                        if not s:
                            continue
                        chk = False
                        m_chk = re.match(r"^(?:-\s*)?\[([xX ])\]\s*(.+)$", s)
                        if m_chk:
                            chk = m_chk.group(1).lower() == "x"
                            s = m_chk.group(2).strip()
                        max_ac_id += 1
                        cur_ac.append({"id": f"AC-{max_ac_id}", "text": s, "checked": chk})
                        ac_modified = True

                if patch.get("edit_ac"):
                    edits = patch["edit_ac"]
                    if isinstance(edits, dict):
                        edits = [{"target": k, "text": v} for k, v in edits.items()]
                    for ed in edits:
                        idx = self._find_ac_index(cur_ac, ed["target"])
                        ntxt = str(ed.get("text", "")).strip()
                        if not ntxt:
                            raise ValueError("Acceptance criterion text cannot be empty.")
                        cur_ac[idx]["text"] = ntxt
                        ac_modified = True

                if patch.get("check_ac"):
                    for ref in patch["check_ac"]:
                        idx = self._find_ac_index(cur_ac, ref)
                        cur_ac[idx]["checked"] = True
                        ac_modified = True

                if patch.get("uncheck_ac"):
                    for ref in patch["uncheck_ac"]:
                        idx = self._find_ac_index(cur_ac, ref)
                        cur_ac[idx]["checked"] = False
                        ac_modified = True

                if patch.get("remove_ac"):
                    # Resolve indices first then remove descending so 1-based positions stay stable
                    rem_indices = sorted({self._find_ac_index(cur_ac, ref) for ref in patch["remove_ac"]}, reverse=True)
                    for ridx in rem_indices:
                        cur_ac.pop(ridx)
                    ac_modified = True

                if ac_modified:
                    record("acceptance_criteria", cur_ac)

                # Custom Fields mutations
                cur_cf = dict(iss.get("custom_fields") or {})
                cf_modified = False
                if patch.get("clear_fields"):
                    cur_cf = {}
                    cf_modified = True
                elif patch.get("replace_custom_fields") and isinstance(patch.get("custom_fields"), dict):
                    cur_cf = {str(k).strip(): v for k, v in patch["custom_fields"].items() if str(k).strip() and v is not None}
                    cf_modified = True
                elif "custom_fields" in patch and patch["custom_fields"] is not None:
                    if not isinstance(patch["custom_fields"], dict):
                        raise ValueError("custom_fields must be an object.")
                    if len(patch["custom_fields"]) == 0:
                        if cur_cf:
                            cur_cf = {}
                            cf_modified = True
                    else:
                        for ck, cv in patch["custom_fields"].items():
                            ks = str(ck).strip()
                            if not ks:
                                continue
                            if cv is None:
                                cur_cf.pop(ks, None)
                            else:
                                cur_cf[ks] = cv
                            cf_modified = True

                if patch.get("set_fields") and isinstance(patch["set_fields"], dict):
                    for ck, cv in patch["set_fields"].items():
                        ks = str(ck).strip()
                        if ks:
                            cur_cf[ks] = cv
                            cf_modified = True

                if patch.get("unset_fields"):
                    for ck in patch["unset_fields"]:
                        cur_cf.pop(str(ck).strip(), None)
                        cf_modified = True

                if cf_modified:
                    record("custom_fields", cur_cf)

            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            if diffs:
                ts = now_iso()
                iss["updated_at"] = ts
                cfg = self._read_json(self.config_path)
                self._append_history(idir, cfg, actor=actor, action="updated", diffs=diffs, timestamp=ts)
                self._atomic_write_json(self.config_path, cfg)
                self._save_issue_unlocked(iss, idir)

            return self._enrich_issues_unlocked([(iss, idir)])[0]

    def clone_issue(self, issue_ref: str, new_name: Optional[str] = None, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            orig, _ = self._resolve_issue_unlocked(issue_ref)
            eff_actor = str(actor or self.default_user).strip() or self.default_user
            if new_name and str(new_name).strip():
                target_name = str(new_name).strip()
            else:
                base = f"Copy of {orig['name']}"[:170]
                target_name = base
                n = 2
                while self._check_other_issue_name_collision_unlocked(target_name) is not None:
                    target_name = f"Copy {n} of {orig['name']}"[:175]
                    n += 1

            cfg = self._read_json(self.config_path)
            cloned = self.create_issue({
                "name": target_name,
                "category": orig.get("category"),
                "state": cfg.get("default_state", "backlog"),
                "priority": orig.get("priority"),
                "severity": orig.get("severity"),
                "description": orig.get("description", ""),
                "acceptance_criteria": [{"text": a.get("text", ""), "checked": False} for a in (orig.get("acceptance_criteria") or [])],
                "assignee": orig.get("assignee"),
                "reporter": eff_actor,
                "tags": list(orig.get("tags", [])),
                "components": list(orig.get("components", [])),
                "parent": orig.get("parent"),
                "sprint": orig.get("sprint"),
                "milestone": orig.get("milestone"),
                "story_points": orig.get("story_points"),
                "estimate_minutes": orig.get("estimate_minutes"),
                "due_date": orig.get("due_date"),
                "custom_fields": dict(orig.get("custom_fields") or {}),
                "cloned_from": orig["key"],
                "actor": eff_actor,
            })
            self.add_link(cloned["key"], "clones", orig["key"], actor=eff_actor)
            return self.get_issue(cloned["key"])

    def archive_issue(self, issue_ref: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            eff_actor = str(actor or self.default_user).strip() or self.default_user
            old_state = iss.get("state", "backlog")
            if not iss.get("archived") or old_state != "archived":
                if old_state != "archived":
                    iss["previous_state"] = old_state
                iss["state"] = "archived"
                iss["archived"] = True
                ts = now_iso()
                iss["updated_at"] = ts
                cfg = self._read_json(self.config_path)
                self._append_history(
                    idir,
                    cfg,
                    actor=eff_actor,
                    action="archived",
                    diffs=[{"field": "state", "old": old_state, "new": "archived"}, {"field": "archived", "old": False, "new": True}],
                    timestamp=ts,
                )
                self._atomic_write_json(self.config_path, cfg)
                self._save_issue_unlocked(iss, idir)
            return self._enrich_issues_unlocked([(iss, idir)])[0]

    def unarchive_issue(self, issue_ref: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            eff_actor = str(actor or self.default_user).strip() or self.default_user
            cfg = self._read_json(self.config_path)
            restore_state = iss.get("previous_state") or cfg.get("default_state", "backlog")
            if restore_state == "archived":
                restore_state = cfg.get("default_state", "backlog")
            old_state = iss.get("state", "archived")
            iss["state"] = restore_state
            iss["archived"] = False
            ts = now_iso()
            iss["updated_at"] = ts
            self._append_history(
                idir,
                cfg,
                actor=eff_actor,
                action="unarchived",
                diffs=[{"field": "state", "old": old_state, "new": restore_state}, {"field": "archived", "old": True, "new": False}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return self._enrich_issues_unlocked([(iss, idir)])[0]

    def delete_issue(self, issue_ref: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            del_key = iss["key"]
            del_name = iss["name"]
            ts = now_iso()

            # Scrub cross-links & child parent references across all other issues
            for ent in os.scandir(self.issues_dir):
                if not ent.is_dir(follow_symlinks=False) or ent.name.startswith(".") or Path(ent.path) == idir:
                    continue
                p_dir = Path(ent.path)
                p_json = p_dir / "issue.json"
                if not p_json.exists():
                    continue
                peer = self._read_json(p_json)
                changed = False
                orig_links = peer.get("links", [])
                new_links = [l for l in orig_links if l.get("target_key") != del_key]
                if len(new_links) != len(orig_links):
                    peer["links"] = new_links
                    changed = True
                if peer.get("parent") == del_key:
                    peer["parent"] = None
                    changed = True
                if changed:
                    peer["updated_at"] = ts
                    self._atomic_write_json(p_json, peer)

            # Delete entire folder (issue.json, history.jsonl, attachments/, comments/)
            shutil.rmtree(idir, ignore_errors=True)

            assert self._db is not None
            cur = self._db.cursor()
            cur.execute("DELETE FROM issues WHERE key = ?", (del_key,))
            cur.execute("DELETE FROM issue_tags WHERE issue_key = ?", (del_key,))
            cur.execute("DELETE FROM issue_components WHERE issue_key = ?", (del_key,))
            cur.execute("DELETE FROM issue_links WHERE blocker_key = ? OR blocked_key = ?", (del_key, del_key))
            cur.execute("DELETE FROM issues_fts WHERE key = ?", (del_key,))
            self._db.commit()

            # Re-index any modified peers cleanly
            self._rebuild_index_unlocked()
            return {"deleted": True, "key": del_key, "name": del_name}

    def get_issue_history(self, issue_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            h_path = idir / "history.jsonl"
            entries: List[Dict[str, Any]] = []
            if h_path.exists():
                with open(h_path, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if s:
                            with contextlib.suppress(Exception):
                                entries.append(json.loads(s))
            return {"key": iss["key"], "name": iss["name"], "history": entries}

    def get_issue_context(self, issue_ref: str) -> Dict[str, Any]:
        iss = self.get_issue(issue_ref)
        hist = self.get_issue_history(iss["key"])["history"]
        lines: List[str] = [
            f"# Agent Context Pack: [{iss['key']}] {iss['name']}",
            "",
            "## Metadata",
            f"- **Key:** `{iss['key']}`",
            f"- **Name (Folder):** `itrack-issues/issues/{iss['folder']}/`",
            f"- **Category:** `{iss['category']}` | **State:** `{iss['state']}` | **Priority:** `{iss['priority']}` | **Severity:** `{iss['severity']}`",
            f"- **Resolution:** `{iss.get('resolution') or 'none'}`",
            f"- **Assignee:** `{iss.get('assignee') or 'unassigned'}` | **Reporter:** `{iss.get('reporter') or 'none'}`",
            f"- **Parent:** `{iss.get('parent') or 'none'}` | **Sprint:** `{iss.get('sprint') or 'none'}` | **Milestone:** `{iss.get('milestone') or 'none'}`",
            f"- **Story Points:** `{iss.get('story_points')}` | **Estimate:** `{iss.get('estimate_minutes')}m` | **Logged:** `{iss.get('logged_minutes')}m` | **Due:** `{iss.get('due_date') or 'none'}`",
            f"- **Tags:** {', '.join(iss.get('tags', [])) or 'none'}",
            f"- **Components:** {', '.join(iss.get('components', [])) or 'none'}",
            f"- **Blocked:** `{iss['is_blocked']}` (Blockers: {', '.join(iss['unresolved_blockers']) or 'none'})",
        ]
        if iss.get("custom_fields"):
            lines.append(f"- **Custom Fields:** `{json.dumps(iss['custom_fields'], sort_keys=True)}`")

        lines.extend(["", "## Description", iss.get("description") or "_No description._"])

        if iss.get("acceptance_criteria"):
            lines.extend(["", "## Acceptance Criteria"])
            for ac in iss["acceptance_criteria"]:
                mark = "x" if ac.get("checked") else " "
                lines.append(f"- [{mark}] ({ac['id']}) {ac['text']}")

        if iss.get("subtasks"):
            lines.extend(["", "## Subtasks"])
            for sub in iss["subtasks"]:
                mark = "x" if sub["completed"] else " "
                lines.append(f"- [{mark}] `{sub['key']}` {sub['name']} (`{sub['state']}`)")

        if iss.get("links"):
            lines.extend(["", "## Linked Issues"])
            for lnk in iss["links"]:
                lines.append(f"- `{lnk['type']}` -> `{lnk['target_key']}` ({lnk.get('target_name', '')})")

        if iss.get("attachments") or iss.get("description_embeds"):
            lines.extend(["", "## Issue Attachments & Embeds"])
            for a in list(iss.get("attachments", [])) + list(iss.get("description_embeds", [])):
                lines.append(f"- `{a['id']}` `{a['filename']}` ({a['mime_type']}, {a['size_bytes']} bytes)")

        if iss.get("comments"):
            lines.extend(["", "## Discussion Comments"])
            for c in iss["comments"]:
                lines.append(f"### {c['id']} by {c['author']} ({c['created_at']})")
                lines.append(c.get("body") or "")
                if c.get("linked_attachments"):
                    for la in c["linked_attachments"]:
                        status_note = " [DELETED]" if la.get("deleted") else ""
                        lines.append(f"- Linked attachment: `{la['id']}` `{la.get('filename', '')}`{status_note}")

        md = "\n".join(lines) + "\n"
        return {
            "key": iss["key"],
            "name": iss["name"],
            "markdown": md,
            "context": {
                "issue": iss,
                "recent_history": hist[-20:],
            },
        }


    # ------------------------------------------------------------------------
    # Embeds, Attachments & Rich Comments Matrix
    # ------------------------------------------------------------------------

    def _store_file_bytes_unlocked(
        self,
        dest_dir: Path,
        cfg: Dict[str, Any],
        raw_filename: Optional[str],
        content_type: Optional[str],
        data: bytes,
        scope: str,
        uploaded_by: str,
        comment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        display_name, safe_suffix = sanitize_filename(raw_filename)
        att_seq = self._next_seq(cfg, "next_att_seq")
        att_id = f"ATT-{att_seq}"
        stored_name = f"{att_id}_{safe_suffix}"
        target_path = (dest_dir / stored_name).resolve()
        if not target_path.is_relative_to(dest_dir.resolve()):
            raise HTTPException(status_code=400, detail="Invalid attachment filename.")
        _atomic_write_bytes(target_path, data)

        guessed_mime = mimetypes.guess_type(display_name)[0]
        mime = (
            content_type
            if (content_type and content_type != "application/octet-stream")
            else (guessed_mime or content_type or "application/octet-stream")
        )
        meta: Dict[str, Any] = {
            "id": att_id,
            "filename": display_name,
            "stored_name": stored_name,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "scope": scope,
            "is_image": is_image_file(display_name, mime),
            "uploaded_by": uploaded_by or self.default_user,
            "created_at": now_iso(),
        }
        if comment_id:
            meta["comment_id"] = comment_id
        return meta

    @staticmethod
    def _apply_embed_to_markdown(body: str, filename: str, att_id: str) -> str:
        text = body or ""
        esc_fn = re.escape(filename)
        md_pat = re.compile(rf"!\[([^\]]*)\]\(embed:{esc_fn}\)")
        if md_pat.search(text):
            return md_pat.sub(lambda m: f"![{m.group(1) or filename}](attachment://{att_id})", text, count=1)
        bare_pat = re.compile(rf"(?<!!\[)(?<!\()embed:{esc_fn}")
        if bare_pat.search(text):
            return bare_pat.sub(f"![{filename}](attachment://{att_id})", text, count=1)
        if f"attachment://{att_id}" in text:
            return text
        tag = f"![{filename}](attachment://{att_id})"
        return tag if not text.strip() else f"{text.rstrip()}\n\n{tag}"

    @staticmethod
    def _strip_embed_from_markdown(body: str, att_id: str) -> str:
        text = body or ""
        esc_id = re.escape(att_id)
        cleaned = re.sub(rf"!\[[^\]]*\]\(attachment://{esc_id}\)", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def add_description_embeds(
        self,
        issue_ref: str,
        files_payload: List[Tuple[str, Optional[str], bytes]],
        uploaded_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not files_payload:
            raise HTTPException(status_code=400, detail="At least one embed file is required.")
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            cfg = self._read_json(self.config_path)
            actor = str(uploaded_by or self.default_user).strip() or self.default_user
            att_dir = idir / "attachments"
            desc = iss.get("description") or ""
            added: List[Dict[str, Any]] = []
            for fname, ctype, raw_bytes in files_payload:
                meta = self._store_file_bytes_unlocked(att_dir, cfg, fname, ctype, raw_bytes, "description_embed", actor)
                desc = self._apply_embed_to_markdown(desc, meta["filename"], meta["id"])
                iss.setdefault("description_embeds", []).append(meta)
                added.append(meta)

            ts = now_iso()
            old_desc = iss.get("description") or ""
            iss["description"] = desc
            iss["updated_at"] = ts
            self._append_history(
                idir,
                cfg,
                actor=actor,
                action="description_embed_added",
                diffs=[
                    {"field": "description_embeds", "old": None, "new": [m["id"] for m in added]},
                    {"field": "description", "old": old_desc, "new": desc},
                ],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return {"key": iss["key"], "embeds": added, "issue": self._enrich_issues_unlocked([(iss, idir)])[0]}

    def delete_description_embed(self, issue_ref: str, att_id: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            up_id = att_id.strip().upper()
            embeds = list(iss.get("description_embeds") or [])
            found = next((e for e in embeds if e["id"].upper() == up_id), None)
            if not found:
                raise HTTPException(status_code=404, detail=f"Description embed '{att_id}' not found on {iss['key']}.")

            fpath = idir / "attachments" / found["stored_name"]
            fpath.unlink(missing_ok=True)
            iss["description_embeds"] = [e for e in embeds if e["id"].upper() != up_id]
            old_desc = iss.get("description") or ""
            iss["description"] = self._strip_embed_from_markdown(old_desc, found["id"])
            ts = now_iso()
            iss["updated_at"] = ts
            cfg = self._read_json(self.config_path)
            self._append_history(
                idir,
                cfg,
                actor=str(actor or self.default_user).strip() or self.default_user,
                action="description_embed_deleted",
                diffs=[{"field": "description_embeds", "old": found["id"], "new": None}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return {"deleted": True, "id": found["id"], "issue": self._enrich_issues_unlocked([(iss, idir)])[0]}

    def add_issue_attachments(
        self,
        issue_ref: str,
        files_payload: List[Tuple[str, Optional[str], bytes]],
        uploaded_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not files_payload:
            raise HTTPException(status_code=400, detail="At least one attachment file is required.")
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            cfg = self._read_json(self.config_path)
            actor = str(uploaded_by or self.default_user).strip() or self.default_user
            att_dir = idir / "attachments"
            added: List[Dict[str, Any]] = []
            for fname, ctype, raw_bytes in files_payload:
                meta = self._store_file_bytes_unlocked(att_dir, cfg, fname, ctype, raw_bytes, "issue", actor)
                iss.setdefault("attachments", []).append(meta)
                added.append(meta)

            ts = now_iso()
            iss["updated_at"] = ts
            self._append_history(
                idir,
                cfg,
                actor=actor,
                action="attachment_added",
                diffs=[{"field": "attachments", "old": None, "new": [m["id"] for m in added]}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return {"key": iss["key"], "attachments": added}

    def list_attachments(self, issue_ref: str, scope: str = "all") -> Dict[str, Any]:
        sc = (scope or "all").strip().lower()
        if sc not in ("issue", "comments", "all"):
            raise HTTPException(status_code=400, detail="Invalid scope. Expected 'issue', 'comments', or 'all'.")
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            out: List[Dict[str, Any]] = []
            if sc in ("issue", "all"):
                out.extend(list(iss.get("attachments") or []))
                out.extend(list(iss.get("description_embeds") or []))
            if sc in ("comments", "all"):
                for c in self._load_raw_comments_from_dir(idir):
                    out.extend(list(c.get("embeds") or []))
                    out.extend(list(c.get("attachments") or []))
            return {"key": iss["key"], "scope": sc, "attachments": out}

    def _locate_attachment_unlocked(
        self, iss: Dict[str, Any], idir: Path, att_id: str
    ) -> Tuple[Dict[str, Any], Path, str, Optional[Dict[str, Any]]]:
        up = att_id.strip().upper()
        for a in iss.get("attachments") or []:
            if a["id"].upper() == up:
                return a, idir / "attachments" / a["stored_name"], "issue", None
        for e in iss.get("description_embeds") or []:
            if e["id"].upper() == up:
                return e, idir / "attachments" / e["stored_name"], "description_embed", None
        for c in self._load_raw_comments_from_dir(idir):
            cid = c["id"]
            for e in c.get("embeds") or []:
                if e["id"].upper() == up:
                    return e, idir / "comments" / cid / "files" / e["stored_name"], "comment_embed", c
            for ca in c.get("attachments") or []:
                if ca["id"].upper() == up:
                    return ca, idir / "comments" / cid / "files" / ca["stored_name"], "comment", c
        raise HTTPException(status_code=404, detail=f"Attachment '{att_id}' not found on issue '{iss['key']}'.")

    def get_attachment_info(self, issue_ref: str, att_id: str) -> Dict[str, Any]:
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            meta, fpath, _, _ = self._locate_attachment_unlocked(iss, idir, att_id)
            info = dict(meta)
            info["issue_key"] = iss["key"]
            info["exists_on_disk"] = fpath.exists()
            preview_text: Optional[str] = None
            if fpath.exists() and is_text_previewable(meta["filename"], meta.get("mime_type", "")):
                with contextlib.suppress(Exception):
                    with open(fpath, "rb") as f:
                        raw = f.read(65536)
                    preview_text = raw.decode("utf-8", errors="replace")
            info["preview_text"] = preview_text
            return info

    def get_attachment_download(self, issue_ref: str, att_id: str) -> Tuple[Path, Dict[str, Any]]:
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            meta, fpath, _, _ = self._locate_attachment_unlocked(iss, idir, att_id)
            if not fpath.exists():
                raise HTTPException(status_code=404, detail=f"Attachment file '{att_id}' missing on disk.")
            return fpath, meta

    def delete_attachment(self, issue_ref: str, att_id: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            meta, fpath, kind, cmt = self._locate_attachment_unlocked(iss, idir, att_id)
            real_id = meta["id"]
            fpath.unlink(missing_ok=True)
            ts = now_iso()
            eff_actor = str(actor or self.default_user).strip() or self.default_user

            if kind == "issue":
                iss["attachments"] = [a for a in (iss.get("attachments") or []) if a["id"] != real_id]
                iss.setdefault("deleted_attachments", {})[real_id] = {
                    "id": real_id,
                    "filename": meta["filename"],
                    "deleted_at": ts,
                }
            elif kind == "description_embed":
                iss["description_embeds"] = [e for e in (iss.get("description_embeds") or []) if e["id"] != real_id]
                iss["description"] = self._strip_embed_from_markdown(iss.get("description") or "", real_id)
            elif cmt is not None:
                c_path = idir / "comments" / cmt["id"] / "comment.json"
                if kind == "comment_embed":
                    cmt["embeds"] = [e for e in (cmt.get("embeds") or []) if e["id"] != real_id]
                    cmt["body"] = self._strip_embed_from_markdown(cmt.get("body") or "", real_id)
                else:
                    cmt["attachments"] = [a for a in (cmt.get("attachments") or []) if a["id"] != real_id]
                cmt["updated_at"] = ts
                self._atomic_write_json(c_path, cmt)

            iss["updated_at"] = ts
            cfg = self._read_json(self.config_path)
            self._append_history(
                idir,
                cfg,
                actor=eff_actor,
                action="attachment_deleted",
                diffs=[{"field": "attachments", "old": real_id, "new": None}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return {"deleted": True, "id": real_id, "filename": meta["filename"], "scope": kind}

    # --- Rich Comments ---

    def list_comments(self, issue_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            raw_cmts = self._load_raw_comments_from_dir(idir)
            return {"key": iss["key"], "comments": [self._enrich_comment(c, iss) for c in raw_cmts]}

    def get_comment(self, issue_ref: str, comment_id: str) -> Dict[str, Any]:
        with self.read_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            cid = comment_id.strip().upper()
            c_path = idir / "comments" / cid / "comment.json"
            if not c_path.exists():
                raise HTTPException(status_code=404, detail=f"Comment '{comment_id}' not found on {iss['key']}.")
            return self._enrich_comment(self._read_json(c_path), iss)

    def _validate_linkable_issue_attachments_unlocked(self, iss: Dict[str, Any], ids: List[str]) -> List[str]:
        existing = {a["id"].upper(): a["id"] for a in (iss.get("attachments") or [])}
        resolved: List[str] = []
        for raw_id in ids:
            up = str(raw_id).strip().upper()
            if not up:
                continue
            if up not in existing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Issue attachment '{raw_id}' does not exist on issue '{iss['key']}'.",
                )
            canon = existing[up]
            if canon not in resolved:
                resolved.append(canon)
        return resolved

    def add_comment(
        self,
        issue_ref: str,
        body: str = "",
        author: Optional[str] = None,
        embeds_payload: Optional[List[Tuple[str, Optional[str], bytes]]] = None,
        attachments_payload: Optional[List[Tuple[str, Optional[str], bytes]]] = None,
        linked_attachment_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        embeds_payload = embeds_payload or []
        attachments_payload = attachments_payload or []
        linked_attachment_ids = linked_attachment_ids or []
        if not (body and body.strip()) and not embeds_payload and not attachments_payload and not linked_attachment_ids:
            raise HTTPException(status_code=400, detail="Comment must contain body text, embeds, or attachments.")

        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            linked_clean = self._validate_linkable_issue_attachments_unlocked(iss, linked_attachment_ids)

            cfg = self._read_json(self.config_path)
            c_seq = self._next_seq(cfg, "next_comment_seq")
            cid = f"CMT-{c_seq}"
            eff_author = str(author or self.default_user).strip() or self.default_user
            c_dir = idir / "comments" / cid
            c_files_dir = c_dir / "files"
            c_dir.mkdir(parents=True, exist_ok=True)

            text = body or ""
            embeds_meta: List[Dict[str, Any]] = []
            for fname, ctype, raw_bytes in embeds_payload:
                meta = self._store_file_bytes_unlocked(
                    c_files_dir, cfg, fname, ctype, raw_bytes, "comment_embed", eff_author, comment_id=cid
                )
                text = self._apply_embed_to_markdown(text, meta["filename"], meta["id"])
                embeds_meta.append(meta)

            atts_meta: List[Dict[str, Any]] = []
            for fname, ctype, raw_bytes in attachments_payload:
                meta = self._store_file_bytes_unlocked(
                    c_files_dir, cfg, fname, ctype, raw_bytes, "comment", eff_author, comment_id=cid
                )
                atts_meta.append(meta)

            ts = now_iso()
            cmt: Dict[str, Any] = {
                "id": cid,
                "seq": c_seq,
                "issue_key": iss["key"],
                "author": eff_author,
                "body": text,
                "embeds": embeds_meta,
                "attachments": atts_meta,
                "linked_attachment_ids": linked_clean,
                "edit_history": [],
                "created_at": ts,
                "updated_at": ts,
            }
            self._atomic_write_json(c_dir / "comment.json", cmt)

            iss["updated_at"] = ts
            self._append_history(
                idir,
                cfg,
                actor=eff_author,
                action="comment_added",
                diffs=[{"field": "comments", "old": None, "new": cid}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return self._enrich_comment(cmt, iss)

    def update_comment(
        self,
        issue_ref: str,
        comment_id: str,
        body: Optional[str] = None,
        actor: Optional[str] = None,
        add_embeds: Optional[List[Tuple[str, Optional[str], bytes]]] = None,
        remove_embeds: Optional[List[str]] = None,
        add_attachments: Optional[List[Tuple[str, Optional[str], bytes]]] = None,
        remove_attachments: Optional[List[str]] = None,
        link_attachments: Optional[List[str]] = None,
        unlink_attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            cid = comment_id.strip().upper()
            c_dir = idir / "comments" / cid
            c_path = c_dir / "comment.json"
            if not c_path.exists():
                raise HTTPException(status_code=404, detail=f"Comment '{comment_id}' not found on {iss['key']}.")

            cmt = self._read_json(c_path)
            cfg = self._read_json(self.config_path)
            eff_actor = str(actor or self.default_user).strip() or self.default_user
            ts = now_iso()

            old_body = cmt.get("body") or ""
            cur_body = body if body is not None else old_body

            # 1. Remove specified embeds
            for rem_id in (remove_embeds or []):
                up = str(rem_id).strip().upper()
                emb = next((e for e in (cmt.get("embeds") or []) if e["id"].upper() == up), None)
                if not emb:
                    raise HTTPException(status_code=404, detail=f"Embed '{rem_id}' not found on comment '{cid}'.")
                (c_dir / "files" / emb["stored_name"]).unlink(missing_ok=True)
                cmt["embeds"] = [e for e in (cmt.get("embeds") or []) if e["id"].upper() != up]
                cur_body = self._strip_embed_from_markdown(cur_body, emb["id"])

            # 2. Remove specified comment attachments
            for rem_att_id in (remove_attachments or []):
                up = str(rem_att_id).strip().upper()
                ca = next((a for a in (cmt.get("attachments") or []) if a["id"].upper() == up), None)
                if not ca:
                    raise HTTPException(status_code=404, detail=f"Attachment '{rem_att_id}' not found on comment '{cid}'.")
                (c_dir / "files" / ca["stored_name"]).unlink(missing_ok=True)
                cmt["attachments"] = [a for a in (cmt.get("attachments") or []) if a["id"].upper() != up]

            # 3. Add new embeds
            c_files_dir = c_dir / "files"
            for fname, ctype, raw_bytes in (add_embeds or []):
                meta = self._store_file_bytes_unlocked(
                    c_files_dir, cfg, fname, ctype, raw_bytes, "comment_embed", eff_actor, comment_id=cid
                )
                cur_body = self._apply_embed_to_markdown(cur_body, meta["filename"], meta["id"])
                cmt.setdefault("embeds", []).append(meta)

            # 4. Add new comment attachments
            for fname, ctype, raw_bytes in (add_attachments or []):
                meta = self._store_file_bytes_unlocked(
                    c_files_dir, cfg, fname, ctype, raw_bytes, "comment", eff_actor, comment_id=cid
                )
                cmt.setdefault("attachments", []).append(meta)

            # 5. Link / Unlink issue attachments
            linked_ids = list(cmt.get("linked_attachment_ids") or [])
            if link_attachments:
                validated_new = self._validate_linkable_issue_attachments_unlocked(iss, link_attachments)
                for vid in validated_new:
                    if vid not in linked_ids:
                        linked_ids.append(vid)
            if unlink_attachments:
                un_set = {str(x).strip().upper() for x in unlink_attachments if str(x).strip()}
                linked_ids = [lid for lid in linked_ids if lid.upper() not in un_set]
            cmt["linked_attachment_ids"] = linked_ids

            if cur_body != old_body:
                cmt.setdefault("edit_history", []).append({
                    "edited_at": ts,
                    "actor": eff_actor,
                    "previous_body": old_body,
                })
            cmt["body"] = cur_body
            cmt["updated_at"] = ts
            self._atomic_write_json(c_path, cmt)

            iss["updated_at"] = ts
            self._append_history(
                idir,
                cfg,
                actor=eff_actor,
                action="comment_updated",
                diffs=[{"field": "comment", "old": cid, "new": cid}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return self._enrich_comment(cmt, iss)

    def delete_comment(self, issue_ref: str, comment_id: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            cid = comment_id.strip().upper()
            c_dir = idir / "comments" / cid
            if not (c_dir / "comment.json").exists():
                raise HTTPException(status_code=404, detail=f"Comment '{comment_id}' not found on {iss['key']}.")

            shutil.rmtree(c_dir, ignore_errors=True)
            ts = now_iso()
            iss["updated_at"] = ts
            cfg = self._read_json(self.config_path)
            self._append_history(
                idir,
                cfg,
                actor=str(actor or self.default_user).strip() or self.default_user,
                action="comment_deleted",
                diffs=[{"field": "comments", "old": cid, "new": None}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return {"deleted": True, "id": cid, "issue_key": iss["key"]}

    # ------------------------------------------------------------------------
    # Bidirectional Links, Worklogs & Bulk Operations
    # ------------------------------------------------------------------------

    def list_links(self, issue_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            iss, _ = self._resolve_issue_unlocked(issue_ref)
            return {"key": iss["key"], "links": list(iss.get("links") or [])}

    def _has_blocking_path_unlocked(self, start_key: str, goal_key: str) -> bool:
        assert self._db is not None
        rows = self._db.execute("SELECT blocker_key, blocked_key FROM issue_links").fetchall()
        adj: Dict[str, Set[str]] = {}
        for r in rows:
            adj.setdefault(r["blocker_key"], set()).add(r["blocked_key"])
        q: deque[str] = deque([start_key])
        seen: Set[str] = {start_key}
        while q:
            cur = q.popleft()
            if cur == goal_key:
                return True
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        return False

    def add_link(self, issue_ref: str, link_type: str, target_ref: str, actor: Optional[str] = None) -> Dict[str, Any]:
        try:
            norm_type = normalize_link_type(link_type)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        with self.write_lock():
            src, src_dir = self._resolve_issue_unlocked(issue_ref)
            tgt, tgt_dir = self._resolve_issue_unlocked(target_ref)
            src_key = src["key"]
            tgt_key = tgt["key"]

            if src_key == tgt_key:
                raise HTTPException(status_code=409, detail=f"Cannot link issue '{src_key}' to itself.")

            # Duplicate link check
            for ex in src.get("links") or []:
                if ex.get("target_key") == tgt_key and ex.get("type") == norm_type:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Link '{src_key} {norm_type} {tgt_key}' already exists ({ex['id']}).",
                    )

            # Cycle check for blocks / blocked-by
            if norm_type in ("blocks", "blocked-by"):
                blocker = src_key if norm_type == "blocks" else tgt_key
                blocked = tgt_key if norm_type == "blocks" else src_key
                if self._has_blocking_path_unlocked(blocked, blocker):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Adding '{src_key} {norm_type} {tgt_key}' creates a circular blocking dependency.",
                    )

            # Cycle check & parent sync for parent-of
            if norm_type == "parent-of":
                self._check_parent_cycle_unlocked(tgt_key, src_key)

            cfg = self._read_json(self.config_path)
            l_seq = self._next_seq(cfg, "next_link_seq")
            lid = f"LNK-{l_seq}"
            ts = now_iso()
            eff_actor = str(actor or self.default_user).strip() or self.default_user
            inv_type = INVERSE_LINK_TYPES[norm_type]

            src_entry = {
                "id": lid,
                "type": norm_type,
                "direction": "outbound",
                "target_key": tgt_key,
                "target_name": tgt["name"],
                "created_at": ts,
            }
            tgt_entry = {
                "id": lid,
                "type": inv_type,
                "direction": "inbound",
                "target_key": src_key,
                "target_name": src["name"],
                "created_at": ts,
            }

            src.setdefault("links", []).append(src_entry)
            tgt.setdefault("links", []).append(tgt_entry)
            if norm_type == "parent-of":
                tgt["parent"] = src_key

            src["updated_at"] = ts
            tgt["updated_at"] = ts
            self._append_history(
                src_dir, cfg, actor=eff_actor, action="link_added",
                diffs=[{"field": "links", "old": None, "new": f"{norm_type} {tgt_key}"}], timestamp=ts,
            )
            self._append_history(
                tgt_dir, cfg, actor=eff_actor, action="link_added",
                diffs=[{"field": "links", "old": None, "new": f"{inv_type} {src_key}"}], timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(tgt_dir / "issue.json", tgt)
            self._save_issue_unlocked(src, src_dir)
            self._save_issue_unlocked(tgt, tgt_dir)
            return src_entry

    def remove_link(self, issue_ref: str, link_id: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            src, src_dir = self._resolve_issue_unlocked(issue_ref)
            lid = link_id.strip().upper()
            found = next((l for l in (src.get("links") or []) if l["id"].upper() == lid), None)
            if not found:
                raise HTTPException(status_code=404, detail=f"Link '{link_id}' not found on {src['key']}.")

            real_lid = found["id"]
            tgt_key = found["target_key"]
            ts = now_iso()
            eff_actor = str(actor or self.default_user).strip() or self.default_user

            src["links"] = [l for l in (src.get("links") or []) if l["id"] != real_lid]
            if found.get("type") == "child-of" and src.get("parent") == tgt_key:
                src["parent"] = None
            src["updated_at"] = ts

            cfg = self._read_json(self.config_path)
            self._append_history(
                src_dir, cfg, actor=eff_actor, action="link_removed",
                diffs=[{"field": "links", "old": real_lid, "new": None}], timestamp=ts,
            )

            with contextlib.suppress(HTTPException):
                tgt, tgt_dir = self._resolve_issue_unlocked(tgt_key)
                tgt["links"] = [l for l in (tgt.get("links") or []) if l["id"] != real_lid]
                if found.get("type") == "parent-of" and tgt.get("parent") == src["key"]:
                    tgt["parent"] = None
                tgt["updated_at"] = ts
                self._append_history(
                    tgt_dir, cfg, actor=eff_actor, action="link_removed",
                    diffs=[{"field": "links", "old": real_lid, "new": None}], timestamp=ts,
                )
                self._save_issue_unlocked(tgt, tgt_dir)

            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(src, src_dir)
            return {"deleted": True, "id": real_lid}

    # --- Worklogs ---

    def list_worklogs(self, issue_ref: str) -> Dict[str, Any]:
        iss = self.get_issue(issue_ref)
        return {
            "key": iss["key"],
            "worklogs": iss.get("worklogs") or [],
            "time_progress": iss["time_progress"],
        }

    def add_worklog(
        self,
        issue_ref: str,
        minutes: Optional[Any] = None,
        duration: Optional[Any] = None,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            mins = parse_duration_minutes(minutes, duration)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            cfg = self._read_json(self.config_path)
            w_seq = self._next_seq(cfg, "next_worklog_seq")
            wid = f"WL-{w_seq}"
            eff_author = str(author or self.default_user).strip() or self.default_user
            ts = now_iso()
            entry = {
                "id": wid,
                "author": eff_author,
                "minutes": mins,
                "note": str(note or "").strip(),
                "created_at": ts,
            }
            iss.setdefault("worklogs", []).append(entry)
            old_logged = int(iss.get("logged_minutes") or 0)
            iss["logged_minutes"] = sum(int(w.get("minutes", 0)) for w in iss["worklogs"])
            iss["updated_at"] = ts
            self._append_history(
                idir, cfg, actor=eff_author, action="worklog_added",
                diffs=[{"field": "logged_minutes", "old": old_logged, "new": iss["logged_minutes"]}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            enriched = self._enrich_issues_unlocked([(iss, idir)])[0]
            return {"worklog": entry, "time_progress": enriched["time_progress"]}

    def delete_worklog(self, issue_ref: str, worklog_id: str, actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss, idir = self._resolve_issue_unlocked(issue_ref)
            wid = worklog_id.strip().upper()
            wls = list(iss.get("worklogs") or [])
            found = next((w for w in wls if w["id"].upper() == wid), None)
            if not found:
                raise HTTPException(status_code=404, detail=f"Worklog '{worklog_id}' not found on {iss['key']}.")

            iss["worklogs"] = [w for w in wls if w["id"] != found["id"]]
            old_logged = int(iss.get("logged_minutes") or 0)
            iss["logged_minutes"] = sum(int(w.get("minutes", 0)) for w in iss["worklogs"])
            ts = now_iso()
            iss["updated_at"] = ts
            cfg = self._read_json(self.config_path)
            self._append_history(
                idir, cfg, actor=str(actor or self.default_user).strip() or self.default_user,
                action="worklog_deleted",
                diffs=[{"field": "logged_minutes", "old": old_logged, "new": iss["logged_minutes"]}],
                timestamp=ts,
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue_unlocked(iss, idir)
            return {"deleted": True, "id": found["id"], "logged_minutes": iss["logged_minutes"]}

    # --- Bulk Update & Bulk Delete ---

    def _resolve_bulk_targets(self, issues: Optional[List[str]], query: Optional[str]) -> List[str]:
        if issues:
            keys: List[str] = []
            with self.read_lock():
                for ref in issues:
                    s = str(ref).strip()
                    if not s:
                        continue
                    iss, _ = self._resolve_issue_unlocked(s)
                    if iss["key"] not in keys:
                        keys.append(iss["key"])
            if not keys:
                raise HTTPException(status_code=400, detail="No target issues specified.")
            return keys
        if query and str(query).strip():
            res = self.query_issues(iql=str(query).strip(), page=1, page_size=1000)
            return [i["key"] for i in res["issues"]]
        raise HTTPException(status_code=400, detail="Provide either 'issues' list or 'query' iQL expression.")

    def bulk_update(
        self,
        issues: Optional[List[str]] = None,
        query: Optional[str] = None,
        updates: Optional[Dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not updates or not isinstance(updates, dict):
            raise HTTPException(status_code=400, detail="'updates' dictionary is required for bulk update.")
        target_keys = self._resolve_bulk_targets(issues, query)
        updated_keys: List[str] = []
        with self.write_lock():
            for k in target_keys:
                if updates.get("archive") or updates.get("archived") is True:
                    self.archive_issue(k, actor=actor)
                elif updates.get("unarchive") or updates.get("archived") is False:
                    self.unarchive_issue(k, actor=actor)
                patch = {pk: pv for pk, pv in updates.items() if pk not in ("archive", "unarchive", "archived")}
                if patch:
                    patch["actor"] = actor
                    self.update_issue(k, patch)
                updated_keys.append(k)
        return {"updated_count": len(updated_keys), "updated_keys": updated_keys}

    def bulk_delete(
        self,
        issues: Optional[List[str]] = None,
        query: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_keys = self._resolve_bulk_targets(issues, query)
        deleted_keys: List[str] = []
        with self.write_lock():
            for k in target_keys:
                self.delete_issue(k, actor=actor)
                deleted_keys.append(k)
        return {"deleted_count": len(deleted_keys), "deleted_keys": deleted_keys}

    # ------------------------------------------------------------------------
    # Tags, Sprints, Milestones & Saved Views
    # ------------------------------------------------------------------------

    def list_tags(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            assert self._db is not None
            tags = list(self._read_json(self.tags_path, {"tags": []}).get("tags", []))
            counts = {
                r["tag_low"]: int(r["c"])
                for r in self._db.execute("SELECT LOWER(tag) AS tag_low, COUNT(*) AS c FROM issue_tags GROUP BY LOWER(tag)").fetchall()
            }
            out: List[Dict[str, Any]] = []
            for t in tags:
                item = dict(t)
                item["issue_count"] = counts.get(t["name"].lower(), 0)
                out.append(item)
            return out

    def create_tag(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Tag name is required.")
        color = str(data.get("color") or "#3b82f6").strip()
        if not re.fullmatch(r"#[0-9A-Fa-f]{3,8}", color):
            raise HTTPException(status_code=400, detail=f"Invalid hex color '{color}'.")
        with self.write_lock():
            doc = self._read_json(self.tags_path, {"tags": []})
            tags = list(doc.get("tags", []))
            if any(t["name"].lower() == name.lower() for t in tags):
                raise HTTPException(status_code=409, detail=f"Tag '{name}' already exists.")
            ts = now_iso()
            entry = {
                "name": name,
                "color": color,
                "description": str(data.get("description") or "").strip(),
                "created_at": ts,
                "updated_at": ts,
            }
            tags.append(entry)
            self._atomic_write_json(self.tags_path, {"tags": tags})
            self._update_disk_sig_in_db()
            return entry

    def update_tag(self, tag_name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        clean = urllib.parse.unquote(tag_name).strip()
        with self.write_lock():
            doc = self._read_json(self.tags_path, {"tags": []})
            tags = list(doc.get("tags", []))
            found = next((t for t in tags if t["name"].lower() == clean.lower()), None)
            if not found:
                raise HTTPException(status_code=404, detail=f"Tag '{clean}' not found.")

            old_name = found["name"]
            new_name = str(patch["new_name"]).strip() if ("new_name" in patch and patch["new_name"] is not None) else old_name
            if not new_name:
                raise HTTPException(status_code=400, detail="Tag new_name cannot be empty.")
            if new_name.lower() != old_name.lower() and any(t["name"].lower() == new_name.lower() for t in tags):
                raise HTTPException(status_code=409, detail=f"Tag '{new_name}' already exists.")
            if "color" in patch and patch["color"] is not None:
                c = str(patch["color"]).strip()
                if not re.fullmatch(r"#[0-9A-Fa-f]{3,8}", c):
                    raise HTTPException(status_code=400, detail=f"Invalid hex color '{c}'.")
                found["color"] = c
            if "description" in patch and patch["description"] is not None:
                found["description"] = str(patch["description"]).strip()

            ts = now_iso()
            found["name"] = new_name
            found["updated_at"] = ts
            self._atomic_write_json(self.tags_path, {"tags": tags})

            if new_name != old_name:
                for ent in os.scandir(self.issues_dir):
                    if not ent.is_dir(follow_symlinks=False) or ent.name.startswith("."):
                        continue
                    ij = Path(ent.path) / "issue.json"
                    if ij.exists():
                        iss = self._read_json(ij)
                        itags = iss.get("tags") or []
                        if any(t.lower() == old_name.lower() for t in itags):
                            iss["tags"] = self._dedupe_strs([new_name if t.lower() == old_name.lower() else t for t in itags])
                            iss["updated_at"] = ts
                            self._atomic_write_json(ij, iss)
                self._rebuild_index_unlocked()
            else:
                self._update_disk_sig_in_db()
            return found

    def delete_tag(self, tag_name: str) -> Dict[str, Any]:
        clean = urllib.parse.unquote(tag_name).strip()
        with self.write_lock():
            doc = self._read_json(self.tags_path, {"tags": []})
            tags = list(doc.get("tags", []))
            new_tags = [t for t in tags if t["name"].lower() != clean.lower()]
            if len(new_tags) == len(tags):
                raise HTTPException(status_code=404, detail=f"Tag '{clean}' not found.")
            self._atomic_write_json(self.tags_path, {"tags": new_tags})

            ts = now_iso()
            for ent in os.scandir(self.issues_dir):
                if not ent.is_dir(follow_symlinks=False) or ent.name.startswith("."):
                    continue
                ij = Path(ent.path) / "issue.json"
                if ij.exists():
                    iss = self._read_json(ij)
                    itags = iss.get("tags") or []
                    filtered = [t for t in itags if t.lower() != clean.lower()]
                    if len(filtered) != len(itags):
                        iss["tags"] = filtered
                        iss["updated_at"] = ts
                        self._atomic_write_json(ij, iss)
            self._rebuild_index_unlocked()
            return {"deleted": True, "name": clean}

    # --- Sprints ---

    def _enrich_sprint_unlocked(self, sp: Dict[str, Any]) -> Dict[str, Any]:
        assert self._db is not None
        out = dict(sp)
        sid = sp["id"]
        rows = self._db.execute(
            "SELECT state, story_points FROM issues WHERE sprint = ?", (sid,)
        ).fetchall()
        total = len(rows)
        done = sum(1 for r in rows if r["state"] == "done")
        pts_tot = round(sum(float(r["story_points"] or 0.0) for r in rows), 2)
        pts_done = round(sum(float(r["story_points"] or 0.0) for r in rows if r["state"] == "done"), 2)
        out["total_issues"] = total
        out["done_issues"] = done
        out["incomplete_issues"] = sum(1 for r in rows if r["state"] not in TERMINAL_STATES)
        out["total_points"] = pts_tot
        out["completed_points"] = pts_done
        out["progress_pct"] = round(done / total * 100.0, 1) if total else 0.0
        return out

    def list_sprints(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.read_lock():
            sprints = list(self._read_json(self.sprints_path, {"sprints": []}).get("sprints", []))
            if state and state.strip().lower() != "all":
                st = state.strip().lower()
                if st not in ("planned", "active", "completed"):
                    raise HTTPException(status_code=400, detail="Invalid sprint state filter.")
                sprints = [s for s in sprints if s.get("state") == st]
            return [self._enrich_sprint_unlocked(s) for s in sprints]

    def create_sprint(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Sprint name is required.")
        try:
            start_d = validate_calendar_date(data.get("start_date"), "start_date")
            end_d = validate_calendar_date(data.get("end_date"), "end_date")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if start_d and end_d and end_d < start_d:
            raise HTTPException(status_code=400, detail="Sprint end_date cannot be earlier than start_date.")

        with self.write_lock():
            sprints = list(self._read_json(self.sprints_path, {"sprints": []}).get("sprints", []))
            if any(s["name"].lower() == name.lower() for s in sprints):
                raise HTTPException(status_code=409, detail=f"Sprint '{name}' already exists.")
            cfg = self._read_json(self.config_path)
            seq = self._next_seq(cfg, "next_sprint_seq")
            sid = f"SPR-{seq}"
            ts = now_iso()
            sp = {
                "id": sid,
                "seq": seq,
                "name": name,
                "goal": str(data.get("goal") or "").strip(),
                "state": "planned",
                "start_date": start_d,
                "end_date": end_d,
                "started_at": None,
                "completed_at": None,
                "created_at": ts,
                "updated_at": ts,
            }
            sprints.append(sp)
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(self.sprints_path, {"sprints": sprints})
            self._update_disk_sig_in_db()
            return self._enrich_sprint_unlocked(sp)

    def get_sprint(self, sprint_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            assert self._db is not None
            _, _, sp = self._find_sprint_unlocked(sprint_ref)
            enriched_sp = self._enrich_sprint_unlocked(sp)
            folders = [
                r["folder"]
                for r in self._db.execute("SELECT folder FROM issues WHERE sprint = ? ORDER BY priority_rank DESC, seq ASC", (sp["id"],)).fetchall()
            ]
            pairs = [(self._read_json(self.issues_dir / f / "issue.json"), self.issues_dir / f) for f in folders if (self.issues_dir / f / "issue.json").exists()]
            enriched_sp["issues"] = self._enrich_issues_unlocked(pairs)
            return enriched_sp

    def update_sprint(self, sprint_ref: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            sprints, idx, sp = self._find_sprint_unlocked(sprint_ref)
            if "name" in patch and patch["name"] is not None:
                nn = str(patch["name"]).strip()
                if not nn:
                    raise HTTPException(status_code=400, detail="Sprint name cannot be empty.")
                sp["name"] = nn
            if "goal" in patch and patch["goal"] is not None:
                sp["goal"] = str(patch["goal"]).strip()
            try:
                if "start_date" in patch:
                    sp["start_date"] = validate_calendar_date(patch["start_date"], "start_date")
                if "end_date" in patch:
                    sp["end_date"] = validate_calendar_date(patch["end_date"], "end_date")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if sp.get("start_date") and sp.get("end_date") and sp["end_date"] < sp["start_date"]:
                raise HTTPException(status_code=400, detail="Sprint end_date cannot precede start_date.")
            sp["updated_at"] = now_iso()
            sprints[idx] = sp
            self._atomic_write_json(self.sprints_path, {"sprints": sprints})
            self._update_disk_sig_in_db()
            return self._enrich_sprint_unlocked(sp)

    def start_sprint(self, sprint_ref: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            sprints, idx, sp = self._find_sprint_unlocked(sprint_ref)
            if sp.get("state") == "completed":
                raise HTTPException(status_code=400, detail=f"Sprint '{sp['id']}' is already completed.")
            try:
                sd = validate_calendar_date(start_date, "start_date") if start_date else (sp.get("start_date") or today_iso())
                ed = validate_calendar_date(end_date, "end_date") if end_date else sp.get("end_date")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            ts = now_iso()
            sp["state"] = "active"
            sp["start_date"] = sd
            if ed:
                sp["end_date"] = ed
            sp["started_at"] = sp.get("started_at") or ts
            sp["updated_at"] = ts
            sprints[idx] = sp
            self._atomic_write_json(self.sprints_path, {"sprints": sprints})
            self._update_disk_sig_in_db()
            return self._enrich_sprint_unlocked(sp)

    def complete_sprint(self, sprint_ref: str, rollover_to: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            assert self._db is not None
            sprints, idx, sp = self._find_sprint_unlocked(sprint_ref)
            sid = sp["id"]
            target_sid: Optional[str] = None
            if rollover_to and str(rollover_to).strip().lower() not in ("", "none", "null", "backlog"):
                _, _, target_sp = self._find_sprint_unlocked(str(rollover_to))
                if target_sp["id"] == sid:
                    raise HTTPException(status_code=400, detail="Cannot roll over incomplete issues to the same sprint.")
                target_sid = target_sp["id"]

            ts = now_iso()
            sp["state"] = "completed"
            sp["completed_at"] = ts
            sp["updated_at"] = ts
            sprints[idx] = sp
            self._atomic_write_json(self.sprints_path, {"sprints": sprints})

            inc_rows = self._db.execute(
                "SELECT key FROM issues WHERE sprint = ? AND state NOT IN ('done', 'cancelled', 'archived') ORDER BY seq ASC",
                (sid,),
            ).fetchall()
            rolled_keys: List[str] = []
            for r in inc_rows:
                self.update_issue(r["key"], {"sprint": target_sid, "clear_sprint": target_sid is None})
                rolled_keys.append(r["key"])

            self._update_disk_sig_in_db()
            return {
                "sprint": self._enrich_sprint_unlocked(sp),
                "rolled_over_keys": rolled_keys,
                "rollover_to": target_sid,
            }

    def assign_sprint_issues(self, sprint_ref: str, issue_refs: List[str]) -> Dict[str, Any]:
        if not issue_refs:
            raise HTTPException(status_code=400, detail="Provide at least one issue to assign to sprint.")
        with self.write_lock():
            _, _, sp = self._find_sprint_unlocked(sprint_ref)
            assigned: List[str] = []
            for iref in issue_refs:
                updated = self.update_issue(iref, {"sprint": sp["id"]})
                assigned.append(updated["key"])
            return {"sprint": self._enrich_sprint_unlocked(sp), "assigned_keys": assigned}

    def remove_sprint_issues(self, sprint_ref: str, issue_refs: List[str]) -> Dict[str, Any]:
        if not issue_refs:
            raise HTTPException(status_code=400, detail="Provide at least one issue to remove from sprint.")
        with self.write_lock():
            _, _, sp = self._find_sprint_unlocked(sprint_ref)
            removed: List[str] = []
            for iref in issue_refs:
                iss, _ = self._resolve_issue_unlocked(iref)
                if iss.get("sprint") == sp["id"]:
                    self.update_issue(iss["key"], {"clear_sprint": True})
                    removed.append(iss["key"])
            return {"sprint": self._enrich_sprint_unlocked(sp), "removed_keys": removed}

    def delete_sprint(self, sprint_ref: str) -> Dict[str, Any]:
        with self.write_lock():
            assert self._db is not None
            sprints, idx, sp = self._find_sprint_unlocked(sprint_ref)
            sid = sp["id"]
            sprints.pop(idx)
            self._atomic_write_json(self.sprints_path, {"sprints": sprints})
            rows = self._db.execute("SELECT key FROM issues WHERE sprint = ?", (sid,)).fetchall()
            for r in rows:
                self.update_issue(r["key"], {"clear_sprint": True})
            self._update_disk_sig_in_db()
            return {"deleted": True, "id": sid}

    # --- Milestones ---

    def _enrich_milestone_unlocked(self, ms: Dict[str, Any]) -> Dict[str, Any]:
        assert self._db is not None
        out = dict(ms)
        mid = ms["id"]
        rows = self._db.execute("SELECT state FROM issues WHERE milestone = ?", (mid,)).fetchall()
        total = len(rows)
        done = sum(1 for r in rows if r["state"] == "done")
        out["total_issues"] = total
        out["done_issues"] = done
        out["progress_pct"] = round(done / total * 100.0, 1) if total else 0.0
        return out

    def list_milestones(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.read_lock():
            items = list(self._read_json(self.milestones_path, {"milestones": []}).get("milestones", []))
            if state and state.strip().lower() != "all":
                st = state.strip().lower()
                if st not in ("open", "closed"):
                    raise HTTPException(status_code=400, detail="Invalid milestone state. Expected open, closed, or all.")
                items = [m for m in items if m.get("state", "open") == st]
            return [self._enrich_milestone_unlocked(m) for m in items]

    def create_milestone(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Milestone name is required.")
        try:
            due = validate_calendar_date(data.get("due_date"), "due_date")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        with self.write_lock():
            items = list(self._read_json(self.milestones_path, {"milestones": []}).get("milestones", []))
            if any(m["name"].lower() == name.lower() for m in items):
                raise HTTPException(status_code=409, detail=f"Milestone '{name}' already exists.")
            cfg = self._read_json(self.config_path)
            seq = self._next_seq(cfg, "next_milestone_seq")
            mid = f"MIL-{seq}"
            ts = now_iso()
            ms = {
                "id": mid,
                "seq": seq,
                "name": name,
                "description": str(data.get("description") or "").strip(),
                "due_date": due,
                "state": "open",
                "closed_at": None,
                "created_at": ts,
                "updated_at": ts,
            }
            items.append(ms)
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(self.milestones_path, {"milestones": items})
            self._update_disk_sig_in_db()
            return self._enrich_milestone_unlocked(ms)

    def get_milestone(self, milestone_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            assert self._db is not None
            _, _, ms = self._find_milestone_unlocked(milestone_ref)
            out = self._enrich_milestone_unlocked(ms)
            folders = [
                r["folder"]
                for r in self._db.execute("SELECT folder FROM issues WHERE milestone = ? ORDER BY priority_rank DESC, seq ASC", (ms["id"],)).fetchall()
            ]
            pairs = [(self._read_json(self.issues_dir / f / "issue.json"), self.issues_dir / f) for f in folders if (self.issues_dir / f / "issue.json").exists()]
            out["issues"] = self._enrich_issues_unlocked(pairs)
            return out

    def update_milestone(self, milestone_ref: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            items, idx, ms = self._find_milestone_unlocked(milestone_ref)
            if "name" in patch and patch["name"] is not None:
                nn = str(patch["name"]).strip()
                if not nn:
                    raise HTTPException(status_code=400, detail="Milestone name cannot be empty.")
                ms["name"] = nn
            if "description" in patch and patch["description"] is not None:
                ms["description"] = str(patch["description"]).strip()
            if "due_date" in patch:
                try:
                    ms["due_date"] = validate_calendar_date(patch["due_date"], "due_date")
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
            ms["updated_at"] = now_iso()
            items[idx] = ms
            self._atomic_write_json(self.milestones_path, {"milestones": items})
            self._update_disk_sig_in_db()
            return self._enrich_milestone_unlocked(ms)

    def set_milestone_state(self, milestone_ref: str, closed: bool) -> Dict[str, Any]:
        with self.write_lock():
            items, idx, ms = self._find_milestone_unlocked(milestone_ref)
            ts = now_iso()
            ms["state"] = "closed" if closed else "open"
            ms["closed_at"] = ts if closed else None
            ms["updated_at"] = ts
            items[idx] = ms
            self._atomic_write_json(self.milestones_path, {"milestones": items})
            self._update_disk_sig_in_db()
            return self._enrich_milestone_unlocked(ms)

    def delete_milestone(self, milestone_ref: str) -> Dict[str, Any]:
        with self.write_lock():
            assert self._db is not None
            items, idx, ms = self._find_milestone_unlocked(milestone_ref)
            mid = ms["id"]
            items.pop(idx)
            self._atomic_write_json(self.milestones_path, {"milestones": items})
            rows = self._db.execute("SELECT key FROM issues WHERE milestone = ?", (mid,)).fetchall()
            for r in rows:
                self.update_issue(r["key"], {"clear_milestone": True})
            self._update_disk_sig_in_db()
            return {"deleted": True, "id": mid}

    # --- Saved Views ---

    def list_views(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            return list(self._read_json(self.views_path, {"views": []}).get("views", []))

    def _validate_view_fields_unlocked(self, query_str: str, tab: Optional[str], sort: Optional[str], order: Optional[str], swimlane: Optional[str]) -> Tuple[str, str, Optional[str], str, str]:
        try:
            eff_tab = normalize_tab(tab) if tab else "all"
            eff_sw = normalize_swimlane(swimlane) if swimlane else "none"
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        eff_order = (order or "asc").strip().lower()
        if eff_order not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'.")
        compiler = self._make_compiler_unlocked()
        if query_str.strip():
            try:
                ast = parse_iql(query_str)
                compiler.compile_where(ast["where"])
                compiler.compile_order_by(ast["order_by"], sort_field=sort, sort_order=eff_order)
            except IQLError as e:
                raise HTTPException(status_code=400, detail=f"Invalid iQL in saved view: {e.message}")
        elif sort:
            try:
                compiler.compile_order_by([], sort_field=sort, sort_order=eff_order)
            except IQLError as e:
                raise HTTPException(status_code=400, detail=e.message)
        return query_str.strip(), eff_tab, (sort.strip() if sort else None), eff_order, eff_sw

    def create_view(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Saved view name is required.")
        raw_q = str(data.get("query") if "query" in data else (data.get("iql") or ""))
        with self.write_lock():
            views = list(self._read_json(self.views_path, {"views": []}).get("views", []))
            if any(v["name"].lower() == name.lower() for v in views):
                raise HTTPException(status_code=409, detail=f"Saved view '{name}' already exists.")
            q_clean, tab_c, sort_c, ord_c, sw_c = self._validate_view_fields_unlocked(
                raw_q, data.get("tab"), data.get("sort"), data.get("order"), data.get("swimlane")
            )
            cfg = self._read_json(self.config_path)
            seq = self._next_seq(cfg, "next_view_seq")
            vid = f"VIEW-{seq}"
            ts = now_iso()
            vw = {
                "id": vid,
                "seq": seq,
                "name": name,
                "query": q_clean,
                "tab": tab_c,
                "sort": sort_c,
                "order": ord_c,
                "swimlane": sw_c,
                "description": str(data.get("description") or "").strip(),
                "created_at": ts,
                "updated_at": ts,
            }
            views.append(vw)
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(self.views_path, {"views": views})
            self._update_disk_sig_in_db()
            return vw

    def get_view(self, view_ref: str) -> Dict[str, Any]:
        with self.read_lock():
            _, _, vw = self._find_view_unlocked(view_ref)
            return dict(vw)

    def run_view(self, view_ref: str, page: int = 1, page_size: int = 25) -> Dict[str, Any]:
        vw = self.get_view(view_ref)
        res = self.query_issues(
            tab=vw.get("tab", "all"),
            iql=vw.get("query") or None,
            sort=vw.get("sort") or None,
            order=vw.get("order") or "asc",
            page=page,
            page_size=page_size,
        )
        res["view"] = vw
        return res

    def update_view(self, view_ref: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            views, idx, vw = self._find_view_unlocked(view_ref)
            if "name" in patch and patch["name"] is not None:
                nn = str(patch["name"]).strip()
                if not nn:
                    raise HTTPException(status_code=400, detail="View name cannot be empty.")
                vw["name"] = nn
            q_raw = str(patch["query"]) if ("query" in patch and patch["query"] is not None) else vw.get("query", "")
            tab_raw = patch["tab"] if ("tab" in patch and patch["tab"] is not None) else vw.get("tab", "all")
            sort_raw = patch["sort"] if "sort" in patch else vw.get("sort")
            ord_raw = patch["order"] if ("order" in patch and patch["order"] is not None) else vw.get("order", "asc")
            sw_raw = patch["swimlane"] if ("swimlane" in patch and patch["swimlane"] is not None) else vw.get("swimlane", "none")
            q_c, tab_c, sort_c, ord_c, sw_c = self._validate_view_fields_unlocked(q_raw, tab_raw, sort_raw, ord_raw, sw_raw)
            vw["query"] = q_c
            vw["tab"] = tab_c
            vw["sort"] = sort_c
            vw["order"] = ord_c
            vw["swimlane"] = sw_c
            if "description" in patch and patch["description"] is not None:
                vw["description"] = str(patch["description"]).strip()
            vw["updated_at"] = now_iso()
            views[idx] = vw
            self._atomic_write_json(self.views_path, {"views": views})
            self._update_disk_sig_in_db()
            return vw

    def delete_view(self, view_ref: str) -> Dict[str, Any]:
        with self.write_lock():
            views, idx, vw = self._find_view_unlocked(view_ref)
            vid = vw["id"]
            views.pop(idx)
            self._atomic_write_json(self.views_path, {"views": views})
            self._update_disk_sig_in_db()
            return {"deleted": True, "id": vid}


# ============================================================================
# Section 5: FastAPI Application Factory & All /api/v1/* Routes
# ============================================================================

def _parse_str_list_value(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        out: List[str] = []
        for item in raw:
            out.extend(_parse_str_list_value(item))
        return out
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        with contextlib.suppress(Exception):
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
    return [part.strip() for part in s.split(",") if part.strip()]


def _extract_form_str_list(form: Any, *keys: str) -> List[str]:
    out: List[str] = []
    for k in keys:
        for val in form.getlist(k):
            if hasattr(val, "read"):
                continue
            out.extend(_parse_str_list_value(val))
    return out


async def _extract_uploaded_files(form: Any, *field_names: str) -> List[Tuple[str, Optional[str], bytes]]:
    files_out: List[Tuple[str, Optional[str], bytes]] = []
    for fn in field_names:
        for item in form.getlist(fn):
            if hasattr(item, "read"):
                raw_bytes = await item.read()
                filename = getattr(item, "filename", None) or "upload.bin"
                ctype = getattr(item, "content_type", None)
                files_out.append((filename, ctype, raw_bytes))
    return files_out


async def _read_optional_json(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object.")
    return parsed


def create_app(
    repo_root: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    server_ref: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    storage = StorageEngine(repo_root)
    started_mono = time.monotonic()
    started_at_iso = now_iso()
    state_holder = server_ref if server_ref is not None else {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        actual_port = int(state_holder.get("port", port))
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        base_url = f"http://{connect_host}:{actual_port}"
        state_holder["base_url"] = base_url
        daemon_meta = {
            "pid": os.getpid(),
            "host": connect_host,
            "port": actual_port,
            "base_url": base_url,
            "repo_root": str(repo_root),
            "started_at": started_at_iso,
            "version": VERSION,
        }
        storage.runtime_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json_standalone(storage.daemon_path, daemon_meta)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                if storage.daemon_path.exists():
                    cur_meta = storage._read_json(storage.daemon_path, {})
                    if cur_meta.get("pid") == os.getpid():
                        storage.daemon_path.unlink(missing_ok=True)
            storage.close()

    app = FastAPI(
        title="iTrack API",
        version=VERSION,
        description="Local-First Folder-Per-Issue Tracker, Self-Healing FTS5 Index & Offline SPA",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.storage = storage

    @app.middleware("http")
    async def surface_telemetry_middleware(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            route_obj = request.scope.get("route")
            route_path = getattr(route_obj, "path", request.url.path)
            record_surface_event({
                "kind": "api_route",
                "method": request.method,
                "route": route_path,
                "status": response.status_code,
            })
        return response

    favicon_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#2563eb"/>'
        '<text x="16" y="22" font-family="monospace" font-size="16" font-weight="bold" text-anchor="middle" fill="#ffffff">iT</text>'
        "</svg>"
    )

    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    def serve_favicon() -> Response:
        return Response(content=favicon_svg, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    def serve_spa() -> HTMLResponse:
        return HTMLResponse(content=SPA_HTML)

    # --- Workspace, System & Config (Routes 1..8) ---

    @app.get("/api/v1/health")
    def api_health() -> Dict[str, Any]:
        actual_port = int(state_holder.get("port", port))
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        initialized = storage.is_initialized()
        return {
            "status": "ok",
            "initialized": initialized,
            "pid": os.getpid(),
            "host": connect_host,
            "port": actual_port,
            "base_url": state_holder.get("base_url", f"http://{connect_host}:{actual_port}"),
            "repo_root": str(repo_root),
            "issues_root": str(storage.issues_root),
            "issue_count": storage.issue_count() if initialized else 0,
            "uptime_seconds": round(time.monotonic() - started_mono, 3),
            "version": VERSION,
        }

    @app.post("/api/v1/shutdown")
    def api_shutdown(background_tasks: BackgroundTasks) -> Dict[str, Any]:
        def _stop() -> None:
            time.sleep(0.05)
            srv = state_holder.get("uvicorn_server")
            if srv is not None:
                srv.should_exit = True

        background_tasks.add_task(_stop)
        return {"status": "shutting_down", "pid": os.getpid()}

    @app.get("/api/v1/status")
    def api_status() -> Dict[str, Any]:
        return storage.get_status()

    @app.post("/api/v1/reindex")
    def api_reindex() -> Dict[str, Any]:
        return storage.reindex()

    @app.get("/api/v1/config")
    def api_get_config() -> Dict[str, Any]:
        return storage.get_config()

    @app.patch("/api/v1/config")
    async def api_patch_config(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.update_config(body)

    @app.get("/api/v1/stats")
    def api_stats() -> Dict[str, Any]:
        return storage.get_stats()

    @app.get("/api/v1/tree")
    def api_tree(root: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        return storage.get_tree(root_ref=root)

    # --- Issues Core, Lifecycle, Rename, Context, History & Embeds (Routes 9..21) ---

    @app.get("/api/v1/issues")
    def api_list_issues(
        tab: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        iql: Optional[str] = Query(default=None),
        category: Optional[str] = Query(default=None),
        state: Optional[str] = Query(default=None),
        priority: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        resolution: Optional[str] = Query(default=None),
        assignee: Optional[str] = Query(default=None),
        reporter: Optional[str] = Query(default=None),
        tag: Optional[str] = Query(default=None),
        component: Optional[str] = Query(default=None),
        parent: Optional[str] = Query(default=None),
        sprint: Optional[str] = Query(default=None),
        milestone: Optional[str] = Query(default=None),
        overdue: Optional[bool] = Query(default=None),
        sort: Optional[str] = Query(default=None),
        order: Optional[str] = Query(default=None),
        page: int = Query(default=1),
        page_size: int = Query(default=25),
    ) -> Dict[str, Any]:
        return storage.query_issues(
            tab=tab,
            q=q,
            iql=iql,
            category=category,
            state=state,
            priority=priority,
            severity=severity,
            resolution=resolution,
            assignee=assignee,
            reporter=reporter,
            tag=tag,
            component=component,
            parent=parent,
            sprint=sprint,
            milestone=milestone,
            overdue=overdue,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )

    @app.post("/api/v1/issues", status_code=201)
    async def api_create_issue(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.create_issue(body)

    @app.get("/api/v1/issues/{issue_ref}")
    def api_get_issue(issue_ref: str) -> Dict[str, Any]:
        return storage.get_issue(issue_ref)

    @app.patch("/api/v1/issues/{issue_ref}")
    async def api_patch_issue(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.update_issue(issue_ref, body)

    @app.post("/api/v1/issues/{issue_ref}/rename")
    async def api_rename_issue(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        new_name = body.get("new_name") if "new_name" in body else body.get("name")
        return storage.rename_issue(issue_ref, new_name=new_name, actor=body.get("actor"))

    @app.post("/api/v1/issues/{issue_ref}/clone", status_code=201)
    async def api_clone_issue(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        new_name = body.get("new_name") if "new_name" in body else body.get("name")
        return storage.clone_issue(issue_ref, new_name=new_name, actor=body.get("actor"))

    @app.post("/api/v1/issues/{issue_ref}/archive")
    async def api_archive_issue(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.archive_issue(issue_ref, actor=body.get("actor"))

    @app.post("/api/v1/issues/{issue_ref}/unarchive")
    async def api_unarchive_issue(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.unarchive_issue(issue_ref, actor=body.get("actor"))

    @app.delete("/api/v1/issues/{issue_ref}")
    def api_delete_issue(issue_ref: str) -> Dict[str, Any]:
        return storage.delete_issue(issue_ref)

    @app.get("/api/v1/issues/{issue_ref}/context")
    def api_issue_context(issue_ref: str) -> Dict[str, Any]:
        return storage.get_issue_context(issue_ref)

    @app.get("/api/v1/issues/{issue_ref}/history")
    def api_issue_history(issue_ref: str) -> Dict[str, Any]:
        return storage.get_issue_history(issue_ref)

    @app.post("/api/v1/issues/{issue_ref}/embeds", status_code=201)
    async def api_add_description_embeds(issue_ref: str, request: Request) -> Dict[str, Any]:
        form = await request.form()
        files_payload = await _extract_uploaded_files(form, "files", "file", "embeds", "embed")
        uploaded_by = str(form.get("uploaded_by") or form.get("actor") or "") or None
        return storage.add_description_embeds(issue_ref, files_payload=files_payload, uploaded_by=uploaded_by)

    @app.delete("/api/v1/issues/{issue_ref}/embeds/{att_id}")
    def api_delete_description_embed(issue_ref: str, att_id: str) -> Dict[str, Any]:
        return storage.delete_description_embed(issue_ref, att_id)

    # --- Whole-Issue & Unified Attachments (Routes 22..26) ---

    @app.post("/api/v1/issues/{issue_ref}/attachments", status_code=201)
    async def api_add_attachments(issue_ref: str, request: Request) -> Dict[str, Any]:
        form = await request.form()
        files_payload = await _extract_uploaded_files(form, "files", "file", "attachments")
        uploaded_by = str(form.get("uploaded_by") or form.get("by") or form.get("actor") or "") or None
        return storage.add_issue_attachments(issue_ref, files_payload=files_payload, uploaded_by=uploaded_by)

    @app.get("/api/v1/issues/{issue_ref}/attachments")
    def api_list_attachments(
        issue_ref: str,
        scope: str = Query(default="all"),
    ) -> Dict[str, Any]:
        return storage.list_attachments(issue_ref, scope=scope)

    @app.get("/api/v1/issues/{issue_ref}/attachments/{att_id}")
    def api_get_attachment_info(issue_ref: str, att_id: str) -> Dict[str, Any]:
        return storage.get_attachment_info(issue_ref, att_id)

    @app.get("/api/v1/issues/{issue_ref}/attachments/{att_id}/download")
    def api_download_attachment(issue_ref: str, att_id: str) -> FileResponse:
        fpath, meta = storage.get_attachment_download(issue_ref, att_id)
        return FileResponse(
            path=str(fpath),
            media_type=meta.get("mime_type") or "application/octet-stream",
            filename=meta.get("filename") or fpath.name,
        )

    @app.delete("/api/v1/issues/{issue_ref}/attachments/{att_id}")
    def api_delete_attachment(issue_ref: str, att_id: str) -> Dict[str, Any]:
        return storage.delete_attachment(issue_ref, att_id)

    # --- Rich Comments, Embeds, Comment Attachments & Cross-Links (Routes 27..31) ---

    @app.get("/api/v1/issues/{issue_ref}/comments")
    def api_list_comments(issue_ref: str) -> Dict[str, Any]:
        return storage.list_comments(issue_ref)

    @app.post("/api/v1/issues/{issue_ref}/comments", status_code=201)
    async def api_add_comment(issue_ref: str, request: Request) -> Dict[str, Any]:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            body_json = await _read_optional_json(request)
            return storage.add_comment(
                issue_ref=issue_ref,
                body=str(body_json.get("body") or ""),
                author=body_json.get("author") or body_json.get("actor"),
                linked_attachment_ids=_parse_str_list_value(
                    body_json.get("linked_attachment_ids") or body_json.get("link_attachments")
                ),
            )
        form = await request.form()
        body_text = str(form.get("body") or "")
        author = str(form.get("author") or form.get("actor") or "") or None
        linked_ids = _extract_form_str_list(form, "linked_attachment_ids", "link_attachments")
        embeds = await _extract_uploaded_files(form, "embeds", "embed")
        attachments = await _extract_uploaded_files(form, "attachments", "attach", "files")
        return storage.add_comment(
            issue_ref=issue_ref,
            body=body_text,
            author=author,
            embeds_payload=embeds,
            attachments_payload=attachments,
            linked_attachment_ids=linked_ids,
        )

    @app.get("/api/v1/issues/{issue_ref}/comments/{comment_id}")
    def api_get_comment(issue_ref: str, comment_id: str) -> Dict[str, Any]:
        return storage.get_comment(issue_ref, comment_id)

    @app.patch("/api/v1/issues/{issue_ref}/comments/{comment_id}")
    async def api_patch_comment(issue_ref: str, comment_id: str, request: Request) -> Dict[str, Any]:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            bj = await _read_optional_json(request)
            return storage.update_comment(
                issue_ref=issue_ref,
                comment_id=comment_id,
                body=str(bj["body"]) if ("body" in bj and bj["body"] is not None) else None,
                actor=bj.get("actor") or bj.get("author"),
                remove_embeds=_parse_str_list_value(bj.get("remove_embeds")),
                remove_attachments=_parse_str_list_value(bj.get("remove_attachments")),
                link_attachments=_parse_str_list_value(bj.get("link_attachments") or bj.get("linked_attachment_ids")),
                unlink_attachments=_parse_str_list_value(bj.get("unlink_attachments")),
            )
        form = await request.form()
        body_opt = str(form["body"]) if "body" in form and not hasattr(form["body"], "read") else None
        actor = str(form.get("actor") or form.get("author") or "") or None
        remove_embeds = _extract_form_str_list(form, "remove_embeds", "remove_embed")
        remove_attachments = _extract_form_str_list(form, "remove_attachments", "remove_attach")
        link_attachments = _extract_form_str_list(form, "link_attachments", "linked_attachment_ids")
        unlink_attachments = _extract_form_str_list(form, "unlink_attachments", "unlink_attachment")
        add_embeds = await _extract_uploaded_files(form, "add_embeds", "embeds", "embed")
        add_attachments = await _extract_uploaded_files(form, "add_attachments", "attachments", "attach")
        return storage.update_comment(
            issue_ref=issue_ref,
            comment_id=comment_id,
            body=body_opt,
            actor=actor,
            add_embeds=add_embeds,
            remove_embeds=remove_embeds,
            add_attachments=add_attachments,
            remove_attachments=remove_attachments,
            link_attachments=link_attachments,
            unlink_attachments=unlink_attachments,
        )

    @app.delete("/api/v1/issues/{issue_ref}/comments/{comment_id}")
    def api_delete_comment(issue_ref: str, comment_id: str) -> Dict[str, Any]:
        return storage.delete_comment(issue_ref, comment_id)

    # --- Links, Worklogs, Bulk, Board & Search (Routes 32..41) ---

    @app.get("/api/v1/issues/{issue_ref}/links")
    def api_list_links(issue_ref: str) -> Dict[str, Any]:
        return storage.list_links(issue_ref)

    @app.post("/api/v1/issues/{issue_ref}/links", status_code=201)
    async def api_add_link(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        ltype = body.get("type") or body.get("link_type") or ""
        target = body.get("target") or body.get("target_key") or ""
        return storage.add_link(issue_ref, link_type=str(ltype), target_ref=str(target), actor=body.get("actor"))

    @app.delete("/api/v1/issues/{issue_ref}/links/{link_id}")
    def api_remove_link(issue_ref: str, link_id: str) -> Dict[str, Any]:
        return storage.remove_link(issue_ref, link_id)

    @app.get("/api/v1/issues/{issue_ref}/worklogs")
    def api_list_worklogs(issue_ref: str) -> Dict[str, Any]:
        return storage.list_worklogs(issue_ref)

    @app.post("/api/v1/issues/{issue_ref}/worklogs", status_code=201)
    async def api_add_worklog(issue_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.add_worklog(
            issue_ref=issue_ref,
            minutes=body.get("minutes"),
            duration=body.get("duration"),
            author=body.get("author") or body.get("actor"),
            note=body.get("note"),
        )

    @app.delete("/api/v1/issues/{issue_ref}/worklogs/{worklog_id}")
    def api_delete_worklog(issue_ref: str, worklog_id: str) -> Dict[str, Any]:
        return storage.delete_worklog(issue_ref, worklog_id)

    @app.post("/api/v1/bulk/update")
    async def api_bulk_update(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.bulk_update(
            issues=body.get("issues"),
            query=body.get("query"),
            updates=body.get("updates"),
            actor=body.get("actor"),
        )

    @app.post("/api/v1/bulk/delete")
    async def api_bulk_delete(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.bulk_delete(
            issues=body.get("issues"),
            query=body.get("query"),
            actor=body.get("actor"),
        )

    @app.get("/api/v1/board")
    def api_board(
        swimlane: Optional[str] = Query(default="none"),
        category: Optional[str] = Query(default=None),
        priority: Optional[str] = Query(default=None),
        assignee: Optional[str] = Query(default=None),
        sprint: Optional[str] = Query(default=None),
        tag: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        return storage.get_board(
            swimlane=swimlane,
            category=category,
            priority=priority,
            assignee=assignee,
            sprint=sprint,
            tag=tag,
            q=q,
        )

    @app.get("/api/v1/search")
    def api_search(
        q: Optional[str] = Query(default=None),
        iql: Optional[str] = Query(default=None),
        page: int = Query(default=1),
        page_size: int = Query(default=25),
        sort: Optional[str] = Query(default=None),
        order: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        return storage.query_issues(
            tab="all",
            q=q,
            iql=iql,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )

    # --- Tags (Routes 42..45) ---

    @app.get("/api/v1/tags")
    def api_list_tags() -> Dict[str, Any]:
        return {"tags": storage.list_tags()}

    @app.post("/api/v1/tags", status_code=201)
    async def api_create_tag(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.create_tag(body)

    @app.patch("/api/v1/tags/{tag_name}")
    async def api_patch_tag(tag_name: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.update_tag(tag_name, body)

    @app.delete("/api/v1/tags/{tag_name}")
    def api_delete_tag(tag_name: str) -> Dict[str, Any]:
        return storage.delete_tag(tag_name)

    # --- Sprints (Routes 46..54) ---

    @app.get("/api/v1/sprints")
    def api_list_sprints(state: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        return {"sprints": storage.list_sprints(state=state)}

    @app.post("/api/v1/sprints", status_code=201)
    async def api_create_sprint(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.create_sprint(body)

    @app.get("/api/v1/sprints/{sprint_ref}")
    def api_get_sprint(sprint_ref: str) -> Dict[str, Any]:
        return storage.get_sprint(sprint_ref)

    @app.patch("/api/v1/sprints/{sprint_ref}")
    async def api_patch_sprint(sprint_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.update_sprint(sprint_ref, body)

    @app.post("/api/v1/sprints/{sprint_ref}/start")
    async def api_start_sprint(sprint_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.start_sprint(sprint_ref, start_date=body.get("start_date"), end_date=body.get("end_date"))

    @app.post("/api/v1/sprints/{sprint_ref}/complete")
    async def api_complete_sprint(sprint_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.complete_sprint(sprint_ref, rollover_to=body.get("rollover_to"))

    @app.post("/api/v1/sprints/{sprint_ref}/assign")
    async def api_assign_sprint(sprint_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.assign_sprint_issues(sprint_ref, issue_refs=_parse_str_list_value(body.get("issues")))

    @app.post("/api/v1/sprints/{sprint_ref}/remove")
    async def api_remove_sprint(sprint_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.remove_sprint_issues(sprint_ref, issue_refs=_parse_str_list_value(body.get("issues")))

    @app.delete("/api/v1/sprints/{sprint_ref}")
    def api_delete_sprint(sprint_ref: str) -> Dict[str, Any]:
        return storage.delete_sprint(sprint_ref)

    # --- Milestones (Routes 55..61) ---

    @app.get("/api/v1/milestones")
    def api_list_milestones(state: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        return {"milestones": storage.list_milestones(state=state)}

    @app.post("/api/v1/milestones", status_code=201)
    async def api_create_milestone(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.create_milestone(body)

    @app.get("/api/v1/milestones/{milestone_ref}")
    def api_get_milestone(milestone_ref: str) -> Dict[str, Any]:
        return storage.get_milestone(milestone_ref)

    @app.patch("/api/v1/milestones/{milestone_ref}")
    async def api_patch_milestone(milestone_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.update_milestone(milestone_ref, body)

    @app.post("/api/v1/milestones/{milestone_ref}/close")
    def api_close_milestone(milestone_ref: str) -> Dict[str, Any]:
        return storage.set_milestone_state(milestone_ref, closed=True)

    @app.post("/api/v1/milestones/{milestone_ref}/reopen")
    def api_reopen_milestone(milestone_ref: str) -> Dict[str, Any]:
        return storage.set_milestone_state(milestone_ref, closed=False)

    @app.delete("/api/v1/milestones/{milestone_ref}")
    def api_delete_milestone(milestone_ref: str) -> Dict[str, Any]:
        return storage.delete_milestone(milestone_ref)

    # --- Saved Views (Routes 62..67) ---

    @app.get("/api/v1/views")
    def api_list_views() -> Dict[str, Any]:
        return {"views": storage.list_views()}

    @app.post("/api/v1/views", status_code=201)
    async def api_create_view(request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.create_view(body)

    @app.get("/api/v1/views/{view_ref}")
    def api_get_view(view_ref: str) -> Dict[str, Any]:
        return storage.get_view(view_ref)

    @app.post("/api/v1/views/{view_ref}/run")
    async def api_run_view(view_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        page = int(body.get("page", 1))
        page_size = int(body.get("page_size", 25))
        return storage.run_view(view_ref, page=page, page_size=page_size)

    @app.patch("/api/v1/views/{view_ref}")
    async def api_patch_view(view_ref: str, request: Request) -> Dict[str, Any]:
        body = await _read_optional_json(request)
        return storage.update_view(view_ref, body)

    @app.delete("/api/v1/views/{view_ref}")
    def api_delete_view(view_ref: str) -> Dict[str, Any]:
        return storage.delete_view(view_ref)

    return app


# ============================================================================
# Section 6: Embedded Air-Gapped Offline Web UI (SPA_HTML)
# ============================================================================

SPA_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>iTrack — Local-First Issue Tracker</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <style>
    :root, [data-theme="dark"] {
      --bg-app: #0b0f19;
      --bg-panel: #111827;
      --bg-card: #1a2234;
      --bg-hover: #243047;
      --bg-input: #0f172a;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --border: #293548;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --danger: #ef4444;
      --success: #10b981;
      --warning: #f59e0b;
      --shadow: 0 10px 30px rgba(0,0,0,0.45);
    }
    [data-theme="light"] {
      --bg-app: #f8fafc;
      --bg-panel: #ffffff;
      --bg-card: #f1f5f9;
      --bg-hover: #e2e8f0;
      --bg-input: #ffffff;
      --text-main: #0f172a;
      --text-muted: #475569;
      --border: #cbd5e1;
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --danger: #dc2626;
      --success: #059669;
      --warning: #d97706;
      --shadow: 0 10px 25px rgba(15,23,42,0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Ubuntu, sans-serif;
      font-size: 13px;
      background: var(--bg-app);
      color: var(--text-main);
      height: 100vh;
      display: flex;
      overflow: hidden;
    }
    code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    button, input, select, textarea {
      font: inherit;
      color: var(--text-main);
    }
    input, select, textarea {
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 8px;
      outline: none;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--accent); }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 6px 11px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--bg-card);
      cursor: pointer;
      font-weight: 500;
      white-space: nowrap;
    }
    .btn:hover { background: var(--bg-hover); }
    .btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    .btn-sm { padding: 3px 7px; font-size: 12px; }
    .btn-ghost { background: transparent; border-color: transparent; }
    .btn-ghost:hover { background: var(--bg-hover); }

    /* App Layout */
    #sidebar {
      width: 230px;
      background: var(--bg-panel);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      transition: width 0.15s ease;
      flex-shrink: 0;
    }
    #sidebar.collapsed { width: 54px; }
    #sidebar.collapsed .nav-label, #sidebar.collapsed .sidebar-section-title, #sidebar.collapsed #sidebar-saved-views, #sidebar.collapsed #brand-title {
      display: none;
    }
    .sidebar-header {
      padding: 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      font-weight: 700;
      font-size: 15px;
    }
    .nav-list { padding: 8px; display: flex; flex-direction: column; gap: 3px; flex: 1; overflow-y: auto; }
    .nav-item {
      display: flex; align-items: center; gap: 9px;
      width: 100%; text-align: left;
      padding: 7px 10px; border-radius: 6px;
      border: none; background: transparent; cursor: pointer;
      color: var(--text-muted); font-weight: 500;
    }
    .nav-item:hover { background: var(--bg-hover); color: var(--text-main); }
    .nav-item.active { background: var(--accent); color: #fff; }
    .sidebar-section-title {
      font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
      color: var(--text-muted); margin: 12px 8px 4px;
    }
    #main-shell { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
    #topbar {
      height: 52px;
      background: var(--bg-panel);
      border-bottom: 1px solid var(--border);
      padding: 0 14px;
      display: flex; align-items: center; gap: 10px;
      flex-shrink: 0;
    }
    #global-search-input { flex: 1; max-width: 560px; }
    #content-area { flex: 1; overflow: auto; padding: 16px; }
    .view-pane { display: none; }
    .view-pane.active { display: block; }

    /* Badges & Chips */
    .badge {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 2px 7px; border-radius: 999px; font-size: 11px; font-weight: 600;
      background: var(--bg-hover); border: 1px solid var(--border);
    }
    .badge-overdue { background: rgba(239,68,68,0.18); color: #f87171; border-color: #ef4444; }
    .badge-blocked { background: rgba(245,158,11,0.18); color: #fbbf24; border-color: #f59e0b; }
    .badge-done { background: rgba(16,185,129,0.18); color: #34d399; border-color: #10b981; }
    .prio-critical { color: #ef4444; font-weight: 700; }
    .prio-high { color: #f97316; font-weight: 600; }
    .prio-medium { color: #3b82f6; }
    .prio-low { color: #10b981; }
    .prio-trivial { color: #94a3b8; }

    /* Tabs, Filters & Table */
    .preset-tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 10px; }
    .tab-chip {
      padding: 5px 11px; border-radius: 6px; border: 1px solid var(--border);
      background: var(--bg-panel); cursor: pointer; font-weight: 500; color: var(--text-muted);
    }
    .tab-chip.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    .filters-bar {
      display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
      background: var(--bg-panel); padding: 8px 10px; border-radius: 8px;
      border: 1px solid var(--border); margin-bottom: 10px;
    }
    .bulk-bar {
      display: none; align-items: center; gap: 8px; flex-wrap: wrap;
      background: rgba(59,130,246,0.14); border: 1px solid var(--accent);
      padding: 8px 12px; border-radius: 8px; margin-bottom: 10px;
    }
    .bulk-bar.open { display: flex; }
    table.issues-table {
      width: 100%; border-collapse: collapse; background: var(--bg-panel);
      border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    }
    table.issues-table th, table.issues-table td {
      padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left;
    }
    table.issues-table th {
      background: var(--bg-card); color: var(--text-muted); font-size: 11px;
      text-transform: uppercase; cursor: pointer; user-select: none;
    }
    table.issues-table tr:hover td { background: var(--bg-hover); }
    .pagination-bar {
      display: flex; justify-content: space-between; align-items: center;
      margin-top: 10px; padding: 8px; background: var(--bg-panel);
      border: 1px solid var(--border); border-radius: 8px; flex-wrap: wrap; gap: 8px;
    }

    /* Kanban Board */
    .swimlane-block { margin-bottom: 18px; }
    .swimlane-header {
      padding: 7px 12px; background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 6px; font-weight: 700; margin-bottom: 8px; display: flex; justify-content: space-between;
    }
    .kanban-row { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 8px; }
    .kanban-column {
      min-width: 172px; width: 172px; background: var(--bg-panel);
      border: 1px solid var(--border); border-radius: 8px;
      display: flex; flex-direction: column; max-height: 70vh; flex-shrink: 0;
    }
    .kanban-column.drag-over { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent); }
    .kanban-col-head {
      padding: 8px 10px; border-bottom: 1px solid var(--border);
      display: flex; justify-content: space-between; align-items: center; font-weight: 600;
    }
    .wip-exceeded { background: rgba(239,68,68,0.2); border-color: var(--danger); color: #f87171; }
    .kanban-cards { padding: 8px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; min-height: 110px; flex: 1; }
    .kanban-card {
      background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
      padding: 9px; cursor: grab; display: flex; flex-direction: column; gap: 5px;
      user-select: none; -webkit-user-select: none; -webkit-user-drag: element;
    }
    .kanban-card * { pointer-events: none; }
    .kanban-card:hover { border-color: var(--accent); }

    /* Drawer & Modals */
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.62);
      display: none; align-items: center; justify-content: center; z-index: 1000; padding: 16px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
      box-shadow: var(--shadow); width: 100%; max-width: 720px; max-height: 90vh;
      display: flex; flex-direction: column; overflow: hidden;
    }
    .modal-header {
      padding: 12px 16px; border-bottom: 1px solid var(--border);
      display: flex; justify-content: space-between; align-items: center; font-weight: 700; font-size: 15px;
    }
    .modal-body { padding: 16px; overflow-y: auto; flex: 1; }
    .modal-footer { padding: 10px 16px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }

    #issue-drawer {
      position: fixed; top: 0; right: 0; bottom: 0; width: min(980px, 94vw);
      background: var(--bg-panel); border-left: 1px solid var(--border);
      box-shadow: var(--shadow); z-index: 900; display: none; flex-direction: column;
    }
    #issue-drawer.open { display: flex; }
    .detail-grid { display: grid; grid-template-columns: 1fr 310px; gap: 16px; padding: 16px; overflow-y: auto; flex: 1; }
    @media (max-width: 860px) { .detail-grid { grid-template-columns: 1fr; } }
    .meta-sidebar { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 9px; }
    .meta-row { display: flex; flex-direction: column; gap: 3px; }
    .meta-row label { font-size: 11px; color: var(--text-muted); font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
    .progress-track { height: 7px; background: var(--bg-input); border-radius: 999px; overflow: hidden; border: 1px solid var(--border); }
    .progress-fill { height: 100%; background: var(--success); transition: width 0.2s; }
    .dropzone {
      border: 2px dashed var(--border); border-radius: 8px; padding: 12px;
      text-align: center; color: var(--text-muted); cursor: pointer;
    }
    .dropzone.drag-over { border-color: var(--accent); background: rgba(59,130,246,0.08); }

    /* Markdown Rendered Content */
    .md-body { line-height: 1.55; word-break: break-word; }
    .md-body pre { background: var(--bg-input); padding: 10px; border-radius: 6px; border: 1px solid var(--border); overflow-x: auto; }
    .md-body code { background: var(--bg-input); padding: 1px 5px; border-radius: 4px; }
    .md-body blockquote { margin: 6px 0; padding: 4px 12px; border-left: 3px solid var(--accent); color: var(--text-muted); }
    .md-body table { border-collapse: collapse; width: 100%; margin: 8px 0; }
    .md-body th, .md-body td { border: 1px solid var(--border); padding: 6px 8px; }
    .md-inline-embed { max-width: 280px; max-height: 200px; border-radius: 6px; border: 1px solid var(--border); cursor: zoom-in; display: inline-block; margin: 4px 0; }
    .deleted-att-badge {
      display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 4px;
      background: rgba(239,68,68,0.15); border: 1px dashed var(--danger); color: #f87171; font-size: 11px;
    }
    .card-panel { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
    .grid-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }

    /* Toast */
    #toast-container { position: fixed; bottom: 16px; right: 16px; z-index: 2000; display: flex; flex-direction: column; gap: 6px; }
    .toast {
      padding: 9px 14px; border-radius: 8px; background: var(--bg-card);
      border: 1px solid var(--accent); box-shadow: var(--shadow); font-weight: 500;
    }
    .toast-error { border-color: var(--danger); background: #3b1219; color: #fecaca; }
  </style>
</head>
<body>
  <!-- Sidebar -->
  <aside id="sidebar">
    <div class="sidebar-header">
      <span id="brand-title">iTrack</span>
      <button id="btn-toggle-sidebar" class="btn btn-sm btn-ghost" title="Collapse/Expand Sidebar">&#9776;</button>
    </div>
    <nav class="nav-list">
      <button class="nav-item active" data-view="issues"><span>&#9776;</span><span class="nav-label">Issues List</span></button>
      <button class="nav-item" data-view="board"><span>&#9638;</span><span class="nav-label">Kanban Board</span></button>
      <button class="nav-item" data-view="sprints"><span>&#9889;</span><span class="nav-label">Backlog &amp; Sprints</span></button>
      <button class="nav-item" data-view="milestones"><span>&#9873;</span><span class="nav-label">Milestones</span></button>
      <button class="nav-item" data-view="views"><span>&#9733;</span><span class="nav-label">Saved Views</span></button>
      <button class="nav-item" data-view="analytics"><span>&#9650;</span><span class="nav-label">Analytics Dashboard</span></button>
      <button class="nav-item" data-view="tags"><span>&#9830;</span><span class="nav-label">Tags</span></button>
      <button class="nav-item" data-view="settings"><span>&#9881;</span><span class="nav-label">Workspace Settings</span></button>

      <div class="sidebar-section-title">Saved Views</div>
      <div id="sidebar-saved-views"></div>
    </nav>
  </aside>

  <!-- Main Shell -->
  <div id="main-shell">
    <header id="topbar">
      <input id="global-search-input" type="text" placeholder="Search full-text or iQL (e.g. priority = critical AND state != done)... Press '/'" />
      <button id="btn-run-search" class="btn btn-sm">Search</button>
      <button id="btn-save-current-view" class="btn btn-sm" title="Save current filter as Named View">Save View</button>
      <div style="flex:1"></div>
      <button id="btn-open-create" class="btn btn-primary">+ New Issue (c)</button>
      <button id="btn-reindex" class="btn btn-sm" title="Rebuild SQLite + FTS5 index from disk">Reindex</button>
      <button id="btn-theme-toggle" class="btn btn-sm" title="Toggle Dark/Light Mode">Theme</button>
    </header>

    <main id="content-area">
      <!-- 1. Issues List View -->
      <section id="view-issues" class="view-pane active">
        <div class="preset-tabs" id="issues-preset-tabs">
          <button class="tab-chip" data-tab="active">Active</button>
          <button class="tab-chip" data-tab="open">Open</button>
          <button class="tab-chip" data-tab="in-progress">In Progress</button>
          <button class="tab-chip" data-tab="blocked">Blocked</button>
          <button class="tab-chip" data-tab="completed">Completed</button>
          <button class="tab-chip" data-tab="cancelled">Cancelled</button>
          <button class="tab-chip" data-tab="archived">Archived</button>
          <button class="tab-chip active" data-tab="all">All</button>
        </div>

        <div class="filters-bar">
          <select id="filter-category"><option value="">All Categories</option></select>
          <select id="filter-state"><option value="">All States</option></select>
          <select id="filter-priority"><option value="">All Priorities</option></select>
          <select id="filter-severity"><option value="">All Severities</option></select>
          <input id="filter-assignee" type="text" placeholder="Assignee..." style="width:110px" />
          <select id="filter-tag"><option value="">All Tags</option></select>
          <select id="filter-sprint"><option value="">All Sprints</option></select>
          <select id="filter-milestone"><option value="">All Milestones</option></select>
          <label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer">
            <input id="filter-overdue" type="checkbox" /> Overdue
          </label>
          <button id="btn-clear-filters" class="btn btn-sm">Clear Filters</button>
          <button id="btn-open-global-tree" class="btn btn-sm">Hierarchy Tree</button>
        </div>

        <div id="bulk-action-bar" class="bulk-bar">
          <strong><span id="bulk-selected-count">0</span> selected:</strong>
          <select id="bulk-state"><option value="">Set State...</option></select>
          <select id="bulk-priority"><option value="">Set Priority...</option></select>
          <input id="bulk-assignee" type="text" placeholder="Assignee..." style="width:110px" />
          <input id="bulk-tag" type="text" placeholder="Add Tag..." style="width:100px" />
          <select id="bulk-sprint"><option value="">Assign Sprint...</option></select>
          <button id="btn-bulk-apply" class="btn btn-sm btn-primary">Apply Bulk Update</button>
          <button id="btn-bulk-archive" class="btn btn-sm">Archive</button>
          <button id="btn-bulk-delete" class="btn btn-sm btn-danger">Bulk Delete</button>
        </div>

        <div id="issues-table-wrap">
          <table class="issues-table">
            <thead>
              <tr>
                <th style="width:32px"><input type="checkbox" id="check-all-issues" /></th>
                <th data-sort="key">Key</th>
                <th data-sort="name">Name</th>
                <th data-sort="category">Category</th>
                <th data-sort="state">State</th>
                <th data-sort="priority">Priority</th>
                <th data-sort="severity">Severity</th>
                <th data-sort="assignee">Assignee</th>
                <th data-sort="story_points">Pts</th>
                <th data-sort="due_date">Due</th>
                <th data-sort="updated_at">Updated</th>
              </tr>
            </thead>
            <tbody id="issues-tbody"></tbody>
          </table>
          <div id="issues-empty-state" class="card-panel" style="display:none;text-align:center;margin-top:12px;">
            <p>No issues match your current filters or query.</p>
            <button id="btn-empty-clear-filters" class="btn btn-sm btn-primary">Clear All Filters</button>
          </div>
        </div>

        <div class="pagination-bar">
          <div>
            Showing <span id="page-summary">0</span> issues |
            Page Size:
            <select id="page-size-select">
              <option value="10">10</option>
              <option value="25" selected>25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
          </div>
          <div style="display:flex;align-items:center;gap:5px;">
            <button id="btn-page-first" class="btn btn-sm">&laquo; First</button>
            <button id="btn-page-prev" class="btn btn-sm">&lsaquo; Prev</button>
            <span id="pagination-pages" style="display:inline-flex;gap:4px;"></span>
            <button id="btn-page-next" class="btn btn-sm">Next &rsaquo;</button>
            <button id="btn-page-last" class="btn btn-sm">Last &raquo;</button>
          </div>
        </div>
      </section>

      <!-- 2. Kanban Board View -->
      <section id="view-board" class="view-pane">
        <div class="filters-bar">
          <label>Swimlane:
            <select id="board-swimlane-select">
              <option value="none">None</option>
              <option value="category">Category</option>
              <option value="priority">Priority</option>
              <option value="assignee">Assignee</option>
              <option value="sprint">Sprint</option>
            </select>
          </label>
          <select id="board-filter-category"><option value="">All Categories</option></select>
          <select id="board-filter-priority"><option value="">All Priorities</option></select>
          <input id="board-filter-assignee" type="text" placeholder="Filter Assignee..." style="width:130px" />
          <select id="board-filter-sprint"><option value="">All Sprints</option></select>
          <input id="board-filter-q" type="text" placeholder="Filter Board..." style="width:150px" />
          <button id="btn-refresh-board" class="btn btn-sm">Apply</button>
        </div>
        <div id="board-lanes-container"></div>
      </section>

      <!-- 3. Backlog & Sprints View -->
      <section id="view-sprints" class="view-pane">
        <div class="card-panel">
          <h3 style="margin-top:0">Create Sprint</h3>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <input id="sprint-create-name" type="text" placeholder="Sprint Name (e.g. Sprint 1)" />
            <input id="sprint-create-goal" type="text" placeholder="Sprint Goal" style="width:240px" />
            <input id="sprint-create-start" type="date" />
            <input id="sprint-create-end" type="date" />
            <button id="btn-create-sprint" class="btn btn-primary">Create Sprint</button>
          </div>
        </div>
        <div id="sprints-list-container"></div>
      </section>

      <!-- 4. Milestones View -->
      <section id="view-milestones" class="view-pane">
        <div class="card-panel">
          <h3 style="margin-top:0">Create Milestone</h3>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <input id="ms-create-name" type="text" placeholder="Milestone Name (e.g. v2.0 GA)" />
            <input id="ms-create-desc" type="text" placeholder="Description" style="width:260px" />
            <input id="ms-create-due" type="date" />
            <button id="btn-create-milestone" class="btn btn-primary">Create Milestone</button>
          </div>
        </div>
        <div id="milestones-list-container" class="grid-cards"></div>
      </section>

      <!-- 5. Saved Views View -->
      <section id="view-views" class="view-pane">
        <div class="card-panel" style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <h3 style="margin:0">Named Saved Views</h3>
            <div style="color:var(--text-muted)">Save frequently used iQL queries, tabs, and sorts.</div>
          </div>
          <button id="btn-views-open-save-modal" class="btn btn-primary">+ New Saved View</button>
        </div>
        <div id="views-list-container" class="grid-cards"></div>
      </section>

      <!-- 6. Analytics Dashboard View -->
      <section id="view-analytics" class="view-pane">
        <div id="analytics-summary-cards" class="grid-cards" style="margin-bottom:16px;"></div>
        <div id="analytics-distributions" class="grid-cards"></div>
      </section>

      <!-- 7. Tags View -->
      <section id="view-tags" class="view-pane">
        <div class="card-panel">
          <h3 style="margin-top:0">Create Colored Tag</h3>
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
            <input id="tag-create-name" type="text" placeholder="Tag Name" />
            <input id="tag-create-color" type="color" value="#3b82f6" style="padding:2px;height:32px;width:48px" />
            <input id="tag-create-desc" type="text" placeholder="Description" style="width:240px" />
            <button id="btn-create-tag" class="btn btn-primary">Create Tag</button>
          </div>
        </div>
        <div id="tags-list-container" class="grid-cards"></div>
      </section>

      <!-- 8. Workspace Settings View -->
      <section id="view-settings" class="view-pane">
        <div class="card-panel" style="max-width:640px;">
          <h3 style="margin-top:0">Workspace Configuration</h3>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
            <label class="meta-row">Project Title <input id="cfg-project-title" type="text" /></label>
            <label class="meta-row">Project Prefix <input id="cfg-project-prefix" type="text" /></label>
            <label class="meta-row">Default Category <select id="cfg-default-category"></select></label>
            <label class="meta-row">Default State <select id="cfg-default-state"></select></label>
            <label class="meta-row">Default Priority <select id="cfg-default-priority"></select></label>
            <label class="meta-row">Default Severity <select id="cfg-default-severity"></select></label>
            <label class="meta-row">In-Progress WIP Limit <input id="cfg-wip-in-progress" type="number" min="0" placeholder="0 = unlimited" /></label>
            <label class="meta-row">Default Assignee <input id="cfg-default-assignee" type="text" /></label>
          </div>
          <div style="margin-top:12px;">
            <button id="btn-save-settings" class="btn btn-primary">Save Settings</button>
          </div>
        </div>
      </section>

      <!-- Permalink Full-Page Issue Container (#/issues/<ref>) -->
      <section id="view-issue-permalink" class="view-pane">
        <div style="margin-bottom:10px;">
          <button id="btn-permalink-back" class="btn btn-sm">&larr; Back to Issues</button>
        </div>
        <div id="permalink-detail-mount" class="card-panel" style="padding:0;"></div>
      </section>
    </main>
  </div>

  <!-- Slide-Over Issue Detail Drawer (data-modal="issue-detail") -->
  <div id="issue-drawer" data-modal="issue-detail">
    <div class="modal-header">
      <div style="display:flex;align-items:center;gap:8px;flex:1;min-width:0;">
        <span id="detail-key-badge" class="badge mono"></span>
        <input id="detail-title-input" type="text" style="flex:1;font-weight:700;font-size:15px;" />
        <button id="btn-save-title" class="btn btn-sm">Rename</button>
      </div>
      <div style="display:flex;gap:5px;align-items:center;margin-left:8px;">
        <button id="btn-open-permalink" class="btn btn-sm" title="Open Full-Page Permalink">Permalink</button>
        <button id="btn-clone-issue" class="btn btn-sm">Clone</button>
        <button id="btn-copy-context" class="btn btn-sm">Copy Agent Context</button>
        <button id="btn-archive-issue" class="btn btn-sm">Archive</button>
        <button id="btn-unarchive-issue" class="btn btn-sm" style="display:none;">Unarchive</button>
        <button id="btn-delete-issue" class="btn btn-sm btn-danger">Delete</button>
        <button id="btn-close-drawer" class="btn btn-sm btn-ghost">&#10005;</button>
      </div>
    </div>
    <div id="drawer-detail-body" class="detail-grid">
      <!-- Left Column -->
      <div style="min-width:0;display:flex;flex-direction:column;gap:14px;">
        <div id="detail-status-banners" style="display:flex;gap:6px;flex-wrap:wrap;"></div>

        <!-- Description & Inline Embeds -->
        <div class="card-panel" style="margin:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong>Description</strong>
            <div style="display:flex;gap:5px;">
              <label class="btn btn-sm" style="cursor:pointer;">
                + Embed Image <input id="detail-desc-embed-input" type="file" accept="image/*" multiple style="display:none" />
              </label>
              <button id="btn-edit-desc" class="btn btn-sm">Edit</button>
              <button id="btn-clear-desc" class="btn btn-sm">Clear</button>
            </div>
          </div>
          <div id="detail-desc-rendered" class="md-body"></div>
          <div id="detail-desc-editor" style="display:none;margin-top:8px;">
            <textarea id="detail-desc-textarea" rows="5" style="width:100%;"></textarea>
            <div style="display:flex;gap:6px;margin-top:6px;">
              <button id="btn-save-desc" class="btn btn-sm btn-primary">Save Description</button>
              <button id="btn-cancel-desc" class="btn btn-sm">Cancel</button>
            </div>
          </div>
          <div id="detail-desc-embeds-bar" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;"></div>
        </div>

        <!-- Acceptance Criteria -->
        <div class="card-panel" style="margin:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <strong>Acceptance Criteria (<span id="detail-ac-summary">0/0</span>)</strong>
            <button id="btn-clear-ac" class="btn btn-sm">Clear All AC</button>
          </div>
          <div class="progress-track" style="margin-bottom:8px;"><div id="detail-ac-progress" class="progress-fill" style="width:0%"></div></div>
          <div id="detail-ac-list" style="display:flex;flex-direction:column;gap:5px;margin-bottom:8px;"></div>
          <div style="display:flex;gap:6px;">
            <input id="detail-ac-input" type="text" placeholder="Add acceptance criterion..." style="flex:1" />
            <button id="btn-add-ac" class="btn btn-sm btn-primary">Add AC</button>
          </div>
        </div>

        <!-- Subtasks & Hierarchy -->
        <div class="card-panel" style="margin:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <strong>Subtasks (<span id="detail-subtask-summary">0/0</span>)</strong>
            <button id="btn-view-tree" class="btn btn-sm">Hierarchy / Dependency Tree</button>
          </div>
          <div class="progress-track" style="margin-bottom:8px;"><div id="detail-subtask-progress" class="progress-fill" style="width:0%"></div></div>
          <div id="detail-subtasks-list" style="display:flex;flex-direction:column;gap:5px;margin-bottom:8px;"></div>
          <div style="display:flex;gap:6px;">
            <input id="detail-subtask-input" type="text" placeholder="New subtask title (spaces allowed)..." style="flex:1" />
            <button id="btn-add-subtask" class="btn btn-sm btn-primary">Add Subtask</button>
          </div>
        </div>

        <!-- Links -->
        <div class="card-panel" style="margin:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <strong>Linked Issues</strong>
            <button id="btn-open-link-modal" class="btn btn-sm">+ Link Issue</button>
          </div>
          <div id="detail-links-list" style="display:flex;flex-direction:column;gap:5px;"></div>
        </div>

        <!-- Whole-Issue Attachments -->
        <div class="card-panel" style="margin:0;">
          <strong>Whole-Issue Attachments</strong>
          <div id="detail-attach-dropzone" class="dropzone" style="margin:8px 0;">
            Drag &amp; drop files here or
            <label style="color:var(--accent);cursor:pointer;font-weight:600;">
              browse <input id="detail-attach-input" type="file" multiple style="display:none" />
            </label>
          </div>
          <div id="detail-attachments-list" style="display:flex;flex-direction:column;gap:6px;"></div>
        </div>

        <!-- Comments & History Tabs -->
        <div class="card-panel" style="margin:0;">
          <div style="display:flex;gap:6px;margin-bottom:10px;border-bottom:1px solid var(--border);padding-bottom:8px;">
            <button id="tab-btn-comments" class="btn btn-sm btn-primary">Comments (<span id="detail-comment-count">0</span>)</button>
            <button id="tab-btn-history" class="btn btn-sm">History Diffs</button>
          </div>

          <div id="detail-comments-pane">
            <!-- Rich Comment Composer -->
            <div id="cmt-composer-dropzone" style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:12px;background:var(--bg-card);">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:6px;">
                <div style="display:flex;gap:4px;">
                  <button id="cmt-tab-write" type="button" class="btn btn-sm btn-primary">Write</button>
                  <button id="cmt-tab-preview" type="button" class="btn btn-sm">Preview</button>
                </div>
                <div style="display:flex;gap:5px;flex-wrap:wrap;">
                  <label class="btn btn-sm" style="cursor:pointer;">
                    Embed Image <input id="cmt-embed-input" type="file" accept="image/*" multiple style="display:none" />
                  </label>
                  <label class="btn btn-sm" style="cursor:pointer;">
                    Attach File <input id="cmt-attach-input" type="file" multiple style="display:none" />
                  </label>
                  <button id="btn-cmt-link-att" type="button" class="btn btn-sm">Link Existing Issue Attachment</button>
                </div>
              </div>
              <textarea id="cmt-body-input" rows="4" style="width:100%;" placeholder="Write Markdown comment... Paste image (Ctrl+V) or drag &amp; drop files here!"></textarea>
              <div id="cmt-preview-box" class="md-body" style="display:none;min-height:80px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);"></div>
              <div id="cmt-staged-bar" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;"></div>
              <div style="display:flex;justify-content:flex-end;margin-top:8px;">
                <button id="btn-submit-comment" type="button" class="btn btn-primary">Post Comment</button>
              </div>
            </div>

            <div id="detail-comments-list" style="display:flex;flex-direction:column;gap:10px;"></div>
          </div>

          <div id="detail-history-pane" style="display:none;">
            <div id="detail-history-list" style="display:flex;flex-direction:column;gap:8px;"></div>
          </div>
        </div>
      </div>

      <!-- Right Metadata Sidebar -->
      <div class="meta-sidebar">
        <div class="meta-row"><label>Category</label><select id="detail-category"></select></div>
        <div class="meta-row"><label>State</label><select id="detail-state"></select></div>
        <div class="meta-row"><label>Priority</label><select id="detail-priority"></select></div>
        <div class="meta-row"><label>Severity</label><select id="detail-severity"></select></div>
        <div class="meta-row">
          <label>Resolution <button id="btn-clear-resolution" class="btn btn-sm btn-ghost">Clear</button></label>
          <select id="detail-resolution"><option value="">(Unresolved)</option></select>
        </div>
        <div class="meta-row">
          <label>Assignee <button id="btn-clear-assignee" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-assignee" type="text" placeholder="Unassigned" />
        </div>
        <div class="meta-row">
          <label>Reporter <button id="btn-clear-reporter" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-reporter" type="text" placeholder="Reporter" />
        </div>
        <div class="meta-row">
          <label>Parent / Epic <button id="btn-clear-parent" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-parent" type="text" placeholder="ITR-N or Name" />
        </div>
        <div class="meta-row">
          <label>Sprint <button id="btn-clear-sprint" class="btn btn-sm btn-ghost">Clear</button></label>
          <select id="detail-sprint"><option value="">(No Sprint)</option></select>
        </div>
        <div class="meta-row">
          <label>Milestone <button id="btn-clear-milestone" class="btn btn-sm btn-ghost">Clear</button></label>
          <select id="detail-milestone"><option value="">(No Milestone)</option></select>
        </div>
        <div class="meta-row">
          <label>Story Points <button id="btn-clear-points" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-points" type="number" step="0.5" min="0" />
        </div>
        <div class="meta-row">
          <label>Estimate (min) <button id="btn-clear-estimate" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-estimate" type="number" step="1" min="0" />
        </div>
        <div class="meta-row">
          <label>Due Date <span id="detail-overdue-badge"></span> <button id="btn-clear-due" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-due" type="date" />
        </div>
        <div class="meta-row">
          <label>Tags (comma-sep) <button id="btn-clear-tags" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-tags" type="text" placeholder="auth, backend" />
        </div>
        <div class="meta-row">
          <label>Components (comma-sep) <button id="btn-clear-components" class="btn btn-sm btn-ghost">Clear</button></label>
          <input id="detail-components" type="text" placeholder="api, ui" />
        </div>

        <!-- Custom Fields -->
        <div class="meta-row" style="border-top:1px solid var(--border);padding-top:8px;">
          <label>Custom Fields <button id="btn-clear-cf" class="btn btn-sm btn-ghost">Clear All</button></label>
          <div id="detail-cf-list" style="display:flex;flex-direction:column;gap:4px;"></div>
          <div style="display:flex;gap:4px;margin-top:4px;">
            <input id="detail-cf-key" type="text" placeholder="key" style="width:42%" />
            <input id="detail-cf-val" type="text" placeholder="value" style="width:42%" />
            <button id="btn-add-cf" class="btn btn-sm">+</button>
          </div>
        </div>

        <!-- Time Tracking / Worklogs -->
        <div class="meta-row" style="border-top:1px solid var(--border);padding-top:8px;">
          <label>Time Logged (<span id="detail-time-summary">0m</span>) <button id="btn-open-worklog-modal" class="btn btn-sm">+ Log Work</button></label>
          <div class="progress-track"><div id="detail-time-progress" class="progress-fill" style="width:0%"></div></div>
          <div id="detail-worklogs-list" style="display:flex;flex-direction:column;gap:4px;margin-top:4px;"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal 1: Create Issue (data-modal="create-issue") -->
  <div id="modal-create-issue" class="modal-backdrop" data-modal="create-issue">
    <div class="modal-box">
      <div class="modal-header">
        <span>Create Issue</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="create-issue">&#10005;</button>
      </div>
      <div class="modal-body" style="display:flex;flex-direction:column;gap:10px;">
        <label class="meta-row">Issue Name (Spaces &amp; Unicode supported — creates exact folder) *
          <input id="create-name" type="text" placeholder="e.g. OAuth2 Token Refresh Race Condition" />
        </label>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">
          <label class="meta-row">Category <select id="create-category"></select></label>
          <label class="meta-row">Initial State <select id="create-state"></select></label>
          <label class="meta-row">Priority <select id="create-priority"></select></label>
          <label class="meta-row">Severity <select id="create-severity"></select></label>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
          <label class="meta-row">Assignee <input id="create-assignee" type="text" /></label>
          <label class="meta-row">Parent / Epic <input id="create-parent" type="text" placeholder="ITR-N or Name" /></label>
          <label class="meta-row">Due Date <input id="create-due" type="date" /></label>
          <label class="meta-row">Sprint <select id="create-sprint"><option value="">(None)</option></select></label>
          <label class="meta-row">Milestone <select id="create-milestone"><option value="">(None)</option></select></label>
          <label class="meta-row">Story Points <input id="create-points" type="number" step="0.5" min="0" /></label>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">
          <label class="meta-row">Estimate (min) <input id="create-estimate" type="number" min="0" /></label>
          <label class="meta-row">Tags (comma-sep) <input id="create-tags" type="text" /></label>
          <label class="meta-row">Components (comma-sep) <input id="create-components" type="text" /></label>
        </div>
        <label class="meta-row">Description (Markdown)
          <textarea id="create-description" rows="4" placeholder="Rich Markdown description..."></textarea>
        </label>
        <div style="display:flex;gap:8px;align-items:center;">
          <label class="btn btn-sm" style="cursor:pointer;">
            + Inline Embed Image <input id="create-embed-input" type="file" accept="image/*" multiple style="display:none" />
          </label>
          <span id="create-embeds-queued" style="color:var(--text-muted);font-size:12px;"></span>
        </div>
        <label class="meta-row">Acceptance Criteria (one per line)
          <textarea id="create-ac" rows="2" placeholder="Concurrent 401 responses trigger single refresh"></textarea>
        </label>
        <label class="meta-row">Custom Fields (key=value per line)
          <textarea id="create-custom-fields" rows="2" placeholder="env=prod"></textarea>
        </label>
        <div id="create-attach-dropzone" class="dropzone">
          Drag &amp; drop whole-issue attachments or
          <label style="color:var(--accent);cursor:pointer;font-weight:600;">
            choose files <input id="create-attach-input" type="file" multiple style="display:none" />
          </label>
          <div id="create-attach-queued" style="font-size:12px;margin-top:4px;"></div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" data-close-modal="create-issue">Cancel</button>
        <button id="btn-submit-create-issue" class="btn btn-primary">Create Issue</button>
      </div>
    </div>
  </div>

  <!-- Modal 3: Delete Confirm (data-modal="delete-confirm") -->
  <div id="modal-delete-confirm" class="modal-backdrop" data-modal="delete-confirm">
    <div class="modal-box" style="max-width:440px;">
      <div class="modal-header">
        <span>Confirm Permanent Delete</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="delete-confirm">&#10005;</button>
      </div>
      <div class="modal-body">
        <p id="delete-confirm-message">Permanently delete this issue folder and all its attachments &amp; comments?</p>
      </div>
      <div class="modal-footer">
        <button id="btn-cancel-delete" class="btn" data-close-modal="delete-confirm">Cancel</button>
        <button id="btn-confirm-delete" class="btn btn-danger">Confirm Delete</button>
      </div>
    </div>
  </div>

  <!-- Modal 4: Link Issue & Hierarchy Tree (data-modal="link-issue") -->
  <div id="modal-link-issue" class="modal-backdrop" data-modal="link-issue">
    <div class="modal-box" style="max-width:580px;">
      <div class="modal-header">
        <span>Link Issue &amp; Dependency / Hierarchy Tree</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="link-issue">&#10005;</button>
      </div>
      <div class="modal-body" style="display:flex;flex-direction:column;gap:12px;">
        <div id="link-form-row" style="display:flex;gap:8px;flex-wrap:wrap;">
          <select id="link-modal-type">
            <option value="blocks">blocks</option>
            <option value="blocked-by">blocked-by</option>
            <option value="relates-to">relates-to</option>
            <option value="duplicates">duplicates</option>
            <option value="parent-of">parent-of</option>
            <option value="clones">clones</option>
          </select>
          <input id="link-modal-target" type="text" placeholder="Target Key (ITR-2) or Exact Issue Name" style="flex:1" />
          <button id="btn-submit-link" class="btn btn-primary">Add Link</button>
        </div>
        <div>
          <strong>Hierarchy &amp; Dependency Tree</strong>
          <pre id="modal-tree-hierarchy" style="background:var(--bg-input);padding:10px;border-radius:6px;border:1px solid var(--border);max-height:280px;overflow:auto;margin-top:6px;"></pre>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" data-close-modal="link-issue">Close</button>
      </div>
    </div>
  </div>

  <!-- Modal 5: Log Work (data-modal="log-work") -->
  <div id="modal-log-work" class="modal-backdrop" data-modal="log-work">
    <div class="modal-box" style="max-width:460px;">
      <div class="modal-header">
        <span>Log Work</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="log-work">&#10005;</button>
      </div>
      <div class="modal-body" style="display:flex;flex-direction:column;gap:10px;">
        <label class="meta-row">Duration (e.g. 90, 1h 30m, 45m)
          <input id="worklog-modal-duration" type="text" placeholder="1h 30m" />
        </label>
        <label class="meta-row">Note
          <input id="worklog-modal-note" type="text" placeholder="Investigated root cause" />
        </label>
      </div>
      <div class="modal-footer">
        <button class="btn" data-close-modal="log-work">Cancel</button>
        <button id="btn-submit-worklog" class="btn btn-primary">Log Time</button>
      </div>
    </div>
  </div>

  <!-- Modal 6: Sprint Rollover (data-modal="sprint-rollover") -->
  <div id="modal-sprint-rollover" class="modal-backdrop" data-modal="sprint-rollover">
    <div class="modal-box" style="max-width:480px;">
      <div class="modal-header">
        <span>Complete Sprint &amp; Rollover Incomplete Issues</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="sprint-rollover">&#10005;</button>
      </div>
      <div class="modal-body" style="display:flex;flex-direction:column;gap:10px;">
        <p id="rollover-modal-info" style="margin:0;"></p>
        <label class="meta-row">Move incomplete issues to:
          <select id="rollover-target-select"><option value="">Backlog (No Sprint)</option></select>
        </label>
      </div>
      <div class="modal-footer">
        <button class="btn" data-close-modal="sprint-rollover">Cancel</button>
        <button id="btn-confirm-sprint-complete" class="btn btn-primary">Complete Sprint</button>
      </div>
    </div>
  </div>

  <!-- Modal 7: Image Lightbox (data-modal="image-lightbox") -->
  <div id="modal-image-lightbox" class="modal-backdrop" data-modal="image-lightbox">
    <div class="modal-box" style="max-width:88vw;">
      <div class="modal-header">
        <span id="lightbox-title">Image Preview</span>
        <div style="display:flex;gap:8px;align-items:center;">
          <a id="lightbox-download-link" class="btn btn-sm" href="#" download>Download</a>
          <button class="btn btn-sm btn-ghost" data-close-modal="image-lightbox">&#10005;</button>
        </div>
      </div>
      <div class="modal-body" style="text-align:center;background:var(--bg-input);">
        <img id="lightbox-img" src="" alt="Preview" style="max-width:100%;max-height:74vh;border-radius:6px;" />
      </div>
    </div>
  </div>

  <!-- Modal 8: Attachment Text/Code Preview (data-modal="attachment-preview") -->
  <div id="modal-attachment-preview" class="modal-backdrop" data-modal="attachment-preview">
    <div class="modal-box" style="max-width:740px;">
      <div class="modal-header">
        <span id="att-preview-title">Attachment Preview</span>
        <div style="display:flex;gap:8px;align-items:center;">
          <a id="att-preview-download" class="btn btn-sm" href="#" download>Download</a>
          <button class="btn btn-sm btn-ghost" data-close-modal="attachment-preview">&#10005;</button>
        </div>
      </div>
      <div class="modal-body">
        <div id="att-preview-meta" style="color:var(--text-muted);font-size:12px;margin-bottom:8px;"></div>
        <pre id="att-preview-content" style="background:var(--bg-input);padding:12px;border-radius:6px;border:1px solid var(--border);max-height:60vh;overflow:auto;white-space:pre-wrap;"></pre>
      </div>
    </div>
  </div>

  <!-- Modal 9: Link Existing Issue Attachment Picker (data-modal="link-attachment") -->
  <div id="modal-link-attachment" class="modal-backdrop" data-modal="link-attachment">
    <div class="modal-box" style="max-width:500px;">
      <div class="modal-header">
        <span>Link Existing Issue Attachment</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="link-attachment">&#10005;</button>
      </div>
      <div class="modal-body">
        <div id="link-att-picker-list" style="display:flex;flex-direction:column;gap:6px;"></div>
      </div>
      <div class="modal-footer">
        <button class="btn" data-close-modal="link-attachment">Cancel</button>
        <button id="btn-confirm-link-att" class="btn btn-primary">Link Selected</button>
      </div>
    </div>
  </div>

  <!-- Modal 10: Save Named View (data-modal="save-view") -->
  <div id="modal-save-view" class="modal-backdrop" data-modal="save-view">
    <div class="modal-box" style="max-width:480px;">
      <div class="modal-header">
        <span>Save Named View</span>
        <button class="btn btn-sm btn-ghost" data-close-modal="save-view">&#10005;</button>
      </div>
      <div class="modal-body" style="display:flex;flex-direction:column;gap:10px;">
        <label class="meta-row">View Name * <input id="save-view-name" type="text" placeholder="Critical Auth Bugs" /></label>
        <label class="meta-row">iQL Query <input id="save-view-query" type="text" placeholder="priority = critical AND category = bug" /></label>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          <label class="meta-row">Preset Tab <select id="save-view-tab"></select></label>
          <label class="meta-row">Description <input id="save-view-desc" type="text" /></label>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" data-close-modal="save-view">Cancel</button>
        <button id="btn-submit-save-view" class="btn btn-primary">Save View</button>
      </div>
    </div>
  </div>

  <div id="toast-container"></div>
"""

SPA_HTML += r"""  <script>
  (function() {
    'use strict';

    // Telemetry hook for 100% UI Coverage Gate
    window.__itrackTelemetry = { views: new Set(), modals: new Set(), actions: new Set() };
    window.getItrackTelemetry = function() {
      return {
        views: Array.from(window.__itrackTelemetry.views),
        modals: Array.from(window.__itrackTelemetry.modals),
        actions: Array.from(window.__itrackTelemetry.actions),
      };
    };

    const CATEGORIES = ['epic','story','task','subtask','bug','feature','improvement','refactor','spike','incident','security','chore','docs'];
    const STATES = ['triage','backlog','todo','ready','in-progress','in-review','in-qa','on-hold','blocked','done','cancelled','archived'];
    const PRIORITIES = ['critical','high','medium','low','trivial'];
    const SEVERITIES = ['sev1','sev2','sev3','sev4','sev5'];
    const RESOLUTIONS = ['fixed','wont-fix','duplicate','cannot-reproduce','by-design','obsolete'];
    const PRESET_TABS = ['active','open','in-progress','blocked','completed','cancelled','archived','all'];

    const appState = {
      currentView: 'issues',
      tab: 'all',
      sort: 'key',
      order: 'asc',
      page: 1,
      pageSize: 25,
      selectedKeys: new Set(),
      activeIssue: null,
      permalinkMode: false,
      sprints: [],
      milestones: [],
      tags: [],
      views: [],
      config: {},
      createEmbedFiles: [],
      createAttachFiles: [],
      cmtEmbedFiles: [],
      cmtAttachFiles: [],
      cmtLinkedIds: [],
      pendingRolloverSprintId: null,
      suppressHashHandler: false,
    };

    const $ = (id) => document.getElementById(id);

    function esc(val) {
      return String(val === null || val === undefined ? '' : val)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function toast(msg, isError) {
      const box = $('toast-container');
      const el = document.createElement('div');
      el.className = 'toast' + (isError ? ' toast-error' : '');
      el.textContent = msg;
      box.appendChild(el);
      setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 3600);
    }

    async function api(url, opts) {
      const options = Object.assign({ method: 'GET', headers: {} }, opts || {});
      if (options.body && !(options.body instanceof FormData) && typeof options.body === 'object') {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(options.body);
      }
      const resp = await fetch(url, options);
      if (!resp.ok) {
        let errMsg = `HTTP ${resp.status}`;
        try {
          const data = await resp.json();
          if (data && data.detail) {
            errMsg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
          }
        } catch (_) {}
        throw new Error(errMsg);
      }
      const ct = resp.headers.get('content-type') || '';
      if (ct.includes('application/json')) return await resp.json();
      return await resp.text();
    }

    function populateSelect(sel, values, includeEmptyLabel) {
      if (!sel) return;
      const cur = sel.value;
      let html = includeEmptyLabel !== undefined ? `<option value="">${esc(includeEmptyLabel)}</option>` : '';
      values.forEach((v) => {
        const val = typeof v === 'object' ? v.value : v;
        const lbl = typeof v === 'object' ? v.label : v;
        html += `<option value="${esc(val)}">${esc(lbl)}</option>`;
      });
      if (sel.innerHTML !== html) {
        sel.innerHTML = html;
        if (cur) sel.value = cur;
      }
    }

    // ----------------------------------------------------------------------
    // Strict XSS-Safe Rich Markdown Renderer
    // ----------------------------------------------------------------------
    function sanitizeHref(rawUrl) {
      const decoded = String(rawUrl || '').trim();
      const low = decoded.toLowerCase().replace(/\s+/g, '');
      if (low.startsWith('javascript:') || low.startsWith('vbscript:') || low.startsWith('data:text/html')) {
        return '#';
      }
      if (/^(https?:\/\/|mailto:|\/|#)/i.test(decoded)) return decoded;
      return '#';
    }

    function renderInlineMarkdown(escapedLine, issueCtx) {
      const codeSpans = [];
      let s = escapedLine.replace(/`([^`]+)`/g, (_, code) => {
        const idx = codeSpans.length;
        codeSpans.push(`<code>${code}</code>`);
        return `\x00CODE${idx}\x00`;
      });

      const deletedMap = (issueCtx && issueCtx.deleted_attachments) ? issueCtx.deleted_attachments : {};
      const issueKey = (issueCtx && issueCtx.key) ? issueCtx.key : '';
      const stagedMap = (issueCtx && issueCtx.stagedEmbedUrls) ? issueCtx.stagedEmbedUrls : {};

      // Inline images: ![alt](attachment://ATT-X) or ![alt](embed:name)
      s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, rawTarget) => {
        const tgt = rawTarget.replace(/&amp;/g, '&').trim();
        if (tgt.toLowerCase().startsWith('attachment://')) {
          const attId = tgt.slice('attachment://'.length).trim().toUpperCase();
          if (deletedMap[attId]) {
            const fn = esc(deletedMap[attId].filename || attId);
            return `<span class="deleted-att-badge" data-att-id="${esc(attId)}">[Deleted attachment: ${fn}]</span>`;
          }
          if (issueKey) {
            const dlUrl = `/api/v1/issues/${encodeURIComponent(issueKey)}/attachments/${encodeURIComponent(attId)}/download`;
            return `<img class="md-inline-embed" src="${dlUrl}" alt="${alt}" data-att-id="${esc(attId)}" data-full-src="${dlUrl}" />`;
          }
        }
        if (tgt.toLowerCase().startsWith('embed:')) {
          const fn = tgt.slice('embed:'.length);
          if (stagedMap[fn]) {
            return `<img class="md-inline-embed" src="${stagedMap[fn]}" alt="${alt || esc(fn)}" />`;
          }
          return `<span class="badge">[Staged embed: ${esc(fn)}]</span>`;
        }
        const safeSrc = sanitizeHref(tgt);
        if (safeSrc === '#') return `[Image: ${alt}]`;
        return `<img class="md-inline-embed" src="${esc(safeSrc)}" alt="${alt}" data-full-src="${esc(safeSrc)}" />`;
      });

      // Links: [label](attachment://ATT-X) or [label](url)
      s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, rawTarget) => {
        const tgt = rawTarget.replace(/&amp;/g, '&').trim();
        if (tgt.toLowerCase().startsWith('attachment://')) {
          const attId = tgt.slice('attachment://'.length).trim().toUpperCase();
          if (deletedMap[attId]) {
            const fn = esc(deletedMap[attId].filename || attId);
            return `<span class="deleted-att-badge" data-att-id="${esc(attId)}">[Deleted attachment: ${fn}]</span>`;
          }
          if (issueKey) {
            const dlUrl = `/api/v1/issues/${encodeURIComponent(issueKey)}/attachments/${encodeURIComponent(attId)}/download`;
            return `<a href="${dlUrl}" class="md-att-link" data-att-id="${esc(attId)}" download>${label}</a>`;
          }
        }
        const safeHref = sanitizeHref(tgt);
        return `<a href="${esc(safeHref)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      });

      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
      s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');

      // #ITR-N clickable mentions
      s = s.replace(/(^|[\s(])#([A-Za-z][A-Za-z0-9]*-\d+)\b/g, (_, pre, ref) => {
        const up = ref.toUpperCase();
        return `${pre}<a href="#/issues/${encodeURIComponent(up)}" class="md-issue-mention" data-issue-ref="${esc(up)}">#${esc(up)}</a>`;
      });

      s = s.replace(/\x00CODE(\d+)\x00/g, (_, idx) => codeSpans[Number(idx)]);
      return s;
    }

    function renderMarkdown(rawText, issueCtx) {
      if (!rawText) return '<span style="color:var(--text-muted)">No content.</span>';
      // Step 1: Escape ALL raw HTML first to neutralize <script>, onerror=, etc.
      const escaped = esc(String(rawText).replace(/\r\n/g, '\n'));

      // Step 2: Extract fenced code blocks
      const codeBlocks = [];
      const withFences = escaped.replace(/```([A-Za-z0-9_-]*)\n([\s\S]*?)```/g, (_, lang, body) => {
        const idx = codeBlocks.length;
        codeBlocks.push(`<pre><code class="lang-${lang}">${body}</code></pre>`);
        return `\n@@FENCE_${idx}@@\n`;
      });

      const lines = withFences.split('\n');
      const out = [];
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];
        const trimmed = line.trim();
        if (!trimmed) { i++; continue; }

        const fenceMatch = trimmed.match(/^@@FENCE_(\d+)@@$/);
        if (fenceMatch) {
          out.push(codeBlocks[Number(fenceMatch[1])]);
          i++;
          continue;
        }

        const hMatch = line.match(/^(#{1,6})\s+(.+)$/);
        if (hMatch) {
          const lvl = hMatch[1].length;
          out.push(`<h${lvl} style="margin:8px 0 4px;">${renderInlineMarkdown(hMatch[2], issueCtx)}</h${lvl}>`);
          i++;
          continue;
        }

        if (/^---+$/.test(trimmed)) {
          out.push('<hr style="border:none;border-top:1px solid var(--border);margin:8px 0;" />');
          i++;
          continue;
        }

        if (trimmed.startsWith('&gt;')) {
          const qLines = [];
          while (i < lines.length && lines[i].trim().startsWith('&gt;')) {
            qLines.push(renderInlineMarkdown(lines[i].trim().replace(/^&gt;\s?/, ''), issueCtx));
            i++;
          }
          out.push(`<blockquote>${qLines.join('<br/>')}</blockquote>`);
          continue;
        }

        // Markdown Table
        if (trimmed.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
          const parseCells = (rowStr) => rowStr.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
          const headers = parseCells(lines[i]);
          i += 2;
          const rows = [];
          while (i < lines.length && lines[i].trim().includes('|')) {
            rows.push(parseCells(lines[i]));
            i++;
          }
          const thHtml = headers.map((h) => `<th>${renderInlineMarkdown(h, issueCtx)}</th>`).join('');
          const tbHtml = rows.map((r) => `<tr>${r.map((c) => `<td>${renderInlineMarkdown(c, issueCtx)}</td>`).join('')}</tr>`).join('');
          out.push(`<table><thead><tr>${thHtml}</tr></thead><tbody>${tbHtml}</tbody></table>`);
          continue;
        }

        // Lists & Task Checklists
        if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
          const ordered = /^\s*\d+\.\s+/.test(line);
          const tag = ordered ? 'ol' : 'ul';
          const items = [];
          while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
            const content = lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '');
            const chk = content.match(/^\[([xX ])\]\s+(.*)$/);
            if (chk) {
              const checked = chk[1].toLowerCase() === 'x' ? 'checked' : '';
              items.push(`<li class="md-task-item" style="list-style:none;"><label><input type="checkbox" class="md-task-checkbox" ${checked} /> ${renderInlineMarkdown(chk[2], issueCtx)}</label></li>`);
            } else {
              items.push(`<li>${renderInlineMarkdown(content, issueCtx)}</li>`);
            }
            i++;
          }
          out.push(`<${tag} style="margin:6px 0;padding-left:20px;">${items.join('')}</${tag}>`);
          continue;
        }

        out.push(`<p style="margin:5px 0;">${renderInlineMarkdown(line, issueCtx)}</p>`);
        i++;
      }
      return out.join('\n');
    }

    // Delegate clicks on inline images & #ITR-N mentions anywhere in document
    document.addEventListener('click', (e) => {
      const img = e.target.closest('.md-inline-embed');
      if (img && img.getAttribute('src')) {
        e.preventDefault();
        openImageLightbox(img.getAttribute('src'), img.getAttribute('alt') || 'Embedded Image');
        return;
      }
      const mention = e.target.closest('.md-issue-mention');
      if (mention && mention.dataset.issueRef) {
        e.preventDefault();
        openIssueDetail(mention.dataset.issueRef, { fullPage: false });
      }
    });

    // ----------------------------------------------------------------------
    // Modals & Theme Management
    // ----------------------------------------------------------------------
    function openModal(name) {
      window.__itrackTelemetry.modals.add(name);
      const el = document.querySelector(`[data-modal="${name}"]`);
      if (el) el.classList.add('open');
    }

    function closeModal(name) {
      const el = document.querySelector(`[data-modal="${name}"]`);
      if (el) el.classList.remove('open');
    }

    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
      btn.addEventListener('click', () => closeModal(btn.dataset.closeModal));
    });
    document.querySelectorAll('.modal-backdrop').forEach((bd) => {
      bd.addEventListener('click', (e) => {
        if (e.target === bd) bd.classList.remove('open');
      });
    });

    function openImageLightbox(src, title) {
      $('lightbox-img').src = src;
      $('lightbox-title').textContent = title || 'Image Preview';
      $('lightbox-download-link').href = src;
      openModal('image-lightbox');
    }

    function applyTheme(theme) {
      const t = theme === 'light' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', t);
      localStorage.setItem('itrack_theme', t);
      $('btn-theme-toggle').textContent = t === 'dark' ? 'Light Mode' : 'Dark Mode';
    }

    function initTheme() {
      const saved = localStorage.getItem('itrack_theme') || 'dark';
      applyTheme(saved);
    }

    $('btn-theme-toggle').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });

    $('btn-toggle-sidebar').addEventListener('click', () => {
      $('sidebar').classList.toggle('collapsed');
    });

    $('btn-reindex').addEventListener('click', async () => {
      try {
        const res = await api('/api/v1/reindex', { method: 'POST' });
        toast(`Reindexed ${res.indexed_issues} issues (${res.duration_ms} ms)`);
        await refreshMetadataAndCurrentView();
      } catch (err) { toast(err.message, true); }
    });

    // ----------------------------------------------------------------------
    // Metadata Sync & Navigation Views (All 8 Views + Permalink Routing)
    // ----------------------------------------------------------------------
    async function loadReferenceData() {
      const [cfg, tagsRes, spRes, msRes, vwRes] = await Promise.all([
        api('/api/v1/config'),
        api('/api/v1/tags'),
        api('/api/v1/sprints'),
        api('/api/v1/milestones'),
        api('/api/v1/views'),
      ]);
      appState.config = cfg;
      appState.tags = tagsRes.tags || [];
      appState.sprints = spRes.sprints || [];
      appState.milestones = msRes.milestones || [];
      appState.views = vwRes.views || [];

      $('brand-title').textContent = cfg.project_title || 'iTrack';

      const spOpts = appState.sprints.map((s) => ({ value: s.id, label: `${s.id} — ${s.name}` }));
      const msOpts = appState.milestones.map((m) => ({ value: m.id, label: `${m.id} — ${m.name}` }));
      const tagOpts = appState.tags.map((t) => ({ value: t.name, label: t.name }));

      populateSelect($('filter-category'), CATEGORIES, 'All Categories');
      populateSelect($('filter-state'), STATES, 'All States');
      populateSelect($('filter-priority'), PRIORITIES, 'All Priorities');
      populateSelect($('filter-severity'), SEVERITIES, 'All Severities');
      populateSelect($('filter-tag'), tagOpts, 'All Tags');
      populateSelect($('filter-sprint'), spOpts, 'All Sprints');
      populateSelect($('filter-milestone'), msOpts, 'All Milestones');

      populateSelect($('bulk-state'), STATES, 'Set State...');
      populateSelect($('bulk-priority'), PRIORITIES, 'Set Priority...');
      populateSelect($('bulk-sprint'), [{ value: 'none', label: '(Clear Sprint)' }, ...spOpts], 'Assign Sprint...');

      populateSelect($('board-filter-category'), CATEGORIES, 'All Categories');
      populateSelect($('board-filter-priority'), PRIORITIES, 'All Priorities');
      populateSelect($('board-filter-sprint'), spOpts, 'All Sprints');

      populateSelect($('create-category'), CATEGORIES);
      populateSelect($('create-state'), STATES);
      populateSelect($('create-priority'), PRIORITIES);
      populateSelect($('create-severity'), SEVERITIES);
      populateSelect($('create-sprint'), spOpts, '(None)');
      populateSelect($('create-milestone'), msOpts, '(None)');

      populateSelect($('detail-category'), CATEGORIES);
      populateSelect($('detail-state'), STATES);
      populateSelect($('detail-priority'), PRIORITIES);
      populateSelect($('detail-severity'), SEVERITIES);
      populateSelect($('detail-resolution'), RESOLUTIONS, '(Unresolved)');
      populateSelect($('detail-sprint'), spOpts, '(No Sprint)');
      populateSelect($('detail-milestone'), msOpts, '(No Milestone)');

      populateSelect($('cfg-default-category'), CATEGORIES);
      populateSelect($('cfg-default-state'), STATES);
      populateSelect($('cfg-default-priority'), PRIORITIES);
      populateSelect($('cfg-default-severity'), SEVERITIES);
      populateSelect($('save-view-tab'), PRESET_TABS);

      renderSidebarViews();
    }

    function renderSidebarViews() {
      const box = $('sidebar-saved-views');
      if (!appState.views.length) {
        box.innerHTML = '<div style="padding:4px 10px;color:var(--text-muted);font-size:12px;">No saved views</div>';
        return;
      }
      box.innerHTML = appState.views.map((v) => `
        <button class="nav-item sidebar-view-btn" data-view-id="${esc(v.id)}" title="${esc(v.query)}">
          <span>&#9733;</span><span class="nav-label">${esc(v.name)}</span>
        </button>
      `).join('');
      box.querySelectorAll('.sidebar-view-btn').forEach((b) => {
        b.addEventListener('click', () => executeSavedView(b.dataset.viewId));
      });
    }

    async function switchView(viewName, updateHash) {
      if (updateHash === undefined) updateHash = true;
      appState.currentView = viewName;
      appState.permalinkMode = false;
      $('issue-drawer').style.inset = '';
      $('issue-drawer').style.width = '';
      if ($('view-issue-permalink').classList.contains('active')) {
        $('issue-drawer').classList.remove('open');
      }

      window.__itrackTelemetry.views.add(viewName);
      document.querySelectorAll('.nav-item[data-view]').forEach((b) => {
        b.classList.toggle('active', b.dataset.view === viewName);
      });
      document.querySelectorAll('.view-pane').forEach((p) => {
        p.classList.toggle('active', p.id === `view-${viewName}`);
      });

      if (updateHash) {
        const targetHash = `#/views/${viewName}`;
        if (location.hash !== targetHash) {
          appState.suppressHashHandler = true;
          location.hash = targetHash;
          setTimeout(() => { appState.suppressHashHandler = false; }, 0);
        }
      }
      await renderActiveView();
    }

    async function renderActiveView() {
      const v = appState.currentView;
      if (v === 'issues') await loadIssuesList();
      else if (v === 'board') await loadKanbanBoard();
      else if (v === 'sprints') await renderSprintsView();
      else if (v === 'milestones') await renderMilestonesView();
      else if (v === 'views') await renderSavedViewsView();
      else if (v === 'analytics') await renderAnalyticsView();
      else if (v === 'tags') await renderTagsView();
      else if (v === 'settings') renderSettingsView();
    }

    async function refreshMetadataAndCurrentView() {
      await loadReferenceData();
      if (appState.permalinkMode && appState.activeIssue) {
        await openIssueDetail(appState.activeIssue.key, { fullPage: true, skipHash: true });
      } else {
        await renderActiveView();
        if (appState.activeIssue && $('issue-drawer').classList.contains('open')) {
          await openIssueDetail(appState.activeIssue.key, { fullPage: false, skipHash: true });
        }
      }
    }

    document.querySelectorAll('.nav-item[data-view]').forEach((btn) => {
      btn.addEventListener('click', () => switchView(btn.dataset.view, true));
    });

    // ----------------------------------------------------------------------
    // View 1: Issues List, Filters, Search, Sorting, Pagination & Bulk Bar
    // ----------------------------------------------------------------------
    let issuesListLoadSeq = 0;
    async function loadIssuesList() {
      const seq = ++issuesListLoadSeq;
      const params = new URLSearchParams();
      params.set('tab', appState.tab);
      params.set('page', String(appState.page));
      params.set('page_size', String(appState.pageSize));
      params.set('sort', appState.sort);
      params.set('order', appState.order);

      const q = $('global-search-input').value.trim();
      if (q) params.set('q', q);

      const fMap = [
        ['category', $('filter-category').value],
        ['state', $('filter-state').value],
        ['priority', $('filter-priority').value],
        ['severity', $('filter-severity').value],
        ['assignee', $('filter-assignee').value.trim()],
        ['tag', $('filter-tag').value],
        ['sprint', $('filter-sprint').value],
        ['milestone', $('filter-milestone').value],
      ];
      fMap.forEach(([k, val]) => { if (val) params.set(k, val); });
      if ($('filter-overdue').checked) params.set('overdue', 'true');

      try {
        const res = await api(`/api/v1/issues?${params.toString()}`);
        if (seq !== issuesListLoadSeq) return;
        renderIssuesTable(res);
      } catch (err) {
        toast(err.message, true);
      }
    }

    function renderIssuesTable(res) {
      const issues = res.issues || [];
      const tbody = $('issues-tbody');
      $('issues-empty-state').style.display = issues.length === 0 ? 'block' : 'none';
      tbody.innerHTML = issues.map((iss) => {
        const checked = appState.selectedKeys.has(iss.key) ? 'checked' : '';
        const badges = [];
        if (iss.is_blocked) badges.push('<span class="badge badge-blocked">blocked</span>');
        if (iss.is_overdue) badges.push('<span class="badge badge-overdue">overdue</span>');
        (iss.tags || []).forEach((tg) => badges.push(`<span class="badge">${esc(tg)}</span>`));
        return `
          <tr class="issue-row" data-key="${esc(iss.key)}">
            <td><input type="checkbox" class="issue-row-check" data-key="${esc(iss.key)}" ${checked} /></td>
            <td class="mono"><a href="#/issues/${encodeURIComponent(iss.key)}" class="issue-open-link" data-key="${esc(iss.key)}">${esc(iss.key)}</a></td>
            <td>
              <a href="#/issues/${encodeURIComponent(iss.key)}" class="issue-open-link" data-key="${esc(iss.key)}" style="color:var(--text-main);font-weight:600;">${esc(iss.name)}</a>
              ${badges.length ? `<div style="display:inline-flex;gap:4px;margin-left:6px;">${badges.join('')}</div>` : ''}
            </td>
            <td><span class="badge">${esc(iss.category)}</span></td>
            <td><span class="badge ${iss.state === 'done' ? 'badge-done' : ''}">${esc(iss.state)}</span></td>
            <td class="prio-${esc(iss.priority)}">${esc(iss.priority)}</td>
            <td>${esc(iss.severity)}</td>
            <td>${esc(iss.assignee || '—')}</td>
            <td class="mono">${iss.story_points !== null && iss.story_points !== undefined ? esc(iss.story_points) : '—'}</td>
            <td class="mono">${esc(iss.due_date || '—')}</td>
            <td class="mono" style="font-size:11px;color:var(--text-muted)">${esc((iss.updated_at || '').slice(0, 10))}</td>
          </tr>
        `;
      }).join('');

      tbody.querySelectorAll('.issue-open-link').forEach((a) => {
        a.addEventListener('click', (e) => {
          e.preventDefault();
          openIssueDetail(a.dataset.key, { fullPage: false });
        });
      });

      tbody.querySelectorAll('.issue-row-check').forEach((cb) => {
        cb.addEventListener('change', () => {
          if (cb.checked) appState.selectedKeys.add(cb.dataset.key);
          else appState.selectedKeys.delete(cb.dataset.key);
          updateBulkBar();
        });
      });

      $('page-summary').textContent = `${issues.length} of ${res.total} (Page ${res.page}/${res.total_pages})`;
      $('btn-page-first').disabled = !res.has_prev;
      $('btn-page-prev').disabled = !res.has_prev;
      $('btn-page-next').disabled = !res.has_next;
      $('btn-page-last').disabled = !res.has_next;
      $('btn-page-last').dataset.totalPages = String(res.total_pages);

      const pBox = $('pagination-pages');
      const pagesHtml = [];
      const maxP = Math.min(res.total_pages, 7);
      for (let p = 1; p <= maxP; p++) {
        pagesHtml.push(`<button class="btn btn-sm page-num-btn ${p === res.page ? 'btn-primary' : ''}" data-page="${p}">${p}</button>`);
      }
      pBox.innerHTML = pagesHtml.join('');
      pBox.querySelectorAll('.page-num-btn').forEach((b) => {
        b.addEventListener('click', () => { appState.page = Number(b.dataset.page); loadIssuesList(); });
      });
      updateBulkBar();
    }

    function updateBulkBar() {
      const cnt = appState.selectedKeys.size;
      $('bulk-selected-count').textContent = String(cnt);
      $('bulk-action-bar').classList.toggle('open', cnt > 0);
    }

    $('check-all-issues').addEventListener('change', (e) => {
      const checked = e.target.checked;
      document.querySelectorAll('.issue-row-check').forEach((cb) => {
        cb.checked = checked;
        if (checked) appState.selectedKeys.add(cb.dataset.key);
        else appState.selectedKeys.delete(cb.dataset.key);
      });
      updateBulkBar();
    });

    $('issues-preset-tabs').querySelectorAll('.tab-chip').forEach((btn) => {
      btn.addEventListener('click', () => {
        appState.tab = btn.dataset.tab;
        appState.page = 1;
        $('issues-preset-tabs').querySelectorAll('.tab-chip').forEach((b) => b.classList.toggle('active', b === btn));
        loadIssuesList();
      });
    });

    ['filter-category','filter-state','filter-priority','filter-severity','filter-tag','filter-sprint','filter-milestone','filter-overdue'].forEach((id) => {
      $(id).addEventListener('change', () => { appState.page = 1; loadIssuesList(); });
    });
    $('filter-assignee').addEventListener('change', () => { appState.page = 1; loadIssuesList(); });

    function clearAllListFilters() {
      $('global-search-input').value = '';
      ['filter-category','filter-state','filter-priority','filter-severity','filter-assignee','filter-tag','filter-sprint','filter-milestone'].forEach((id) => { $(id).value = ''; });
      $('filter-overdue').checked = false;
      appState.tab = 'all';
      appState.page = 1;
      $('issues-preset-tabs').querySelectorAll('.tab-chip').forEach((b) => b.classList.toggle('active', b.dataset.tab === 'all'));
      loadIssuesList();
    }
    $('btn-clear-filters').addEventListener('click', clearAllListFilters);
    $('btn-empty-clear-filters').addEventListener('click', clearAllListFilters);

    $('btn-run-search').addEventListener('click', async () => {
      const q = $('global-search-input').value.trim();
      appState.page = 1;
      if (appState.currentView !== 'issues') await switchView('issues');
      if (q) {
        try {
          const res = await api(`/api/v1/search?q=${encodeURIComponent(q)}&page=1&page_size=${appState.pageSize}&sort=${appState.sort}&order=${appState.order}`);
          renderIssuesTable(res);
        } catch (err) { toast(err.message, true); }
      } else {
        await loadIssuesList();
      }
    });
    $('global-search-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('btn-run-search').click();
    });

    document.querySelectorAll('table.issues-table th[data-sort]').forEach((th) => {
      th.addEventListener('click', () => {
        const sf = th.dataset.sort;
        if (appState.sort === sf) appState.order = appState.order === 'asc' ? 'desc' : 'asc';
        else { appState.sort = sf; appState.order = 'asc'; }
        loadIssuesList();
      });
    });

    $('page-size-select').addEventListener('change', (e) => {
      appState.pageSize = Number(e.target.value);
      appState.page = 1;
      loadIssuesList();
    });
    $('btn-page-first').addEventListener('click', () => { appState.page = 1; loadIssuesList(); });
    $('btn-page-prev').addEventListener('click', () => { if (appState.page > 1) { appState.page--; loadIssuesList(); } });
    $('btn-page-next').addEventListener('click', () => { appState.page++; loadIssuesList(); });
    $('btn-page-last').addEventListener('click', (e) => {
      appState.page = Number(e.currentTarget.dataset.totalPages || 1);
      loadIssuesList();
    });

    $('btn-bulk-apply').addEventListener('click', async () => {
      const keys = Array.from(appState.selectedKeys);
      if (!keys.length) return;
      const updates = {};
      if ($('bulk-state').value) updates.state = $('bulk-state').value;
      if ($('bulk-priority').value) updates.priority = $('bulk-priority').value;
      if ($('bulk-assignee').value.trim()) updates.assignee = $('bulk-assignee').value.trim();
      if ($('bulk-tag').value.trim()) updates.add_tags = [$('bulk-tag').value.trim()];
      if ($('bulk-sprint').value) {
        if ($('bulk-sprint').value === 'none') updates.clear_sprint = true;
        else updates.sprint = $('bulk-sprint').value;
      }
      if (!Object.keys(updates).length) return toast('Select at least one bulk field update', true);
      try {
        await api('/api/v1/bulk/update', { method: 'POST', body: { issues: keys, updates } });
        appState.selectedKeys.clear();
        $('check-all-issues').checked = false;
        $('bulk-state').value = ''; $('bulk-priority').value = ''; $('bulk-assignee').value = ''; $('bulk-tag').value = ''; $('bulk-sprint').value = '';
        toast(`Updated ${keys.length} issues`);
        await refreshMetadataAndCurrentView();
      } catch (err) { toast(err.message, true); }
    });

    $('btn-bulk-archive').addEventListener('click', async () => {
      const keys = Array.from(appState.selectedKeys);
      if (!keys.length) return;
      try {
        await api('/api/v1/bulk/update', { method: 'POST', body: { issues: keys, updates: { archive: true } } });
        appState.selectedKeys.clear();
        toast(`Archived ${keys.length} issues`);
        await loadIssuesList();
      } catch (err) { toast(err.message, true); }
    });

    $('btn-bulk-delete').addEventListener('click', async () => {
      const keys = Array.from(appState.selectedKeys);
      if (!keys.length) return;
      try {
        await api('/api/v1/bulk/delete', { method: 'POST', body: { issues: keys } });
        appState.selectedKeys.clear();
        $('check-all-issues').checked = false;
        toast(`Deleted ${keys.length} issues`);
        await refreshMetadataAndCurrentView();
      } catch (err) { toast(err.message, true); }
    });

    // ----------------------------------------------------------------------
    // View 2: Kanban Board (5 Swimlanes + Real Card Drag-and-Drop)
    // ----------------------------------------------------------------------
    let boardLoadSeq = 0;
    async function loadKanbanBoard() {
      const seq = ++boardLoadSeq;
      const params = new URLSearchParams();
      params.set('swimlane', $('board-swimlane-select').value || 'none');
      if ($('board-filter-category').value) params.set('category', $('board-filter-category').value);
      if ($('board-filter-priority').value) params.set('priority', $('board-filter-priority').value);
      if ($('board-filter-assignee').value.trim()) params.set('assignee', $('board-filter-assignee').value.trim());
      if ($('board-filter-sprint').value) params.set('sprint', $('board-filter-sprint').value);
      if ($('board-filter-q').value.trim()) params.set('q', $('board-filter-q').value.trim());

      try {
        const data = await api(`/api/v1/board?${params.toString()}`);
        if (seq !== boardLoadSeq) return;
        const container = $('board-lanes-container');
        container.innerHTML = (data.lanes || []).map((lane) => `
          <div class="swimlane-block" data-lane="${esc(lane.key)}">
            ${data.swimlane !== 'none' ? `<div class="swimlane-header"><span>${esc(lane.label)}</span><span class="badge">${lane.count}</span></div>` : ''}
            <div class="kanban-row">
              ${lane.columns.map((col) => {
                const wipBadge = col.wip_limit ? `${col.count}/${col.wip_limit}` : `${col.count}`;
                return `
                  <div class="kanban-column" data-state="${esc(col.state)}">
                    <div class="kanban-col-head ${col.wip_exceeded ? 'wip-exceeded' : ''}">
                      <span>${esc(col.state)}</span>
                      <span class="badge ${col.wip_exceeded ? 'badge-overdue' : ''}">${wipBadge}</span>
                    </div>
                    <div class="kanban-cards" data-drop-state="${esc(col.state)}">
                      ${col.issues.map((iss) => `
                        <div class="kanban-card" draggable="true" data-key="${esc(iss.key)}">
                          <div style="display:flex;justify-content:space-between;align-items:center;">
                            <span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(iss.key)}</span>
                            <span class="prio-${esc(iss.priority)}" style="font-size:11px">${esc(iss.priority)}</span>
                          </div>
                          <div style="font-weight:600;">${esc(iss.name)}</div>
                          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);">
                            <span>${esc(iss.category)}</span>
                            <span>${esc(iss.assignee || 'unassigned')}</span>
                          </div>
                        </div>
                      `).join('')}
                    </div>
                  </div>
                `;
              }).join('')}
            </div>
          </div>
        `).join('');

        let draggedIssueKey = null;
        let pointerDrag = null;
        let moveInFlight = false;

        async function moveCardToState(key, targetState) {
          if (!key || !targetState || moveInFlight) return;
          moveInFlight = true;
          try {
            await api(`/api/v1/issues/${encodeURIComponent(key)}`, { method: 'PATCH', body: { state: targetState } });
            toast(`Moved ${key} -> ${targetState}`);
            await loadKanbanBoard();
          } catch (err) {
            toast(err.message, true);
          } finally {
            moveInFlight = false;
          }
        }

        container.querySelectorAll('.kanban-card').forEach((card) => {
          card.addEventListener('dragstart', (e) => {
            draggedIssueKey = card.dataset.key;
            pointerDrag = null;
            window.removeEventListener('mousemove', onWinMove);
            window.removeEventListener('mouseup', onWinUp);
            if (e.dataTransfer) {
              e.dataTransfer.setData('text/plain', card.dataset.key);
              e.dataTransfer.effectAllowed = 'move';
            }
          });
          card.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            const srcCol = card.closest('.kanban-column');
            pointerDrag = {
              key: card.dataset.key,
              fromState: srcCol ? srcCol.dataset.state : null,
              startX: e.clientX,
              startY: e.clientY,
              moved: false,
            };
            window.addEventListener('mousemove', onWinMove);
            window.addEventListener('mouseup', onWinUp);
          });
          card.addEventListener('click', () => {
            if (pointerDrag && pointerDrag.moved) return;
            openIssueDetail(card.dataset.key, { fullPage: false });
          });
        });

        function onWinMove(e) {
          if (!pointerDrag) return;
          if (Math.hypot(e.clientX - pointerDrag.startX, e.clientY - pointerDrag.startY) > 8) {
            pointerDrag.moved = true;
          }
        }
        function onWinUp(e) {
          window.removeEventListener('mousemove', onWinMove);
          window.removeEventListener('mouseup', onWinUp);
          if (!pointerDrag) return;
          const pd = pointerDrag;
          setTimeout(() => { pointerDrag = null; }, 50);
          if (!pd.moved) return;
          const el = document.elementFromPoint(e.clientX, e.clientY);
          const colEl = el ? el.closest('.kanban-column') : null;
          if (colEl && colEl.dataset.state && colEl.dataset.state !== pd.fromState) {
            moveCardToState(pd.key, colEl.dataset.state);
          }
        }

        container.querySelectorAll('.kanban-column').forEach((colEl) => {
          colEl.addEventListener('dragover', (e) => {
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
            colEl.classList.add('drag-over');
          });
          colEl.addEventListener('dragleave', () => colEl.classList.remove('drag-over'));
          colEl.addEventListener('drop', async (e) => {
            e.preventDefault();
            colEl.classList.remove('drag-over');
            const key = (e.dataTransfer && e.dataTransfer.getData('text/plain')) || draggedIssueKey;
            draggedIssueKey = null;
            pointerDrag = null;
            await moveCardToState(key, colEl.dataset.state);
          });
        });
      } catch (err) { toast(err.message, true); }
    }

    ['board-swimlane-select','board-filter-category','board-filter-priority','board-filter-sprint'].forEach((id) => {
      $(id).addEventListener('change', loadKanbanBoard);
    });
    $('btn-refresh-board').addEventListener('click', loadKanbanBoard);

    // ----------------------------------------------------------------------
    // Views 3..8: Sprints, Milestones, Saved Views, Analytics, Tags, Settings
    // ----------------------------------------------------------------------
    async function renderSprintsView() {
      await loadReferenceData();
      const details = await Promise.all(appState.sprints.map((s) => api(`/api/v1/sprints/${encodeURIComponent(s.id)}`)));
      const box = $('sprints-list-container');
      if (!details.length) {
        box.innerHTML = '<div class="card-panel">No sprints created yet.</div>';
        return;
      }
      box.innerHTML = details.map((sp) => `
        <div class="card-panel sprint-card" data-sprint-id="${esc(sp.id)}">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
            <div>
              <strong style="font-size:15px">${esc(sp.id)} — ${esc(sp.name)}</strong>
              <span class="badge" style="margin-left:8px">${esc(sp.state)}</span>
              <div style="color:var(--text-muted);font-size:12px;margin-top:3px;">Goal: ${esc(sp.goal || '—')} | ${esc(sp.start_date || '?')} &rarr; ${esc(sp.end_date || '?')} | ${sp.done_issues}/${sp.total_issues} done (${sp.progress_pct}%)</div>
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap;">
              ${sp.state === 'planned' ? `<button class="btn btn-sm btn-primary btn-sprint-start" data-id="${esc(sp.id)}">Start Sprint</button>` : ''}
              ${sp.state !== 'completed' ? `<button class="btn btn-sm btn-primary btn-sprint-complete" data-id="${esc(sp.id)}" data-inc="${sp.incomplete_issues}">Complete Sprint</button>` : ''}
              <button class="btn btn-sm btn-sprint-edit" data-id="${esc(sp.id)}" data-goal="${esc(sp.goal || '')}">Edit Goal</button>
              <button class="btn btn-sm btn-danger btn-sprint-delete" data-id="${esc(sp.id)}">Delete</button>
            </div>
          </div>
          <div style="display:flex;gap:6px;margin-top:10px;">
            <input type="text" class="sprint-assign-input" data-id="${esc(sp.id)}" placeholder="Assign issue (ITR-1 or Exact Name)..." style="width:240px" />
            <button class="btn btn-sm btn-sprint-assign" data-id="${esc(sp.id)}">Assign to Sprint</button>
          </div>
          <div style="margin-top:8px;display:flex;flex-direction:column;gap:4px;">
            ${(sp.issues || []).map((iss) => `
              <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;background:var(--bg-card);border-radius:5px;">
                <span><strong class="mono">${esc(iss.key)}</strong> ${esc(iss.name)} <span class="badge">${esc(iss.state)}</span></span>
                <button class="btn btn-sm btn-ghost btn-sprint-remove-issue" data-sprint="${esc(sp.id)}" data-issue="${esc(iss.key)}">Remove</button>
              </div>
            `).join('')}
          </div>
        </div>
      `).join('');

      box.querySelectorAll('.btn-sprint-start').forEach((b) => {
        b.addEventListener('click', async () => {
          try {
            await api(`/api/v1/sprints/${encodeURIComponent(b.dataset.id)}/start`, { method: 'POST', body: {} });
            toast(`Started ${b.dataset.id}`);
            await renderSprintsView();
          } catch (err) { toast(err.message, true); }
        });
      });
      box.querySelectorAll('.btn-sprint-complete').forEach((b) => {
        b.addEventListener('click', () => {
          appState.pendingRolloverSprintId = b.dataset.id;
          $('rollover-modal-info').textContent = `Completing ${b.dataset.id} (${b.dataset.inc} incomplete issues). Choose destination for incomplete issues:`;
          const others = appState.sprints.filter((s) => s.id !== b.dataset.id && s.state !== 'completed').map((s) => ({ value: s.id, label: `${s.id} — ${s.name}` }));
          populateSelect($('rollover-target-select'), others, 'Backlog (No Sprint)');
          openModal('sprint-rollover');
        });
      });
      box.querySelectorAll('.btn-sprint-edit').forEach((b) => {
        b.addEventListener('click', async () => {
          const card = b.closest('.sprint-card');
          const nextGoal = card.dataset.editedGoal || (b.dataset.goal + ' (updated)');
          await api(`/api/v1/sprints/${encodeURIComponent(b.dataset.id)}`, { method: 'PATCH', body: { goal: nextGoal } });
          toast('Updated sprint');
          await renderSprintsView();
        });
      });
      box.querySelectorAll('.btn-sprint-assign').forEach((b) => {
        b.addEventListener('click', async () => {
          const inp = box.querySelector(`.sprint-assign-input[data-id="${b.dataset.id}"]`);
          const val = inp.value.trim();
          if (!val) return;
          try {
            await api(`/api/v1/sprints/${encodeURIComponent(b.dataset.id)}/assign`, { method: 'POST', body: { issues: [val] } });
            inp.value = '';
            await renderSprintsView();
          } catch (err) { toast(err.message, true); }
        });
      });
      box.querySelectorAll('.btn-sprint-remove-issue').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/sprints/${encodeURIComponent(b.dataset.sprint)}/remove`, { method: 'POST', body: { issues: [b.dataset.issue] } });
          await renderSprintsView();
        });
      });
      box.querySelectorAll('.btn-sprint-delete').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/sprints/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          toast(`Deleted ${b.dataset.id}`);
          await renderSprintsView();
        });
      });
    }

    $('btn-create-sprint').addEventListener('click', async () => {
      try {
        await api('/api/v1/sprints', {
          method: 'POST',
          body: {
            name: $('sprint-create-name').value.trim(),
            goal: $('sprint-create-goal').value.trim(),
            start_date: $('sprint-create-start').value || null,
            end_date: $('sprint-create-end').value || null,
          },
        });
        $('sprint-create-name').value = ''; $('sprint-create-goal').value = '';
        toast('Sprint created');
        await renderSprintsView();
      } catch (err) { toast(err.message, true); }
    });

    $('btn-confirm-sprint-complete').addEventListener('click', async () => {
      const sid = appState.pendingRolloverSprintId;
      if (!sid) return;
      try {
        const rollover_to = $('rollover-target-select').value || null;
        await api(`/api/v1/sprints/${encodeURIComponent(sid)}/complete`, { method: 'POST', body: { rollover_to } });
        closeModal('sprint-rollover');
        toast(`Completed ${sid}`);
        await renderSprintsView();
      } catch (err) { toast(err.message, true); }
    });

    async function renderMilestonesView() {
      await loadReferenceData();
      const details = await Promise.all(appState.milestones.map((m) => api(`/api/v1/milestones/${encodeURIComponent(m.id)}`)));
      const box = $('milestones-list-container');
      if (!details.length) {
        box.innerHTML = '<div class="card-panel">No milestones yet.</div>';
        return;
      }
      box.innerHTML = details.map((m) => `
        <div class="card-panel" data-milestone-id="${esc(m.id)}">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong>${esc(m.id)} — ${esc(m.name)}</strong>
            <span class="badge">${esc(m.state)}</span>
          </div>
          <p style="color:var(--text-muted);margin:6px 0;">${esc(m.description || 'No description')} | Due: ${esc(m.due_date || '—')}</p>
          <div class="progress-track" style="margin:6px 0;"><div class="progress-fill" style="width:${m.progress_pct}%"></div></div>
          <div style="font-size:12px;margin-bottom:8px;">${m.done_issues}/${m.total_issues} completed (${m.progress_pct}%)</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn btn-sm btn-ms-edit" data-id="${esc(m.id)}" data-desc="${esc(m.description || '')}">Edit</button>
            ${m.state === 'open'
              ? `<button class="btn btn-sm btn-ms-close" data-id="${esc(m.id)}">Close</button>`
              : `<button class="btn btn-sm btn-ms-reopen" data-id="${esc(m.id)}">Reopen</button>`}
            <button class="btn btn-sm btn-danger btn-ms-delete" data-id="${esc(m.id)}">Delete</button>
          </div>
        </div>
      `).join('');

      box.querySelectorAll('.btn-ms-edit').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/milestones/${encodeURIComponent(b.dataset.id)}`, { method: 'PATCH', body: { description: (b.dataset.desc || 'Milestone') + ' (updated)' } });
          await renderMilestonesView();
        });
      });
      box.querySelectorAll('.btn-ms-close').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/milestones/${encodeURIComponent(b.dataset.id)}/close`, { method: 'POST', body: {} });
          await renderMilestonesView();
        });
      });
      box.querySelectorAll('.btn-ms-reopen').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/milestones/${encodeURIComponent(b.dataset.id)}/reopen`, { method: 'POST', body: {} });
          await renderMilestonesView();
        });
      });
      box.querySelectorAll('.btn-ms-delete').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/milestones/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          await renderMilestonesView();
        });
      });
    }

    $('btn-create-milestone').addEventListener('click', async () => {
      try {
        await api('/api/v1/milestones', {
          method: 'POST',
          body: {
            name: $('ms-create-name').value.trim(),
            description: $('ms-create-desc').value.trim(),
            due_date: $('ms-create-due').value || null,
          },
        });
        $('ms-create-name').value = ''; $('ms-create-desc').value = '';
        toast('Milestone created');
        await renderMilestonesView();
      } catch (err) { toast(err.message, true); }
    });

    async function executeSavedView(viewId) {
      try {
        const detail = await api(`/api/v1/views/${encodeURIComponent(viewId)}`);
        const res = await api(`/api/v1/views/${encodeURIComponent(viewId)}/run`, { method: 'POST', body: { page: 1, page_size: appState.pageSize } });
        appState.tab = detail.tab || 'all';
        $('global-search-input').value = detail.query || '';
        await switchView('issues');
        renderIssuesTable(res);
        toast(`Loaded view: ${detail.name}`);
      } catch (err) { toast(err.message, true); }
    }

    async function renderSavedViewsView() {
      await loadReferenceData();
      const box = $('views-list-container');
      if (!appState.views.length) {
        box.innerHTML = '<div class="card-panel">No saved views yet.</div>';
        return;
      }
      box.innerHTML = appState.views.map((v) => `
        <div class="card-panel" data-saved-view="${esc(v.id)}">
          <strong>${esc(v.name)}</strong> <span class="badge mono">${esc(v.id)}</span>
          <div class="mono" style="margin:6px 0;font-size:12px;color:var(--accent);">${esc(v.query || '(all issues)')}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">Tab: ${esc(v.tab)} | ${esc(v.description || '')}</div>
          <div style="display:flex;gap:6px;">
            <button class="btn btn-sm btn-primary btn-view-run" data-id="${esc(v.id)}">Run View</button>
            <button class="btn btn-sm btn-view-edit" data-id="${esc(v.id)}" data-desc="${esc(v.description || '')}">Edit</button>
            <button class="btn btn-sm btn-danger btn-view-delete" data-id="${esc(v.id)}">Delete</button>
          </div>
        </div>
      `).join('');

      box.querySelectorAll('.btn-view-run').forEach((b) => b.addEventListener('click', () => executeSavedView(b.dataset.id)));
      box.querySelectorAll('.btn-view-edit').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/views/${encodeURIComponent(b.dataset.id)}`, { method: 'PATCH', body: { description: (b.dataset.desc || 'Saved view') + ' (updated)' } });
          await renderSavedViewsView();
        });
      });
      box.querySelectorAll('.btn-view-delete').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/views/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          await renderSavedViewsView();
        });
      });
    }

    function openSaveViewModal() {
      $('save-view-name').value = '';
      $('save-view-query').value = $('global-search-input').value.trim();
      $('save-view-tab').value = appState.tab || 'all';
      $('save-view-desc').value = '';
      openModal('save-view');
    }
    $('btn-save-current-view').addEventListener('click', openSaveViewModal);
    $('btn-views-open-save-modal').addEventListener('click', openSaveViewModal);

    $('btn-submit-save-view').addEventListener('click', async () => {
      try {
        await api('/api/v1/views', {
          method: 'POST',
          body: {
            name: $('save-view-name').value.trim(),
            query: $('save-view-query').value.trim(),
            tab: $('save-view-tab').value || 'all',
            sort: appState.sort,
            order: appState.order,
            description: $('save-view-desc').value.trim(),
          },
        });
        closeModal('save-view');
        toast('Saved view created');
        await loadReferenceData();
        if (appState.currentView === 'views') await renderSavedViewsView();
      } catch (err) { toast(err.message, true); }
    });

    async function renderAnalyticsView() {
      const [stats, statusInfo] = await Promise.all([api('/api/v1/stats'), api('/api/v1/status')]);
      $('analytics-summary-cards').innerHTML = `
        <div class="card-panel"><strong>Total Issues</strong><div style="font-size:26px;font-weight:700;" id="stat-total-issues">${stats.total_issues}</div><div style="color:var(--text-muted)">${esc(statusInfo.project_title)}</div></div>
        <div class="card-panel"><strong>Active / Open</strong><div style="font-size:26px;font-weight:700;" id="stat-active-issues">${stats.active_issues}</div></div>
        <div class="card-panel"><strong>Completed</strong><div style="font-size:26px;font-weight:700;color:var(--success)" id="stat-done-issues">${stats.done_issues} (${stats.completion_rate_pct}%)</div></div>
        <div class="card-panel"><strong>Blocked / Overdue</strong><div style="font-size:26px;font-weight:700;color:var(--warning)">${stats.blocked_issues} / ${stats.overdue_issues}</div></div>
        <div class="card-panel"><strong>Story Points</strong><div style="font-size:24px;font-weight:700;">${stats.completed_points} / ${stats.total_points}</div></div>
        <div class="card-panel"><strong>Logged Time</strong><div style="font-size:24px;font-weight:700;">${stats.total_logged_minutes}m / ${stats.total_estimate_minutes}m</div></div>
      `;
      const renderDist = (title, obj) => {
        const entries = Object.entries(obj || {});
        const maxVal = Math.max(1, ...entries.map(([, v]) => v));
        return `<div class="card-panel"><strong>${esc(title)}</strong><div style="margin-top:8px;display:flex;flex-direction:column;gap:5px;">
          ${entries.map(([k, v]) => `
            <div>
              <div style="display:flex;justify-content:space-between;font-size:12px;"><span>${esc(k)}</span><span class="mono">${v}</span></div>
              <div class="progress-track"><div class="progress-fill" style="width:${Math.round(v / maxVal * 100)}%"></div></div>
            </div>
          `).join('')}
        </div></div>`;
      };
      $('analytics-distributions').innerHTML =
        renderDist('By State', stats.by_state) +
        renderDist('By Priority', stats.by_priority) +
        renderDist('By Category', stats.by_category) +
        renderDist('By Assignee', stats.by_assignee);
    }

    async function renderTagsView() {
      await loadReferenceData();
      const box = $('tags-list-container');
      if (!appState.tags.length) {
        box.innerHTML = '<div class="card-panel">No tags created yet.</div>';
        return;
      }
      box.innerHTML = appState.tags.map((t) => `
        <div class="card-panel" data-tag-card="${esc(t.name)}">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span class="badge" style="background:${esc(t.color)};color:#fff;border:none;">${esc(t.name)}</span>
            <span class="mono" style="font-size:12px;">${t.issue_count || 0} issues</span>
          </div>
          <p style="color:var(--text-muted);margin:8px 0;">${esc(t.description || 'No description')}</p>
          <div style="display:flex;gap:6px;align-items:center;">
            <input type="color" class="tag-edit-color" data-name="${esc(t.name)}" value="${esc(t.color || '#3b82f6')}" style="width:36px;height:28px;padding:1px;" />
            <button class="btn btn-sm btn-tag-save" data-name="${esc(t.name)}">Save Color</button>
            <button class="btn btn-sm btn-danger btn-tag-delete" data-name="${esc(t.name)}">Delete</button>
          </div>
        </div>
      `).join('');

      box.querySelectorAll('.btn-tag-save').forEach((b) => {
        b.addEventListener('click', async () => {
          const cInp = box.querySelector(`.tag-edit-color[data-name="${b.dataset.name}"]`);
          await api(`/api/v1/tags/${encodeURIComponent(b.dataset.name)}`, { method: 'PATCH', body: { color: cInp.value } });
          toast(`Updated tag ${b.dataset.name}`);
          await renderTagsView();
        });
      });
      box.querySelectorAll('.btn-tag-delete').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/tags/${encodeURIComponent(b.dataset.name)}`, { method: 'DELETE' });
          toast(`Deleted tag ${b.dataset.name}`);
          await renderTagsView();
        });
      });
    }

    $('btn-create-tag').addEventListener('click', async () => {
      try {
        await api('/api/v1/tags', {
          method: 'POST',
          body: {
            name: $('tag-create-name').value.trim(),
            color: $('tag-create-color').value,
            description: $('tag-create-desc').value.trim(),
          },
        });
        $('tag-create-name').value = ''; $('tag-create-desc').value = '';
        toast('Tag created');
        await renderTagsView();
      } catch (err) { toast(err.message, true); }
    });

    function renderSettingsView() {
      const c = appState.config || {};
      $('cfg-project-title').value = c.project_title || '';
      $('cfg-project-prefix').value = c.project_prefix || 'ITR';
      $('cfg-default-category').value = c.default_category || 'task';
      $('cfg-default-state').value = c.default_state || 'backlog';
      $('cfg-default-priority').value = c.default_priority || 'medium';
      $('cfg-default-severity').value = c.default_severity || 'sev3';
      $('cfg-wip-in-progress').value = (c.wip_limits && c.wip_limits['in-progress']) ? c.wip_limits['in-progress'] : '';
      $('cfg-default-assignee').value = c.default_assignee || '';
    }

    $('btn-save-settings').addEventListener('click', async () => {
      try {
        const wipVal = $('cfg-wip-in-progress').value.trim();
        await api('/api/v1/config', {
          method: 'PATCH',
          body: {
            project_title: $('cfg-project-title').value.trim(),
            project_prefix: $('cfg-project-prefix').value.trim(),
            default_category: $('cfg-default-category').value,
            default_state: $('cfg-default-state').value,
            default_priority: $('cfg-default-priority').value,
            default_severity: $('cfg-default-severity').value,
            default_assignee: $('cfg-default-assignee').value.trim() || null,
            'wip_limits.in-progress': wipVal ? Number(wipVal) : 0,
          },
        });
        toast('Workspace settings saved');
        await loadReferenceData();
      } catch (err) { toast(err.message, true); }
    });
"""

SPA_HTML += r"""
    // ----------------------------------------------------------------------
    // Create Issue Modal (with inline embeds & drag-and-drop attachments)
    // ----------------------------------------------------------------------
    function openCreateModal() {
      const c = appState.config || {};
      $('create-name').value = '';
      $('create-category').value = c.default_category || 'task';
      $('create-state').value = c.default_state || 'backlog';
      $('create-priority').value = c.default_priority || 'medium';
      $('create-severity').value = c.default_severity || 'sev3';
      $('create-assignee').value = c.default_assignee || '';
      $('create-parent').value = '';
      $('create-due').value = '';
      $('create-sprint').value = '';
      $('create-milestone').value = '';
      $('create-points').value = '';
      $('create-estimate').value = '';
      $('create-tags').value = '';
      $('create-components').value = '';
      $('create-description').value = '';
      $('create-ac').value = '';
      $('create-custom-fields').value = '';
      appState.createEmbedFiles = [];
      appState.createAttachFiles = [];
      $('create-embeds-queued').textContent = '';
      $('create-attach-queued').textContent = '';
      openModal('create-issue');
      setTimeout(() => $('create-name').focus(), 20);
    }

    $('btn-open-create').addEventListener('click', openCreateModal);

    $('create-embed-input').addEventListener('change', (e) => {
      const files = Array.from(e.target.files || []);
      files.forEach((f) => {
        appState.createEmbedFiles.push(f);
        const ta = $('create-description');
        const tag = `![${f.name}](embed:${f.name})`;
        ta.value = ta.value ? `${ta.value}\n${tag}` : tag;
      });
      $('create-embeds-queued').textContent = `${appState.createEmbedFiles.length} embed(s) queued`;
      e.target.value = '';
    });

    function queueCreateAttachments(files) {
      Array.from(files || []).forEach((f) => appState.createAttachFiles.push(f));
      $('create-attach-queued').textContent = appState.createAttachFiles.map((f) => f.name).join(', ');
    }
    $('create-attach-input').addEventListener('change', (e) => {
      queueCreateAttachments(e.target.files);
      e.target.value = '';
    });
    $('create-attach-dropzone').addEventListener('dragover', (e) => { e.preventDefault(); $('create-attach-dropzone').classList.add('drag-over'); });
    $('create-attach-dropzone').addEventListener('dragleave', () => $('create-attach-dropzone').classList.remove('drag-over'));
    $('create-attach-dropzone').addEventListener('drop', (e) => {
      e.preventDefault();
      $('create-attach-dropzone').classList.remove('drag-over');
      if (e.dataTransfer && e.dataTransfer.files) queueCreateAttachments(e.dataTransfer.files);
    });

    $('btn-submit-create-issue').addEventListener('click', async () => {
      try {
        const cfObj = {};
        $('create-custom-fields').value.split('\n').map((s) => s.trim()).filter(Boolean).forEach((line) => {
          const eq = line.indexOf('=');
          if (eq > 0) cfObj[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
        });
        const acArr = $('create-ac').value.split('\n').map((s) => s.trim()).filter(Boolean);
        const payload = {
          name: $('create-name').value,
          category: $('create-category').value,
          state: $('create-state').value,
          priority: $('create-priority').value,
          severity: $('create-severity').value,
          description: $('create-description').value,
          acceptance_criteria: acArr,
          assignee: $('create-assignee').value.trim() || null,
          parent: $('create-parent').value.trim() || null,
          sprint: $('create-sprint').value || null,
          milestone: $('create-milestone').value || null,
          story_points: $('create-points').value !== '' ? Number($('create-points').value) : null,
          estimate_minutes: $('create-estimate').value !== '' ? Number($('create-estimate').value) : null,
          due_date: $('create-due').value || null,
          tags: $('create-tags').value.split(',').map((s) => s.trim()).filter(Boolean),
          components: $('create-components').value.split(',').map((s) => s.trim()).filter(Boolean),
          custom_fields: cfObj,
        };
        const created = await api('/api/v1/issues', { method: 'POST', body: payload });
        if (appState.createEmbedFiles.length) {
          const fd = new FormData();
          appState.createEmbedFiles.forEach((f) => fd.append('files', f, f.name));
          await api(`/api/v1/issues/${encodeURIComponent(created.key)}/embeds`, { method: 'POST', body: fd });
        }
        if (appState.createAttachFiles.length) {
          const fd = new FormData();
          appState.createAttachFiles.forEach((f) => fd.append('files', f, f.name));
          await api(`/api/v1/issues/${encodeURIComponent(created.key)}/attachments`, { method: 'POST', body: fd });
        }
        closeModal('create-issue');
        toast(`Created ${created.key}: ${created.name}`);
        await refreshMetadataAndCurrentView();
        await openIssueDetail(created.key, { fullPage: false });
      } catch (err) {
        toast(err.message, true);
      }
    });

    // ----------------------------------------------------------------------
    // Issue Detail (Slide-Over Drawer & Full-Page #/issues/<ref> Permalink)
    // ----------------------------------------------------------------------
    let issueDetailLoadSeq = 0;
    async function openIssueDetail(issueRef, opts) {
      const seq = ++issueDetailLoadSeq;
      const options = opts || {};
      try {
        const iss = await api(`/api/v1/issues/${encodeURIComponent(issueRef)}`);
        if (seq !== issueDetailLoadSeq) return;
        appState.activeIssue = iss;
        window.__itrackTelemetry.modals.add('issue-detail');

        const drawer = $('issue-drawer');
        if (options.fullPage) {
          appState.permalinkMode = true;
          document.querySelectorAll('.view-pane').forEach((p) => p.classList.toggle('active', p.id === 'view-issue-permalink'));
          drawer.style.inset = '52px 0 0 230px';
          drawer.style.width = 'auto';
          if (!options.skipHash) {
            appState.suppressHashHandler = true;
            location.hash = `#/issues/${encodeURIComponent(iss.key)}`;
            setTimeout(() => { appState.suppressHashHandler = false; }, 0);
          }
        } else {
          appState.permalinkMode = false;
          drawer.style.inset = '';
          drawer.style.width = '';
        }
        drawer.classList.add('open');

        renderIssueDetailDOM(iss);
      } catch (err) {
        toast(err.message, true);
      }
    }

    function closeIssueDetail() {
      $('issue-drawer').classList.remove('open');
      $('issue-drawer').style.inset = '';
      $('issue-drawer').style.width = '';
      appState.activeIssue = null;
      if (appState.permalinkMode) {
        appState.permalinkMode = false;
        switchView('issues', true);
      }
    }
    $('btn-close-drawer').addEventListener('click', closeIssueDetail);
    $('btn-permalink-back').addEventListener('click', () => switchView('issues', true));

    $('btn-open-permalink').addEventListener('click', () => {
      if (!appState.activeIssue) return;
      location.hash = `#/issues/${encodeURIComponent(appState.activeIssue.key)}`;
    });

    function renderIssueDetailDOM(iss) {
      $('detail-key-badge').textContent = iss.key;
      $('detail-title-input').value = iss.name;
      $('btn-archive-issue').style.display = iss.archived ? 'none' : 'inline-flex';
      $('btn-unarchive-issue').style.display = iss.archived ? 'inline-flex' : 'none';

      const banners = [];
      if (iss.archived) banners.push('<span class="badge badge-overdue">ARCHIVED</span>');
      if (iss.is_blocked) banners.push(`<span class="badge badge-blocked">BLOCKED${iss.unresolved_blockers.length ? ' by ' + esc(iss.unresolved_blockers.join(', ')) : ''}</span>`);
      if (iss.is_overdue) banners.push('<span class="badge badge-overdue">OVERDUE</span>');
      $('detail-status-banners').innerHTML = banners.join('');

      // Metadata Sidebar
      $('detail-category').value = iss.category;
      $('detail-state').value = iss.state;
      $('detail-priority').value = iss.priority;
      $('detail-severity').value = iss.severity;
      $('detail-resolution').value = iss.resolution || '';
      $('detail-assignee').value = iss.assignee || '';
      $('detail-reporter').value = iss.reporter || '';
      $('detail-parent').value = iss.parent || '';
      $('detail-sprint').value = iss.sprint || '';
      $('detail-milestone').value = iss.milestone || '';
      $('detail-points').value = iss.story_points !== null && iss.story_points !== undefined ? iss.story_points : '';
      $('detail-estimate').value = iss.estimate_minutes !== null && iss.estimate_minutes !== undefined ? iss.estimate_minutes : '';
      $('detail-due').value = iss.due_date || '';
      $('detail-overdue-badge').innerHTML = iss.is_overdue ? '<span class="badge badge-overdue">Overdue</span>' : '';
      $('detail-tags').value = (iss.tags || []).join(', ');
      $('detail-components').value = (iss.components || []).join(', ');

      // Custom Fields
      const cfEntries = Object.entries(iss.custom_fields || {});
      $('detail-cf-list').innerHTML = cfEntries.map(([k, v]) => `
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;background:var(--bg-input);padding:3px 6px;border-radius:4px;">
          <span><strong class="mono">${esc(k)}</strong> = <span class="mono">${esc(v)}</span></span>
          <button class="btn btn-sm btn-ghost btn-unset-cf" data-cf-key="${esc(k)}" style="padding:0 4px;">&#10005;</button>
        </div>
      `).join('');
      $('detail-cf-list').querySelectorAll('.btn-unset-cf').forEach((b) => {
        b.addEventListener('click', () => patchIssue({ unset_fields: [b.dataset.cfKey] }));
      });

      // Description & Description Embeds
      $('detail-desc-editor').style.display = 'none';
      $('detail-desc-rendered').style.display = 'block';
      $('detail-desc-rendered').innerHTML = renderMarkdown(iss.description || '', iss);
      $('detail-desc-textarea').value = iss.description || '';
      $('detail-desc-embeds-bar').innerHTML = (iss.description_embeds || []).map((emb) => `
        <span class="badge">
          ${esc(emb.filename)} (${esc(emb.id)})
          <button class="btn btn-sm btn-ghost btn-del-desc-embed" data-id="${esc(emb.id)}" title="Remove embed" style="padding:0 4px;">&#10005;</button>
        </span>
      `).join('');
      $('detail-desc-embeds-bar').querySelectorAll('.btn-del-desc-embed').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/embeds/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          toast(`Removed description embed ${b.dataset.id}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      // Acceptance Criteria
      $('detail-ac-summary').textContent = `${iss.ac_progress.checked}/${iss.ac_progress.total}`;
      $('detail-ac-progress').style.width = `${iss.ac_progress.pct}%`;
      $('detail-ac-list').innerHTML = (iss.acceptance_criteria || []).map((ac) => `
        <div class="ac-row" data-ac-id="${esc(ac.id)}" style="display:flex;align-items:center;gap:6px;background:var(--bg-card);padding:5px 8px;border-radius:5px;">
          <input type="checkbox" class="ac-check-toggle" data-id="${esc(ac.id)}" ${ac.checked ? 'checked' : ''} />
          <span style="flex:1;${ac.checked ? 'text-decoration:line-through;color:var(--text-muted);' : ''}">${esc(ac.text)}</span>
          <button class="btn btn-sm btn-ghost btn-edit-ac" data-id="${esc(ac.id)}" data-text="${esc(ac.text)}">Edit</button>
          <button class="btn btn-sm btn-ghost btn-del-ac" data-id="${esc(ac.id)}">&#10005;</button>
        </div>
      `).join('');
      $('detail-ac-list').querySelectorAll('.ac-check-toggle').forEach((cb) => {
        cb.addEventListener('change', () => {
          patchIssue(cb.checked ? { check_ac: [cb.dataset.id] } : { uncheck_ac: [cb.dataset.id] });
        });
      });
      $('detail-ac-list').querySelectorAll('.btn-edit-ac').forEach((b) => {
        b.addEventListener('click', () => {
          const row = b.closest('.ac-row');
          row.innerHTML = `
            <input type="text" class="ac-inline-edit-input" value="${esc(b.dataset.text)}" style="flex:1" />
            <button class="btn btn-sm btn-primary btn-save-ac-inline">Save</button>
          `;
          row.querySelector('.btn-save-ac-inline').addEventListener('click', () => {
            const val = row.querySelector('.ac-inline-edit-input').value.trim();
            if (val) patchIssue({ edit_ac: [{ target: b.dataset.id, text: val }] });
          });
        });
      });
      $('detail-ac-list').querySelectorAll('.btn-del-ac').forEach((b) => {
        b.addEventListener('click', () => patchIssue({ remove_ac: [b.dataset.id] }));
      });

      // Subtasks
      $('detail-subtask-summary').textContent = `${iss.subtask_progress.completed}/${iss.subtask_progress.total}`;
      $('detail-subtask-progress').style.width = `${iss.subtask_progress.pct}%`;
      $('detail-subtasks-list').innerHTML = (iss.subtasks || []).map((sub) => `
        <div style="display:flex;align-items:center;gap:6px;background:var(--bg-card);padding:5px 8px;border-radius:5px;">
          <input type="checkbox" class="subtask-toggle" data-key="${esc(sub.key)}" ${sub.completed ? 'checked' : ''} />
          <a href="#/issues/${encodeURIComponent(sub.key)}" class="subtask-open mono" data-key="${esc(sub.key)}">${esc(sub.key)}</a>
          <span style="flex:1;${sub.completed ? 'text-decoration:line-through;color:var(--text-muted);' : ''}">${esc(sub.name)}</span>
          <span class="badge">${esc(sub.state)}</span>
        </div>
      `).join('');
      $('detail-subtasks-list').querySelectorAll('.subtask-toggle').forEach((cb) => {
        cb.addEventListener('change', async () => {
          await api(`/api/v1/issues/${encodeURIComponent(cb.dataset.key)}`, { method: 'PATCH', body: { state: cb.checked ? 'done' : 'todo' } });
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
          await renderActiveView();
        });
      });
      $('detail-subtasks-list').querySelectorAll('.subtask-open').forEach((a) => {
        a.addEventListener('click', (e) => { e.preventDefault(); openIssueDetail(a.dataset.key, { fullPage: appState.permalinkMode }); });
      });

      // Links
      $('detail-links-list').innerHTML = (iss.links || []).map((lnk) => `
        <div style="display:flex;justify-content:space-between;align-items:center;background:var(--bg-card);padding:5px 8px;border-radius:5px;">
          <span>
            <span class="badge">${esc(lnk.type)}</span>
            <a href="#/issues/${encodeURIComponent(lnk.target_key)}" class="link-target-open mono" data-key="${esc(lnk.target_key)}">${esc(lnk.target_key)}</a>
            — ${esc(lnk.target_name || '')}
          </span>
          <button class="btn btn-sm btn-ghost btn-remove-link" data-id="${esc(lnk.id)}">&#10005;</button>
        </div>
      `).join('') || '<div style="color:var(--text-muted);font-size:12px;">No linked issues.</div>';

      $('detail-links-list').querySelectorAll('.link-target-open').forEach((a) => {
        a.addEventListener('click', (e) => { e.preventDefault(); openIssueDetail(a.dataset.key, { fullPage: appState.permalinkMode }); });
      });
      $('detail-links-list').querySelectorAll('.btn-remove-link').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/links/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          toast('Removed link');
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      // Whole-Issue Attachments
      $('detail-attachments-list').innerHTML = (iss.attachments || []).map((att) => {
        const dlUrl = `/api/v1/issues/${encodeURIComponent(iss.key)}/attachments/${encodeURIComponent(att.id)}/download`;
        return `
          <div class="issue-att-row" data-att-id="${esc(att.id)}" style="display:flex;justify-content:space-between;align-items:center;background:var(--bg-card);padding:6px 8px;border-radius:5px;">
            <div>
              <strong class="mono">${esc(att.id)}</strong> ${esc(att.filename)}
              <span style="color:var(--text-muted);font-size:11px;">(${att.size_bytes} B, ${esc(att.mime_type)})</span>
            </div>
            <div style="display:flex;gap:5px;">
              ${att.is_image
                ? `<button class="btn btn-sm btn-att-lightbox" data-src="${dlUrl}" data-name="${esc(att.filename)}">Preview Image</button>`
                : `<button class="btn btn-sm btn-att-preview" data-id="${esc(att.id)}" data-src="${dlUrl}">Preview</button>`}
              <a class="btn btn-sm btn-att-download" href="${dlUrl}" download="${esc(att.filename)}">Download</a>
              <button class="btn btn-sm btn-danger btn-att-delete" data-id="${esc(att.id)}">Delete</button>
            </div>
          </div>
        `;
      }).join('') || '<div style="color:var(--text-muted);font-size:12px;">No whole-issue attachments.</div>';

      $('detail-attachments-list').querySelectorAll('.btn-att-lightbox').forEach((b) => {
        b.addEventListener('click', () => openImageLightbox(b.dataset.src, b.dataset.name));
      });
      $('detail-attachments-list').querySelectorAll('.btn-att-preview').forEach((b) => {
        b.addEventListener('click', () => openAttachmentPreviewModal(iss.key, b.dataset.id, b.dataset.src));
      });
      $('detail-attachments-list').querySelectorAll('.btn-att-delete').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/attachments/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          toast(`Deleted attachment ${b.dataset.id}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      // Worklogs
      $('detail-time-summary').textContent = iss.time_progress.estimate_minutes
        ? `${iss.time_progress.logged_minutes}m / ${iss.time_progress.estimate_minutes}m`
        : `${iss.time_progress.logged_minutes}m`;
      $('detail-time-progress').style.width = `${iss.time_progress.pct}%`;
      $('detail-worklogs-list').innerHTML = (iss.worklogs || []).map((w) => `
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;background:var(--bg-input);padding:3px 6px;border-radius:4px;">
          <span><strong>${esc(w.author)}</strong>: <span class="mono">${w.minutes}m</span> ${w.note ? '— ' + esc(w.note) : ''}</span>
          <button class="btn btn-sm btn-ghost btn-del-worklog" data-id="${esc(w.id)}" style="padding:0 4px;">&#10005;</button>
        </div>
      `).join('');
      $('detail-worklogs-list').querySelectorAll('.btn-del-worklog').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/worklogs/${encodeURIComponent(b.dataset.id)}`, { method: 'DELETE' });
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      // Comments
      $('detail-comment-count').textContent = String(iss.comment_count || 0);
      renderCommentsList(iss);
    }

    async function openAttachmentPreviewModal(issueKey, attId, dlUrl) {
      try {
        const info = await api(`/api/v1/issues/${encodeURIComponent(issueKey)}/attachments/${encodeURIComponent(attId)}`);
        $('att-preview-title').textContent = `${info.id} — ${info.filename}`;
        $('att-preview-meta').textContent = `MIME: ${info.mime_type} | Size: ${info.size_bytes} bytes | SHA-256: ${info.sha256}`;
        $('att-preview-content').textContent = info.preview_text !== null && info.preview_text !== undefined
          ? info.preview_text
          : '(Binary file — click Download to inspect raw bytes.)';
        $('att-preview-download').href = dlUrl;
        openModal('attachment-preview');
      } catch (err) { toast(err.message, true); }
    }

    function renderCommentsList(iss) {
      const box = $('detail-comments-list');
      box.innerHTML = (iss.comments || []).map((cmt) => {
        const editedBadge = (cmt.edit_history && cmt.edit_history.length) ? '<span class="badge" title="Edited">edited</span>' : '';
        const embedsHtml = (cmt.embeds || []).map((emb) => `
          <span class="badge">
            Embed: ${esc(emb.filename)} (${esc(emb.id)})
            <button class="btn btn-sm btn-ghost btn-del-cmt-embed" data-cid="${esc(cmt.id)}" data-aid="${esc(emb.id)}" style="padding:0 4px;" title="Delete embedded image">&#10005;</button>
          </span>
        `).join('');

        const attsHtml = (cmt.attachments || []).map((ca) => {
          const dl = `/api/v1/issues/${encodeURIComponent(iss.key)}/attachments/${encodeURIComponent(ca.id)}/download`;
          return `
            <span class="badge">
              File: <a href="${dl}" download="${esc(ca.filename)}">${esc(ca.filename)}</a> (${esc(ca.id)})
              <button class="btn btn-sm btn-ghost btn-preview-cmt-att" data-aid="${esc(ca.id)}" data-dl="${dl}" style="padding:0 4px;">Preview</button>
              <button class="btn btn-sm btn-ghost btn-del-cmt-att" data-cid="${esc(cmt.id)}" data-aid="${esc(ca.id)}" style="padding:0 4px;" title="Delete comment attachment">&#10005;</button>
            </span>
          `;
        }).join('');

        const linkedHtml = (cmt.linked_attachments || []).map((la) => {
          if (la.deleted) {
            return `<span class="deleted-att-badge" data-att-id="${esc(la.id)}">[Deleted attachment: ${esc(la.filename || la.id)}]</span>`;
          }
          const dl = `/api/v1/issues/${encodeURIComponent(iss.key)}/attachments/${encodeURIComponent(la.id)}/download`;
          return `
            <span class="badge">
              Linked: <a href="${dl}" download="${esc(la.filename)}">${esc(la.filename)}</a> (${esc(la.id)})
              <button class="btn btn-sm btn-ghost btn-unlink-cmt-att" data-cid="${esc(cmt.id)}" data-aid="${esc(la.id)}" style="padding:0 4px;" title="Unlink attachment">Unlink</button>
            </span>
          `;
        }).join('');

        return `
          <div class="comment-card" data-comment-id="${esc(cmt.id)}" style="border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--bg-card);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
              <div>
                <strong>${esc(cmt.author)}</strong>
                <span class="mono" style="color:var(--text-muted);font-size:11px;margin-left:6px;">${esc(cmt.id)} · ${esc(cmt.created_at)}</span>
                ${editedBadge}
              </div>
              <div style="display:flex;gap:5px;">
                <button class="btn btn-sm btn-edit-comment" data-cid="${esc(cmt.id)}">Edit</button>
                <button class="btn btn-sm btn-danger btn-delete-comment" data-cid="${esc(cmt.id)}">Delete</button>
              </div>
            </div>
            <div class="md-body comment-rendered-body">${renderMarkdown(cmt.body || '', iss)}</div>
            <div class="comment-edit-box" style="display:none;margin-top:6px;">
              <textarea class="comment-edit-textarea" rows="3" style="width:100%;">${esc(cmt.body || '')}</textarea>
              <div style="display:flex;gap:6px;margin-top:5px;">
                <button class="btn btn-sm btn-primary btn-save-comment-edit" data-cid="${esc(cmt.id)}">Save Edit</button>
                <button class="btn btn-sm btn-cancel-comment-edit">Cancel</button>
              </div>
            </div>
            ${(embedsHtml || attsHtml || linkedHtml)
              ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;padding-top:6px;border-top:1px solid var(--border);">${embedsHtml}${attsHtml}${linkedHtml}</div>`
              : ''}
          </div>
        `;
      }).join('') || '<div style="color:var(--text-muted)">No comments yet.</div>';

      box.querySelectorAll('.btn-del-cmt-embed').forEach((b) => {
        b.addEventListener('click', async () => {
          const fd = new FormData();
          fd.append('remove_embeds', JSON.stringify([b.dataset.aid]));
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/comments/${encodeURIComponent(b.dataset.cid)}`, { method: 'PATCH', body: fd });
          toast(`Deleted embed ${b.dataset.aid}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      box.querySelectorAll('.btn-del-cmt-att').forEach((b) => {
        b.addEventListener('click', async () => {
          const fd = new FormData();
          fd.append('remove_attachments', JSON.stringify([b.dataset.aid]));
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/comments/${encodeURIComponent(b.dataset.cid)}`, { method: 'PATCH', body: fd });
          toast(`Deleted comment attachment ${b.dataset.aid}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      box.querySelectorAll('.btn-preview-cmt-att').forEach((b) => {
        b.addEventListener('click', () => openAttachmentPreviewModal(iss.key, b.dataset.aid, b.dataset.dl));
      });

      box.querySelectorAll('.btn-unlink-cmt-att').forEach((b) => {
        b.addEventListener('click', async () => {
          const fd = new FormData();
          fd.append('unlink_attachments', JSON.stringify([b.dataset.aid]));
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/comments/${encodeURIComponent(b.dataset.cid)}`, { method: 'PATCH', body: fd });
          toast(`Unlinked ${b.dataset.aid}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      box.querySelectorAll('.btn-edit-comment').forEach((b) => {
        b.addEventListener('click', () => {
          const card = b.closest('.comment-card');
          card.querySelector('.comment-edit-box').style.display = 'block';
        });
      });
      box.querySelectorAll('.btn-cancel-comment-edit').forEach((b) => {
        b.addEventListener('click', () => { b.closest('.comment-edit-box').style.display = 'none'; });
      });
      box.querySelectorAll('.btn-save-comment-edit').forEach((b) => {
        b.addEventListener('click', async () => {
          const card = b.closest('.comment-card');
          const newBody = card.querySelector('.comment-edit-textarea').value;
          const fd = new FormData();
          fd.append('body', newBody);
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/comments/${encodeURIComponent(b.dataset.cid)}`, { method: 'PATCH', body: fd });
          toast(`Updated ${b.dataset.cid}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });

      box.querySelectorAll('.btn-delete-comment').forEach((b) => {
        b.addEventListener('click', async () => {
          await api(`/api/v1/issues/${encodeURIComponent(iss.key)}/comments/${encodeURIComponent(b.dataset.cid)}`, { method: 'DELETE' });
          toast(`Deleted ${b.dataset.cid}`);
          await openIssueDetail(iss.key, { fullPage: appState.permalinkMode, skipHash: true });
        });
      });
    }

    // ----------------------------------------------------------------------
    // Issue Detail Field Handlers, Clear Controls & Lifecycle Buttons
    // ----------------------------------------------------------------------
    async function patchIssue(fields) {
      if (!appState.activeIssue) return;
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}`, { method: 'PATCH', body: fields });
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
        await renderActiveView();
      } catch (err) { toast(err.message, true); }
    }

    async function submitTitleRename() {
      if (!appState.activeIssue) return;
      const newName = $('detail-title-input').value.trim();
      if (!newName || newName === appState.activeIssue.name) return;
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/rename`, {
          method: 'POST',
          body: { new_name: newName },
        });
        toast(`Renamed folder -> "${newName}"`);
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
        await renderActiveView();
      } catch (err) {
        toast(err.message, true);
        $('detail-title-input').value = appState.activeIssue.name;
      }
    }
    $('btn-save-title').addEventListener('click', submitTitleRename);
    $('detail-title-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submitTitleRename(); } });

    $('detail-category').addEventListener('change', () => patchIssue({ category: $('detail-category').value }));
    $('detail-state').addEventListener('change', () => patchIssue({ state: $('detail-state').value }));
    $('detail-priority').addEventListener('change', () => patchIssue({ priority: $('detail-priority').value }));
    $('detail-severity').addEventListener('change', () => patchIssue({ severity: $('detail-severity').value }));
    $('detail-resolution').addEventListener('change', () => patchIssue({ resolution: $('detail-resolution').value || null }));
    $('detail-assignee').addEventListener('change', () => patchIssue({ assignee: $('detail-assignee').value.trim() || null }));
    $('detail-reporter').addEventListener('change', () => patchIssue({ reporter: $('detail-reporter').value.trim() || null }));
    $('detail-parent').addEventListener('change', () => patchIssue({ parent: $('detail-parent').value.trim() || null }));
    $('detail-sprint').addEventListener('change', () => patchIssue({ sprint: $('detail-sprint').value || null }));
    $('detail-milestone').addEventListener('change', () => patchIssue({ milestone: $('detail-milestone').value || null }));
    $('detail-points').addEventListener('change', () => patchIssue({ story_points: $('detail-points').value !== '' ? Number($('detail-points').value) : null }));
    $('detail-estimate').addEventListener('change', () => patchIssue({ estimate_minutes: $('detail-estimate').value !== '' ? Number($('detail-estimate').value) : null }));
    $('detail-due').addEventListener('change', () => patchIssue({ due_date: $('detail-due').value || null }));
    $('detail-tags').addEventListener('change', () => patchIssue({ tags: $('detail-tags').value.split(',').map((s) => s.trim()).filter(Boolean) }));
    $('detail-components').addEventListener('change', () => patchIssue({ components: $('detail-components').value.split(',').map((s) => s.trim()).filter(Boolean) }));

    // Explicit Clear Buttons
    $('btn-clear-resolution').addEventListener('click', () => patchIssue({ clear_resolution: true }));
    $('btn-clear-assignee').addEventListener('click', () => patchIssue({ clear_assignee: true }));
    $('btn-clear-reporter').addEventListener('click', () => patchIssue({ clear_reporter: true }));
    $('btn-clear-parent').addEventListener('click', () => patchIssue({ clear_parent: true }));
    $('btn-clear-sprint').addEventListener('click', () => patchIssue({ clear_sprint: true }));
    $('btn-clear-milestone').addEventListener('click', () => patchIssue({ clear_milestone: true }));
    $('btn-clear-points').addEventListener('click', () => patchIssue({ clear_points: true }));
    $('btn-clear-estimate').addEventListener('click', () => patchIssue({ clear_estimate: true }));
    $('btn-clear-due').addEventListener('click', () => patchIssue({ clear_due: true }));
    $('btn-clear-tags').addEventListener('click', () => patchIssue({ clear_tags: true }));
    $('btn-clear-components').addEventListener('click', () => patchIssue({ clear_components: true }));
    $('btn-clear-cf').addEventListener('click', () => patchIssue({ clear_fields: true }));
    $('btn-clear-ac').addEventListener('click', () => patchIssue({ clear_ac: true }));
    $('btn-clear-desc').addEventListener('click', () => patchIssue({ clear_desc: true }));

    $('btn-add-cf').addEventListener('click', () => {
      const k = $('detail-cf-key').value.trim();
      const v = $('detail-cf-val').value.trim();
      if (!k) return;
      $('detail-cf-key').value = ''; $('detail-cf-val').value = '';
      patchIssue({ set_fields: { [k]: v } });
    });

    $('btn-edit-desc').addEventListener('click', () => {
      $('detail-desc-rendered').style.display = 'none';
      $('detail-desc-editor').style.display = 'block';
    });
    $('btn-cancel-desc').addEventListener('click', () => {
      $('detail-desc-editor').style.display = 'none';
      $('detail-desc-rendered').style.display = 'block';
    });
    $('btn-save-desc').addEventListener('click', () => patchIssue({ description: $('detail-desc-textarea').value }));

    $('detail-desc-embed-input').addEventListener('change', async (e) => {
      if (!appState.activeIssue || !e.target.files || !e.target.files.length) return;
      const fd = new FormData();
      Array.from(e.target.files).forEach((f) => fd.append('files', f, f.name));
      e.target.value = '';
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/embeds`, { method: 'POST', body: fd });
        toast('Embedded image into description');
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
      } catch (err) { toast(err.message, true); }
    });

    $('btn-add-ac').addEventListener('click', () => {
      const txt = $('detail-ac-input').value.trim();
      if (!txt) return;
      $('detail-ac-input').value = '';
      patchIssue({ add_ac: [txt] });
    });

    $('btn-add-subtask').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      const name = $('detail-subtask-input').value.trim();
      if (!name) return;
      try {
        await api('/api/v1/issues', {
          method: 'POST',
          body: { name, category: 'subtask', parent: appState.activeIssue.key, state: 'todo' },
        });
        $('detail-subtask-input').value = '';
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
        await renderActiveView();
      } catch (err) { toast(err.message, true); }
    });

    // Clone, Context Pack, Archive, Unarchive, Delete Confirm Modal
    $('btn-clone-issue').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      try {
        const cloned = await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/clone`, { method: 'POST', body: {} });
        toast(`Cloned -> ${cloned.key}`);
        await renderActiveView();
        await openIssueDetail(cloned.key, { fullPage: appState.permalinkMode });
      } catch (err) { toast(err.message, true); }
    });

    $('btn-copy-context').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      try {
        const ctx = await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/context`);
        window.__lastAgentContextPack = ctx.markdown;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          try { await navigator.clipboard.writeText(ctx.markdown); } catch (_) {}
        }
        toast('Copied Agent Context Pack');
      } catch (err) { toast(err.message, true); }
    });

    $('btn-archive-issue').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/archive`, { method: 'POST', body: {} });
      toast(`Archived ${appState.activeIssue.key}`);
      await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
      await renderActiveView();
    });

    $('btn-unarchive-issue').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/unarchive`, { method: 'POST', body: {} });
      toast(`Unarchived ${appState.activeIssue.key}`);
      await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
      await renderActiveView();
    });

    $('btn-delete-issue').addEventListener('click', () => {
      if (!appState.activeIssue) return;
      $('delete-confirm-message').textContent = `Permanently delete ${appState.activeIssue.key} ("${appState.activeIssue.name}") and its entire folder?`;
      openModal('delete-confirm');
    });

    $('btn-confirm-delete').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      const k = appState.activeIssue.key;
      try {
        await api(`/api/v1/issues/${encodeURIComponent(k)}`, { method: 'DELETE' });
        closeModal('delete-confirm');
        closeIssueDetail();
        toast(`Deleted ${k}`);
        await refreshMetadataAndCurrentView();
      } catch (err) { toast(err.message, true); }
    });

    // Whole-Issue Attachment Upload (Picker + Drag-and-Drop)
    async function uploadIssueAttachments(fileList) {
      if (!appState.activeIssue || !fileList || !fileList.length) return;
      const fd = new FormData();
      Array.from(fileList).forEach((f) => fd.append('files', f, f.name));
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/attachments`, { method: 'POST', body: fd });
        toast(`Uploaded ${fileList.length} attachment(s)`);
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
      } catch (err) { toast(err.message, true); }
    }
    $('detail-attach-input').addEventListener('change', (e) => {
      uploadIssueAttachments(e.target.files);
      e.target.value = '';
    });
    $('detail-attach-dropzone').addEventListener('dragover', (e) => { e.preventDefault(); $('detail-attach-dropzone').classList.add('drag-over'); });
    $('detail-attach-dropzone').addEventListener('dragleave', () => $('detail-attach-dropzone').classList.remove('drag-over'));
    $('detail-attach-dropzone').addEventListener('drop', (e) => {
      e.preventDefault();
      $('detail-attach-dropzone').classList.remove('drag-over');
      if (e.dataTransfer && e.dataTransfer.files) uploadIssueAttachments(e.dataTransfer.files);
    });

    // Links Modal & Tree Hierarchy
    function formatTreeLines(nodes, indent) {
      let out = '';
      (nodes || []).forEach((n) => {
        const blk = n.is_blocked ? ' [BLOCKED]' : '';
        const lnks = (n.links || []).map((l) => `${l.type}->${l.target_key}`).join(', ');
        out += `${indent}${n.key} "${n.name}" (${n.state}, ${n.priority})${blk}${lnks ? ' {' + lnks + '}' : ''}\n`;
        out += formatTreeLines(n.children, indent + '  └─ ');
      });
      return out;
    }

    async function openLinkAndTreeModal(rootKey) {
      $('link-form-row').style.display = rootKey ? 'flex' : 'none';
      const url = rootKey ? `/api/v1/tree?root=${encodeURIComponent(rootKey)}` : '/api/v1/tree';
      const treeData = await api(url);
      $('modal-tree-hierarchy').textContent = formatTreeLines(treeData.trees, '') || '(No issues in hierarchy)';
      openModal('link-issue');
    }

    $('btn-open-link-modal').addEventListener('click', () => {
      if (appState.activeIssue) openLinkAndTreeModal(appState.activeIssue.key);
    });
    $('btn-view-tree').addEventListener('click', () => {
      if (appState.activeIssue) openLinkAndTreeModal(appState.activeIssue.key);
    });
    $('btn-open-global-tree').addEventListener('click', () => openLinkAndTreeModal(null));

    $('btn-submit-link').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      const tgt = $('link-modal-target').value.trim();
      if (!tgt) return;
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/links`, {
          method: 'POST',
          body: { type: $('link-modal-type').value, target: tgt },
        });
        $('link-modal-target').value = '';
        toast('Link added');
        await openLinkAndTreeModal(appState.activeIssue.key);
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
        await renderActiveView();
      } catch (err) { toast(err.message, true); }
    });

    // Worklog Modal
    $('btn-open-worklog-modal').addEventListener('click', () => {
      $('worklog-modal-duration').value = '';
      $('worklog-modal-note').value = '';
      openModal('log-work');
    });
    $('btn-submit-worklog').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/worklogs`, {
          method: 'POST',
          body: {
            duration: $('worklog-modal-duration').value.trim(),
            note: $('worklog-modal-note').value.trim(),
          },
        });
        closeModal('log-work');
        toast('Logged work');
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
      } catch (err) { toast(err.message, true); }
    });

    // Comments vs History Tabs
    $('tab-btn-comments').addEventListener('click', () => {
      $('tab-btn-comments').classList.add('btn-primary');
      $('tab-btn-history').classList.remove('btn-primary');
      $('detail-comments-pane').style.display = 'block';
      $('detail-history-pane').style.display = 'none';
    });
    $('tab-btn-history').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      $('tab-btn-history').classList.add('btn-primary');
      $('tab-btn-comments').classList.remove('btn-primary');
      $('detail-comments-pane').style.display = 'none';
      $('detail-history-pane').style.display = 'block';
      const hRes = await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/history`);
      $('detail-history-list').innerHTML = (hRes.history || []).slice().reverse().map((h) => `
        <div style="background:var(--bg-card);padding:8px;border-radius:6px;border:1px solid var(--border);">
          <div><span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(h.timestamp)}</span> · <strong>${esc(h.actor)}</strong> · <span class="badge">${esc(h.action)}</span></div>
          <div style="margin-top:4px;font-size:12px;" class="mono">
            ${(h.diffs || []).map((d) => `${esc(d.field)}: ${esc(JSON.stringify(d.old))} &rarr; ${esc(JSON.stringify(d.new))}`).join('<br/>')}
          </div>
        </div>
      `).join('');
    });

    // ----------------------------------------------------------------------
    // Rich Comment Composer: Write/Preview, File Input, Drag-Drop, Paste & Link Picker
    // ----------------------------------------------------------------------
    function renderComposerStagedBar() {
      const chips = [];
      appState.cmtEmbedFiles.forEach((f, idx) => {
        chips.push(`<span class="badge">Embed: ${esc(f.name)} <button type="button" class="btn btn-sm btn-ghost rm-staged-emb" data-idx="${idx}" style="padding:0 3px;">&#10005;</button></span>`);
      });
      appState.cmtAttachFiles.forEach((f, idx) => {
        chips.push(`<span class="badge">Attach: ${esc(f.name)} <button type="button" class="btn btn-sm btn-ghost rm-staged-att" data-idx="${idx}" style="padding:0 3px;">&#10005;</button></span>`);
      });
      appState.cmtLinkedIds.forEach((id, idx) => {
        chips.push(`<span class="badge">Linked: ${esc(id)} <button type="button" class="btn btn-sm btn-ghost rm-staged-lnk" data-idx="${idx}" style="padding:0 3px;">&#10005;</button></span>`);
      });
      const bar = $('cmt-staged-bar');
      bar.innerHTML = chips.join('');
      bar.querySelectorAll('.rm-staged-emb').forEach((b) => b.addEventListener('click', () => { appState.cmtEmbedFiles.splice(Number(b.dataset.idx), 1); renderComposerStagedBar(); }));
      bar.querySelectorAll('.rm-staged-att').forEach((b) => b.addEventListener('click', () => { appState.cmtAttachFiles.splice(Number(b.dataset.idx), 1); renderComposerStagedBar(); }));
      bar.querySelectorAll('.rm-staged-lnk').forEach((b) => b.addEventListener('click', () => { appState.cmtLinkedIds.splice(Number(b.dataset.idx), 1); renderComposerStagedBar(); }));
    }

    function stageComposerEmbedFile(file) {
      let fname = file.name && file.name !== 'image.png'
        ? file.name
        : `pasted-image-${appState.cmtEmbedFiles.length + 1}.png`;
      const namedFile = new File([file], fname, { type: file.type || 'image/png' });
      appState.cmtEmbedFiles.push(namedFile);
      const ta = $('cmt-body-input');
      const mdTag = `![${fname}](embed:${fname})`;
      ta.value = ta.value ? `${ta.value}\n${mdTag}` : mdTag;
      renderComposerStagedBar();
    }

    $('cmt-tab-write').addEventListener('click', () => {
      $('cmt-tab-write').classList.add('btn-primary');
      $('cmt-tab-preview').classList.remove('btn-primary');
      $('cmt-body-input').style.display = 'block';
      $('cmt-preview-box').style.display = 'none';
    });

    $('cmt-tab-preview').addEventListener('click', () => {
      $('cmt-tab-preview').classList.add('btn-primary');
      $('cmt-tab-write').classList.remove('btn-primary');
      $('cmt-body-input').style.display = 'none';
      const stagedEmbedUrls = {};
      appState.cmtEmbedFiles.forEach((f) => {
        try { stagedEmbedUrls[f.name] = URL.createObjectURL(f); } catch (_) {}
      });
      const previewCtx = Object.assign({}, appState.activeIssue || {}, { stagedEmbedUrls });
      $('cmt-preview-box').innerHTML = renderMarkdown($('cmt-body-input').value, previewCtx);
      $('cmt-preview-box').style.display = 'block';
    });

    // Embed Method 1: File input
    $('cmt-embed-input').addEventListener('change', (e) => {
      Array.from(e.target.files || []).forEach(stageComposerEmbedFile);
      e.target.value = '';
    });

    // Comment Attachment File input
    $('cmt-attach-input').addEventListener('change', (e) => {
      Array.from(e.target.files || []).forEach((f) => appState.cmtAttachFiles.push(f));
      renderComposerStagedBar();
      e.target.value = '';
    });

    // Embed Method 2: Drag-and-drop onto composer
    $('cmt-composer-dropzone').addEventListener('dragover', (e) => { e.preventDefault(); $('cmt-composer-dropzone').classList.add('drag-over'); });
    $('cmt-composer-dropzone').addEventListener('dragleave', () => $('cmt-composer-dropzone').classList.remove('drag-over'));
    $('cmt-composer-dropzone').addEventListener('drop', (e) => {
      e.preventDefault();
      $('cmt-composer-dropzone').classList.remove('drag-over');
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      files.forEach((f) => {
        if ((f.type && f.type.startsWith('image/')) || /\.(png|jpe?g|gif|webp|svg)$/i.test(f.name)) {
          stageComposerEmbedFile(f);
        } else {
          appState.cmtAttachFiles.push(f);
        }
      });
      renderComposerStagedBar();
    });

    // Embed Method 3: Clipboard Paste (Ctrl+V with image/png blob)
    $('cmt-body-input').addEventListener('paste', (e) => {
      const cd = e.clipboardData;
      if (!cd) return;
      const pastedImages = [];
      if (cd.items && cd.items.length) {
        for (let i = 0; i < cd.items.length; i++) {
          const item = cd.items[i];
          if (item.type && item.type.startsWith('image/')) {
            const blob = item.getAsFile();
            if (blob) pastedImages.push(blob);
          }
        }
      }
      if (!pastedImages.length && cd.files && cd.files.length) {
        for (let i = 0; i < cd.files.length; i++) {
          const f = cd.files[i];
          if (f.type && f.type.startsWith('image/')) pastedImages.push(f);
        }
      }
      if (pastedImages.length) {
        e.preventDefault();
        pastedImages.forEach(stageComposerEmbedFile);
        toast(`Pasted ${pastedImages.length} inline image(s)`);
      }
    });

    // Link Existing Issue Attachment Picker Modal
    $('btn-cmt-link-att').addEventListener('click', () => {
      if (!appState.activeIssue) return;
      const atts = appState.activeIssue.attachments || [];
      $('link-att-picker-list').innerHTML = atts.length
        ? atts.map((a) => `
          <label style="display:flex;align-items:center;gap:8px;padding:6px;background:var(--bg-card);border-radius:6px;cursor:pointer;">
            <input type="checkbox" class="link-att-check" value="${esc(a.id)}" ${appState.cmtLinkedIds.includes(a.id) ? 'checked' : ''} />
            <strong class="mono">${esc(a.id)}</strong> ${esc(a.filename)} <span style="color:var(--text-muted);font-size:11px;">(${a.size_bytes} B)</span>
          </label>
        `).join('')
        : '<div style="color:var(--text-muted)">No whole-issue attachments available on this issue yet.</div>';
      openModal('link-attachment');
    });

    $('btn-confirm-link-att').addEventListener('click', () => {
      const selected = [];
      $('link-att-picker-list').querySelectorAll('.link-att-check:checked').forEach((cb) => selected.push(cb.value));
      appState.cmtLinkedIds = selected;
      renderComposerStagedBar();
      closeModal('link-attachment');
    });

    $('btn-submit-comment').addEventListener('click', async () => {
      if (!appState.activeIssue) return;
      const fd = new FormData();
      fd.append('body', $('cmt-body-input').value);
      appState.cmtEmbedFiles.forEach((f) => fd.append('embeds', f, f.name));
      appState.cmtAttachFiles.forEach((f) => fd.append('attachments', f, f.name));
      if (appState.cmtLinkedIds.length) {
        fd.append('linked_attachment_ids', JSON.stringify(appState.cmtLinkedIds));
      }
      try {
        await api(`/api/v1/issues/${encodeURIComponent(appState.activeIssue.key)}/comments`, { method: 'POST', body: fd });
        $('cmt-body-input').value = '';
        appState.cmtEmbedFiles = [];
        appState.cmtAttachFiles = [];
        appState.cmtLinkedIds = [];
        renderComposerStagedBar();
        $('cmt-tab-write').click();
        toast('Comment posted');
        await openIssueDetail(appState.activeIssue.key, { fullPage: appState.permalinkMode, skipHash: true });
      } catch (err) { toast(err.message, true); }
    });

    // ----------------------------------------------------------------------
    // Hash Routing (Permalinks + Back/Forward History) & Keyboard Shortcuts
    // ----------------------------------------------------------------------
    async function handleHashRoute() {
      if (appState.suppressHashHandler) return;
      const h = location.hash || '';
      if (h.startsWith('#/issues/')) {
        const ref = decodeURIComponent(h.slice('#/issues/'.length));
        if (ref) {
          await openIssueDetail(ref, { fullPage: true, skipHash: true });
          return;
        }
      }
      if (h.startsWith('#/views/')) {
        const vName = h.slice('#/views/'.length);
        if (['issues','board','sprints','milestones','views','analytics','tags','settings'].includes(vName)) {
          await switchView(vName, false);
          return;
        }
      }
      await switchView('issues', false);
    }

    window.addEventListener('hashchange', handleHashRoute);

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const openModals = Array.from(document.querySelectorAll('.modal-backdrop.open'));
        if (openModals.length) {
          openModals[openModals.length - 1].classList.remove('open');
          return;
        }
        if ($('issue-drawer').classList.contains('open') && !appState.permalinkMode) {
          closeIssueDetail();
        }
        return;
      }
      const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable)) {
        return;
      }
      if (e.key === 'c' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        openCreateModal();
      } else if (e.key === '/') {
        e.preventDefault();
        $('global-search-input').focus();
      }
    });

    // Boot SPA
    initTheme();
    loadReferenceData().then(() => handleHashRoute()).catch((err) => toast(err.message, true));
  })();
  </script>
</body>
</html>
"""


# ============================================================================
# Section 7: Server CLI Entrypoint, Pre-Flight Exit Codes & Pre-Bound Socket
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="itrack-server.py",
        description="iTrack Local-First Folder-Per-Issue Daemon, Self-Healing FTS5 Index & Offline SPA",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Git repository root directory (defaults to ITRACK_ROOT env var or cwd).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="TCP bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP bind port (default: 0 for race-free ephemeral port allocation).",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize itrack-issues/ workspace on disk if not already initialized.",
    )
    parser.add_argument(
        "--prefix",
        default="ITR",
        help="Project key prefix when --init is used (default: ITR).",
    )
    parser.add_argument(
        "--title",
        default="iTrack Workspace",
        help="Project title when --init is used (default: iTrack Workspace).",
    )
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log verbosity (default: warning).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    raw_root = resolve_raw_root_arg(args.root)
    repo_root = check_and_resolve_git_root(raw_root)

    if args.init:
        init_workspace_on_disk(repo_root, prefix=args.prefix, title=args.title)
    elif not (repo_root / "itrack-issues" / "config.json").exists():
        print(
            f"Error [Workspace Not Initialized]: '{repo_root / 'itrack-issues' / 'config.json'}' not found. "
            f"Run 'itrack init' first.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(3)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, int(args.port)))
    except OSError as exc:
        sock.close()
        print(f"Error: Failed to bind {args.host}:{args.port}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    actual_port = int(sock.getsockname()[1])
    server_ref: Dict[str, Any] = {"port": actual_port}
    app = create_app(repo_root=repo_root, host=args.host, port=actual_port, server_ref=server_ref)

    config = uvicorn.Config(
        app=app,
        host=args.host,
        port=actual_port,
        log_level=args.log_level,
        access_log=(args.log_level in ("info", "debug", "trace")),
    )
    server = uvicorn.Server(config)
    server_ref["uvicorn_server"] = server

    try:
        server.run(sockets=[sock])
    finally:
        with contextlib.suppress(Exception):
            sock.close()


if __name__ == "__main__":
    main()
