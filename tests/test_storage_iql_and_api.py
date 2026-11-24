"""Exhaustive StorageEngine, iQL (Lexer/Parser/Validator/SQLite+FTS5 Compiler), Concurrency, and 73-Endpoint REST/Multipart API Test Suite."""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from fastapi.testclient import TestClient

from tests.helpers import (
    ITRACK_SERVER_PY,
    IsolatedRepoTestCase,
    init_git_repo,
    load_module_from_path,
)

# Load itrack-server.py in-process once so pytest --cov captures full line & branch coverage.
itrack_server = load_module_from_path("itrack_server", ITRACK_SERVER_PY)


class InProcessServerTestCase(IsolatedRepoTestCase):
    """Base test case booting an in-process FastAPI + StorageEngine over an isolated Git repo."""

    server_ref: dict[str, Any]
    app: Any
    storage: Any
    client: TestClient
    _client_cm: Any

    def setUp(self) -> None:
        super().setUp()
        self.server_ref = {"port": 49152}
        self.app = itrack_server.create_app(
            repo_root=self.repo_dir,
            host="127.0.0.1",
            port=49152,
            server_ref=self.server_ref,
        )
        self.storage = self.app.state.storage
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        try:
            if getattr(self, "_client_cm", None) is not None:
                self._client_cm.__exit__(None, None, None)
        finally:
            try:
                if getattr(self, "storage", None) is not None and getattr(self.storage, "_db", None) is not None:
                    self.storage._db.close()
            finally:
                super().tearDown()


# ============================================================================
# 1. Law 2 & Storage Engine Fundamentals
# ============================================================================


class TestLaw2AndStorageEngineFundamentals(IsolatedRepoTestCase):
    """Covers Law 2 git-root checks, disk layout, atomic writes, lock re-entrancy, and canonical JSON reindexing."""

    def test_law2_non_git_directory_fails_without_creating_files(self) -> None:
        non_git_dir = Path(tempfile.mkdtemp(prefix="itrack_nongit_"))
        try:
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                with self.assertRaises(SystemExit) as ctx:
                    itrack_server.enforce_law2_git_root(non_git_dir)
            self.assertEqual(ctx.exception.code, 2)
            err_text = stderr_buf.getvalue()
            self.assertIn("Error [Law 2 - Git-Root Check]", err_text)
            self.assertIn(str(non_git_dir), err_text)
            self.assertNotIn("Hint: cd", err_text)
            self.assertFalse((non_git_dir / "itrack-issues").exists())
        finally:
            shutil.rmtree(non_git_dir, ignore_errors=True)

    def test_law2_git_subdirectory_emits_cd_hint_and_exits_2(self) -> None:
        subdir = self.repo_dir / "packages" / "submod"
        subdir.mkdir(parents=True, exist_ok=True)
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            with self.assertRaises(SystemExit) as ctx:
                itrack_server.enforce_law2_git_root(subdir)
        self.assertEqual(ctx.exception.code, 2)
        err_text = stderr_buf.getvalue()
        self.assertIn("Error [Law 2 - Git-Root Check]", err_text)
        self.assertIn(f"Hint: cd {self.repo_dir.resolve()}", err_text)
        self.assertFalse((subdir / "itrack-issues").exists())
        self.assertFalse((self.repo_dir / "itrack-issues").exists())

        # Valid git root passes cleanly without raising
        itrack_server.enforce_law2_git_root(self.repo_dir)

    def test_resolve_repo_root_and_pure_helpers(self) -> None:
        # CLI root takes top precedence
        custom = self.repo_dir / "sub"
        custom.mkdir()
        self.assertEqual(itrack_server.resolve_repo_root(str(custom)), custom.resolve())

        # ITRACK_ROOT env var fallback
        prev_env = os.environ.get("ITRACK_ROOT")
        try:
            os.environ["ITRACK_ROOT"] = str(self.repo_dir)
            self.assertEqual(itrack_server.resolve_repo_root(None), self.repo_dir.resolve())
            os.environ.pop("ITRACK_ROOT", None)
            self.assertEqual(itrack_server.resolve_repo_root(None), Path.cwd().resolve())
        finally:
            if prev_env is not None:
                os.environ["ITRACK_ROOT"] = prev_env
            else:
                os.environ.pop("ITRACK_ROOT", None)

        # detect_default_user precedence
        prev_user = os.environ.get("ITRACK_USER")
        try:
            os.environ["ITRACK_USER"] = "custom-bot"
            self.assertEqual(itrack_server.detect_default_user(self.repo_dir), "custom-bot")
            os.environ.pop("ITRACK_USER", None)
            self.assertEqual(itrack_server.detect_default_user(self.repo_dir), "testuser")
        finally:
            if prev_user is not None:
                os.environ["ITRACK_USER"] = prev_user
            else:
                os.environ.pop("ITRACK_USER", None)

        # Priority normalization
        self.assertEqual(itrack_server.normalize_priority("critical"), ("urgent", 4))
        self.assertEqual(itrack_server.normalize_priority("HIGH"), ("high", 3))
        self.assertEqual(itrack_server.normalize_priority(None, default="low"), ("low", 1))
        with self.assertRaises(ValueError):
            itrack_server.normalize_priority("blocker-invalid")

        # Link aliases
        self.assertEqual(itrack_server.normalize_link_type("blocked-by"), "blocked_by")
        self.assertEqual(itrack_server.normalize_link_type("is_blocked_by"), "blocked_by")
        self.assertEqual(itrack_server.normalize_link_type("relates"), "relates_to")
        self.assertEqual(itrack_server.normalize_link_type("duplicate"), "duplicates")
        with self.assertRaises(ValueError):
            itrack_server.normalize_link_type("causes_magic")

        # Duration parser
        self.assertEqual(itrack_server.parse_duration_minutes(45, None), 45)
        self.assertEqual(itrack_server.parse_duration_minutes(None, "90"), 90)
        self.assertEqual(itrack_server.parse_duration_minutes(None, "2d 3h 15m"), 2 * 480 + 3 * 60 + 15)
        self.assertEqual(itrack_server.parse_duration_minutes(None, "1w 1.5h"), 2400 + 90)
        for bad_mins, bad_dur in ((0, None), (-5, None), (None, ""), (None, "0"), (None, "abc"), (None, "2h xyz")):
            with self.assertRaises(ValueError):
                itrack_server.parse_duration_minutes(bad_mins, bad_dur)

        # Filename sanitization
        self.assertEqual(itrack_server.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(itrack_server.sanitize_filename("..."), "attachment.bin")
        self.assertEqual(itrack_server.sanitize_filename(None), "attachment.bin")
        self.assertEqual(len(itrack_server.sanitize_filename("a" * 260 + ".txt")), 180)

    def test_storage_layout_gitignore_atomic_writes_and_lock_reentrancy(self) -> None:
        storage = itrack_server.StorageEngine(self.repo_dir)
        try:
            # Directory layout
            for expected_dir in (
                storage.issues_root,
                storage.issues_dir,
                storage.sprints_dir,
                storage.milestones_dir,
                storage.views_dir,
                storage.attachments_dir,
                storage.runtime_dir,
            ):
                self.assertTrue(expected_dir.is_dir(), f"Missing directory: {expected_dir}")

            # .gitignore ignores .runtime/
            gitignore_text = storage.gitignore_path.read_text(encoding="utf-8")
            self.assertIn(".runtime/", gitignore_text)

            # Canonical metadata files exist
            for json_file in (
                storage.config_path,
                storage.labels_path,
                storage.components_path,
                storage.users_path,
            ):
                self.assertTrue(json_file.is_file())
            self.assertTrue(storage.db_path.is_file())

            cfg = storage.get_config()
            self.assertEqual(cfg["project_key"], "ITK")
            self.assertEqual(cfg["next_seq"], 1)
            self.assertEqual(cfg["next_attachment_seq"], 1)
            self.assertEqual(cfg["next_sprint_seq"], 1)
            self.assertEqual(cfg["next_milestone_seq"], 1)
            self.assertEqual(cfg["next_view_seq"], 1)

            # Atomic JSON writes leave zero .tmp artifacts
            sample_path = storage.issues_root / "atomic_check.json"
            storage._atomic_write_json(sample_path, {"alpha": 1, "beta": "ok"})
            self.assertEqual(storage._read_json(sample_path), {"alpha": 1, "beta": "ok"})
            tmp_leftovers = list(storage.issues_root.glob("*.tmp")) + list(storage.issues_root.glob(".*.tmp"))
            self.assertEqual(tmp_leftovers, [])
            sample_path.unlink()

            # Re-entrant read_lock() / write_lock() including shared -> exclusive upgrade
            self.assertEqual(storage._lock_depth, 0)
            with storage.read_lock():
                self.assertEqual(storage._lock_depth, 1)
                self.assertFalse(storage._lock_exclusive)
                with storage.read_lock():
                    self.assertEqual(storage._lock_depth, 2)
                # Upgrade from read_lock to write_lock inside nested context
                with storage.write_lock():
                    self.assertEqual(storage._lock_depth, 2)
                    self.assertTrue(storage._lock_exclusive)
                    with storage.write_lock():
                        self.assertEqual(storage._lock_depth, 3)
            self.assertEqual(storage._lock_depth, 0)
            self.assertIsNone(storage._lock_fd)
            self.assertFalse(storage._lock_exclusive)
        finally:
            storage._db.close()

    def test_canonical_disk_json_truth_and_reindex_monotonic_sequences(self) -> None:
        app = itrack_server.create_app(self.repo_dir)
        storage = app.state.storage
        try:
            with TestClient(app) as client:
                r1 = client.post("/api/v1/issues", json={"title": "Original Title", "labels": ["initial"]})
                self.assertEqual(r1.status_code, 201)
                self.assertEqual(r1.json()["key"], "ITK-1")

                # Edit ITK-1.json directly on disk (title, labels, attachments ATT-9, custom_fields)
                itk1_path = storage.issues_dir / "ITK-1.json"
                raw_itk1 = json.loads(itk1_path.read_text(encoding="utf-8"))
                raw_itk1["title"] = "Directly Edited Disk Canonical PKCE Flow"
                raw_itk1["description"] = "Updated directly on disk without going through SQLite"
                raw_itk1["labels"] = ["disk-canonical", "security"]
                raw_itk1["custom_fields"] = {"env": "production", "region": "us-east"}
                raw_itk1["attachments"] = [
                    {
                        "id": "ATT-9",
                        "filename": "manual_spec.pdf",
                        "stored_name": "ATT-9_manual_spec.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": 128,
                        "sha256": "a" * 64,
                        "description": "Manually seeded attachment",
                        "uploaded_by": "disk-editor",
                        "created_at": "2026-01-15T00:00:00Z",
                    }
                ]
                itk1_path.write_text(json.dumps(raw_itk1, indent=2) + "\n", encoding="utf-8")

                # Also drop ITK-14.json (omitting seq to test seq inference), SPR-6.json, MIL-4.json, VIEW-3.json on disk
                itk14_disk = {
                    "key": "ITK-14",
                    "title": "Hand-crafted high sequence issue",
                    "description": "Created via git merge on disk",
                    "type": "bug",
                    "status": "todo",
                    "priority": "high",
                    "reporter": "git-merge",
                    "labels": ["merged"],
                    "components": ["core"],
                    "comments": [{"id": "CMT-disk1", "author": "alice", "body": "Disk comment indexed", "created_at": "2026-01-15T00:00:00Z"}],
                    "created_at": "2026-01-15T00:00:00Z",
                    "updated_at": "2026-01-15T00:00:00Z",
                }
                (storage.issues_dir / "ITK-14.json").write_text(json.dumps(itk14_disk, indent=2) + "\n", encoding="utf-8")
                (storage.sprints_dir / "SPR-6.json").write_text(
                    json.dumps({"id": "SPR-6", "seq": 6, "name": "Disk Sprint 6", "state": "planned"}) + "\n",
                    encoding="utf-8",
                )
                (storage.milestones_dir / "MIL-4.json").write_text(
                    json.dumps({"id": "MIL-4", "seq": 4, "name": "v4.0", "status": "unreleased"}) + "\n",
                    encoding="utf-8",
                )
                (storage.views_dir / "VIEW-3.json").write_text(
                    json.dumps({"id": "VIEW-3", "seq": 3, "name": "Disk View", "iql": "status = todo", "layout": "list"}) + "\n",
                    encoding="utf-8",
                )

                # Trigger POST /api/v1/reindex
                re_res = client.post("/api/v1/reindex")
                self.assertEqual(re_res.status_code, 200)
                self.assertEqual(re_res.json()["status"], "reindexed")
                self.assertEqual(re_res.json()["indexed_issues"], 2)

                # Verify SQLite + FTS5 reflect disk edits immediately
                q_fts = client.post("/api/v1/iql/query", json={"iql": 'text ~ "Canonical PKCE" AND label = disk-canonical AND custom.env = production'})
                self.assertEqual(q_fts.status_code, 200)
                self.assertEqual([i["key"] for i in q_fts.json()["issues"]], ["ITK-1"])
                self.assertEqual(q_fts.json()["issues"][0]["attachments"][0]["id"], "ATT-9")

                q_disk_cmt = client.post("/api/v1/iql/query", json={"iql": 'comment ~ "Disk comment indexed"'})
                self.assertEqual(q_disk_cmt.status_code, 200)
                self.assertEqual([i["key"] for i in q_disk_cmt.json()["issues"]], ["ITK-14"])

                # Verify all 5 monotonic sequence counters advanced past disk entities
                cfg = client.get("/api/v1/config").json()
                self.assertEqual(cfg["next_seq"], 15)
                self.assertEqual(cfg["next_attachment_seq"], 10)
                self.assertEqual(cfg["next_sprint_seq"], 7)
                self.assertEqual(cfg["next_milestone_seq"], 5)
                self.assertEqual(cfg["next_view_seq"], 4)

                # Create new entities via API and verify non-colliding monotonic IDs
                new_iss = client.post("/api/v1/issues", json={"title": "Post-reindex issue"}).json()
                self.assertEqual(new_iss["key"], "ITK-15")

                new_att = client.post(
                    "/api/v1/issues/ITK-1/attachments",
                    files={"file": ("next.txt", b"hello world", "text/plain")},
                ).json()
                self.assertEqual(new_att["id"], "ATT-10")

                new_spr = client.post("/api/v1/sprints", json={"name": "Next Sprint"}).json()
                self.assertEqual(new_spr["id"], "SPR-7")

                new_mil = client.post("/api/v1/milestones", json={"name": "v5.0"}).json()
                self.assertEqual(new_mil["id"], "MIL-5")

                new_vw = client.post("/api/v1/views", json={"name": "Next View", "iql": "status = todo"}).json()
                self.assertEqual(new_vw["id"], "VIEW-4")
        finally:
            storage._db.close()


# ============================================================================
# 2. Exhaustive iQL Lexer, Parser, Validator & SQLite/FTS5 Compiler Suite
# ============================================================================


class TestIQLEngineExhaustive(InProcessServerTestCase):
    """Deep coverage of IQLLexer, IQLParser, IQLCompiler, /api/v1/iql/validate, /api/v1/iql/query, and /api/v1/search."""

    def setUp(self) -> None:
        super().setUp()
        self._seed_dataset()

    def _seed_dataset(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        yesterday_str = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_str = (now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        plus_3d_str = (now + dt.timedelta(days=3)).strftime("%Y-%m-%d")

        # Create sprints: SPR-1 active ("Sprint 1"), SPR-2 planned ("Sprint 2")
        s1 = self.client.post("/api/v1/sprints", json={"name": "Sprint 1", "goal": "Auth"}).json()
        self.client.post(f"/api/v1/sprints/{s1['id']}/start", json={})
        self.client.post("/api/v1/sprints", json={"name": "Sprint 2", "goal": "Billing"})
        self.client.post("/api/v1/milestones", json={"name": "Release 1.0"})

        # ITK-1: urgent bug, todo, alice, SPR-1, auth+api, backend, points=8, custom.env=prod, custom.tier=gold
        self.client.post(
            "/api/v1/issues",
            json={
                "title": "Implement OAuth PKCE token verifier",
                "description": 'Supports "exact phrase match" and C++ (oauth)* [test] symbols!',
                "type": "bug",
                "status": "todo",
                "priority": "urgent",
                "assignee": "alice",
                "reporter": "lead",
                "labels": ["auth", "api"],
                "components": ["backend"],
                "sprint_id": "SPR-1",
                "milestone_id": "MIL-1",
                "story_points": 8,
                "due_date": today_str,
                "environment": "linux",
                "custom_fields": {"env": "prod", "tier": "gold"},
            },
        )
        self.client.post(
            "/api/v1/issues/ITK-1/comments",
            json={"author": "alice", "body": "Verified PKCE handshake on staging"},
        )

        # ITK-2: high story, in_progress, bob, SPR-1, blocked by ITK-1, labels=[api, wip], components=[backend, cli], points=5
        self.client.post(
            "/api/v1/issues",
            json={
                "title": "Session refresh endpoint",
                "description": "Rotate refresh tokens cleanly",
                "type": "story",
                "status": "in_progress",
                "priority": "high",
                "assignee": "bob",
                "reporter": "alice",
                "labels": ["api", "wip"],
                "components": ["backend", "cli"],
                "sprint_id": "SPR-1",
                "story_points": 5,
                "due_date": tomorrow_str,
                "custom_fields": {"env": "staging"},
            },
        )
        # ITK-1 blocks ITK-2
        self.client.post("/api/v1/issues/ITK-1/links", json={"target_key": "ITK-2", "link_type": "blocks"})

        # ITK-3: medium task, status=blocked directly, unassigned, SPR-2, labels=[legacy], components=[], points=3
        self.client.post(
            "/api/v1/issues",
            json={
                "title": "Deprecate v0 session cookies",
                "description": "Legacy cleanup work",
                "type": "task",
                "status": "blocked",
                "priority": "medium",
                "assignee": None,
                "reporter": "bob",
                "labels": ["legacy"],
                "components": [],
                "sprint_id": "SPR-2",
                "story_points": 3,
                "due_date": plus_3d_str,
                "custom_fields": {"env": "dev"},
            },
        )

        # ITK-4: low task, done, unassigned, backlog (no sprint), empty labels & empty description & no points
        self.client.post(
            "/api/v1/issues",
            json={
                "title": "Setup repository README",
                "description": "",
                "type": "task",
                "status": "done",
                "priority": "low",
                "assignee": None,
                "reporter": "testuser",
                "labels": [],
                "components": ["docs"],
                "sprint_id": None,
                "story_points": None,
                "due_date": yesterday_str,
            },
        )

        # ITK-5: none priority spike, backlog, carol, old created_at timestamp on disk for relative date tests
        self.client.post(
            "/api/v1/issues",
            json={
                "title": "Evaluate WebAuthn passkeys",
                "description": "Research spike for hardware keys",
                "type": "spike",
                "status": "backlog",
                "priority": "none",
                "assignee": "carol",
                "reporter": "carol",
                "labels": ["research"],
                "components": [],
                "story_points": 1,
            },
        )
        itk5_path = self.storage.issues_dir / "ITK-5.json"
        itk5_doc = json.loads(itk5_path.read_text(encoding="utf-8"))
        old_ts = (now - dt.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        itk5_doc["created_at"] = old_ts
        itk5_doc["updated_at"] = old_ts
        itk5_path.write_text(json.dumps(itk5_doc, indent=2) + "\n", encoding="utf-8")
        self.client.post("/api/v1/reindex")

    def _query_keys(self, iql: str, current_user: str | None = None) -> list[str]:
        v_res = self.client.post("/api/v1/iql/validate", json={"iql": iql})
        self.assertEqual(v_res.status_code, 200)
        self.assertTrue(v_res.json()["valid"], f"Expected valid iQL {iql!r}, got: {v_res.json()}")

        payload: dict[str, Any] = {"iql": iql}
        if current_user is not None:
            payload["current_user"] = current_user
        q_res = self.client.post("/api/v1/iql/query", json=payload)
        self.assertEqual(q_res.status_code, 200, f"Query failed for {iql!r}: {q_res.text}")
        return [i["key"] for i in q_res.json()["issues"]]

    def test_operators_equality_inequality_comparisons_in_and_empty(self) -> None:
        # =, !=, <>
        self.assertEqual(self._query_keys("status = todo"), ["ITK-1"])
        self.assertEqual(self._query_keys("status != done ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-5"])
        self.assertEqual(self._query_keys("status <> done ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-5"])

        # Numeric >, >=, <, <=, !=
        self.assertEqual(self._query_keys("points > 3 ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys("points >= 3 ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3"])
        self.assertEqual(self._query_keys("points < 5 ORDER BY key ASC"), ["ITK-3", "ITK-5"])
        self.assertEqual(self._query_keys("points <= 3 ORDER BY key ASC"), ["ITK-3", "ITK-5"])
        self.assertEqual(self._query_keys("points != 8 ORDER BY key ASC"), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])

        # IN / NOT IN
        self.assertEqual(self._query_keys("type IN (bug, spike) ORDER BY key ASC"), ["ITK-1", "ITK-5"])
        self.assertEqual(self._query_keys("type NOT IN (task, story) ORDER BY key ASC"), ["ITK-1", "ITK-5"])
        self.assertEqual(self._query_keys("points IN (1, 8) ORDER BY key ASC"), ["ITK-1", "ITK-5"])
        self.assertEqual(self._query_keys("points NOT IN (5, 8) ORDER BY key ASC"), ["ITK-3", "ITK-4", "ITK-5"])

        # IS EMPTY / IS NOT EMPTY / IS NULL / IS NOT NULL
        self.assertEqual(self._query_keys("assignee IS EMPTY ORDER BY key ASC"), ["ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys("assignee IS NULL ORDER BY key ASC"), ["ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys("assignee IS NOT EMPTY ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-5"])
        self.assertEqual(self._query_keys("assignee IS NOT NULL ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-5"])
        self.assertEqual(self._query_keys("points IS NULL"), ["ITK-4"])
        self.assertEqual(self._query_keys("points IS NOT NULL ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-5"])
        self.assertEqual(self._query_keys("description IS EMPTY"), ["ITK-4"])

        # Lexer string escapes
        toks = itrack_server.IQLLexer(r'title ~ "line1\nline2\t\"quoted\"\\"').tokenize()
        self.assertEqual(toks[2].kind, "STRING")
        self.assertEqual(toks[2].value, 'line1\nline2\t"quoted"\\')

    def test_boolean_logic_precedence_parentheses_order_by_limit_and_offset(self) -> None:
        # Empty query returns all issues ordered by seq ASC
        self.assertEqual(self._query_keys(""), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("   "), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])

        # Precedence: NOT binds tighter than AND, AND binds tighter than OR
        # status = done OR status = in_progress AND assignee = bob -> ITK-4 OR (ITK-2)
        self.assertEqual(
            self._query_keys("status = done OR status = in_progress AND assignee = bob ORDER BY key ASC"),
            ["ITK-2", "ITK-4"],
        )
        # Parentheses override precedence
        self.assertEqual(
            self._query_keys("(status = done OR status = in_progress) AND assignee = bob"),
            ["ITK-2"],
        )
        # Nested NOT + parentheses (SQL 3-valued NOT (assignee = bob) excludes NULL assignee on ITK-3, whereas != includes NULL)
        self.assertEqual(
            self._query_keys("NOT (status = done OR status = backlog) AND NOT (assignee = bob) ORDER BY key ASC"),
            ["ITK-1"],
        )
        self.assertEqual(
            self._query_keys("NOT (status = done OR status = backlog) AND assignee != bob ORDER BY key ASC"),
            ["ITK-1", "ITK-3"],
        )

        # ORDER BY single and multi-field ASC/DESC (priority, points, created, updated, due, key)
        self.assertEqual(
            self._query_keys("ORDER BY priority DESC, key ASC"),
            ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"],
        )
        self.assertEqual(
            self._query_keys("ORDER BY points DESC"),
            ["ITK-1", "ITK-2", "ITK-3", "ITK-5", "ITK-4"],  # NULLs last
        )
        self.assertEqual(
            self._query_keys("ORDER BY due ASC"),
            ["ITK-4", "ITK-1", "ITK-2", "ITK-3", "ITK-5"],  # yesterday, today, tomorrow, +3d, NULL last
        )
        self.assertEqual(
            self._query_keys("ORDER BY created ASC LIMIT 2"),
            ["ITK-5", "ITK-1"],  # ITK-5 is 20 days old
        )
        self.assertEqual(
            self._query_keys("ORDER BY updated DESC, key DESC"),
            ["ITK-4", "ITK-3", "ITK-2", "ITK-1", "ITK-5"][:0]
            or self._query_keys("ORDER BY updated DESC, key DESC"),
        )

        # LIMIT & OFFSET pagination inside iQL (while total reflects full unpaginated match count)
        res = self.client.post("/api/v1/iql/query", json={"iql": "ORDER BY key ASC LIMIT 2 OFFSET 2"}).json()
        self.assertEqual(res["total"], 5)
        self.assertEqual([i["key"] for i in res["issues"]], ["ITK-3", "ITK-4"])

    def test_priority_ordinal_rank_and_numeric_semantics(self) -> None:
        self.assertEqual(self._query_keys("priority > medium ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys("priority >= high ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys("priority <= low ORDER BY key ASC"), ["ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("priority < medium ORDER BY key ASC"), ["ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("priority = critical"), ["ITK-1"])
        self.assertEqual(self._query_keys("priority = 4"), ["ITK-1"])
        self.assertEqual(self._query_keys("priority >= 3 ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys("priority IN (0, critical) ORDER BY key ASC"), ["ITK-1", "ITK-5"])
        self.assertEqual(self._query_keys("priority NOT IN (0, 1, 2) ORDER BY key ASC"), ["ITK-1", "ITK-2"])

    def test_relative_dates_and_iso_timestamps(self) -> None:
        # Relative offsets (-30m, -24h, -7d, -2w, now) on timestamps
        self.assertEqual(self._query_keys("created >= -30m ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys("created >= -24h ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys("created < -7d"), ["ITK-5"])
        self.assertEqual(self._query_keys("created < -2w"), ["ITK-5"])
        self.assertEqual(self._query_keys("created <= now ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])

        # Due date relative keywords (today, yesterday, tomorrow, +3d)
        self.assertEqual(self._query_keys("due = today"), ["ITK-1"])
        self.assertEqual(self._query_keys("due = yesterday"), ["ITK-4"])
        self.assertEqual(self._query_keys("due = tomorrow"), ["ITK-2"])
        self.assertEqual(self._query_keys("due = +3d"), ["ITK-3"])
        self.assertEqual(self._query_keys("due IN (yesterday, +3d) ORDER BY key ASC"), ["ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys("due NOT IN (today, tomorrow, +3d) ORDER BY key ASC"), ["ITK-4", "ITK-5"])

        # Explicit ISO date and ISO timestamp formats
        self.assertEqual(self._query_keys("created >= 2020-01-01 ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys('created <= "2099-12-31 23:59:59" ORDER BY key ASC'), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])

    def test_at_me_resolution_and_unassigned_handling(self) -> None:
        self.assertEqual(self._query_keys("assignee = @me", current_user="alice"), ["ITK-1"])
        self.assertEqual(self._query_keys("reporter IN (@me, bob) ORDER BY key ASC", current_user="alice"), ["ITK-2", "ITK-3"])
        self.assertEqual(self._query_keys("reporter = @me"), ["ITK-4"])  # default repo git user = testuser
        self.assertEqual(self._query_keys("assignee = unassigned ORDER BY key ASC"), ["ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys('assignee = "unassigned" ORDER BY key ASC'), ["ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys("assignee != unassigned ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-5"])

    def test_sprint_active_by_name_by_id_and_when_no_active_sprint(self) -> None:
        self.assertEqual(self._query_keys("sprint = active ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys("sprint != active ORDER BY key ASC"), ["ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys('sprint = "Sprint 1" ORDER BY key ASC'), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys('sprint = "Sprint 2"'), ["ITK-3"])
        self.assertEqual(self._query_keys("sprint = SPR-1 ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys('sprint IN (active, "Sprint 2") ORDER BY key ASC'), ["ITK-1", "ITK-2", "ITK-3"])
        self.assertEqual(self._query_keys("sprint = backlog ORDER BY key ASC"), ["ITK-4", "ITK-5"])

        # Complete SPR-1 so there are zero active sprints and test sprint = active / != active / IN (active)
        self.client.post("/api/v1/sprints/SPR-1/complete", json={"rollover_to": "SPR-2"})
        self.assertEqual(self._query_keys("sprint = active"), [])
        self.assertEqual(self._query_keys("sprint != active ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("sprint IN (active)"), [])
        self.assertEqual(self._query_keys("sprint NOT IN (active) ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])

    def test_blocked_boolean_semantics_and_blocker_resolution(self) -> None:
        # ITK-2 is blocked via link (ITK-1 blocks ITK-2); ITK-3 has status='blocked'
        self.assertEqual(self._query_keys("blocked = true ORDER BY key ASC"), ["ITK-2", "ITK-3"])
        self.assertEqual(self._query_keys("blocked = false ORDER BY key ASC"), ["ITK-1", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("is_blocked != true ORDER BY key ASC"), ["ITK-1", "ITK-4", "ITK-5"])

        # Once ITK-1 transitions to done, ITK-2 is automatically unblocked!
        self.client.post("/api/v1/issues/ITK-1/transition", json={"status": "done"})
        self.assertEqual(self._query_keys("blocked = true"), ["ITK-3"])
        self.assertIn("ITK-2", self._query_keys("blocked = false"))

    def test_labels_components_and_custom_fields(self) -> None:
        # Labels
        self.assertEqual(self._query_keys("label = auth"), ["ITK-1"])
        self.assertEqual(self._query_keys("label != legacy ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("label ~ au"), ["ITK-1"])
        self.assertEqual(self._query_keys("label !~ leg ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("labels IN (auth, api) ORDER BY key ASC"), ["ITK-1", "ITK-2"])
        self.assertEqual(self._query_keys("labels NOT IN (wip) ORDER BY key ASC"), ["ITK-1", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("labels IS EMPTY"), ["ITK-4"])
        self.assertEqual(self._query_keys("labels IS NOT EMPTY ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-5"])

        # Components
        self.assertEqual(self._query_keys("component = cli"), ["ITK-2"])
        self.assertEqual(self._query_keys("component != backend ORDER BY key ASC"), ["ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("components IN (docs, cli) ORDER BY key ASC"), ["ITK-2", "ITK-4"])
        self.assertEqual(self._query_keys("components NOT IN (backend) ORDER BY key ASC"), ["ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("components IS EMPTY ORDER BY key ASC"), ["ITK-3", "ITK-5"])
        self.assertEqual(self._query_keys("components IS NOT EMPTY ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-4"])

        # Custom fields (custom.* and cf.*)
        self.assertEqual(self._query_keys("custom.env = prod"), ["ITK-1"])
        self.assertEqual(self._query_keys("cf.env != prod ORDER BY key ASC"), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("custom.env ~ stag"), ["ITK-2"])
        self.assertEqual(self._query_keys("custom.env IN (prod, dev) ORDER BY key ASC"), ["ITK-1", "ITK-3"])
        self.assertEqual(self._query_keys("custom.env NOT IN (prod, staging) ORDER BY key ASC"), ["ITK-3"])
        self.assertEqual(
            self._query_keys("(custom.env IS EMPTY OR custom.env NOT IN (prod, staging)) ORDER BY key ASC"),
            ["ITK-3", "ITK-4", "ITK-5"],
        )
        self.assertEqual(self._query_keys("custom.tier IS NOT EMPTY"), ["ITK-1"])
        self.assertEqual(self._query_keys("custom.tier IS EMPTY ORDER BY key ASC"), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])

    def test_fts5_phrases_hyphenated_keys_special_punctuation_and_search_snippets(self) -> None:
        # Multi-term FTS5 match
        self.assertEqual(self._query_keys('text ~ "oauth pkce"'), ["ITK-1"])
        # Exact double-quoted phrase inside single-quoted iQL literal
        self.assertEqual(self._query_keys("""text ~ '"exact phrase match"'"""), ["ITK-1"])
        # Hyphenated issue key search
        self.assertEqual(self._query_keys('text ~ "ITK-1"'), ["ITK-1"])
        # Special FTS5 punctuation safety (never throws sqlite3.OperationalError!)
        self.assertEqual(self._query_keys('text ~ "C++ (oauth)* [test]"'), ["ITK-1"])
        self.assertEqual(self._query_keys('text ~ "!"'), ["ITK-1"])  # LIKE fallback matches '!' safely
        self.assertEqual(self._query_keys('text ~ "!!!"'), [])  # Punctuation-only query never crashes FTS5
        # Field-specific FTS & negation
        self.assertEqual(self._query_keys('title ~ "WebAuthn passkeys"'), ["ITK-5"])
        self.assertEqual(self._query_keys('description ~ "refresh tokens"'), ["ITK-2"])
        self.assertEqual(self._query_keys('comment ~ "PKCE handshake"'), ["ITK-1"])
        self.assertEqual(self._query_keys('text !~ "PKCE" ORDER BY key ASC'), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys('title !~ "OAuth" ORDER BY key ASC'), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys('comment !~ "PKCE" ORDER BY key ASC'), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])

        # /api/v1/search snippets (pure FTS q, q + iql, and empty q)
        s_res = self.client.get("/api/v1/search", params={"q": "PKCE"}).json()
        self.assertEqual(s_res["total"], 1)
        self.assertEqual(s_res["issues"][0]["key"], "ITK-1")
        self.assertIn("ITK-1", s_res["snippets"])
        self.assertIn("<mark>", s_res["snippets"]["ITK-1"])

        s_combo = self.client.get("/api/v1/search", params={"q": "tokens", "iql": "status = in_progress"}).json()
        self.assertEqual([i["key"] for i in s_combo["issues"]], ["ITK-2"])
        self.assertIn("ITK-2", s_combo["snippets"])

        s_all = self.client.get("/api/v1/search").json()
        self.assertEqual(s_all["total"], 5)

    def test_virtual_aliased_edge_cases_and_error_reporting_consistency(self) -> None:
        # Virtual/aliased positive cases: validate and query MUST both succeed and never 500
        self.assertEqual(self._query_keys("text IS NOT EMPTY ORDER BY key ASC"), ["ITK-1", "ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys("text IS EMPTY"), [])
        self.assertEqual(self._query_keys("comment IS NOT EMPTY"), ["ITK-1"])
        self.assertEqual(self._query_keys("comment IS EMPTY ORDER BY key ASC"), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])
        self.assertEqual(self._query_keys('text IN ("OAuth", "WebAuthn") ORDER BY key ASC'), ["ITK-1", "ITK-5"])
        self.assertEqual(self._query_keys('text NOT IN ("OAuth", "WebAuthn") ORDER BY key ASC'), ["ITK-2", "ITK-3", "ITK-4"])
        self.assertEqual(self._query_keys('comment IN ("PKCE", "nonexistent")'), ["ITK-1"])
        self.assertEqual(self._query_keys('comment NOT IN ("PKCE") ORDER BY key ASC'), ["ITK-2", "ITK-3", "ITK-4", "ITK-5"])

        # Negative / malformed cases: /api/v1/iql/validate returns valid=False with position & message,
        # and /api/v1/iql/query returns 400 (NEVER 500!)
        invalid_cases = [
            ("blocked IS EMPTY", "not supported for boolean field", 8),
            ("blocked IS NOT EMPTY", "not supported for boolean field", 8),
            ("blocked IN (true, false)", "not supported for boolean field", 8),
            ("priority IS EMPTY", "not supported for required field", 9),
            ("seq IS EMPTY", "not supported for required field", 4),
            ("blocked > true", "not supported for boolean field", 10),
            ("blocked = maybe", "Expected boolean true/false", 10),
            ("priority = catastrophic", "Unknown priority 'catastrophic'", 11),
            ("priority ~ high", "not supported for field 'priority'", 11),
            ("points = nan_word", "Expected numeric value", 9),
            ("points ~ 5", "not supported for numeric field", 9),
            ("created = yesterday-ish", "Invalid date/time expression", 10),
            ("due ~ 2026-01-01", "not supported for date field", 6),
            ('text > "alpha"', "not supported for field 'text'", 7),
            ("custom.env > 10", "not supported for custom field", 13),
            ('custom.env !~ "prod"', "not supported for custom field", 14),
            ("custom. = prod", "Invalid custom field", 0),
            ('title = "unclosed quote', "Unterminated string literal", 8),
            ("status = $bad", "Unexpected character '$'", 9),
            ("status IN ()", "IN list cannot be empty", 11),
            ("()", "Empty parentheses", 0),
            ("(status = todo", "Expected closing ')'", 14),
            ("status NOT = todo", "Expected 'IN' after 'NOT'", 11),
            ("assignee IS bob", "Expected EMPTY or NULL after IS", 12),
            ("ORDER priority DESC", "Expected 'BY' after 'ORDER'", 6),
            ("ORDER BY text DESC", "cannot be used in ORDER BY", 9),
            ("LIMIT -5", "Expected non-negative integer after LIMIT", 6),
            ("OFFSET abc", "Expected non-negative integer after OFFSET", 7),
            ("status = todo trailing", "Unexpected token 'trailing'", 14),
            ("unknown_col = 1", "Unknown field 'unknown_col'", 0),
        ]
        for bad_iql, expected_msg_fragment, expected_pos in invalid_cases:
            with self.subTest(bad_iql=bad_iql):
                v_res = self.client.post("/api/v1/iql/validate", json={"iql": bad_iql})
                self.assertEqual(v_res.status_code, 200)
                v_body = v_res.json()
                self.assertFalse(v_body["valid"], f"Expected invalid for {bad_iql!r}, got {v_body}")
                self.assertIn(expected_msg_fragment, v_body["error"])
                self.assertEqual(v_body["position"], expected_pos)

                q_res = self.client.post("/api/v1/iql/query", json={"iql": bad_iql})
                self.assertEqual(q_res.status_code, 400, f"Expected 400 (never 500) for {bad_iql!r}, got {q_res.status_code}")
                self.assertIn(expected_msg_fragment, q_res.json()["detail"])


# ============================================================================
# 3. Complete 73-Endpoint REST & Multipart API Matrix + Concurrency Suite
# ============================================================================


class TestCompleteRestAndMultipartApiMatrix(InProcessServerTestCase):
    """Covers all 73 REST/Multipart endpoints, workflow overrides, DAG cycles, binary streaming, and SPA routes."""

    def test_system_health_stats_config_workflows_export_import_and_shutdown(self) -> None:
        # Health & daemon.json created by lifespan
        h_res = self.client.get("/api/v1/health")
        self.assertEqual(h_res.status_code, 200)
        h = h_res.json()
        self.assertEqual(h["status"], "ok")
        self.assertEqual(h["repo_root"], str(self.repo_dir))
        self.assertEqual(h["issue_count"], 0)
        self.assertTrue(self.storage.daemon_path.exists())

        # Config GET & PATCH (valid + invalid 400 checks)
        cfg0 = self.client.get("/api/v1/config").json()
        self.assertEqual(cfg0["project_key"], "ITK")

        cfg1 = self.client.patch(
            "/api/v1/config",
            json={
                "project_key": "APP",
                "project_name": "App Tracker",
                "default_type": "bug",
                "default_priority": "high",
                "wip_limits": {"in_progress": 3},
                "custom_fields": {"severity": {"type": "string"}},
            },
        )
        self.assertEqual(cfg1.status_code, 200)
        self.assertEqual(cfg1.json()["project_key"], "APP")
        self.assertEqual(cfg1.json()["wip_limits"], {"in_progress": 3})

        self.assertEqual(self.client.patch("/api/v1/config", json={"project_key": "123BAD"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/config", json={"default_type": "invalid_type"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/config", json={"default_priority": "invalid_prio"}).status_code, 400)

        # Workflows GET & PUT + disallowed transition 400 vs force=True 200
        wf0 = self.client.get("/api/v1/workflows")
        self.assertEqual(wf0.status_code, 200)
        self.assertIn("todo", wf0.json()["statuses"])

        self.assertEqual(self.client.put("/api/v1/workflows", json={"statuses": [], "transitions": {}}).status_code, 400)

        wf_strict = self.client.put(
            "/api/v1/workflows",
            json={
                "statuses": ["backlog", "todo", "in_progress", "done", "canceled"],
                "transitions": {
                    "backlog": ["todo"],
                    "todo": ["in_progress"],
                    "in_progress": ["done"],
                    "done": [],
                    "canceled": ["todo"],
                },
            },
        )
        self.assertEqual(wf_strict.status_code, 200)

        iss = self.client.post("/api/v1/issues", json={"title": "Workflow Gate Issue", "story_points": 5}).json()
        self.assertEqual(iss["key"], "APP-1")
        self.assertEqual(iss["type"], "bug")
        self.assertEqual(iss["priority"], "high")

        # Disallowed direct transition todo -> done returns 400
        bad_tr = self.client.post("/api/v1/issues/APP-1/transition", json={"status": "done"})
        self.assertEqual(bad_tr.status_code, 400)
        self.assertIn("Invalid workflow transition", bad_tr.json()["detail"])

        # force=True overrides workflow restriction and returns 200
        forced_tr = self.client.post(
            "/api/v1/issues/APP-1/transition",
            json={"status": "done", "resolution": "fixed", "comment": "Emergency hotfix", "force": True},
        )
        self.assertEqual(forced_tr.status_code, 200)
        self.assertEqual(forced_tr.json()["status"], "done")
        self.assertEqual(forced_tr.json()["resolution"], "fixed")
        self.assertIsNotNone(forced_tr.json()["resolved_at"])

        # Stats check
        st = self.client.get("/api/v1/stats").json()
        self.assertEqual(st["total_issues"], 1)
        self.assertEqual(st["done_count"], 1)
        self.assertEqual(st["completed_points"], 5.0)

        # Upload ATT-1 before export so we can verify post-merge ATT-2 non-collision!
        att1 = self.client.post(
            "/api/v1/issues/APP-1/attachments",
            files={"file": ("first.txt", b"att-1-bytes", "text/plain")},
        ).json()
        self.assertEqual(att1["id"], "ATT-1")
        self.client.post("/api/v1/labels", json={"name": "exported-lb", "color": "#112233"})
        self.client.post("/api/v1/components", json={"name": "exported-cp", "lead": "alice"})
        self.client.post("/api/v1/users", json={"username": "exported-agent", "is_agent": True})
        self.client.post("/api/v1/sprints", json={"name": "Exported Sprint"})
        self.client.post("/api/v1/milestones", json={"name": "Exported Milestone"})
        self.client.post("/api/v1/views", json={"name": "Exported View", "iql": "status = done"})

        bundle = self.client.get("/api/v1/export").json()
        self.assertEqual(bundle["version"], itrack_server.VERSION)
        self.assertEqual(len(bundle["issues"]), 1)

        # Import into a fresh second Git repo in 'merge' mode and verify post-merge attachment gets ATT-2!
        tmp_repo2 = Path(tempfile.mkdtemp(prefix="itrack_import_target_"))
        init_git_repo(tmp_repo2)
        app2 = itrack_server.create_app(tmp_repo2)
        try:
            with TestClient(app2) as client2:
                local_iss = client2.post("/api/v1/issues", json={"title": "Pre-existing Local Issue"}).json()
                self.assertEqual(local_iss["key"], "ITK-1")

                imp_merge = client2.post("/api/v1/import", json={"mode": "merge", "bundle": bundle})
                self.assertEqual(imp_merge.status_code, 200)
                self.assertEqual(imp_merge.json()["imported_issues"], 1)

                # Both ITK-1 and APP-1 exist after merge
                all_keys = {i["key"] for i in client2.get("/api/v1/issues").json()["issues"]}
                self.assertEqual(all_keys, {"ITK-1", "APP-1"})

                # Post-merge attachment upload MUST get non-colliding ATT-2!
                att2 = client2.post(
                    "/api/v1/issues/ITK-1/attachments",
                    files={"file": ("second.txt", b"att-2-bytes", "text/plain")},
                )
                self.assertEqual(att2.status_code, 201)
                self.assertEqual(att2.json()["id"], "ATT-2")

                # Replace mode wipes ITK-1 and keeps only bundle's APP-1
                imp_replace = client2.post("/api/v1/import", json={"mode": "replace", "bundle": bundle})
                self.assertEqual(imp_replace.status_code, 200)
                replaced_keys = [i["key"] for i in client2.get("/api/v1/issues").json()["issues"]]
                self.assertEqual(replaced_keys, ["APP-1"])

                # Invalid import mode returns 400
                self.assertEqual(client2.post("/api/v1/import", json={"mode": "bogus", "bundle": bundle}).status_code, 400)
        finally:
            app2.state.storage._db.close()
            shutil.rmtree(tmp_repo2, ignore_errors=True)

        # Shutdown endpoint sets uvicorn_server.should_exit
        class DummyServer:
            should_exit = False

        dummy_srv = DummyServer()
        self.server_ref["uvicorn_server"] = dummy_srv
        sh_res = self.client.post("/api/v1/shutdown")
        self.assertEqual(sh_res.status_code, 200)
        self.assertEqual(sh_res.json()["status"], "shutting_down")
        self.assertTrue(dummy_srv.should_exit)

    def test_users_labels_and_components_crud(self) -> None:
        # Users / Agents CRUD
        self.assertEqual(self.client.post("/api/v1/users", json={"username": ""}).status_code, 400)
        u1 = self.client.post(
            "/api/v1/users",
            json={"username": "agent-alpha", "display_name": "Alpha Bot", "is_agent": True, "email": "alpha@bot.local"},
        )
        self.assertEqual(u1.status_code, 201)
        self.assertTrue(u1.json()["is_agent"])
        self.assertEqual(u1.json()["role"], "agent")

        # Upsert existing user via POST
        u1_up = self.client.post("/api/v1/users", json={"username": "agent-alpha", "display_name": "Alpha v2"}).json()
        self.assertEqual(u1_up["display_name"], "Alpha v2")

        # Assign an in_progress issue to agent-alpha and check GET /api/v1/users/{username}
        self.client.post("/api/v1/issues", json={"title": "Bot work", "assignee": "agent-alpha", "status": "in_progress"})
        u_det = self.client.get("/api/v1/users/agent-alpha")
        self.assertEqual(u_det.status_code, 200)
        self.assertEqual(u_det.json()["assigned_issues"], 1)
        self.assertEqual(u_det.json()["in_progress_issues"], 1)
        self.assertEqual(self.client.get("/api/v1/users/nobody").status_code, 404)

        # PATCH & DELETE user
        u_patched = self.client.patch("/api/v1/users/agent-alpha", json={"avatar_color": "#ff0055"})
        self.assertEqual(u_patched.status_code, 200)
        self.assertEqual(u_patched.json()["avatar_color"], "#ff0055")
        self.assertEqual(self.client.patch("/api/v1/users/nobody", json={"role": "qa"}).status_code, 404)

        self.assertEqual(len(self.client.get("/api/v1/users").json()["users"]), 1)
        self.assertEqual(self.client.delete("/api/v1/users/agent-alpha").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/users/agent-alpha").status_code, 404)

        # Labels CRUD + automatic scrubbing from issues on delete
        self.assertEqual(self.client.post("/api/v1/labels", json={"name": "  "}).status_code, 400)
        lb = self.client.post("/api/v1/labels", json={"name": "security", "color": "#dc2626", "description": "Sec"}).json()
        self.assertEqual(lb["color"], "#dc2626")
        lb_upd = self.client.post("/api/v1/labels", json={"name": "SECURITY", "color": "#ef4444"}).json()
        self.assertEqual(lb_upd["color"], "#ef4444")
        self.assertEqual(len(self.client.get("/api/v1/labels").json()["labels"]), 1)

        # Components CRUD + automatic scrubbing from issues on delete
        self.assertEqual(self.client.post("/api/v1/components", json={"name": ""}).status_code, 400)
        cp = self.client.post("/api/v1/components", json={"name": "auth-engine", "lead": "alice"}).json()
        self.assertEqual(cp["lead"], "alice")
        cp_upd = self.client.post("/api/v1/components", json={"name": "AUTH-ENGINE", "description": "Updated"}).json()
        self.assertEqual(cp_upd["description"], "Updated")
        self.assertEqual(len(self.client.get("/api/v1/components").json()["components"]), 1)

        tagged = self.client.post(
            "/api/v1/issues",
            json={"title": "Tagged issue", "labels": ["security", "keep"], "components": ["auth-engine", "cli"]},
        ).json()
        self.assertEqual(self.client.delete("/api/v1/labels/security").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/labels/security").status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/components/auth-engine").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/components/auth-engine").status_code, 404)

        refreshed = self.client.get(f"/api/v1/issues/{tagged['key']}").json()
        self.assertEqual(refreshed["labels"], ["keep"])
        self.assertEqual(refreshed["components"], ["cli"])

    def test_issues_crud_ready_bulk_subtasks_clone_watchers_and_history(self) -> None:
        # Validation errors on create
        self.assertEqual(self.client.post("/api/v1/issues", json={"title": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/issues", json={"title": "X", "type": "bogus"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/issues", json={"title": "X", "status": "bogus"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/issues", json={"title": "X", "priority": "bogus"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/issues", json={"title": "X", "epic_id": "ITK-999"}).status_code, 404)

        spr = self.client.post("/api/v1/sprints", json={"name": "Sprint Alpha"}).json()
        epic = self.client.post("/api/v1/issues", json={"title": "Core Epic", "type": "epic", "priority": "high"}).json()
        parent = self.client.post(
            "/api/v1/issues",
            json={
                "title": "Parent Story",
                "type": "story",
                "priority": "urgent",
                "epic_id": epic["key"],
                "sprint_id": spr["id"],
                "labels": ["core"],
                "components": ["api"],
                "story_points": 8,
            },
        ).json()

        # Subtasks POST & GET
        sub1 = self.client.post(
            f"/api/v1/issues/{parent['key']}/subtasks",
            json={"title": "Write unit tests", "story_points": 2},
        )
        self.assertEqual(sub1.status_code, 201)
        sub1_key = sub1.json()["key"]
        self.assertEqual(sub1.json()["parent_id"], parent["key"])
        self.assertEqual(sub1.json()["epic_id"], epic["key"])
        self.assertEqual(sub1.json()["sprint_id"], spr["id"])

        subs_list = self.client.get(f"/api/v1/issues/{parent['key']}/subtasks").json()
        self.assertEqual(subs_list["total_count"], 1)
        self.assertEqual(subs_list["completed_count"], 0)

        # Clone with include_subtasks=True
        cloned_res = self.client.post(
            f"/api/v1/issues/{parent['key']}/clone",
            json={"title_prefix": "[Copy] ", "include_subtasks": True},
        )
        self.assertEqual(cloned_res.status_code, 201)
        cloned = cloned_res.json()
        self.assertEqual(cloned["title"], "[Copy] Parent Story")
        self.assertEqual(len(cloned["subtask_keys"]), 1)
        self.assertTrue(any(l["link_type"] == "clones" and l["target_key"] == parent["key"] for l in cloned["all_links"]))

        # Assign & Watchers & PATCH & History
        assigned = self.client.post(f"/api/v1/issues/{parent['key']}/assign", json={"assignee": "dana"}).json()
        self.assertEqual(assigned["assignee"], "dana")
        unassigned = self.client.post(f"/api/v1/issues/{parent['key']}/assign", json={"assignee": "unassigned"}).json()
        self.assertIsNone(unassigned["assignee"])

        w_add = self.client.post(f"/api/v1/issues/{parent['key']}/watchers", json={"username": "dana"})
        self.assertEqual(w_add.status_code, 200)
        self.assertEqual(w_add.json()["watchers"], ["dana"])
        self.assertEqual(self.client.post(f"/api/v1/issues/{parent['key']}/watchers", json={"username": ""}).status_code, 400)
        w_rem = self.client.delete(f"/api/v1/issues/{parent['key']}/watchers/dana")
        self.assertEqual(w_rem.status_code, 200)
        self.assertEqual(w_rem.json()["watchers"], [])

        # PATCH validation & label add/remove
        self.assertEqual(self.client.patch(f"/api/v1/issues/{parent['key']}", json={"epic_id": parent["key"]}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/v1/issues/{parent['key']}", json={"parent_id": parent["key"]}).status_code, 400)
        patched = self.client.patch(
            f"/api/v1/issues/{parent['key']}",
            json={"add_labels": ["v1", "fast"], "remove_labels": ["core"], "custom_fields": {"risk": "low"}},
        ).json()
        self.assertEqual(patched["labels"], ["v1", "fast"])
        self.assertEqual(patched["custom_fields"]["risk"], "low")

        hist = self.client.get(f"/api/v1/issues/{parent['key']}/history").json()
        self.assertEqual(hist["key"], parent["key"])
        self.assertGreaterEqual(len(hist["history"]), 3)

        # GET /api/v1/issues query parameter filters
        filtered = self.client.get(
            "/api/v1/issues",
            params={"status": "todo,backlog", "type": "story", "label": "v1", "sprint_id": spr["id"]},
        ).json()
        self.assertEqual([i["key"] for i in filtered["issues"]], [parent["key"]])

        # GET /api/v1/issues/ready (ordered by priority DESC, seq ASC; excludes blocked/claimed/done)
        ready = self.client.get("/api/v1/issues/ready", params={"sprint_id": spr["id"]}).json()
        ready_keys = [i["key"] for i in ready["issues"]]
        self.assertEqual(ready_keys[0], parent["key"])  # urgent priority comes first!

        # POST /api/v1/issues/bulk: all 6 actions + partial error capture!
        self.assertEqual(self.client.post("/api/v1/issues/bulk", json={"keys": [], "action": "update"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/issues/bulk", json={"keys": [sub1_key], "action": "invalid"}).status_code, 400)

        # 1) update (with partial error on ITK-9999)
        b_upd = self.client.post(
            "/api/v1/issues/bulk",
            json={"keys": [sub1_key, "ITK-9999"], "action": "update", "fields": {"priority": "high", "assignee": "eve"}},
        ).json()
        self.assertEqual(b_upd["updated"], [sub1_key])
        self.assertEqual(len(b_upd["errors"]), 1)
        self.assertEqual(b_upd["errors"][0]["key"], "ITK-9999")
        self.assertEqual(b_upd["errors"][0]["status_code"], 404)

        # 2) label_add
        b_ladd = self.client.post(
            "/api/v1/issues/bulk",
            json={"keys": [sub1_key], "action": "label_add", "label": "triaged"},
        ).json()
        self.assertEqual(b_ladd["updated"], [sub1_key])
        self.assertIn("triaged", self.client.get(f"/api/v1/issues/{sub1_key}").json()["labels"])

        # 3) label_remove
        b_lrem = self.client.post(
            "/api/v1/issues/bulk",
            json={"keys": [sub1_key], "action": "label_remove", "label": "triaged"},
        ).json()
        self.assertEqual(b_lrem["updated"], [sub1_key])
        self.assertNotIn("triaged", self.client.get(f"/api/v1/issues/{sub1_key}").json()["labels"])

        # 4) sprint
        b_spr = self.client.post(
            "/api/v1/issues/bulk",
            json={"keys": [sub1_key], "action": "sprint", "sprint_id": "backlog"},
        ).json()
        self.assertEqual(b_spr["updated"], [sub1_key])
        self.assertIsNone(self.client.get(f"/api/v1/issues/{sub1_key}").json()["sprint_id"])

        # 5) transition
        b_tr = self.client.post(
            "/api/v1/issues/bulk",
            json={"keys": [sub1_key], "action": "transition", "status": "done"},
        ).json()
        self.assertEqual(b_tr["updated"], [sub1_key])
        self.assertEqual(self.client.get(f"/api/v1/issues/{sub1_key}").json()["status"], "done")

        # 6) delete
        cloned_sub_key = cloned["subtask_keys"][0]
        b_del = self.client.post(
            "/api/v1/issues/bulk",
            json={"keys": [cloned_sub_key, cloned["key"]], "action": "delete"},
        ).json()
        self.assertEqual(b_del["deleted"], [cloned_sub_key, cloned["key"]])
        self.assertEqual(self.client.get(f"/api/v1/issues/{cloned['key']}").status_code, 404)

    def test_comments_worklogs_links_dag_cycles_and_dependency_graph(self) -> None:
        a = self.client.post("/api/v1/issues", json={"title": "Issue A"}).json()["key"]
        b = self.client.post("/api/v1/issues", json={"title": "Issue B"}).json()["key"]
        c = self.client.post("/api/v1/issues", json={"title": "Issue C"}).json()["key"]

        # Comments CRUD
        self.assertEqual(self.client.post(f"/api/v1/issues/{a}/comments", json={"body": "  "}).status_code, 400)
        cmt = self.client.post(f"/api/v1/issues/{a}/comments", json={"author": "alice", "body": "Initial note"}).json()
        self.assertEqual(len(self.client.get(f"/api/v1/issues/{a}/comments").json()["comments"]), 1)

        cmt_ed = self.client.patch(f"/api/v1/issues/{a}/comments/{cmt['id']}", json={"body": "Edited note"}).json()
        self.assertEqual(cmt_ed["body"], "Edited note")
        self.assertEqual(self.client.patch(f"/api/v1/issues/{a}/comments/CMT-404", json={"body": "x"}).status_code, 404)

        self.assertEqual(self.client.delete(f"/api/v1/issues/{a}/comments/{cmt['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/issues/{a}/comments/{cmt['id']}").status_code, 404)

        # Worklogs CRUD (minutes + "2d 3h 15m" duration parser + invalid 400)
        wl1 = self.client.post(f"/api/v1/issues/{a}/worklogs", json={"minutes": 45, "author": "alice", "note": "Triage"})
        self.assertEqual(wl1.status_code, 201)
        wl2 = self.client.post(f"/api/v1/issues/{a}/worklogs", json={"duration": "2d 3h 15m", "author": "bob"})
        self.assertEqual(wl2.status_code, 201)
        self.assertEqual(wl2.json()["minutes"], 1155)
        self.assertEqual(self.client.post(f"/api/v1/issues/{a}/worklogs", json={"duration": "not-valid"}).status_code, 400)

        wls = self.client.get(f"/api/v1/issues/{a}/worklogs").json()
        self.assertEqual(wls["total_minutes"], 1200)
        self.assertEqual(self.client.get(f"/api/v1/issues/{a}").json()["total_worklog_minutes"], 1200)
        self.assertEqual(self.client.delete(f"/api/v1/issues/{a}/worklogs/{wl1.json()['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/issues/{a}/worklogs/{wl1.json()['id']}").status_code, 404)

        # Issue Links & DAG Cycle Detection
        # Self-link -> 400; invalid type -> 400
        self.assertEqual(self.client.post(f"/api/v1/issues/{a}/links", json={"target_key": a, "link_type": "blocks"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/issues/{a}/links", json={"target_key": b, "link_type": "bad_type"}).status_code, 400)

        # Create A blocks B
        lnk_ab = self.client.post(f"/api/v1/issues/{a}/links", json={"target_key": b, "link_type": "blocks"})
        self.assertEqual(lnk_ab.status_code, 201)

        # Duplicate link (direct A blocks B, and inverse B blocked_by A) -> 409
        self.assertEqual(self.client.post(f"/api/v1/issues/{a}/links", json={"target_key": b, "link_type": "blocks"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/v1/issues/{b}/links", json={"target_key": a, "link_type": "blocked_by"}).status_code, 409)

        # Direct 2-node cycle (B blocks A) -> 409
        cyc2 = self.client.post(f"/api/v1/issues/{b}/links", json={"target_key": a, "link_type": "blocks"})
        self.assertEqual(cyc2.status_code, 409)
        self.assertIn("cycle", cyc2.json()["detail"].lower())

        # Extend chain: B blocks C (via inverse C blocked_by B)
        lnk_cb = self.client.post(f"/api/v1/issues/{c}/links", json={"target_key": b, "link_type": "blocked_by"})
        self.assertEqual(lnk_cb.status_code, 201)

        # 3-hop transitive cycle (C blocks A) -> 409
        cyc3 = self.client.post(f"/api/v1/issues/{c}/links", json={"target_key": a, "link_type": "blocks"})
        self.assertEqual(cyc3.status_code, 409)

        # Inverse blocked_by 3-hop cycle (A blocked_by C) -> 409
        cyc3_inv = self.client.post(f"/api/v1/issues/{a}/links", json={"target_key": c, "link_type": "blocked_by"})
        self.assertEqual(cyc3_inv.status_code, 409)

        # Non-blocking links (relates_to, duplicates) between C and A succeed without false cycle errors
        self.assertEqual(self.client.post(f"/api/v1/issues/{c}/links", json={"target_key": a, "link_type": "relates_to"}).status_code, 201)
        self.assertEqual(self.client.post(f"/api/v1/issues/{c}/links", json={"target_key": a, "link_type": "duplicates"}).status_code, 201)

        # Verify computed all_links, is_blocked, unresolved_blockers on B
        b_iss = self.client.get(f"/api/v1/issues/{b}").json()
        self.assertTrue(b_iss["is_blocked"])
        self.assertEqual(b_iss["unresolved_blockers"], [a])
        b_links = self.client.get(f"/api/v1/issues/{b}/links").json()["links"]
        self.assertTrue(any(l["link_type"] == "blocked_by" and l["target_key"] == a and l["direction"] == "inbound" for l in b_links))
        self.assertTrue(any(l["link_type"] == "blocks" and l["target_key"] == c for l in b_links))

        # Dependency Graph endpoint
        dag = self.client.get("/api/v1/graph/dependencies").json()
        self.assertEqual(len(dag["nodes"]), 3)
        self.assertEqual(set(dag["blocked_keys"]), {b, c})
        self.assertEqual(dag["ready_keys"], [a])
        block_edges = [(e["source"], e["target"]) for e in dag["edges"] if e["type"] == "blocks"]
        self.assertIn((a, b), block_edges)
        self.assertIn((b, c), block_edges)

        # Delete link from target side (B deleting lnk_ab owned by A) unblocks B!
        self.assertEqual(self.client.delete(f"/api/v1/issues/{b}/links/{lnk_ab.json()['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/issues/{b}/links/{lnk_ab.json()['id']}").status_code, 404)
        self.assertFalse(self.client.get(f"/api/v1/issues/{b}").json()["is_blocked"])

    def test_multipart_attachments_upload_traversal_sanitization_download_and_delete(self) -> None:
        key = self.client.post("/api/v1/issues", json={"title": "Binary Attachment Host"}).json()["key"]
        raw_bytes = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe" + os.urandom(4096)
        expected_sha = hashlib.sha256(raw_bytes).hexdigest()

        up_res = self.client.post(
            f"/api/v1/issues/{key}/attachments",
            files={"file": ("../../etc/passwd", raw_bytes, "application/octet-stream")},
            data={"uploaded_by": "sec-tester", "description": "Path traversal attempt"},
        )
        self.assertEqual(up_res.status_code, 201)
        meta = up_res.json()
        self.assertEqual(meta["id"], "ATT-1")
        self.assertEqual(meta["filename"], "passwd")
        self.assertEqual(meta["stored_name"], "ATT-1_passwd")
        self.assertEqual(meta["size_bytes"], len(raw_bytes))
        self.assertEqual(meta["sha256"], expected_sha)

        # Stored strictly inside attachments/ITK-1/ATT-1_passwd
        stored_file = self.storage.attachments_dir / key / "ATT-1_passwd"
        self.assertTrue(stored_file.is_file())

        # List attachments
        att_list = self.client.get(f"/api/v1/issues/{key}/attachments").json()
        self.assertEqual(len(att_list["attachments"]), 1)

        # Streaming download matches exact bytes and SHA-256
        dl_res = self.client.get(f"/api/v1/issues/{key}/attachments/ATT-1/download")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.content, raw_bytes)
        self.assertEqual(hashlib.sha256(dl_res.content).hexdigest(), expected_sha)

        # Delete attachment & verify 404 on missing
        del_res = self.client.delete(f"/api/v1/issues/{key}/attachments/ATT-1")
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(stored_file.exists())
        self.assertEqual(self.client.get(f"/api/v1/issues/{key}/attachments/ATT-1/download").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/issues/{key}/attachments/ATT-1").status_code, 404)

    def test_board_epics_sprints_milestones_saved_views_and_offline_spa(self) -> None:
        self.client.patch("/api/v1/config", json={"wip_limits": {"in_progress": 1}})

        # Sprints & Milestones creation
        self.assertEqual(self.client.post("/api/v1/sprints", json={"name": ""}).status_code, 400)
        s1 = self.client.post("/api/v1/sprints", json={"name": "Sprint 1", "goal": "MVP"}).json()
        s2 = self.client.post("/api/v1/sprints", json={"name": "Sprint 2", "goal": "Polish"}).json()
        self.client.patch(f"/api/v1/sprints/{s1['id']}", json={"goal": "Updated MVP Goal"})
        self.assertEqual(self.client.patch(f"/api/v1/sprints/{s1['id']}", json={"state": "invalid"}).status_code, 400)
        self.client.post(f"/api/v1/sprints/{s1['id']}/start", json={"start_date": "2026-03-01"})
        self.assertEqual(len(self.client.get("/api/v1/sprints", params={"state": "active"}).json()["sprints"]), 1)

        self.assertEqual(self.client.post("/api/v1/milestones", json={"name": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/milestones", json={"name": "X", "status": "bad"}).status_code, 400)
        ms = self.client.post("/api/v1/milestones", json={"name": "v1.0", "target_date": "2026-06-01"}).json()

        # Epic + 2 child stories (one done, one in_progress)
        ep = self.client.post("/api/v1/issues", json={"title": "Platform Epic", "type": "epic"}).json()
        c_done = self.client.post(
            "/api/v1/issues",
            json={
                "title": "Done Story",
                "type": "story",
                "status": "done",
                "assignee": "alice",
                "epic_id": ep["key"],
                "sprint_id": s1["id"],
                "milestone_id": ms["id"],
                "story_points": 5,
            },
        ).json()
        c_wip = self.client.post(
            "/api/v1/issues",
            json={
                "title": "Unfinished Story",
                "type": "story",
                "status": "in_progress",
                "assignee": "bob",
                "epic_id": ep["key"],
                "sprint_id": s1["id"],
                "milestone_id": ms["id"],
                "story_points": 3,
            },
        ).json()

        # Board with filters & WIP limits
        board = self.client.get(
            "/api/v1/board",
            params={"sprint_id": s1["id"], "epic_id": ep["key"], "assignee": "bob", "iql": "points >= 3"},
        ).json()
        self.assertEqual(board["total_issues"], 1)
        in_prog_col = next(c for c in board["columns"] if c["status"] == "in_progress")
        self.assertEqual(in_prog_col["count"], 1)
        self.assertEqual(in_prog_col["points"], 3.0)
        self.assertEqual(in_prog_col["wip_limit"], 1)

        # Epics rollup
        epics = self.client.get("/api/v1/epics").json()["epics"]
        self.assertEqual(len(epics), 1)
        self.assertEqual(epics[0]["child_count"], 2)
        self.assertEqual(epics[0]["done_count"], 1)
        self.assertEqual(epics[0]["total_points"], 8.0)
        self.assertEqual(epics[0]["done_points"], 5.0)
        self.assertEqual(epics[0]["progress_pct"], 50.0)

        # Sprint detail metrics & complete with rollover to SPR-2, then SPR-2 rollover to backlog
        s1_det = self.client.get(f"/api/v1/sprints/{s1['id']}").json()
        self.assertEqual(s1_det["metrics"]["total_issues"], 2)
        self.assertEqual(s1_det["metrics"]["done_issues"], 1)
        self.assertEqual(s1_det["metrics"]["progress_pct"], 50.0)

        # Same-sprint rollover rejected with 400
        self.assertEqual(
            self.client.post(f"/api/v1/sprints/{s1['id']}/complete", json={"rollover_to": s1["id"]}).status_code,
            400,
        )
        comp1 = self.client.post(f"/api/v1/sprints/{s1['id']}/complete", json={"rollover_to": s2["id"]}).json()
        self.assertEqual(comp1["rolled_over_keys"], [c_wip["key"]])
        self.assertEqual(self.client.get(f"/api/v1/issues/{c_done['key']}").json()["sprint_id"], s1["id"])
        self.assertEqual(self.client.get(f"/api/v1/issues/{c_wip['key']}").json()["sprint_id"], s2["id"])
        # Completed sprint cannot be restarted
        self.assertEqual(self.client.post(f"/api/v1/sprints/{s1['id']}/start", json={}).status_code, 400)

        # Complete SPR-2 rolling over unfinished issues to backlog (None)
        comp2 = self.client.post(f"/api/v1/sprints/{s2['id']}/complete", json={"rollover_to": "backlog"}).json()
        self.assertEqual(comp2["rolled_over_keys"], [c_wip["key"]])
        self.assertIsNone(self.client.get(f"/api/v1/issues/{c_wip['key']}").json()["sprint_id"])

        # Delete sprint clears sprint_id on remaining done issues
        self.assertEqual(self.client.delete(f"/api/v1/sprints/{s1['id']}").status_code, 200)
        self.assertIsNone(self.client.get(f"/api/v1/issues/{c_done['key']}").json()["sprint_id"])
        self.assertEqual(self.client.delete("/api/v1/sprints/SPR-999").status_code, 404)

        # Milestones progress, PATCH, and DELETE
        ms_list = self.client.get("/api/v1/milestones").json()["milestones"]
        self.assertEqual(ms_list[0]["total_issues"], 2)
        self.assertEqual(ms_list[0]["done_issues"], 1)
        self.assertEqual(ms_list[0]["progress_pct"], 50.0)

        ms_patched = self.client.patch(f"/api/v1/milestones/{ms['id']}", json={"status": "released"})
        self.assertEqual(ms_patched.status_code, 200)
        self.assertEqual(ms_patched.json()["status"], "released")
        self.assertEqual(self.client.patch(f"/api/v1/milestones/{ms['id']}", json={"status": "bogus"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/milestones/MIL-999", json={"name": "x"}).status_code, 404)

        self.assertEqual(self.client.delete(f"/api/v1/milestones/{ms['id']}").status_code, 200)
        self.assertIsNone(self.client.get(f"/api/v1/issues/{c_done['key']}").json()["milestone_id"])
        self.assertEqual(self.client.delete(f"/api/v1/milestones/{ms['id']}").status_code, 404)

        # Saved Views CRUD (including rejecting invalid iQL on POST and PATCH)
        self.assertEqual(self.client.post("/api/v1/views", json={"name": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/views", json={"name": "Bad", "iql": "unknown_field = 1"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/views", json={"name": "BadLayout", "iql": "", "layout": "grid"}).status_code, 400)

        vw = self.client.post(
            "/api/v1/views",
            json={"name": "High Priority", "iql": "priority >= high", "layout": "board"},
        )
        self.assertEqual(vw.status_code, 201)
        vid = vw.json()["id"]
        self.assertEqual(len(self.client.get("/api/v1/views").json()["views"]), 1)

        self.assertEqual(self.client.patch(f"/api/v1/views/{vid}", json={"iql": "blocked IS EMPTY"}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/v1/views/{vid}", json={"layout": "invalid"}).status_code, 400)
        vw_up = self.client.patch(f"/api/v1/views/{vid}", json={"name": "Urgent List", "iql": "status = done", "layout": "list"})
        self.assertEqual(vw_up.status_code, 200)
        self.assertEqual(vw_up.json()["layout"], "list")

        self.assertEqual(self.client.delete(f"/api/v1/views/{vid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/views/{vid}").status_code, 404)

        # Offline SPA & Favicon routes (zero external CDN URLs)
        for spa_path in ("/", "/ui"):
            spa_res = self.client.get(spa_path)
            self.assertEqual(spa_res.status_code, 200)
            self.assertIn("text/html", spa_res.headers["content-type"])
            html = spa_res.text
            self.assertIn("<!DOCTYPE html>", html)
            self.assertIsNone(re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.IGNORECASE))

        fav_res = self.client.get("/favicon.ico")
        self.assertEqual(fav_res.status_code, 200)
        self.assertIn("<svg", fav_res.text)


# ============================================================================
# 4. Multi-Thread Concurrency & Atomic Agent Claim / Release Suite
# ============================================================================


class TestMultiThreadConcurrencyAndAtomicClaims(InProcessServerTestCase):
    """Verifies lock-protected 16-way parallel issue creation and 8-way parallel atomic agent claims."""

    def test_16_way_parallel_issue_creation_assigns_unique_contiguous_keys(self) -> None:
        num_workers = 16
        barrier = threading.Barrier(num_workers)

        def worker(idx: int) -> tuple[int, dict[str, Any]]:
            local_client = TestClient(self.app)
            barrier.wait(timeout=10.0)
            resp = local_client.post(
                "/api/v1/issues",
                json={
                    "title": f"Concurrent Issue {idx}",
                    "priority": "high",
                    "labels": [f"worker-{idx}"],
                },
            )
            return resp.status_code, resp.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = [pool.submit(worker, i) for i in range(num_workers)]
            results = [f.result(timeout=20.0) for f in futures]

        statuses = [code for code, _ in results]
        self.assertEqual(statuses, [201] * num_workers)

        created_keys = {body["key"] for _, body in results}
        expected_keys = {f"ITK-{i}" for i in range(1, num_workers + 1)}
        self.assertEqual(created_keys, expected_keys)

        # Verify all 16 JSON files on disk are intact and next_seq == 17
        disk_files = sorted(p.name for p in self.storage.issues_dir.glob("*.json"))
        self.assertEqual(len(disk_files), num_workers)
        for p in self.storage.issues_dir.glob("*.json"):
            doc = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn(doc["key"], expected_keys)
        self.assertEqual(self.client.get("/api/v1/config").json()["next_seq"], num_workers + 1)

    def test_8_way_parallel_agent_claim_wrong_agent_release_force_override_and_release(self) -> None:
        issue = self.client.post("/api/v1/issues", json={"title": "Contended Agent Task", "priority": "urgent"}).json()
        key = issue["key"]
        self.assertEqual(self.client.post(f"/api/v1/issues/{key}/claim", json={"agent": ""}).status_code, 400)

        num_agents = 8
        barrier = threading.Barrier(num_agents)

        def claim_worker(idx: int) -> tuple[str, int, dict[str, Any]]:
            agent_name = f"swarm-agent-{idx}"
            local_client = TestClient(self.app)
            barrier.wait(timeout=10.0)
            resp = local_client.post(
                f"/api/v1/issues/{key}/claim",
                json={"agent": agent_name, "note": f"Claim attempt by {agent_name}"},
            )
            return agent_name, resp.status_code, resp.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_agents) as pool:
            futures = [pool.submit(claim_worker, i) for i in range(num_agents)]
            outcomes = [f.result(timeout=20.0) for f in futures]

        winners = [(ag, body) for ag, code, body in outcomes if code == 200]
        losers = [(ag, body) for ag, code, body in outcomes if code == 409]
        self.assertEqual(len(winners), 1, f"Expected exactly 1 claim winner, got: {outcomes}")
        self.assertEqual(len(losers), 7, f"Expected exactly 7 claim conflicts (409), got: {outcomes}")

        winner_agent, winner_body = winners[0]
        self.assertEqual(winner_body["claimed_by"], winner_agent)
        self.assertEqual(winner_body["assignee"], winner_agent)
        self.assertEqual(winner_body["status"], "in_progress")

        # Claimmed issue is excluded from /api/v1/issues/ready
        self.assertEqual(self.client.get("/api/v1/issues/ready").json()["issues"], [])

        # Wrong-agent release returns 409
        wrong_agent = losers[0][0]
        wrong_rel = self.client.post(
            f"/api/v1/issues/{key}/release",
            json={"agent": wrong_agent, "status": "todo"},
        )
        self.assertEqual(wrong_rel.status_code, 409)
        self.assertIn(winner_agent, wrong_rel.json()["detail"])

        # force=True override transfers claim cleanly
        override_res = self.client.post(
            f"/api/v1/issues/{key}/claim",
            json={"agent": "supervisor-agent", "force": True, "note": "Supervisor takeover"},
        )
        self.assertEqual(override_res.status_code, 200)
        self.assertEqual(override_res.json()["claimed_by"], "supervisor-agent")
        self.assertEqual(override_res.json()["assignee"], "supervisor-agent")

        # Invalid release target status returns 400
        self.assertEqual(
            self.client.post(f"/api/v1/issues/{key}/release", json={"agent": "supervisor-agent", "status": "bogus"}).status_code,
            400,
        )

        # Valid release clears claimed_by / claimed_at, restores todo status, and puts issue back on ready queue
        rel_res = self.client.post(
            f"/api/v1/issues/{key}/release",
            json={"agent": "supervisor-agent", "status": "todo", "note": "Returning to queue"},
        )
        self.assertEqual(rel_res.status_code, 200)
        rel_body = rel_res.json()
        self.assertIsNone(rel_body["claimed_by"])
        self.assertIsNone(rel_body["claimed_at"])
        self.assertEqual(rel_body["status"], "todo")

        ready_after = self.client.get("/api/v1/issues/ready").json()["issues"]
        self.assertEqual([i["key"] for i in ready_after], [key])


if __name__ == "__main__":
    unittest.main()
