#!/usr/bin/env python3
"""itrack CLI, Agent Manual & Auto-Daemon Client.

Zero-setup, Git-rooted issue tracker CLI with Rich TUI, 100% --json parity,
Law 2 Git-root enforcement, Law 3 Zero-Arg Agent Manual, and transparent
per-repository FastAPI daemon auto-spawning.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

import httpx
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

VERSION = "1.0.0"

stdout_console = Console(highlight=False)
stderr_console = Console(stderr=True, highlight=False)

STATUS_STYLES: dict[str, str] = {
    "backlog": "dim white",
    "todo": "cyan",
    "in_progress": "bold yellow",
    "in_review": "bold magenta",
    "blocked": "bold white on red",
    "done": "bold green",
    "canceled": "strike dim",
}

PRIORITY_STYLES: dict[str, str] = {
    "none": "dim",
    "low": "blue",
    "medium": "cyan",
    "high": "bold yellow",
    "urgent": "bold red",
    "critical": "bold red",
}

TYPE_ICONS: dict[str, str] = {
    "epic": "⚡",
    "story": "📘",
    "task": "☑️",
    "bug": "🐞",
    "subtask": "↳",
    "spike": "🔬",
}

# Known CLI options across subcommands that consume the next argument token.
# Tracking these ensures `extract_global_flags` never strips `--root` or `--json`
# when passed as a literal value to another option (e.g., `-m "--json"`).
OPTIONS_TAKING_VALUE: set[str] = {
    "-d", "--description", "--desc",
    "-t", "--type",
    "-s", "--status",
    "-p", "--priority",
    "-a", "--assignee",
    "-r", "--reporter",
    "-l", "--label", "--labels",
    "-c", "--component", "--components",
    "--epic", "--parent", "--sprint", "--milestone",
    "--points", "--due", "--actor", "--title",
    "--add-label", "--remove-label", "--resolution",
    "--environment", "--sort",
    "-q", "--query", "--iql",
    "--limit", "--offset",
    "-m", "--message", "--note", "--comment",
    "--agent", "--keys", "--action",
    "--author", "--by",
    "-o", "--output",
    "--state", "--goal", "--start", "--end", "--rollover",
    "--target", "--color", "--lead",
    "--name", "--email", "--role", "--avatar-color",
    "--layout", "--icon",
    "--key", "--default-type", "--default-priority", "--wip-limits",
    "--file", "--mode", "--port", "-n", "--lines", "--title-prefix",
}


class CLIError(Exception):
    """Structured CLI or API error."""

    def __init__(self, message: str, error_code: str = "cli_error", status_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code


def norm_key(key: str) -> str:
    """Normalize issue/sprint/milestone/view keys to uppercase (e.g. itk-1 -> ITK-1)."""
    val = key.strip()
    return val.upper() if val else val


def norm_opt_key(key: str | None) -> str | None:
    if key is None:
        return None
    val = key.strip()
    if not val or val.lower() in ("none", "null", "unassigned", "backlog"):
        return None
    return val.upper()


def split_csv(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items


def default_actor(repo_root: Path | None = None) -> str:
    env_user = os.environ.get("ITRACK_USER") or os.environ.get("ITRACK_AGENT")
    if env_user:
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


def extract_global_flags(argv: list[str]) -> tuple[bool, str | None, list[str]]:
    """Extract `--json` and `--root <path>` / `--root=<path>` from any position in argv."""
    json_mode = False
    root_arg: str | None = None
    remaining: list[str] = []
    i = 0
    n = len(argv)
    expect_value = False
    primary_cmd: str | None = None

    while i < n:
        tok = argv[i]
        if tok == "--":
            remaining.extend(argv[i:])
            break
        if expect_value:
            remaining.append(tok)
            expect_value = False
            i += 1
            continue
        if tok == "--json":
            json_mode = True
            i += 1
            continue
        if tok == "--root":
            if i + 1 >= n:
                raise CLIError("Option '--root' requires a path argument.", "invalid_arguments", 400)
            root_arg = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--root="):
            root_arg = tok.split("=", 1)[1]
            if not root_arg:
                raise CLIError("Option '--root' requires a non-empty path.", "invalid_arguments", 400)
            i += 1
            continue

        if primary_cmd is None and not tok.startswith("-"):
            primary_cmd = tok

        remaining.append(tok)
        if tok in OPTIONS_TAKING_VALUE:
            if tok == "--iql" and primary_cmd == "search":
                expect_value = False
            elif tok == "--agent" and primary_cmd == "user":
                expect_value = False
            else:
                expect_value = True
        i += 1

    return json_mode, root_arg, remaining


def detect_git_toplevel_hint(target_path: Path) -> str | None:
    """If inside a subdirectory of a Git repository, detect parent git root for hint only."""
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
            if toplevel != target_path:
                return str(toplevel)
    except Exception:
        pass
    return None


def resolve_and_check_git_root(root_arg: str | None, json_mode: bool) -> Path:
    """Law 2: Enforce that repo_root is a valid Git repository root (.git exists)."""
    if root_arg is not None:
        raw_root = Path(root_arg).expanduser()
    elif os.environ.get("ITRACK_ROOT"):
        raw_root = Path(os.environ["ITRACK_ROOT"]).expanduser()
    else:
        raw_root = Path.cwd()

    repo_root = raw_root.resolve()
    if (repo_root / ".git").exists():
        return repo_root

    toplevel_hint = detect_git_toplevel_hint(repo_root)
    base_msg = (
        f"Error [Law 2 - Git-Root Check]: '{repo_root}' is not a Git repository root "
        f"(.git not found). Run from the root of a Git repository."
    )
    if toplevel_hint:
        full_msg = f"{base_msg} Hint: cd {toplevel_hint}"
    else:
        full_msg = base_msg

    if json_mode:
        payload: dict[str, Any] = {
            "error": "not_git_root",
            "message": full_msg,
            "path": str(repo_root),
        }
        if toplevel_hint:
            payload["hint_git_root"] = toplevel_hint
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(full_msg + "\n")
        sys.stderr.flush()
    sys.exit(2)


def build_manual_payload(repo_root: Path) -> dict[str, Any]:
    """Structured Law 3 Agent & Operator Manual payload."""
    return {
        "name": "itrack",
        "version": VERSION,
        "repo_root": str(repo_root),
        "summary": (
            "Git-rooted local-first Jira/Linear-grade issue tracker with atomic file-locked "
            "JSON storage, SQLite FTS5 + iQL query engine, multi-agent coordination, Rich TUI, "
            "and embedded offline Web SPA."
        ),
        "laws": {
            "law_1_repo_footprint": "Self-contained 2-script architecture (itrack.py + itrack-server.py + requirements.txt).",
            "law_2_git_root": "Commands execute strictly against a Git repository root containing .git (exit code 2 otherwise).",
            "law_3_zero_arg_manual": "Running `itrack` with zero arguments (or `manual` / `--manual`) prints this manual without spawning the daemon.",
        },
        "global_flags": {
            "--json": "Position-independent flag. Emits pure structured JSON to stdout on all commands and errors.",
            "--root <PATH>": "Position-independent flag. Target Git repository root (overrides ITRACK_ROOT and cwd).",
        },
        "storage": {
            "directory": f"{repo_root}/itrack-issues/",
            "source_of_truth": "Atomic, flock-synchronized per-issue JSON files in itrack-issues/issues/{KEY}.json (Git-friendly).",
            "runtime_ephemeral": "itrack-issues/.runtime/ (gitignored: storage.lock, index.db SQLite+FTS5 cache, daemon.json, daemon.log).",
        },
        "daemon": {
            "auto_spawn": "Any active CLI command transparently discovers or spawns `itrack-server.py` on an ephemeral 127.0.0.1 port.",
            "commands": [
                "itrack daemon status",
                "itrack daemon start [--port PORT]",
                "itrack daemon restart [--port PORT]",
                "itrack daemon stop",
                "itrack daemon logs [-n 50]",
                "itrack ui [--port PORT] [--no-open]",
            ],
        },
        "workflows": {
            "autonomous_agent_loop": [
                "1. Discover unblocked work: `itrack ready --json`",
                "2. Atomically claim issue: `itrack claim ITK-1 --agent agent-alpha -m 'Starting implementation' --json`",
                "3. Decompose if needed: `itrack issue subtask-add ITK-1 'Write unit tests' -p high --json`",
                "4. Log progress & artifacts: `itrack comment add ITK-1 'Implemented parser'` / `itrack attach upload ITK-1 report.txt` / `itrack worklog add ITK-1 '45m' -m 'Implementation'`",
                "5. Complete & verify: `itrack transition ITK-1 done --resolution fixed -m 'All tests green' --json` (or `itrack close ITK-1`)",
                "6. Or release if blocked/handing off: `itrack release ITK-1 --agent agent-alpha -s blocked -m 'Waiting on API key'`",
            ],
            "default_statuses": ["backlog", "todo", "in_progress", "in_review", "blocked", "done", "canceled"],
            "priorities": ["none (0)", "low (1)", "medium (2)", "high (3)", "urgent (4)"],
            "issue_types": ["epic", "story", "task", "bug", "subtask", "spike"],
            "link_types": ["blocks", "blocked_by", "relates_to", "duplicates", "duplicated_by", "clones", "cloned_from"],
        },
        "iql_syntax": {
            "grammar": "[expr] [ORDER BY field [ASC|DESC], ...] [LIMIT n] [OFFSET n]",
            "operators": ["=", "!=", ">", ">=", "<", "<=", "~", "!~", "IN (...)", "NOT IN (...)", "IS EMPTY", "IS NOT EMPTY", "AND", "OR", "NOT", "( )"],
            "fields": [
                "key", "type", "status", "priority", "assignee", "reporter", "claimed_by",
                "epic", "parent", "sprint", "milestone", "label", "component", "points",
                "title", "description", "comment", "text", "blocked", "resolution",
                "created", "updated", "due", "resolved",
            ],
            "special_values": ["@me", "today", "now", "-7d", "-24h", "+3d", "sprint = active"],
            "examples": [
                "status IN (todo, in_progress) AND priority >= high ORDER BY priority DESC",
                "assignee = @me AND blocked = false",
                "label = security AND text ~ 'oauth' LIMIT 20",
                "sprint = active AND status != done",
                "updated >= -7d AND assignee IS EMPTY",
            ],
        },
        "iql_reference": "Use `itrack iql \"<EXPR>\"` to query or `itrack iql \"<EXPR>\" --validate` to parse/validate AST.",
        "commands": {
            "shortcuts": {
                "init": "itrack init [--key ITK] [--name 'Project Name']",
                "ui": "itrack ui [--port PORT] [--no-open]",
                "create": "itrack create <TITLE> [-d DESC] [-t TYPE] [-p PRIORITY] [-a ASSIGNEE] [-l L1,L2] [-c C1,C2] [--epic KEY] [--sprint ID] [--points N]",
                "list": "itrack list [--iql EXPR] [-s STATUS] [-p PRIORITY] [-t TYPE] [-a ASSIGNEE] [-l LABEL]",
                "show": "itrack show <KEY>",
                "ready": "itrack ready [-a ASSIGNEE] [--sprint ID] [--limit N]",
                "claim": "itrack claim <KEY> --agent <AGENT> [-m NOTE] [--force]",
                "release": "itrack release <KEY> [--agent <AGENT>] [-s STATUS] [-m NOTE]",
                "move": "itrack move <KEY> <STATUS> [--resolution RES] [-m COMMENT] [--force]",
                "close": "itrack close <KEY> [--resolution fixed] [-m COMMENT]",
                "reopen": "itrack reopen <KEY> [-m COMMENT]",
                "search": "itrack search <QUERY> [--iql]",
                "iql": "itrack iql <IQL_EXPR> [--validate] [--limit N] [--offset N]",
                "board": "itrack board [--sprint ID] [--epic KEY] [--assignee USER] [--iql EXPR]",
                "graph": "itrack graph",
                "stats": "itrack stats",
                "reindex": "itrack reindex",
                "export": "itrack export [-o backup.json]",
                "import": "itrack import <backup.json> [--mode merge|replace]",
            },
            "groups": {
                "issue": "create | list | show | update | delete | transition | assign | clone | claim | release | ready | subtasks | subtask-add | history | bulk | watch | unwatch",
                "comment": "list <KEY> | add <KEY> <BODY> [--author USER] | edit <KEY> <ID> <BODY> | delete <KEY> <ID>",
                "attach": "list <KEY> | upload <KEY> <FILE> [--by USER] [-d DESC] | download <KEY> <ATT_ID> [-o PATH] | delete <KEY> <ATT_ID>",
                "link": "list <KEY> | add <KEY> <blocks|blocked_by|relates_to|duplicates|clones> <TARGET_KEY> | remove <KEY> <LINK_ID>",
                "worklog": "list <KEY> | add <KEY> <DURATION_OR_MINUTES> [-m NOTE] [--author USER] | delete <KEY> <WL_ID>",
                "sprint": "list [--state S] | create <NAME> [--goal G] [--start D] [--end D] | show <ID> | update <ID> | start <ID> | complete <ID> [--rollover ID] | delete <ID>",
                "epic": "list | create <TITLE> [-d DESC] [-p PRIORITY] [-a ASSIGNEE]",
                "milestone": "list | create <NAME> [-d DESC] [--target YYYY-MM-DD] | update <ID> | delete <ID>",
                "label": "list | add <NAME> [--color HEX] [-d DESC] | delete <NAME>",
                "component": "list | add <NAME> [--lead USER] [-d DESC] | delete <NAME>",
                "user": "list | add <USERNAME> [--name DISPLAY] [--email E] [--role R] [--agent] | show <USERNAME> | update <USERNAME> | delete <USERNAME>",
                "view": "list | create <NAME> --iql <EXPR> [--layout board|list] [-d DESC] | update <ID> | delete <ID>",
                "config": "show | set [--key K] [--name N] [--default-type T] [--default-priority P]",
                "workflow": "show | set --file <JSON_FILE_OR_INLINE>",
                "daemon": "start [--port PORT] | stop | restart [--port PORT] | status | logs [-n LINES]",
            },
        },
    }


MANUAL_MARKDOWN_TEMPLATE = r"""\
# itrack v{version} — Agent & Operator Manual

**Repository Root**: `{repo_root}`  
**Storage Path**: `{repo_root}/itrack-issues/` *(auto-initialized on first active command; daemon NOT spawned by manual)*

---

## 1. Quickstart & Autonomous Agent Workflow

Every command supports **position-independent `--json` and `--root <PATH>`** flags (e.g. `itrack --json ready` or `itrack ready --json`). Issue keys normalize automatically (`itk-1` -> `ITK-1`).

```bash
# 1. Initialize workspace (optional custom key/name) & inspect ready unblocked work
itrack init --key ITK --name "Core Platform"
itrack ready --json

# 2. Atomically claim an issue (409 conflict if already claimed by another agent unless --force)
itrack claim ITK-1 --agent coder-1 -m "Implementing OAuth2 PKCE" --json

# 3. Break down subtasks, link blockers, attach artifacts, comment & log time
itrack issue subtask-add ITK-1 "Add token refresh unit tests" -p high
itrack link add ITK-1 blocks ITK-2
itrack comment add ITK-1 "PKCE verifier passing local tests" --author coder-1
itrack attach upload ITK-1 ./test-report.txt --by coder-1 -d "Pytest log"
itrack worklog add ITK-1 "1h 15m" -m "Core auth flow"

# 4. Complete or hand off
itrack transition ITK-1 done --resolution fixed -m "Merged & verified"
# Or release claim back to queue:
itrack release ITK-1 --agent coder-1 -s in_review -m "Ready for review"
```

---

## 2. Command Reference

### Top-Level Shortcuts
| Command | Usage & Description |
| :--- | :--- |
| `init` | `itrack init [--key ITK] [--name "Name"]` — Initialize workspace & project config |
| `ui` | `itrack ui [--port PORT] [--no-open]` — Launch offline Web SPA & print URL |
| `create` | `itrack create <TITLE> [-d DESC] [-t task\|bug\|story\|epic\|spike] [-p low\|medium\|high\|urgent] [-a USER] [-l a,b] [-c x,y] [--epic KEY] [--parent KEY] [--sprint ID] [--milestone ID] [--points N] [--due YYYY-MM-DD]` |
| `list` | `itrack list [--iql EXPR] [-q TEXT] [-s STATUS] [-p PRIORITY] [-t TYPE] [-a USER] [-l LABEL] [-c COMP] [--sprint ID] [--epic KEY] [--limit N]` |
| `show` | `itrack show <KEY>` — Full Rich view (metadata, Markdown body, subtasks, links, comments, attachments) |
| `ready` | `itrack ready [-a USER] [--sprint ID] [--limit N]` — Unblocked actionable issues (`todo`/`backlog`, no open blockers) |
| `claim` / `release` | `itrack claim <KEY> --agent <ID> [-m NOTE] [--force]` / `itrack release <KEY> [--agent <ID>] [-s STATUS] [-m NOTE]` |
| `move` / `close` / `reopen` | `itrack move <KEY> <STATUS> [-m MSG] [--resolution RES] [--force]` · `itrack close <KEY>` (`-> done`) · `itrack reopen <KEY>` (`-> todo`) |
| `search` / `iql` | `itrack search <TEXT> [--iql]` · `itrack iql "<EXPR>" [--validate] [--limit N]` |
| `board` / `graph` / `stats` | `itrack board [--sprint ID] [--epic KEY] [--assignee U] [--iql EXPR]` · `itrack graph` · `itrack stats` |
| `export` / `import` / `reindex` | `itrack export [-o file.json]` · `itrack import <file.json> [--mode merge\|replace]` · `itrack reindex` |

### Resource Subcommands
- **`issue`**: `create`, `list`, `show`, `update <KEY> [--title --desc -t -s -p -a --add-label --remove-label -l -c --epic --parent --sprint --milestone --points --due --resolution]`, `delete <KEY>`, `transition <KEY> <STATUS>`, `assign <KEY> <USER|none>`, `clone <KEY> [--include-subtasks]`, `claim`, `release`, `ready`, `subtasks <KEY>`, `subtask-add <KEY> <TITLE>`, `history <KEY>`, `bulk --keys K1,K2 --action <update|transition|delete|label_add|label_remove|sprint>`, `watch <KEY> <USER>`, `unwatch <KEY> <USER>`
- **`comment`**: `list <KEY>` · `add <KEY> <BODY> [--author U]` · `edit <KEY> <CMT_ID> <BODY>` · `delete <KEY> <CMT_ID>`
- **`attach`** *(alias `attachment`)*: `list <KEY>` · `upload <KEY> <PATH> [--by U] [-d DESC]` · `download <KEY> <ATT_ID> [-o OUT]` · `delete <KEY> <ATT_ID>`
- **`link`**: `list <KEY>` · `add <KEY> <blocks|blocked_by|relates_to|duplicates|clones> <TARGET_KEY>` · `remove <KEY> <LINK_ID>`
- **`worklog`**: `list <KEY>` · `add <KEY> <"1h 30m"|90> [-m NOTE] [--author U]` · `delete <KEY> <WL_ID>`
- **`sprint`**: `list [--state planned|active|completed]` · `create <NAME> [--goal G] [--start D] [--end D]` · `show <ID>` · `update <ID>` · `start <ID>` · `complete <ID> [--rollover ID|backlog]` · `delete <ID>`
- **`epic`**: `list` · `create <TITLE> [-d DESC] [-p PRIORITY] [-a USER]`
- **`milestone`**: `list` · `create <NAME> [-d DESC] [--target YYYY-MM-DD]` · `update <ID> [--status unreleased|released|archived]` · `delete <ID>`
- **`label` / `component`**: `label list | add <NAME> [--color HEX] [-d DESC] | delete <NAME>` · `component list | add <NAME> [--lead U] [-d DESC] | delete <NAME>`
- **`user` / `view`**: `user list | add <USER> [--name N] [--email E] [--role R] [--agent] | show <U> | update <U> | delete <U>` · `view list | create <NAME> --iql <EXPR> [--layout board|list] | update <ID> | delete <ID>`
- **`config` / `workflow`**: `config show | set [--key K] [--name N] [--default-type T] [--default-priority P]` · `workflow show | set --file <JSON_OR_PATH>`
- **`daemon`** *(alias `server`)*: `start [--port PORT]` · `stop` · `restart [--port PORT]` · `status` · `logs [-n 50]`

---

## 3. iQL (itrack Query Language)

- **Operators**: `=`, `!=`, `>`, `>=`, `<`, `<=`, `~` (FTS/contains), `!~`, `IN (...)`, `NOT IN (...)`, `IS EMPTY`, `IS NOT EMPTY`
- **Combinators & Clauses**: `AND`, `OR`, `NOT`, `( ... )`, `ORDER BY <field> [ASC|DESC]`, `LIMIT <n>`, `OFFSET <n>`
- **Fields**: `key`, `type`, `status`, `priority` *(ordinal: `urgent > high > medium > low > none`)*, `assignee`, `reporter`, `claimed_by`, `epic`, `parent`, `sprint` *(`sprint = active`)*, `milestone`, `label`, `component`, `points`, `title`, `description`, `comment`, `text` *(FTS5 across all text)*, `blocked`, `resolution`, `created`, `updated`, `due`, `resolved`
- **Dynamic Literals**: `@me`, `today`, `now`, `-7d`, `-24h`, `-2w`, `+3d`
- **Examples**:
  - `itrack iql "priority >= high AND status != done ORDER BY priority DESC"`
  - `itrack iql "text ~ 'authentication' AND label IN (auth, backend)"`
  - `itrack list --iql "assignee = @me AND blocked = false"`

---

## 4. Architecture, Storage & Daemon Lifecycle

- **Git-Friendly Source of Truth**: `<repo>/itrack-issues/issues/ITK-*.json`, `sprints/SPR-*.json`, `milestones/MIL-*.json`, `config.json` — human-readable, deterministic `sort_keys=True` JSON protected by `fcntl.flock` + atomic rename.
- **Rebuildable Hybrid Index**: `<repo>/itrack-issues/.runtime/index.db` (SQLite WAL + FTS5) is gitignored automatically via `itrack-issues/.gitignore`. Run `itrack reindex` anytime after `git pull`/`git merge`.
- **Auto-Daemon**: Active CLI commands check `itrack-issues/.runtime/daemon.json` + `/api/v1/health` (`127.0.0.1`) and transparently auto-spawn `itrack-server.py` on an ephemeral port if needed.
"""


def render_agent_manual(repo_root: Path, json_mode: bool) -> None:
    """Render Law 3 manual without starting or touching the daemon."""
    if json_mode:
        sys.stdout.write(json.dumps(build_manual_payload(repo_root), indent=2) + "\n")
        sys.stdout.flush()
        return
    md_text = MANUAL_MARKDOWN_TEMPLATE.format(version=VERSION, repo_root=repo_root)
    stdout_console.print(Markdown(md_text))


# ---------------------------------------------------------------------------
# Daemon Discovery, Auto-Spawner & HTTP Client
# ---------------------------------------------------------------------------


def get_runtime_dir(repo_root: Path) -> Path:
    return repo_root / "itrack-issues" / ".runtime"


def get_daemon_json_path(repo_root: Path) -> Path:
    return get_runtime_dir(repo_root) / "daemon.json"


def get_daemon_log_path(repo_root: Path) -> Path:
    return get_runtime_dir(repo_root) / "daemon.log"


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
    """Check daemon.json and verify /api/v1/health matches repo_root."""
    meta = read_daemon_json(repo_root)
    if not meta:
        return None
    base_url = str(meta["base_url"]).rstrip("/")
    try:
        with httpx.Client(timeout=1.5, trust_env=False) as client:
            resp = client.get(f"{base_url}/api/v1/health")
            if resp.status_code != 200:
                return None
            data = resp.json()
            remote_root = data.get("repo_root")
            if not remote_root:
                return None
            if Path(remote_root).resolve() != repo_root.resolve():
                return None
            return base_url, data
    except Exception:
        return None


def locate_server_script() -> Path:
    override = os.environ.get("ITRACK_SERVER_SCRIPT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "itrack-server.py"


def spawn_daemon(repo_root: Path, requested_port: int = 0) -> tuple[str, dict[str, Any]]:
    """Spawn `itrack-server.py` in background under exclusive flock and poll up to 8.0s until healthy."""
    server_script = locate_server_script()
    if not server_script.exists():
        raise CLIError(
            f"Cannot find server script at '{server_script}'. Ensure 'itrack-server.py' sits alongside 'itrack.py'.",
            error_code="server_script_missing",
            status_code=500,
        )

    rdir = get_runtime_dir(repo_root)
    rdir.mkdir(parents=True, exist_ok=True)
    spawn_lock_path = rdir / "daemon-spawn.lock"

    with open(spawn_lock_path, "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            # Double-check after acquiring lock: a peer CLI process may have just started the daemon!
            probed = probe_daemon(repo_root)
            if probed is not None:
                cur_port = int(probed[1].get("port") or 0)
                if requested_port == 0 or cur_port == requested_port:
                    return probed

            djson = get_daemon_json_path(repo_root)
            # Remove stale daemon.json before spawning fresh instance
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

            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                probed = probe_daemon(repo_root)
                if probed is not None:
                    return probed
                rc = proc.poll()
                if rc is not None:
                    tail = read_log_tail(repo_root, lines=20)
                    detail = f"itrack-server.py exited prematurely with code {rc}."
                    if tail:
                        detail = f"{detail} Recent log:\n{tail}"
                    raise CLIError(detail, error_code="daemon_spawn_failed", status_code=500)
                time.sleep(0.05)

            tail = read_log_tail(repo_root, lines=20)
            detail = "Timed out after 8.0s waiting for itrack-server.py to become healthy."
            if tail:
                detail = f"{detail} Recent log:\n{tail}"
            raise CLIError(detail, error_code="daemon_timeout", status_code=504)
        finally:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def ensure_daemon(repo_root: Path, requested_port: int = 0) -> tuple[str, dict[str, Any]]:
    """Return `(base_url, health_data)` for active daemon, auto-spawning if needed."""
    probed = probe_daemon(repo_root)
    if probed is not None:
        return probed
    return spawn_daemon(repo_root, requested_port=requested_port)


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


def stop_daemon(repo_root: Path) -> dict[str, Any]:
    """Gracefully stop running daemon via POST /api/v1/shutdown + SIGTERM fallback."""
    meta = read_daemon_json(repo_root)
    probed = probe_daemon(repo_root)
    djson = get_daemon_json_path(repo_root)

    if probed is None and meta is None:
        return {"status": "not_running", "stopped": False, "repo_root": str(repo_root)}

    pid = int((probed[1].get("pid") if probed else (meta or {}).get("pid")) or 0)
    base_url = probed[0] if probed else str((meta or {}).get("base_url", "")).rstrip("/")

    if probed is not None and base_url:
        try:
            with httpx.Client(timeout=2.5, trust_env=False) as client:
                client.post(f"{base_url}/api/v1/shutdown")
        except Exception:
            pass

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if pid and not is_pid_alive(pid) and probe_daemon(repo_root) is None:
            break
        if not pid and probe_daemon(repo_root) is None:
            break
        time.sleep(0.05)

    if pid and is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        term_deadline = time.monotonic() + 2.0
        while time.monotonic() < term_deadline and is_pid_alive(pid):
            time.sleep(0.05)

    if djson.exists():
        try:
            djson.unlink()
        except OSError:
            pass

    return {
        "status": "stopped",
        "stopped": True,
        "pid": pid or None,
        "repo_root": str(repo_root),
    }


def read_log_tail(repo_root: Path, lines: int = 50) -> str:
    log_path = get_daemon_log_path(repo_root)
    if not log_path.exists():
        return ""
    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(all_lines[-max(1, lines):])
    except Exception:
        return ""


class ITrackClient:
    """Thin httpx wrapper enforcing `trust_env=False` and structured CLIError translation."""

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
                detail = payload.get("detail", payload)
            else:
                detail = payload
        except Exception:
            detail = resp.text.strip() or f"HTTP {resp.status_code}"

        if isinstance(detail, (dict, list)):
            msg = json.dumps(detail)
        else:
            msg = str(detail)

        code_map = {
            400: "bad_request",
            404: "not_found",
            409: "conflict",
            422: "validation_error",
        }
        err_code = code_map.get(resp.status_code, "api_error")
        raise CLIError(msg, error_code=err_code, status_code=resp.status_code)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        clean_params = (
            {k: v for k, v in params.items() if v is not None}
            if params is not None
            else None
        )
        try:
            resp = self._client.request(method, path, params=clean_params, json=json_body)
        except httpx.HTTPError as exc:
            raise CLIError(
                f"Failed communicating with itrack daemon ({self.base_url}): {exc}",
                error_code="connection_error",
                status_code=503,
            ) from exc

        if resp.status_code >= 400:
            self._raise_api_error(resp)
        if not resp.content:
            return {}
        return resp.json()

    def upload_attachment(
        self,
        issue_key: str,
        file_path: Path,
        uploaded_by: str,
        description: str = "",
    ) -> dict[str, Any]:
        if not file_path.exists() or not file_path.is_file():
            raise CLIError(f"Attachment file not found: '{file_path}'", "file_not_found", 400)
        mime_type, _ = mimetypes.guess_type(str(file_path))
        content_type = mime_type or "application/octet-stream"
        url_key = urllib.parse.quote(norm_key(issue_key), safe="")
        try:
            with open(file_path, "rb") as fh:
                files = {"file": (file_path.name, fh, content_type)}
                data = {"uploaded_by": uploaded_by, "description": description}
                resp = self._client.post(f"/api/v1/issues/{url_key}/attachments", files=files, data=data)
        except httpx.HTTPError as exc:
            raise CLIError(f"Upload failed: {exc}", "connection_error", 503) from exc

        if resp.status_code >= 400:
            self._raise_api_error(resp)
        return resp.json()

    def download_attachment(
        self,
        issue_key: str,
        attachment_id: str,
        output_path: Path | None,
    ) -> dict[str, Any]:
        url_key = urllib.parse.quote(norm_key(issue_key), safe="")
        url_att = urllib.parse.quote(attachment_id.strip(), safe="")
        try:
            resp = self._client.get(f"/api/v1/issues/{url_key}/attachments/{url_att}/download")
        except httpx.HTTPError as exc:
            raise CLIError(f"Download failed: {exc}", "connection_error", 503) from exc

        if resp.status_code >= 400:
            self._raise_api_error(resp)

        filename = attachment_id
        cd = resp.headers.get("content-disposition", "")
        match = re.search(r'filename="?([^";]+)"?', cd)
        if match:
            filename = Path(match.group(1)).name

        dest = output_path if output_path is not None else (Path.cwd() / filename)
        if dest.exists() and dest.is_dir():
            dest = dest / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return {
            "key": norm_key(issue_key),
            "attachment_id": attachment_id,
            "output_path": str(dest.resolve()),
            "size_bytes": len(resp.content),
        }


def emit_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")
    sys.stdout.flush()


def handle_cli_error(err: CLIError, json_mode: bool) -> NoReturn:
    if json_mode:
        emit_json(
            {
                "error": err.error_code,
                "detail": err.message,
                "status_code": err.status_code,
            }
        )
    else:
        stderr_console.print(f"[bold red]Error ({err.error_code}):[/bold red] {err.message}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Rich TUI Renderers
# ---------------------------------------------------------------------------


def fmt_status(status: str | None) -> str:
    s = (status or "unknown").lower()
    style = STATUS_STYLES.get(s, "white")
    return f"[{style}]{s}[/{style}]"


def fmt_priority(priority: str | None) -> str:
    p = (priority or "none").lower()
    style = PRIORITY_STYLES.get(p, "white")
    return f"[{style}]{p}[/{style}]"


def fmt_type(issue_type: str | None) -> str:
    t = (issue_type or "task").lower()
    icon = TYPE_ICONS.get(t, "•")
    return f"{icon} {t}"


def fmt_minutes(total_minutes: int | float | None) -> str:
    mins = int(total_minutes or 0)
    if mins <= 0:
        return "0m"
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def render_issue_table(
    issues: list[dict[str, Any]],
    *,
    title: str = "Issues",
    total: int | None = None,
    snippets: dict[str, str] | None = None,
) -> None:
    count_str = f"{len(issues)}" if total is None or total == len(issues) else f"{len(issues)} of {total}"
    if not issues:
        stdout_console.print(f"[dim]{title}: 0 issues matched.[/dim]")
        return

    table = Table(title=f"{title} ({count_str})", box=box.ROUNDED, expand=False)
    table.add_column("Key", style="bold cyan", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Priority", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Assignee", style="green", no_wrap=True)
    table.add_column("Labels", style="magenta")
    table.add_column("Pts", justify="right", no_wrap=True)

    for iss in issues:
        key = iss.get("key", "")
        blocked_badge = " [bold red][BLOCKED][/bold red]" if iss.get("is_blocked") else ""
        claim_badge = f" [yellow](⚡{iss['claimed_by']})[/yellow]" if iss.get("claimed_by") else ""
        title_line = f"{iss.get('title', '')}{blocked_badge}{claim_badge}"
        if snippets and key in snippets and snippets[key]:
            title_line += f"\n[dim]{snippets[key]}[/dim]"
        pts = iss.get("story_points")
        pts_str = f"{pts:g}" if isinstance(pts, (int, float)) else "-"
        labels_str = ", ".join(iss.get("labels") or []) or "-"
        assignee_str = iss.get("assignee") or "[dim]-[/dim]"
        table.add_row(
            key,
            fmt_type(iss.get("type")),
            fmt_status(iss.get("status")),
            fmt_priority(iss.get("priority")),
            title_line,
            assignee_str,
            labels_str,
            pts_str,
        )
    stdout_console.print(table)


def render_issue_detail(issue: dict[str, Any]) -> None:
    key = issue.get("key", "ISSUE")
    title = issue.get("title", "")
    blocked_flag = " [bold white on red] BLOCKED [/bold white on red]" if issue.get("is_blocked") else ""
    header = (
        f"[bold cyan]{key}[/bold cyan]  [bold]{title}[/bold]{blocked_flag}\n"
        f"Type: {fmt_type(issue.get('type'))}   Status: {fmt_status(issue.get('status'))}   "
        f"Priority: {fmt_priority(issue.get('priority'))}   Points: [bold]{issue.get('story_points') or '-'}[/bold]"
    )

    meta = Table.grid(padding=(0, 3))
    meta.add_column(style="bold dim")
    meta.add_column()
    meta.add_column(style="bold dim")
    meta.add_column()
    meta.add_row("Assignee:", issue.get("assignee") or "-", "Reporter:", issue.get("reporter") or "-")
    meta.add_row("Claimed By:", issue.get("claimed_by") or "-", "Resolution:", issue.get("resolution") or "-")
    meta.add_row("Epic / Parent:", f"{issue.get('epic_id') or '-'} / {issue.get('parent_id') or '-'}", "Sprint / Milestone:", f"{issue.get('sprint_id') or '-'} / {issue.get('milestone_id') or '-'}")
    meta.add_row("Labels:", ", ".join(issue.get("labels") or []) or "-", "Components:", ", ".join(issue.get("components") or []) or "-")
    meta.add_row("Due Date:", issue.get("due_date") or "-", "Worklogged:", fmt_minutes(issue.get("total_worklog_minutes")))
    meta.add_row("Created:", issue.get("created_at") or "-", "Updated:", issue.get("updated_at") or "-")
    if issue.get("unresolved_blockers"):
        meta.add_row("Open Blockers:", f"[bold red]{', '.join(issue['unresolved_blockers'])}[/bold red]", "Subtasks:", ", ".join(issue.get("subtask_keys") or []) or "-")
    elif issue.get("subtask_keys"):
        meta.add_row("Subtasks:", ", ".join(issue["subtask_keys"]), "Watchers:", ", ".join(issue.get("watchers") or []) or "-")

    stdout_console.print(Panel(meta, title=header, title_align="left", border_style="cyan"))

    desc = (issue.get("description") or "").strip()
    if desc:
        stdout_console.print(Panel(Markdown(desc), title="Description", title_align="left", border_style="dim"))

    all_links = issue.get("all_links") or issue.get("links") or []
    if all_links:
        lt = Table(title="Links", box=box.SIMPLE_HEAD)
        lt.add_column("ID", style="dim")
        lt.add_column("Relation", style="yellow")
        lt.add_column("Source -> Target", style="cyan")
        for lnk in all_links:
            lt.add_row(str(lnk.get("id", "-")), str(lnk.get("link_type", "")), f"{lnk.get('source_key', key)} -> {lnk.get('target_key', '')}")
        stdout_console.print(lt)

    attachments = issue.get("attachments") or []
    if attachments:
        at = Table(title="Attachments", box=box.SIMPLE_HEAD)
        at.add_column("ID", style="bold cyan")
        at.add_column("Filename")
        at.add_column("Size", justify="right")
        at.add_column("By", style="green")
        at.add_column("Description", style="dim")
        for att in attachments:
            at.add_row(
                str(att.get("id", "")),
                str(att.get("filename", "")),
                f"{att.get('size_bytes', 0)} B",
                str(att.get("uploaded_by", "-")),
                str(att.get("description", "")),
            )
        stdout_console.print(at)

    worklogs = issue.get("worklogs") or []
    if worklogs:
        wt = Table(title=f"Worklogs (Total: {fmt_minutes(issue.get('total_worklog_minutes'))})", box=box.SIMPLE_HEAD)
        wt.add_column("ID", style="dim")
        wt.add_column("Author", style="green")
        wt.add_column("Duration", justify="right")
        wt.add_column("Note")
        for wl in worklogs:
            wt.add_row(str(wl.get("id", "")), str(wl.get("author", "-")), fmt_minutes(wl.get("minutes")), str(wl.get("note", "")))
        stdout_console.print(wt)

    comments = issue.get("comments") or []
    if comments:
        ct = Table(title=f"Comments ({len(comments)})", box=box.SIMPLE_HEAD, show_lines=True)
        ct.add_column("ID", style="dim", no_wrap=True)
        ct.add_column("Author", style="green", no_wrap=True)
        ct.add_column("Created", style="dim", no_wrap=True)
        ct.add_column("Comment")
        for cmt in comments:
            ct.add_row(str(cmt.get("id", "")), str(cmt.get("author", "-")), str(cmt.get("created_at", "")), str(cmt.get("body", "")))
        stdout_console.print(ct)


def render_board(data: dict[str, Any]) -> None:
    columns = data.get("columns") or []
    total = data.get("total_issues", 0)
    if not columns:
        stdout_console.print("[dim]Board has no columns configured.[/dim]")
        return

    active_cols = [c for c in columns if c.get("issues") or c.get("status") in ("todo", "in_progress", "in_review", "blocked", "done")]
    if not active_cols:
        active_cols = columns

    board_table = Table(title=f"Kanban Board ({total} issues)", box=box.ROUNDED, show_lines=True, expand=True)
    for col in active_cols:
        st = col.get("status", "")
        cnt = col.get("count", len(col.get("issues") or []))
        wip = col.get("wip_limit")
        pts = col.get("points", 0)
        wip_str = f"/{wip}" if wip else ""
        over_wip = bool(wip and cnt > wip)
        hdr_style = "bold red" if over_wip else STATUS_STYLES.get(st, "bold")
        pts_txt = f" · {pts:g}pt" if pts else ""
        board_table.add_column(f"[{hdr_style}]{st.upper()} ({cnt}{wip_str}{pts_txt})[/{hdr_style}]", ratio=1)

    max_rows = max((len(c.get("issues") or []) for c in active_cols), default=0)
    if max_rows == 0:
        board_table.add_row(*["[dim]Empty[/dim]" for _ in active_cols])
    else:
        for idx in range(max_rows):
            row_cells: list[str] = []
            for col in active_cols:
                col_issues = col.get("issues") or []
                if idx < len(col_issues):
                    iss = col_issues[idx]
                    assignee = f" @{iss['assignee']}" if iss.get("assignee") else ""
                    blk = " [bold red]![/bold red]" if iss.get("is_blocked") else ""
                    cell = (
                        f"[bold cyan]{iss.get('key')}[/bold cyan]{blk} {fmt_priority(iss.get('priority'))}[green]{assignee}[/green]\n"
                        f"{iss.get('title', '')}"
                    )
                    row_cells.append(cell)
                else:
                    row_cells.append("")
            board_table.add_row(*row_cells)
    stdout_console.print(board_table)


def render_graph(data: dict[str, Any]) -> None:
    nodes = {n["key"]: n for n in (data.get("nodes") or []) if "key" in n}
    edges = data.get("edges") or []
    ready_keys = data.get("ready_keys") or []
    blocked_keys = data.get("blocked_keys") or []

    tree = Tree(f"[bold cyan]Dependency Graph[/bold cyan] ({len(nodes)} nodes, {len(edges)} edges)")
    outgoing: dict[str, list[tuple[str, str]]] = {}
    inbound_blocks: set[str] = set()
    for e in edges:
        src, tgt, etype = e.get("source", ""), e.get("target", ""), e.get("type", "relates_to")
        outgoing.setdefault(src, []).append((etype, tgt))
        if etype == "blocks":
            inbound_blocks.add(tgt)

    roots = [k for k in sorted(nodes) if k in outgoing and k not in inbound_blocks]
    if not roots:
        roots = [k for k in sorted(nodes) if k in outgoing]

    def add_children(branch: Tree, key: str, seen: set[str]) -> None:
        for etype, tgt in outgoing.get(key, []):
            tnode = nodes.get(tgt, {"key": tgt, "title": "", "status": "?"})
            label = f"[yellow]--({etype})-->[/yellow] [bold cyan]{tgt}[/bold cyan] ({fmt_status(tnode.get('status'))}) {tnode.get('title', '')}"
            if tgt in seen:
                branch.add(label + " [dim](cycle/ref)[/dim]")
            else:
                sub = branch.add(label)
                add_children(sub, tgt, seen | {tgt})

    if roots:
        for rk in roots:
            rnode = nodes.get(rk, {"key": rk, "title": "", "status": "?"})
            b = tree.add(f"[bold cyan]{rk}[/bold cyan] ({fmt_status(rnode.get('status'))}) {rnode.get('title', '')}")
            add_children(b, rk, {rk})
    else:
        tree.add("[dim]No explicit dependency links recorded yet.[/dim]")

    stdout_console.print(tree)
    stdout_console.print(
        f"[bold green]Ready ({len(ready_keys)}):[/bold green] {', '.join(ready_keys) or '-'}   "
        f"[bold red]Blocked ({len(blocked_keys)}):[/bold red] {', '.join(blocked_keys) or '-'}"
    )


# ---------------------------------------------------------------------------
# Command Handlers — System, Daemon, UI, Search, Board & Issues
# ---------------------------------------------------------------------------


def can_open_gui_browser() -> bool:
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def cmd_init(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, health = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        patch_body: dict[str, Any] = {}
        if args.key:
            patch_body["project_key"] = args.key.strip().upper()
        if args.name:
            patch_body["project_name"] = args.name.strip()
        cfg = client.request("PATCH", "/api/v1/config", json_body=patch_body) if patch_body else client.request("GET", "/api/v1/config")

    result = {
        "status": "initialized",
        "repo_root": str(repo_root),
        "issues_dir": str(repo_root / "itrack-issues"),
        "daemon_url": base_url,
        "daemon_pid": health.get("pid"),
        "config": cfg,
    }
    if json_mode:
        emit_json(result)
        return
    stdout_console.print(
        Panel(
            f"[bold green]Workspace Ready[/bold green]\n"
            f"Project: [bold cyan]{cfg.get('project_name')}[/bold cyan] ([bold]{cfg.get('project_key')}[/bold])\n"
            f"Storage: `{repo_root / 'itrack-issues'}`\n"
            f"Daemon:  {base_url} (PID {health.get('pid')})",
            title="itrack init",
            border_style="green",
        )
    )


def cmd_ui(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    req_port = int(args.port or 0)
    probed = probe_daemon(repo_root)
    if probed is not None and req_port > 0 and int(probed[1].get("port") or 0) != req_port:
        stop_daemon(repo_root)
        base_url, health = spawn_daemon(repo_root, requested_port=req_port)
    elif probed is not None:
        base_url, health = probed
    else:
        base_url, health = spawn_daemon(repo_root, requested_port=req_port)

    url = f"{base_url}/"
    opened = False
    if not args.no_open and can_open_gui_browser():
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False

    payload = {
        "url": url,
        "host": health.get("host", "127.0.0.1"),
        "port": health.get("port"),
        "pid": health.get("pid"),
        "repo_root": str(repo_root),
        "opened_browser": opened,
    }
    if json_mode:
        emit_json(payload)
        return
    browser_note = "Opened in default browser" if opened else "Headless / --no-open (open URL in browser)"
    stdout_console.print(
        Panel(
            f"[bold cyan]Web UI URL:[/bold cyan] [underline bold]{url}[/underline bold]\n"
            f"[bold]Daemon PID:[/bold] {payload['pid']}   [bold]Port:[/bold] {payload['port']}\n"
            f"[bold]Repository:[/bold] {repo_root}\n"
            f"[dim]{browser_note}[/dim]",
            title="itrack Web UI",
            border_style="cyan",
        )
    )


def cmd_daemon(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    sub = args.daemon_action
    if sub == "status":
        probed = probe_daemon(repo_root)
        if probed is None:
            data = {"running": False, "status": "stopped", "repo_root": str(repo_root)}
            if json_mode:
                emit_json(data)
            else:
                stdout_console.print(f"[yellow]Daemon is stopped[/yellow] for `{repo_root}`.")
            return
        base_url, health = probed
        data = {"running": True, **health}
        if json_mode:
            emit_json(data)
        else:
            stdout_console.print(
                f"[bold green]Daemon running[/bold green] at [underline]{base_url}[/underline] "
                f"(PID {health.get('pid')}, issues={health.get('issue_count', 0)}, uptime={health.get('uptime_seconds', 0):.1f}s)"
            )
        return

    if sub == "stop":
        res = stop_daemon(repo_root)
        if json_mode:
            emit_json(res)
        elif res.get("stopped"):
            stdout_console.print(f"[green]Stopped daemon[/green] (PID {res.get('pid')}).")
        else:
            stdout_console.print("[dim]Daemon was not running.[/dim]")
        return

    if sub == "start":
        req_port = int(getattr(args, "port", 0) or 0)
        probed = probe_daemon(repo_root)
        if probed is not None:
            cur_port = int(probed[1].get("port") or 0)
            if req_port == 0 or cur_port == req_port:
                res = {"running": True, "already_running": True, **probed[1]}
                if json_mode:
                    emit_json(res)
                else:
                    stdout_console.print(f"[green]Daemon already running[/green] at {probed[0]} (PID {probed[1].get('pid')}).")
                return
            stop_daemon(repo_root)
        base_url, health = spawn_daemon(repo_root, requested_port=req_port)
        res = {"running": True, "already_running": False, **health}
        if json_mode:
            emit_json(res)
        else:
            stdout_console.print(f"[bold green]Started daemon[/bold green] at {base_url} (PID {health.get('pid')}).")
        return

    if sub == "restart":
        req_port = int(getattr(args, "port", 0) or 0)
        stop_daemon(repo_root)
        base_url, health = spawn_daemon(repo_root, requested_port=req_port)
        res = {"running": True, "restarted": True, **health}
        if json_mode:
            emit_json(res)
        else:
            stdout_console.print(f"[bold green]Restarted daemon[/bold green] at {base_url} (PID {health.get('pid')}).")
        return

    if sub == "logs":
        n_lines = int(getattr(args, "lines", 50) or 50)
        log_path = get_daemon_log_path(repo_root)
        content = read_log_tail(repo_root, lines=n_lines)
        if json_mode:
            emit_json({
                "log_path": str(log_path),
                "lines": content.splitlines() if content else [],
                "content": content,
            })
        else:
            if content:
                stdout_console.print(Panel(content, title=f"Daemon Logs ({log_path})", border_style="dim"))
            else:
                stdout_console.print(f"[dim]No daemon logs at {log_path}.[/dim]")
        return


def cmd_stats(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/stats")
    if json_mode:
        emit_json(data)
        return

    summary = (
        f"[bold]Total:[/bold] {data.get('total_issues', 0)}   "
        f"[cyan]Open:[/cyan] {data.get('open_count', 0)}   "
        f"[green]Done:[/green] {data.get('done_count', 0)}   "
        f"[red]Blocked:[/red] {data.get('blocked_count', 0)}   "
        f"[bold yellow]Ready:[/bold yellow] {data.get('ready_count', 0)}\n"
        f"[bold]Story Points:[/bold] {data.get('completed_points', 0):g} / {data.get('total_points', 0):g} completed"
    )
    stdout_console.print(Panel(summary, title="Workspace Statistics", border_style="cyan"))

    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("By Status")
    t.add_column("By Priority")
    t.add_column("By Type")
    t.add_column("By Assignee")
    fmt_map = lambda d: "\n".join(f"{k}: [bold]{v}[/bold]" for k, v in (d or {}).items()) or "-"
    t.add_row(
        fmt_map(data.get("by_status")),
        fmt_map(data.get("by_priority")),
        fmt_map(data.get("by_type")),
        fmt_map(data.get("by_assignee")),
    )
    stdout_console.print(t)


def cmd_reindex(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        data = client.request("POST", "/api/v1/reindex")
    if json_mode:
        emit_json(data)
        return
    stdout_console.print(
        f"[bold green]Reindexed[/bold green] {data.get('indexed_issues', 0)} issues in {data.get('duration_ms', 0):.1f} ms."
    )


def cmd_config(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if args.config_action == "show":
            data = client.request("GET", "/api/v1/config")
        else:
            body: dict[str, Any] = {}
            if args.key:
                body["project_key"] = args.key.strip().upper()
            if args.name:
                body["project_name"] = args.name.strip()
            if args.default_type:
                body["default_type"] = args.default_type.strip().lower()
            if args.default_priority:
                body["default_priority"] = args.default_priority.strip().lower()
            if args.wip_limits:
                try:
                    body["wip_limits"] = json.loads(args.wip_limits)
                except json.JSONDecodeError as exc:
                    raise CLIError(f"Invalid JSON for --wip-limits: {exc}", "invalid_json", 400) from exc
            data = client.request("PATCH", "/api/v1/config", json_body=body)

    if json_mode:
        emit_json(data)
        return
    t = Table(title="Project Configuration", box=box.ROUNDED)
    t.add_column("Field", style="bold cyan")
    t.add_column("Value")
    for k in ("project_key", "project_name", "default_type", "default_priority", "statuses", "priorities", "issue_types", "wip_limits", "next_seq"):
        if k in data:
            val = data[k]
            t.add_row(k, json.dumps(val) if isinstance(val, (list, dict)) else str(val))
    stdout_console.print(t)


def cmd_workflow(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if args.workflow_action == "show":
            data = client.request("GET", "/api/v1/workflows")
        else:
            raw = args.file
            p = Path(raw).expanduser()
            text_payload = p.read_text(encoding="utf-8") if p.exists() and p.is_file() else raw
            try:
                wf_body = json.loads(text_payload)
            except json.JSONDecodeError as exc:
                raise CLIError(f"Invalid workflow JSON: {exc}", "invalid_json", 400) from exc
            data = client.request("PUT", "/api/v1/workflows", json_body=wf_body)

    if json_mode:
        emit_json(data)
        return
    t = Table(title="Workflow State Machine", box=box.ROUNDED)
    t.add_column("From Status", style="bold cyan")
    t.add_column("Allowed Transitions")
    transitions = data.get("transitions") or {}
    for st in data.get("statuses") or list(transitions.keys()):
        targets = ", ".join(transitions.get(st, [])) or "[dim]none[/dim]"
        t.add_row(fmt_status(st), targets)
    stdout_console.print(t)


def cmd_export(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        bundle = client.request("GET", "/api/v1/export")
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        if json_mode:
            emit_json({"status": "exported", "output_path": str(out_path), "issue_count": len(bundle.get("issues") or [])})
        else:
            stdout_console.print(f"[bold green]Exported workspace[/bold green] to `{out_path}`.")
        return
    emit_json(bundle)


def cmd_import(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    in_path = Path(args.file).expanduser().resolve()
    if not in_path.exists():
        raise CLIError(f"Import file not found: '{in_path}'", "file_not_found", 400)
    try:
        bundle = json.loads(in_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CLIError(f"Invalid JSON in '{in_path}': {exc}", "invalid_json", 400) from exc

    # Support importing either `{bundle: ...}` or a direct export bundle
    payload_bundle = bundle["bundle"] if isinstance(bundle, dict) and "bundle" in bundle and isinstance(bundle["bundle"], dict) else bundle
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        res = client.request("POST", "/api/v1/import", json_body={"mode": args.mode, "bundle": payload_bundle})
    if json_mode:
        emit_json(res)
        return
    stdout_console.print(
        f"[bold green]Imported workspace[/bold green] (mode={args.mode}): "
        f"{res.get('imported_issues', 0)} issues, {res.get('imported_sprints', 0)} sprints, {res.get('imported_milestones', 0)} milestones."
    )


def cmd_search(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    params: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.is_iql:
        params["iql"] = args.query_str
    else:
        params["q"] = args.query_str
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/search", params=params)
    if json_mode:
        emit_json(data)
        return
    render_issue_table(
        data.get("issues") or [],
        title=f"Search Results: {args.query_str}",
        total=data.get("total"),
        snippets=data.get("snippets"),
    )


def cmd_iql(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if args.validate:
            data = client.request("POST", "/api/v1/iql/validate", json_body={"iql": args.expr})
            if json_mode:
                emit_json(data)
                if not data.get("valid"):
                    sys.exit(1)
                return
            if data.get("valid"):
                stdout_console.print(f"[bold green]Valid iQL:[/bold green] `{args.expr}`")
            else:
                raise CLIError(f"Invalid iQL: {data.get('error')} (pos {data.get('position')})", "invalid_iql", 400)
            return

        body: dict[str, Any] = {
            "iql": args.expr,
            "limit": args.limit,
            "offset": args.offset,
            "current_user": default_actor(repo_root),
        }
        data = client.request("POST", "/api/v1/iql/query", json_body=body)
    if json_mode:
        emit_json(data)
        return
    render_issue_table(data.get("issues") or [], title=f"iQL: {args.expr}", total=data.get("total"))


def cmd_board(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    params = {
        "sprint_id": norm_opt_key(args.sprint),
        "epic_id": norm_opt_key(args.epic),
        "assignee": args.assignee,
        "iql": args.iql,
    }
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/board", params=params)
    if json_mode:
        emit_json(data)
        return
    render_board(data)


def cmd_graph(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        data = client.request("GET", "/api/v1/graph/dependencies")
    if json_mode:
        emit_json(data)
        return
    render_graph(data)


def cmd_issue(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    act = args.issue_action
    base_url, _ = ensure_daemon(repo_root)
    actor = getattr(args, "actor", None) or default_actor(repo_root)

    with ITrackClient(base_url) as client:
        if act == "create":
            body: dict[str, Any] = {"title": args.title, "actor": actor}
            if args.description is not None:
                body["description"] = args.description
            if args.type:
                body["type"] = args.type.lower()
            if args.status:
                body["status"] = args.status.lower()
            if args.priority:
                body["priority"] = args.priority.lower()
            if args.assignee:
                body["assignee"] = None if args.assignee.lower() in ("none", "unassigned") else args.assignee
            body["reporter"] = args.reporter or actor
            if args.labels is not None:
                body["labels"] = split_csv(args.labels) or []
            if args.components is not None:
                body["components"] = split_csv(args.components) or []
            if args.epic:
                body["epic_id"] = norm_opt_key(args.epic)
            if args.parent:
                body["parent_id"] = norm_opt_key(args.parent)
            if args.sprint:
                body["sprint_id"] = norm_opt_key(args.sprint)
            if args.milestone:
                body["milestone_id"] = norm_opt_key(args.milestone)
            if args.points is not None:
                body["story_points"] = args.points
            if args.due:
                body["due_date"] = args.due
            if getattr(args, "environment", None):
                body["environment"] = args.environment

            issue = client.request("POST", "/api/v1/issues", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Created[/bold green] [bold cyan]{issue.get('key')}[/bold cyan]: "
                    f"{issue.get('title')} ({fmt_type(issue.get('type'))}, {fmt_priority(issue.get('priority'))})"
                )
            return

        if act == "list":
            params = {
                "iql": args.iql,
                "q": args.query,
                "status": args.status.lower() if args.status else None,
                "priority": args.priority.lower() if args.priority else None,
                "type": args.type.lower() if args.type else None,
                "assignee": args.assignee,
                "label": args.label,
                "component": args.component,
                "epic_id": norm_opt_key(args.epic) if args.epic else None,
                "parent_id": norm_opt_key(args.parent) if args.parent else None,
                "sprint_id": norm_opt_key(args.sprint) if args.sprint else None,
                "milestone_id": norm_opt_key(args.milestone) if args.milestone else None,
                "sort": getattr(args, "sort", None),
                "limit": args.limit,
                "offset": args.offset,
            }
            data = client.request("GET", "/api/v1/issues", params=params)
            if json_mode:
                emit_json(data)
            else:
                render_issue_table(data.get("issues") or [], title="Issues", total=data.get("total"))
            return

        if act == "ready":
            params = {
                "assignee": args.assignee,
                "sprint_id": norm_opt_key(args.sprint) if args.sprint else None,
                "limit": args.limit,
            }
            data = client.request("GET", "/api/v1/issues/ready", params=params)
            if json_mode:
                emit_json(data)
            else:
                render_issue_table(data.get("issues") or [], title="Ready Issues (Unblocked)", total=data.get("total"))
            return

        if act == "show":
            key = norm_key(args.key)
            issue = client.request("GET", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}")
            if json_mode:
                emit_json(issue)
            else:
                render_issue_detail(issue)
            return

        if act == "update":
            key = norm_key(args.key)
            ukey = urllib.parse.quote(key, safe="")
            body = {"actor": actor}
            if args.title is not None:
                body["title"] = args.title
            if args.description is not None:
                body["description"] = args.description
            if args.type is not None:
                body["type"] = args.type.lower()
            if args.status is not None:
                body["status"] = args.status.lower()
            if args.priority is not None:
                body["priority"] = args.priority.lower()
            if args.assignee is not None:
                body["assignee"] = None if args.assignee.lower() in ("none", "null", "unassigned") else args.assignee
            if args.components is not None:
                body["components"] = split_csv(args.components) or []
            if args.epic is not None:
                body["epic_id"] = norm_opt_key(args.epic)
            if args.parent is not None:
                body["parent_id"] = norm_opt_key(args.parent)
            if args.sprint is not None:
                body["sprint_id"] = norm_opt_key(args.sprint)
            if args.milestone is not None:
                body["milestone_id"] = norm_opt_key(args.milestone)
            if args.points is not None:
                body["story_points"] = args.points
            if args.due is not None:
                body["due_date"] = None if args.due.lower() in ("none", "null") else args.due
            if args.resolution is not None:
                body["resolution"] = None if args.resolution.lower() in ("none", "null") else args.resolution

            if args.labels is not None:
                body["labels"] = split_csv(args.labels) or []
            else:
                if args.add_label:
                    add_list: list[str] = []
                    for lb in args.add_label:
                        for item in split_csv(lb) or []:
                            if item not in add_list:
                                add_list.append(item)
                    if add_list:
                        body["add_labels"] = add_list
                if args.remove_label:
                    rem_list: list[str] = []
                    for lb in args.remove_label:
                        for item in split_csv(lb) or []:
                            if item not in rem_list:
                                rem_list.append(item)
                    if rem_list:
                        body["remove_labels"] = rem_list

            issue = client.request("PATCH", f"/api/v1/issues/{ukey}", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(f"[bold green]Updated[/bold green] [bold cyan]{issue.get('key')}[/bold cyan]: {issue.get('title')}")
            return

        if act == "delete":
            key = norm_key(args.key)
            res = client.request("DELETE", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[bold yellow]Deleted[/bold yellow] issue [bold cyan]{key}[/bold cyan].")
            return

        if act == "transition":
            key = norm_key(args.key)
            body = {
                "status": args.status.lower(),
                "resolution": getattr(args, "resolution", None),
                "comment": getattr(args, "comment", None),
                "force": bool(getattr(args, "force", False)),
                "actor": actor,
            }
            issue = client.request("POST", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/transition", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Transitioned[/bold green] [bold cyan]{issue.get('key')}[/bold cyan] -> {fmt_status(issue.get('status'))}"
                )
            return

        if act == "assign":
            key = norm_key(args.key)
            raw_u = args.username.strip()
            assignee = None if raw_u.lower() in ("none", "null", "unassigned", "-") else raw_u
            issue = client.request(
                "POST",
                f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/assign",
                json_body={"assignee": assignee, "actor": actor},
            )
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Assigned[/bold green] [bold cyan]{issue.get('key')}[/bold cyan] -> [green]{issue.get('assignee') or 'unassigned'}[/green]"
                )
            return

        if act == "clone":
            key = norm_key(args.key)
            body = {
                "include_subtasks": bool(args.include_subtasks),
                "actor": actor,
            }
            if getattr(args, "title_prefix", None):
                body["title_prefix"] = args.title_prefix
            issue = client.request("POST", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/clone", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Cloned[/bold green] {key} -> [bold cyan]{issue.get('key')}[/bold cyan]: {issue.get('title')}"
                )
            return

        if act == "claim":
            key = norm_key(args.key)
            agent_name = args.agent or actor
            body = {"agent": agent_name, "note": args.note, "force": bool(args.force)}
            issue = client.request("POST", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/claim", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Claimed[/bold green] [bold cyan]{issue.get('key')}[/bold cyan] for agent [bold yellow]{agent_name}[/bold yellow] ({fmt_status(issue.get('status'))})"
                )
            return

        if act == "release":
            key = norm_key(args.key)
            body = {
                "agent": args.agent,
                "status": args.status.lower() if args.status else None,
                "note": args.note,
            }
            issue = client.request("POST", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/release", json_body=body)
            if json_mode:
                emit_json(issue)
            else:
                stdout_console.print(
                    f"[bold green]Released[/bold green] [bold cyan]{issue.get('key')}[/bold cyan] (status: {fmt_status(issue.get('status'))})"
                )
            return

        if act == "subtasks":
            key = norm_key(args.key)
            data = client.request("GET", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/subtasks")
            if json_mode:
                emit_json(data)
            else:
                render_issue_table(
                    data.get("subtasks") or [],
                    title=f"Subtasks of {key} ({data.get('completed_count', 0)}/{data.get('total_count', 0)} done)",
                )
            return

        if act == "subtask-add":
            key = norm_key(args.key)
            body = {
                "title": args.title,
                "description": args.description,
                "priority": args.priority.lower() if args.priority else None,
                "assignee": args.assignee,
                "story_points": args.points,
                "actor": actor,
            }
            sub_issue = client.request(
                "POST",
                f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/subtasks",
                json_body={k: v for k, v in body.items() if v is not None},
            )
            if json_mode:
                emit_json(sub_issue)
            else:
                stdout_console.print(
                    f"[bold green]Created subtask[/bold green] [bold cyan]{sub_issue.get('key')}[/bold cyan] under {key}: {sub_issue.get('title')}"
                )
            return

        if act == "history":
            key = norm_key(args.key)
            data = client.request("GET", f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/history")
            if json_mode:
                emit_json(data)
            else:
                hist = data.get("history") or []
                t = Table(title=f"History for {key} ({len(hist)} events)", box=box.SIMPLE_HEAD)
                t.add_column("Timestamp", style="dim", no_wrap=True)
                t.add_column("Actor", style="green", no_wrap=True)
                t.add_column("Action", style="yellow", no_wrap=True)
                t.add_column("Changes")
                for ev in hist:
                    t.add_row(
                        str(ev.get("timestamp", "")),
                        str(ev.get("actor", "")),
                        str(ev.get("action", "")),
                        json.dumps(ev.get("changes") or {}),
                    )
                stdout_console.print(t)
            return

        if act == "bulk":
            keys = [norm_key(k) for k in (split_csv(args.keys) or [])]
            if not keys:
                raise CLIError("Provide at least one issue key via --keys K1,K2.", "invalid_arguments", 400)
            fields: dict[str, Any] = {}
            if args.status:
                fields["status"] = args.status.lower()
            if args.priority:
                fields["priority"] = args.priority.lower()
            if args.assignee is not None:
                fields["assignee"] = None if args.assignee.lower() in ("none", "unassigned") else args.assignee
            if args.label:
                fields["label"] = args.label
                fields["labels"] = split_csv(args.label) or []
            if args.sprint is not None:
                fields["sprint_id"] = norm_opt_key(args.sprint)

            res = client.request(
                "POST",
                "/api/v1/issues/bulk",
                json_body={"keys": keys, "action": args.action, "fields": fields, "actor": actor},
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Bulk {args.action}:[/bold green] updated={len(res.get('updated') or [])}, "
                    f"deleted={len(res.get('deleted') or [])}, errors={len(res.get('errors') or [])}"
                )
                for err_item in res.get("errors") or []:
                    stderr_console.print(f"  [red]• {err_item}[/red]")
            return

        if act == "watch":
            key = norm_key(args.key)
            res = client.request(
                "POST",
                f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/watchers",
                json_body={"username": args.username},
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[green]Added watcher[/green] `{args.username}` on {key}. Watchers: {', '.join(res.get('watchers') or [])}")
            return

        if act == "unwatch":
            key = norm_key(args.key)
            res = client.request(
                "DELETE",
                f"/api/v1/issues/{urllib.parse.quote(key, safe='')}/watchers/{urllib.parse.quote(args.username, safe='')}",
            )
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Removed watcher[/yellow] `{args.username}` from {key}.")
            return


# ---------------------------------------------------------------------------
# Command Handlers — Comments, Attachments, Links, Worklogs & Agile Resources
# ---------------------------------------------------------------------------


def cmd_comment(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    actor = default_actor(repo_root)
    key = norm_key(args.key)
    ukey = urllib.parse.quote(key, safe="")

    with ITrackClient(base_url) as client:
        if args.comment_action == "list":
            data = client.request("GET", f"/api/v1/issues/{ukey}/comments")
            if json_mode:
                emit_json(data)
            else:
                comments = data.get("comments") or []
                t = Table(title=f"Comments on {key} ({len(comments)})", box=box.ROUNDED, show_lines=True)
                t.add_column("ID", style="bold cyan", no_wrap=True)
                t.add_column("Author", style="green", no_wrap=True)
                t.add_column("Updated", style="dim", no_wrap=True)
                t.add_column("Body")
                for c in comments:
                    t.add_row(str(c.get("id", "")), str(c.get("author", "")), str(c.get("updated_at", "")), str(c.get("body", "")))
                stdout_console.print(t)
            return

        if args.comment_action == "add":
            cmt = client.request(
                "POST",
                f"/api/v1/issues/{ukey}/comments",
                json_body={"body": args.body, "author": args.author or actor},
            )
            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(f"[bold green]Added comment[/bold green] [cyan]{cmt.get('id')}[/cyan] on {key}.")
            return

        if args.comment_action == "edit":
            cid = urllib.parse.quote(args.comment_id.strip(), safe="")
            cmt = client.request(
                "PATCH",
                f"/api/v1/issues/{ukey}/comments/{cid}",
                json_body={"body": args.body, "actor": actor},
            )
            if json_mode:
                emit_json(cmt)
            else:
                stdout_console.print(f"[bold green]Updated comment[/bold green] [cyan]{cmt.get('id')}[/cyan] on {key}.")
            return

        if args.comment_action == "delete":
            cid = urllib.parse.quote(args.comment_id.strip(), safe="")
            res = client.request("DELETE", f"/api/v1/issues/{ukey}/comments/{cid}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted comment[/yellow] `{args.comment_id}` from {key}.")
            return


def cmd_attach(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    actor = default_actor(repo_root)
    key = norm_key(args.key)
    ukey = urllib.parse.quote(key, safe="")

    with ITrackClient(base_url) as client:
        if args.attach_action == "list":
            data = client.request("GET", f"/api/v1/issues/{ukey}/attachments")
            if json_mode:
                emit_json(data)
            else:
                atts = data.get("attachments") or []
                t = Table(title=f"Attachments on {key} ({len(atts)})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Filename")
                t.add_column("MIME", style="dim")
                t.add_column("Size", justify="right")
                t.add_column("By", style="green")
                t.add_column("Description")
                for a in atts:
                    t.add_row(
                        str(a.get("id", "")),
                        str(a.get("filename", "")),
                        str(a.get("mime_type", "")),
                        f"{a.get('size_bytes', 0)} B",
                        str(a.get("uploaded_by", "")),
                        str(a.get("description", "")),
                    )
                stdout_console.print(t)
            return

        if args.attach_action == "upload":
            fpath = Path(args.file_path).expanduser()
            att = client.upload_attachment(
                key,
                fpath,
                uploaded_by=args.by or actor,
                description=args.description or "",
            )
            if json_mode:
                emit_json(att)
            else:
                stdout_console.print(
                    f"[bold green]Uploaded[/bold green] [cyan]{att.get('id')}[/cyan] (`{att.get('filename')}`, {att.get('size_bytes', 0)} B) to {key}."
                )
            return

        if args.attach_action == "download":
            out_p = Path(args.output).expanduser() if args.output else None
            att_id = norm_key(args.attachment_id) if args.attachment_id.lower().startswith("att-") else args.attachment_id
            res = client.download_attachment(key, att_id, out_p)
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(
                    f"[bold green]Downloaded[/bold green] `{att_id}` -> `{res['output_path']}` ({res['size_bytes']} B)."
                )
            return

        if args.attach_action == "delete":
            att_id = norm_key(args.attachment_id) if args.attachment_id.lower().startswith("att-") else args.attachment_id
            res = client.request("DELETE", f"/api/v1/issues/{ukey}/attachments/{urllib.parse.quote(att_id, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted attachment[/yellow] `{att_id}` from {key}.")
            return


def cmd_link(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    actor = default_actor(repo_root)
    key = norm_key(args.key)
    ukey = urllib.parse.quote(key, safe="")

    with ITrackClient(base_url) as client:
        if args.link_action == "list":
            data = client.request("GET", f"/api/v1/issues/{ukey}/links")
            if json_mode:
                emit_json(data)
            else:
                links = data.get("links") or []
                t = Table(title=f"Links on {key} ({len(links)})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Source", style="cyan")
                t.add_column("Type", style="yellow")
                t.add_column("Target", style="cyan")
                for lnk in links:
                    t.add_row(
                        str(lnk.get("id", "")),
                        str(lnk.get("source_key", key)),
                        str(lnk.get("link_type", "")),
                        str(lnk.get("target_key", "")),
                    )
                stdout_console.print(t)
            return

        if args.link_action == "add":
            target = norm_key(args.target_key)
            lnk = client.request(
                "POST",
                f"/api/v1/issues/{ukey}/links",
                json_body={"target_key": target, "link_type": args.link_type.lower(), "actor": actor},
            )
            if json_mode:
                emit_json(lnk)
            else:
                stdout_console.print(
                    f"[bold green]Linked[/bold green] [cyan]{key}[/cyan] --({lnk.get('link_type')})--> [cyan]{target}[/cyan] ([dim]{lnk.get('id')}[/dim])."
                )
            return

        if args.link_action == "remove":
            lid = urllib.parse.quote(args.link_id.strip(), safe="")
            res = client.request("DELETE", f"/api/v1/issues/{ukey}/links/{lid}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Removed link[/yellow] `{args.link_id}` from {key}.")
            return


def cmd_worklog(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    actor = default_actor(repo_root)
    key = norm_key(args.key)
    ukey = urllib.parse.quote(key, safe="")

    with ITrackClient(base_url) as client:
        if args.worklog_action == "list":
            data = client.request("GET", f"/api/v1/issues/{ukey}/worklogs")
            if json_mode:
                emit_json(data)
            else:
                wls = data.get("worklogs") or []
                t = Table(
                    title=f"Worklogs on {key} (Total: {fmt_minutes(data.get('total_minutes'))})",
                    box=box.ROUNDED,
                )
                t.add_column("ID", style="bold cyan")
                t.add_column("Author", style="green")
                t.add_column("Minutes", justify="right")
                t.add_column("Note")
                t.add_column("Created", style="dim")
                for w in wls:
                    t.add_row(
                        str(w.get("id", "")),
                        str(w.get("author", "")),
                        fmt_minutes(w.get("minutes")),
                        str(w.get("note", "")),
                        str(w.get("created_at", "")),
                    )
                stdout_console.print(t)
            return

        if args.worklog_action == "add":
            raw_dur = args.duration.strip()
            body: dict[str, Any] = {
                "author": args.author or actor,
                "note": args.note or "",
            }
            if raw_dur.isdigit():
                body["minutes"] = int(raw_dur)
            else:
                body["duration"] = raw_dur
            wl = client.request("POST", f"/api/v1/issues/{ukey}/worklogs", json_body=body)
            if json_mode:
                emit_json(wl)
            else:
                stdout_console.print(
                    f"[bold green]Logged[/bold green] {fmt_minutes(wl.get('minutes'))} on {key} ([cyan]{wl.get('id')}[/cyan])."
                )
            return

        if args.worklog_action == "delete":
            wid = urllib.parse.quote(args.worklog_id.strip(), safe="")
            res = client.request("DELETE", f"/api/v1/issues/{ukey}/worklogs/{wid}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted worklog[/yellow] `{args.worklog_id}` from {key}.")
            return


def cmd_sprint(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    act = args.sprint_action

    with ITrackClient(base_url) as client:
        if act == "list":
            params = {"state": args.state.lower() if args.state else None}
            data = client.request("GET", "/api/v1/sprints", params=params)
            if json_mode:
                emit_json(data)
            else:
                sprints = data.get("sprints") or []
                t = Table(title=f"Sprints ({len(sprints)})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Name", style="bold")
                t.add_column("State", style="yellow")
                t.add_column("Dates")
                t.add_column("Goal")
                for sp in sprints:
                    sid = sp.get("id") or sp.get("key") or ""
                    t.add_row(
                        str(sid),
                        str(sp.get("name", "")),
                        str(sp.get("state", "")),
                        f"{sp.get('start_date') or '-'} -> {sp.get('end_date') or '-'}",
                        str(sp.get("goal") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "create":
            body = {
                "name": args.name,
                "goal": args.goal,
                "start_date": args.start,
                "end_date": args.end,
            }
            sp = client.request("POST", "/api/v1/sprints", json_body={k: v for k, v in body.items() if v is not None})
            if json_mode:
                emit_json(sp)
            else:
                sid = sp.get("id") or sp.get("key")
                stdout_console.print(f"[bold green]Created sprint[/bold green] [bold cyan]{sid}[/bold cyan]: {sp.get('name')}")
            return

        if act == "show":
            sid = norm_key(args.sprint_id)
            data = client.request("GET", f"/api/v1/sprints/{urllib.parse.quote(sid, safe='')}")
            if json_mode:
                emit_json(data)
            else:
                sp = data.get("sprint") or {}
                m = data.get("metrics") or {}
                stdout_console.print(
                    Panel(
                        f"[bold cyan]{sp.get('id') or sid}[/bold cyan] — [bold]{sp.get('name')}[/bold] ({sp.get('state')})\n"
                        f"Goal: {sp.get('goal') or '-'}\n"
                        f"Dates: {sp.get('start_date') or '-'} -> {sp.get('end_date') or '-'}\n"
                        f"Progress: {m.get('done_issues', 0)}/{m.get('total_issues', 0)} issues "
                        f"({m.get('done_points', 0):g}/{m.get('total_points', 0):g} pts, {m.get('progress_pct', 0):.1f}%)",
                        title="Sprint Overview",
                        border_style="cyan",
                    )
                )
                render_issue_table(data.get("issues") or [], title=f"Sprint {sid} Issues")
            return

        if act == "update":
            sid = norm_key(args.sprint_id)
            body: dict[str, Any] = {}
            if args.name is not None:
                body["name"] = args.name
            if args.goal is not None:
                body["goal"] = args.goal
            if args.start is not None:
                body["start_date"] = args.start
            if args.end is not None:
                body["end_date"] = args.end
            if getattr(args, "state", None) is not None:
                body["state"] = args.state.lower()
            sp = client.request("PATCH", f"/api/v1/sprints/{urllib.parse.quote(sid, safe='')}", json_body=body)
            if json_mode:
                emit_json(sp)
            else:
                stdout_console.print(f"[bold green]Updated sprint[/bold green] [cyan]{sid}[/cyan].")
            return

        if act == "start":
            sid = norm_key(args.sprint_id)
            body = {k: v for k, v in {"start_date": args.start, "end_date": args.end}.items() if v is not None}
            sp = client.request("POST", f"/api/v1/sprints/{urllib.parse.quote(sid, safe='')}/start", json_body=body)
            if json_mode:
                emit_json(sp)
            else:
                stdout_console.print(f"[bold green]Started sprint[/bold green] [cyan]{sid}[/cyan].")
            return

        if act == "complete":
            sid = norm_key(args.sprint_id)
            rollover = norm_opt_key(args.rollover)
            res = client.request(
                "POST",
                f"/api/v1/sprints/{urllib.parse.quote(sid, safe='')}/complete",
                json_body={"rollover_to": rollover},
            )
            if json_mode:
                emit_json(res)
            else:
                rolled = res.get("rolled_over_keys") or []
                stdout_console.print(
                    f"[bold green]Completed sprint[/bold green] [cyan]{sid}[/cyan] (rolled over {len(rolled)} issues -> {rollover or 'backlog'})."
                )
            return

        if act == "delete":
            sid = norm_key(args.sprint_id)
            res = client.request("DELETE", f"/api/v1/sprints/{urllib.parse.quote(sid, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted sprint[/yellow] `{sid}`.")
            return


def cmd_epic(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    actor = default_actor(repo_root)
    with ITrackClient(base_url) as client:
        if args.epic_action == "list":
            data = client.request("GET", "/api/v1/epics")
            if json_mode:
                emit_json(data)
            else:
                epics = data.get("epics") or []
                t = Table(title=f"Epics ({len(epics)})", box=box.ROUNDED)
                t.add_column("Key", style="bold cyan")
                t.add_column("Title", style="bold")
                t.add_column("Status")
                t.add_column("Priority")
                t.add_column("Progress", justify="right")
                t.add_column("Points", justify="right")
                for ep in epics:
                    iss = ep.get("issue") or {}
                    t.add_row(
                        str(iss.get("key", "")),
                        str(iss.get("title", "")),
                        fmt_status(iss.get("status")),
                        fmt_priority(iss.get("priority")),
                        f"{ep.get('done_count', 0)}/{ep.get('child_count', 0)} ({ep.get('progress_pct', 0):.0f}%)",
                        f"{ep.get('done_points', 0):g}/{ep.get('total_points', 0):g}",
                    )
                stdout_console.print(t)
            return

        if args.epic_action == "create":
            body: dict[str, Any] = {
                "title": args.title,
                "type": "epic",
                "actor": actor,
                "reporter": actor,
            }
            if args.description is not None:
                body["description"] = args.description
            if args.priority:
                body["priority"] = args.priority.lower()
            if args.assignee:
                body["assignee"] = args.assignee
            iss = client.request("POST", "/api/v1/issues", json_body=body)
            if json_mode:
                emit_json(iss)
            else:
                stdout_console.print(f"[bold green]Created epic[/bold green] [bold cyan]{iss.get('key')}[/bold cyan]: {iss.get('title')}")
            return


def cmd_milestone(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    act = args.milestone_action
    with ITrackClient(base_url) as client:
        if act == "list":
            data = client.request("GET", "/api/v1/milestones")
            if json_mode:
                emit_json(data)
            else:
                ms = data.get("milestones") or []
                t = Table(title=f"Milestones ({len(ms)})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Name", style="bold")
                t.add_column("Status", style="yellow")
                t.add_column("Target Date")
                t.add_column("Description")
                for m in ms:
                    t.add_row(
                        str(m.get("id") or m.get("key") or ""),
                        str(m.get("name", "")),
                        str(m.get("status", "")),
                        str(m.get("target_date") or "-"),
                        str(m.get("description") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "create":
            body = {"name": args.name, "description": args.description, "target_date": args.target}
            m = client.request("POST", "/api/v1/milestones", json_body={k: v for k, v in body.items() if v is not None})
            if json_mode:
                emit_json(m)
            else:
                stdout_console.print(f"[bold green]Created milestone[/bold green] [bold cyan]{m.get('id')}[/bold cyan]: {m.get('name')}")
            return

        if act == "update":
            mid = norm_key(args.milestone_id)
            body = {
                k: v
                for k, v in {
                    "name": args.name,
                    "description": args.description,
                    "target_date": args.target,
                    "status": args.status.lower() if args.status else None,
                }.items()
                if v is not None
            }
            m = client.request("PATCH", f"/api/v1/milestones/{urllib.parse.quote(mid, safe='')}", json_body=body)
            if json_mode:
                emit_json(m)
            else:
                stdout_console.print(f"[bold green]Updated milestone[/bold green] [cyan]{mid}[/cyan].")
            return

        if act == "delete":
            mid = norm_key(args.milestone_id)
            res = client.request("DELETE", f"/api/v1/milestones/{urllib.parse.quote(mid, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted milestone[/yellow] `{mid}`.")
            return


def cmd_label(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if args.label_action == "list":
            data = client.request("GET", "/api/v1/labels")
            if json_mode:
                emit_json(data)
            else:
                labels = data.get("labels") or []
                t = Table(title=f"Labels ({len(labels)})", box=box.ROUNDED)
                t.add_column("Name", style="bold magenta")
                t.add_column("Color")
                t.add_column("Description")
                for lb in labels:
                    t.add_row(str(lb.get("name", "")), str(lb.get("color", "")), str(lb.get("description", "")))
                stdout_console.print(t)
            return

        if args.label_action == "add":
            body = {"name": args.name, "color": args.color, "description": args.description}
            lb = client.request("POST", "/api/v1/labels", json_body={k: v for k, v in body.items() if v is not None})
            if json_mode:
                emit_json(lb)
            else:
                stdout_console.print(f"[bold green]Added label[/bold green] [magenta]{lb.get('name')}[/magenta].")
            return

        if args.label_action == "delete":
            res = client.request("DELETE", f"/api/v1/labels/{urllib.parse.quote(args.name, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted label[/yellow] `{args.name}`.")
            return


def cmd_component(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    with ITrackClient(base_url) as client:
        if args.component_action == "list":
            data = client.request("GET", "/api/v1/components")
            if json_mode:
                emit_json(data)
            else:
                comps = data.get("components") or []
                t = Table(title=f"Components ({len(comps)})", box=box.ROUNDED)
                t.add_column("Name", style="bold cyan")
                t.add_column("Lead", style="green")
                t.add_column("Description")
                for cp in comps:
                    t.add_row(str(cp.get("name", "")), str(cp.get("lead") or "-"), str(cp.get("description") or "-"))
                stdout_console.print(t)
            return

        if args.component_action == "add":
            body = {"name": args.name, "lead": args.lead, "description": args.description}
            cp = client.request("POST", "/api/v1/components", json_body={k: v for k, v in body.items() if v is not None})
            if json_mode:
                emit_json(cp)
            else:
                stdout_console.print(f"[bold green]Added component[/bold green] [cyan]{cp.get('name')}[/cyan].")
            return

        if args.component_action == "delete":
            res = client.request("DELETE", f"/api/v1/components/{urllib.parse.quote(args.name, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted component[/yellow] `{args.name}`.")
            return


def cmd_user(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    act = args.user_action
    with ITrackClient(base_url) as client:
        if act == "list":
            data = client.request("GET", "/api/v1/users")
            if json_mode:
                emit_json(data)
            else:
                users = data.get("users") or []
                t = Table(title=f"Users & Agents ({len(users)})", box=box.ROUNDED)
                t.add_column("Username", style="bold green")
                t.add_column("Display Name")
                t.add_column("Role")
                t.add_column("Agent?", justify="center")
                t.add_column("Email", style="dim")
                for u in users:
                    t.add_row(
                        str(u.get("username", "")),
                        str(u.get("display_name") or "-"),
                        str(u.get("role") or "-"),
                        "🤖" if u.get("is_agent") else "👤",
                        str(u.get("email") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "add":
            body = {
                "username": args.username,
                "display_name": args.name,
                "email": args.email,
                "role": args.role,
                "is_agent": bool(args.agent),
                "avatar_color": getattr(args, "avatar_color", None),
            }
            u = client.request("POST", "/api/v1/users", json_body={k: v for k, v in body.items() if v is not None})
            if json_mode:
                emit_json(u)
            else:
                stdout_console.print(f"[bold green]Registered user[/bold green] [green]{u.get('username')}[/green].")
            return

        if act == "show":
            data = client.request("GET", f"/api/v1/users/{urllib.parse.quote(args.username, safe='')}")
            if json_mode:
                emit_json(data)
            else:
                u = data.get("user") or {}
                stdout_console.print(
                    Panel(
                        f"Username: [bold green]{u.get('username')}[/bold green] ({'Agent' if u.get('is_agent') else 'Human'})\n"
                        f"Display:  {u.get('display_name') or '-'}\n"
                        f"Role:     {u.get('role') or '-'}   Email: {u.get('email') or '-'}\n"
                        f"Assigned: {data.get('assigned_issues', 0)}   In Progress: {data.get('in_progress_issues', 0)}",
                        title="User Profile",
                        border_style="green",
                    )
                )
            return

        if act == "update":
            body = {
                k: v
                for k, v in {
                    "display_name": args.name,
                    "email": args.email,
                    "role": args.role,
                    "is_agent": True if args.agent else (False if args.human else None),
                    "avatar_color": getattr(args, "avatar_color", None),
                }.items()
                if v is not None
            }
            u = client.request("PATCH", f"/api/v1/users/{urllib.parse.quote(args.username, safe='')}", json_body=body)
            if json_mode:
                emit_json(u)
            else:
                stdout_console.print(f"[bold green]Updated user[/bold green] [green]{u.get('username')}[/green].")
            return

        if act == "delete":
            res = client.request("DELETE", f"/api/v1/users/{urllib.parse.quote(args.username, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted user[/yellow] `{args.username}`.")
            return


def cmd_view(args: argparse.Namespace, repo_root: Path, json_mode: bool) -> None:
    base_url, _ = ensure_daemon(repo_root)
    act = args.view_action
    with ITrackClient(base_url) as client:
        if act == "list":
            data = client.request("GET", "/api/v1/views")
            if json_mode:
                emit_json(data)
            else:
                views = data.get("views") or []
                t = Table(title=f"Saved Views ({len(views)})", box=box.ROUNDED)
                t.add_column("ID", style="bold cyan")
                t.add_column("Name", style="bold")
                t.add_column("Layout")
                t.add_column("iQL", style="yellow")
                t.add_column("Description", style="dim")
                for v in views:
                    t.add_row(
                        str(v.get("id", "")),
                        str(v.get("name", "")),
                        str(v.get("layout", "list")),
                        str(v.get("iql", "")),
                        str(v.get("description") or "-"),
                    )
                stdout_console.print(t)
            return

        if act == "create":
            body = {
                "name": args.name,
                "iql": args.iql,
                "layout": args.layout,
                "description": args.description,
                "icon": getattr(args, "icon", None),
            }
            v = client.request("POST", "/api/v1/views", json_body={k: val for k, val in body.items() if val is not None})
            if json_mode:
                emit_json(v)
            else:
                stdout_console.print(f"[bold green]Created view[/bold green] [cyan]{v.get('id')}[/cyan]: {v.get('name')}")
            return

        if act == "update":
            vid = norm_key(args.view_id)
            body = {
                k: val
                for k, val in {
                    "name": args.name,
                    "iql": args.iql,
                    "layout": args.layout,
                    "description": args.description,
                    "icon": getattr(args, "icon", None),
                }.items()
                if val is not None
            }
            v = client.request("PATCH", f"/api/v1/views/{urllib.parse.quote(vid, safe='')}", json_body=body)
            if json_mode:
                emit_json(v)
            else:
                stdout_console.print(f"[bold green]Updated view[/bold green] [cyan]{vid}[/cyan].")
            return

        if act == "delete":
            vid = norm_key(args.view_id)
            res = client.request("DELETE", f"/api/v1/views/{urllib.parse.quote(vid, safe='')}")
            if json_mode:
                emit_json(res)
            else:
                stdout_console.print(f"[yellow]Deleted view[/yellow] `{vid}`.")
            return


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entrypoint
# ---------------------------------------------------------------------------


class CLIArgumentParser(argparse.ArgumentParser):
    """ArgumentParser subclass raising CLIError on parse failure for clean --json parity."""

    def error(self, message: str) -> NoReturn:
        raise CLIError(message, error_code="invalid_arguments", status_code=400)


def _configure_issue_create_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument("title", help="Issue summary/title")
    p.add_argument("-d", "--description", "--desc", dest="description", help="Markdown description")
    p.add_argument("-t", "--type", help="Issue type (epic, story, task, bug, subtask, spike)")
    p.add_argument("-s", "--status", help="Initial status")
    p.add_argument("-p", "--priority", help="Priority (none, low, medium, high, urgent)")
    p.add_argument("-a", "--assignee", help="Assignee username")
    p.add_argument("-r", "--reporter", help="Reporter username")
    p.add_argument("-l", "--labels", "--label", dest="labels", help="Comma-separated labels")
    p.add_argument("-c", "--components", "--component", dest="components", help="Comma-separated components")
    p.add_argument("--epic", help="Parent epic key (e.g. ITK-1)")
    p.add_argument("--parent", help="Parent issue key")
    p.add_argument("--sprint", help="Sprint ID (e.g. SPR-1)")
    p.add_argument("--milestone", help="Milestone ID (e.g. MIL-1)")
    p.add_argument("--points", type=float, help="Story points estimate")
    p.add_argument("--due", help="Due date (YYYY-MM-DD)")
    p.add_argument("--environment", help="Environment context")
    p.add_argument("--actor", help="Actor username")
    p.set_defaults(issue_action="create", handler=cmd_issue)


def _configure_issue_list_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument("--iql", help="iQL filter expression")
    p.add_argument("-q", "--query", help="Full-text search filter")
    p.add_argument("-s", "--status", help="Filter by status")
    p.add_argument("-p", "--priority", help="Filter by priority")
    p.add_argument("-t", "--type", help="Filter by issue type")
    p.add_argument("-a", "--assignee", help="Filter by assignee")
    p.add_argument("-l", "--label", help="Filter by label")
    p.add_argument("-c", "--component", help="Filter by component")
    p.add_argument("--epic", help="Filter by epic key")
    p.add_argument("--parent", help="Filter by parent key")
    p.add_argument("--sprint", help="Filter by sprint ID")
    p.add_argument("--milestone", help="Filter by milestone ID")
    p.add_argument("--sort", help="Sort clause")
    p.add_argument("--limit", type=int, default=200, help="Max issues (default 200)")
    p.add_argument("--offset", type=int, default=0, help="Pagination offset")
    p.set_defaults(issue_action="list", handler=cmd_issue)


def _configure_issue_ready_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument("-a", "--assignee", help="Filter by assignee")
    p.add_argument("--sprint", help="Filter by sprint ID")
    p.add_argument("--limit", type=int, default=50, help="Max issues (default 50)")
    p.set_defaults(issue_action="ready", handler=cmd_issue)


def _configure_issue_transition_parser(p: argparse.ArgumentParser, fixed_status: str | None = None) -> None:
    p.add_argument("key", help="Issue key (e.g. ITK-1)")
    if fixed_status is None:
        p.add_argument("status", help="Target status")
    else:
        p.set_defaults(status=fixed_status)
    p.add_argument("--resolution", help="Optional resolution (e.g. fixed, wontfix, duplicate)")
    p.add_argument("-m", "--comment", "--message", dest="comment", help="Transition comment")
    p.add_argument("--force", action="store_true", help="Force transition bypassing workflow guard")
    p.add_argument("--actor", help="Actor username")
    p.set_defaults(issue_action="transition", handler=cmd_issue)


def _configure_issue_claim_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument("key", help="Issue key (e.g. ITK-1)")
    p.add_argument("--agent", help="Agent or user identifier claiming this issue")
    p.add_argument("-m", "--note", "--message", dest="note", help="Optional claim note")
    p.add_argument("--force", action="store_true", help="Override existing claim by another agent")
    p.set_defaults(issue_action="claim", handler=cmd_issue)


def _configure_issue_release_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument("key", help="Issue key (e.g. ITK-1)")
    p.add_argument("--agent", help="Agent identifier releasing claim")
    p.add_argument("-s", "--status", help="Target status after release (default todo)")
    p.add_argument("-m", "--note", "--message", dest="note", help="Optional handoff/release note")
    p.set_defaults(issue_action="release", handler=cmd_issue)


def build_parser() -> CLIArgumentParser:
    parser = CLIArgumentParser(
        prog="itrack",
        description="itrack — Git-rooted Jira/Linear-grade CLI, Agent Coordinator & Web UI launcher.",
    )
    parser.add_argument("-V", "--version", action="store_true", help="Show itrack version")
    parser.add_argument("--manual", action="store_true", help="Show complete Agent & Operator Manual")

    sub = parser.add_subparsers(dest="command")

    # manual
    p_manual = sub.add_parser("manual", help="Display Law 3 Agent & Operator Manual")
    p_manual.set_defaults(handler=lambda _a, r, j: render_agent_manual(r, j))

    # init
    p_init = sub.add_parser("init", help="Initialize workspace and optionally configure project key/name")
    p_init.add_argument("--key", help="Project issue key prefix (e.g. ITK)")
    p_init.add_argument("--name", help="Human-readable project name")
    p_init.set_defaults(handler=cmd_init)

    # ui
    p_ui = sub.add_parser("ui", help="Ensure daemon is running and launch embedded Web SPA")
    p_ui.add_argument("--port", type=int, default=0, help="Preferred TCP port (default ephemeral)")
    p_ui.add_argument("--no-open", action="store_true", help="Do not launch desktop web browser")
    p_ui.set_defaults(handler=cmd_ui)

    # daemon / server
    p_daemon = sub.add_parser("daemon", aliases=["server"], help="Manage per-repo background server")
    dsub = p_daemon.add_subparsers(dest="daemon_action", required=True)
    p_dstart = dsub.add_parser("start", help="Start background daemon")
    p_dstart.add_argument("--port", type=int, default=0, help="TCP port (0 for auto ephemeral)")
    dsub.add_parser("stop", help="Stop background daemon")
    p_drestart = dsub.add_parser("restart", help="Restart background daemon")
    p_drestart.add_argument("--port", type=int, default=0, help="TCP port (0 for auto ephemeral)")
    dsub.add_parser("status", help="Show daemon health status")
    p_dlogs = dsub.add_parser("logs", help="Show recent daemon log output")
    p_dlogs.add_argument("-n", "--lines", type=int, default=50, help="Number of tail lines")
    p_daemon.set_defaults(handler=cmd_daemon)

    # stats & reindex
    sub.add_parser("stats", help="Show workspace metrics and distributions").set_defaults(handler=cmd_stats)
    sub.add_parser("reindex", help="Rebuild SQLite + FTS5 index from canonical JSON files").set_defaults(handler=cmd_reindex)

    # config
    p_cfg = sub.add_parser("config", help="Show or update workspace configuration")
    csub = p_cfg.add_subparsers(dest="config_action", required=True)
    csub.add_parser("show", help="Show config.json")
    p_cset = csub.add_parser("set", help="Update config fields")
    p_cset.add_argument("--key", help="Project key prefix")
    p_cset.add_argument("--name", help="Project name")
    p_cset.add_argument("--default-type", help="Default issue type")
    p_cset.add_argument("--default-priority", help="Default priority")
    p_cset.add_argument("--wip-limits", help="JSON object mapping status to WIP limit")
    p_cfg.set_defaults(handler=cmd_config)

    # workflow
    p_wf = sub.add_parser("workflow", help="Show or replace workflow status transitions")
    wfsub = p_wf.add_subparsers(dest="workflow_action", required=True)
    wfsub.add_parser("show", help="Show workflow transitions")
    p_wfset = wfsub.add_parser("set", help="Set workflow transitions from JSON file or inline JSON")
    p_wfset.add_argument("--file", required=True, help="Path to JSON file or inline JSON string")
    p_wf.set_defaults(handler=cmd_workflow)

    # export & import
    p_exp = sub.add_parser("export", help="Export full workspace bundle as JSON")
    p_exp.add_argument("-o", "--output", help="Write export bundle to file")
    p_exp.set_defaults(handler=cmd_export)

    p_imp = sub.add_parser("import", help="Import workspace bundle from JSON file")
    p_imp.add_argument("file", help="Path to JSON bundle")
    p_imp.add_argument("--mode", choices=["merge", "replace"], default="merge", help="Import mode (default merge)")
    p_imp.set_defaults(handler=cmd_import)

    # Top-level issue shortcuts
    _configure_issue_create_parser(sub.add_parser("create", help="Shortcut for `issue create`"))
    _configure_issue_list_parser(sub.add_parser("list", help="Shortcut for `issue list`"))
    p_top_show = sub.add_parser("show", help="Shortcut for `issue show <KEY>`")
    p_top_show.add_argument("key", help="Issue key")
    p_top_show.set_defaults(issue_action="show", handler=cmd_issue)
    _configure_issue_ready_parser(sub.add_parser("ready", help="Shortcut for `issue ready` (unblocked work)"))
    _configure_issue_claim_parser(sub.add_parser("claim", help="Atomically claim an issue for an agent"))
    _configure_issue_release_parser(sub.add_parser("release", help="Release an issue claim"))
    _configure_issue_transition_parser(sub.add_parser("transition", help="Shortcut for `issue transition <KEY> <STATUS>`"))
    _configure_issue_transition_parser(sub.add_parser("move", help="Alias for `issue transition <KEY> <STATUS>`"))
    _configure_issue_transition_parser(sub.add_parser("close", help="Transition issue to `done`"), fixed_status="done")
    _configure_issue_transition_parser(sub.add_parser("reopen", help="Transition issue back to `todo`"), fixed_status="todo")

    # search, iql, board, graph
    p_search = sub.add_parser("search", help="Full-text FTS5 or iQL search")
    p_search.add_argument("query_str", help="Search text or iQL expression")
    p_search.add_argument("--iql", dest="is_iql", action="store_true", help="Interpret query as iQL")
    p_search.add_argument("--limit", type=int, default=50, help="Max results")
    p_search.add_argument("--offset", type=int, default=0, help="Pagination offset")
    p_search.set_defaults(handler=cmd_search)

    p_iql = sub.add_parser("iql", help="Execute or validate an iQL query")
    p_iql.add_argument("expr", help="iQL query string")
    p_iql.add_argument("--validate", action="store_true", help="Validate syntax and output AST without executing")
    p_iql.add_argument("--limit", type=int, default=200, help="Result limit")
    p_iql.add_argument("--offset", type=int, default=0, help="Result offset")
    p_iql.set_defaults(handler=cmd_iql)

    p_board = sub.add_parser("board", help="Render terminal Kanban board")
    p_board.add_argument("--sprint", help="Filter by sprint ID")
    p_board.add_argument("--epic", help="Filter by epic key")
    p_board.add_argument("-a", "--assignee", help="Filter by assignee")
    p_board.add_argument("--iql", help="Filter by iQL expression")
    p_board.set_defaults(handler=cmd_board)

    sub.add_parser("graph", help="Render issue dependency & blocker tree").set_defaults(handler=cmd_graph)

    # issue resource group
    p_issue = sub.add_parser("issue", help="Manage issues, subtasks, claims, transitions & bulk actions")
    isub = p_issue.add_subparsers(dest="issue_action", required=True)
    _configure_issue_create_parser(isub.add_parser("create", help="Create a new issue"))
    _configure_issue_list_parser(isub.add_parser("list", help="List and filter issues"))
    _configure_issue_ready_parser(isub.add_parser("ready", help="List unblocked actionable issues"))

    p_ishow = isub.add_parser("show", help="Show complete details of an issue")
    p_ishow.add_argument("key", help="Issue key")

    p_iupd = isub.add_parser("update", help="Update fields on an issue")
    p_iupd.add_argument("key", help="Issue key")
    p_iupd.add_argument("--title", help="New title")
    p_iupd.add_argument("-d", "--description", "--desc", dest="description", help="New description")
    p_iupd.add_argument("-t", "--type", help="New issue type")
    p_iupd.add_argument("-s", "--status", help="New status")
    p_iupd.add_argument("-p", "--priority", help="New priority")
    p_iupd.add_argument("-a", "--assignee", help="New assignee (or 'none')")
    p_iupd.add_argument("--add-label", action="append", help="Add label(s)")
    p_iupd.add_argument("--remove-label", action="append", help="Remove label(s)")
    p_iupd.add_argument("-l", "--labels", dest="labels", help="Replace all labels (comma-separated)")
    p_iupd.add_argument("-c", "--components", dest="components", help="Replace all components (comma-separated)")
    p_iupd.add_argument("--epic", help="Epic key (or 'none')")
    p_iupd.add_argument("--parent", help="Parent issue key (or 'none')")
    p_iupd.add_argument("--sprint", help="Sprint ID (or 'none')")
    p_iupd.add_argument("--milestone", help="Milestone ID (or 'none')")
    p_iupd.add_argument("--points", type=float, help="Story points")
    p_iupd.add_argument("--due", help="Due date YYYY-MM-DD (or 'none')")
    p_iupd.add_argument("--resolution", help="Resolution")
    p_iupd.add_argument("--actor", help="Actor username")

    p_idel = isub.add_parser("delete", help="Delete an issue")
    p_idel.add_argument("key", help="Issue key")

    _configure_issue_transition_parser(isub.add_parser("transition", aliases=["move"], help="Transition issue status"))
    _configure_issue_transition_parser(isub.add_parser("close", help="Close issue (-> done)"), fixed_status="done")
    _configure_issue_transition_parser(isub.add_parser("reopen", help="Reopen issue (-> todo)"), fixed_status="todo")

    p_iasn = isub.add_parser("assign", help="Assign or unassign an issue")
    p_iasn.add_argument("key", help="Issue key")
    p_iasn.add_argument("username", help="Username or 'none'")
    p_iasn.add_argument("--actor", help="Actor username")

    p_iclone = isub.add_parser("clone", help="Clone an existing issue")
    p_iclone.add_argument("key", help="Issue key to clone")
    p_iclone.add_argument("--include-subtasks", action="store_true", help="Clone child subtasks too")
    p_iclone.add_argument("--title-prefix", help="Prefix for cloned issue title")
    p_iclone.add_argument("--actor", help="Actor username")

    _configure_issue_claim_parser(isub.add_parser("claim", help="Claim issue for an agent"))
    _configure_issue_release_parser(isub.add_parser("release", help="Release issue claim"))

    p_isubs = isub.add_parser("subtasks", help="List subtasks of an issue")
    p_isubs.add_argument("key", help="Parent issue key")

    p_isubadd = isub.add_parser("subtask-add", help="Create a subtask under a parent issue")
    p_isubadd.add_argument("key", help="Parent issue key")
    p_isubadd.add_argument("title", help="Subtask title")
    p_isubadd.add_argument("-d", "--description", "--desc", dest="description", help="Subtask description")
    p_isubadd.add_argument("-p", "--priority", help="Priority")
    p_isubadd.add_argument("-a", "--assignee", help="Assignee")
    p_isubadd.add_argument("--points", type=float, help="Story points")
    p_isubadd.add_argument("--actor", help="Actor username")

    p_ihist = isub.add_parser("history", help="Show audit history of an issue")
    p_ihist.add_argument("key", help="Issue key")

    p_ibulk = isub.add_parser("bulk", help="Perform bulk operations across issue keys")
    p_ibulk.add_argument("--keys", required=True, help="Comma-separated issue keys (e.g. ITK-1,ITK-2)")
    p_ibulk.add_argument(
        "--action",
        required=True,
        choices=["update", "transition", "delete", "label_add", "label_remove", "sprint"],
        help="Bulk action",
    )
    p_ibulk.add_argument("-s", "--status", help="Target status")
    p_ibulk.add_argument("-p", "--priority", help="Target priority")
    p_ibulk.add_argument("-a", "--assignee", help="Target assignee")
    p_ibulk.add_argument("-l", "--label", help="Label name(s)")
    p_ibulk.add_argument("--sprint", help="Sprint ID (or 'none')")
    p_ibulk.add_argument("--actor", help="Actor username")

    p_iwatch = isub.add_parser("watch", help="Add watcher to an issue")
    p_iwatch.add_argument("key", help="Issue key")
    p_iwatch.add_argument("username", help="Username")

    p_iunwatch = isub.add_parser("unwatch", help="Remove watcher from an issue")
    p_iunwatch.add_argument("key", help="Issue key")
    p_iunwatch.add_argument("username", help="Username")
    p_issue.set_defaults(handler=cmd_issue)

    # comment
    p_cmt = sub.add_parser("comment", help="Manage issue comments")
    cmtsub = p_cmt.add_subparsers(dest="comment_action", required=True)
    p_cl = cmtsub.add_parser("list", help="List comments on issue")
    p_cl.add_argument("key", help="Issue key")
    p_ca = cmtsub.add_parser("add", help="Add comment to issue")
    p_ca.add_argument("key", help="Issue key")
    p_ca.add_argument("body", help="Markdown comment body")
    p_ca.add_argument("--author", help="Author username")
    p_ce = cmtsub.add_parser("edit", help="Edit existing comment")
    p_ce.add_argument("key", help="Issue key")
    p_ce.add_argument("comment_id", help="Comment ID")
    p_ce.add_argument("body", help="Updated comment body")
    p_cd = cmtsub.add_parser("delete", help="Delete comment")
    p_cd.add_argument("key", help="Issue key")
    p_cd.add_argument("comment_id", help="Comment ID")
    p_cmt.set_defaults(handler=cmd_comment)

    # attach / attachment
    p_att = sub.add_parser("attach", aliases=["attachment"], help="Manage file attachments on issues")
    attsub = p_att.add_subparsers(dest="attach_action", required=True)
    p_al = attsub.add_parser("list", help="List attachments on issue")
    p_al.add_argument("key", help="Issue key")
    p_au = attsub.add_parser("upload", help="Upload file attachment to issue")
    p_au.add_argument("key", help="Issue key")
    p_au.add_argument("file_path", help="Local path to file")
    p_au.add_argument("--by", help="Uploader username")
    p_au.add_argument("-d", "--description", help="Attachment description")
    p_ad = attsub.add_parser("download", help="Download attachment from issue")
    p_ad.add_argument("key", help="Issue key")
    p_ad.add_argument("attachment_id", help="Attachment ID (e.g. ATT-1)")
    p_ad.add_argument("-o", "--output", help="Destination path")
    p_ax = attsub.add_parser("delete", help="Delete attachment from issue")
    p_ax.add_argument("key", help="Issue key")
    p_ax.add_argument("attachment_id", help="Attachment ID")
    p_att.set_defaults(handler=cmd_attach)

    # link
    p_lnk = sub.add_parser("link", help="Manage typed dependency & relationship links")
    lnksub = p_lnk.add_subparsers(dest="link_action", required=True)
    p_ll = lnksub.add_parser("list", help="List links on issue")
    p_ll.add_argument("key", help="Issue key")
    p_la = lnksub.add_parser("add", help="Create link between two issues")
    p_la.add_argument("key", help="Source issue key")
    p_la.add_argument("link_type", help="Link type (blocks, blocked_by, relates_to, duplicates, clones)")
    p_la.add_argument("target_key", help="Target issue key")
    p_lr = lnksub.add_parser("remove", aliases=["delete"], help="Remove link by ID")
    p_lr.add_argument("key", help="Issue key")
    p_lr.add_argument("link_id", help="Link ID")
    p_lnk.set_defaults(list_or_mod=True, handler=cmd_link)
    # Normalize `delete` alias to `remove`
    p_lr.set_defaults(link_action="remove")

    # worklog
    p_wl = sub.add_parser("worklog", help="Track time spent on issues")
    wlsub = p_wl.add_subparsers(dest="worklog_action", required=True)
    p_wll = wlsub.add_parser("list", help="List worklogs on issue")
    p_wll.add_argument("key", help="Issue key")
    p_wla = wlsub.add_parser("add", help="Add worklog entry (e.g. '1h 30m' or '90')")
    p_wla.add_argument("key", help="Issue key")
    p_wla.add_argument("duration", help="Duration string ('1h 30m') or integer minutes ('90')")
    p_wla.add_argument("-m", "--note", "--message", dest="note", help="Worklog note")
    p_wla.add_argument("--author", help="Author username")
    p_wld = wlsub.add_parser("delete", help="Delete worklog entry")
    p_wld.add_argument("key", help="Issue key")
    p_wld.add_argument("worklog_id", help="Worklog ID")
    p_wl.set_defaults(handler=cmd_worklog)

    # sprint
    p_sp = sub.add_parser("sprint", help="Manage Agile sprints")
    spsub = p_sp.add_subparsers(dest="sprint_action", required=True)
    p_spl = spsub.add_parser("list", help="List sprints")
    p_spl.add_argument("--state", help="Filter by state (planned, active, completed)")
    p_spc = spsub.add_parser("create", help="Create sprint")
    p_spc.add_argument("name", help="Sprint name")
    p_spc.add_argument("--goal", help="Sprint goal")
    p_spc.add_argument("--start", help="Start date (YYYY-MM-DD)")
    p_spc.add_argument("--end", help="End date (YYYY-MM-DD)")
    p_sps = spsub.add_parser("show", help="Show sprint metrics and issues")
    p_sps.add_argument("sprint_id", help="Sprint ID (e.g. SPR-1)")
    p_spu = spsub.add_parser("update", help="Update sprint details")
    p_spu.add_argument("sprint_id", help="Sprint ID")
    p_spu.add_argument("--name", help="New name")
    p_spu.add_argument("--goal", help="New goal")
    p_spu.add_argument("--start", help="Start date")
    p_spu.add_argument("--end", help="End date")
    p_spu.add_argument("--state", help="State")
    p_spgo = spsub.add_parser("start", help="Start sprint")
    p_spgo.add_argument("sprint_id", help="Sprint ID")
    p_spgo.add_argument("--start", help="Start date")
    p_spgo.add_argument("--end", help="End date")
    p_spdone = spsub.add_parser("complete", help="Complete sprint and roll over unfinished work")
    p_spdone.add_argument("sprint_id", help="Sprint ID")
    p_spdone.add_argument("--rollover", help="Target sprint ID or 'backlog'")
    p_spdel = spsub.add_parser("delete", help="Delete sprint")
    p_spdel.add_argument("sprint_id", help="Sprint ID")
    p_sp.set_defaults(handler=cmd_sprint)

    # epic
    p_ep = sub.add_parser("epic", help="List or create epics with rollup progress")
    epsub = p_ep.add_subparsers(dest="epic_action", required=True)
    epsub.add_parser("list", help="List epics and child completion progress")
    p_epc = epsub.add_parser("create", help="Create a new epic")
    p_epc.add_argument("title", help="Epic title")
    p_epc.add_argument("-d", "--description", "--desc", dest="description", help="Epic description")
    p_epc.add_argument("-p", "--priority", help="Priority")
    p_epc.add_argument("-a", "--assignee", help="Assignee")
    p_ep.set_defaults(handler=cmd_epic)

    # milestone
    p_ms = sub.add_parser("milestone", help="Manage release milestones")
    mssub = p_ms.add_subparsers(dest="milestone_action", required=True)
    mssub.add_parser("list", help="List milestones")
    p_msc = mssub.add_parser("create", help="Create milestone")
    p_msc.add_argument("name", help="Milestone name")
    p_msc.add_argument("-d", "--description", "--desc", dest="description", help="Description")
    p_msc.add_argument("--target", help="Target release date (YYYY-MM-DD)")
    p_msu = mssub.add_parser("update", help="Update milestone")
    p_msu.add_argument("milestone_id", help="Milestone ID (e.g. MIL-1)")
    p_msu.add_argument("--name", help="New name")
    p_msu.add_argument("-d", "--description", "--desc", dest="description", help="Description")
    p_msu.add_argument("--target", help="Target date")
    p_msu.add_argument("--status", choices=["unreleased", "released", "archived"], help="Milestone status")
    p_msd = mssub.add_parser("delete", help="Delete milestone")
    p_msd.add_argument("milestone_id", help="Milestone ID")
    p_ms.set_defaults(handler=cmd_milestone)

    # label
    p_lb = sub.add_parser("label", help="Manage project labels")
    lbsub = p_lb.add_subparsers(dest="label_action", required=True)
    lbsub.add_parser("list", help="List labels")
    p_lba = lbsub.add_parser("add", help="Create/update label")
    p_lba.add_argument("name", help="Label name")
    p_lba.add_argument("--color", help="Hex color (e.g. #ef4444)")
    p_lba.add_argument("-d", "--description", help="Description")
    p_lbd = lbsub.add_parser("delete", help="Delete label")
    p_lbd.add_argument("name", help="Label name")
    p_lb.set_defaults(handler=cmd_label)

    # component
    p_cp = sub.add_parser("component", help="Manage system components")
    cpsub = p_cp.add_subparsers(dest="component_action", required=True)
    cpsub.add_parser("list", help="List components")
    p_cpa = cpsub.add_parser("add", help="Create/update component")
    p_cpa.add_argument("name", help="Component name")
    p_cpa.add_argument("--lead", help="Component lead username")
    p_cpa.add_argument("-d", "--description", help="Description")
    p_cpd = cpsub.add_parser("delete", help="Delete component")
    p_cpd.add_argument("name", help="Component name")
    p_cp.set_defaults(handler=cmd_component)

    # user
    p_usr = sub.add_parser("user", help="Manage team members & AI agents")
    usrsub = p_usr.add_subparsers(dest="user_action", required=True)
    usrsub.add_parser("list", help="List users and agents")
    p_ua = usrsub.add_parser("add", help="Register user or AI agent")
    p_ua.add_argument("username", help="Unique username/handle")
    p_ua.add_argument("--name", help="Display name")
    p_ua.add_argument("--email", help="Email address")
    p_ua.add_argument("--role", help="Role (e.g. developer, lead, coding-agent)")
    p_ua.add_argument("--agent", action="store_true", help="Mark account as AI agent")
    p_ua.add_argument("--avatar-color", help="Hex color")
    p_us = usrsub.add_parser("show", help="Show user profile & active workload")
    p_us.add_argument("username", help="Username")
    p_uu = usrsub.add_parser("update", help="Update user/agent profile")
    p_uu.add_argument("username", help="Username")
    p_uu.add_argument("--name", help="Display name")
    p_uu.add_argument("--email", help="Email")
    p_uu.add_argument("--role", help="Role")
    p_uu.add_argument("--agent", action="store_true", help="Mark as AI agent")
    p_uu.add_argument("--human", action="store_true", help="Mark as human user")
    p_uu.add_argument("--avatar-color", help="Hex color")
    p_ud = usrsub.add_parser("delete", help="Delete user")
    p_ud.add_argument("username", help="Username")
    p_usr.set_defaults(handler=cmd_user)

    # view
    p_vw = sub.add_parser("view", help="Manage saved iQL views")
    vwsub = p_vw.add_subparsers(dest="view_action", required=True)
    vwsub.add_parser("list", help="List saved views")
    p_vwc = vwsub.add_parser("create", help="Create saved view")
    p_vwc.add_argument("name", help="View name")
    p_vwc.add_argument("--iql", required=True, help="iQL filter expression")
    p_vwc.add_argument("--layout", choices=["board", "list"], default="list", help="Default view layout")
    p_vwc.add_argument("-d", "--description", help="Description")
    p_vwc.add_argument("--icon", help="Optional icon")
    p_vwu = vwsub.add_parser("update", help="Update saved view")
    p_vwu.add_argument("view_id", help="View ID (e.g. VIEW-1)")
    p_vwu.add_argument("--name", help="New name")
    p_vwu.add_argument("--iql", help="New iQL expression")
    p_vwu.add_argument("--layout", choices=["board", "list"], help="Layout")
    p_vwu.add_argument("-d", "--description", help="Description")
    p_vwu.add_argument("--icon", help="Icon")
    p_vwd = vwsub.add_parser("delete", help="Delete saved view")
    p_vwd.add_argument("view_id", help="View ID")
    p_vw.set_defaults(handler=cmd_view)

    return parser


def main(argv: list[str] | None = None) -> None:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    prelim_json = "--json" in raw_argv
    try:
        json_mode, root_arg, remaining = extract_global_flags(raw_argv)
    except CLIError as err:
        handle_cli_error(err, prelim_json)

    # Law 2: Verify Git repository root before doing anything else (exits with code 2 if invalid).
    repo_root = resolve_and_check_git_root(root_arg, json_mode)

    # Law 3: Zero-Arg Agent Manual (or explicit manual/--manual) — never spawns daemon!
    if not remaining or remaining == ["manual"] or "--manual" in remaining:
        render_agent_manual(repo_root, json_mode)
        return

    if remaining in (["-V"], ["--version"]):
        if json_mode:
            emit_json({"name": "itrack", "version": VERSION})
        else:
            stdout_console.print(f"itrack {VERSION}")
        return

    parser = build_parser()
    try:
        args = parser.parse_args(remaining)
        if getattr(args, "version", False):
            if json_mode:
                emit_json({"name": "itrack", "version": VERSION})
            else:
                stdout_console.print(f"itrack {VERSION}")
            return
        if getattr(args, "manual", False) or not getattr(args, "command", None):
            render_agent_manual(repo_root, json_mode)
            return

        # Normalize `issue move` alias to `transition`
        if getattr(args, "issue_action", None) == "move":
            args.issue_action = "transition"
        if getattr(args, "link_action", None) == "delete":
            args.link_action = "remove"

        handler = getattr(args, "handler", None)
        if handler is None:
            render_agent_manual(repo_root, json_mode)
            return
        handler(args, repo_root, json_mode)
    except CLIError as err:
        handle_cli_error(err, json_mode)


if __name__ == "__main__":
    main()
