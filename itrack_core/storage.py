"""Shared SQLite + FTS5 + local image asset persistence layer for iTrack."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Sequence

from itrack_core.models import (
    ConflictError,
    DEFAULT_ISSUE_TYPE,
    DEFAULT_PRIORITY,
    DEFAULT_STATUS,
    INVERSE_RELATIONSHIP_MAP,
    NotFoundError,
    SORT_FIELD_ALIASES,
    VALID_ISSUE_TYPES,
    VALID_PRIORITIES,
    VALID_RELATIONSHIP_TYPES,
    VALID_STATUSES,
    ValidationError,
    normalize_filter_bound,
    normalize_label_list,
    parse_issue_number,
    parse_positive_int,
    utc_now_iso,
    validate_enum,
    validate_hex_color,
    validate_iso_date,
    validate_label_name,
    validate_optional_text,
    validate_sort_by,
    validate_sort_order,
    validate_summary,
)
from itrack_core.richtext import (
    extract_visible_text_and_images,
    validate_mandatory_rich_text,
)

DEFAULT_REPO_ROOT: Path = Path(__file__).resolve().parent.parent

_ASSET_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}
_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def resolve_data_dir(cli_data_dir: str | Path | None = None) -> Path:
    """Resolve data directory in priority: explicit arg -> ITRACK_DATA_DIR -> <repo_root>/.itrack_data."""
    if cli_data_dir is not None and str(cli_data_dir).strip():
        return Path(cli_data_dir).expanduser().resolve()
    env_dir = os.environ.get("ITRACK_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (DEFAULT_REPO_ROOT / ".itrack_data").resolve()


def _normalize_multi_filter(
    raw: Sequence[str] | str | None,
    allowed: tuple[str, ...],
    field_name: str,
) -> list[str]:
    if raw is None:
        return []
    tokens: list[str] = []
    if isinstance(raw, str):
        tokens = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        for item in raw:
            if isinstance(item, str):
                tokens.extend(p.strip() for p in item.split(",") if p.strip())
    result: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        val = validate_enum(tok, allowed, field_name)
        if val not in seen:
            seen.add(val)
            result.append(val)
    return result


def _detect_image_mime(content: bytes, filename: str, declared_mime: str | None) -> tuple[str, str]:
    ext = Path(filename or "").suffix.lower()
    mime = (declared_mime or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(filename or "")
        if guessed:
            mime = guessed.lower()
        elif ext in _MIME_BY_EXT:
            mime = _MIME_BY_EXT[ext]
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif content.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            mime = "image/webp"
        elif content.startswith(b"BM"):
            mime = "image/bmp"
        elif b"<svg" in content[:512].lower():
            mime = "image/svg+xml"
    if not mime.startswith("image/"):
        raise ValidationError(
            f"Unsupported asset media type '{mime or 'unknown'}': only image files are allowed."
        )
    safe_ext = ext if ext in _MIME_BY_EXT else _EXT_BY_MIME.get(mime, ".bin")
    return mime, safe_ext


def _build_fts_query(query: str) -> str | None:
    raw_tokens = re.findall(r"[A-Za-z0-9_]+", query)
    if not raw_tokens:
        return None
    parts = [f'"{tok}"*' for tok in raw_tokens[:12]]
    return " ".join(parts)


class IssueStore:
    """Thread-safe, transaction-safe SQLite + FTS5 + local asset store shared by CLI and Web UI."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = resolve_data_dir(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._assets_dir = self._data_dir / "assets"
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "itrack.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
            timeout=15.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def assets_dir(self) -> Path:
        return self._assets_dir

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA busy_timeout=10000;")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO counters (name, value) VALUES ('issue_number', 0);

                CREATE TABLE IF NOT EXISTS issues (
                    number INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    description TEXT NOT NULL,
                    description_text TEXT NOT NULL,
                    description_images_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'open',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    issue_type TEXT NOT NULL DEFAULT 'task',
                    assignee TEXT,
                    reporter TEXT,
                    milestone TEXT,
                    due_date TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    cloned_from INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS labels (
                    name TEXT PRIMARY KEY,
                    color TEXT NOT NULL DEFAULT '#3b82f6',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issue_labels (
                    issue_number INTEGER NOT NULL REFERENCES issues(number) ON DELETE CASCADE,
                    label_name TEXT NOT NULL REFERENCES labels(name) ON UPDATE CASCADE ON DELETE CASCADE,
                    PRIMARY KEY (issue_number, label_name)
                );

                CREATE TABLE IF NOT EXISTS issue_relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_number INTEGER NOT NULL REFERENCES issues(number) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    target_number INTEGER NOT NULL REFERENCES issues(number) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    UNIQUE (source_number, relation_type, target_number)
                );

                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_number INTEGER NOT NULL REFERENCES issues(number) ON DELETE CASCADE,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    images_json TEXT NOT NULL DEFAULT '[]',
                    is_edited INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_number INTEGER NOT NULL REFERENCES issues(number) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    changes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    alt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
                    issue_number UNINDEXED,
                    number_text,
                    summary,
                    description_text,
                    comments_text
                );

                CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
                CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority);
                CREATE INDEX IF NOT EXISTS idx_issues_type ON issues(issue_type);
                CREATE INDEX IF NOT EXISTS idx_issues_assignee ON issues(assignee);
                CREATE INDEX IF NOT EXISTS idx_issues_archived ON issues(archived);
                CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(issue_number, id);
                CREATE INDEX IF NOT EXISTS idx_activity_issue ON activity_log(issue_number, id);
                CREATE INDEX IF NOT EXISTS idx_rel_source ON issue_relationships(source_number);
                CREATE INDEX IF NOT EXISTS idx_rel_target ON issue_relationships(target_number);
                """
            )

    def _begin_immediate(self) -> sqlite3.Cursor:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        return cur

    def _ensure_issue_exists(self, cur: sqlite3.Cursor, number: int) -> sqlite3.Row:
        row = cur.execute("SELECT * FROM issues WHERE number = ?;", (number,)).fetchone()
        if row is None:
            raise NotFoundError(f"Issue #{number} not found.")
        return row

    def _ensure_labels_exist(self, cur: sqlite3.Cursor, label_names: Sequence[str], now: str) -> None:
        for name in label_names:
            cur.execute(
                "INSERT OR IGNORE INTO labels (name, color, created_at) VALUES (?, '#3b82f6', ?);",
                (name, now),
            )

    def _set_issue_labels(self, cur: sqlite3.Cursor, issue_number: int, label_names: Sequence[str], now: str) -> None:
        self._ensure_labels_exist(cur, label_names, now)
        cur.execute("DELETE FROM issue_labels WHERE issue_number = ?;", (issue_number,))
        for name in label_names:
            cur.execute(
                "INSERT OR IGNORE INTO issue_labels (issue_number, label_name) VALUES (?, ?);",
                (issue_number, name),
            )

    def _get_issue_labels(self, cur: sqlite3.Cursor, issue_number: int) -> list[str]:
        rows = cur.execute(
            "SELECT label_name FROM issue_labels WHERE issue_number = ? ORDER BY LOWER(label_name) ASC, label_name ASC;",
            (issue_number,),
        ).fetchall()
        return [r["label_name"] for r in rows]

    def _log_activity(
        self,
        cur: sqlite3.Cursor,
        issue_number: int,
        event_type: str,
        actor: str | None,
        summary: str,
        changes: list[dict[str, Any]] | None = None,
        created_at: str | None = None,
    ) -> None:
        actor_clean = (actor or "").strip() or "anonymous"
        ts = created_at or utc_now_iso()
        cur.execute(
            """
            INSERT INTO activity_log (issue_number, event_type, actor, summary, changes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (issue_number, event_type, actor_clean, summary, json.dumps(changes or []), ts),
        )

    def _sync_fts(self, cur: sqlite3.Cursor, issue_number: int) -> None:
        issue = cur.execute(
            "SELECT number, summary, description_text FROM issues WHERE number = ?;",
            (issue_number,),
        ).fetchone()
        cur.execute("DELETE FROM issues_fts WHERE issue_number = ?;", (issue_number,))
        if issue is None:
            return
        comment_rows = cur.execute(
            "SELECT body_text FROM comments WHERE issue_number = ? ORDER BY id ASC;",
            (issue_number,),
        ).fetchall()
        comments_text = " ".join(r["body_text"] for r in comment_rows if r["body_text"])
        number_text = f"#{issue['number']} {issue['number']}"
        cur.execute(
            """
            INSERT INTO issues_fts (issue_number, number_text, summary, description_text, comments_text)
            VALUES (?, ?, ?, ?, ?);
            """,
            (issue_number, number_text, issue["summary"], issue["description_text"], comments_text),
        )

    def _collect_issue_asset_ids(self, cur: sqlite3.Cursor, issue_number: int) -> set[str]:
        ids: set[str] = set()
        row = cur.execute(
            "SELECT description_images_json FROM issues WHERE number = ?;",
            (issue_number,),
        ).fetchone()
        if row:
            for img in json.loads(row["description_images_json"] or "[]"):
                if img.get("asset_id"):
                    ids.add(str(img["asset_id"]))
        crows = cur.execute(
            "SELECT images_json FROM comments WHERE issue_number = ?;",
            (issue_number,),
        ).fetchall()
        for crow in crows:
            for img in json.loads(crow["images_json"] or "[]"):
                if img.get("asset_id"):
                    ids.add(str(img["asset_id"]))
        return ids

    def _cleanup_candidate_assets(self, cur: sqlite3.Cursor, candidate_ids: set[str]) -> list[Path]:
        if not candidate_ids:
            return []
        still_referenced: set[str] = set()
        for row in cur.execute("SELECT description_images_json FROM issues;").fetchall():
            for img in json.loads(row["description_images_json"] or "[]"):
                if img.get("asset_id"):
                    still_referenced.add(str(img["asset_id"]))
        for row in cur.execute("SELECT images_json FROM comments;").fetchall():
            for img in json.loads(row["images_json"] or "[]"):
                if img.get("asset_id"):
                    still_referenced.add(str(img["asset_id"]))
        to_delete = candidate_ids - still_referenced
        paths_to_unlink: list[Path] = []
        for aid in to_delete:
            cur.execute("DELETE FROM assets WHERE asset_id = ?;", (aid,))
            if _ASSET_ID_SAFE_RE.match(aid):
                paths_to_unlink.append(self._assets_dir / aid)
        return paths_to_unlink

    def _unlink_paths(self, paths: Sequence[Path]) -> None:
        for p in paths:
            try:
                if p.exists() and p.is_file():
                    p.unlink()
            except OSError:
                pass

    def _row_to_issue_summary(self, cur: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
        num = int(row["number"])
        labels = self._get_issue_labels(cur, num)
        comment_count = cur.execute(
            "SELECT COUNT(*) AS cnt FROM comments WHERE issue_number = ?;",
            (num,),
        ).fetchone()["cnt"]
        return {
            "number": num,
            "display_id": f"#{num}",
            "summary": row["summary"],
            "description": row["description"],
            "description_text": row["description_text"],
            "description_images": json.loads(row["description_images_json"] or "[]"),
            "status": row["status"],
            "priority": row["priority"],
            "issue_type": row["issue_type"],
            "labels": labels,
            "assignee": row["assignee"],
            "reporter": row["reporter"],
            "milestone": row["milestone"],
            "due_date": row["due_date"],
            "archived": bool(row["archived"]),
            "cloned_from": row["cloned_from"],
            "comment_count": int(comment_count),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
        }

    def _list_relationships_cur(self, cur: sqlite3.Cursor, number: int) -> list[dict[str, Any]]:
        rows = cur.execute(
            """
            SELECT r.id, r.source_number, r.relation_type, r.target_number, r.created_at,
                   s.summary AS source_summary, s.status AS source_status,
                   t.summary AS target_summary, t.status AS target_status
            FROM issue_relationships r
            JOIN issues s ON s.number = r.source_number
            JOIN issues t ON t.number = r.target_number
            WHERE r.source_number = ? OR r.target_number = ?
            ORDER BY r.id ASC;
            """,
            (number, number),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            if r["source_number"] == number:
                other_num = int(r["target_number"])
                out.append(
                    {
                        "id": int(r["id"]),
                        "source_number": number,
                        "relation_type": r["relation_type"],
                        "target_number": other_num,
                        "target_display_id": f"#{other_num}",
                        "target_summary": r["target_summary"],
                        "target_status": r["target_status"],
                        "direction": "outbound",
                        "created_at": r["created_at"],
                    }
                )
            else:
                other_num = int(r["source_number"])
                inv = INVERSE_RELATIONSHIP_MAP.get(r["relation_type"], r["relation_type"])
                out.append(
                    {
                        "id": int(r["id"]),
                        "source_number": number,
                        "relation_type": inv,
                        "target_number": other_num,
                        "target_display_id": f"#{other_num}",
                        "target_summary": r["source_summary"],
                        "target_status": r["source_status"],
                        "direction": "inbound",
                        "created_at": r["created_at"],
                    }
                )
        return out

    def _row_to_comment(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "issue_number": int(row["issue_number"]),
            "author": row["author"],
            "body": row["body"],
            "body_text": row["body_text"],
            "images": json.loads(row["images_json"] or "[]"),
            "is_edited": bool(row["is_edited"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _list_comments_cur(self, cur: sqlite3.Cursor, number: int) -> list[dict[str, Any]]:
        rows = cur.execute(
            "SELECT * FROM comments WHERE issue_number = ? ORDER BY id ASC;",
            (number,),
        ).fetchall()
        return [self._row_to_comment(r) for r in rows]

    def _get_activity_cur(self, cur: sqlite3.Cursor, number: int) -> list[dict[str, Any]]:
        rows = cur.execute(
            "SELECT * FROM activity_log WHERE issue_number = ? ORDER BY id ASC;",
            (number,),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "issue_number": int(r["issue_number"]),
                "event_type": r["event_type"],
                "actor": r["actor"],
                "summary": r["summary"],
                "changes": json.loads(r["changes_json"] or "[]"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def _build_issue_detail(self, cur: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
        num = int(row["number"])
        detail = self._row_to_issue_summary(cur, row)
        detail["relationships"] = self._list_relationships_cur(cur, num)
        detail["comments"] = self._list_comments_cur(cur, num)
        detail["activity"] = self._get_activity_cur(cur, num)
        return detail

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------
    def save_asset(
        self,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        alt: str = "",
    ) -> dict[str, Any]:
        if not content:
            raise ValidationError("Image asset content cannot be empty.")
        clean_filename = Path(filename or "image.png").name or "image.png"
        mime, safe_ext = _detect_image_mime(content, clean_filename, mime_type)
        asset_id = f"{uuid.uuid4().hex}{safe_ext}"
        sha256 = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)
        now = utc_now_iso()
        dest = self._assets_dir / asset_id
        dest.write_bytes(content)
        with self._lock:
            cur = self._begin_immediate()
            try:
                cur.execute(
                    """
                    INSERT INTO assets (asset_id, original_filename, mime_type, size_bytes, sha256, alt, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (asset_id, clean_filename, mime, size_bytes, sha256, alt or "", now),
                )
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                if dest.exists():
                    dest.unlink()
                raise
        return {
            "asset_id": asset_id,
            "url": f"/api/assets/{asset_id}",
            "original_filename": clean_filename,
            "mime_type": mime,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "alt": alt or "",
        }

    def get_asset(self, asset_id: str) -> tuple[Path, dict[str, Any]]:
        if not asset_id or not _ASSET_ID_SAFE_RE.match(asset_id):
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?;",
                (asset_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        file_path = self._assets_dir / asset_id
        if not file_path.is_file():
            raise NotFoundError(f"Asset file '{asset_id}' not found.")
        meta = {
            "asset_id": row["asset_id"],
            "url": f"/api/assets/{row['asset_id']}",
            "original_filename": row["original_filename"],
            "mime_type": row["mime_type"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "alt": row["alt"],
            "created_at": row["created_at"],
        }
        return file_path, meta

    # ------------------------------------------------------------------
    # Issue CRUD & Lifecycle
    # ------------------------------------------------------------------
    def create_issue(
        self,
        summary: str,
        description: str,
        *,
        status: str = "open",
        priority: str = "medium",
        issue_type: str = "task",
        labels: Sequence[str] | None = None,
        assignee: str | None = None,
        reporter: str | None = None,
        milestone: str | None = None,
        due_date: str | None = None,
        relationships: Sequence[dict[str, Any]] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        clean_summary = validate_summary(summary)
        desc_html, desc_text, desc_images = validate_mandatory_rich_text(
            description, field_label="Description"
        )
        clean_status = validate_enum(status or DEFAULT_STATUS, VALID_STATUSES, "status")
        clean_priority = validate_enum(priority or DEFAULT_PRIORITY, VALID_PRIORITIES, "priority")
        clean_type = validate_enum(issue_type or DEFAULT_ISSUE_TYPE, VALID_ISSUE_TYPES, "issue_type")
        clean_labels = normalize_label_list(labels)
        clean_assignee = validate_optional_text(assignee)
        clean_reporter = validate_optional_text(reporter) or validate_optional_text(actor)
        clean_milestone = validate_optional_text(milestone)
        clean_due_date = validate_iso_date(due_date, "due_date")
        actor_clean = (actor or clean_reporter or "anonymous").strip() or "anonymous"

        parsed_rels: list[tuple[str, int]] = []
        if relationships:
            seen_rels: set[tuple[str, int]] = set()
            for rel in relationships:
                if not isinstance(rel, dict):
                    raise ValidationError("Each relationship must be an object with relation_type and target_number.")
                rt = validate_enum(
                    str(rel.get("relation_type", "")),
                    VALID_RELATIONSHIP_TYPES,
                    "relation_type",
                )
                target_num = parse_issue_number(rel.get("target_number", ""))
                if (rt, target_num) not in seen_rels:
                    seen_rels.add((rt, target_num))
                    parsed_rels.append((rt, target_num))

        now = utc_now_iso()
        closed_at = now if clean_status == "closed" else None

        with self._lock:
            cur = self._begin_immediate()
            try:
                for _, target_num in parsed_rels:
                    self._ensure_issue_exists(cur, target_num)

                row_cnt = cur.execute("SELECT value FROM counters WHERE name = 'issue_number';").fetchone()
                next_num = int(row_cnt["value"]) + 1 if row_cnt else 1
                cur.execute(
                    "INSERT OR REPLACE INTO counters (name, value) VALUES ('issue_number', ?);",
                    (next_num,),
                )

                cur.execute(
                    """
                    INSERT INTO issues (
                        number, summary, description, description_text, description_images_json,
                        status, priority, issue_type, assignee, reporter, milestone, due_date,
                        archived, cloned_from, created_at, updated_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?);
                    """,
                    (
                        next_num,
                        clean_summary,
                        desc_html,
                        desc_text,
                        json.dumps(desc_images),
                        clean_status,
                        clean_priority,
                        clean_type,
                        clean_assignee,
                        clean_reporter,
                        clean_milestone,
                        clean_due_date,
                        now,
                        now,
                        closed_at,
                    ),
                )

                if clean_labels:
                    self._set_issue_labels(cur, next_num, clean_labels, now)

                self._log_activity(
                    cur,
                    next_num,
                    "issue_created",
                    actor_clean,
                    f"Issue #{next_num} created",
                    [{"field": "status", "old": None, "new": clean_status}],
                    created_at=now,
                )

                for rt, target_num in parsed_rels:
                    if target_num == next_num:
                        raise ConflictError("An issue cannot be linked to itself.")
                    cur.execute(
                        """
                        INSERT INTO issue_relationships (source_number, relation_type, target_number, created_at)
                        VALUES (?, ?, ?, ?);
                        """,
                        (next_num, rt, target_num, now),
                    )
                    self._log_activity(
                        cur,
                        next_num,
                        "relationship_added",
                        actor_clean,
                        f"Linked #{next_num} {rt} #{target_num}",
                        [{"field": "relationship", "old": None, "new": f"{rt} #{target_num}"}],
                        created_at=now,
                    )
                    inv = INVERSE_RELATIONSHIP_MAP.get(rt, rt)
                    self._log_activity(
                        cur,
                        target_num,
                        "relationship_added",
                        actor_clean,
                        f"Linked #{target_num} {inv} #{next_num}",
                        [{"field": "relationship", "old": None, "new": f"{inv} #{next_num}"}],
                        created_at=now,
                    )

                self._sync_fts(cur, next_num)
                issue_row = self._ensure_issue_exists(cur, next_num)
                result = self._build_issue_detail(cur, issue_row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def get_issue(self, number: int | str) -> dict[str, Any]:
        num = parse_issue_number(number)
        with self._lock:
            cur = self._conn.cursor()
            row = self._ensure_issue_exists(cur, num)
            return self._build_issue_detail(cur, row)

    def list_issues(
        self,
        *,
        page: int = 1,
        per_page: int = 10,
        status: Sequence[str] | str | None = None,
        priority: Sequence[str] | str | None = None,
        issue_type: Sequence[str] | str | None = None,
        labels: Sequence[str] | str | None = None,
        assignee: str | None = None,
        reporter: str | None = None,
        milestone: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
        search: str | None = None,
        sort_by: str = "number",
        sort_order: str = "asc",
        include_archived: bool = False,
        archived_only: bool = False,
    ) -> dict[str, Any]:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValidationError("page must be an integer >= 1.")
        if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 1 or per_page > 500:
            raise ValidationError("per_page must be an integer between 1 and 500.")

        statuses = _normalize_multi_filter(status, VALID_STATUSES, "status")
        priorities = _normalize_multi_filter(priority, VALID_PRIORITIES, "priority")
        issue_types = _normalize_multi_filter(issue_type, VALID_ISSUE_TYPES, "issue_type")
        filter_labels = normalize_label_list(labels)
        clean_assignee = validate_optional_text(assignee)
        clean_reporter = validate_optional_text(reporter)
        clean_milestone = validate_optional_text(milestone)
        c_from = normalize_filter_bound(created_from, is_upper=False, field_name="created_from")
        c_to = normalize_filter_bound(created_to, is_upper=True, field_name="created_to")
        u_from = normalize_filter_bound(updated_from, is_upper=False, field_name="updated_from")
        u_to = normalize_filter_bound(updated_to, is_upper=True, field_name="updated_to")
        clean_sort_by = validate_sort_by(sort_by)
        clean_sort_order = validate_sort_order(sort_order)

        where_clauses: list[str] = []
        params: list[Any] = []

        if archived_only:
            where_clauses.append("i.archived = 1")
        elif not include_archived:
            where_clauses.append("i.archived = 0")

        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            where_clauses.append(f"i.status IN ({placeholders})")
            params.extend(statuses)

        if priorities:
            placeholders = ", ".join("?" for _ in priorities)
            where_clauses.append(f"i.priority IN ({placeholders})")
            params.extend(priorities)

        if issue_types:
            placeholders = ", ".join("?" for _ in issue_types)
            where_clauses.append(f"i.issue_type IN ({placeholders})")
            params.extend(issue_types)

        for lbl in filter_labels:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_number = i.number AND il.label_name = ?)"
            )
            params.append(lbl)

        if clean_assignee is not None:
            where_clauses.append("i.assignee = ?")
            params.append(clean_assignee)

        if clean_reporter is not None:
            where_clauses.append("i.reporter = ?")
            params.append(clean_reporter)

        if clean_milestone is not None:
            where_clauses.append("i.milestone = ?")
            params.append(clean_milestone)

        if c_from is not None:
            where_clauses.append("i.created_at >= ?")
            params.append(c_from)
        if c_to is not None:
            where_clauses.append("i.created_at <= ?")
            params.append(c_to)
        if u_from is not None:
            where_clauses.append("i.updated_at >= ?")
            params.append(u_from)
        if u_to is not None:
            where_clauses.append("i.updated_at <= ?")
            params.append(u_to)

        clean_search = (search or "").strip()
        if clean_search:
            sub_clauses: list[str] = []
            fts_q = _build_fts_query(clean_search)
            if fts_q:
                sub_clauses.append(
                    "i.number IN (SELECT issue_number FROM issues_fts WHERE issues_fts MATCH ?)"
                )
                params.append(fts_q)

            # Fallback per-word substring match across number, summary, description_text, and comments
            words = [w for w in clean_search.split() if w]
            if words:
                word_ands: list[str] = []
                for w in words:
                    like_pat = f"%{w}%"
                    w_clean_num = w[1:] if w.startswith("#") else w
                    if w_clean_num.isdigit():
                        word_ands.append(
                            "(i.number = ? OR i.summary LIKE ? OR i.description_text LIKE ? OR "
                            "EXISTS (SELECT 1 FROM comments c WHERE c.issue_number = i.number AND c.body_text LIKE ?))"
                        )
                        params.extend([int(w_clean_num), like_pat, like_pat, like_pat])
                    else:
                        word_ands.append(
                            "(i.summary LIKE ? OR i.description_text LIKE ? OR "
                            "EXISTS (SELECT 1 FROM comments c WHERE c.issue_number = i.number AND c.body_text LIKE ?))"
                        )
                        params.extend([like_pat, like_pat, like_pat])
                sub_clauses.append("(" + " AND ".join(word_ands) + ")")

            if sub_clauses:
                where_clauses.append("(" + " OR ".join(sub_clauses) + ")")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        direction = "ASC" if clean_sort_order == "asc" else "DESC"
        if clean_sort_by == "number":
            order_sql = f"i.number {direction}"
        elif clean_sort_by == "created_at":
            order_sql = f"i.created_at {direction}, i.number ASC"
        elif clean_sort_by == "updated_at":
            order_sql = f"i.updated_at {direction}, i.number ASC"
        elif clean_sort_by == "priority":
            order_sql = (
                f"CASE i.priority WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 3 WHEN 'critical' THEN 4 ELSE 99 END {direction}, "
                "i.number ASC"
            )
        elif clean_sort_by == "status":
            order_sql = (
                f"CASE i.status WHEN 'open' THEN 1 WHEN 'in_progress' THEN 2 WHEN 'blocked' THEN 3 WHEN 'resolved' THEN 4 WHEN 'closed' THEN 5 ELSE 99 END {direction}, "
                "i.number ASC"
            )
        else:  # summary
            order_sql = f"LOWER(i.summary) {direction}, i.summary {direction}, i.number ASC"

        with self._lock:
            cur = self._conn.cursor()
            total = int(
                cur.execute(
                    f"SELECT COUNT(*) AS cnt FROM issues i {where_sql};",
                    params,
                ).fetchone()["cnt"]
            )
            total_pages = max(1, (total + per_page - 1) // per_page)
            offset = (page - 1) * per_page
            rows = cur.execute(
                f"SELECT i.* FROM issues i {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?;",
                [*params, per_page, offset],
            ).fetchall()
            items = [self._row_to_issue_summary(cur, r) for r in rows]

        has_prev = page > 1
        has_next = page < total_pages
        return {
            "items": items,
            "issues": list(items),
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "has_prev": has_prev,
            "has_next": has_next,
            "first_page": 1,
            "prev_page": (page - 1) if has_prev else None,
            "next_page": (page + 1) if has_next else None,
            "last_page": total_pages,
        }

    def update_issue(
        self,
        number: int | str,
        *,
        summary: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        issue_type: str | None = None,
        assignee: str | None = ...,
        reporter: str | None = None,
        milestone: str | None = ...,
        due_date: str | None = ...,
        set_labels: Sequence[str] | None = None,
        add_labels: Sequence[str] | None = None,
        remove_labels: Sequence[str] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"

        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = self._ensure_issue_exists(cur, num)
                now = utc_now_iso()
                changes: list[dict[str, Any]] = []
                updates: dict[str, Any] = {}

                if summary is not None:
                    new_summary = validate_summary(summary)
                    if new_summary != existing["summary"]:
                        changes.append({"field": "summary", "old": existing["summary"], "new": new_summary})
                        updates["summary"] = new_summary

                if description is not None:
                    desc_html, desc_text, desc_images = validate_mandatory_rich_text(
                        description, field_label="Description"
                    )
                    if desc_html != existing["description"]:
                        changes.append(
                            {
                                "field": "description",
                                "old": existing["description_text"],
                                "new": desc_text,
                            }
                        )
                        updates["description"] = desc_html
                        updates["description_text"] = desc_text
                        updates["description_images_json"] = json.dumps(desc_images)

                if status is not None:
                    new_status = validate_enum(status, VALID_STATUSES, "status")
                    if new_status != existing["status"]:
                        changes.append({"field": "status", "old": existing["status"], "new": new_status})
                        updates["status"] = new_status
                        if new_status == "closed":
                            updates["closed_at"] = now
                        elif existing["closed_at"] is not None:
                            updates["closed_at"] = None

                if priority is not None:
                    new_priority = validate_enum(priority, VALID_PRIORITIES, "priority")
                    if new_priority != existing["priority"]:
                        changes.append({"field": "priority", "old": existing["priority"], "new": new_priority})
                        updates["priority"] = new_priority

                if issue_type is not None:
                    new_type = validate_enum(issue_type, VALID_ISSUE_TYPES, "issue_type")
                    if new_type != existing["issue_type"]:
                        changes.append({"field": "issue_type", "old": existing["issue_type"], "new": new_type})
                        updates["issue_type"] = new_type

                if assignee is not ...:
                    new_assignee = validate_optional_text(assignee)
                    if new_assignee != existing["assignee"]:
                        changes.append({"field": "assignee", "old": existing["assignee"], "new": new_assignee})
                        updates["assignee"] = new_assignee

                if reporter is not None:
                    new_reporter = validate_optional_text(reporter)
                    if new_reporter != existing["reporter"]:
                        changes.append({"field": "reporter", "old": existing["reporter"], "new": new_reporter})
                        updates["reporter"] = new_reporter

                if milestone is not ...:
                    new_milestone = validate_optional_text(milestone)
                    if new_milestone != existing["milestone"]:
                        changes.append({"field": "milestone", "old": existing["milestone"], "new": new_milestone})
                        updates["milestone"] = new_milestone

                if due_date is not ...:
                    new_due_date = validate_iso_date(due_date, "due_date")
                    if new_due_date != existing["due_date"]:
                        changes.append({"field": "due_date", "old": existing["due_date"], "new": new_due_date})
                        updates["due_date"] = new_due_date

                old_labels = self._get_issue_labels(cur, num)
                new_labels: list[str] | None = None
                if set_labels is not None or add_labels is not None or remove_labels is not None:
                    if set_labels is not None:
                        working = normalize_label_list(set_labels)
                    else:
                        working = list(old_labels)
                    if add_labels:
                        for lbl in normalize_label_list(add_labels):
                            if lbl not in working:
                                working.append(lbl)
                    if remove_labels:
                        to_remove = set(normalize_label_list(remove_labels))
                        working = [lbl for lbl in working if lbl not in to_remove]
                    if working != old_labels:
                        new_labels = working
                        changes.append({"field": "labels", "old": old_labels, "new": new_labels})

                if updates or new_labels is not None:
                    updates["updated_at"] = now
                    set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
                    cur.execute(
                        f"UPDATE issues SET {set_clause} WHERE number = ?;",
                        [*updates.values(), num],
                    )
                    if new_labels is not None:
                        self._set_issue_labels(cur, num, new_labels, now)
                    changed_fields = ", ".join(c["field"] for c in changes)
                    self._log_activity(
                        cur,
                        num,
                        "issue_updated",
                        actor_clean,
                        f"Updated {changed_fields}",
                        changes,
                        created_at=now,
                    )
                    self._sync_fts(cur, num)

                row = self._ensure_issue_exists(cur, num)
                result = self._build_issue_detail(cur, row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def close_issue(self, number: int | str, *, actor: str | None = None) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = self._ensure_issue_exists(cur, num)
                now = utc_now_iso()
                prev_status = existing["status"]
                if prev_status != "closed" or existing["closed_at"] is None:
                    closed_at = existing["closed_at"] or now
                    cur.execute(
                        "UPDATE issues SET status = 'closed', closed_at = ?, updated_at = ? WHERE number = ?;",
                        (closed_at, now, num),
                    )
                    self._log_activity(
                        cur,
                        num,
                        "issue_closed",
                        actor_clean,
                        f"Issue #{num} closed",
                        [{"field": "status", "old": prev_status, "new": "closed"}],
                        created_at=now,
                    )
                row = self._ensure_issue_exists(cur, num)
                result = self._build_issue_detail(cur, row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def reopen_issue(self, number: int | str, *, actor: str | None = None) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = self._ensure_issue_exists(cur, num)
                now = utc_now_iso()
                prev_status = existing["status"]
                if prev_status != "open" or existing["closed_at"] is not None:
                    cur.execute(
                        "UPDATE issues SET status = 'open', closed_at = NULL, updated_at = ? WHERE number = ?;",
                        (now, num),
                    )
                    self._log_activity(
                        cur,
                        num,
                        "issue_reopened",
                        actor_clean,
                        f"Issue #{num} reopened",
                        [{"field": "status", "old": prev_status, "new": "open"}],
                        created_at=now,
                    )
                row = self._ensure_issue_exists(cur, num)
                result = self._build_issue_detail(cur, row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def clone_issue(
        self,
        number: int | str,
        *,
        summary: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                src = self._ensure_issue_exists(cur, num)
                new_summary = (
                    validate_summary(summary)
                    if summary is not None
                    else validate_summary(f"Clone of #{num}: {src['summary']}")
                )
                src_labels = self._get_issue_labels(cur, num)
                row_cnt = cur.execute("SELECT value FROM counters WHERE name = 'issue_number';").fetchone()
                new_num = int(row_cnt["value"]) + 1 if row_cnt else 1
                cur.execute(
                    "INSERT OR REPLACE INTO counters (name, value) VALUES ('issue_number', ?);",
                    (new_num,),
                )
                now = utc_now_iso()
                cur.execute(
                    """
                    INSERT INTO issues (
                        number, summary, description, description_text, description_images_json,
                        status, priority, issue_type, assignee, reporter, milestone, due_date,
                        archived, cloned_from, created_at, updated_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL);
                    """,
                    (
                        new_num,
                        new_summary,
                        src["description"],
                        src["description_text"],
                        src["description_images_json"],
                        src["priority"],
                        src["issue_type"],
                        src["assignee"],
                        (actor or src["reporter"] or "anonymous"),
                        src["milestone"],
                        src["due_date"],
                        num,
                        now,
                        now,
                    ),
                )
                if src_labels:
                    self._set_issue_labels(cur, new_num, src_labels, now)
                self._log_activity(
                    cur,
                    new_num,
                    "issue_cloned",
                    actor_clean,
                    f"Issue #{new_num} cloned from #{num}",
                    [{"field": "cloned_from", "old": None, "new": num}],
                    created_at=now,
                )
                self._sync_fts(cur, new_num)
                row = self._ensure_issue_exists(cur, new_num)
                result = self._build_issue_detail(cur, row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def archive_issue(self, number: int | str, *, actor: str | None = None) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = self._ensure_issue_exists(cur, num)
                now = utc_now_iso()
                if not existing["archived"]:
                    cur.execute(
                        "UPDATE issues SET archived = 1, updated_at = ? WHERE number = ?;",
                        (now, num),
                    )
                    self._log_activity(
                        cur,
                        num,
                        "issue_archived",
                        actor_clean,
                        f"Issue #{num} archived",
                        [{"field": "archived", "old": False, "new": True}],
                        created_at=now,
                    )
                row = self._ensure_issue_exists(cur, num)
                result = self._build_issue_detail(cur, row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def restore_issue(self, number: int | str, *, actor: str | None = None) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = self._ensure_issue_exists(cur, num)
                now = utc_now_iso()
                if existing["archived"]:
                    cur.execute(
                        "UPDATE issues SET archived = 0, updated_at = ? WHERE number = ?;",
                        (now, num),
                    )
                    self._log_activity(
                        cur,
                        num,
                        "issue_restored",
                        actor_clean,
                        f"Issue #{num} restored",
                        [{"field": "archived", "old": True, "new": False}],
                        created_at=now,
                    )
                row = self._ensure_issue_exists(cur, num)
                result = self._build_issue_detail(cur, row)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def delete_issue(self, number: int | str, *, actor: str | None = None) -> dict[str, Any]:
        num = parse_issue_number(number)
        paths_to_unlink: list[Path] = []
        with self._lock:
            cur = self._begin_immediate()
            try:
                self._ensure_issue_exists(cur, num)
                candidate_assets = self._collect_issue_asset_ids(cur, num)
                cur.execute("DELETE FROM issues_fts WHERE issue_number = ?;", (num,))
                cur.execute("DELETE FROM issues WHERE number = ?;", (num,))
                paths_to_unlink = self._cleanup_candidate_assets(cur, candidate_assets)
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
        self._unlink_paths(paths_to_unlink)
        return {"deleted": True, "number": num}

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    def add_relationship(
        self,
        number: int | str,
        relation_type: str,
        target_number: int | str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        num = parse_issue_number(number)
        target_num = parse_issue_number(target_number)
        rt = validate_enum(relation_type, VALID_RELATIONSHIP_TYPES, "relation_type")
        if num == target_num:
            raise ConflictError("An issue cannot have a relationship with itself.")
        actor_clean = (actor or "").strip() or "anonymous"

        with self._lock:
            cur = self._begin_immediate()
            try:
                self._ensure_issue_exists(cur, num)
                target_row = self._ensure_issue_exists(cur, target_num)

                dup = cur.execute(
                    """
                    SELECT id FROM issue_relationships
                    WHERE source_number = ? AND relation_type = ? AND target_number = ?;
                    """,
                    (num, rt, target_num),
                ).fetchone()
                if dup is not None:
                    raise ConflictError(f"Relationship #{num} {rt} #{target_num} already exists.")

                inv_rt = INVERSE_RELATIONSHIP_MAP.get(rt)
                if inv_rt:
                    inv_dup = cur.execute(
                        """
                        SELECT id FROM issue_relationships
                        WHERE source_number = ? AND relation_type = ? AND target_number = ?;
                        """,
                        (target_num, inv_rt, num),
                    ).fetchone()
                    if inv_dup is not None:
                        raise ConflictError(f"Equivalent relationship between #{num} and #{target_num} already exists.")

                now = utc_now_iso()
                cur.execute(
                    """
                    INSERT INTO issue_relationships (source_number, relation_type, target_number, created_at)
                    VALUES (?, ?, ?, ?);
                    """,
                    (num, rt, target_num, now),
                )
                rel_id = int(cur.lastrowid)
                cur.execute("UPDATE issues SET updated_at = ? WHERE number IN (?, ?);", (now, num, target_num))
                self._log_activity(
                    cur,
                    num,
                    "relationship_added",
                    actor_clean,
                    f"Linked #{num} {rt} #{target_num}",
                    [{"field": "relationship", "old": None, "new": f"{rt} #{target_num}"}],
                    created_at=now,
                )
                self._log_activity(
                    cur,
                    target_num,
                    "relationship_added",
                    actor_clean,
                    f"Linked #{target_num} {inv_rt or rt} #{num}",
                    [{"field": "relationship", "old": None, "new": f"{inv_rt or rt} #{num}"}],
                    created_at=now,
                )
                cur.execute("COMMIT;")
                return {
                    "id": rel_id,
                    "source_number": num,
                    "relation_type": rt,
                    "target_number": target_num,
                    "target_display_id": f"#{target_num}",
                    "target_summary": target_row["summary"],
                    "target_status": target_row["status"],
                    "direction": "outbound",
                    "created_at": now,
                }
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def remove_relationship(
        self,
        number: int | str,
        relationship_id: int | None = None,
        *,
        relation_type: str | None = None,
        target_number: int | str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        num = parse_issue_number(number)
        actor_clean = (actor or "").strip() or "anonymous"

        with self._lock:
            cur = self._begin_immediate()
            try:
                self._ensure_issue_exists(cur, num)
                row: sqlite3.Row | None = None
                if relationship_id is not None:
                    rid = parse_positive_int(relationship_id, "relationship_id")
                    row = cur.execute(
                        """
                        SELECT * FROM issue_relationships
                        WHERE id = ? AND (source_number = ? OR target_number = ?);
                        """,
                        (rid, num, num),
                    ).fetchone()
                elif relation_type is not None and target_number is not None:
                    rt = validate_enum(relation_type, VALID_RELATIONSHIP_TYPES, "relation_type")
                    t_num = parse_issue_number(target_number)
                    inv_rt = INVERSE_RELATIONSHIP_MAP.get(rt, rt)
                    row = cur.execute(
                        """
                        SELECT * FROM issue_relationships
                        WHERE (source_number = ? AND relation_type = ? AND target_number = ?)
                           OR (source_number = ? AND relation_type = ? AND target_number = ?);
                        """,
                        (num, rt, t_num, t_num, inv_rt, num),
                    ).fetchone()
                else:
                    raise ValidationError("Provide relationship_id or both relation_type and target_number.")

                if row is None:
                    raise NotFoundError("Relationship not found.")

                rel_id = int(row["id"])
                src_num = int(row["source_number"])
                tgt_num = int(row["target_number"])
                rt_val = row["relation_type"]
                inv_val = INVERSE_RELATIONSHIP_MAP.get(rt_val, rt_val)
                now = utc_now_iso()

                cur.execute("DELETE FROM issue_relationships WHERE id = ?;", (rel_id,))
                cur.execute("UPDATE issues SET updated_at = ? WHERE number IN (?, ?);", (now, src_num, tgt_num))
                self._log_activity(
                    cur,
                    src_num,
                    "relationship_removed",
                    actor_clean,
                    f"Removed link #{src_num} {rt_val} #{tgt_num}",
                    [{"field": "relationship", "old": f"{rt_val} #{tgt_num}", "new": None}],
                    created_at=now,
                )
                self._log_activity(
                    cur,
                    tgt_num,
                    "relationship_removed",
                    actor_clean,
                    f"Removed link #{tgt_num} {inv_val} #{src_num}",
                    [{"field": "relationship", "old": f"{inv_val} #{src_num}", "new": None}],
                    created_at=now,
                )
                cur.execute("COMMIT;")
                return {"deleted": True, "id": rel_id}
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def list_relationships(self, number: int | str) -> list[dict[str, Any]]:
        num = parse_issue_number(number)
        with self._lock:
            cur = self._conn.cursor()
            self._ensure_issue_exists(cur, num)
            return self._list_relationships_cur(cur, num)

    # ------------------------------------------------------------------
    # Labels Management
    # ------------------------------------------------------------------
    def list_labels(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT l.name, l.color, l.created_at, COUNT(il.issue_number) AS issue_count
                FROM labels l
                LEFT JOIN issue_labels il ON il.label_name = l.name
                GROUP BY l.name
                ORDER BY LOWER(l.name) ASC, l.name ASC;
                """
            ).fetchall()
            return [
                {
                    "name": r["name"],
                    "color": r["color"],
                    "issue_count": int(r["issue_count"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def create_label(self, name: str, color: str = "#3b82f6") -> dict[str, Any]:
        clean_name = validate_label_name(name)
        clean_color = validate_hex_color(color)
        now = utc_now_iso()
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = cur.execute(
                    "SELECT name FROM labels WHERE name = ?;",
                    (clean_name,),
                ).fetchone()
                if existing is not None:
                    raise ConflictError(f"Label '{clean_name}' already exists.")
                cur.execute(
                    "INSERT INTO labels (name, color, created_at) VALUES (?, ?, ?);",
                    (clean_name, clean_color, now),
                )
                cur.execute("COMMIT;")
                return {
                    "name": clean_name,
                    "color": clean_color,
                    "issue_count": 0,
                    "created_at": now,
                }
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def rename_label(
        self,
        old_name: str,
        new_name: str,
        color: str | None = None,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        clean_old = validate_label_name(old_name)
        clean_new = validate_label_name(new_name)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = cur.execute("SELECT * FROM labels WHERE name = ?;", (clean_old,)).fetchone()
                if existing is None:
                    raise NotFoundError(f"Label '{clean_old}' not found.")
                if clean_new != clean_old:
                    dup = cur.execute("SELECT name FROM labels WHERE name = ?;", (clean_new,)).fetchone()
                    if dup is not None:
                        raise ConflictError(f"Label '{clean_new}' already exists.")
                new_color = validate_hex_color(color, default=existing["color"]) if color is not None else existing["color"]
                affected = cur.execute(
                    "SELECT issue_number FROM issue_labels WHERE label_name = ?;",
                    (clean_old,),
                ).fetchall()
                cur.execute(
                    "UPDATE labels SET name = ?, color = ? WHERE name = ?;",
                    (clean_new, new_color, clean_old),
                )
                now = utc_now_iso()
                if clean_new != clean_old:
                    for row in affected:
                        inum = int(row["issue_number"])
                        cur.execute("UPDATE issues SET updated_at = ? WHERE number = ?;", (now, inum))
                        self._log_activity(
                            cur,
                            inum,
                            "label_renamed",
                            actor_clean,
                            f"Label '{clean_old}' renamed to '{clean_new}'",
                            [{"field": "labels", "old": clean_old, "new": clean_new}],
                            created_at=now,
                        )
                cnt = int(
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM issue_labels WHERE label_name = ?;",
                        (clean_new,),
                    ).fetchone()["cnt"]
                )
                cur.execute("COMMIT;")
                return {
                    "name": clean_new,
                    "color": new_color,
                    "issue_count": cnt,
                    "created_at": existing["created_at"],
                }
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def delete_label(self, name: str, *, actor: str | None = None) -> dict[str, Any]:
        clean_name = validate_label_name(name)
        actor_clean = (actor or "").strip() or "anonymous"
        with self._lock:
            cur = self._begin_immediate()
            try:
                existing = cur.execute("SELECT name FROM labels WHERE name = ?;", (clean_name,)).fetchone()
                if existing is None:
                    raise NotFoundError(f"Label '{clean_name}' not found.")
                affected = cur.execute(
                    "SELECT issue_number FROM issue_labels WHERE label_name = ?;",
                    (clean_name,),
                ).fetchall()
                now = utc_now_iso()
                for row in affected:
                    inum = int(row["issue_number"])
                    cur.execute("UPDATE issues SET updated_at = ? WHERE number = ?;", (now, inum))
                    self._log_activity(
                        cur,
                        inum,
                        "label_deleted",
                        actor_clean,
                        f"Label '{clean_name}' removed globally",
                        [{"field": "labels", "old": clean_name, "new": None}],
                        created_at=now,
                    )
                cur.execute("DELETE FROM labels WHERE name = ?;", (clean_name,))
                cur.execute("COMMIT;")
                return {"deleted": True, "name": clean_name}
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    # ------------------------------------------------------------------
    # Bulk Operations
    # ------------------------------------------------------------------
    def bulk_update(
        self,
        issue_numbers: Sequence[int | str],
        *,
        status: str | None = None,
        priority: str | None = None,
        assignee: str | None = ...,
        add_labels: Sequence[str] | None = None,
        remove_labels: Sequence[str] | None = None,
        set_labels: Sequence[str] | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if not issue_numbers:
            raise ValidationError("At least one issue number must be specified for bulk update.")
        nums: list[int] = []
        seen: set[int] = set()
        for raw in issue_numbers:
            num = parse_issue_number(raw)
            if num not in seen:
                seen.add(num)
                nums.append(num)

        has_change = any(
            [
                status is not None,
                priority is not None,
                assignee is not ...,
                add_labels is not None,
                remove_labels is not None,
                set_labels is not None,
            ]
        )
        if not has_change:
            raise ValidationError("Specify at least one field to bulk update.")

        clean_status = validate_enum(status, VALID_STATUSES, "status") if status is not None else None
        clean_priority = validate_enum(priority, VALID_PRIORITIES, "priority") if priority is not None else None
        clean_assignee = validate_optional_text(assignee) if assignee is not ... else ...
        norm_set = normalize_label_list(set_labels) if set_labels is not None else None
        norm_add = normalize_label_list(add_labels) if add_labels is not None else []
        norm_rem = set(normalize_label_list(remove_labels)) if remove_labels is not None else set()
        actor_clean = (actor or "").strip() or "anonymous"

        with self._lock:
            cur = self._begin_immediate()
            try:
                now = utc_now_iso()
                for num in nums:
                    existing = self._ensure_issue_exists(cur, num)
                    changes: list[dict[str, Any]] = []
                    updates: dict[str, Any] = {}

                    if clean_status is not None and clean_status != existing["status"]:
                        changes.append({"field": "status", "old": existing["status"], "new": clean_status})
                        updates["status"] = clean_status
                        if clean_status == "closed":
                            updates["closed_at"] = now
                        elif existing["closed_at"] is not None:
                            updates["closed_at"] = None

                    if clean_priority is not None and clean_priority != existing["priority"]:
                        changes.append({"field": "priority", "old": existing["priority"], "new": clean_priority})
                        updates["priority"] = clean_priority

                    if clean_assignee is not ... and clean_assignee != existing["assignee"]:
                        changes.append({"field": "assignee", "old": existing["assignee"], "new": clean_assignee})
                        updates["assignee"] = clean_assignee

                    old_labels = self._get_issue_labels(cur, num)
                    new_labels: list[str] | None = None
                    if norm_set is not None or norm_add or norm_rem:
                        working = list(norm_set) if norm_set is not None else list(old_labels)
                        for lbl in norm_add:
                            if lbl not in working:
                                working.append(lbl)
                        if norm_rem:
                            working = [lbl for lbl in working if lbl not in norm_rem]
                        if working != old_labels:
                            new_labels = working
                            changes.append({"field": "labels", "old": old_labels, "new": new_labels})

                    if updates or new_labels is not None:
                        updates["updated_at"] = now
                        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
                        cur.execute(
                            f"UPDATE issues SET {set_clause} WHERE number = ?;",
                            [*updates.values(), num],
                        )
                        if new_labels is not None:
                            self._set_issue_labels(cur, num, new_labels, now)
                        self._log_activity(
                            cur,
                            num,
                            "bulk_updated",
                            actor_clean,
                            f"Bulk updated {', '.join(c['field'] for c in changes)}",
                            changes,
                            created_at=now,
                        )
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise

        return {"action": "update", "affected_count": len(nums), "issue_numbers": nums}

    def bulk_delete(self, issue_numbers: Sequence[int | str], *, actor: str | None = None) -> dict[str, Any]:
        if not issue_numbers:
            raise ValidationError("At least one issue number must be specified for bulk delete.")
        nums: list[int] = []
        seen: set[int] = set()
        for raw in issue_numbers:
            num = parse_issue_number(raw)
            if num not in seen:
                seen.add(num)
                nums.append(num)

        paths_to_unlink: list[Path] = []
        with self._lock:
            cur = self._begin_immediate()
            try:
                candidate_assets: set[str] = set()
                for num in nums:
                    self._ensure_issue_exists(cur, num)
                    candidate_assets |= self._collect_issue_asset_ids(cur, num)
                for num in nums:
                    cur.execute("DELETE FROM issues_fts WHERE issue_number = ?;", (num,))
                    cur.execute("DELETE FROM issues WHERE number = ?;", (num,))
                paths_to_unlink = self._cleanup_candidate_assets(cur, candidate_assets)
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
        self._unlink_paths(paths_to_unlink)
        return {"action": "delete", "affected_count": len(nums), "issue_numbers": nums}

    # ------------------------------------------------------------------
    # Rich-Text Comments
    # ------------------------------------------------------------------
    def add_comment(self, issue_number: int | str, body: str, *, author: str | None = None) -> dict[str, Any]:
        num = parse_issue_number(issue_number)
        body_html, body_text, images = validate_mandatory_rich_text(body, field_label="Comment")
        author_clean = (author or "").strip() or "anonymous"
        now = utc_now_iso()
        with self._lock:
            cur = self._begin_immediate()
            try:
                self._ensure_issue_exists(cur, num)
                cur.execute(
                    """
                    INSERT INTO comments (issue_number, author, body, body_text, images_json, is_edited, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?);
                    """,
                    (num, author_clean, body_html, body_text, json.dumps(images), now, now),
                )
                cid = int(cur.lastrowid)
                cur.execute("UPDATE issues SET updated_at = ? WHERE number = ?;", (now, num))
                self._log_activity(
                    cur,
                    num,
                    "comment_added",
                    author_clean,
                    f"Comment #{cid} added by {author_clean}",
                    [{"field": "comment", "old": None, "new": cid}],
                    created_at=now,
                )
                self._sync_fts(cur, num)
                crow = cur.execute("SELECT * FROM comments WHERE id = ?;", (cid,)).fetchone()
                result = self._row_to_comment(crow)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        num = parse_issue_number(issue_number)
        with self._lock:
            cur = self._conn.cursor()
            self._ensure_issue_exists(cur, num)
            return self._list_comments_cur(cur, num)

    def get_comment(self, issue_number: int | str, comment_id: int | str) -> dict[str, Any]:
        num = parse_issue_number(issue_number)
        cid = parse_positive_int(comment_id, "comment_id")
        with self._lock:
            cur = self._conn.cursor()
            self._ensure_issue_exists(cur, num)
            crow = cur.execute(
                "SELECT * FROM comments WHERE id = ? AND issue_number = ?;",
                (cid, num),
            ).fetchone()
            if crow is None:
                raise NotFoundError(f"Comment #{cid} on issue #{num} not found.")
            return self._row_to_comment(crow)

    def update_comment(
        self,
        issue_number: int | str,
        comment_id: int | str,
        body: str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        num = parse_issue_number(issue_number)
        cid = parse_positive_int(comment_id, "comment_id")
        body_html, body_text, images = validate_mandatory_rich_text(body, field_label="Comment")
        actor_clean = (actor or "").strip() or "anonymous"
        now = utc_now_iso()
        with self._lock:
            cur = self._begin_immediate()
            try:
                self._ensure_issue_exists(cur, num)
                existing = cur.execute(
                    "SELECT * FROM comments WHERE id = ? AND issue_number = ?;",
                    (cid, num),
                ).fetchone()
                if existing is None:
                    raise NotFoundError(f"Comment #{cid} on issue #{num} not found.")
                cur.execute(
                    """
                    UPDATE comments
                    SET body = ?, body_text = ?, images_json = ?, is_edited = 1, updated_at = ?
                    WHERE id = ? AND issue_number = ?;
                    """,
                    (body_html, body_text, json.dumps(images), now, cid, num),
                )
                cur.execute("UPDATE issues SET updated_at = ? WHERE number = ?;", (now, num))
                self._log_activity(
                    cur,
                    num,
                    "comment_edited",
                    actor_clean,
                    f"Comment #{cid} edited",
                    [{"field": "comment", "old": existing["body_text"], "new": body_text}],
                    created_at=now,
                )
                self._sync_fts(cur, num)
                crow = cur.execute("SELECT * FROM comments WHERE id = ?;", (cid,)).fetchone()
                result = self._row_to_comment(crow)
                cur.execute("COMMIT;")
                return result
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def delete_comment(
        self,
        issue_number: int | str,
        comment_id: int | str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        num = parse_issue_number(issue_number)
        cid = parse_positive_int(comment_id, "comment_id")
        actor_clean = (actor or "").strip() or "anonymous"
        now = utc_now_iso()
        paths_to_unlink: list[Path] = []
        with self._lock:
            cur = self._begin_immediate()
            try:
                self._ensure_issue_exists(cur, num)
                existing = cur.execute(
                    "SELECT * FROM comments WHERE id = ? AND issue_number = ?;",
                    (cid, num),
                ).fetchone()
                if existing is None:
                    raise NotFoundError(f"Comment #{cid} on issue #{num} not found.")
                candidate_assets: set[str] = set()
                for img in json.loads(existing["images_json"] or "[]"):
                    if img.get("asset_id"):
                        candidate_assets.add(str(img["asset_id"]))
                cur.execute("DELETE FROM comments WHERE id = ? AND issue_number = ?;", (cid, num))
                cur.execute("UPDATE issues SET updated_at = ? WHERE number = ?;", (now, num))
                self._log_activity(
                    cur,
                    num,
                    "comment_deleted",
                    actor_clean,
                    f"Comment #{cid} deleted",
                    [{"field": "comment", "old": cid, "new": None}],
                    created_at=now,
                )
                self._sync_fts(cur, num)
                paths_to_unlink = self._cleanup_candidate_assets(cur, candidate_assets)
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
        self._unlink_paths(paths_to_unlink)
        return {"deleted": True, "id": cid, "issue_number": num}

    # ------------------------------------------------------------------
    # Activity Audit Log & Metadata
    # ------------------------------------------------------------------
    def get_activity_log(self, number: int | str) -> list[dict[str, Any]]:
        num = parse_issue_number(number)
        with self._lock:
            cur = self._conn.cursor()
            self._ensure_issue_exists(cur, num)
            return self._get_activity_cur(cur, num)

    def get_metadata_options(self) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            labels = self.list_labels()
            milestones = [
                r["milestone"]
                for r in cur.execute(
                    "SELECT DISTINCT milestone FROM issues WHERE milestone IS NOT NULL AND milestone != '' ORDER BY LOWER(milestone);"
                ).fetchall()
            ]
            assignees = [
                r["assignee"]
                for r in cur.execute(
                    "SELECT DISTINCT assignee FROM issues WHERE assignee IS NOT NULL AND assignee != '' ORDER BY LOWER(assignee);"
                ).fetchall()
            ]
            reporters = [
                r["reporter"]
                for r in cur.execute(
                    "SELECT DISTINCT reporter FROM issues WHERE reporter IS NOT NULL AND reporter != '' ORDER BY LOWER(reporter);"
                ).fetchall()
            ]
        return {
            "statuses": list(VALID_STATUSES),
            "priorities": list(VALID_PRIORITIES),
            "issue_types": list(VALID_ISSUE_TYPES),
            "relationship_types": list(VALID_RELATIONSHIP_TYPES),
            "labels": labels,
            "milestones": milestones,
            "assignees": assignees,
            "reporters": reporters,
        }
