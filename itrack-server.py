#!/usr/bin/env python3
"""
itrack-server.py — Local-First File-Locked Issue Tracker, iQL Engine & Offline SPA Server.

Architectural Laws:
  - Law 1: Strict 3-File Repository Root (requirements.txt, itrack.py, itrack-server.py).
  - Law 2: Git-Root Check enforced before touching disk or binding sockets.
  - Canonical truth lives in JSON files under <repo_root>/itrack-issues/ protected by fcntl.flock,
    paired with a rebuildable hybrid SQLite3 + FTS5 index at .runtime/index.db.
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
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

import uvicorn
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

VERSION = "1.0.0"

PRIORITY_RANKS: Dict[str, int] = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "urgent": 4,
    "critical": 4,
}

DEFAULT_STATUSES: List[str] = [
    "backlog",
    "todo",
    "in_progress",
    "in_review",
    "blocked",
    "done",
    "canceled",
]

DEFAULT_TRANSITIONS: Dict[str, List[str]] = {
    "backlog": ["todo", "in_progress", "blocked", "done", "canceled"],
    "todo": ["backlog", "in_progress", "in_review", "blocked", "done", "canceled"],
    "in_progress": ["backlog", "todo", "in_review", "blocked", "done", "canceled"],
    "in_review": ["todo", "in_progress", "blocked", "done", "canceled"],
    "blocked": ["backlog", "todo", "in_progress", "in_review", "done", "canceled"],
    "done": ["backlog", "todo", "in_progress"],
    "canceled": ["backlog", "todo"],
}

DEFAULT_PRIORITIES: List[str] = ["none", "low", "medium", "high", "urgent"]
DEFAULT_ISSUE_TYPES: List[str] = ["epic", "story", "task", "bug", "subtask", "spike"]

INVERSE_LINK_TYPES: Dict[str, str] = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "duplicates": "duplicated_by",
    "duplicated_by": "duplicates",
    "relates_to": "relates_to",
    "clones": "cloned_from",
    "cloned_from": "clones",
}

LINK_TYPE_ALIASES: Dict[str, str] = {
    "blocks": "blocks",
    "block": "blocks",
    "blocked_by": "blocked_by",
    "blocked-by": "blocked_by",
    "is_blocked_by": "blocked_by",
    "duplicates": "duplicates",
    "duplicate": "duplicates",
    "duplicated_by": "duplicated_by",
    "duplicated-by": "duplicated_by",
    "relates_to": "relates_to",
    "relates-to": "relates_to",
    "relates": "relates_to",
    "clones": "clones",
    "cloned_from": "cloned_from",
    "cloned-from": "cloned_from",
}


# ============================================================================
# Utility & Law 2 Helpers
# ============================================================================

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def normalize_priority(val: Optional[str], default: str = "medium") -> Tuple[str, int]:
    raw = (val or default).strip().lower()
    if raw == "critical":
        raw = "urgent"
    if raw not in PRIORITY_RANKS:
        raise ValueError(
            f"Invalid priority '{val}'. Expected one of: {', '.join(DEFAULT_PRIORITIES)} (or 'critical')."
        )
    return raw, PRIORITY_RANKS[raw]


def normalize_link_type(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in LINK_TYPE_ALIASES:
        raise ValueError(
            f"Invalid link_type '{raw}'. Expected one of: {', '.join(sorted(INVERSE_LINK_TYPES.keys()))}."
        )
    return LINK_TYPE_ALIASES[cleaned]


def parse_duration_minutes(minutes: Optional[int], duration: Optional[str]) -> int:
    if minutes is not None:
        if int(minutes) <= 0:
            raise ValueError("Worklog minutes must be positive.")
        return int(minutes)
    if not duration or not str(duration).strip():
        raise ValueError("Provide either 'minutes' or 'duration' (e.g. '1h 30m').")
    s = str(duration).strip().lower()
    if re.fullmatch(r"\d+", s):
        val = int(s)
        if val <= 0:
            raise ValueError("Worklog duration must be positive.")
        return val
    tokens = re.findall(r"(\d+(?:\.\d+)?)\s*([wdhm])", s)
    if not tokens:
        raise ValueError(f"Cannot parse duration '{duration}'. Expected format like '1h 30m', '2d', '45m', or '90'.")
    reconstructed = "".join(f"{num}{unit}" for num, unit in tokens)
    stripped_s = re.sub(r"\s+", "", s)
    if reconstructed != stripped_s:
        # Double-check float preservation
        check = re.sub(r"(\d+(?:\.\d+)?)\s*([wdhm])", "", s).strip()
        if check:
            raise ValueError(f"Invalid trailing text '{check}' in duration '{duration}'.")
    mult = {"w": 5 * 8 * 60, "d": 8 * 60, "h": 60, "m": 1}
    total = int(round(sum(float(num) * mult[unit] for num, unit in tokens)))
    if total <= 0:
        raise ValueError("Worklog duration must be > 0 minutes.")
    return total


def sanitize_filename(name: Optional[str]) -> str:
    base = Path(name or "attachment.bin").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    if not cleaned:
        cleaned = "attachment.bin"
    return cleaned[:180]


def detect_default_user(repo_root: Path) -> str:
    env_user = os.environ.get("ITRACK_USER")
    if env_user and env_user.strip():
        return env_user.strip()
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
    return os.environ.get("USER") or "system"


def resolve_repo_root(cli_root: Optional[str]) -> Path:
    if cli_root:
        return Path(cli_root).expanduser().resolve()
    env_root = os.environ.get("ITRACK_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def enforce_law2_git_root(repo_root: Path) -> None:
    """Law 2: Verify .git exists in repo_root BEFORE creating any directories or files."""
    if (repo_root / ".git").exists():
        return
    msg = (
        f"Error [Law 2 - Git-Root Check]: '{repo_root}' is not a Git repository root "
        f"(.git not found). Run from the root of a Git repository."
    )
    try:
        if repo_root.exists():
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                top = Path(proc.stdout.strip()).resolve()
                if top != repo_root:
                    msg += f"\nHint: cd {top}"
    except Exception:
        pass
    print(msg, file=sys.stderr, flush=True)
    sys.exit(2)


# ============================================================================
# Recursive-Descent iQL Engine (Lexer -> AST -> Validator -> SQLite/FTS5)
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

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r}, pos={self.pos})"


IQL_KEYWORDS: Set[str] = {
    "AND",
    "OR",
    "NOT",
    "IN",
    "IS",
    "EMPTY",
    "NULL",
    "ORDER",
    "BY",
    "ASC",
    "DESC",
    "LIMIT",
    "OFFSET",
}

FIELD_ALIASES: Dict[str, str] = {
    "key": "key",
    "id": "key",
    "type": "type",
    "issuetype": "type",
    "issue_type": "type",
    "status": "status",
    "state": "status",
    "priority": "priority",
    "assignee": "assignee",
    "owner": "assignee",
    "reporter": "reporter",
    "creator": "reporter",
    "claimed_by": "claimed_by",
    "epic": "epic_id",
    "epic_id": "epic_id",
    "parent": "parent_id",
    "parent_id": "parent_id",
    "sprint": "sprint_id",
    "sprint_id": "sprint_id",
    "milestone": "milestone_id",
    "milestone_id": "milestone_id",
    "release": "milestone_id",
    "label": "labels",
    "labels": "labels",
    "tag": "labels",
    "tags": "labels",
    "component": "components",
    "components": "components",
    "points": "story_points",
    "story_points": "story_points",
    "estimate": "story_points",
    "title": "title",
    "summary": "title",
    "description": "description",
    "body": "description",
    "comment": "comments",
    "comments": "comments",
    "text": "text",
    "q": "text",
    "created": "created_at",
    "created_at": "created_at",
    "updated": "updated_at",
    "updated_at": "updated_at",
    "due": "due_date",
    "due_date": "due_date",
    "resolved": "resolved_at",
    "resolved_at": "resolved_at",
    "started": "started_at",
    "started_at": "started_at",
    "closed": "closed_at",
    "closed_at": "closed_at",
    "blocked": "is_blocked",
    "is_blocked": "is_blocked",
    "resolution": "resolution",
    "environment": "environment",
    "seq": "seq",
}

ORDER_SQL_MAP: Dict[str, str] = {
    "key": "issues.seq",
    "seq": "issues.seq",
    "type": "issues.type",
    "status": "issues.status",
    "priority": "issues.priority_rank",
    "assignee": "issues.assignee",
    "reporter": "issues.reporter",
    "claimed_by": "issues.claimed_by",
    "epic_id": "issues.epic_id",
    "parent_id": "issues.parent_id",
    "sprint_id": "issues.sprint_id",
    "milestone_id": "issues.milestone_id",
    "story_points": "issues.story_points",
    "title": "LOWER(issues.title)",
    "created_at": "issues.created_at",
    "updated_at": "issues.updated_at",
    "due_date": "issues.due_date",
    "resolved_at": "issues.resolved_at",
    "started_at": "issues.started_at",
    "closed_at": "issues.closed_at",
    "resolution": "issues.resolution",
}


class IQLLexer:
    WORD_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@+-.")
    WORD_CONT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@+.:/-")

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.pos = 0

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
            # Two-char operators
            two = s[i : i + 2]
            if two in ("!=", ">=", "<=", "!~", "<>"):
                op_val = "!=" if two == "<>" else two
                tokens.append(Token("OP", op_val, i))
                i += 2
                continue
            # Single-char operators & punctuation
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
            # Quoted string
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
                        esc_map = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
                        buf.append(esc_map.get(nxt, nxt))
                        i += 2
                        continue
                    if c == quote:
                        i += 1
                        closed = True
                        break
                    buf.append(c)
                    i += 1
                if not closed:
                    raise IQLError(f"Unterminated string literal starting at position {start}", start)
                tokens.append(Token("STRING", "".join(buf), start, quoted=True))
                continue
            # Word / identifier / number / relative date / @me
            if ch in self.WORD_START:
                start = i
                i += 1
                while i < n and s[i] in self.WORD_CONT:
                    i += 1
                word = s[start:i]
                upper = word.upper()
                if upper in IQL_KEYWORDS:
                    tokens.append(Token("KW", upper, start))
                else:
                    tokens.append(Token("WORD", word, start))
                continue
            raise IQLError(f"Unexpected character '{ch}' at position {i}", i)
        tokens.append(Token("EOF", "", n))
        return tokens


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
            raise IQLError(f"Expected {kind} but got '{t.value or t.kind}' at position {t.pos}", t.pos)
        if val and t.value != val:
            raise IQLError(f"Expected '{val}' but got '{t.value}' at position {t.pos}", t.pos)
        self.idx += 1
        return t

    def parse(self) -> Dict[str, Any]:
        where_expr: Optional[Dict[str, Any]] = None
        order_by: List[Dict[str, str]] = []
        limit_val: Optional[int] = None
        offset_val: Optional[int] = None

        if self.cur().kind != "EOF" and not self.peek_kw("ORDER") and not self.peek_kw("LIMIT") and not self.peek_kw("OFFSET"):
            where_expr = self.parse_or()

        if self.peek_kw("ORDER"):
            self.consume("KW", "ORDER")
            if not self.peek_kw("BY"):
                t = self.cur()
                raise IQLError(f"Expected 'BY' after 'ORDER' at position {t.pos}", t.pos)
            self.consume("KW", "BY")
            order_by.append(self.parse_order_item())
            while self.cur().kind == "COMMA":
                self.consume("COMMA")
                order_by.append(self.parse_order_item())

        if self.peek_kw("LIMIT"):
            self.consume("KW", "LIMIT")
            num_tok = self.cur()
            if num_tok.kind not in ("WORD", "STRING") or not re.fullmatch(r"\d+", num_tok.value):
                raise IQLError(f"Expected non-negative integer after LIMIT at position {num_tok.pos}", num_tok.pos)
            self.idx += 1
            limit_val = int(num_tok.value)

        if self.peek_kw("OFFSET"):
            self.consume("KW", "OFFSET")
            num_tok = self.cur()
            if num_tok.kind not in ("WORD", "STRING") or not re.fullmatch(r"\d+", num_tok.value):
                raise IQLError(f"Expected non-negative integer after OFFSET at position {num_tok.pos}", num_tok.pos)
            self.idx += 1
            offset_val = int(num_tok.value)

        if self.cur().kind != "EOF":
            t = self.cur()
            raise IQLError(f"Unexpected token '{t.value}' at position {t.pos}", t.pos)

        return {
            "type": "query",
            "where": where_expr,
            "order_by": order_by,
            "limit": limit_val,
            "offset": offset_val,
        }

    def parse_order_item(self) -> Dict[str, str]:
        tok = self.cur()
        if tok.kind not in ("WORD", "KW", "STRING") or not tok.value:
            raise IQLError(f"Expected field name in ORDER BY at position {tok.pos}", tok.pos)
        self.idx += 1
        canonical = self._resolve_field(tok.value, tok.pos)
        if canonical not in ORDER_SQL_MAP:
            raise IQLError(f"Field '{tok.value}' cannot be used in ORDER BY at position {tok.pos}", tok.pos)
        direction = "ASC"
        if self.peek_kw("ASC"):
            self.consume("KW", "ASC")
            direction = "ASC"
        elif self.peek_kw("DESC"):
            self.consume("KW", "DESC")
            direction = "DESC"
        return {"field": canonical, "raw_field": tok.value, "direction": direction}

    def parse_or(self) -> Dict[str, Any]:
        left = self.parse_and()
        children = [left]
        while self.peek_kw("OR"):
            self.consume("KW", "OR")
            children.append(self.parse_and())
        if len(children) == 1:
            return children[0]
        return {"type": "or", "children": children}

    def parse_and(self) -> Dict[str, Any]:
        left = self.parse_not()
        children = [left]
        while self.peek_kw("AND"):
            self.consume("KW", "AND")
            children.append(self.parse_not())
        if len(children) == 1:
            return children[0]
        return {"type": "and", "children": children}

    def parse_not(self) -> Dict[str, Any]:
        if self.peek_kw("NOT"):
            self.consume("KW", "NOT")
            child = self.parse_not()
            return {"type": "not", "child": child}
        if self.cur().kind == "LPAREN":
            lparen = self.consume("LPAREN")
            if self.cur().kind == "RPAREN":
                raise IQLError(f"Empty parentheses at position {lparen.pos}", lparen.pos)
            expr = self.parse_or()
            if self.cur().kind != "RPAREN":
                t = self.cur()
                raise IQLError(f"Expected closing ')' at position {t.pos}", t.pos)
            self.consume("RPAREN")
            return expr
        return self.parse_predicate()

    def _resolve_field(self, raw_field: str, pos: int) -> str:
        low = raw_field.lower()
        if low in FIELD_ALIASES:
            return FIELD_ALIASES[low]
        if low.startswith("cf.") or low.startswith("custom."):
            sub = raw_field.split(".", 1)[1]
            if not sub:
                raise IQLError(f"Invalid custom field '{raw_field}' at position {pos}", pos)
            return f"custom.{sub}"
        raise IQLError(f"Unknown field '{raw_field}' at position {pos}", pos)

    def _parse_value_token(self) -> Tuple[Any, int]:
        t = self.cur()
        if t.kind == "STRING":
            self.idx += 1
            return t.value, t.pos
        if t.kind == "WORD":
            self.idx += 1
            return t.value, t.pos
        if t.kind == "KW" and t.value in ("EMPTY", "NULL"):
            self.idx += 1
            return t.value, t.pos
        raise IQLError(f"Expected value at position {t.pos}, got '{t.value or t.kind}'", t.pos)

    def parse_predicate(self) -> Dict[str, Any]:
        ftok = self.cur()
        if ftok.kind not in ("WORD", "STRING"):
            raise IQLError(f"Expected field name at position {ftok.pos}, got '{ftok.value or ftok.kind}'", ftok.pos)
        self.idx += 1
        canonical = self._resolve_field(ftok.value, ftok.pos)

        # IS [NOT] (EMPTY | NULL)
        if self.peek_kw("IS"):
            is_tok = self.consume("KW", "IS")
            negated = False
            if self.peek_kw("NOT"):
                self.consume("KW", "NOT")
                negated = True
            t = self.cur()
            if t.kind == "KW" and t.value in ("EMPTY", "NULL"):
                self.idx += 1
                if canonical == "is_blocked":
                    raise IQLError(
                        f"Operator 'IS EMPTY' is not supported for boolean field '{ftok.value}' at position {is_tok.pos}; use '= true' or '= false'.",
                        is_tok.pos,
                    )
                if canonical in ("priority", "seq"):
                    raise IQLError(
                        f"Operator 'IS EMPTY' is not supported for required field '{ftok.value}' at position {is_tok.pos}.",
                        is_tok.pos,
                    )
                return {
                    "type": "empty",
                    "field": canonical,
                    "raw_field": ftok.value,
                    "negated": negated,
                }
            raise IQLError(f"Expected EMPTY or NULL after IS at position {t.pos}", t.pos)

        # [NOT] IN (...)
        if self.peek_kw("NOT") or self.peek_kw("IN"):
            op_start = self.cur().pos
            negated = False
            if self.peek_kw("NOT"):
                self.consume("KW", "NOT")
                negated = True
            if not self.peek_kw("IN"):
                t = self.cur()
                raise IQLError(f"Expected 'IN' after 'NOT' at position {t.pos}", t.pos)
            self.consume("KW", "IN")
            if canonical == "is_blocked":
                raise IQLError(
                    f"Operator 'IN' is not supported for boolean field '{ftok.value}' at position {op_start}; use '= true' or '= false'.",
                    op_start,
                )
            self.consume("LPAREN")
            values: List[Any] = []
            if self.cur().kind == "RPAREN":
                t = self.cur()
                raise IQLError(f"IN list cannot be empty at position {t.pos}", t.pos)
            v, vpos = self._parse_value_token()
            self._validate_field_value(canonical, "IN", v, vpos)
            values.append(v)
            while self.cur().kind == "COMMA":
                self.consume("COMMA")
                v, vpos = self._parse_value_token()
                self._validate_field_value(canonical, "IN", v, vpos)
                values.append(v)
            self.consume("RPAREN")
            return {
                "type": "in",
                "field": canonical,
                "raw_field": ftok.value,
                "negated": negated,
                "values": values,
            }

        # Standard binary comparison operator
        optok = self.cur()
        if optok.kind != "OP":
            raise IQLError(
                f"Expected comparison operator (=, !=, >, >=, <, <=, ~, !~, IN, IS) at position {optok.pos}",
                optok.pos,
            )
        self.idx += 1
        val, vpos = self._parse_value_token()
        self._validate_field_value(canonical, optok.value, val, vpos)
        return {
            "type": "cmp",
            "field": canonical,
            "raw_field": ftok.value,
            "op": optok.value,
            "value": val,
        }

    def _validate_field_value(self, field: str, op: str, val: Any, pos: int) -> None:
        sval = str(val)
        if field == "priority":
            low = sval.lower()
            if low not in PRIORITY_RANKS and not re.fullmatch(r"[0-4]", low):
                raise IQLError(
                    f"Unknown priority '{sval}' at position {pos}. Expected none, low, medium, high, urgent.",
                    pos,
                )
            if op in ("~", "!~"):
                raise IQLError(f"Operator '{op}' is not supported for field 'priority' at position {pos}", pos)
        elif field in ("story_points", "seq"):
            if op in ("~", "!~"):
                raise IQLError(f"Operator '{op}' is not supported for numeric field '{field}' at position {pos}", pos)
            try:
                float(sval)
            except ValueError:
                raise IQLError(f"Expected numeric value for '{field}', got '{sval}' at position {pos}", pos)
        elif field == "is_blocked":
            if op not in ("=", "!="):
                raise IQLError(f"Operator '{op}' is not supported for boolean field 'blocked' at position {pos}", pos)
            if sval.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise IQLError(f"Expected boolean true/false for 'blocked', got '{sval}' at position {pos}", pos)
        elif field in ("created_at", "updated_at", "due_date", "resolved_at", "started_at", "closed_at"):
            if op in ("~", "!~"):
                raise IQLError(f"Operator '{op}' is not supported for date field '{field}' at position {pos}", pos)
            # Validate date/relative expression
            resolve_relative_date(sval, date_only=(field == "due_date"), pos=pos)
        elif field in ("text", "comments", "title", "description", "labels", "components"):
            if op in (">", ">=", "<", "<="):
                raise IQLError(f"Operator '{op}' is not supported for field '{field}' at position {pos}", pos)
        elif field.startswith("custom."):
            if op in (">", ">=", "<", "<=", "!~"):
                raise IQLError(f"Operator '{op}' is not supported for custom field '{field}' at position {pos}", pos)


def resolve_relative_date(raw: str, date_only: bool = False, pos: Optional[int] = None) -> str:
    s = raw.strip()
    low = s.lower()
    now = dt.datetime.now(dt.timezone.utc)
    if low == "now":
        return now.strftime("%Y-%m-%d") if date_only else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if low == "today":
        return now.strftime("%Y-%m-%d") if date_only else now.strftime("%Y-%m-%dT00:00:00Z")
    if low == "yesterday":
        target = now - dt.timedelta(days=1)
        return target.strftime("%Y-%m-%d") if date_only else target.strftime("%Y-%m-%dT00:00:00Z")
    if low == "tomorrow":
        target = now + dt.timedelta(days=1)
        return target.strftime("%Y-%m-%d") if date_only else target.strftime("%Y-%m-%dT00:00:00Z")

    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([mhdw])", low)
    if m:
        amt = float(m.group(1))
        unit = m.group(2)
        delta_map = {
            "m": dt.timedelta(minutes=amt),
            "h": dt.timedelta(hours=amt),
            "d": dt.timedelta(days=amt),
            "w": dt.timedelta(weeks=amt),
        }
        target = now + delta_map[unit]
        return target.strftime("%Y-%m-%d") if date_only else target.strftime("%Y-%m-%dT%H:%M:%SZ")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s if date_only else f"{s}T00:00:00Z"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", s):
        if date_only:
            return s[:10]
        cleaned = s.replace(" ", "T")
        if not cleaned.endswith("Z") and "+" not in cleaned[10:] and "-" not in cleaned[10:]:
            cleaned += "Z"
        return cleaned

    raise IQLError(
        f"Invalid date/time expression '{raw}'. Expected YYYY-MM-DD, ISO timestamp, 'today', 'now', or relative offset like '-7d', '+3d', '-24h', '-2w'.",
        pos,
    )


def build_fts_match_expr(raw: str, column: Optional[str] = None) -> Optional[str]:
    """Build a safe SQLite FTS5 MATCH query from arbitrary user text."""
    if not raw or not raw.strip():
        return None
    phrases: List[str] = []
    # Extract double-quoted exact phrases first
    rem = raw
    for pm in re.finditer(r'"([^"]+)"', raw):
        inner = pm.group(1).strip()
        if re.search(r"[A-Za-z0-9]", inner):
            safe_inner = inner.replace('"', '""')
            phrases.append(f'"{safe_inner}"')
    rem = re.sub(r'"[^"]+"', " ", rem)
    # Extract alphanumeric/hyphen tokens
    for tok in rem.split():
        # Split on characters FTS5 treats specially except keep alphanumeric sequences
        sub_tokens = re.findall(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*", tok)
        for st in sub_tokens:
            safe_st = st.replace('"', '""')
            phrases.append(f'"{safe_st}"*')
    if not phrases:
        return None
    joined = " AND ".join(phrases)
    if column:
        return f"{column} : ({joined})"
    return joined


def parse_iql(iql: str) -> Dict[str, Any]:
    lexer = IQLLexer(iql or "")
    tokens = lexer.tokenize()
    parser = IQLParser(tokens)
    return parser.parse()


class IQLCompiler:
    BLOCKED_SQL = """(
        issues.status = 'blocked'
        OR EXISTS (
            SELECT 1 FROM issue_links blk_l
            JOIN issues blk_i ON (
                (blk_l.link_type = 'blocks' AND blk_l.target_key = issues.key AND blk_i.key = blk_l.source_key)
                OR (blk_l.link_type = 'blocked_by' AND blk_l.source_key = issues.key AND blk_i.key = blk_l.target_key)
            )
            WHERE blk_i.status NOT IN ('done', 'canceled')
        )
    )"""

    def __init__(
        self,
        current_user: str,
        active_sprint_ids: List[str],
        sprint_name_to_id: Dict[str, str],
    ) -> None:
        self.current_user = current_user
        self.active_sprint_ids = [s.upper() for s in active_sprint_ids]
        self.sprint_name_to_id = {k.lower(): v.upper() for k, v in sprint_name_to_id.items()}

    def compile_where(self, node: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        if node is None:
            return "1 = 1", []
        ntype = node["type"]
        if ntype == "or":
            parts: List[str] = []
            params: List[Any] = []
            for ch in node["children"]:
                s, p = self.compile_where(ch)
                parts.append(f"({s})")
                params.extend(p)
            return " OR ".join(parts), params
        if ntype == "and":
            parts = []
            params = []
            for ch in node["children"]:
                s, p = self.compile_where(ch)
                parts.append(f"({s})")
                params.extend(p)
            return " AND ".join(parts), params
        if ntype == "not":
            s, p = self.compile_where(node["child"])
            return f"NOT ({s})", p
        if ntype == "empty":
            return self._compile_empty(node["field"], node["negated"])
        if ntype == "in":
            return self._compile_in(node["field"], node["negated"], node["values"])
        if ntype == "cmp":
            return self._compile_cmp(node["field"], node["op"], node["value"])
        raise IQLError(f"Unsupported AST node '{ntype}'")

    def _resolve_user_val(self, val: Any) -> str:
        s = str(val)
        if s.lower() == "@me":
            return self.current_user
        return s

    def _compile_empty(self, field: str, negated: bool) -> Tuple[str, List[Any]]:
        if field == "is_blocked":
            raise IQLError("Operator 'IS EMPTY' is not supported for boolean field 'blocked'; use '= true' or '= false'.")
        if field == "text":
            if negated:
                return (
                    "((issues.title IS NOT NULL AND issues.title != '') "
                    "OR (issues.description IS NOT NULL AND issues.description != '') "
                    "OR (issues.comments_text IS NOT NULL AND issues.comments_text != ''))",
                    [],
                )
            return (
                "((issues.title IS NULL OR issues.title = '') "
                "AND (issues.description IS NULL OR issues.description = '') "
                "AND (issues.comments_text IS NULL OR issues.comments_text = ''))",
                [],
            )
        if field == "labels":
            op = "EXISTS" if negated else "NOT EXISTS"
            return f"{op} (SELECT 1 FROM issue_labels il WHERE il.issue_key = issues.key)", []
        if field == "components":
            op = "EXISTS" if negated else "NOT EXISTS"
            return f"{op} (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key)", []
        if field == "comments":
            if negated:
                return "(issues.comments_text IS NOT NULL AND issues.comments_text != '')", []
            return "(issues.comments_text IS NULL OR issues.comments_text = '')", []
        if field == "story_points":
            return ("issues.story_points IS NOT NULL" if negated else "issues.story_points IS NULL"), []
        if field.startswith("custom."):
            cf_key = field.split(".", 1)[1]
            path = f"$.custom_fields.{cf_key}"
            if negated:
                return "(json_extract(issues.raw_json, ?) IS NOT NULL AND json_extract(issues.raw_json, ?) != '')", [path, path]
            return "(json_extract(issues.raw_json, ?) IS NULL OR json_extract(issues.raw_json, ?) = '')", [path, path]
        col = f"issues.{field}"
        if negated:
            return f"({col} IS NOT NULL AND {col} != '')", []
        return f"({col} IS NULL OR {col} = '')", []

    def _compile_in(self, field: str, negated: bool, values: List[Any]) -> Tuple[str, List[Any]]:
        if field == "is_blocked":
            raise IQLError("Operator 'IN' is not supported for boolean field 'blocked'; use '= true' or '= false'.")
        if field in ("text", "comments"):
            sub_clauses: List[str] = []
            sub_params: List[Any] = []
            for v in values:
                sc, sp = self._compile_cmp(field, "~", v)
                sub_clauses.append(f"({sc})")
                sub_params.extend(sp)
            joined = " OR ".join(sub_clauses)
            return (f"NOT ({joined})" if negated else f"({joined})"), sub_params
        if field.startswith("custom."):
            sub_clauses = []
            sub_params = []
            for v in values:
                sc, sp = self._compile_cmp(field, "=", v)
                sub_clauses.append(f"({sc})")
                sub_params.extend(sp)
            joined = " OR ".join(sub_clauses)
            return (f"NOT ({joined})" if negated else f"({joined})"), sub_params
        if field in ("created_at", "updated_at", "due_date", "resolved_at", "started_at", "closed_at"):
            dates = [resolve_relative_date(str(v), date_only=(field == "due_date")) for v in values]
            placeholders = ", ".join("?" for _ in dates)
            if negated:
                return f"(issues.{field} IS NULL OR issues.{field} NOT IN ({placeholders}))", dates
            return f"issues.{field} IN ({placeholders})", dates
        if field == "labels":
            placeholders = ", ".join("LOWER(?)" for _ in values)
            kw = "NOT EXISTS" if negated else "EXISTS"
            return (
                f"{kw} (SELECT 1 FROM issue_labels il WHERE il.issue_key = issues.key AND LOWER(il.label) IN ({placeholders}))",
                [str(v) for v in values],
            )
        if field == "components":
            placeholders = ", ".join("LOWER(?)" for _ in values)
            kw = "NOT EXISTS" if negated else "EXISTS"
            return (
                f"{kw} (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) IN ({placeholders}))",
                [str(v) for v in values],
            )
        if field == "priority":
            ranks: List[int] = []
            for v in values:
                sv = str(v).lower()
                ranks.append(int(sv) if re.fullmatch(r"[0-4]", sv) else PRIORITY_RANKS[sv])
            placeholders = ", ".join("?" for _ in ranks)
            op = "NOT IN" if negated else "IN"
            return f"issues.priority_rank {op} ({placeholders})", ranks
        if field in ("story_points", "seq"):
            nums = [float(v) for v in values]
            placeholders = ", ".join("?" for _ in nums)
            if negated:
                return f"(issues.{field} IS NULL OR issues.{field} NOT IN ({placeholders}))", nums
            return f"issues.{field} IN ({placeholders})", nums
        if field in ("assignee", "reporter", "claimed_by"):
            resolved = [self._resolve_user_val(v) for v in values]
            placeholders = ", ".join("LOWER(?)" for _ in resolved)
            if negated:
                return f"(issues.{field} IS NULL OR LOWER(issues.{field}) NOT IN ({placeholders}))", resolved
            return f"LOWER(issues.{field}) IN ({placeholders})", resolved
        if field == "sprint_id":
            expanded: List[str] = []
            for v in values:
                sv = str(v)
                if sv.lower() == "active":
                    expanded.extend(self.active_sprint_ids)
                elif sv.lower() in self.sprint_name_to_id:
                    expanded.append(self.sprint_name_to_id[sv.lower()])
                else:
                    expanded.append(sv.upper())
            if not expanded:
                return ("1 = 1" if negated else "1 = 0"), []
            placeholders = ", ".join("?" for _ in expanded)
            if negated:
                return f"(issues.sprint_id IS NULL OR UPPER(issues.sprint_id) NOT IN ({placeholders}))", expanded
            return f"UPPER(issues.sprint_id) IN ({placeholders})", expanded
        # General string columns
        strs = [str(v) for v in values]
        placeholders = ", ".join("LOWER(?)" for _ in strs)
        if negated:
            return f"(issues.{field} IS NULL OR LOWER(issues.{field}) NOT IN ({placeholders}))", strs
        return f"LOWER(issues.{field}) IN ({placeholders})", strs

    def _compile_cmp(self, field: str, op: str, raw_val: Any) -> Tuple[str, List[Any]]:
        sval = str(raw_val)

        if field == "is_blocked":
            truthy = sval.lower() in ("true", "1", "yes")
            if op == "!=":
                truthy = not truthy
            if truthy:
                return self.BLOCKED_SQL, []
            return f"NOT {self.BLOCKED_SQL}", []

        if field == "priority":
            low = sval.lower()
            rank = int(low) if re.fullmatch(r"[0-4]", low) else PRIORITY_RANKS[low]
            return f"issues.priority_rank {op} ?", [rank]

        if field in ("story_points", "seq"):
            num = float(sval)
            if op == "!=":
                return f"(issues.{field} IS NULL OR issues.{field} != ?)", [num]
            return f"(issues.{field} IS NOT NULL AND issues.{field} {op} ?)", [num]

        if field in ("created_at", "updated_at", "due_date", "resolved_at", "started_at", "closed_at"):
            resolved_dt = resolve_relative_date(sval, date_only=(field == "due_date"))
            if op == "!=":
                return f"(issues.{field} IS NULL OR issues.{field} != ?)", [resolved_dt]
            return f"(issues.{field} IS NOT NULL AND issues.{field} {op} ?)", [resolved_dt]

        if field == "labels":
            if op == "=":
                return "EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_key = issues.key AND LOWER(il.label) = LOWER(?))", [sval]
            if op == "!=":
                return "NOT EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_key = issues.key AND LOWER(il.label) = LOWER(?))", [sval]
            if op == "~":
                return "EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_key = issues.key AND LOWER(il.label) LIKE LOWER(?))", [f"%{sval}%"]
            if op == "!~":
                return "NOT EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_key = issues.key AND LOWER(il.label) LIKE LOWER(?))", [f"%{sval}%"]
            raise IQLError(f"Operator '{op}' not supported for 'labels'")

        if field == "components":
            if op == "=":
                return "EXISTS (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) = LOWER(?))", [sval]
            if op == "!=":
                return "NOT EXISTS (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) = LOWER(?))", [sval]
            if op == "~":
                return "EXISTS (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) LIKE LOWER(?))", [f"%{sval}%"]
            if op == "!~":
                return "NOT EXISTS (SELECT 1 FROM issue_components ic WHERE ic.issue_key = issues.key AND LOWER(ic.component) LIKE LOWER(?))", [f"%{sval}%"]
            raise IQLError(f"Operator '{op}' not supported for 'components'")

        if field == "sprint_id":
            low = sval.lower()
            if low == "active":
                if not self.active_sprint_ids:
                    return ("1 = 1" if op == "!=" else "1 = 0"), []
                placeholders = ", ".join("?" for _ in self.active_sprint_ids)
                if op == "=":
                    return f"UPPER(issues.sprint_id) IN ({placeholders})", list(self.active_sprint_ids)
                if op == "!=":
                    return f"(issues.sprint_id IS NULL OR UPPER(issues.sprint_id) NOT IN ({placeholders}))", list(self.active_sprint_ids)
            if low in ("backlog", "none", "unassigned"):
                if op == "=":
                    return "(issues.sprint_id IS NULL OR issues.sprint_id = '')", []
                if op == "!=":
                    return "(issues.sprint_id IS NOT NULL AND issues.sprint_id != '')", []
            target_spr = self.sprint_name_to_id.get(low, sval.upper())
            if op == "=":
                return "UPPER(issues.sprint_id) = ?", [target_spr]
            if op == "!=":
                return "(issues.sprint_id IS NULL OR UPPER(issues.sprint_id) != ?)", [target_spr]
            if op == "~":
                return "UPPER(issues.sprint_id) LIKE UPPER(?)", [f"%{sval}%"]
            raise IQLError(f"Operator '{op}' not supported for 'sprint'")

        if field in ("assignee", "reporter", "claimed_by"):
            uval = self._resolve_user_val(sval)
            if field == "assignee" and uval.lower() in ("unassigned", "none"):
                if op == "=":
                    return "(issues.assignee IS NULL OR issues.assignee = '')", []
                if op == "!=":
                    return "(issues.assignee IS NOT NULL AND issues.assignee != '')", []
            if op == "=":
                return f"LOWER(issues.{field}) = LOWER(?)", [uval]
            if op == "!=":
                return f"(issues.{field} IS NULL OR LOWER(issues.{field}) != LOWER(?))", [uval]
            if op == "~":
                return f"LOWER(issues.{field}) LIKE LOWER(?)", [f"%{uval}%"]
            if op == "!~":
                return f"(issues.{field} IS NULL OR LOWER(issues.{field}) NOT LIKE LOWER(?))", [f"%{uval}%"]
            raise IQLError(f"Operator '{op}' not supported for '{field}'")

        if field == "text":
            fts_q = build_fts_match_expr(sval)
            like_pat = f"%{sval}%"
            if fts_q:
                base_sql = (
                    "(issues.key IN (SELECT key FROM issues_fts WHERE issues_fts MATCH ?) "
                    "OR LOWER(issues.key) LIKE LOWER(?) "
                    "OR LOWER(issues.title) LIKE LOWER(?) "
                    "OR LOWER(issues.description) LIKE LOWER(?) "
                    "OR LOWER(issues.comments_text) LIKE LOWER(?))"
                )
                base_params: List[Any] = [fts_q, like_pat, like_pat, like_pat, like_pat]
            else:
                base_sql = (
                    "(LOWER(issues.key) LIKE LOWER(?) "
                    "OR LOWER(issues.title) LIKE LOWER(?) "
                    "OR LOWER(issues.description) LIKE LOWER(?) "
                    "OR LOWER(issues.comments_text) LIKE LOWER(?))"
                )
                base_params = [like_pat, like_pat, like_pat, like_pat]
            if op in ("~", "="):
                return base_sql, base_params
            if op in ("!~", "!="):
                return f"NOT {base_sql}", base_params
            raise IQLError(f"Operator '{op}' not supported for 'text'")

        if field in ("title", "description", "comments"):
            db_col = "comments_text" if field == "comments" else field
            fts_col = "comments_text" if field == "comments" else field
            if op == "~":
                fts_q = build_fts_match_expr(sval, column=fts_col)
                like_pat = f"%{sval}%"
                if fts_q:
                    return (
                        f"(issues.key IN (SELECT key FROM issues_fts WHERE issues_fts MATCH ?) OR LOWER(issues.{db_col}) LIKE LOWER(?))",
                        [fts_q, like_pat],
                    )
                return f"LOWER(issues.{db_col}) LIKE LOWER(?)", [like_pat]
            if op == "!~":
                fts_q = build_fts_match_expr(sval, column=fts_col)
                like_pat = f"%{sval}%"
                if fts_q:
                    return (
                        f"NOT (issues.key IN (SELECT key FROM issues_fts WHERE issues_fts MATCH ?) OR LOWER(issues.{db_col}) LIKE LOWER(?))",
                        [fts_q, like_pat],
                    )
                return f"LOWER(issues.{db_col}) NOT LIKE LOWER(?)", [like_pat]
            if op == "=":
                return f"LOWER(issues.{db_col}) = LOWER(?)", [sval]
            if op == "!=":
                return f"LOWER(issues.{db_col}) != LOWER(?)", [sval]
            raise IQLError(f"Operator '{op}' not supported for '{field}'")

        if field.startswith("custom."):
            cf_key = field.split(".", 1)[1]
            path = f"$.custom_fields.{cf_key}"
            if op == "=":
                return "LOWER(CAST(json_extract(issues.raw_json, ?) AS TEXT)) = LOWER(?)", [path, sval]
            if op == "!=":
                return "(json_extract(issues.raw_json, ?) IS NULL OR LOWER(CAST(json_extract(issues.raw_json, ?) AS TEXT)) != LOWER(?))", [path, path, sval]
            if op == "~":
                return "LOWER(CAST(json_extract(issues.raw_json, ?) AS TEXT)) LIKE LOWER(?)", [path, f"%{sval}%"]
            raise IQLError(f"Operator '{op}' not supported for custom field '{field}'")

        # Standard scalar text fields (key, type, status, epic_id, parent_id, milestone_id, resolution, environment)
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
        raise IQLError(f"Unsupported operator '{op}' for field '{field}'")

    def compile_order_by(self, order_items: List[Dict[str, str]], default_sort: Optional[str] = None) -> str:
        if order_items:
            clauses: List[str] = []
            for item in order_items:
                f = item["field"]
                d = item["direction"]
                sql_col = ORDER_SQL_MAP[f]
                if f in ("story_points", "due_date", "resolved_at", "assignee", "sprint_id", "milestone_id"):
                    clauses.append(f"({sql_col} IS NULL) ASC, {sql_col} {d}")
                else:
                    clauses.append(f"{sql_col} {d}")
            clauses.append("issues.seq ASC")
            return ", ".join(clauses)
        if default_sort:
            parts = [p.strip() for p in default_sort.split(",") if p.strip()]
            clauses = []
            for p in parts:
                direction = "ASC"
                fname = p
                if p.startswith("-"):
                    direction = "DESC"
                    fname = p[1:]
                elif " " in p:
                    sp = p.split()
                    fname = sp[0]
                    if len(sp) > 1 and sp[1].upper() in ("ASC", "DESC"):
                        direction = sp[1].upper()
                low = fname.lower()
                canonical = FIELD_ALIASES.get(low, low)
                if canonical in ORDER_SQL_MAP:
                    clauses.append(f"{ORDER_SQL_MAP[canonical]} {direction}")
            if clauses:
                clauses.append("issues.seq ASC")
                return ", ".join(clauses)
        return "issues.seq ASC"


# ============================================================================
# File-Locked Storage + Hybrid SQLite3/FTS5 Index Engine
# ============================================================================

class StorageEngine:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.issues_root = repo_root / "itrack-issues"
        self.issues_dir = self.issues_root / "issues"
        self.sprints_dir = self.issues_root / "sprints"
        self.milestones_dir = self.issues_root / "milestones"
        self.views_dir = self.issues_root / "views"
        self.attachments_dir = self.issues_root / "attachments"
        self.runtime_dir = self.issues_root / ".runtime"

        self.config_path = self.issues_root / "config.json"
        self.labels_path = self.issues_root / "labels.json"
        self.components_path = self.issues_root / "components.json"
        self.users_path = self.issues_root / "users.json"
        self.gitignore_path = self.issues_root / ".gitignore"

        self.lock_path = self.runtime_dir / "storage.lock"
        self.db_path = self.runtime_dir / "index.db"
        self.daemon_path = self.runtime_dir / "daemon.json"

        self._rlock = threading.RLock()
        self._lock_depth = 0
        self._lock_fd: Optional[Any] = None
        self._lock_exclusive = False
        self.default_user = detect_default_user(repo_root)

        self._initialize_layout()
        self._db = self._connect_db()
        self._init_db_schema()
        self.reindex()

    def _initialize_layout(self) -> None:
        for d in (
            self.issues_root,
            self.issues_dir,
            self.sprints_dir,
            self.milestones_dir,
            self.views_dir,
            self.attachments_dir,
            self.runtime_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        with self.write_lock():
            if not self.gitignore_path.exists():
                self._atomic_write_text(self.gitignore_path, ".runtime/\n")

            if not self.config_path.exists():
                ts = now_iso()
                default_cfg = {
                    "project_key": "ITK",
                    "project_name": self.repo_root.name or "itrack Workspace",
                    "next_seq": 1,
                    "next_sprint_seq": 1,
                    "next_milestone_seq": 1,
                    "next_view_seq": 1,
                    "next_attachment_seq": 1,
                    "default_type": "task",
                    "default_priority": "medium",
                    "statuses": list(DEFAULT_STATUSES),
                    "transitions": {k: list(v) for k, v in DEFAULT_TRANSITIONS.items()},
                    "priorities": list(DEFAULT_PRIORITIES),
                    "issue_types": list(DEFAULT_ISSUE_TYPES),
                    "wip_limits": {},
                    "custom_fields": {},
                    "created_at": ts,
                    "updated_at": ts,
                }
                self._atomic_write_json(self.config_path, default_cfg)

            if not self.labels_path.exists():
                self._atomic_write_json(self.labels_path, {"labels": []})

            if not self.components_path.exists():
                self._atomic_write_json(self.components_path, {"components": []})

            if not self.users_path.exists():
                self._atomic_write_json(self.users_path, {"users": []})

    @contextlib.contextmanager
    def write_lock(self) -> Iterator[None]:
        with self._rlock:
            if self._lock_depth == 0:
                f = open(self.lock_path, "a+", encoding="utf-8")
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                self._lock_fd = f
                self._lock_exclusive = True
            elif not self._lock_exclusive and self._lock_fd is not None:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX)
                self._lock_exclusive = True
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0 and self._lock_fd is not None:
                    try:
                        fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                    finally:
                        self._lock_fd.close()
                        self._lock_fd = None
                        self._lock_exclusive = False

    @contextlib.contextmanager
    def read_lock(self) -> Iterator[None]:
        with self._rlock:
            if self._lock_depth == 0:
                f = open(self.lock_path, "a+", encoding="utf-8")
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                self._lock_fd = f
                self._lock_exclusive = False
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0 and self._lock_fd is not None:
                    try:
                        fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                    finally:
                        self._lock_fd.close()
                        self._lock_fd = None
                        self._lock_exclusive = False

    def _atomic_write_text(self, path: Path, content: str) -> None:
        tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.{short_id()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
        os.replace(tmp, path)

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        self._atomic_write_text(path, payload)

    def _read_json(self, path: Path) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db_schema(self) -> None:
        with self._rlock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    key TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    priority_rank INTEGER NOT NULL,
                    resolution TEXT,
                    assignee TEXT,
                    reporter TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    epic_id TEXT,
                    parent_id TEXT,
                    sprint_id TEXT,
                    milestone_id TEXT,
                    story_points REAL,
                    due_date TEXT,
                    environment TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    resolved_at TEXT,
                    closed_at TEXT,
                    comments_text TEXT NOT NULL DEFAULT '',
                    labels_text TEXT NOT NULL DEFAULT '',
                    components_text TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
                CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority_rank);
                CREATE INDEX IF NOT EXISTS idx_issues_assignee ON issues(assignee);
                CREATE INDEX IF NOT EXISTS idx_issues_sprint ON issues(sprint_id);
                CREATE INDEX IF NOT EXISTS idx_issues_epic ON issues(epic_id);
                CREATE INDEX IF NOT EXISTS idx_issues_parent ON issues(parent_id);

                CREATE TABLE IF NOT EXISTS issue_labels (
                    issue_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    PRIMARY KEY (issue_key, label)
                );
                CREATE INDEX IF NOT EXISTS idx_issue_labels_label ON issue_labels(LOWER(label));

                CREATE TABLE IF NOT EXISTS issue_components (
                    issue_key TEXT NOT NULL,
                    component TEXT NOT NULL,
                    PRIMARY KEY (issue_key, component)
                );
                CREATE INDEX IF NOT EXISTS idx_issue_components_comp ON issue_components(LOWER(component));

                CREATE TABLE IF NOT EXISTS issue_links (
                    id TEXT PRIMARY KEY,
                    source_key TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_links_source ON issue_links(source_key);
                CREATE INDEX IF NOT EXISTS idx_links_target ON issue_links(target_key);

                CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
                    key,
                    title,
                    description,
                    comments_text,
                    labels_text,
                    components_text
                );
                """
            )
            self._db.commit()

    def _index_issue_in_tx(self, cur: sqlite3.Cursor, issue: Dict[str, Any]) -> None:
        key = issue["key"]
        comments_text = "\n".join(c.get("body", "") for c in issue.get("comments", []))
        labels = issue.get("labels", [])
        components = issue.get("components", [])
        labels_text = " ".join(labels)
        components_text = " ".join(components)
        _, p_rank = normalize_priority(issue.get("priority", "medium"))
        issue["priority_rank"] = p_rank
        raw_json = json.dumps(issue, sort_keys=True, ensure_ascii=False)

        cur.execute("DELETE FROM issue_labels WHERE issue_key = ?", (key,))
        cur.execute("DELETE FROM issue_components WHERE issue_key = ?", (key,))
        cur.execute("DELETE FROM issue_links WHERE source_key = ?", (key,))
        cur.execute("DELETE FROM issues_fts WHERE key = ?", (key,))

        cur.execute(
            """
            INSERT OR REPLACE INTO issues (
                key, seq, title, description, type, status, priority, priority_rank,
                resolution, assignee, reporter, claimed_by, claimed_at,
                epic_id, parent_id, sprint_id, milestone_id, story_points,
                due_date, environment, created_at, updated_at, started_at,
                resolved_at, closed_at, comments_text, labels_text, components_text, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                int(issue.get("seq", 0)),
                issue.get("title", ""),
                issue.get("description", "") or "",
                issue.get("type", "task"),
                issue.get("status", "todo"),
                issue.get("priority", "medium"),
                p_rank,
                issue.get("resolution"),
                issue.get("assignee"),
                issue.get("reporter"),
                issue.get("claimed_by"),
                issue.get("claimed_at"),
                issue.get("epic_id"),
                issue.get("parent_id"),
                issue.get("sprint_id"),
                issue.get("milestone_id"),
                issue.get("story_points"),
                issue.get("due_date"),
                issue.get("environment"),
                issue.get("created_at") or now_iso(),
                issue.get("updated_at") or now_iso(),
                issue.get("started_at"),
                issue.get("resolved_at"),
                issue.get("closed_at"),
                comments_text,
                labels_text,
                components_text,
                raw_json,
            ),
        )

        for lb in labels:
            cur.execute("INSERT OR IGNORE INTO issue_labels (issue_key, label) VALUES (?, ?)", (key, lb))
        for cp in components:
            cur.execute("INSERT OR IGNORE INTO issue_components (issue_key, component) VALUES (?, ?)", (key, cp))
        for lnk in issue.get("links", []):
            cur.execute(
                "INSERT OR REPLACE INTO issue_links (id, source_key, target_key, link_type, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lnk["id"],
                    lnk["source_key"],
                    lnk["target_key"],
                    lnk["link_type"],
                    lnk.get("created_by"),
                    lnk.get("created_at"),
                ),
            )
        cur.execute(
            "INSERT INTO issues_fts (key, title, description, comments_text, labels_text, components_text) VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                issue.get("title", ""),
                issue.get("description", "") or "",
                comments_text,
                labels_text,
                components_text,
            ),
        )

    def _remove_issue_from_index(self, key: str) -> None:
        cur = self._db.cursor()
        cur.execute("DELETE FROM issues WHERE key = ?", (key,))
        cur.execute("DELETE FROM issue_labels WHERE issue_key = ?", (key,))
        cur.execute("DELETE FROM issue_components WHERE issue_key = ?", (key,))
        cur.execute("DELETE FROM issue_links WHERE source_key = ? OR target_key = ?", (key, key))
        cur.execute("DELETE FROM issues_fts WHERE key = ?", (key,))
        self._db.commit()

    def _save_issue(self, issue: Dict[str, Any]) -> None:
        path = self.issues_dir / f"{issue['key']}.json"
        self._atomic_write_json(path, issue)
        cur = self._db.cursor()
        self._index_issue_in_tx(cur, issue)
        self._db.commit()

    def reindex(self) -> Dict[str, Any]:
        """Rebuild SQLite3 + FTS5 index completely from canonical JSON files on disk."""
        t0 = time.perf_counter()
        with self.write_lock():
            cur = self._db.cursor()
            cur.execute("DELETE FROM issues;")
            cur.execute("DELETE FROM issue_labels;")
            cur.execute("DELETE FROM issue_components;")
            cur.execute("DELETE FROM issue_links;")
            cur.execute("DELETE FROM issues_fts;")

            count = 0
            max_seq = 0
            max_att_seq = 0
            for p in sorted(self.issues_dir.glob("*.json")):
                if p.name.startswith("."):
                    continue
                issue = self._read_json(p)
                if not isinstance(issue, dict) or "key" not in issue:
                    continue
                # Ensure seq exists even if manually edited on disk
                m_key = re.search(r"-(\d+)$", str(issue["key"]))
                if "seq" not in issue:
                    issue["seq"] = int(m_key.group(1)) if m_key else (count + 1)
                max_seq = max(max_seq, int(issue["seq"]), int(m_key.group(1)) if m_key else 0)
                for att in issue.get("attachments", []):
                    if isinstance(att, dict):
                        m_att = re.search(r"-(\d+)$", str(att.get("id", "")))
                        if m_att:
                            max_att_seq = max(max_att_seq, int(m_att.group(1)))
                self._index_issue_in_tx(cur, issue)
                count += 1

            # Also scan attachment files on disk in case any exist
            for att_p in self.attachments_dir.rglob("*"):
                if att_p.is_file() and not att_p.name.startswith("."):
                    m_f = re.match(r"ATT-(\d+)_", att_p.name, re.IGNORECASE)
                    if m_f:
                        max_att_seq = max(max_att_seq, int(m_f.group(1)))

            max_spr_seq = 0
            for sp_p in self.sprints_dir.glob("*.json"):
                if sp_p.name.startswith("."):
                    continue
                sp = self._read_json(sp_p)
                if isinstance(sp, dict):
                    m_sp = re.search(r"-(\d+)$", str(sp.get("id", "")))
                    max_spr_seq = max(max_spr_seq, int(sp.get("seq") or 0), int(m_sp.group(1)) if m_sp else 0)

            max_mil_seq = 0
            for ms_p in self.milestones_dir.glob("*.json"):
                if ms_p.name.startswith("."):
                    continue
                ms = self._read_json(ms_p)
                if isinstance(ms, dict):
                    m_ms = re.search(r"-(\d+)$", str(ms.get("id", "")))
                    max_mil_seq = max(max_mil_seq, int(ms.get("seq") or 0), int(m_ms.group(1)) if m_ms else 0)

            max_view_seq = 0
            for vw_p in self.views_dir.glob("*.json"):
                if vw_p.name.startswith("."):
                    continue
                vw = self._read_json(vw_p)
                if isinstance(vw, dict):
                    m_vw = re.search(r"-(\d+)$", str(vw.get("id", "")))
                    max_view_seq = max(max_view_seq, int(vw.get("seq") or 0), int(m_vw.group(1)) if m_vw else 0)

            self._db.commit()

            cfg = self._read_json(self.config_path)
            dirty = False
            for seq_key, max_val in (
                ("next_seq", max_seq),
                ("next_attachment_seq", max_att_seq),
                ("next_sprint_seq", max_spr_seq),
                ("next_milestone_seq", max_mil_seq),
                ("next_view_seq", max_view_seq),
            ):
                if max_val >= int(cfg.get(seq_key, 1)):
                    cfg[seq_key] = max_val + 1
                    dirty = True
            if dirty:
                cfg["updated_at"] = now_iso()
                self._atomic_write_json(self.config_path, cfg)

        dur_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {"status": "reindexed", "indexed_issues": count, "duration_ms": dur_ms}

    # ------------------------------------------------------------------------
    # Enrichment & Computed Fields
    # ------------------------------------------------------------------------

    def _enrich_issues(self, raw_issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not raw_issues:
            return []
        with self._rlock:
            # Fetch all links and status map from SQLite for O(1) enrichment
            status_rows = self._db.execute("SELECT key, seq, status, parent_id FROM issues ORDER BY seq ASC").fetchall()
            status_by_key: Dict[str, str] = {}
            subtasks_by_parent: Dict[str, List[str]] = {}
            seq_by_key: Dict[str, int] = {}
            for r in status_rows:
                k = r["key"]
                status_by_key[k] = r["status"]
                seq_by_key[k] = r["seq"]
                pid = r["parent_id"]
                if pid:
                    subtasks_by_parent.setdefault(pid, []).append(k)

            link_rows = self._db.execute(
                "SELECT id, source_key, target_key, link_type, created_by, created_at FROM issue_links"
            ).fetchall()

            inbound_by_target: Dict[str, List[sqlite3.Row]] = {}
            for lr in link_rows:
                inbound_by_target.setdefault(lr["target_key"], []).append(lr)

            enriched: List[Dict[str, Any]] = []
            for item in raw_issues:
                iss = dict(item)
                key = iss["key"]
                outbound = list(iss.get("links", []))
                all_links: List[Dict[str, Any]] = []
                seen_triples: Set[Tuple[str, str, str]] = set()
                blockers: Set[str] = set()

                for lnk in outbound:
                    ltype = lnk["link_type"]
                    tgt = lnk["target_key"]
                    entry = {
                        "id": lnk["id"],
                        "source_key": key,
                        "target_key": tgt,
                        "link_type": ltype,
                        "direction": "outbound",
                        "created_by": lnk.get("created_by"),
                        "created_at": lnk.get("created_at"),
                    }
                    all_links.append(entry)
                    seen_triples.add((key, tgt, ltype))
                    if ltype == "blocked_by":
                        st = status_by_key.get(tgt)
                        if st and st not in ("done", "canceled"):
                            blockers.add(tgt)

                for in_lnk in inbound_by_target.get(key, []):
                    src = in_lnk["source_key"]
                    if src == key:
                        continue
                    orig_type = in_lnk["link_type"]
                    inv_type = INVERSE_LINK_TYPES.get(orig_type, orig_type)
                    if (key, src, inv_type) not in seen_triples:
                        seen_triples.add((key, src, inv_type))
                        all_links.append(
                            {
                                "id": in_lnk["id"],
                                "source_key": key,
                                "target_key": src,
                                "link_type": inv_type,
                                "direction": "inbound",
                                "origin_source_key": src,
                                "created_by": in_lnk["created_by"],
                                "created_at": in_lnk["created_at"],
                            }
                        )
                    if orig_type == "blocks":
                        st = status_by_key.get(src)
                        if st and st not in ("done", "canceled"):
                            blockers.add(src)

                sorted_blockers = sorted(blockers, key=lambda bk: (seq_by_key.get(bk, 10**9), bk))
                iss["all_links"] = all_links
                iss["unresolved_blockers"] = sorted_blockers
                iss["is_blocked"] = (iss.get("status") == "blocked") or (len(sorted_blockers) > 0)
                iss["subtask_keys"] = subtasks_by_parent.get(key, [])
                iss["total_worklog_minutes"] = sum(int(w.get("minutes", 0)) for w in iss.get("worklogs", []))
                enriched.append(iss)
            return enriched

    def _get_raw_issue_or_404(self, key: str) -> Dict[str, Any]:
        norm_key = key.strip().upper()
        path = self.issues_dir / f"{norm_key}.json"
        if not path.exists():
            # Case-insensitive fallback scan if custom casing existed
            row = self._db.execute("SELECT key FROM issues WHERE UPPER(key) = ?", (norm_key,)).fetchone()
            if row:
                path = self.issues_dir / f"{row['key']}.json"
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"Issue '{norm_key}' not found.")
        return self._read_json(path)

    def get_issue(self, key: str) -> Dict[str, Any]:
        with self.read_lock():
            raw = self._get_raw_issue_or_404(key)
            return self._enrich_issues([raw])[0]

    def issue_count(self) -> int:
        with self._rlock:
            row = self._db.execute("SELECT COUNT(*) AS c FROM issues").fetchone()
            return int(row["c"]) if row else 0

    # ------------------------------------------------------------------------
    # Config & Workflows
    # ------------------------------------------------------------------------

    def get_config(self) -> Dict[str, Any]:
        with self.read_lock():
            return self._read_json(self.config_path)

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            if "project_key" in patch and patch["project_key"] is not None:
                pk = str(patch["project_key"]).strip().upper()
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,11}", pk):
                    raise HTTPException(
                        status_code=400,
                        detail="project_key must start with a letter and contain 1-12 uppercase letters/digits/underscores.",
                    )
                cfg["project_key"] = pk
            if "project_name" in patch and patch["project_name"] is not None:
                cfg["project_name"] = str(patch["project_name"]).strip() or cfg["project_name"]
            if "default_type" in patch and patch["default_type"] is not None:
                dt_val = str(patch["default_type"]).strip().lower()
                if dt_val not in cfg["issue_types"]:
                    raise HTTPException(status_code=400, detail=f"Invalid default_type '{dt_val}'.")
                cfg["default_type"] = dt_val
            if "default_priority" in patch and patch["default_priority"] is not None:
                try:
                    dp, _ = normalize_priority(str(patch["default_priority"]))
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                cfg["default_priority"] = dp
            if "wip_limits" in patch and patch["wip_limits"] is not None:
                cfg["wip_limits"] = dict(patch["wip_limits"])
            if "custom_fields" in patch and patch["custom_fields"] is not None:
                cfg["custom_fields"] = dict(patch["custom_fields"])
            cfg["updated_at"] = now_iso()
            self._atomic_write_json(self.config_path, cfg)
            return cfg

    def get_workflows(self) -> Dict[str, Any]:
        cfg = self.get_config()
        return {"statuses": cfg["statuses"], "transitions": cfg["transitions"]}

    def set_workflows(self, statuses: List[str], transitions: Dict[str, List[str]]) -> Dict[str, Any]:
        if not statuses or not all(isinstance(s, str) and s.strip() for s in statuses):
            raise HTTPException(status_code=400, detail="statuses must be a non-empty list of status strings.")
        clean_statuses = [s.strip().lower() for s in statuses]
        clean_transitions: Dict[str, List[str]] = {}
        for src, targets in transitions.items():
            s_clean = src.strip().lower()
            clean_transitions[s_clean] = [t.strip().lower() for t in targets if t.strip()]
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            cfg["statuses"] = clean_statuses
            cfg["transitions"] = clean_transitions
            cfg["updated_at"] = now_iso()
            self._atomic_write_json(self.config_path, cfg)
            return {"statuses": clean_statuses, "transitions": clean_transitions}

    # ------------------------------------------------------------------------
    # Sprints, Milestones, Labels, Components, Users, Views
    # ------------------------------------------------------------------------

    def _list_dir_json(self, directory: Path) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for p in sorted(directory.glob("*.json")):
            if p.name.startswith("."):
                continue
            items.append(self._read_json(p))
        return items

    def list_sprints(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.read_lock():
            sprints = self._list_dir_json(self.sprints_dir)
            if state:
                st = state.strip().lower()
                sprints = [s for s in sprints if s.get("state") == st]
            sprints.sort(key=lambda s: int(s.get("seq", 0)))
            return sprints

    def create_sprint(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Sprint name is required.")
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            seq = int(cfg.get("next_sprint_seq", 1))
            cfg["next_sprint_seq"] = seq + 1
            cfg["updated_at"] = now_iso()
            sprint_id = f"SPR-{seq}"
            ts = now_iso()
            sprint = {
                "id": sprint_id,
                "seq": seq,
                "name": name,
                "goal": (data.get("goal") or "").strip(),
                "state": "planned",
                "start_date": data.get("start_date") or None,
                "end_date": data.get("end_date") or None,
                "started_at": None,
                "completed_at": None,
                "created_at": ts,
                "updated_at": ts,
            }
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(self.sprints_dir / f"{sprint_id}.json", sprint)
            return sprint

    def _get_sprint_or_404(self, sprint_id: str) -> Dict[str, Any]:
        sid = sprint_id.strip().upper()
        p = self.sprints_dir / f"{sid}.json"
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Sprint '{sid}' not found.")
        return self._read_json(p)

    def get_sprint_detail(self, sprint_id: str) -> Dict[str, Any]:
        with self.read_lock():
            sprint = self._get_sprint_or_404(sprint_id)
            sid = sprint["id"]
            rows = self._db.execute(
                "SELECT raw_json FROM issues WHERE UPPER(sprint_id) = ? ORDER BY priority_rank DESC, seq ASC",
                (sid,),
            ).fetchall()
            issues = self._enrich_issues([json.loads(r["raw_json"]) for r in rows])
            total_issues = len(issues)
            done_issues = sum(1 for i in issues if i["status"] == "done")
            total_pts = round(sum(float(i.get("story_points") or 0.0) for i in issues), 2)
            done_pts = round(sum(float(i.get("story_points") or 0.0) for i in issues if i["status"] == "done"), 2)
            pct = round((done_issues / total_issues * 100.0), 1) if total_issues > 0 else 0.0
            return {
                "sprint": sprint,
                "issues": issues,
                "metrics": {
                    "total_issues": total_issues,
                    "done_issues": done_issues,
                    "total_points": total_pts,
                    "done_points": done_pts,
                    "progress_pct": pct,
                },
            }

    def update_sprint(self, sprint_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            sprint = self._get_sprint_or_404(sprint_id)
            for k in ("name", "goal", "start_date", "end_date"):
                if k in patch:
                    sprint[k] = patch[k]
            if "state" in patch and patch["state"] is not None:
                st = str(patch["state"]).strip().lower()
                if st not in ("planned", "active", "completed"):
                    raise HTTPException(status_code=400, detail="Invalid sprint state. Expected planned, active, or completed.")
                sprint["state"] = st
            sprint["updated_at"] = now_iso()
            self._atomic_write_json(self.sprints_dir / f"{sprint['id']}.json", sprint)
            return sprint

    def start_sprint(self, sprint_id: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            sprint = self._get_sprint_or_404(sprint_id)
            if sprint["state"] == "completed":
                raise HTTPException(status_code=400, detail=f"Sprint '{sprint['id']}' is already completed.")
            ts = now_iso()
            sprint["state"] = "active"
            sprint["started_at"] = sprint.get("started_at") or ts
            sprint["start_date"] = start_date or sprint.get("start_date") or today_iso()
            if end_date is not None:
                sprint["end_date"] = end_date
            elif not sprint.get("end_date"):
                sprint["end_date"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=14)).strftime("%Y-%m-%d")
            sprint["updated_at"] = ts
            self._atomic_write_json(self.sprints_dir / f"{sprint['id']}.json", sprint)
            return sprint

    def complete_sprint(self, sprint_id: str, rollover_to: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            sprint = self._get_sprint_or_404(sprint_id)
            sid = sprint["id"]
            target_sprint: Optional[str] = None
            if rollover_to and rollover_to.strip().lower() not in ("backlog", "none", "null", ""):
                target_sprint = rollover_to.strip().upper()
                if target_sprint == sid:
                    raise HTTPException(status_code=400, detail="Cannot rollover unfinished issues into the same sprint.")
                self._get_sprint_or_404(target_sprint)

            ts = now_iso()
            sprint["state"] = "completed"
            sprint["completed_at"] = ts
            sprint["updated_at"] = ts
            self._atomic_write_json(self.sprints_dir / f"{sid}.json", sprint)

            rows = self._db.execute(
                "SELECT key FROM issues WHERE UPPER(sprint_id) = ? AND status NOT IN ('done', 'canceled') ORDER BY seq ASC",
                (sid,),
            ).fetchall()
            rolled_keys: List[str] = []
            for r in rows:
                iss = self._get_raw_issue_or_404(r["key"])
                old_sp = iss.get("sprint_id")
                iss["sprint_id"] = target_sprint
                iss["updated_at"] = ts
                iss.setdefault("history", []).append(
                    {
                        "id": f"EVT-{short_id()}",
                        "actor": self.default_user,
                        "action": "sprint_rollover",
                        "changes": {"sprint_id": {"from": old_sp, "to": target_sprint}},
                        "timestamp": ts,
                    }
                )
                self._save_issue(iss)
                rolled_keys.append(iss["key"])

            return {"sprint": sprint, "rolled_over_keys": rolled_keys}

    def delete_sprint(self, sprint_id: str) -> Dict[str, Any]:
        with self.write_lock():
            sprint = self._get_sprint_or_404(sprint_id)
            sid = sprint["id"]
            rows = self._db.execute("SELECT key FROM issues WHERE UPPER(sprint_id) = ?", (sid,)).fetchall()
            ts = now_iso()
            for r in rows:
                iss = self._get_raw_issue_or_404(r["key"])
                iss["sprint_id"] = None
                iss["updated_at"] = ts
                self._save_issue(iss)
            (self.sprints_dir / f"{sid}.json").unlink(missing_ok=True)
            return {"deleted": True, "id": sid}

    # Milestones
    def _enrich_milestone(self, ms: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(ms)
        mid = ms["id"]
        rows = self._db.execute("SELECT status FROM issues WHERE UPPER(milestone_id) = ?", (mid,)).fetchall()
        total = len(rows)
        done = sum(1 for r in rows if r["status"] == "done")
        out["total_issues"] = total
        out["done_issues"] = done
        out["progress_pct"] = round((done / total * 100.0), 1) if total > 0 else 0.0
        return out

    def list_milestones(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            items = self._list_dir_json(self.milestones_dir)
            items.sort(key=lambda m: int(m.get("seq", 0)))
            return [self._enrich_milestone(m) for m in items]

    def create_milestone(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Milestone name is required.")
        status = (data.get("status") or "unreleased").strip().lower()
        if status not in ("unreleased", "released", "archived"):
            raise HTTPException(status_code=400, detail="Invalid milestone status.")
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            seq = int(cfg.get("next_milestone_seq", 1))
            cfg["next_milestone_seq"] = seq + 1
            cfg["updated_at"] = now_iso()
            mid = f"MIL-{seq}"
            ts = now_iso()
            ms = {
                "id": mid,
                "seq": seq,
                "name": name,
                "description": (data.get("description") or "").strip(),
                "target_date": data.get("target_date") or None,
                "status": status,
                "created_at": ts,
                "updated_at": ts,
            }
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(self.milestones_dir / f"{mid}.json", ms)
            return self._enrich_milestone(ms)

    def update_milestone(self, milestone_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            mid = milestone_id.strip().upper()
            p = self.milestones_dir / f"{mid}.json"
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"Milestone '{mid}' not found.")
            ms = self._read_json(p)
            for k in ("name", "description", "target_date"):
                if k in patch:
                    ms[k] = patch[k]
            if "status" in patch and patch["status"] is not None:
                st = str(patch["status"]).strip().lower()
                if st not in ("unreleased", "released", "archived"):
                    raise HTTPException(status_code=400, detail="Invalid milestone status.")
                ms["status"] = st
            ms["updated_at"] = now_iso()
            self._atomic_write_json(p, ms)
            return self._enrich_milestone(ms)

    def delete_milestone(self, milestone_id: str) -> Dict[str, Any]:
        with self.write_lock():
            mid = milestone_id.strip().upper()
            p = self.milestones_dir / f"{mid}.json"
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"Milestone '{mid}' not found.")
            rows = self._db.execute("SELECT key FROM issues WHERE UPPER(milestone_id) = ?", (mid,)).fetchall()
            ts = now_iso()
            for r in rows:
                iss = self._get_raw_issue_or_404(r["key"])
                iss["milestone_id"] = None
                iss["updated_at"] = ts
                self._save_issue(iss)
            p.unlink(missing_ok=True)
            return {"deleted": True, "id": mid}

    # Labels & Components
    def list_labels(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            return self._read_json(self.labels_path).get("labels", [])

    def add_label(self, name: str, color: Optional[str] = None, description: Optional[str] = None) -> Dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            raise HTTPException(status_code=400, detail="Label name is required.")
        with self.write_lock():
            doc = self._read_json(self.labels_path)
            labels = doc.get("labels", [])
            ts = now_iso()
            for lb in labels:
                if lb["name"].lower() == clean.lower():
                    if color is not None:
                        lb["color"] = color
                    if description is not None:
                        lb["description"] = description
                    lb["updated_at"] = ts
                    self._atomic_write_json(self.labels_path, {"labels": labels})
                    return lb
            entry = {
                "name": clean,
                "color": color or "#3b82f6",
                "description": description or "",
                "created_at": ts,
            }
            labels.append(entry)
            self._atomic_write_json(self.labels_path, {"labels": labels})
            return entry

    def delete_label(self, name: str) -> Dict[str, Any]:
        clean = name.strip()
        with self.write_lock():
            doc = self._read_json(self.labels_path)
            labels = doc.get("labels", [])
            new_labels = [lb for lb in labels if lb["name"].lower() != clean.lower()]
            if len(new_labels) == len(labels):
                raise HTTPException(status_code=404, detail=f"Label '{clean}' not found.")
            self._atomic_write_json(self.labels_path, {"labels": new_labels})
            rows = self._db.execute(
                "SELECT issue_key FROM issue_labels WHERE LOWER(label) = LOWER(?)", (clean,)
            ).fetchall()
            ts = now_iso()
            for r in rows:
                iss = self._get_raw_issue_or_404(r["issue_key"])
                iss["labels"] = [l for l in iss.get("labels", []) if l.lower() != clean.lower()]
                iss["updated_at"] = ts
                self._save_issue(iss)
            return {"deleted": True, "name": clean}

    def list_components(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            return self._read_json(self.components_path).get("components", [])

    def add_component(self, name: str, lead: Optional[str] = None, description: Optional[str] = None) -> Dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            raise HTTPException(status_code=400, detail="Component name is required.")
        with self.write_lock():
            doc = self._read_json(self.components_path)
            comps = doc.get("components", [])
            ts = now_iso()
            for cp in comps:
                if cp["name"].lower() == clean.lower():
                    if lead is not None:
                        cp["lead"] = lead
                    if description is not None:
                        cp["description"] = description
                    cp["updated_at"] = ts
                    self._atomic_write_json(self.components_path, {"components": comps})
                    return cp
            entry = {
                "name": clean,
                "lead": lead or None,
                "description": description or "",
                "created_at": ts,
            }
            comps.append(entry)
            self._atomic_write_json(self.components_path, {"components": comps})
            return entry

    def delete_component(self, name: str) -> Dict[str, Any]:
        clean = name.strip()
        with self.write_lock():
            doc = self._read_json(self.components_path)
            comps = doc.get("components", [])
            new_comps = [cp for cp in comps if cp["name"].lower() != clean.lower()]
            if len(new_comps) == len(comps):
                raise HTTPException(status_code=404, detail=f"Component '{clean}' not found.")
            self._atomic_write_json(self.components_path, {"components": new_comps})
            rows = self._db.execute(
                "SELECT issue_key FROM issue_components WHERE LOWER(component) = LOWER(?)", (clean,)
            ).fetchall()
            ts = now_iso()
            for r in rows:
                iss = self._get_raw_issue_or_404(r["issue_key"])
                iss["components"] = [c for c in iss.get("components", []) if c.lower() != clean.lower()]
                iss["updated_at"] = ts
                self._save_issue(iss)
            return {"deleted": True, "name": clean}

    # Users / Agents
    def list_users(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            return self._read_json(self.users_path).get("users", [])

    def upsert_user(self, data: Dict[str, Any]) -> Dict[str, Any]:
        uname = (data.get("username") or "").strip()
        if not uname:
            raise HTTPException(status_code=400, detail="username is required.")
        with self.write_lock():
            doc = self._read_json(self.users_path)
            users = doc.get("users", [])
            ts = now_iso()
            for u in users:
                if u["username"].lower() == uname.lower():
                    for k in ("display_name", "email", "role", "is_agent", "avatar_color"):
                        if k in data and data[k] is not None:
                            u[k] = data[k]
                    u["updated_at"] = ts
                    self._atomic_write_json(self.users_path, {"users": users})
                    return u
            entry = {
                "username": uname,
                "display_name": data.get("display_name") or uname,
                "email": data.get("email") or "",
                "role": data.get("role") or ("agent" if data.get("is_agent") else "developer"),
                "is_agent": bool(data.get("is_agent", False)),
                "avatar_color": data.get("avatar_color") or "#2563eb",
                "created_at": ts,
                "updated_at": ts,
            }
            users.append(entry)
            self._atomic_write_json(self.users_path, {"users": users})
            return entry

    def get_user_detail(self, username: str) -> Dict[str, Any]:
        uname = username.strip()
        with self.read_lock():
            users = self._read_json(self.users_path).get("users", [])
            found = next((u for u in users if u["username"].lower() == uname.lower()), None)
            if not found:
                raise HTTPException(status_code=404, detail=f"User '{uname}' not found.")
            assigned = self._db.execute(
                "SELECT COUNT(*) AS c FROM issues WHERE LOWER(assignee) = LOWER(?)", (uname,)
            ).fetchone()["c"]
            in_prog = self._db.execute(
                "SELECT COUNT(*) AS c FROM issues WHERE LOWER(assignee) = LOWER(?) AND status = 'in_progress'",
                (uname,),
            ).fetchone()["c"]
            return {
                "user": found,
                "assigned_issues": int(assigned),
                "in_progress_issues": int(in_prog),
            }

    def update_user(self, username: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        uname = username.strip()
        with self.write_lock():
            doc = self._read_json(self.users_path)
            users = doc.get("users", [])
            found = next((u for u in users if u["username"].lower() == uname.lower()), None)
            if not found:
                raise HTTPException(status_code=404, detail=f"User '{uname}' not found.")
            for k in ("display_name", "email", "role", "is_agent", "avatar_color"):
                if k in patch and patch[k] is not None:
                    found[k] = patch[k]
            found["updated_at"] = now_iso()
            self._atomic_write_json(self.users_path, {"users": users})
            return found

    def delete_user(self, username: str) -> Dict[str, Any]:
        uname = username.strip()
        with self.write_lock():
            doc = self._read_json(self.users_path)
            users = doc.get("users", [])
            new_users = [u for u in users if u["username"].lower() != uname.lower()]
            if len(new_users) == len(users):
                raise HTTPException(status_code=404, detail=f"User '{uname}' not found.")
            self._atomic_write_json(self.users_path, {"users": new_users})
            return {"deleted": True, "username": uname}

    # Saved Views
    def list_views(self) -> List[Dict[str, Any]]:
        with self.read_lock():
            views = self._list_dir_json(self.views_dir)
            views.sort(key=lambda v: int(v.get("seq", 0)))
            return views

    def create_view(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="View name is required.")
        iql_str = data.get("iql") or ""
        try:
            ast = parse_iql(iql_str)
            compiler = self._make_compiler()
            compiler.compile_where(ast["where"])
            compiler.compile_order_by(ast["order_by"])
        except IQLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid iQL: {e.message}")
        layout = (data.get("layout") or "list").strip().lower()
        if layout not in ("board", "list"):
            raise HTTPException(status_code=400, detail="layout must be 'board' or 'list'.")
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            seq = int(cfg.get("next_view_seq", 1))
            cfg["next_view_seq"] = seq + 1
            cfg["updated_at"] = now_iso()
            vid = f"VIEW-{seq}"
            ts = now_iso()
            view = {
                "id": vid,
                "seq": seq,
                "name": name,
                "iql": iql_str,
                "layout": layout,
                "description": data.get("description") or "",
                "icon": data.get("icon") or "filter",
                "created_at": ts,
                "updated_at": ts,
            }
            self._atomic_write_json(self.config_path, cfg)
            self._atomic_write_json(self.views_dir / f"{vid}.json", view)
            return view

    def update_view(self, view_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        vid = view_id.strip().upper()
        with self.write_lock():
            p = self.views_dir / f"{vid}.json"
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"View '{vid}' not found.")
            view = self._read_json(p)
            if "iql" in patch and patch["iql"] is not None:
                try:
                    ast = parse_iql(patch["iql"])
                    compiler = self._make_compiler()
                    compiler.compile_where(ast["where"])
                    compiler.compile_order_by(ast["order_by"])
                except IQLError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid iQL: {e.message}")
                view["iql"] = patch["iql"]
            if "layout" in patch and patch["layout"] is not None:
                ly = str(patch["layout"]).strip().lower()
                if ly not in ("board", "list"):
                    raise HTTPException(status_code=400, detail="layout must be 'board' or 'list'.")
                view["layout"] = ly
            for k in ("name", "description", "icon"):
                if k in patch and patch[k] is not None:
                    view[k] = patch[k]
            view["updated_at"] = now_iso()
            self._atomic_write_json(p, view)
            return view

    def delete_view(self, view_id: str) -> Dict[str, Any]:
        vid = view_id.strip().upper()
        with self.write_lock():
            p = self.views_dir / f"{vid}.json"
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"View '{vid}' not found.")
            p.unlink(missing_ok=True)
            return {"deleted": True, "id": vid}

    # ------------------------------------------------------------------------
    # Issues CRUD, Lifecycle, Subtasks, Bulk & Agent Coordination
    # ------------------------------------------------------------------------

    def _clean_nullable_id(self, val: Optional[str]) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        if not s or s.lower() in ("none", "null", "unassigned", "backlog"):
            return None
        return s.upper()

    def _clean_assignee(self, val: Optional[str]) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        if not s or s.lower() in ("none", "null", "unassigned"):
            return None
        return s

    def _dedupe_strs(self, items: Optional[List[str]]) -> List[str]:
        if not items:
            return []
        seen: Set[str] = set()
        out: List[str] = []
        for x in items:
            s = str(x).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    def create_issue(self, data: Dict[str, Any]) -> Dict[str, Any]:
        title = (data.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Issue title cannot be empty.")
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            itype = (data.get("type") or cfg.get("default_type", "task")).strip().lower()
            if itype not in cfg["issue_types"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid issue type '{itype}'. Allowed: {', '.join(cfg['issue_types'])}.",
                )
            status = (data.get("status") or "todo").strip().lower()
            if status not in cfg["statuses"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status '{status}'. Allowed: {', '.join(cfg['statuses'])}.",
                )
            try:
                priority, p_rank = normalize_priority(data.get("priority"), cfg.get("default_priority", "medium"))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            parent_id = self._clean_nullable_id(data.get("parent_id"))
            if parent_id:
                self._get_raw_issue_or_404(parent_id)
            epic_id = self._clean_nullable_id(data.get("epic_id"))
            if epic_id:
                self._get_raw_issue_or_404(epic_id)
            sprint_id = self._clean_nullable_id(data.get("sprint_id"))
            if sprint_id:
                self._get_sprint_or_404(sprint_id)
            milestone_id = self._clean_nullable_id(data.get("milestone_id"))
            if milestone_id and not (self.milestones_dir / f"{milestone_id}.json").exists():
                raise HTTPException(status_code=404, detail=f"Milestone '{milestone_id}' not found.")

            seq = int(cfg.get("next_seq", 1))
            key = f"{cfg['project_key']}-{seq}"
            cfg["next_seq"] = seq + 1
            ts = now_iso()
            cfg["updated_at"] = ts

            actor = (data.get("actor") or data.get("reporter") or self.default_user).strip()
            reporter = (data.get("reporter") or actor).strip()
            points = data.get("story_points")
            if points is not None and points != "":
                points = float(points)
            else:
                points = None

            started_at = ts if status == "in_progress" else None
            resolved_at = ts if status in ("done", "canceled") else None
            closed_at = ts if status in ("done", "canceled") else None

            issue: Dict[str, Any] = {
                "key": key,
                "seq": seq,
                "title": title,
                "description": data.get("description") or "",
                "type": itype,
                "status": status,
                "priority": priority,
                "priority_rank": p_rank,
                "resolution": data.get("resolution") or ("done" if status == "done" else None),
                "assignee": self._clean_assignee(data.get("assignee")),
                "reporter": reporter,
                "labels": self._dedupe_strs(data.get("labels")),
                "components": self._dedupe_strs(data.get("components")),
                "epic_id": epic_id,
                "parent_id": parent_id,
                "sprint_id": sprint_id,
                "milestone_id": milestone_id,
                "story_points": points,
                "due_date": data.get("due_date") or None,
                "environment": data.get("environment") or None,
                "custom_fields": dict(data.get("custom_fields") or {}),
                "watchers": [],
                "claimed_by": None,
                "claimed_at": None,
                "comments": [],
                "attachments": [],
                "links": [],
                "worklogs": [],
                "history": [
                    {
                        "id": f"EVT-{short_id()}",
                        "actor": actor,
                        "action": "created",
                        "changes": {},
                        "timestamp": ts,
                    }
                ],
                "created_at": ts,
                "updated_at": ts,
                "started_at": started_at,
                "resolved_at": resolved_at,
                "closed_at": closed_at,
            }
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue(issue)
            return self._enrich_issues([issue])[0]

    def update_issue(self, key: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            iss = self._get_raw_issue_or_404(key)
            actor = (patch.get("actor") or self.default_user).strip()
            ts = now_iso()
            changes: Dict[str, Any] = {}

            def set_field(fname: str, new_val: Any) -> None:
                old_val = iss.get(fname)
                if old_val != new_val:
                    changes[fname] = {"from": old_val, "to": new_val}
                    iss[fname] = new_val

            if "title" in patch and patch["title"] is not None:
                t = str(patch["title"]).strip()
                if not t:
                    raise HTTPException(status_code=400, detail="Issue title cannot be empty.")
                set_field("title", t)

            if "description" in patch and patch["description"] is not None:
                set_field("description", str(patch["description"]))

            if "type" in patch and patch["type"] is not None:
                tp = str(patch["type"]).strip().lower()
                if tp not in cfg["issue_types"]:
                    raise HTTPException(status_code=400, detail=f"Invalid issue type '{tp}'.")
                set_field("type", tp)

            if "status" in patch and patch["status"] is not None:
                st = str(patch["status"]).strip().lower()
                if st not in cfg["statuses"]:
                    raise HTTPException(status_code=400, detail=f"Invalid status '{st}'.")
                set_field("status", st)
                self._apply_status_timestamps(iss, st, ts)

            if "priority" in patch and patch["priority"] is not None:
                try:
                    pr, prank = normalize_priority(str(patch["priority"]))
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                set_field("priority", pr)
                iss["priority_rank"] = prank

            if "resolution" in patch:
                res = patch["resolution"]
                if isinstance(res, str) and res.strip().lower() in ("none", "null", ""):
                    res = None
                set_field("resolution", res)

            if "assignee" in patch:
                set_field("assignee", self._clean_assignee(patch["assignee"]))

            if "reporter" in patch and patch["reporter"] is not None:
                set_field("reporter", str(patch["reporter"]).strip())

            if "labels" in patch and patch["labels"] is not None:
                set_field("labels", self._dedupe_strs(patch["labels"]))

            if "add_labels" in patch and patch["add_labels"]:
                merged = self._dedupe_strs(list(iss.get("labels", [])) + list(patch["add_labels"]))
                set_field("labels", merged)

            if "remove_labels" in patch and patch["remove_labels"]:
                rem_set = {str(x).strip().lower() for x in patch["remove_labels"]}
                filtered = [l for l in iss.get("labels", []) if l.lower() not in rem_set]
                set_field("labels", filtered)

            if "components" in patch and patch["components"] is not None:
                set_field("components", self._dedupe_strs(patch["components"]))

            if "epic_id" in patch:
                eid = self._clean_nullable_id(patch["epic_id"])
                if eid:
                    if eid == iss["key"]:
                        raise HTTPException(status_code=400, detail="Issue cannot be its own epic.")
                    self._get_raw_issue_or_404(eid)
                set_field("epic_id", eid)

            if "parent_id" in patch:
                pid = self._clean_nullable_id(patch["parent_id"])
                if pid:
                    if pid == iss["key"]:
                        raise HTTPException(status_code=400, detail="Issue cannot be its own parent.")
                    self._get_raw_issue_or_404(pid)
                set_field("parent_id", pid)

            if "sprint_id" in patch:
                sid = self._clean_nullable_id(patch["sprint_id"])
                if sid:
                    self._get_sprint_or_404(sid)
                set_field("sprint_id", sid)

            if "milestone_id" in patch:
                mid = self._clean_nullable_id(patch["milestone_id"])
                if mid and not (self.milestones_dir / f"{mid}.json").exists():
                    raise HTTPException(status_code=404, detail=f"Milestone '{mid}' not found.")
                set_field("milestone_id", mid)

            if "story_points" in patch:
                sp = patch["story_points"]
                sp_val = float(sp) if (sp is not None and sp != "") else None
                set_field("story_points", sp_val)

            if "due_date" in patch:
                dd = patch["due_date"]
                dd_val = str(dd).strip() if (dd is not None and str(dd).strip().lower() not in ("", "none", "null")) else None
                set_field("due_date", dd_val)

            if "environment" in patch:
                set_field("environment", patch["environment"] or None)

            if "custom_fields" in patch and patch["custom_fields"] is not None:
                merged_cf = dict(iss.get("custom_fields") or {})
                merged_cf.update(patch["custom_fields"])
                set_field("custom_fields", merged_cf)

            if changes:
                iss["updated_at"] = ts
                iss.setdefault("history", []).append(
                    {
                        "id": f"EVT-{short_id()}",
                        "actor": actor,
                        "action": "updated",
                        "changes": changes,
                        "timestamp": ts,
                    }
                )
                self._save_issue(iss)

            return self._enrich_issues([iss])[0]

    def _apply_status_timestamps(self, iss: Dict[str, Any], new_status: str, ts: str) -> None:
        if new_status == "in_progress" and not iss.get("started_at"):
            iss["started_at"] = ts
        if new_status in ("done", "canceled"):
            iss["resolved_at"] = iss.get("resolved_at") or ts
            iss["closed_at"] = ts
            if not iss.get("resolution"):
                iss["resolution"] = "done" if new_status == "done" else "canceled"
            # Clear active agent claim when done/canceled
            iss["claimed_by"] = None
            iss["claimed_at"] = None
        else:
            iss["resolved_at"] = None
            iss["closed_at"] = None
            if iss.get("resolution") in ("done", "canceled"):
                iss["resolution"] = None

    def transition_issue(
        self,
        key: str,
        target_status: str,
        resolution: Optional[str] = None,
        comment: Optional[str] = None,
        force: bool = False,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            iss = self._get_raw_issue_or_404(key)
            target = (target_status or "").strip().lower()
            if target not in cfg["statuses"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown status '{target}'. Allowed statuses: {', '.join(cfg['statuses'])}.",
                )
            cur_status = iss["status"]
            if cur_status != target and not force:
                allowed = cfg.get("transitions", {}).get(cur_status, [])
                if target not in allowed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid workflow transition from '{cur_status}' to '{target}'. Allowed transitions: {allowed}. Pass force=true to override.",
                    )
            ts = now_iso()
            eff_actor = (actor or self.default_user).strip()
            changes: Dict[str, Any] = {}
            if cur_status != target:
                changes["status"] = {"from": cur_status, "to": target}
                iss["status"] = target
                self._apply_status_timestamps(iss, target, ts)
            if resolution is not None:
                changes["resolution"] = {"from": iss.get("resolution"), "to": resolution}
                iss["resolution"] = resolution
            if comment and comment.strip():
                iss.setdefault("comments", []).append(
                    {
                        "id": f"CMT-{short_id()}",
                        "author": eff_actor,
                        "body": comment.strip(),
                        "created_at": ts,
                        "updated_at": ts,
                    }
                )
            iss["updated_at"] = ts
            iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": eff_actor,
                    "action": "transitioned",
                    "changes": changes or {"status": {"from": cur_status, "to": target}},
                    "timestamp": ts,
                }
            )
            self._save_issue(iss)
            return self._enrich_issues([iss])[0]

    def assign_issue(self, key: str, assignee: Optional[str], actor: Optional[str] = None) -> Dict[str, Any]:
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            clean_a = self._clean_assignee(assignee)
            old_a = iss.get("assignee")
            ts = now_iso()
            iss["assignee"] = clean_a
            iss["updated_at"] = ts
            iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": (actor or self.default_user).strip(),
                    "action": "assigned",
                    "changes": {"assignee": {"from": old_a, "to": clean_a}},
                    "timestamp": ts,
                }
            )
            self._save_issue(iss)
            return self._enrich_issues([iss])[0]

    def claim_issue(self, key: str, agent: str, note: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        clean_agent = (agent or "").strip()
        if not clean_agent:
            raise HTTPException(status_code=400, detail="agent identifier is required.")
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            cur_claim = iss.get("claimed_by")
            cur_status = iss.get("status")
            if not force:
                if cur_claim and cur_claim != clean_agent:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Issue {iss['key']} is already claimed by '{cur_claim}'. Pass force=true to override.",
                    )
                if cur_status == "in_progress" and cur_claim != clean_agent:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Issue {iss['key']} is already in_progress (assignee={iss.get('assignee')!r}, claimed_by={cur_claim!r}). Pass force=true to override.",
                    )
            ts = now_iso()
            old_status = cur_status
            old_assignee = iss.get("assignee")
            iss["claimed_by"] = clean_agent
            iss["claimed_at"] = ts
            iss["assignee"] = clean_agent
            iss["status"] = "in_progress"
            iss["started_at"] = iss.get("started_at") or ts
            iss["resolved_at"] = None
            iss["closed_at"] = None
            if note and note.strip():
                iss.setdefault("comments", []).append(
                    {
                        "id": f"CMT-{short_id()}",
                        "author": clean_agent,
                        "body": note.strip(),
                        "created_at": ts,
                        "updated_at": ts,
                    }
                )
            iss["updated_at"] = ts
            iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": clean_agent,
                    "action": "claimed",
                    "changes": {
                        "claimed_by": {"from": cur_claim, "to": clean_agent},
                        "status": {"from": old_status, "to": "in_progress"},
                        "assignee": {"from": old_assignee, "to": clean_agent},
                    },
                    "timestamp": ts,
                }
            )
            self._save_issue(iss)
            return self._enrich_issues([iss])[0]

    def release_issue(
        self,
        key: str,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.write_lock():
            cfg = self._read_json(self.config_path)
            iss = self._get_raw_issue_or_404(key)
            cur_claim = iss.get("claimed_by")
            if agent and cur_claim and cur_claim != agent.strip():
                raise HTTPException(
                    status_code=409,
                    detail=f"Issue {iss['key']} is claimed by '{cur_claim}', not '{agent}'.",
                )
            target_status = (status or "todo").strip().lower()
            if target_status not in cfg["statuses"]:
                raise HTTPException(status_code=400, detail=f"Invalid status '{target_status}'.")
            ts = now_iso()
            actor = (agent or cur_claim or self.default_user).strip()
            old_status = iss["status"]
            iss["claimed_by"] = None
            iss["claimed_at"] = None
            iss["status"] = target_status
            self._apply_status_timestamps(iss, target_status, ts)
            if note and note.strip():
                iss.setdefault("comments", []).append(
                    {
                        "id": f"CMT-{short_id()}",
                        "author": actor,
                        "body": note.strip(),
                        "created_at": ts,
                        "updated_at": ts,
                    }
                )
            iss["updated_at"] = ts
            iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": actor,
                    "action": "released",
                    "changes": {
                        "claimed_by": {"from": cur_claim, "to": None},
                        "status": {"from": old_status, "to": target_status},
                    },
                    "timestamp": ts,
                }
            )
            self._save_issue(iss)
            return self._enrich_issues([iss])[0]

    def clone_issue(
        self,
        key: str,
        title_prefix: str = "[Clone] ",
        include_subtasks: bool = False,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.write_lock():
            orig = self._get_raw_issue_or_404(key)
            eff_actor = (actor or self.default_user).strip()
            cloned = self.create_issue(
                {
                    "title": f"{title_prefix}{orig['title']}",
                    "description": orig.get("description", ""),
                    "type": orig.get("type", "task"),
                    "status": "todo",
                    "priority": orig.get("priority", "medium"),
                    "assignee": orig.get("assignee"),
                    "reporter": eff_actor,
                    "labels": list(orig.get("labels", [])),
                    "components": list(orig.get("components", [])),
                    "epic_id": orig.get("epic_id"),
                    "parent_id": orig.get("parent_id"),
                    "sprint_id": orig.get("sprint_id"),
                    "milestone_id": orig.get("milestone_id"),
                    "story_points": orig.get("story_points"),
                    "due_date": orig.get("due_date"),
                    "environment": orig.get("environment"),
                    "custom_fields": dict(orig.get("custom_fields") or {}),
                    "actor": eff_actor,
                }
            )
            self.add_link(cloned["key"], orig["key"], "clones", actor=eff_actor)
            if include_subtasks:
                sub_rows = self._db.execute(
                    "SELECT key FROM issues WHERE parent_id = ? ORDER BY seq ASC", (orig["key"],)
                ).fetchall()
                for sr in sub_rows:
                    sub = self._get_raw_issue_or_404(sr["key"])
                    self.create_issue(
                        {
                            "title": sub["title"],
                            "description": sub.get("description", ""),
                            "type": "subtask",
                            "status": "todo",
                            "priority": sub.get("priority", "medium"),
                            "assignee": sub.get("assignee"),
                            "reporter": eff_actor,
                            "labels": list(sub.get("labels", [])),
                            "components": list(sub.get("components", [])),
                            "epic_id": cloned.get("epic_id"),
                            "parent_id": cloned["key"],
                            "sprint_id": cloned.get("sprint_id"),
                            "story_points": sub.get("story_points"),
                            "actor": eff_actor,
                        }
                    )
            return self.get_issue(cloned["key"])

    def delete_issue(self, key: str) -> Dict[str, Any]:
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            real_key = iss["key"]
            ts = now_iso()

            # Clean inbound links on peer issues
            peer_rows = self._db.execute(
                "SELECT DISTINCT source_key FROM issue_links WHERE target_key = ? AND source_key != ?",
                (real_key, real_key),
            ).fetchall()
            for pr in peer_rows:
                peer = self._get_raw_issue_or_404(pr["source_key"])
                peer["links"] = [l for l in peer.get("links", []) if l["target_key"] != real_key]
                peer["updated_at"] = ts
                self._save_issue(peer)

            # Clear parent_id / epic_id references
            ref_rows = self._db.execute(
                "SELECT key FROM issues WHERE parent_id = ? OR epic_id = ?",
                (real_key, real_key),
            ).fetchall()
            for rr in ref_rows:
                if rr["key"] == real_key:
                    continue
                child = self._get_raw_issue_or_404(rr["key"])
                if child.get("parent_id") == real_key:
                    child["parent_id"] = None
                if child.get("epic_id") == real_key:
                    child["epic_id"] = None
                child["updated_at"] = ts
                self._save_issue(child)

            # Remove attachments folder & JSON file & index rows
            att_dir = self.attachments_dir / real_key
            if att_dir.exists():
                shutil.rmtree(att_dir, ignore_errors=True)
            (self.issues_dir / f"{real_key}.json").unlink(missing_ok=True)
            self._remove_issue_from_index(real_key)
            return {"deleted": True, "key": real_key}

    def list_subtasks(self, parent_key: str) -> Dict[str, Any]:
        with self.read_lock():
            parent = self._get_raw_issue_or_404(parent_key)
            pk = parent["key"]
            rows = self._db.execute(
                "SELECT raw_json FROM issues WHERE parent_id = ? ORDER BY seq ASC", (pk,)
            ).fetchall()
            subs = self._enrich_issues([json.loads(r["raw_json"]) for r in rows])
            done_cnt = sum(1 for s in subs if s["status"] == "done")
            return {
                "parent_key": pk,
                "subtasks": subs,
                "completed_count": done_cnt,
                "total_count": len(subs),
            }

    def create_subtask(self, parent_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.write_lock():
            parent = self._get_raw_issue_or_404(parent_key)
            pk = parent["key"]
            payload = {
                "title": data.get("title"),
                "description": data.get("description") or "",
                "type": "subtask",
                "status": data.get("status") or "todo",
                "priority": data.get("priority") or parent.get("priority", "medium"),
                "assignee": data.get("assignee"),
                "story_points": data.get("story_points"),
                "parent_id": pk,
                "epic_id": parent["key"] if parent.get("type") == "epic" else parent.get("epic_id"),
                "sprint_id": parent.get("sprint_id"),
                "milestone_id": parent.get("milestone_id"),
                "actor": data.get("actor"),
            }
            return self.create_issue(payload)

    def bulk_action(
        self,
        keys: List[str],
        action: str,
        fields: Dict[str, Any],
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        act = (action or "").strip().lower()
        if act not in ("update", "transition", "delete", "label_add", "label_remove", "sprint"):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported bulk action '{action}'. Expected update, transition, delete, label_add, label_remove, or sprint.",
            )
        updated: List[str] = []
        deleted: List[str] = []
        errors: List[Dict[str, Any]] = []
        with self.write_lock():
            for raw_k in keys:
                k = raw_k.strip().upper()
                if not k:
                    continue
                try:
                    if act == "delete":
                        res = self.delete_issue(k)
                        deleted.append(res["key"])
                    elif act == "transition":
                        st = fields.get("status")
                        if not st:
                            raise HTTPException(status_code=400, detail="fields.status required for bulk transition.")
                        iss = self.transition_issue(
                            k,
                            target_status=st,
                            resolution=fields.get("resolution"),
                            force=bool(fields.get("force", True)),
                            actor=actor,
                        )
                        updated.append(iss["key"])
                    elif act == "label_add":
                        lb = fields.get("label") or fields.get("labels")
                        lbs = lb if isinstance(lb, list) else ([lb] if lb else [])
                        iss = self.update_issue(k, {"add_labels": lbs, "actor": actor})
                        updated.append(iss["key"])
                    elif act == "label_remove":
                        lb = fields.get("label") or fields.get("labels")
                        lbs = lb if isinstance(lb, list) else ([lb] if lb else [])
                        iss = self.update_issue(k, {"remove_labels": lbs, "actor": actor})
                        updated.append(iss["key"])
                    elif act == "sprint":
                        sid = fields.get("sprint_id") if "sprint_id" in fields else fields.get("sprint")
                        iss = self.update_issue(k, {"sprint_id": sid, "actor": actor})
                        updated.append(iss["key"])
                    elif act == "update":
                        patch = dict(fields)
                        if actor:
                            patch["actor"] = actor
                        iss = self.update_issue(k, patch)
                        updated.append(iss["key"])
                except HTTPException as he:
                    errors.append({"key": k, "status_code": he.status_code, "error": he.detail})
                except Exception as exc:
                    errors.append({"key": k, "status_code": 400, "error": str(exc)})
        return {"updated": updated, "deleted": deleted, "errors": errors}

    # ------------------------------------------------------------------------
    # Watchers, Comments, Attachments, Links (DAG Cycle Check), Worklogs
    # ------------------------------------------------------------------------

    def add_watcher(self, key: str, username: str) -> Dict[str, Any]:
        uname = (username or "").strip()
        if not uname:
            raise HTTPException(status_code=400, detail="username is required.")
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            watchers = list(iss.get("watchers", []))
            if uname not in watchers:
                watchers.append(uname)
                iss["watchers"] = watchers
                iss["updated_at"] = now_iso()
                self._save_issue(iss)
            return {"key": iss["key"], "watchers": watchers}

    def remove_watcher(self, key: str, username: str) -> Dict[str, Any]:
        uname = username.strip()
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            watchers = [w for w in iss.get("watchers", []) if w != uname]
            iss["watchers"] = watchers
            iss["updated_at"] = now_iso()
            self._save_issue(iss)
            return {"key": iss["key"], "watchers": watchers}

    def list_comments(self, key: str) -> Dict[str, Any]:
        with self.read_lock():
            iss = self._get_raw_issue_or_404(key)
            return {"key": iss["key"], "comments": iss.get("comments", [])}

    def add_comment(self, key: str, body: str, author: Optional[str] = None) -> Dict[str, Any]:
        text = (body or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Comment body cannot be empty.")
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            ts = now_iso()
            eff_author = (author or self.default_user).strip()
            cmt = {
                "id": f"CMT-{short_id()}",
                "author": eff_author,
                "body": text,
                "created_at": ts,
                "updated_at": ts,
            }
            iss.setdefault("comments", []).append(cmt)
            iss["updated_at"] = ts
            iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": eff_author,
                    "action": "comment_added",
                    "changes": {"comment_id": cmt["id"]},
                    "timestamp": ts,
                }
            )
            self._save_issue(iss)
            return cmt

    def edit_comment(self, key: str, comment_id: str, body: str, actor: Optional[str] = None) -> Dict[str, Any]:
        text = (body or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Comment body cannot be empty.")
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            cmt = next((c for c in iss.get("comments", []) if c["id"] == comment_id), None)
            if not cmt:
                raise HTTPException(status_code=404, detail=f"Comment '{comment_id}' not found on {iss['key']}.")
            ts = now_iso()
            cmt["body"] = text
            cmt["updated_at"] = ts
            iss["updated_at"] = ts
            self._save_issue(iss)
            return cmt

    def delete_comment(self, key: str, comment_id: str) -> Dict[str, Any]:
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            comments = iss.get("comments", [])
            new_comments = [c for c in comments if c["id"] != comment_id]
            if len(new_comments) == len(comments):
                raise HTTPException(status_code=404, detail=f"Comment '{comment_id}' not found on {iss['key']}.")
            iss["comments"] = new_comments
            iss["updated_at"] = now_iso()
            self._save_issue(iss)
            return {"deleted": True, "id": comment_id}

    # Multipart Attachments
    def list_attachments(self, key: str) -> Dict[str, Any]:
        with self.read_lock():
            iss = self._get_raw_issue_or_404(key)
            return {"key": iss["key"], "attachments": iss.get("attachments", [])}

    def save_attachment(
        self,
        key: str,
        filename: Optional[str],
        content_type: Optional[str],
        stream: Any,
        uploaded_by: str = "system",
        description: str = "",
    ) -> Dict[str, Any]:
        safe_name = sanitize_filename(filename)
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            real_key = iss["key"]
            cfg = self._read_json(self.config_path)
            att_seq = int(cfg.get("next_attachment_seq", 1))
            cfg["next_attachment_seq"] = att_seq + 1
            att_id = f"ATT-{att_seq}"
            stored_name = f"{att_id}_{safe_name}"

            issue_att_dir = (self.attachments_dir / real_key).resolve()
            if not issue_att_dir.is_relative_to(self.attachments_dir.resolve()):
                raise HTTPException(status_code=400, detail="Invalid attachment storage path.")
            issue_att_dir.mkdir(parents=True, exist_ok=True)
            target_path = (issue_att_dir / stored_name).resolve()
            if not target_path.is_relative_to(issue_att_dir):
                raise HTTPException(status_code=400, detail="Invalid attachment filename.")

            tmp_path = issue_att_dir / f".{stored_name}.{short_id()}.tmp"
            hasher = hashlib.sha256()
            size_bytes = 0
            with open(tmp_path, "wb") as out_f:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size_bytes += len(chunk)
                    out_f.write(chunk)
                out_f.flush()
            os.replace(tmp_path, target_path)

            guessed_mime = mimetypes.guess_type(safe_name)[0]
            mime = content_type if (content_type and content_type != "application/octet-stream") else (guessed_mime or content_type or "application/octet-stream")
            ts = now_iso()
            actor = (uploaded_by or self.default_user).strip()
            att_meta = {
                "id": att_id,
                "filename": safe_name,
                "stored_name": stored_name,
                "mime_type": mime,
                "size_bytes": size_bytes,
                "sha256": hasher.hexdigest(),
                "description": description or "",
                "uploaded_by": actor,
                "created_at": ts,
            }
            iss.setdefault("attachments", []).append(att_meta)
            iss["updated_at"] = ts
            cfg["updated_at"] = ts
            iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": actor,
                    "action": "attachment_added",
                    "changes": {"attachment_id": att_id, "filename": safe_name},
                    "timestamp": ts,
                }
            )
            self._atomic_write_json(self.config_path, cfg)
            self._save_issue(iss)
            return att_meta

    def get_attachment_download_info(self, key: str, attachment_id: str) -> Tuple[Path, Dict[str, Any]]:
        with self.read_lock():
            iss = self._get_raw_issue_or_404(key)
            real_key = iss["key"]
            att_norm = attachment_id.strip().upper()
            att = next(
                (a for a in iss.get("attachments", []) if a["id"].upper() == att_norm),
                None,
            )
            if not att:
                raise HTTPException(status_code=404, detail=f"Attachment '{attachment_id}' not found on {real_key}.")
            fpath = (self.attachments_dir / real_key / att["stored_name"]).resolve()
            if not fpath.is_relative_to(self.attachments_dir.resolve()) or not fpath.exists():
                raise HTTPException(status_code=404, detail=f"Attachment file missing on disk for '{attachment_id}'.")
            return fpath, att

    def delete_attachment(self, key: str, attachment_id: str) -> Dict[str, Any]:
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            real_key = iss["key"]
            att_norm = attachment_id.strip().upper()
            att = next((a for a in iss.get("attachments", []) if a["id"].upper() == att_norm), None)
            if not att:
                raise HTTPException(status_code=404, detail=f"Attachment '{attachment_id}' not found on {real_key}.")
            fpath = (self.attachments_dir / real_key / att["stored_name"]).resolve()
            if fpath.is_relative_to(self.attachments_dir.resolve()):
                fpath.unlink(missing_ok=True)
            iss["attachments"] = [a for a in iss.get("attachments", []) if a["id"].upper() != att_norm]
            iss["updated_at"] = now_iso()
            self._save_issue(iss)
            return {"deleted": True, "id": att["id"]}

    # Links & DAG Cycle Detection
    def list_links(self, key: str) -> Dict[str, Any]:
        iss = self.get_issue(key)
        return {"key": iss["key"], "links": iss["all_links"]}

    def _find_blocking_path(self, start: str, goal: str) -> Optional[List[str]]:
        """BFS over current blocking graph (blocker -> blocked) to detect cycles."""
        adj: Dict[str, Set[str]] = {}
        rows = self._db.execute("SELECT source_key, target_key, link_type FROM issue_links").fetchall()
        for r in rows:
            lt = r["link_type"]
            if lt == "blocks":
                adj.setdefault(r["source_key"], set()).add(r["target_key"])
            elif lt == "blocked_by":
                adj.setdefault(r["target_key"], set()).add(r["source_key"])

        q: deque[List[str]] = deque([[start]])
        visited: Set[str] = {start}
        while q:
            path = q.popleft()
            curr = path[-1]
            if curr == goal:
                return path
            for nxt in adj.get(curr, ()):
                if nxt not in visited:
                    visited.add(nxt)
                    q.append(path + [nxt])
        return None

    def add_link(self, source_key: str, target_key: str, link_type: str, actor: Optional[str] = None) -> Dict[str, Any]:
        try:
            norm_type = normalize_link_type(link_type)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        with self.write_lock():
            src_iss = self._get_raw_issue_or_404(source_key)
            tgt_iss = self._get_raw_issue_or_404(target_key)
            s_key = src_iss["key"]
            t_key = tgt_iss["key"]
            if s_key == t_key:
                raise HTTPException(status_code=400, detail="Cannot link an issue to itself.")

            # Duplicate check (direct or inverse)
            inv_type = INVERSE_LINK_TYPES[norm_type]
            dup = self._db.execute(
                """
                SELECT id FROM issue_links
                WHERE (source_key = ? AND target_key = ? AND link_type = ?)
                   OR (source_key = ? AND target_key = ? AND link_type = ?)
                """,
                (s_key, t_key, norm_type, t_key, s_key, inv_type),
            ).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail=f"Link '{s_key} {norm_type} {t_key}' already exists.")

            # DAG Cycle check for blocks / blocked_by
            if norm_type in ("blocks", "blocked_by"):
                blocker = s_key if norm_type == "blocks" else t_key
                blocked = t_key if norm_type == "blocks" else s_key
                existing_path = self._find_blocking_path(blocked, blocker)
                if existing_path is not None:
                    cycle_str = " -> ".join(existing_path + [blocked])
                    raise HTTPException(
                        status_code=409,
                        detail=f"Dependency cycle detected: adding '{blocker} blocks {blocked}' creates cycle ({cycle_str}).",
                    )

            ts = now_iso()
            eff_actor = (actor or self.default_user).strip()
            lnk = {
                "id": f"LNK-{short_id()}",
                "source_key": s_key,
                "target_key": t_key,
                "link_type": norm_type,
                "created_by": eff_actor,
                "created_at": ts,
            }
            src_iss.setdefault("links", []).append(lnk)
            src_iss["updated_at"] = ts
            src_iss.setdefault("history", []).append(
                {
                    "id": f"EVT-{short_id()}",
                    "actor": eff_actor,
                    "action": "link_added",
                    "changes": {"link": f"{s_key} {norm_type} {t_key}"},
                    "timestamp": ts,
                }
            )
            self._save_issue(src_iss)
            return lnk

    def delete_link(self, key: str, link_id: str) -> Dict[str, Any]:
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            real_key = iss["key"]
            row = self._db.execute(
                "SELECT id, source_key, target_key FROM issue_links WHERE id = ?", (link_id,)
            ).fetchone()
            if not row or (row["source_key"] != real_key and row["target_key"] != real_key):
                raise HTTPException(status_code=404, detail=f"Link '{link_id}' not found on {real_key}.")
            owner_iss = self._get_raw_issue_or_404(row["source_key"])
            owner_iss["links"] = [l for l in owner_iss.get("links", []) if l["id"] != link_id]
            owner_iss["updated_at"] = now_iso()
            self._save_issue(owner_iss)
            return {"deleted": True, "id": link_id}

    # Worklogs
    def list_worklogs(self, key: str) -> Dict[str, Any]:
        with self.read_lock():
            iss = self._get_raw_issue_or_404(key)
            wls = iss.get("worklogs", [])
            total = sum(int(w.get("minutes", 0)) for w in wls)
            return {"key": iss["key"], "worklogs": wls, "total_minutes": total}

    def add_worklog(
        self,
        key: str,
        minutes: Optional[int] = None,
        duration: Optional[str] = None,
        author: Optional[str] = None,
        note: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            mins = parse_duration_minutes(minutes, duration)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            ts = now_iso()
            eff_author = (author or self.default_user).strip()
            wl = {
                "id": f"WL-{short_id()}",
                "author": eff_author,
                "minutes": mins,
                "note": (note or "").strip(),
                "started_at": started_at or ts,
                "created_at": ts,
            }
            iss.setdefault("worklogs", []).append(wl)
            iss["updated_at"] = ts
            self._save_issue(iss)
            return wl

    def delete_worklog(self, key: str, worklog_id: str) -> Dict[str, Any]:
        with self.write_lock():
            iss = self._get_raw_issue_or_404(key)
            wls = iss.get("worklogs", [])
            new_wls = [w for w in wls if w["id"] != worklog_id]
            if len(new_wls) == len(wls):
                raise HTTPException(status_code=404, detail=f"Worklog '{worklog_id}' not found on {iss['key']}.")
            iss["worklogs"] = new_wls
            iss["updated_at"] = now_iso()
            self._save_issue(iss)
            return {"deleted": True, "id": worklog_id}

    # ------------------------------------------------------------------------
    # iQL Execution, Search, Ready Queue, Board, Epics, Graph, Stats, Export/Import
    # ------------------------------------------------------------------------

    def _make_compiler(self, current_user: Optional[str] = None) -> IQLCompiler:
        sprints = self.list_sprints()
        active_ids = [s["id"] for s in sprints if s.get("state") == "active"]
        name_map = {s["name"]: s["id"] for s in sprints if s.get("name")}
        return IQLCompiler(
            current_user=(current_user or self.default_user),
            active_sprint_ids=active_ids,
            sprint_name_to_id=name_map,
        )

    def execute_iql(
        self,
        iql: Optional[str] = None,
        extra_filters: Optional[List[Dict[str, Any]]] = None,
        sort: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        current_user: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
        with self.read_lock():
            try:
                ast = parse_iql(iql or "")
            except IQLError as e:
                raise HTTPException(status_code=400, detail=f"iQL error: {e.message}")

            where_node = ast["where"]
            if extra_filters:
                combined = ([where_node] if where_node else []) + extra_filters
                where_node = combined[0] if len(combined) == 1 else {"type": "and", "children": combined}

            compiler = self._make_compiler(current_user=current_user)
            try:
                where_sql, params = compiler.compile_where(where_node)
                order_sql = compiler.compile_order_by(ast["order_by"], default_sort=sort)
            except IQLError as e:
                raise HTTPException(status_code=400, detail=f"iQL compilation error: {e.message}")

            eff_limit = ast["limit"] if ast["limit"] is not None else max(0, int(limit))
            eff_offset = ast["offset"] if ast["offset"] is not None else max(0, int(offset))

            try:
                total_row = self._db.execute(
                    f"SELECT COUNT(*) AS c FROM issues WHERE {where_sql}", params
                ).fetchone()
                total = int(total_row["c"]) if total_row else 0

                rows = self._db.execute(
                    f"SELECT raw_json FROM issues WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    [*params, eff_limit, eff_offset],
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise HTTPException(status_code=400, detail=f"iQL query execution error: {exc}")
            issues = self._enrich_issues([json.loads(r["raw_json"]) for r in rows])
            return issues, total, ast

    def get_ready_issues(
        self,
        assignee: Optional[str] = None,
        sprint_id: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        filters: List[Dict[str, Any]] = [
            {"type": "in", "field": "status", "raw_field": "status", "negated": False, "values": ["backlog", "todo"]},
            {"type": "cmp", "field": "is_blocked", "raw_field": "blocked", "op": "=", "value": "false"},
            {"type": "empty", "field": "claimed_by", "raw_field": "claimed_by", "negated": False},
        ]
        if assignee:
            filters.append({"type": "cmp", "field": "assignee", "raw_field": "assignee", "op": "=", "value": assignee})
        if sprint_id:
            filters.append({"type": "cmp", "field": "sprint_id", "raw_field": "sprint_id", "op": "=", "value": sprint_id})
        issues, total, _ = self.execute_iql(
            iql="ORDER BY priority DESC, seq ASC",
            extra_filters=filters,
            limit=limit,
            offset=0,
        )
        return {"issues": issues, "total": total}

    def search_issues(
        self,
        q: Optional[str] = None,
        iql: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        with self.read_lock():
            snippets: Dict[str, str] = {}
            extra: List[Dict[str, Any]] = []
            if q and q.strip():
                extra.append({"type": "cmp", "field": "text", "raw_field": "text", "op": "~", "value": q.strip()})

            if iql and iql.strip():
                issues, total, _ = self.execute_iql(
                    iql=iql,
                    extra_filters=extra or None,
                    limit=limit,
                    offset=offset,
                )
            elif q and q.strip():
                fts_q = build_fts_match_expr(q.strip())
                if fts_q:
                    fts_rows = self._db.execute(
                        "SELECT key, snippet(issues_fts, -1, '<mark>', '</mark>', '...', 16) AS snip "
                        "FROM issues_fts WHERE issues_fts MATCH ? ORDER BY bm25(issues_fts)",
                        (fts_q,),
                    ).fetchall()
                    for fr in fts_rows:
                        snippets[fr["key"]] = fr["snip"]
                issues, total, _ = self.execute_iql(
                    iql="",
                    extra_filters=extra,
                    sort="-priority,seq",
                    limit=limit,
                    offset=offset,
                )
            else:
                issues, total, _ = self.execute_iql(iql="", limit=limit, offset=offset)

            # Populate snippets for matched issues even when matched via iQL or substring
            needle = (q or "").strip().lower()
            for iss in issues:
                k = iss["key"]
                if k not in snippets:
                    body = iss.get("description") or iss.get("title") or ""
                    if needle and needle in body.lower():
                        idx = body.lower().find(needle)
                        start = max(0, idx - 36)
                        end = min(len(body), idx + len(needle) + 64)
                        snippets[k] = ("..." if start > 0 else "") + body[start:end] + ("..." if end < len(body) else "")
                    else:
                        snippets[k] = (iss.get("description") or iss.get("title") or "")[:120]

            return {
                "issues": issues,
                "snippets": snippets,
                "total": total,
                "query": iql if (iql and iql.strip()) else (q or ""),
            }

    def get_board(
        self,
        sprint_id: Optional[str] = None,
        epic_id: Optional[str] = None,
        assignee: Optional[str] = None,
        iql: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.read_lock():
            cfg = self._read_json(self.config_path)
            extra: List[Dict[str, Any]] = []
            if sprint_id:
                extra.append({"type": "cmp", "field": "sprint_id", "raw_field": "sprint_id", "op": "=", "value": sprint_id})
            if epic_id:
                extra.append({"type": "cmp", "field": "epic_id", "raw_field": "epic_id", "op": "=", "value": epic_id})
            if assignee:
                extra.append({"type": "cmp", "field": "assignee", "raw_field": "assignee", "op": "=", "value": assignee})

            issues, _, _ = self.execute_iql(
                iql=iql or "",
                extra_filters=extra or None,
                sort="-priority,seq",
                limit=2000,
                offset=0,
            )
            statuses = list(cfg.get("statuses", DEFAULT_STATUSES))
            for iss in issues:
                if iss["status"] not in statuses:
                    statuses.append(iss["status"])

            wip_limits = cfg.get("wip_limits") or {}
            by_status: Dict[str, List[Dict[str, Any]]] = {st: [] for st in statuses}
            for iss in issues:
                by_status[iss["status"]].append(iss)

            columns: List[Dict[str, Any]] = []
            for st in statuses:
                col_issues = by_status[st]
                pts = round(sum(float(i.get("story_points") or 0.0) for i in col_issues), 2)
                columns.append(
                    {
                        "status": st,
                        "issues": col_issues,
                        "count": len(col_issues),
                        "points": pts,
                        "wip_limit": wip_limits.get(st),
                    }
                )
            return {"columns": columns, "total_issues": len(issues)}

    def get_epics(self) -> Dict[str, Any]:
        with self.read_lock():
            epic_rows = self._db.execute(
                "SELECT raw_json FROM issues WHERE type = 'epic' ORDER BY priority_rank DESC, seq ASC"
            ).fetchall()
            epics = self._enrich_issues([json.loads(r["raw_json"]) for r in epic_rows])
            out: List[Dict[str, Any]] = []
            for ep in epics:
                ekey = ep["key"]
                child_rows = self._db.execute(
                    "SELECT key, status, story_points FROM issues WHERE (epic_id = ? OR parent_id = ?) AND key != ? ORDER BY seq ASC",
                    (ekey, ekey, ekey),
                ).fetchall()
                child_count = len(child_rows)
                done_count = sum(1 for r in child_rows if r["status"] == "done")
                total_pts = round(sum(float(r["story_points"] or 0.0) for r in child_rows), 2)
                done_pts = round(
                    sum(float(r["story_points"] or 0.0) for r in child_rows if r["status"] == "done"), 2
                )
                if child_count > 0:
                    pct = round(done_count / child_count * 100.0, 1)
                else:
                    pct = 100.0 if ep["status"] == "done" else 0.0
                out.append(
                    {
                        "issue": ep,
                        "child_count": child_count,
                        "done_count": done_count,
                        "total_points": total_pts,
                        "done_points": done_pts,
                        "progress_pct": pct,
                        "children": [r["key"] for r in child_rows],
                    }
                )
            return {"epics": out}

    def get_dependency_graph(self) -> Dict[str, Any]:
        with self.read_lock():
            rows = self._db.execute("SELECT raw_json FROM issues ORDER BY seq ASC").fetchall()
            issues = self._enrich_issues([json.loads(r["raw_json"]) for r in rows])
            nodes = [
                {
                    "key": i["key"],
                    "title": i["title"],
                    "status": i["status"],
                    "priority": i["priority"],
                    "assignee": i.get("assignee"),
                    "is_blocked": i["is_blocked"],
                }
                for i in issues
            ]
            link_rows = self._db.execute(
                "SELECT id, source_key, target_key, link_type FROM issue_links"
            ).fetchall()
            edges: List[Dict[str, Any]] = []
            seen_edges: Set[Tuple[str, str, str]] = set()
            for lr in link_rows:
                lt = lr["link_type"]
                src = lr["source_key"]
                tgt = lr["target_key"]
                if lt == "blocked_by":
                    src, tgt, lt = tgt, src, "blocks"
                elif lt == "duplicated_by":
                    src, tgt, lt = tgt, src, "duplicates"
                elif lt == "cloned_from":
                    src, tgt, lt = tgt, src, "clones"
                sig = (src, tgt, lt)
                if sig not in seen_edges:
                    seen_edges.add(sig)
                    edges.append({"id": lr["id"], "source": src, "target": tgt, "type": lt})

            blocked_keys = [
                i["key"] for i in issues if i["is_blocked"] and i["status"] not in ("done", "canceled")
            ]
            ready_keys = [
                i["key"]
                for i in issues
                if i["status"] in ("backlog", "todo") and not i["is_blocked"] and not i.get("claimed_by")
            ]
            return {
                "nodes": nodes,
                "edges": edges,
                "blocked_keys": blocked_keys,
                "ready_keys": ready_keys,
            }

    def get_stats(self) -> Dict[str, Any]:
        with self.read_lock():
            rows = self._db.execute("SELECT raw_json FROM issues ORDER BY seq ASC").fetchall()
            issues = self._enrich_issues([json.loads(r["raw_json"]) for r in rows])
            total_issues = len(issues)
            open_count = sum(1 for i in issues if i["status"] not in ("done", "canceled"))
            done_count = sum(1 for i in issues if i["status"] == "done")
            blocked_count = sum(
                1 for i in issues if i["is_blocked"] and i["status"] not in ("done", "canceled")
            )
            ready_count = sum(
                1
                for i in issues
                if i["status"] in ("backlog", "todo") and not i["is_blocked"] and not i.get("claimed_by")
            )
            total_points = round(sum(float(i.get("story_points") or 0.0) for i in issues), 2)
            completed_points = round(
                sum(float(i.get("story_points") or 0.0) for i in issues if i["status"] == "done"), 2
            )

            by_status: Dict[str, int] = {}
            by_priority: Dict[str, int] = {}
            by_type: Dict[str, int] = {}
            by_assignee: Dict[str, int] = {}
            recent_events: List[Dict[str, Any]] = []

            for i in issues:
                by_status[i["status"]] = by_status.get(i["status"], 0) + 1
                by_priority[i["priority"]] = by_priority.get(i["priority"], 0) + 1
                by_type[i["type"]] = by_type.get(i["type"], 0) + 1
                assignee_key = i.get("assignee") or "unassigned"
                by_assignee[assignee_key] = by_assignee.get(assignee_key, 0) + 1
                for ev in i.get("history", []):
                    recent_events.append(
                        {
                            "issue_key": i["key"],
                            "issue_title": i["title"],
                            "id": ev.get("id"),
                            "actor": ev.get("actor"),
                            "action": ev.get("action"),
                            "changes": ev.get("changes", {}),
                            "timestamp": ev.get("timestamp", ""),
                        }
                    )

            recent_events.sort(key=lambda e: e["timestamp"], reverse=True)
            active_sprints = self.list_sprints(state="active")
            return {
                "total_issues": total_issues,
                "open_count": open_count,
                "done_count": done_count,
                "blocked_count": blocked_count,
                "ready_count": ready_count,
                "total_points": total_points,
                "completed_points": completed_points,
                "by_status": by_status,
                "by_priority": by_priority,
                "by_type": by_type,
                "by_assignee": by_assignee,
                "active_sprints": active_sprints,
                "recent_activity": recent_events[:25],
            }

    def export_bundle(self) -> Dict[str, Any]:
        with self.read_lock():
            return {
                "version": VERSION,
                "exported_at": now_iso(),
                "config": self._read_json(self.config_path),
                "labels": self.list_labels(),
                "components": self.list_components(),
                "users": self.list_users(),
                "issues": self._list_dir_json(self.issues_dir),
                "sprints": self._list_dir_json(self.sprints_dir),
                "milestones": self._list_dir_json(self.milestones_dir),
                "views": self._list_dir_json(self.views_dir),
            }

    def import_bundle(self, bundle: Dict[str, Any], mode: str = "merge") -> Dict[str, Any]:
        if mode not in ("merge", "replace"):
            raise HTTPException(status_code=400, detail="Import mode must be 'merge' or 'replace'.")
        if not isinstance(bundle, dict):
            raise HTTPException(status_code=400, detail="Invalid bundle object.")
        with self.write_lock():
            if mode == "replace":
                for d in (self.issues_dir, self.sprints_dir, self.milestones_dir, self.views_dir):
                    for p in d.glob("*.json"):
                        p.unlink(missing_ok=True)
                if "config" in bundle and isinstance(bundle["config"], dict):
                    self._atomic_write_json(self.config_path, bundle["config"])
                if "labels" in bundle and isinstance(bundle["labels"], list):
                    self._atomic_write_json(self.labels_path, {"labels": bundle["labels"]})
                if "components" in bundle and isinstance(bundle["components"], list):
                    self._atomic_write_json(self.components_path, {"components": bundle["components"]})
                if "users" in bundle and isinstance(bundle["users"], list):
                    self._atomic_write_json(self.users_path, {"users": bundle["users"]})
            else:
                for lb in bundle.get("labels", []):
                    if isinstance(lb, dict) and lb.get("name"):
                        self.add_label(lb["name"], lb.get("color"), lb.get("description"))
                for cp in bundle.get("components", []):
                    if isinstance(cp, dict) and cp.get("name"):
                        self.add_component(cp["name"], cp.get("lead"), cp.get("description"))
                for u in bundle.get("users", []):
                    if isinstance(u, dict) and u.get("username"):
                        self.upsert_user(u)

            n_sprints = 0
            max_spr_seq = 0
            for sp in bundle.get("sprints", []):
                if isinstance(sp, dict) and sp.get("id"):
                    sid = sp["id"].upper()
                    sp["id"] = sid
                    m = re.search(r"-(\d+)$", sid)
                    seq = int(sp.get("seq") or (int(m.group(1)) if m else (n_sprints + 1)))
                    sp["seq"] = seq
                    max_spr_seq = max(max_spr_seq, seq)
                    self._atomic_write_json(self.sprints_dir / f"{sid}.json", sp)
                    n_sprints += 1

            n_milestones = 0
            max_mil_seq = 0
            for ms in bundle.get("milestones", []):
                if isinstance(ms, dict) and ms.get("id"):
                    mid = ms["id"].upper()
                    ms["id"] = mid
                    m = re.search(r"-(\d+)$", mid)
                    seq = int(ms.get("seq") or (int(m.group(1)) if m else (n_milestones + 1)))
                    ms["seq"] = seq
                    max_mil_seq = max(max_mil_seq, seq)
                    self._atomic_write_json(self.milestones_dir / f"{mid}.json", ms)
                    n_milestones += 1

            max_view_seq = 0
            for vw in bundle.get("views", []):
                if isinstance(vw, dict) and vw.get("id"):
                    vid = vw["id"].upper()
                    vw["id"] = vid
                    m = re.search(r"-(\d+)$", vid)
                    seq = int(vw.get("seq") or (int(m.group(1)) if m else 1))
                    vw["seq"] = seq
                    max_view_seq = max(max_view_seq, seq)
                    self._atomic_write_json(self.views_dir / f"{vid}.json", vw)

            n_issues = 0
            for iss in bundle.get("issues", []):
                if isinstance(iss, dict) and iss.get("key"):
                    ikey = iss["key"].upper()
                    iss["key"] = ikey
                    self._atomic_write_json(self.issues_dir / f"{ikey}.json", iss)
                    n_issues += 1

            cfg = self._read_json(self.config_path)
            if max_spr_seq >= cfg.get("next_sprint_seq", 1):
                cfg["next_sprint_seq"] = max_spr_seq + 1
            if max_mil_seq >= cfg.get("next_milestone_seq", 1):
                cfg["next_milestone_seq"] = max_mil_seq + 1
            if max_view_seq >= cfg.get("next_view_seq", 1):
                cfg["next_view_seq"] = max_view_seq + 1
            cfg["updated_at"] = now_iso()
            self._atomic_write_json(self.config_path, cfg)

            self.reindex()
            return {
                "status": "imported",
                "imported_issues": n_issues,
                "imported_sprints": n_sprints,
                "imported_milestones": n_milestones,
            }


# ============================================================================
# Pydantic Request Schemas & FastAPI Factory (All 73 REST/Multipart Routes)
# ============================================================================

class FlexModel(BaseModel):
    model_config = ConfigDict(extra="allow")


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
        with storage.write_lock():
            storage._atomic_write_json(storage.daemon_path, daemon_meta)
        try:
            yield
        finally:
            try:
                if storage.daemon_path.exists():
                    cur_meta = storage._read_json(storage.daemon_path)
                    if cur_meta.get("pid") == os.getpid():
                        storage.daemon_path.unlink(missing_ok=True)
            except Exception:
                pass

    app = FastAPI(
        title="itrack API",
        version=VERSION,
        description="Local-First Issue Tracker, iQL Engine & Agent Coordination Daemon",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.storage = storage

    # --- Embedded Offline SPA & Favicon Routes ---
    favicon_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#2563eb"/>'
        '<text x="16" y="22" font-family="monospace" font-size="16" font-weight="bold" text-anchor="middle" fill="#ffffff">it</text>'
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

    # --- 1..10 System, Config & Workspace ---
    @app.get("/api/v1/health")
    def api_health() -> Dict[str, Any]:
        actual_port = int(state_holder.get("port", port))
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return {
            "status": "ok",
            "pid": os.getpid(),
            "host": connect_host,
            "port": actual_port,
            "base_url": state_holder.get("base_url", f"http://{connect_host}:{actual_port}"),
            "repo_root": str(repo_root),
            "issues_dir": str(storage.issues_root),
            "issue_count": storage.issue_count(),
            "uptime_seconds": round(time.monotonic() - started_mono, 3),
            "version": VERSION,
        }

    @app.post("/api/v1/shutdown")
    def api_shutdown(background_tasks: BackgroundTasks) -> Dict[str, Any]:
        def _trigger_stop() -> None:
            time.sleep(0.05)
            srv = state_holder.get("uvicorn_server")
            if srv is not None:
                srv.should_exit = True

        background_tasks.add_task(_trigger_stop)
        return {"status": "shutting_down", "pid": os.getpid()}

    @app.get("/api/v1/stats")
    def api_stats() -> Dict[str, Any]:
        return storage.get_stats()

    @app.post("/api/v1/reindex")
    def api_reindex() -> Dict[str, Any]:
        return storage.reindex()

    @app.get("/api/v1/config")
    def api_get_config() -> Dict[str, Any]:
        return storage.get_config()

    @app.patch("/api/v1/config")
    def api_patch_config(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.update_config(body)

    @app.get("/api/v1/workflows")
    def api_get_workflows() -> Dict[str, Any]:
        return storage.get_workflows()

    @app.put("/api/v1/workflows")
    def api_put_workflows(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.set_workflows(
            statuses=body.get("statuses", []),
            transitions=body.get("transitions", {}),
        )

    @app.get("/api/v1/export")
    def api_export() -> Dict[str, Any]:
        return storage.export_bundle()

    @app.post("/api/v1/import")
    def api_import(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        bundle = body.get("bundle") if "bundle" in body else body
        mode = body.get("mode", "merge") if "bundle" in body else "merge"
        return storage.import_bundle(bundle=bundle, mode=mode)

    # --- 11..13 Board, Epics & Dependency Graph ---
    @app.get("/api/v1/board")
    def api_board(
        sprint_id: Optional[str] = Query(default=None),
        epic_id: Optional[str] = Query(default=None),
        assignee: Optional[str] = Query(default=None),
        iql: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        return storage.get_board(sprint_id=sprint_id, epic_id=epic_id, assignee=assignee, iql=iql)

    @app.get("/api/v1/epics")
    def api_epics() -> Dict[str, Any]:
        return storage.get_epics()

    @app.get("/api/v1/graph/dependencies")
    def api_graph_dependencies() -> Dict[str, Any]:
        return storage.get_dependency_graph()

    # --- 14..18 Users / Agents ---
    @app.get("/api/v1/users")
    def api_list_users() -> Dict[str, Any]:
        return {"users": storage.list_users()}

    @app.post("/api/v1/users", status_code=201)
    def api_create_user(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.upsert_user(body)

    @app.get("/api/v1/users/{username}")
    def api_get_user(username: str) -> Dict[str, Any]:
        return storage.get_user_detail(username)

    @app.patch("/api/v1/users/{username}")
    def api_patch_user(username: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.update_user(username, body)

    @app.delete("/api/v1/users/{username}")
    def api_delete_user(username: str) -> Dict[str, Any]:
        return storage.delete_user(username)

    # --- 19..33 Issues Core, Lifecycle, Subtasks, Bulk & Agent Coordination ---
    @app.get("/api/v1/issues")
    def api_list_issues(
        iql: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        priority: Optional[str] = Query(default=None),
        type: Optional[str] = Query(default=None),
        assignee: Optional[str] = Query(default=None),
        reporter: Optional[str] = Query(default=None),
        label: Optional[str] = Query(default=None),
        component: Optional[str] = Query(default=None),
        epic_id: Optional[str] = Query(default=None),
        parent_id: Optional[str] = Query(default=None),
        sprint_id: Optional[str] = Query(default=None),
        milestone_id: Optional[str] = Query(default=None),
        sort: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=0, le=5000),
        offset: int = Query(default=0, ge=0),
    ) -> Dict[str, Any]:
        extra: List[Dict[str, Any]] = []
        if q:
            extra.append({"type": "cmp", "field": "text", "raw_field": "q", "op": "~", "value": q})
        for field_name, raw_val in (
            ("status", status),
            ("priority", priority),
            ("type", type),
            ("assignee", assignee),
            ("reporter", reporter),
            ("labels", label),
            ("components", component),
            ("epic_id", epic_id),
            ("parent_id", parent_id),
            ("sprint_id", sprint_id),
            ("milestone_id", milestone_id),
        ):
            if raw_val is not None and str(raw_val).strip() != "":
                if "," in str(raw_val) and field_name in ("status", "priority", "type", "labels", "components"):
                    vals = [v.strip() for v in str(raw_val).split(",") if v.strip()]
                    extra.append({"type": "in", "field": field_name, "raw_field": field_name, "negated": False, "values": vals})
                else:
                    extra.append({"type": "cmp", "field": field_name, "raw_field": field_name, "op": "=", "value": str(raw_val).strip()})

        issues, total, ast = storage.execute_iql(
            iql=iql,
            extra_filters=extra or None,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        eff_limit = ast["limit"] if ast["limit"] is not None else limit
        eff_offset = ast["offset"] if ast["offset"] is not None else offset
        return {"issues": issues, "total": total, "limit": eff_limit, "offset": eff_offset}

    @app.post("/api/v1/issues", status_code=201)
    def api_create_issue(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.create_issue(body)

    @app.get("/api/v1/issues/ready")
    def api_ready_issues(
        assignee: Optional[str] = Query(default=None),
        sprint_id: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> Dict[str, Any]:
        return storage.get_ready_issues(assignee=assignee, sprint_id=sprint_id, limit=limit)

    @app.post("/api/v1/issues/bulk")
    def api_bulk_issues(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        keys = body.get("keys") or []
        if not isinstance(keys, list) or not keys:
            raise HTTPException(status_code=400, detail="keys must be a non-empty list of issue keys.")
        action = body.get("action", "")
        fields = dict(body.get("fields") or {})
        for k in ("status", "priority", "assignee", "label", "labels", "sprint_id", "sprint", "resolution", "force"):
            if k in body and k not in fields:
                fields[k] = body[k]
        return storage.bulk_action(keys=keys, action=action, fields=fields, actor=body.get("actor"))

    @app.get("/api/v1/issues/{key}")
    def api_get_issue(key: str) -> Dict[str, Any]:
        return storage.get_issue(key)

    @app.patch("/api/v1/issues/{key}")
    def api_patch_issue(key: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.update_issue(key, body)

    @app.delete("/api/v1/issues/{key}")
    def api_delete_issue(key: str) -> Dict[str, Any]:
        return storage.delete_issue(key)

    @app.post("/api/v1/issues/{key}/transition")
    def api_transition_issue(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        if "status" not in body:
            raise HTTPException(status_code=400, detail="status is required.")
        return storage.transition_issue(
            key=key,
            target_status=body["status"],
            resolution=body.get("resolution"),
            comment=body.get("comment"),
            force=bool(body.get("force", False)),
            actor=body.get("actor"),
        )

    @app.post("/api/v1/issues/{key}/assign")
    def api_assign_issue(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.assign_issue(key=key, assignee=body.get("assignee"), actor=body.get("actor"))

    @app.post("/api/v1/issues/{key}/clone", status_code=201)
    def api_clone_issue(key: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.clone_issue(
            key=key,
            title_prefix=body.get("title_prefix", "[Clone] "),
            include_subtasks=bool(body.get("include_subtasks", False)),
            actor=body.get("actor"),
        )

    @app.post("/api/v1/issues/{key}/claim")
    def api_claim_issue(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.claim_issue(
            key=key,
            agent=body.get("agent", ""),
            note=body.get("note"),
            force=bool(body.get("force", False)),
        )

    @app.post("/api/v1/issues/{key}/release")
    def api_release_issue(key: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.release_issue(
            key=key,
            agent=body.get("agent"),
            status=body.get("status"),
            note=body.get("note"),
        )

    @app.get("/api/v1/issues/{key}/subtasks")
    def api_list_subtasks(key: str) -> Dict[str, Any]:
        return storage.list_subtasks(key)

    @app.post("/api/v1/issues/{key}/subtasks", status_code=201)
    def api_create_subtask(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.create_subtask(key, body)

    @app.get("/api/v1/issues/{key}/history")
    def api_issue_history(key: str) -> Dict[str, Any]:
        iss = storage.get_issue(key)
        return {"key": iss["key"], "history": iss.get("history", [])}

    # --- 34..35 Watchers ---
    @app.post("/api/v1/issues/{key}/watchers")
    def api_add_watcher(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.add_watcher(key, body.get("username", ""))

    @app.delete("/api/v1/issues/{key}/watchers/{username}")
    def api_remove_watcher(key: str, username: str) -> Dict[str, Any]:
        return storage.remove_watcher(key, username)

    # --- 36..42 Search, iQL & Saved Views ---
    @app.get("/api/v1/search")
    def api_search(
        q: Optional[str] = Query(default=None),
        iql: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> Dict[str, Any]:
        return storage.search_issues(q=q, iql=iql, limit=limit, offset=offset)

    @app.post("/api/v1/iql/query")
    def api_iql_query(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        iql_str = body.get("iql", "")
        limit = int(body.get("limit", 200))
        offset = int(body.get("offset", 0))
        current_user = body.get("current_user")
        issues, total, ast = storage.execute_iql(
            iql=iql_str,
            limit=limit,
            offset=offset,
            current_user=current_user,
        )
        return {"issues": issues, "total": total, "ast": ast}

    @app.post("/api/v1/iql/validate")
    def api_iql_validate(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        iql_str = body.get("iql", "")
        try:
            ast = parse_iql(iql_str)
            # Also dry-run SQL compilation to catch any semantic issues
            compiler = storage._make_compiler()
            compiler.compile_where(ast["where"])
            compiler.compile_order_by(ast["order_by"])
            return {"valid": True, "ast": ast, "error": None, "position": None}
        except IQLError as e:
            return {"valid": False, "ast": None, "error": e.message, "position": e.position}

    @app.get("/api/v1/views")
    def api_list_views() -> Dict[str, Any]:
        return {"views": storage.list_views()}

    @app.post("/api/v1/views", status_code=201)
    def api_create_view(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.create_view(body)

    @app.patch("/api/v1/views/{view_id}")
    def api_patch_view(view_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.update_view(view_id, body)

    @app.delete("/api/v1/views/{view_id}")
    def api_delete_view(view_id: str) -> Dict[str, Any]:
        return storage.delete_view(view_id)

    # --- 43..46 Comments ---
    @app.get("/api/v1/issues/{key}/comments")
    def api_list_comments(key: str) -> Dict[str, Any]:
        return storage.list_comments(key)

    @app.post("/api/v1/issues/{key}/comments", status_code=201)
    def api_add_comment(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.add_comment(key=key, body=body.get("body", ""), author=body.get("author"))

    @app.patch("/api/v1/issues/{key}/comments/{comment_id}")
    def api_edit_comment(key: str, comment_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.edit_comment(
            key=key,
            comment_id=comment_id,
            body=body.get("body", ""),
            actor=body.get("actor"),
        )

    @app.delete("/api/v1/issues/{key}/comments/{comment_id}")
    def api_delete_comment(key: str, comment_id: str) -> Dict[str, Any]:
        return storage.delete_comment(key=key, comment_id=comment_id)

    # --- 47..50 Multipart Attachments ---
    @app.get("/api/v1/issues/{key}/attachments")
    def api_list_attachments(key: str) -> Dict[str, Any]:
        return storage.list_attachments(key)

    @app.post("/api/v1/issues/{key}/attachments", status_code=201)
    def api_upload_attachment(
        key: str,
        file: UploadFile = File(...),
        uploaded_by: str = Form("system"),
        description: str = Form(""),
    ) -> Dict[str, Any]:
        return storage.save_attachment(
            key=key,
            filename=file.filename,
            content_type=file.content_type,
            stream=file.file,
            uploaded_by=uploaded_by,
            description=description,
        )

    @app.get("/api/v1/issues/{key}/attachments/{attachment_id}/download")
    def api_download_attachment(key: str, attachment_id: str) -> FileResponse:
        fpath, att = storage.get_attachment_download_info(key, attachment_id)
        return FileResponse(
            path=str(fpath),
            media_type=att.get("mime_type") or "application/octet-stream",
            filename=att.get("filename") or fpath.name,
        )

    @app.delete("/api/v1/issues/{key}/attachments/{attachment_id}")
    def api_delete_attachment(key: str, attachment_id: str) -> Dict[str, Any]:
        return storage.delete_attachment(key, attachment_id)

    # --- 51..53 Issue Links ---
    @app.get("/api/v1/issues/{key}/links")
    def api_list_links(key: str) -> Dict[str, Any]:
        return storage.list_links(key)

    @app.post("/api/v1/issues/{key}/links", status_code=201)
    def api_add_link(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.add_link(
            source_key=key,
            target_key=body.get("target_key", ""),
            link_type=body.get("link_type", ""),
            actor=body.get("actor"),
        )

    @app.delete("/api/v1/issues/{key}/links/{link_id}")
    def api_delete_link(key: str, link_id: str) -> Dict[str, Any]:
        return storage.delete_link(key, link_id)

    # --- 54..56 Worklogs ---
    @app.get("/api/v1/issues/{key}/worklogs")
    def api_list_worklogs(key: str) -> Dict[str, Any]:
        return storage.list_worklogs(key)

    @app.post("/api/v1/issues/{key}/worklogs", status_code=201)
    def api_add_worklog(key: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.add_worklog(
            key=key,
            minutes=body.get("minutes"),
            duration=body.get("duration"),
            author=body.get("author"),
            note=body.get("note"),
            started_at=body.get("started_at"),
        )

    @app.delete("/api/v1/issues/{key}/worklogs/{worklog_id}")
    def api_delete_worklog(key: str, worklog_id: str) -> Dict[str, Any]:
        return storage.delete_worklog(key, worklog_id)

    # --- 57..62 Labels & Components ---
    @app.get("/api/v1/labels")
    def api_list_labels() -> Dict[str, Any]:
        return {"labels": storage.list_labels()}

    @app.post("/api/v1/labels", status_code=201)
    def api_add_label(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.add_label(
            name=body.get("name", ""),
            color=body.get("color"),
            description=body.get("description"),
        )

    @app.delete("/api/v1/labels/{name}")
    def api_delete_label(name: str) -> Dict[str, Any]:
        return storage.delete_label(name)

    @app.get("/api/v1/components")
    def api_list_components() -> Dict[str, Any]:
        return {"components": storage.list_components()}

    @app.post("/api/v1/components", status_code=201)
    def api_add_component(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.add_component(
            name=body.get("name", ""),
            lead=body.get("lead"),
            description=body.get("description"),
        )

    @app.delete("/api/v1/components/{name}")
    def api_delete_component(name: str) -> Dict[str, Any]:
        return storage.delete_component(name)

    # --- 63..73 Sprints & Milestones ---
    @app.get("/api/v1/sprints")
    def api_list_sprints(state: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        return {"sprints": storage.list_sprints(state=state)}

    @app.post("/api/v1/sprints", status_code=201)
    def api_create_sprint(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.create_sprint(body)

    @app.get("/api/v1/sprints/{sprint_id}")
    def api_get_sprint(sprint_id: str) -> Dict[str, Any]:
        return storage.get_sprint_detail(sprint_id)

    @app.patch("/api/v1/sprints/{sprint_id}")
    def api_patch_sprint(sprint_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.update_sprint(sprint_id, body)

    @app.post("/api/v1/sprints/{sprint_id}/start")
    def api_start_sprint(sprint_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.start_sprint(
            sprint_id=sprint_id,
            start_date=body.get("start_date"),
            end_date=body.get("end_date"),
        )

    @app.post("/api/v1/sprints/{sprint_id}/complete")
    def api_complete_sprint(sprint_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.complete_sprint(sprint_id=sprint_id, rollover_to=body.get("rollover_to"))

    @app.delete("/api/v1/sprints/{sprint_id}")
    def api_delete_sprint(sprint_id: str) -> Dict[str, Any]:
        return storage.delete_sprint(sprint_id)

    @app.get("/api/v1/milestones")
    def api_list_milestones() -> Dict[str, Any]:
        return {"milestones": storage.list_milestones()}

    @app.post("/api/v1/milestones", status_code=201)
    def api_create_milestone(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return storage.create_milestone(body)

    @app.patch("/api/v1/milestones/{milestone_id}")
    def api_patch_milestone(milestone_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        return storage.update_milestone(milestone_id, body)

    @app.delete("/api/v1/milestones/{milestone_id}")
    def api_delete_milestone(milestone_id: str) -> Dict[str, Any]:
        return storage.delete_milestone(milestone_id)

    return app


# ============================================================================
# Embedded 100% Offline Jira/Linear Single-Page Application (Zero External URLs)
# ============================================================================

SPA_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>itrack — Local-First Issue &amp; Agent Workspace</title>
  <meta name="description" content="Local-first Git-backed issue tracker, iQL query engine, Kanban board, dependency graph, and AI agent coordination workspace." />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <style>
    :root, [data-theme="dark"] {
      --bg-canvas: hsl(220, 14%, 8%);
      --bg-sidebar: hsl(220, 13%, 10.5%);
      --bg-surface-1: hsl(220, 13%, 12.5%);
      --bg-surface-2: hsl(220, 12%, 15.5%);
      --bg-surface-3: hsl(220, 12%, 19%);
      --bg-overlay: hsla(220, 18%, 4%, 0.72);
      --border-subtle: hsl(220, 11%, 18%);
      --border-strong: hsl(220, 11%, 25%);
      --text-primary: hsl(215, 20%, 93%);
      --text-secondary: hsl(215, 12%, 65%);
      --text-muted: hsl(215, 10%, 46%);
      --accent: hsl(212, 90%, 56%);
      --accent-hover: hsl(212, 92%, 62%);
      --accent-soft: hsla(212, 90%, 56%, 0.14);
      --ok: hsl(152, 60%, 45%);
      --ok-soft: hsla(152, 60%, 45%, 0.14);
      --warn: hsl(38, 92%, 54%);
      --warn-soft: hsla(38, 92%, 54%, 0.15);
      --danger: hsl(356, 75%, 58%);
      --danger-soft: hsla(356, 75%, 58%, 0.15);
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.35);
      --shadow-lg: 0 16px 40px rgba(0, 0, 0, 0.55);
    }

    [data-theme="light"] {
      --bg-canvas: hsl(40, 20%, 97.5%);
      --bg-sidebar: hsl(38, 16%, 94%);
      --bg-surface-1: hsl(0, 0%, 100%);
      --bg-surface-2: hsl(220, 14%, 95.5%);
      --bg-surface-3: hsl(220, 14%, 91.5%);
      --bg-overlay: hsla(220, 20%, 12%, 0.42);
      --border-subtle: hsl(220, 13%, 88%);
      --border-strong: hsl(220, 13%, 78%);
      --text-primary: hsl(220, 22%, 12%);
      --text-secondary: hsl(220, 12%, 38%);
      --text-muted: hsl(220, 10%, 54%);
      --accent: hsl(212, 86%, 45%);
      --accent-hover: hsl(212, 86%, 39%);
      --accent-soft: hsla(212, 86%, 45%, 0.11);
      --ok: hsl(152, 64%, 34%);
      --ok-soft: hsla(152, 64%, 34%, 0.12);
      --warn: hsl(34, 90%, 42%);
      --warn-soft: hsla(34, 90%, 42%, 0.13);
      --danger: hsl(356, 72%, 48%);
      --danger-soft: hsla(356, 72%, 48%, 0.12);
      --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.06);
      --shadow-lg: 0 16px 38px rgba(15, 23, 42, 0.16);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      font-size: 13px;
      line-height: 1.45;
      background: var(--bg-canvas);
      color: var(--text-primary);
      -webkit-font-smoothing: antialiased;
      overflow: hidden;
    }
    code, pre, .mono {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }
    button, input, select, textarea {
      font: inherit;
      color: inherit;
    }

    /* App Shell */
    .app-shell {
      display: grid;
      grid-template-columns: 232px 1fr;
      grid-template-rows: 52px 1fr;
      height: 100vh;
      width: 100vw;
    }

    /* Sidebar */
    .sidebar {
      grid-row: 1 / -1;
      grid-column: 1;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border-subtle);
      display: flex;
      flex-direction: column;
      padding: 12px 10px;
      gap: 14px;
      overflow-y: auto;
    }
    .brand-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 2px 6px 6px;
      border-bottom: 1px solid var(--border-subtle);
    }
    .brand-left {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .brand-mark {
      width: 24px;
      height: 24px;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      font-size: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      letter-spacing: -0.03em;
      flex-shrink: 0;
    }
    .brand-title {
      font-size: 13.5px;
      font-weight: 650;
      letter-spacing: -0.02em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .project-key-pill {
      font-size: 10.5px;
      font-weight: 600;
      padding: 2px 6px;
      border-radius: 4px;
      background: var(--bg-surface-2);
      border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
    }

    .nav-group-label {
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      padding: 0 8px 4px;
    }
    .nav-list {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .nav-btn {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      padding: 6.5px 9px;
      border-radius: 6px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-secondary);
      cursor: pointer;
      text-align: left;
      transition: background 120ms ease, color 120ms ease;
    }
    .nav-btn:hover {
      background: var(--bg-surface-2);
      color: var(--text-primary);
    }
    .nav-btn.active {
      background: var(--bg-surface-2);
      border-color: var(--border-subtle);
      color: var(--text-primary);
      font-weight: 550;
    }
    .nav-btn-left {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .nav-count {
      font-size: 11px;
      color: var(--text-muted);
      padding: 0 5px;
      border-radius: 999px;
      background: var(--bg-surface-1);
    }
    .saved-view-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 4px;
      padding: 5px 8px;
      border-radius: 5px;
      cursor: pointer;
      color: var(--text-secondary);
      font-size: 12px;
    }
    .saved-view-item:hover {
      background: var(--bg-surface-2);
      color: var(--text-primary);
    }
    .sidebar-footer {
      margin-top: auto;
      padding-top: 10px;
      border-top: 1px solid var(--border-subtle);
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .kbd-hint {
      display: inline-block;
      padding: 1px 5px;
      font-size: 10px;
      border-radius: 4px;
      background: var(--bg-surface-2);
      border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
      font-family: monospace;
    }

    /* Top Header & iQL Bar */
    .topbar {
      grid-row: 1;
      grid-column: 2;
      background: var(--bg-surface-1);
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 16px;
      min-width: 0;
    }
    .iql-bar-wrap {
      flex: 1;
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--bg-canvas);
      border: 1px solid var(--border-subtle);
      border-radius: 7px;
      padding: 3px 8px;
      min-width: 200px;
      transition: border-color 120ms ease;
    }
    .iql-bar-wrap:focus-within {
      border-color: var(--accent);
    }
    .iql-prefix {
      font-size: 11px;
      font-weight: 650;
      color: var(--accent);
      padding: 1px 5px;
      background: var(--accent-soft);
      border-radius: 4px;
      user-select: none;
    }
    #global-iql-input {
      flex: 1;
      border: none;
      outline: none;
      background: transparent;
      font-size: 12.5px;
      padding: 3px 2px;
      min-width: 100px;
    }
    .status-badge {
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 4px;
      white-space: nowrap;
      font-weight: 500;
    }
    .status-badge.valid { color: var(--ok); background: var(--ok-soft); }
    .status-badge.invalid { color: var(--danger); background: var(--danger-soft); }
    .status-badge.idle { color: var(--text-muted); background: var(--bg-surface-2); }

    /* Buttons & Inputs */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 5.5px 11px;
      border-radius: 6px;
      border: 1px solid var(--border-subtle);
      background: var(--bg-surface-2);
      color: var(--text-primary);
      font-size: 12.5px;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .btn:hover { background: var(--bg-surface-3); border-color: var(--border-strong); }
    .btn-primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    .btn-primary:hover {
      background: var(--accent-hover);
      border-color: var(--accent-hover);
    }
    .btn-danger {
      color: var(--danger);
      border-color: var(--border-subtle);
      background: var(--danger-soft);
    }
    .btn-sm { padding: 3px 8px; font-size: 11.5px; border-radius: 5px; }
    .btn-ghost { background: transparent; border-color: transparent; color: var(--text-secondary); }
    .btn-ghost:hover { background: var(--bg-surface-2); color: var(--text-primary); }

    .input, .select, .textarea {
      width: 100%;
      padding: 6px 9px;
      border-radius: 6px;
      border: 1px solid var(--border-subtle);
      background: var(--bg-canvas);
      color: var(--text-primary);
      font-size: 12.5px;
      outline: none;
    }
    .input:focus, .select:focus, .textarea:focus { border-color: var(--accent); }
    .textarea { resize: vertical; min-height: 76px; line-height: 1.5; }

    /* Main Workspace Content */
    .workspace-main {
      grid-row: 2;
      grid-column: 2;
      overflow: auto;
      padding: 16px 18px 64px;
      position: relative;
    }
    .view-panel { display: none; }
    .view-panel.active { display: block; }

    .view-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .filter-pills {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }
    .pill {
      padding: 3.5px 9px;
      border-radius: 999px;
      font-size: 11.5px;
      border: 1px solid var(--border-subtle);
      background: var(--bg-surface-1);
      color: var(--text-secondary);
      cursor: pointer;
    }
    .pill.active, .pill:hover {
      background: var(--accent-soft);
      color: var(--accent);
      border-color: var(--accent);
    }

    /* Kanban Board */
    .board-grid {
      display: flex;
      gap: 12px;
      align-items: flex-start;
      overflow-x: auto;
      padding-bottom: 14px;
      min-height: calc(100vh - 135px);
    }
    .board-col {
      width: 278px;
      min-width: 278px;
      background: var(--bg-surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      max-height: calc(100vh - 135px);
    }
    .board-col.drag-over {
      border-color: var(--accent);
      background: var(--bg-surface-2);
    }
    .board-col-header {
      padding: 9px 11px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
    }
    .col-title-wrap {
      display: flex;
      align-items: center;
      gap: 7px;
      font-weight: 600;
      font-size: 12px;
      text-transform: capitalize;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
      flex-shrink: 0;
    }
    .dot-backlog { background: hsl(215, 10%, 50%); }
    .dot-todo { background: hsl(212, 85%, 58%); }
    .dot-in_progress { background: hsl(38, 92%, 54%); }
    .dot-in_review { background: hsl(190, 80%, 46%); }
    .dot-blocked { background: hsl(356, 75%, 58%); }
    .dot-done { background: hsl(152, 60%, 45%); }
    .dot-canceled { background: hsl(215, 8%, 38%); }

    .board-cards {
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 7px;
      overflow-y: auto;
      min-height: 80px;
    }
    .issue-card {
      background: var(--bg-surface-2);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 9px 10px;
      cursor: pointer;
      box-shadow: var(--shadow-sm);
      transition: transform 100ms ease, border-color 120ms ease;
    }
    .issue-card:hover {
      border-color: var(--border-strong);
      transform: translateY(-1px);
    }
    .card-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      margin-bottom: 5px;
      font-size: 11px;
      color: var(--text-muted);
    }
    .card-title {
      font-size: 12.5px;
      font-weight: 550;
      color: var(--text-primary);
      margin-bottom: 7px;
      line-height: 1.35;
      word-break: break-word;
    }
    .card-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 5px;
    }
    .tag-row { display: flex; flex-wrap: wrap; gap: 4px; }
    .tag {
      font-size: 10.5px;
      padding: 1px 6px;
      border-radius: 4px;
      background: var(--bg-surface-1);
      border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
    }
    .tag-blocked {
      color: var(--danger);
      background: var(--danger-soft);
      border-color: transparent;
      font-weight: 600;
    }
    .tag-claimed {
      color: var(--warn);
      background: var(--warn-soft);
      border-color: transparent;
      font-weight: 600;
    }
    .prio-badge {
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
      padding: 1px 5px;
      border-radius: 3px;
    }
    .prio-urgent { color: var(--danger); background: var(--danger-soft); }
    .prio-high { color: var(--warn); background: var(--warn-soft); }
    .prio-medium { color: var(--accent); background: var(--accent-soft); }
    .prio-low, .prio-none { color: var(--text-muted); background: var(--bg-surface-1); }

    /* Linear High-Density List */
    .list-table-wrap {
      background: var(--bg-surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      overflow: hidden;
    }
    table.issue-table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
    }
    .issue-table th {
      padding: 8px 10px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border-subtle);
      background: var(--bg-surface-2);
    }
    .issue-table td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--border-subtle);
      vertical-align: middle;
    }
    .issue-table tr:last-child td { border-bottom: none; }
    .issue-table tbody tr:hover { background: var(--bg-surface-2); cursor: pointer; }

    /* Bulk Triage Floating Bar */
    #bulk-action-bar {
      position: fixed;
      bottom: 18px;
      left: calc(50% + 116px);
      transform: translateX(-50%);
      max-width: calc(100vw - 260px);
      background: var(--bg-surface-2);
      border: 1px solid var(--border-strong);
      box-shadow: var(--shadow-lg);
      border-radius: 9px;
      padding: 8px 14px;
      display: none;
      align-items: center;
      gap: 8px;
      z-index: 40;
      overflow-x: auto;
    }
    #bulk-action-bar.visible { display: flex; }
    #bulk-selected-count { white-space: nowrap; flex-shrink: 0; }

    /* Cards & Panels for Sprints, Epics, Ready, Stats */
    .panel-card {
      background: var(--bg-surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 14px 16px;
      margin-bottom: 12px;
    }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
    .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 14px; }
    .kpi-box {
      background: var(--bg-surface-1);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 12px 14px;
    }
    .kpi-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .kpi-val { font-size: 22px; font-weight: 700; margin-top: 3px; letter-spacing: -0.02em; }

    .progress-track {
      height: 7px;
      width: 100%;
      background: var(--bg-surface-3);
      border-radius: 999px;
      overflow: hidden;
      margin: 6px 0;
    }
    .progress-fill {
      height: 100%;
      background: var(--ok);
      border-radius: 999px;
      transition: width 200ms ease;
    }
    .bar-row {
      display: grid;
      grid-template-columns: 110px 1fr 40px;
      align-items: center;
      gap: 8px;
      margin: 6px 0;
      font-size: 12px;
    }

    /* Modals & Slide-over Issue Drawer */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: var(--bg-overlay);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 60;
      padding: 20px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      width: 100%;
      max-width: 580px;
      background: var(--bg-surface-1);
      border: 1px solid var(--border-strong);
      border-radius: 10px;
      box-shadow: var(--shadow-lg);
      padding: 18px 20px;
      max-height: 90vh;
      overflow-y: auto;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 10px;
    }
    .form-field { display: flex; flex-direction: column; gap: 4px; }
    .form-field.full { grid-column: 1 / -1; }
    .form-field label { font-size: 11px; font-weight: 600; color: var(--text-secondary); }

    /* Issue Drawer */
    .drawer-backdrop {
      position: fixed;
      inset: 0;
      background: var(--bg-overlay);
      display: none;
      justify-content: flex-end;
      z-index: 55;
    }
    .drawer-backdrop.open { display: flex; }
    #issue-drawer {
      width: min(780px, 94vw);
      height: 100vh;
      background: var(--bg-surface-1);
      border-left: 1px solid var(--border-strong);
      box-shadow: var(--shadow-lg);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .drawer-header {
      padding: 12px 18px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      background: var(--bg-surface-2);
    }
    .drawer-body {
      flex: 1;
      overflow-y: auto;
      padding: 16px 20px 36px;
      display: grid;
      grid-template-columns: 1fr 235px;
      gap: 18px;
    }
    @media (max-width: 820px) {
      .app-shell { grid-template-columns: 190px 1fr; }
      .drawer-body { grid-template-columns: 1fr; }
      #bulk-action-bar { left: calc(50% + 95px); max-width: calc(100vw - 210px); }
    }
    .drawer-section {
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--border-subtle);
    }
    .drawer-section h3 {
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }
    .meta-prop {
      display: flex;
      flex-direction: column;
      gap: 3px;
      margin-bottom: 10px;
    }
    .meta-prop label {
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 600;
    }
    .comment-item {
      background: var(--bg-surface-2);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 8px 10px;
      margin-bottom: 8px;
    }
    .toast-stack {
      position: fixed;
      bottom: 72px;
      right: 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      z-index: 90;
    }
    .toast {
      padding: 8px 13px;
      border-radius: 6px;
      background: var(--bg-surface-2);
      border: 1px solid var(--border-strong);
      box-shadow: var(--shadow-lg);
      font-size: 12px;
    }
    .toast.err { border-color: var(--danger); color: var(--danger); }
  </style>
</head>
<body>
  <div class="app-shell">
    <!-- Navigation Sidebar -->
    <aside class="sidebar" aria-label="Workspace Navigation">
      <div class="brand-row">
        <div class="brand-left">
          <span class="brand-mark">it</span>
          <h1 class="brand-title" id="workspace-project-name">itrack</h1>
        </div>
        <span class="project-key-pill mono" id="workspace-project-key">ITK</span>
      </div>

      <div>
        <div class="nav-group-label">Views</div>
        <nav class="nav-list">
          <button class="nav-btn active" id="nav-board" data-view="board">
            <span class="nav-btn-left">&#9638; Kanban Board</span>
            <span class="nav-count" id="cnt-board">0</span>
          </button>
          <button class="nav-btn" id="nav-list" data-view="list">
            <span class="nav-btn-left">&#9776; Issue List</span>
            <span class="nav-count" id="cnt-list">0</span>
          </button>
          <button class="nav-btn" id="nav-ready" data-view="ready">
            <span class="nav-btn-left">&#9889; Ready Queue</span>
            <span class="nav-count" id="cnt-ready">0</span>
          </button>
          <button class="nav-btn" id="nav-sprints" data-view="sprints">
            <span class="nav-btn-left">&#10227; Sprints</span>
            <span class="nav-count" id="cnt-sprints">0</span>
          </button>
          <button class="nav-btn" id="nav-epics" data-view="epics">
            <span class="nav-btn-left">&#9670; Epics &amp; Releases</span>
            <span class="nav-count" id="cnt-epics">0</span>
          </button>
          <button class="nav-btn" id="nav-graph" data-view="graph">
            <span class="nav-btn-left">&#8644; Dependency DAG</span>
            <span class="nav-count" id="cnt-blocked">0</span>
          </button>
          <button class="nav-btn" id="nav-stats" data-view="stats">
            <span class="nav-btn-left">&#9636; Analytics &amp; Config</span>
          </button>
        </nav>
      </div>

      <div>
        <div class="nav-group-label">Saved iQL Views</div>
        <div id="saved-views-list" class="nav-list"></div>
      </div>

      <div class="sidebar-footer">
        <div>Shortcuts: <span class="kbd-hint">c</span> Create <span class="kbd-hint">/</span> iQL <span class="kbd-hint">b</span> Board <span class="kbd-hint">l</span> List</div>
        <div class="mono" id="daemon-status-line">Local Git Storage</div>
      </div>
    </aside>

    <!-- Top Header Bar -->
    <header class="topbar">
      <div class="iql-bar-wrap">
        <span class="iql-prefix mono">iQL</span>
        <input id="global-iql-input" type="text" placeholder="Filter with iQL, e.g. priority >= high AND blocked = false ORDER BY priority DESC" autocomplete="off" />
        <span id="iql-status-badge" class="status-badge idle">All Issues</span>
      </div>
      <button class="btn btn-sm" id="btn-run-iql" title="Execute iQL query">Run</button>
      <button class="btn btn-sm" id="btn-save-view" title="Save current iQL as a named view">Save View</button>
      <button class="btn btn-sm" id="btn-cmd-palette" title="Command Palette (Ctrl+K)">&#8984;K</button>
      <button class="btn btn-sm" id="btn-theme-toggle" title="Toggle Dark / Light Theme">Theme</button>
      <button class="btn btn-primary" id="btn-new-issue">+ New Issue</button>
    </header>

    <!-- Main Workspace Views -->
    <main class="workspace-main">
      <!-- 1. Kanban Board -->
      <section id="view-board" class="view-panel active">
        <div class="view-toolbar">
          <div class="filter-pills" id="board-filter-pills"></div>
          <div style="font-size:12px;color:var(--text-muted);" id="board-summary-label">Drag cards across columns to transition</div>
        </div>
        <div class="board-grid" id="board-columns"></div>
      </section>

      <!-- 2. Linear High-Density List + Bulk Triage -->
      <section id="view-list" class="view-panel">
        <div class="view-toolbar">
          <div style="font-weight:600;" id="list-summary-count">Issues</div>
          <div style="display:flex;gap:6px;align-items:center;">
            <input type="text" class="input" id="list-fts-input" placeholder="Full-text FTS5 search..." style="width:220px;" />
            <button class="btn btn-sm" id="btn-list-fts">Search</button>
          </div>
        </div>
        <div class="list-table-wrap">
          <table class="issue-table">
            <thead>
              <tr>
                <th style="width:32px;"><input type="checkbox" id="list-select-all" /></th>
                <th style="width:88px;">Key</th>
                <th style="width:76px;">Priority</th>
                <th>Title</th>
                <th style="width:118px;">Status</th>
                <th style="width:84px;">Type</th>
                <th style="width:110px;">Assignee</th>
                <th style="width:66px;">Pts</th>
              </tr>
            </thead>
            <tbody id="list-tbody"></tbody>
          </table>
        </div>
        <div id="bulk-action-bar">
          <span class="mono" id="bulk-selected-count" style="font-weight:650;">0 selected</span>
          <select class="select" id="bulk-status-select" style="width:130px;"></select>
          <button class="btn btn-sm" id="btn-bulk-transition">Move</button>
          <select class="select" id="bulk-priority-select" style="width:110px;">
            <option value="">Priority...</option>
            <option value="urgent">Urgent</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
            <option value="none">None</option>
          </select>
          <button class="btn btn-sm" id="btn-bulk-priority">Set Prio</button>
          <input class="input" id="bulk-assignee-input" placeholder="Assignee..." style="width:105px;" />
          <button class="btn btn-sm" id="btn-bulk-assign">Assign</button>
          <input class="input" id="bulk-label-input" placeholder="+Label..." style="width:95px;" />
          <button class="btn btn-sm" id="btn-bulk-label">Tag</button>
          <select class="select" id="bulk-sprint-select" style="width:120px;"></select>
          <button class="btn btn-sm" id="btn-bulk-sprint">Sprint</button>
          <button class="btn btn-sm btn-danger" id="btn-bulk-delete">Delete</button>
        </div>
      </section>

      <!-- 3. Ready Queue (Agent & Operator Actionable Work) -->
      <section id="view-ready" class="view-panel">
        <div class="view-toolbar">
          <div>
            <h2 style="font-size:15px;font-weight:650;">Ready Queue — Unblocked Actionable Work</h2>
            <p style="font-size:12px;color:var(--text-secondary);">Issues in backlog/todo with zero unresolved blockers and no active claim.</p>
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            <label style="font-size:11.5px;color:var(--text-secondary);">Claim as:</label>
            <input class="input mono" id="ready-agent-input" value="agent-1" style="width:140px;" />
          </div>
        </div>
        <div class="grid-2">
          <div>
            <h3 style="font-size:12px;text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;">Ready to Claim</h3>
            <div id="ready-queue-cards"></div>
          </div>
          <div>
            <h3 style="font-size:12px;text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;">Currently Claimed / In Progress</h3>
            <div id="claimed-active-cards"></div>
          </div>
        </div>
      </section>

      <!-- 4. Sprints & Backlog Planner -->
      <section id="view-sprints" class="view-panel">
        <div class="panel-card">
          <div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;">
            <div class="form-field" style="flex:1;min-width:180px;">
              <label>New Sprint Name</label>
              <input class="input" id="sprint-name-input" placeholder="Sprint 1 — Core Engine" />
            </div>
            <div class="form-field" style="flex:2;min-width:200px;">
              <label>Sprint Goal</label>
              <input class="input" id="sprint-goal-input" placeholder="Ship iQL + Kanban + Agent Claims" />
            </div>
            <div class="form-field">
              <label>Start Date</label>
              <input class="input" type="date" id="sprint-start-input" />
            </div>
            <div class="form-field">
              <label>End Date</label>
              <input class="input" type="date" id="sprint-end-input" />
            </div>
            <button class="btn btn-primary" id="btn-create-sprint">+ Create Sprint</button>
          </div>
        </div>
        <div id="sprints-container"></div>
        <div class="panel-card">
          <h3 style="font-size:13px;font-weight:650;margin-bottom:8px;">Unscheduled Backlog (Assign to Sprint)</h3>
          <div id="backlog-planner-list"></div>
        </div>
      </section>

      <!-- 5. Epics & Milestones -->
      <section id="view-epics" class="view-panel">
        <div class="grid-2">
          <div>
            <div class="view-toolbar">
              <h2 style="font-size:15px;font-weight:650;">Epics Rollup</h2>
              <button class="btn btn-sm btn-primary" id="btn-quick-epic">+ New Epic</button>
            </div>
            <div id="epics-list-container"></div>
          </div>
          <div>
            <div class="view-toolbar">
              <h2 style="font-size:15px;font-weight:650;">Milestones &amp; Releases</h2>
            </div>
            <div class="panel-card">
              <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
                <div class="form-field" style="flex:1;">
                  <label>Milestone Name</label>
                  <input class="input" id="ms-name-input" placeholder="v1.0.0 GA" />
                </div>
                <div class="form-field">
                  <label>Target Date</label>
                  <input class="input" type="date" id="ms-date-input" />
                </div>
                <button class="btn btn-primary" id="btn-create-milestone">Add</button>
              </div>
            </div>
            <div id="milestones-list-container"></div>
          </div>
        </div>
      </section>

      <!-- 6. Dependency DAG Graph -->
      <section id="view-graph" class="view-panel">
        <div class="panel-card" style="display:flex;align-items:flex-end;gap:8px;flex-wrap:wrap;">
          <div class="form-field">
            <label>Source Issue</label>
            <input class="input mono" id="graph-src-input" placeholder="ITK-1" style="width:110px;" />
          </div>
          <div class="form-field">
            <label>Relationship</label>
            <select class="select" id="graph-type-select" style="width:140px;">
              <option value="blocks">blocks</option>
              <option value="blocked_by">blocked_by</option>
              <option value="relates_to">relates_to</option>
              <option value="duplicates">duplicates</option>
            </select>
          </div>
          <div class="form-field">
            <label>Target Issue</label>
            <input class="input mono" id="graph-tgt-input" placeholder="ITK-2" style="width:110px;" />
          </div>
          <button class="btn btn-primary" id="btn-graph-add-link">Add Dependency Link</button>
          <span style="font-size:11.5px;color:var(--text-muted);margin-left:auto;">DAG Cycle Prevention Active (HTTP 409 on circular blocks)</span>
        </div>
        <div class="panel-card" style="overflow-x:auto;">
          <h3 style="font-size:12px;text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;">Directed Blocker &amp; Link Topology (Click any node to inspect)</h3>
          <div id="graph-svg-container" style="min-height:240px;"></div>
        </div>
        <div class="grid-2">
          <div class="panel-card">
            <h3 style="font-size:12px;color:var(--danger);text-transform:uppercase;margin-bottom:8px;">Blocked Issues</h3>
            <div id="graph-blocked-list"></div>
          </div>
          <div class="panel-card">
            <h3 style="font-size:12px;color:var(--ok);text-transform:uppercase;margin-bottom:8px;">Ready (Unblocked) Issues</h3>
            <div id="graph-ready-list"></div>
          </div>
        </div>
      </section>

      <!-- 7. Analytics & Workspace Settings -->
      <section id="view-stats" class="view-panel">
        <div class="grid-4" id="stats-kpi-grid"></div>
        <div class="grid-2">
          <div class="panel-card">
            <h3 style="font-size:13px;font-weight:650;margin-bottom:8px;">Breakdown by Status &amp; Priority</h3>
            <div id="stats-bars-left"></div>
          </div>
          <div class="panel-card">
            <h3 style="font-size:13px;font-weight:650;margin-bottom:8px;">Breakdown by Type &amp; Assignee</h3>
            <div id="stats-bars-right"></div>
          </div>
        </div>
        <div class="grid-2">
          <div class="panel-card">
            <h3 style="font-size:13px;font-weight:650;margin-bottom:8px;">Labels &amp; Components</h3>
            <div style="display:flex;gap:6px;margin-bottom:8px;">
              <input class="input" id="new-label-name" placeholder="Label name" />
              <input class="input" type="color" id="new-label-color" value="#3b82f6" style="width:42px;padding:2px;" />
              <button class="btn btn-sm btn-primary" id="btn-add-label">Add Label</button>
            </div>
            <div id="settings-labels-list" class="tag-row" style="margin-bottom:12px;"></div>
            <div style="display:flex;gap:6px;margin-bottom:8px;">
              <input class="input" id="new-comp-name" placeholder="Component name" />
              <input class="input" id="new-comp-lead" placeholder="Lead" style="width:110px;" />
              <button class="btn btn-sm btn-primary" id="btn-add-comp">Add Component</button>
            </div>
            <div id="settings-comps-list" class="tag-row"></div>
          </div>

          <div class="panel-card">
            <h3 style="font-size:13px;font-weight:650;margin-bottom:8px;">Users, Agents &amp; Project Index</h3>
            <div style="display:flex;gap:6px;margin-bottom:8px;align-items:center;">
              <input class="input" id="new-user-name" placeholder="username / agent-id" />
              <label style="display:flex;align-items:center;gap:4px;font-size:11.5px;white-space:nowrap;">
                <input type="checkbox" id="new-user-agent" /> AI Agent
              </label>
              <button class="btn btn-sm btn-primary" id="btn-add-user">Register</button>
            </div>
            <div id="settings-users-list" class="tag-row" style="margin-bottom:14px;"></div>

            <div style="display:flex;gap:6px;margin-bottom:10px;align-items:flex-end;">
              <div class="form-field" style="width:95px;">
                <label>Project Key</label>
                <input class="input mono" id="cfg-project-key" />
              </div>
              <div class="form-field" style="flex:1;">
                <label>Project Name</label>
                <input class="input" id="cfg-project-name" />
              </div>
              <button class="btn btn-sm" id="btn-save-config">Save Config</button>
            </div>

            <div style="display:flex;gap:8px;padding-top:8px;border-top:1px solid var(--border-subtle);">
              <button class="btn btn-sm" id="btn-reindex">&#8635; Rebuild SQLite/FTS5 Index</button>
              <button class="btn btn-sm" id="btn-export-json">&#8681; Export Bundle JSON</button>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>

  <!-- Create Issue Modal -->
  <div class="modal-backdrop" id="create-issue-modal">
    <div class="modal-box">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <h2 style="font-size:15px;font-weight:650;">Create Issue</h2>
        <button class="btn btn-sm btn-ghost" id="btn-close-create-modal">&#10005;</button>
      </div>
      <form id="form-create-issue">
        <div class="form-grid">
          <div class="form-field full">
            <label for="ci-title">Title *</label>
            <input class="input" id="ci-title" required placeholder="Concise actionable summary..." />
          </div>
          <div class="form-field full">
            <label for="ci-desc">Description (Markdown)</label>
            <textarea class="textarea" id="ci-desc" placeholder="Context, acceptance criteria, notes..."></textarea>
          </div>
          <div class="form-field">
            <label for="ci-type">Type</label>
            <select class="select" id="ci-type"></select>
          </div>
          <div class="form-field">
            <label for="ci-priority">Priority</label>
            <select class="select" id="ci-priority">
              <option value="urgent">urgent</option>
              <option value="high">high</option>
              <option value="medium" selected>medium</option>
              <option value="low">low</option>
              <option value="none">none</option>
            </select>
          </div>
          <div class="form-field">
            <label for="ci-status">Status</label>
            <select class="select" id="ci-status"></select>
          </div>
          <div class="form-field">
            <label for="ci-assignee">Assignee</label>
            <input class="input" id="ci-assignee" placeholder="alice or agent-1" />
          </div>
          <div class="form-field">
            <label for="ci-labels">Labels (comma-separated)</label>
            <input class="input" id="ci-labels" placeholder="backend, security" />
          </div>
          <div class="form-field">
            <label for="ci-points">Story Points</label>
            <input class="input" type="number" step="0.5" id="ci-points" placeholder="3" />
          </div>
          <div class="form-field">
            <label for="ci-epic">Epic</label>
            <select class="select" id="ci-epic"><option value="">None</option></select>
          </div>
          <div class="form-field">
            <label for="ci-sprint">Sprint</label>
            <select class="select" id="ci-sprint"><option value="">Backlog</option></select>
          </div>
          <div class="form-field">
            <label for="ci-due">Due Date</label>
            <input class="input" type="date" id="ci-due" />
          </div>
          <div class="form-field">
            <label for="ci-components">Components (CSV)</label>
            <input class="input" id="ci-components" placeholder="api, ui" />
          </div>
        </div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
          <button type="button" class="btn" id="btn-cancel-create">Cancel</button>
          <button type="submit" class="btn btn-primary" id="btn-submit-create">Create Issue</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Command Palette Modal -->
  <div class="modal-backdrop" id="cmd-palette-modal">
    <div class="modal-box" style="max-width:480px;">
      <input class="input" id="cmd-palette-input" placeholder="Jump to issue key, view, or command..." />
      <div id="cmd-palette-results" style="margin-top:10px;display:flex;flex-direction:column;gap:4px;max-height:320px;overflow-y:auto;"></div>
    </div>
  </div>

  <!-- Slide-Over Issue Detail Drawer -->
  <div class="drawer-backdrop" id="issue-drawer-backdrop">
    <aside id="issue-drawer" aria-label="Issue Detail Drawer">
      <div class="drawer-header">
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="mono" style="font-weight:700;font-size:14px;" id="dr-key">ITK-1</span>
          <span class="tag tag-blocked" id="dr-blocked-badge" style="display:none;">BLOCKED</span>
          <span class="tag tag-claimed" id="dr-claimed-badge" style="display:none;"></span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <button class="btn btn-sm" id="dr-btn-claim">Claim</button>
          <button class="btn btn-sm" id="dr-btn-release" style="display:none;">Release</button>
          <button class="btn btn-sm" id="dr-btn-clone">Clone</button>
          <button class="btn btn-sm btn-danger" id="dr-btn-delete">Delete</button>
          <button class="btn btn-sm btn-ghost" id="dr-btn-close">&#10005;</button>
        </div>
      </div>

      <div class="drawer-body">
        <!-- Left Column: Title, Description, Subtasks, Links, Attachments, Comments, Worklogs, History -->
        <div>
          <input class="input" id="dr-title" style="font-size:16px;font-weight:650;margin-bottom:8px;" />
          <textarea class="textarea" id="dr-desc" placeholder="Add Markdown description..." style="min-height:92px;"></textarea>
          <div style="display:flex;justify-content:flex-end;margin-top:6px;">
            <button class="btn btn-sm btn-primary" id="dr-btn-save-text">Save Title &amp; Description</button>
          </div>

          <!-- Subtasks -->
          <div class="drawer-section">
            <h3>Subtasks (<span id="dr-subtasks-count">0/0</span>)</h3>
            <div id="dr-subtasks-list" style="margin-bottom:8px;"></div>
            <div style="display:flex;gap:6px;">
              <input class="input" id="dr-subtask-title" placeholder="Add subtask..." />
              <button class="btn btn-sm" id="dr-btn-add-subtask">Add</button>
            </div>
          </div>

          <!-- Links & Blockers -->
          <div class="drawer-section">
            <h3>Links &amp; Dependencies</h3>
            <div id="dr-links-list" style="margin-bottom:8px;"></div>
            <div style="display:flex;gap:6px;">
              <select class="select" id="dr-link-type" style="width:130px;">
                <option value="blocks">blocks</option>
                <option value="blocked_by">blocked_by</option>
                <option value="relates_to">relates_to</option>
                <option value="duplicates">duplicates</option>
              </select>
              <input class="input mono" id="dr-link-target" placeholder="Target key (e.g. ITK-2)" />
              <button class="btn btn-sm" id="dr-btn-add-link">Link</button>
            </div>
          </div>

          <!-- Multipart File Attachments -->
          <div class="drawer-section">
            <h3>Attachments</h3>
            <div id="dr-attachments-list" style="margin-bottom:8px;"></div>
            <div style="display:flex;gap:6px;align-items:center;">
              <input type="file" class="input" id="attachment-file-input" style="padding:3px;" />
              <button class="btn btn-sm" id="dr-btn-upload-att">Upload</button>
            </div>
          </div>

          <!-- Comments -->
          <div class="drawer-section">
            <h3>Comments</h3>
            <div id="dr-comments-list"></div>
            <textarea class="textarea" id="comment-body-input" placeholder="Write a comment..." style="min-height:60px;margin-top:6px;"></textarea>
            <div style="display:flex;justify-content:flex-end;margin-top:6px;">
              <button class="btn btn-sm btn-primary" id="btn-add-comment">Post Comment</button>
            </div>
          </div>

          <!-- Worklogs -->
          <div class="drawer-section">
            <h3>Worklogs (<span id="dr-worklog-total">0m</span>)</h3>
            <div id="dr-worklogs-list" style="margin-bottom:6px;"></div>
            <div style="display:flex;gap:6px;">
              <input class="input mono" id="worklog-duration-input" placeholder="e.g. 1h 30m" style="width:120px;" />
              <input class="input" id="worklog-note-input" placeholder="Work note..." />
              <button class="btn btn-sm" id="dr-btn-add-worklog">Log Time</button>
            </div>
          </div>

          <!-- Audit History -->
          <div class="drawer-section">
            <h3>Audit History</h3>
            <div id="dr-history-list" style="font-size:11.5px;color:var(--text-secondary);"></div>
          </div>
        </div>

        <!-- Right Metadata Sidebar inside Drawer -->
        <div>
          <div class="meta-prop">
            <label>Status</label>
            <select class="select" id="dr-status"></select>
          </div>
          <div class="meta-prop">
            <label>Priority</label>
            <select class="select" id="dr-priority">
              <option value="urgent">urgent</option>
              <option value="high">high</option>
              <option value="medium">medium</option>
              <option value="low">low</option>
              <option value="none">none</option>
            </select>
          </div>
          <div class="meta-prop">
            <label>Type</label>
            <select class="select" id="dr-type"></select>
          </div>
          <div class="meta-prop">
            <label>Assignee</label>
            <input class="input" id="dr-assignee" placeholder="Unassigned" />
          </div>
          <div class="meta-prop">
            <label>Story Points</label>
            <input class="input" type="number" step="0.5" id="dr-points" />
          </div>
          <div class="meta-prop">
            <label>Sprint</label>
            <select class="select" id="dr-sprint"><option value="">Backlog</option></select>
          </div>
          <div class="meta-prop">
            <label>Epic</label>
            <select class="select" id="dr-epic"><option value="">None</option></select>
          </div>
          <div class="meta-prop">
            <label>Milestone</label>
            <select class="select" id="dr-milestone"><option value="">None</option></select>
          </div>
          <div class="meta-prop">
            <label>Due Date</label>
            <input class="input" type="date" id="dr-due" />
          </div>
          <div class="meta-prop">
            <label>Labels (CSV)</label>
            <input class="input" id="dr-labels" placeholder="tag1, tag2" />
          </div>
          <div class="meta-prop">
            <label>Components (CSV)</label>
            <input class="input" id="dr-components" placeholder="backend, cli" />
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:12px;" id="dr-timestamps"></div>
        </div>
      </div>
    </aside>
  </div>

  <div class="toast-stack" id="toast-stack"></div>

  <script>
    const state = {
      view: 'board',
      iql: '',
      boardSprint: '',
      boardAssignee: '',
      config: null,
      issues: [],
      board: null,
      sprints: [],
      epics: [],
      milestones: [],
      views: [],
      labels: [],
      components: [],
      users: [],
      selectedKeys: new Set(),
      activeIssue: null,
    };

    const $ = (id) => document.getElementById(id);

    function esc(s) {
      return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
    }

    function toast(msg, isErr = false) {
      const el = document.createElement('div');
      el.className = 'toast' + (isErr ? ' err' : '');
      el.textContent = msg;
      $('toast-stack').appendChild(el);
      setTimeout(() => el.remove(), 3400);
    }

    async function api(path, opts = {}) {
      const headers = Object.assign({}, opts.headers || {});
      if (opts.body && !(opts.body instanceof FormData) && typeof opts.body === 'object') {
        headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(opts.body);
      }
      const resp = await fetch(path, Object.assign({}, opts, { headers }));
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail || `HTTP ${resp.status}`);
      }
      return data;
    }

    // Theme Management
    function initTheme() {
      const saved = localStorage.getItem('itrack_theme') || 'dark';
      document.documentElement.setAttribute('data-theme', saved);
    }
    $('btn-theme-toggle').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      const nxt = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', nxt);
      localStorage.setItem('itrack_theme', nxt);
    });

    // Navigation
    function switchView(v) {
      state.view = v;
      document.querySelectorAll('.nav-btn[data-view]').forEach((b) => {
        b.classList.toggle('active', b.dataset.view === v);
      });
      document.querySelectorAll('.view-panel').forEach((p) => {
        p.classList.toggle('active', p.id === `view-${v}`);
      });
      refreshCurrentView();
    }
    document.querySelectorAll('.nav-btn[data-view]').forEach((btn) => {
      btn.addEventListener('click', () => switchView(btn.dataset.view));
    });

    // Live iQL Validation & Execution
    let iqlTimer = null;
    $('global-iql-input').addEventListener('input', (e) => {
      const val = e.target.value.trim();
      clearTimeout(iqlTimer);
      if (!val) {
        $('iql-status-badge').className = 'status-badge idle';
        $('iql-status-badge').textContent = 'All Issues';
        return;
      }
      iqlTimer = setTimeout(async () => {
        try {
          const res = await api('/api/v1/iql/validate', { method: 'POST', body: { iql: val } });
          if (res.valid) {
            $('iql-status-badge').className = 'status-badge valid';
            $('iql-status-badge').textContent = 'Valid iQL';
          } else {
            $('iql-status-badge').className = 'status-badge invalid';
            $('iql-status-badge').textContent = 'Syntax Err';
            $('iql-status-badge').title = res.error || '';
          }
        } catch (_) {}
      }, 180);
    });

    $('global-iql-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        runGlobalIQL();
      }
    });
    $('btn-run-iql').addEventListener('click', runGlobalIQL);

    async function runGlobalIQL() {
      state.iql = $('global-iql-input').value.trim();
      if (state.view !== 'board' && state.view !== 'list') {
        switchView('list');
      } else {
        await refreshCurrentView();
      }
    }

    $('btn-save-view').addEventListener('click', async () => {
      const iql = $('global-iql-input').value.trim();
      const name = prompt('Saved View Name:', iql ? `Filter: ${iql.slice(0, 24)}` : 'All Open');
      if (!name) return;
      try {
        await api('/api/v1/views', {
          method: 'POST',
          body: { name, iql, layout: state.view === 'board' ? 'board' : 'list' }
        });
        toast(`Saved view "${name}"`);
        await loadWorkspaceMeta();
      } catch (err) {
        toast(err.message, true);
      }
    });

    async function loadWorkspaceMeta() {
      const [cfg, health, sprintsRes, epicsRes, msRes, viewsRes, labelsRes, compsRes, usersRes, readyRes, statsRes] = await Promise.all([
        api('/api/v1/config'),
        api('/api/v1/health'),
        api('/api/v1/sprints'),
        api('/api/v1/epics'),
        api('/api/v1/milestones'),
        api('/api/v1/views'),
        api('/api/v1/labels'),
        api('/api/v1/components'),
        api('/api/v1/users'),
        api('/api/v1/issues/ready?limit=200'),
        api('/api/v1/stats'),
      ]);
      state.config = cfg;
      state.sprints = sprintsRes.sprints || [];
      state.epics = epicsRes.epics || [];
      state.milestones = msRes.milestones || [];
      state.views = viewsRes.views || [];
      state.labels = labelsRes.labels || [];
      state.components = compsRes.components || [];
      state.users = usersRes.users || [];

      $('workspace-project-name').textContent = cfg.project_name || 'itrack';
      $('workspace-project-key').textContent = cfg.project_key || 'ITK';
      $('daemon-status-line').textContent = `PID ${health.pid} · :${health.port}`;
      $('cnt-ready').textContent = readyRes.total ?? 0;
      $('cnt-sprints').textContent = state.sprints.length;
      $('cnt-epics').textContent = state.epics.length;
      $('cnt-blocked').textContent = statsRes.blocked_count ?? 0;

      populateSelectOptions();
      renderSavedViews();
    }

    function populateSelectOptions() {
      if (!state.config) return;
      const statuses = state.config.statuses || [];
      const types = state.config.issue_types || [];

      const statusHtml = statuses.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      $('ci-status').innerHTML = statusHtml;
      $('ci-status').value = statuses.includes('todo') ? 'todo' : statuses[0];
      $('dr-status').innerHTML = statusHtml;
      $('bulk-status-select').innerHTML = `<option value="">Status...</option>` + statusHtml;

      const typeHtml = types.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
      $('ci-type').innerHTML = typeHtml;
      $('ci-type').value = state.config.default_type || 'task';
      $('dr-type').innerHTML = typeHtml;

      const epicOpts = `<option value="">None</option>` + state.epics.map((e) => `<option value="${esc(e.issue.key)}">${esc(e.issue.key)} — ${esc(e.issue.title)}</option>`).join('');
      $('ci-epic').innerHTML = epicOpts;
      $('dr-epic').innerHTML = epicOpts;

      const sprintOpts = `<option value="">Backlog</option>` + state.sprints.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}: ${esc(s.name)} (${esc(s.state)})</option>`).join('');
      $('ci-sprint').innerHTML = sprintOpts;
      $('dr-sprint').innerHTML = sprintOpts;
      $('bulk-sprint-select').innerHTML = `<option value="">Sprint...</option><option value="backlog">Backlog</option>` + state.sprints.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}: ${esc(s.name)}</option>`).join('');

      const msOpts = `<option value="">None</option>` + state.milestones.map((m) => `<option value="${esc(m.id)}">${esc(m.id)}: ${esc(m.name)}</option>`).join('');
      $('dr-milestone').innerHTML = msOpts;
    }

    function renderSavedViews() {
      const box = $('saved-views-list');
      if (!state.views.length) {
        box.innerHTML = `<div style="padding:4px 8px;font-size:11.5px;color:var(--text-muted);">No saved views yet</div>`;
        return;
      }
      box.innerHTML = state.views.map((v) => `
        <div class="saved-view-item" data-vid="${esc(v.id)}">
          <span class="sv-run" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(v.name)}</span>
          <button class="btn btn-sm btn-ghost sv-del" title="Delete view" style="padding:0 4px;">&#10005;</button>
        </div>
      `).join('');
      box.querySelectorAll('.saved-view-item').forEach((row) => {
        const v = state.views.find((x) => x.id === row.dataset.vid);
        row.querySelector('.sv-run').addEventListener('click', () => {
          $('global-iql-input').value = v.iql || '';
          state.iql = v.iql || '';
          switchView(v.layout === 'board' ? 'board' : 'list');
        });
        row.querySelector('.sv-del').addEventListener('click', async (e) => {
          e.stopPropagation();
          await api(`/api/v1/views/${v.id}`, { method: 'DELETE' });
          await loadWorkspaceMeta();
        });
      });
    }

    async function refreshCurrentView() {
      try {
        await loadWorkspaceMeta();
        if (state.view === 'board') await renderBoardView();
        else if (state.view === 'list') await renderListView();
        else if (state.view === 'ready') await renderReadyView();
        else if (state.view === 'sprints') await renderSprintsView();
        else if (state.view === 'epics') await renderEpicsView();
        else if (state.view === 'graph') await renderGraphView();
        else if (state.view === 'stats') await renderStatsView();
      } catch (err) {
        toast(err.message, true);
      }
    }

    // 1. Kanban Board View
    async function renderBoardView() {
      const params = new URLSearchParams();
      if (state.iql) params.set('iql', state.iql);
      if (state.boardSprint) params.set('sprint_id', state.boardSprint);
      if (state.boardAssignee) params.set('assignee', state.boardAssignee);
      const data = await api(`/api/v1/board?${params.toString()}`);
      state.board = data;
      $('cnt-board').textContent = data.total_issues;
      $('cnt-list').textContent = data.total_issues;

      // Filter pills
      const pills = [];
      pills.push(`<button class="pill ${!state.boardSprint ? 'active' : ''}" data-sp="">All Sprints</button>`);
      const hasActive = state.sprints.some((s) => s.state === 'active');
      if (hasActive) {
        pills.push(`<button class="pill ${state.boardSprint === 'active' ? 'active' : ''}" data-sp="active">Active Sprint</button>`);
      }
      state.sprints.filter((s) => s.state !== 'completed').forEach((s) => {
        pills.push(`<button class="pill ${state.boardSprint === s.id ? 'active' : ''}" data-sp="${esc(s.id)}">${esc(s.name)}</button>`);
      });
      $('board-filter-pills').innerHTML = pills.join('');
      $('board-filter-pills').querySelectorAll('.pill').forEach((p) => {
        p.addEventListener('click', () => {
          state.boardSprint = p.dataset.sp;
          renderBoardView();
        });
      });

      const colsEl = $('board-columns');
      colsEl.innerHTML = data.columns.map((col) => {
        const wipWarn = col.wip_limit && col.count > col.wip_limit ? 'style="color:var(--danger);font-weight:700;"' : '';
        return `
          <div class="board-col" data-status="${esc(col.status)}">
            <div class="board-col-header">
              <div class="col-title-wrap">
                <span class="status-dot dot-${esc(col.status)}"></span>
                <span>${esc(col.status.replace('_', ' '))}</span>
                <span class="nav-count" ${wipWarn}>${col.count}${col.wip_limit ? '/' + col.wip_limit : ''}</span>
              </div>
              <div style="display:flex;align-items:center;gap:6px;">
                ${col.points ? `<span style="font-size:11px;color:var(--text-muted);">${col.points}pt</span>` : ''}
                <button class="btn btn-sm btn-ghost col-quick-add" data-status="${esc(col.status)}" title="Create issue in ${esc(col.status)}" style="padding:1px 6px;">+</button>
              </div>
            </div>
            <div class="board-cards" data-status="${esc(col.status)}">
              ${col.issues.map((iss) => `
                <article class="issue-card" draggable="true" data-key="${esc(iss.key)}">
                  <div class="card-top">
                    <span class="mono">${esc(iss.key)}</span>
                    <span class="prio-badge prio-${esc(iss.priority)}">${esc(iss.priority)}</span>
                  </div>
                  <div class="card-title">${esc(iss.title)}</div>
                  <div class="card-meta">
                    <div class="tag-row">
                      <span class="tag">${esc(iss.type)}</span>
                      ${iss.is_blocked ? `<span class="tag tag-blocked">Blocked</span>` : ''}
                      ${iss.claimed_by ? `<span class="tag tag-claimed">@${esc(iss.claimed_by)}</span>` : ''}
                      ${(iss.labels || []).slice(0, 2).map((lb) => `<span class="tag">${esc(lb)}</span>`).join('')}
                    </div>
                    <span style="font-size:11px;color:var(--text-secondary);">${[iss.assignee ? esc(iss.assignee) : '', iss.story_points != null ? `${iss.story_points}p` : ''].filter(Boolean).join(' · ')}</span>
                  </div>
                </article>
              `).join('')}
            </div>
          </div>
        `;
      }).join('');

      colsEl.querySelectorAll('.col-quick-add').forEach((btn) => {
        btn.addEventListener('click', () => openCreateModal({ status: btn.dataset.status }));
      });
      colsEl.querySelectorAll('.issue-card').forEach((card) => {
        card.addEventListener('click', () => openIssueDrawer(card.dataset.key));
        card.addEventListener('dragstart', (e) => {
          e.dataTransfer.setData('text/plain', card.dataset.key);
        });
      });
      colsEl.querySelectorAll('.board-col').forEach((colEl) => {
        colEl.addEventListener('dragover', (e) => {
          e.preventDefault();
          colEl.classList.add('drag-over');
        });
        colEl.addEventListener('dragleave', () => colEl.classList.remove('drag-over'));
        colEl.addEventListener('drop', async (e) => {
          e.preventDefault();
          colEl.classList.remove('drag-over');
          const key = e.dataTransfer.getData('text/plain');
          const targetStatus = colEl.dataset.status;
          if (!key || !targetStatus) return;
          try {
            await api(`/api/v1/issues/${key}/transition`, {
              method: 'POST',
              body: { status: targetStatus }
            });
            await renderBoardView();
          } catch (err) {
            toast(err.message, true);
          }
        });
      });
    }

    // 2. Linear List & Bulk Triage View
    async function renderListView(ftsQuery = '') {
      const params = new URLSearchParams();
      if (state.iql) params.set('iql', state.iql);
      if (ftsQuery) params.set('q', ftsQuery);
      params.set('limit', '500');
      const res = await api(`/api/v1/issues?${params.toString()}`);
      state.issues = res.issues || [];
      $('cnt-list').textContent = res.total;
      $('list-summary-count').textContent = `${res.total} issue${res.total === 1 ? '' : 's'}${state.iql ? ` matching iQL` : ''}`;

      const tbody = $('list-tbody');
      if (!state.issues.length) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:26px;">No matching issues. Press <b>c</b> to create one.</td></tr>`;
      } else {
        tbody.innerHTML = state.issues.map((iss) => `
          <tr data-key="${esc(iss.key)}">
            <td class="td-chk"><input type="checkbox" class="row-chk" data-key="${esc(iss.key)}" ${state.selectedKeys.has(iss.key) ? 'checked' : ''} /></td>
            <td class="mono" style="font-weight:600;">${esc(iss.key)}</td>
            <td><span class="prio-badge prio-${esc(iss.priority)}">${esc(iss.priority)}</span></td>
            <td>
              <span style="font-weight:550;">${esc(iss.title)}</span>
              ${iss.is_blocked ? `<span class="tag tag-blocked" style="margin-left:6px;">Blocked</span>` : ''}
              ${(iss.labels || []).map((l) => `<span class="tag" style="margin-left:4px;">${esc(l)}</span>`).join('')}
            </td>
            <td><span class="status-dot dot-${esc(iss.status)}" style="margin-right:5px;"></span>${esc(iss.status)}</td>
            <td><span class="tag">${esc(iss.type)}</span></td>
            <td style="color:var(--text-secondary);">${esc(iss.assignee || '—')}</td>
            <td class="mono">${iss.story_points != null ? iss.story_points : '—'}</td>
          </tr>
        `).join('');
      }

      tbody.querySelectorAll('tr[data-key]').forEach((tr) => {
        tr.addEventListener('click', (e) => {
          if (e.target.classList.contains('row-chk') || e.target.closest('.td-chk')) return;
          openIssueDrawer(tr.dataset.key);
        });
      });
      tbody.querySelectorAll('.row-chk').forEach((chk) => {
        chk.addEventListener('change', () => {
          if (chk.checked) state.selectedKeys.add(chk.dataset.key);
          else state.selectedKeys.delete(chk.dataset.key);
          syncBulkBar();
        });
      });
      syncBulkBar();
    }

    $('list-select-all').addEventListener('change', (e) => {
      if (e.target.checked) state.issues.forEach((i) => state.selectedKeys.add(i.key));
      else state.selectedKeys.clear();
      renderListView($('list-fts-input').value.trim());
    });
    $('btn-list-fts').addEventListener('click', () => renderListView($('list-fts-input').value.trim()));
    $('list-fts-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') renderListView($('list-fts-input').value.trim());
    });

    function syncBulkBar() {
      const n = state.selectedKeys.size;
      $('bulk-selected-count').textContent = `${n} selected`;
      $('bulk-action-bar').classList.toggle('visible', n > 0 && state.view === 'list');
    }

    async function runBulk(action, fields = {}) {
      const keys = Array.from(state.selectedKeys);
      if (!keys.length) return;
      try {
        await api('/api/v1/issues/bulk', { method: 'POST', body: { keys, action, fields } });
        state.selectedKeys.clear();
        $('list-select-all').checked = false;
        toast(`Bulk ${action} applied to ${keys.length} issue(s)`);
        await renderListView();
      } catch (err) {
        toast(err.message, true);
      }
    }
    $('btn-bulk-transition').addEventListener('click', () => {
      const st = $('bulk-status-select').value;
      if (st) runBulk('transition', { status: st, force: true });
    });
    $('btn-bulk-priority').addEventListener('click', () => {
      const p = $('bulk-priority-select').value;
      if (p) runBulk('update', { priority: p });
    });
    $('btn-bulk-assign').addEventListener('click', () => {
      runBulk('update', { assignee: $('bulk-assignee-input').value.trim() || null });
    });
    $('btn-bulk-label').addEventListener('click', () => {
      const lb = $('bulk-label-input').value.trim();
      if (lb) runBulk('label_add', { label: lb });
    });
    $('btn-bulk-sprint').addEventListener('click', () => {
      const sp = $('bulk-sprint-select').value;
      if (sp) runBulk('sprint', { sprint_id: sp === 'backlog' ? null : sp });
    });
    $('btn-bulk-delete').addEventListener('click', () => {
      if (confirm(`Delete ${state.selectedKeys.size} selected issue(s)?`)) runBulk('delete');
    });

    // 3. Ready Queue View
    async function renderReadyView() {
      const [readyRes, claimedRes] = await Promise.all([
        api('/api/v1/issues/ready?limit=100'),
        api('/api/v1/issues?iql=' + encodeURIComponent('status = in_progress ORDER BY updated DESC')),
      ]);
      const readyList = readyRes.issues || [];
      const activeList = claimedRes.issues || [];
      $('cnt-ready').textContent = readyRes.total;

      const rBox = $('ready-queue-cards');
      rBox.innerHTML = readyList.length ? readyList.map((iss) => `
        <div class="panel-card" style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
          <div style="cursor:pointer;flex:1;" data-open="${esc(iss.key)}">
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:3px;">
              <span class="mono" style="font-weight:700;">${esc(iss.key)}</span>
              <span class="prio-badge prio-${esc(iss.priority)}">${esc(iss.priority)}</span>
              <span class="tag">${esc(iss.type)}</span>
            </div>
            <div style="font-weight:550;">${esc(iss.title)}</div>
          </div>
          <button class="btn btn-sm btn-primary btn-claim-row" data-key="${esc(iss.key)}">Claim</button>
        </div>
      `).join('') : `<div class="panel-card" style="color:var(--text-muted);">Queue empty — all open tasks are either claimed or blocked.</div>`;

      const cBox = $('claimed-active-cards');
      cBox.innerHTML = activeList.length ? activeList.map((iss) => `
        <div class="panel-card" style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
          <div style="cursor:pointer;flex:1;" data-open="${esc(iss.key)}">
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:3px;">
              <span class="mono" style="font-weight:700;">${esc(iss.key)}</span>
              <span class="tag tag-claimed">${esc(iss.claimed_by || iss.assignee || 'in_progress')}</span>
            </div>
            <div style="font-weight:550;">${esc(iss.title)}</div>
          </div>
          <div style="display:flex;gap:6px;">
            <button class="btn btn-sm btn-rel-row" data-key="${esc(iss.key)}">Release</button>
            <button class="btn btn-sm btn-primary btn-done-row" data-key="${esc(iss.key)}">Done</button>
          </div>
        </div>
      `).join('') : `<div class="panel-card" style="color:var(--text-muted);">No issues currently in progress.</div>`;

      document.querySelectorAll('#view-ready [data-open]').forEach((el) => {
        el.addEventListener('click', () => openIssueDrawer(el.dataset.open));
      });
      rBox.querySelectorAll('.btn-claim-row').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const agent = $('ready-agent-input').value.trim() || 'agent-1';
          try {
            await api(`/api/v1/issues/${btn.dataset.key}/claim`, { method: 'POST', body: { agent } });
            toast(`${btn.dataset.key} claimed by ${agent}`);
            await renderReadyView();
          } catch (err) { toast(err.message, true); }
        });
      });
      cBox.querySelectorAll('.btn-rel-row').forEach((btn) => {
        btn.addEventListener('click', async () => {
          await api(`/api/v1/issues/${btn.dataset.key}/release`, { method: 'POST', body: { status: 'todo' } });
          await renderReadyView();
        });
      });
      cBox.querySelectorAll('.btn-done-row').forEach((btn) => {
        btn.addEventListener('click', async () => {
          await api(`/api/v1/issues/${btn.dataset.key}/transition`, { method: 'POST', body: { status: 'done' } });
          await renderReadyView();
        });
      });
    }

    // 4. Sprints & Backlog Planner
    $('btn-create-sprint').addEventListener('click', async () => {
      const name = $('sprint-name-input').value.trim();
      if (!name) return toast('Enter a sprint name', true);
      await api('/api/v1/sprints', {
        method: 'POST',
        body: {
          name,
          goal: $('sprint-goal-input').value.trim(),
          start_date: $('sprint-start-input').value || null,
          end_date: $('sprint-end-input').value || null,
        }
      });
      $('sprint-name-input').value = '';
      $('sprint-goal-input').value = '';
      toast(`Created sprint "${name}"`);
      await renderSprintsView();
    });

    async function renderSprintsView() {
      const details = await Promise.all(state.sprints.map((s) => api(`/api/v1/sprints/${s.id}`)));
      const backlogRes = await api('/api/v1/issues?iql=' + encodeURIComponent('sprint IS EMPTY AND status NOT IN (done, canceled)'));
      const box = $('sprints-container');

      box.innerHTML = details.length ? details.map((d) => {
        const s = d.sprint;
        const m = d.metrics;
        const otherSprints = state.sprints.filter((x) => x.id !== s.id && x.state !== 'completed');
        return `
          <div class="panel-card">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
              <div>
                <span class="mono" style="font-weight:700;">${esc(s.id)}</span>
                <strong style="font-size:14px;margin-left:6px;">${esc(s.name)}</strong>
                <span class="tag" style="margin-left:6px;">${esc(s.state)}</span>
                ${s.goal ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px;">Goal: ${esc(s.goal)}</div>` : ''}
              </div>
              <div style="display:flex;gap:6px;align-items:center;">
                <span style="font-size:12px;color:var(--text-secondary);">${m.done_issues}/${m.total_issues} done · ${m.done_points}/${m.total_points} pts (${m.progress_pct}%)</span>
                ${s.state === 'planned' ? `<button class="btn btn-sm btn-primary sp-start" data-id="${esc(s.id)}">Start Sprint</button>` : ''}
                ${s.state === 'active' ? `
                  <select class="select sp-rollover" data-id="${esc(s.id)}" style="width:140px;">
                    <option value="backlog">Rollover -> Backlog</option>
                    ${otherSprints.map((o) => `<option value="${esc(o.id)}">Rollover -> ${esc(o.id)}</option>`).join('')}
                  </select>
                  <button class="btn btn-sm btn-primary sp-complete" data-id="${esc(s.id)}">Complete Sprint</button>
                ` : ''}
                <button class="btn btn-sm btn-danger sp-del" data-id="${esc(s.id)}">&#10005;</button>
              </div>
            </div>
            <div class="progress-track"><div class="progress-fill" style="width:${m.progress_pct}%"></div></div>
            <div style="margin-top:8px;display:flex;flex-direction:column;gap:4px;">
              ${d.issues.map((i) => `
                <div style="display:flex;justify-content:space-between;padding:4px 6px;background:var(--bg-surface-2);border-radius:4px;cursor:pointer;" data-open="${esc(i.key)}">
                  <span><span class="mono">${esc(i.key)}</span> · ${esc(i.title)}</span>
                  <span class="tag">${esc(i.status)}</span>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      }).join('') : `<div class="panel-card" style="color:var(--text-muted);">No sprints created yet.</div>`;

      box.querySelectorAll('.sp-start').forEach((b) => b.addEventListener('click', async () => {
        await api(`/api/v1/sprints/${b.dataset.id}/start`, { method: 'POST', body: {} });
        toast(`Started ${b.dataset.id}`);
        await refreshCurrentView();
      }));
      box.querySelectorAll('.sp-complete').forEach((b) => b.addEventListener('click', async () => {
        const sel = box.querySelector(`.sp-rollover[data-id="${b.dataset.id}"]`);
        const res = await api(`/api/v1/sprints/${b.dataset.id}/complete`, {
          method: 'POST',
          body: { rollover_to: sel ? sel.value : 'backlog' }
        });
        toast(`Completed ${b.dataset.id} (${res.rolled_over_keys.length} rolled over)`);
        await refreshCurrentView();
      }));
      box.querySelectorAll('.sp-del').forEach((b) => b.addEventListener('click', async () => {
        await api(`/api/v1/sprints/${b.dataset.id}`, { method: 'DELETE' });
        await refreshCurrentView();
      }));
      box.querySelectorAll('[data-open]').forEach((el) => el.addEventListener('click', () => openIssueDrawer(el.dataset.open)));

      const blBox = $('backlog-planner-list');
      const openSprints = state.sprints.filter((s) => s.state !== 'completed');
      blBox.innerHTML = (backlogRes.issues || []).length ? backlogRes.issues.map((i) => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border-subtle);">
          <span style="cursor:pointer;" data-open="${esc(i.key)}"><span class="mono" style="font-weight:600;">${esc(i.key)}</span> ${esc(i.title)}</span>
          <select class="select bl-assign-sp" data-key="${esc(i.key)}" style="width:160px;">
            <option value="">Assign to sprint...</option>
            ${openSprints.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}: ${esc(s.name)}</option>`).join('')}
          </select>
        </div>
      `).join('') : `<div style="color:var(--text-muted);font-size:12px;">No unscheduled backlog issues.</div>`;
      blBox.querySelectorAll('[data-open]').forEach((el) => el.addEventListener('click', () => openIssueDrawer(el.dataset.open)));
      blBox.querySelectorAll('.bl-assign-sp').forEach((sel) => sel.addEventListener('change', async () => {
        if (!sel.value) return;
        await api(`/api/v1/issues/${sel.dataset.key}`, { method: 'PATCH', body: { sprint_id: sel.value } });
        await renderSprintsView();
      }));
    }

    // 5. Epics & Milestones
    $('btn-quick-epic').addEventListener('click', () => openCreateModal({ type: 'epic' }));
    $('btn-create-milestone').addEventListener('click', async () => {
      const name = $('ms-name-input').value.trim();
      if (!name) return toast('Enter milestone name', true);
      await api('/api/v1/milestones', {
        method: 'POST',
        body: { name, target_date: $('ms-date-input').value || null }
      });
      $('ms-name-input').value = '';
      await refreshCurrentView();
    });

    async function renderEpicsView() {
      const eBox = $('epics-list-container');
      eBox.innerHTML = state.epics.length ? state.epics.map((ep) => `
        <div class="panel-card">
          <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;" data-open="${esc(ep.issue.key)}">
            <div><span class="mono" style="font-weight:700;">${esc(ep.issue.key)}</span> <strong>${esc(ep.issue.title)}</strong></div>
            <span style="font-size:12px;color:var(--text-secondary);">${ep.done_count}/${ep.child_count} issues · ${ep.progress_pct}%</span>
          </div>
          <div class="progress-track"><div class="progress-fill" style="width:${ep.progress_pct}%"></div></div>
          <div class="tag-row" style="margin-top:6px;">
            ${ep.children.map((ck) => `<span class="tag mono" style="cursor:pointer;" data-open="${esc(ck)}">${esc(ck)}</span>`).join('')}
          </div>
        </div>
      `).join('') : `<div class="panel-card" style="color:var(--text-muted);">No epics yet. Click "+ New Epic" to create one.</div>`;

      const mBox = $('milestones-list-container');
      mBox.innerHTML = state.milestones.length ? state.milestones.map((m) => `
        <div class="panel-card">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <span class="mono" style="font-weight:700;">${esc(m.id)}</span> <strong>${esc(m.name)}</strong>
              ${m.target_date ? `<span style="font-size:11.5px;color:var(--text-muted);margin-left:6px;">Due ${esc(m.target_date)}</span>` : ''}
            </div>
            <div style="display:flex;gap:6px;align-items:center;">
              <select class="select ms-status" data-id="${esc(m.id)}" style="width:115px;">
                <option value="unreleased" ${m.status === 'unreleased' ? 'selected' : ''}>unreleased</option>
                <option value="released" ${m.status === 'released' ? 'selected' : ''}>released</option>
                <option value="archived" ${m.status === 'archived' ? 'selected' : ''}>archived</option>
              </select>
              <button class="btn btn-sm btn-danger ms-del" data-id="${esc(m.id)}">&#10005;</button>
            </div>
          </div>
          <div class="progress-track"><div class="progress-fill" style="width:${m.progress_pct}%"></div></div>
          <div style="font-size:11.5px;color:var(--text-secondary);">${m.done_issues}/${m.total_issues} issues complete</div>
        </div>
      `).join('') : `<div class="panel-card" style="color:var(--text-muted);">No milestones yet.</div>`;

      eBox.querySelectorAll('[data-open]').forEach((el) => el.addEventListener('click', (e) => {
        e.stopPropagation();
        openIssueDrawer(el.dataset.open);
      }));
      mBox.querySelectorAll('.ms-status').forEach((sel) => sel.addEventListener('change', async () => {
        await api(`/api/v1/milestones/${sel.dataset.id}`, { method: 'PATCH', body: { status: sel.value } });
        await refreshCurrentView();
      }));
      mBox.querySelectorAll('.ms-del').forEach((b) => b.addEventListener('click', async () => {
        await api(`/api/v1/milestones/${b.dataset.id}`, { method: 'DELETE' });
        await refreshCurrentView();
      }));
    }

    // 6. Dependency DAG View
    $('btn-graph-add-link').addEventListener('click', async () => {
      const src = $('graph-src-input').value.trim().toUpperCase();
      const tgt = $('graph-tgt-input').value.trim().toUpperCase();
      const link_type = $('graph-type-select').value;
      if (!src || !tgt) return toast('Provide both source and target issue keys', true);
      try {
        await api(`/api/v1/issues/${src}/links`, { method: 'POST', body: { target_key: tgt, link_type } });
        toast(`Linked: ${src} ${link_type} ${tgt}`);
        await renderGraphView();
      } catch (err) {
        toast(err.message, true);
      }
    });

    async function renderGraphView() {
      const g = await api('/api/v1/graph/dependencies');
      $('cnt-blocked').textContent = g.blocked_keys.length;

      // Topological layer layout for SVG DAG
      const nodes = g.nodes || [];
      const edges = g.edges || [];
      const svgWrap = $('graph-svg-container');

      if (!nodes.length) {
        svgWrap.innerHTML = `<div style="color:var(--text-muted);padding:20px;">No issues in workspace yet.</div>`;
      } else {
        const rank = {};
        nodes.forEach((n) => { rank[n.key] = 0; });
        // First pass: strict DAG blocking edges
        for (let iter = 0; iter < nodes.length; iter++) {
          edges.forEach((e) => {
            if (e.type === 'blocks' && rank[e.source] !== undefined && rank[e.target] !== undefined) {
              if (rank[e.target] <= rank[e.source]) rank[e.target] = rank[e.source] + 1;
            }
          });
        }
        // Second pass: advance non-blocking edges (relates_to, duplicates, clones) when acyclic
        const maxCap = Math.max(1, nodes.length - 1);
        for (let iter = 0; iter < nodes.length; iter++) {
          edges.forEach((e) => {
            if (e.type !== 'blocks' && rank[e.source] !== undefined && rank[e.target] !== undefined) {
              if (rank[e.target] <= rank[e.source] && rank[e.source] + 1 <= maxCap) {
                const reverseExists = edges.some((r) => r.source === e.target && r.target === e.source);
                if (!reverseExists || e.source < e.target) {
                  rank[e.target] = rank[e.source] + 1;
                }
              }
            }
          });
        }
        const layers = {};
        nodes.forEach((n) => {
          const r = rank[n.key] || 0;
          (layers[r] = layers[r] || []).push(n);
        });
        const layerKeys = Object.keys(layers).map(Number).sort((a, b) => a - b);
        const pos = {};
        const nodeW = 168, nodeH = 48, colGap = 230, rowGap = 68;
        let maxRows = 1;
        layerKeys.forEach((lk, colIdx) => {
          const arr = layers[lk];
          maxRows = Math.max(maxRows, arr.length);
          arr.forEach((n, rowIdx) => {
            pos[n.key] = { x: 24 + colIdx * colGap, y: 28 + rowIdx * rowGap };
          });
        });
        const svgW = Math.max(600, 84 + layerKeys.length * colGap);
        const svgH = Math.max(200, 56 + maxRows * rowGap);

        const edgePaths = edges.map((e) => {
          const p1 = pos[e.source], p2 = pos[e.target];
          if (!p1 || !p2) return '';
          const color = e.type === 'blocks' ? 'var(--danger)' : 'var(--accent)';
          const dash = e.type === 'blocks' ? '' : 'stroke-dasharray="5,3"';
          if (p2.x > p1.x) {
            const x1 = p1.x + nodeW, y1 = p1.y + nodeH / 2;
            const x2 = p2.x, y2 = p2.y + nodeH / 2;
            const cx = (x1 + x2) / 2;
            return `<path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}" fill="none" stroke="${color}" ${dash} stroke-width="1.8" marker-end="url(#arrow)" />`;
          } else if (p2.x === p1.x) {
            const x1 = p1.x + nodeW, y1 = p1.y + nodeH / 2;
            const x2 = p2.x + nodeW, y2 = p2.y + nodeH / 2;
            const arcX = x1 + 44;
            return `<path d="M${x1},${y1} C${arcX},${y1} ${arcX},${y2} ${x2},${y2}" fill="none" stroke="${color}" ${dash} stroke-width="1.8" marker-end="url(#arrow)" />`;
          } else {
            const x1 = p1.x + nodeW / 2, y1 = p1.y;
            const x2 = p2.x + nodeW / 2, y2 = p2.y;
            const arcY = Math.max(4, Math.min(y1, y2) - 26);
            return `<path d="M${x1},${y1} C${x1},${arcY} ${x2},${arcY} ${x2},${y2}" fill="none" stroke="${color}" ${dash} stroke-width="1.8" marker-end="url(#arrow)" />`;
          }
        }).join('');

        const nodeRects = nodes.map((n) => {
          const p = pos[n.key];
          const stroke = n.status === 'done' ? 'var(--ok)' : (n.is_blocked ? 'var(--danger)' : 'var(--border-strong)');
          return `
            <g class="dag-node" data-key="${esc(n.key)}" style="cursor:pointer;">
              <rect x="${p.x}" y="${p.y}" width="${nodeW}" height="${nodeH}" rx="7" fill="var(--bg-surface-2)" stroke="${stroke}" stroke-width="1.5" />
              <text x="${p.x + 10}" y="${p.y + 19}" fill="var(--text-primary)" font-size="11.5" font-weight="700" font-family="monospace">${esc(n.key)} · ${esc(n.status)}</text>
              <text x="${p.x + 10}" y="${p.y + 36}" fill="var(--text-secondary)" font-size="11">${esc(n.title.slice(0, 21))}</text>
            </g>
          `;
        }).join('');

        svgWrap.innerHTML = `
          <svg width="${svgW}" height="${svgH}" viewBox="0 0 ${svgW} ${svgH}">
            <defs>
              <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                <path d="M0,0 L8,4 L0,8 Z" fill="var(--text-secondary)" />
              </marker>
            </defs>
            ${edgePaths}
            ${nodeRects}
          </svg>
        `;
        svgWrap.querySelectorAll('.dag-node').forEach((gn) => {
          gn.addEventListener('click', () => openIssueDrawer(gn.dataset.key));
        });
      }

      $('graph-blocked-list').innerHTML = g.blocked_keys.length
        ? g.blocked_keys.map((k) => `<span class="tag tag-blocked mono" style="cursor:pointer;margin:2px;" data-open="${esc(k)}">${esc(k)}</span>`).join('')
        : `<span style="color:var(--text-muted);font-size:12px;">Zero blocked issues</span>`;
      $('graph-ready-list').innerHTML = g.ready_keys.length
        ? g.ready_keys.map((k) => `<span class="tag mono" style="cursor:pointer;margin:2px;" data-open="${esc(k)}">${esc(k)}</span>`).join('')
        : `<span style="color:var(--text-muted);font-size:12px;">No ready issues</span>`;
      document.querySelectorAll('#view-graph [data-open]').forEach((el) => {
        el.addEventListener('click', () => openIssueDrawer(el.dataset.open));
      });
    }

    // 7. Stats & Workspace Settings
    async function renderStatsView() {
      const st = await api('/api/v1/stats');
      $('cnt-blocked').textContent = st.blocked_count;
      $('stats-kpi-grid').innerHTML = `
        <div class="kpi-box"><div class="kpi-label">Total Issues</div><div class="kpi-val">${st.total_issues}</div></div>
        <div class="kpi-box"><div class="kpi-label">Open / Active</div><div class="kpi-val">${st.open_count}</div></div>
        <div class="kpi-box"><div class="kpi-label">Ready Queue</div><div class="kpi-val" style="color:var(--ok);">${st.ready_count}</div></div>
        <div class="kpi-box"><div class="kpi-label">Blocked</div><div class="kpi-val" style="color:var(--danger);">${st.blocked_count}</div></div>
        <div class="kpi-box"><div class="kpi-label">Points Done</div><div class="kpi-val">${st.completed_points} / ${st.total_points}</div></div>
      `;

      const renderBars = (obj) => {
        const entries = Object.entries(obj || {});
        const total = Math.max(1, entries.reduce((a, [, v]) => a + v, 0));
        return entries.map(([k, v]) => `
          <div class="bar-row">
            <span style="color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;">${esc(k)}</span>
            <div class="progress-track" style="margin:0;"><div class="progress-fill" style="width:${Math.round(v / total * 100)}%;background:var(--accent);"></div></div>
            <span class="mono" style="text-align:right;">${v}</span>
          </div>
        `).join('') || `<div style="color:var(--text-muted);font-size:12px;">No data</div>`;
      };

      $('stats-bars-left').innerHTML = `<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">BY STATUS</div>` + renderBars(st.by_status)
        + `<div style="font-size:11px;color:var(--text-muted);margin:12px 0 4px;">BY PRIORITY</div>` + renderBars(st.by_priority);
      $('stats-bars-right').innerHTML = `<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">BY TYPE</div>` + renderBars(st.by_type)
        + `<div style="font-size:11px;color:var(--text-muted);margin:12px 0 4px;">BY ASSIGNEE</div>` + renderBars(st.by_assignee);

      $('cfg-project-key').value = state.config.project_key || 'ITK';
      $('cfg-project-name').value = state.config.project_name || '';

      $('settings-labels-list').innerHTML = state.labels.map((l) => `
        <span class="tag" style="border-left:3px solid ${esc(l.color || '#3b82f6')};">${esc(l.name)} <b style="cursor:pointer;margin-left:4px;" data-del-lb="${esc(l.name)}">&times;</b></span>
      `).join('');
      $('settings-labels-list').querySelectorAll('[data-del-lb]').forEach((x) => x.addEventListener('click', async () => {
        await api(`/api/v1/labels/${encodeURIComponent(x.dataset.delLb)}`, { method: 'DELETE' });
        await refreshCurrentView();
      }));

      $('settings-comps-list').innerHTML = state.components.map((c) => `
        <span class="tag">${esc(c.name)}${c.lead ? ` (${esc(c.lead)})` : ''} <b style="cursor:pointer;margin-left:4px;" data-del-cp="${esc(c.name)}">&times;</b></span>
      `).join('');
      $('settings-comps-list').querySelectorAll('[data-del-cp]').forEach((x) => x.addEventListener('click', async () => {
        await api(`/api/v1/components/${encodeURIComponent(x.dataset.delCp)}`, { method: 'DELETE' });
        await refreshCurrentView();
      }));

      $('settings-users-list').innerHTML = state.users.map((u) => `
        <span class="tag">${u.is_agent ? '&#9889; ' : ''}${esc(u.username)} <b style="cursor:pointer;margin-left:4px;" data-del-u="${esc(u.username)}">&times;</b></span>
      `).join('');
      $('settings-users-list').querySelectorAll('[data-del-u]').forEach((x) => x.addEventListener('click', async () => {
        await api(`/api/v1/users/${encodeURIComponent(x.dataset.delU)}`, { method: 'DELETE' });
        await refreshCurrentView();
      }));
    }

    $('btn-add-label').addEventListener('click', async () => {
      const name = $('new-label-name').value.trim();
      if (!name) return;
      await api('/api/v1/labels', { method: 'POST', body: { name, color: $('new-label-color').value } });
      $('new-label-name').value = '';
      await refreshCurrentView();
    });
    $('btn-add-comp').addEventListener('click', async () => {
      const name = $('new-comp-name').value.trim();
      if (!name) return;
      await api('/api/v1/components', { method: 'POST', body: { name, lead: $('new-comp-lead').value.trim() || null } });
      $('new-comp-name').value = '';
      $('new-comp-lead').value = '';
      await refreshCurrentView();
    });
    $('btn-add-user').addEventListener('click', async () => {
      const username = $('new-user-name').value.trim();
      if (!username) return;
      await api('/api/v1/users', { method: 'POST', body: { username, is_agent: $('new-user-agent').checked } });
      $('new-user-name').value = '';
      await refreshCurrentView();
    });
    $('btn-save-config').addEventListener('click', async () => {
      try {
        await api('/api/v1/config', {
          method: 'PATCH',
          body: { project_key: $('cfg-project-key').value.trim(), project_name: $('cfg-project-name').value.trim() }
        });
        toast('Workspace configuration saved');
        await refreshCurrentView();
      } catch (err) { toast(err.message, true); }
    });
    $('btn-reindex').addEventListener('click', async () => {
      const res = await api('/api/v1/reindex', { method: 'POST' });
      toast(`Reindexed ${res.indexed_issues} issues from JSON in ${res.duration_ms}ms`);
    });
    $('btn-export-json').addEventListener('click', async () => {
      const bundle = await api('/api/v1/export');
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `itrack-export-${Date.now()}.json`;
      a.click();
    });

    // Create Issue Modal
    function openCreateModal(prefill = {}) {
      $('form-create-issue').reset();
      populateSelectOptions();
      if (prefill.status) $('ci-status').value = prefill.status;
      if (prefill.type) $('ci-type').value = prefill.type;
      $('create-issue-modal').classList.add('open');
      setTimeout(() => $('ci-title').focus(), 30);
    }
    function closeCreateModal() {
      $('create-issue-modal').classList.remove('open');
    }
    $('btn-new-issue').addEventListener('click', () => openCreateModal());
    $('btn-close-create-modal').addEventListener('click', closeCreateModal);
    $('btn-cancel-create').addEventListener('click', closeCreateModal);

    $('form-create-issue').addEventListener('submit', async (e) => {
      e.preventDefault();
      const labels = $('ci-labels').value.split(',').map((s) => s.trim()).filter(Boolean);
      const components = $('ci-components').value.split(',').map((s) => s.trim()).filter(Boolean);
      const pts = $('ci-points').value;
      try {
        const created = await api('/api/v1/issues', {
          method: 'POST',
          body: {
            title: $('ci-title').value.trim(),
            description: $('ci-desc').value,
            type: $('ci-type').value,
            priority: $('ci-priority').value,
            status: $('ci-status').value,
            assignee: $('ci-assignee').value.trim() || null,
            labels,
            components,
            story_points: pts !== '' ? Number(pts) : null,
            epic_id: $('ci-epic').value || null,
            sprint_id: $('ci-sprint').value || null,
            due_date: $('ci-due').value || null,
          }
        });
        closeCreateModal();
        toast(`Created ${created.key}`);
        await refreshCurrentView();
      } catch (err) {
        toast(err.message, true);
      }
    });

    // Slide-over Issue Detail Drawer
    async function openIssueDrawer(key) {
      try {
        const [iss, subRes] = await Promise.all([
          api(`/api/v1/issues/${key}`),
          api(`/api/v1/issues/${key}/subtasks`),
        ]);
        state.activeIssue = iss;
        populateSelectOptions();

        $('dr-key').textContent = iss.key;
        $('dr-title').value = iss.title || '';
        $('dr-desc').value = iss.description || '';
        $('dr-status').value = iss.status;
        $('dr-priority').value = iss.priority;
        $('dr-type').value = iss.type;
        $('dr-assignee').value = iss.assignee || '';
        $('dr-points').value = iss.story_points != null ? iss.story_points : '';
        $('dr-sprint').value = iss.sprint_id || '';
        $('dr-epic').value = iss.epic_id || '';
        $('dr-milestone').value = iss.milestone_id || '';
        $('dr-due').value = iss.due_date || '';
        $('dr-labels').value = (iss.labels || []).join(', ');
        $('dr-components').value = (iss.components || []).join(', ');

        $('dr-blocked-badge').style.display = iss.is_blocked ? 'inline-block' : 'none';
        $('dr-blocked-badge').textContent = iss.unresolved_blockers.length
          ? `BLOCKED BY ${iss.unresolved_blockers.join(', ')}`
          : 'BLOCKED';

        if (iss.claimed_by) {
          $('dr-claimed-badge').style.display = 'inline-block';
          $('dr-claimed-badge').textContent = `Claimed: ${iss.claimed_by}`;
          $('dr-btn-claim').style.display = 'none';
          $('dr-btn-release').style.display = 'inline-flex';
        } else {
          $('dr-claimed-badge').style.display = 'none';
          $('dr-btn-claim').style.display = 'inline-flex';
          $('dr-btn-release').style.display = 'none';
        }

        $('dr-timestamps').innerHTML = `Created: ${esc(iss.created_at)}<br/>Updated: ${esc(iss.updated_at)}`;

        // Subtasks
        $('dr-subtasks-count').textContent = `${subRes.completed_count}/${subRes.total_count}`;
        $('dr-subtasks-list').innerHTML = (subRes.subtasks || []).map((st) => `
          <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border-subtle);">
            <label style="display:flex;align-items:center;gap:7px;cursor:pointer;">
              <input type="checkbox" class="sub-toggle" data-key="${esc(st.key)}" ${st.status === 'done' ? 'checked' : ''} />
              <span class="mono">${esc(st.key)}</span>
              <span style="${st.status === 'done' ? 'text-decoration:line-through;color:var(--text-muted);' : ''}">${esc(st.title)}</span>
            </label>
            <button class="btn btn-sm btn-ghost" data-jump="${esc(st.key)}">Open</button>
          </div>
        `).join('');
        $('dr-subtasks-list').querySelectorAll('.sub-toggle').forEach((chk) => {
          chk.addEventListener('change', async () => {
            await api(`/api/v1/issues/${chk.dataset.key}/transition`, {
              method: 'POST',
              body: { status: chk.checked ? 'done' : 'todo', force: true }
            });
            await openIssueDrawer(iss.key);
            await refreshCurrentView();
          });
        });
        $('dr-subtasks-list').querySelectorAll('[data-jump]').forEach((b) => {
          b.addEventListener('click', () => openIssueDrawer(b.dataset.jump));
        });

        // Links
        $('dr-links-list').innerHTML = (iss.all_links || []).map((l) => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--border-subtle);">
            <span><span class="tag">${esc(l.link_type)}</span> <a href="#" class="mono link-jump" data-key="${esc(l.target_key)}" style="color:var(--accent);">${esc(l.target_key)}</a></span>
            <button class="btn btn-sm btn-ghost link-del" data-id="${esc(l.id)}">&#10005;</button>
          </div>
        `).join('') || `<div style="font-size:12px;color:var(--text-muted);">No linked issues.</div>`;
        $('dr-links-list').querySelectorAll('.link-jump').forEach((a) => {
          a.addEventListener('click', (e) => { e.preventDefault(); openIssueDrawer(a.dataset.key); });
        });
        $('dr-links-list').querySelectorAll('.link-del').forEach((b) => {
          b.addEventListener('click', async () => {
            await api(`/api/v1/issues/${iss.key}/links/${b.dataset.id}`, { method: 'DELETE' });
            await openIssueDrawer(iss.key);
            await refreshCurrentView();
          });
        });

        // Attachments
        $('dr-attachments-list').innerHTML = (iss.attachments || []).map((a) => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border-subtle);">
            <div>
              <a href="/api/v1/issues/${encodeURIComponent(iss.key)}/attachments/${encodeURIComponent(a.id)}/download" style="color:var(--accent);font-weight:550;">${esc(a.filename)}</a>
              <span class="mono" style="font-size:11px;color:var(--text-muted);margin-left:6px;">${a.size_bytes}B · sha256:${esc((a.sha256 || '').slice(0, 10))}</span>
            </div>
            <button class="btn btn-sm btn-ghost att-del" data-id="${esc(a.id)}">&#10005;</button>
          </div>
        `).join('') || `<div style="font-size:12px;color:var(--text-muted);">No attachments uploaded.</div>`;
        $('dr-attachments-list').querySelectorAll('.att-del').forEach((b) => {
          b.addEventListener('click', async () => {
            await api(`/api/v1/issues/${iss.key}/attachments/${b.dataset.id}`, { method: 'DELETE' });
            await openIssueDrawer(iss.key);
          });
        });

        // Comments
        $('dr-comments-list').innerHTML = (iss.comments || []).map((c) => `
          <div class="comment-item">
            <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);margin-bottom:4px;">
              <span><strong>${esc(c.author)}</strong> · ${esc(c.created_at)}</span>
              <span>
                <button class="btn btn-sm btn-ghost cmt-edit" data-id="${esc(c.id)}" style="padding:0 4px;">Edit</button>
                <button class="btn btn-sm btn-ghost cmt-del" data-id="${esc(c.id)}" style="padding:0 4px;">&#10005;</button>
              </span>
            </div>
            <div style="white-space:pre-wrap;">${esc(c.body)}</div>
          </div>
        `).join('');
        $('dr-comments-list').querySelectorAll('.cmt-edit').forEach((b) => {
          b.addEventListener('click', async () => {
            const c = iss.comments.find((x) => x.id === b.dataset.id);
            const next = prompt('Edit comment:', c ? c.body : '');
            if (next && next.trim()) {
              await api(`/api/v1/issues/${iss.key}/comments/${b.dataset.id}`, { method: 'PATCH', body: { body: next } });
              await openIssueDrawer(iss.key);
            }
          });
        });
        $('dr-comments-list').querySelectorAll('.cmt-del').forEach((b) => {
          b.addEventListener('click', async () => {
            await api(`/api/v1/issues/${iss.key}/comments/${b.dataset.id}`, { method: 'DELETE' });
            await openIssueDrawer(iss.key);
          });
        });

        // Worklogs
        $('dr-worklog-total').textContent = `${iss.total_worklog_minutes || 0}m`;
        $('dr-worklogs-list').innerHTML = (iss.worklogs || []).map((w) => `
          <div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0;border-bottom:1px solid var(--border-subtle);">
            <span><strong>${esc(w.author)}</strong> logged <span class="mono">${w.minutes}m</span>${w.note ? ` — ${esc(w.note)}` : ''}</span>
            <button class="btn btn-sm btn-ghost wl-del" data-id="${esc(w.id)}" style="padding:0 4px;">&#10005;</button>
          </div>
        `).join('');
        $('dr-worklogs-list').querySelectorAll('.wl-del').forEach((b) => {
          b.addEventListener('click', async () => {
            await api(`/api/v1/issues/${iss.key}/worklogs/${b.dataset.id}`, { method: 'DELETE' });
            await openIssueDrawer(iss.key);
          });
        });

        // History
        $('dr-history-list').innerHTML = (iss.history || []).slice().reverse().map((h) => `
          <div style="padding:4px 0;border-bottom:1px solid var(--border-subtle);">
            <span class="mono">${esc(h.timestamp)}</span> · <strong>${esc(h.actor)}</strong> · ${esc(h.action)}
          </div>
        `).join('');

        $('issue-drawer-backdrop').classList.add('open');
      } catch (err) {
        toast(err.message, true);
      }
    }

    function closeIssueDrawer() {
      $('issue-drawer-backdrop').classList.remove('open');
      state.activeIssue = null;
    }
    $('dr-btn-close').addEventListener('click', closeIssueDrawer);
    $('issue-drawer-backdrop').addEventListener('click', (e) => {
      if (e.target === $('issue-drawer-backdrop')) closeIssueDrawer();
    });

    async function patchActiveIssue(fields) {
      if (!state.activeIssue) return;
      try {
        await api(`/api/v1/issues/${state.activeIssue.key}`, { method: 'PATCH', body: fields });
        await openIssueDrawer(state.activeIssue.key);
        await refreshCurrentView();
      } catch (err) { toast(err.message, true); }
    }

    $('dr-btn-save-text').addEventListener('click', () => {
      patchActiveIssue({ title: $('dr-title').value.trim(), description: $('dr-desc').value });
      toast('Saved issue content');
    });
    $('dr-status').addEventListener('change', async () => {
      if (!state.activeIssue) return;
      try {
        await api(`/api/v1/issues/${state.activeIssue.key}/transition`, {
          method: 'POST',
          body: { status: $('dr-status').value }
        });
        await openIssueDrawer(state.activeIssue.key);
        await refreshCurrentView();
      } catch (err) {
        toast(err.message, true);
        $('dr-status').value = state.activeIssue.status;
      }
    });
    $('dr-priority').addEventListener('change', () => patchActiveIssue({ priority: $('dr-priority').value }));
    $('dr-type').addEventListener('change', () => patchActiveIssue({ type: $('dr-type').value }));
    $('dr-assignee').addEventListener('change', () => patchActiveIssue({ assignee: $('dr-assignee').value.trim() || null }));
    $('dr-points').addEventListener('change', () => patchActiveIssue({ story_points: $('dr-points').value !== '' ? Number($('dr-points').value) : null }));
    $('dr-sprint').addEventListener('change', () => patchActiveIssue({ sprint_id: $('dr-sprint').value || null }));
    $('dr-epic').addEventListener('change', () => patchActiveIssue({ epic_id: $('dr-epic').value || null }));
    $('dr-milestone').addEventListener('change', () => patchActiveIssue({ milestone_id: $('dr-milestone').value || null }));
    $('dr-due').addEventListener('change', () => patchActiveIssue({ due_date: $('dr-due').value || null }));
    $('dr-labels').addEventListener('change', () => patchActiveIssue({ labels: $('dr-labels').value.split(',').map((s) => s.trim()).filter(Boolean) }));
    $('dr-components').addEventListener('change', () => patchActiveIssue({ components: $('dr-components').value.split(',').map((s) => s.trim()).filter(Boolean) }));

    $('dr-btn-claim').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const agent = $('ready-agent-input').value.trim() || prompt('Claim as user/agent:', 'agent-1');
      if (!agent) return;
      try {
        await api(`/api/v1/issues/${state.activeIssue.key}/claim`, { method: 'POST', body: { agent } });
        toast(`Claimed by ${agent}`);
        await openIssueDrawer(state.activeIssue.key);
        await refreshCurrentView();
      } catch (err) { toast(err.message, true); }
    });
    $('dr-btn-release').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      await api(`/api/v1/issues/${state.activeIssue.key}/release`, { method: 'POST', body: { status: 'todo' } });
      await openIssueDrawer(state.activeIssue.key);
      await refreshCurrentView();
    });
    $('dr-btn-clone').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const cloned = await api(`/api/v1/issues/${state.activeIssue.key}/clone`, { method: 'POST', body: { include_subtasks: true } });
      toast(`Cloned -> ${cloned.key}`);
      await openIssueDrawer(cloned.key);
      await refreshCurrentView();
    });
    $('dr-btn-delete').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const k = state.activeIssue.key;
      if (!confirm(`Delete ${k}?`)) return;
      await api(`/api/v1/issues/${k}`, { method: 'DELETE' });
      closeIssueDrawer();
      toast(`Deleted ${k}`);
      await refreshCurrentView();
    });

    $('dr-btn-add-subtask').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const title = $('dr-subtask-title').value.trim();
      if (!title) return;
      await api(`/api/v1/issues/${state.activeIssue.key}/subtasks`, { method: 'POST', body: { title } });
      $('dr-subtask-title').value = '';
      await openIssueDrawer(state.activeIssue.key);
      await refreshCurrentView();
    });

    $('dr-btn-add-link').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const target_key = $('dr-link-target').value.trim().toUpperCase();
      if (!target_key) return;
      try {
        await api(`/api/v1/issues/${state.activeIssue.key}/links`, {
          method: 'POST',
          body: { target_key, link_type: $('dr-link-type').value }
        });
        $('dr-link-target').value = '';
        await openIssueDrawer(state.activeIssue.key);
        await refreshCurrentView();
      } catch (err) { toast(err.message, true); }
    });

    $('dr-btn-upload-att').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const fileInput = $('attachment-file-input');
      if (!fileInput.files || !fileInput.files.length) return toast('Choose a file first', true);
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      fd.append('uploaded_by', 'web-ui');
      try {
        await api(`/api/v1/issues/${state.activeIssue.key}/attachments`, { method: 'POST', body: fd });
        fileInput.value = '';
        toast('Attachment uploaded');
        await openIssueDrawer(state.activeIssue.key);
      } catch (err) { toast(err.message, true); }
    });

    $('btn-add-comment').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const body = $('comment-body-input').value.trim();
      if (!body) return;
      await api(`/api/v1/issues/${state.activeIssue.key}/comments`, { method: 'POST', body: { body } });
      $('comment-body-input').value = '';
      await openIssueDrawer(state.activeIssue.key);
    });

    $('dr-btn-add-worklog').addEventListener('click', async () => {
      if (!state.activeIssue) return;
      const duration = $('worklog-duration-input').value.trim();
      if (!duration) return;
      try {
        await api(`/api/v1/issues/${state.activeIssue.key}/worklogs`, {
          method: 'POST',
          body: { duration, note: $('worklog-note-input').value.trim() }
        });
        $('worklog-duration-input').value = '';
        $('worklog-note-input').value = '';
        await openIssueDrawer(state.activeIssue.key);
      } catch (err) { toast(err.message, true); }
    });

    // Command Palette (Ctrl/Cmd+K) & Keyboard Shortcuts
    function openCmdPalette() {
      $('cmd-palette-modal').classList.add('open');
      $('cmd-palette-input').value = '';
      renderCmdResults('');
      setTimeout(() => $('cmd-palette-input').focus(), 20);
    }
    function closeCmdPalette() {
      $('cmd-palette-modal').classList.remove('open');
    }
    $('btn-cmd-palette').addEventListener('click', openCmdPalette);
    $('cmd-palette-modal').addEventListener('click', (e) => {
      if (e.target === $('cmd-palette-modal')) closeCmdPalette();
    });

    async function renderCmdResults(q) {
      const cmds = [
        { label: '+ Create New Issue', run: () => openCreateModal() },
        { label: 'Go to Kanban Board', run: () => switchView('board') },
        { label: 'Go to Issue List & Bulk Triage', run: () => switchView('list') },
        { label: 'Go to Ready Queue (Agent Claims)', run: () => switchView('ready') },
        { label: 'Go to Sprints & Backlog', run: () => switchView('sprints') },
        { label: 'Go to Epics & Milestones', run: () => switchView('epics') },
        { label: 'Go to Dependency DAG', run: () => switchView('graph') },
        { label: 'Go to Analytics & Settings', run: () => switchView('stats') },
        { label: 'Toggle Dark / Light Theme', run: () => $('btn-theme-toggle').click() },
        { label: 'Rebuild SQLite/FTS5 Index from Disk JSON', run: () => $('btn-reindex').click() },
      ];
      let filtered = cmds.filter((c) => !q || c.label.toLowerCase().includes(q.toLowerCase()));
      if (q && q.length >= 2) {
        try {
          const sr = await api(`/api/v1/search?q=${encodeURIComponent(q)}&limit=6`);
          (sr.issues || []).forEach((iss) => {
            filtered.unshift({
              label: `${iss.key} — ${iss.title} [${iss.status}]`,
              run: () => openIssueDrawer(iss.key),
            });
          });
        } catch (_) {}
      }
      const box = $('cmd-palette-results');
      box.innerHTML = filtered.map((item, idx) => `
        <button class="nav-btn" data-idx="${idx}">${esc(item.label)}</button>
      `).join('');
      box.querySelectorAll('.nav-btn').forEach((b) => {
        b.addEventListener('click', () => {
          closeCmdPalette();
          filtered[Number(b.dataset.idx)].run();
        });
      });
    }
    $('cmd-palette-input').addEventListener('input', (e) => renderCmdResults(e.target.value.trim()));

    document.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        openCmdPalette();
        return;
      }
      if (e.key === 'Escape') {
        closeCreateModal();
        closeCmdPalette();
        closeIssueDrawer();
        return;
      }
      const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable)) {
        return;
      }
      if (e.key === 'c') { e.preventDefault(); openCreateModal(); }
      else if (e.key === '/') { e.preventDefault(); $('global-iql-input').focus(); }
      else if (e.key === 'b') { e.preventDefault(); switchView('board'); }
      else if (e.key === 'l') { e.preventDefault(); switchView('list'); }
      else if (e.key === 'r') { e.preventDefault(); switchView('ready'); }
    });

    initTheme();
    refreshCurrentView();
  </script>
</body>
</html>
"""


# ============================================================================
# Server CLI Entrypoint & Pre-Bound Socket Runner
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="itrack-server.py",
        description="itrack Local-First File-Locked Issue Daemon, iQL Engine & Embedded Offline SPA",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Git repository root directory (defaults to ITRACK_ROOT env var or current working directory).",
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
        "--log-level",
        default="warning",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log verbosity (default: warning).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = resolve_repo_root(args.root)
    # Law 2: Enforce Git-root check BEFORE creating any directories, files, or sockets!
    enforce_law2_git_root(repo_root)

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
