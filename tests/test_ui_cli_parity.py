"""Bidirectional UI <-> CLI Feature Parity & Offline Air-Gap Playwright Suite for itrack.

Covers:
  1. 100% Air-Gapped Offline & Zero-Error Invariants (zero external HTTP/HTTPS requests,
     zero JS pageerror exceptions, zero unexpected browser console.error messages).
  2. CLI -> UI Parity (seed & mutate via `itrack.py` CLI, verify live DOM across Kanban Board,
     Issue List, Ready Queue dynamic unblocking, Sprints, Epics & Milestones, SVG Dependency DAG,
     Analytics & Settings, Saved Views, and Slide-Over Issue Detail Drawer).
  3. UI -> CLI Parity (mutate via Headless Chrome across all 15 interactive SPA flows and
     verify persisted canonical state back via `itrack.py ... --json`).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Iterator

sys.dont_write_bytecode = True

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from tests.helpers import (
    cleanup_repo_daemon,
    find_chrome_executable,
    init_git_repo,
    run_itrack_cli,
    run_itrack_json,
)


class _PlaywrightParityBase(unittest.TestCase):
    """Shared isolated Git repo + daemon + headless Chrome harness with strict air-gap & error telemetry."""

    _tmp_dir: str
    repo_dir: Path
    base_url: str
    _pw: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    total_requests: list[str]
    external_requests: list[dict[str, str]]
    page_errors: list[str]
    console_errors: list[dict[str, Any]]
    expected_http_console_errors: list[dict[str, Any]]
    _allowed_console_http_statuses: set[int]
    _inflight_requests: int
    _last_net_ts: float

    @classmethod
    def cli_cls(
        cls,
        *args: str,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return run_itrack_cli(cls.repo_dir, *args, cwd=cwd, env_extra=env_extra)

    @classmethod
    def cli_json_cls(
        cls,
        *args: str,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        expect_rc: int | None = 0,
    ) -> Any:
        _, data, _ = run_itrack_json(cls.repo_dir, *args, cwd=cwd, env_extra=env_extra, expect_rc=expect_rc)
        return data

    def cli(
        self,
        *args: str,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.cli_cls(*args, cwd=cwd, env_extra=env_extra)

    def cli_json(
        self,
        *args: str,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        expect_rc: int | None = 0,
    ) -> Any:
        return self.cli_json_cls(*args, cwd=cwd, env_extra=env_extra, expect_rc=expect_rc)

    @classmethod
    def _seed_workspace(cls) -> None:
        """Override in subclasses to seed CLI data prior to launching the browser."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmp_dir = tempfile.mkdtemp(prefix="itrack_ui_cli_parity_")
        cls.repo_dir = init_git_repo(Path(cls._tmp_dir))

        cls.total_requests = []
        cls.external_requests = []
        cls.page_errors = []
        cls.console_errors = []
        cls.expected_http_console_errors = []
        cls._allowed_console_http_statuses = set()
        cls._inflight_requests = 0
        cls._last_net_ts = time.monotonic()

        try:
            cls._seed_workspace()
            ui_meta = cls.cli_json_cls("ui", "--no-open")
            cls.base_url = str(ui_meta["url"]).rstrip("/")

            cls._pw = sync_playwright().start()
            chrome_bin = find_chrome_executable()
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-gpu"],
            }
            if chrome_bin:
                launch_kwargs["executable_path"] = chrome_bin
            cls.browser = cls._pw.chromium.launch(**launch_kwargs)
            cls.context = cls.browser.new_context(viewport={"width": 1440, "height": 900})
            cls.page = cls.context.new_page()

            def _on_request(req: Any) -> None:
                url = req.url
                cls.total_requests.append(url)
                if url.startswith(cls.base_url):
                    cls._inflight_requests += 1
                    cls._last_net_ts = time.monotonic()
                if not (url.startswith(cls.base_url) or url.startswith("data:") or url.startswith("blob:")):
                    cls.external_requests.append(
                        {
                            "url": url,
                            "method": req.method,
                            "resource_type": req.resource_type,
                        }
                    )

            def _on_request_done(req: Any) -> None:
                if req.url.startswith(cls.base_url):
                    cls._inflight_requests = max(0, cls._inflight_requests - 1)
                    cls._last_net_ts = time.monotonic()

            def _on_pageerror(err: Any) -> None:
                cls.page_errors.append(str(err))

            def _on_console(msg: Any) -> None:
                if msg.type != "error":
                    return
                entry = {"text": msg.text, "location": msg.location}
                matched_expected = False
                for code in list(cls._allowed_console_http_statuses):
                    if f"status of {code}" in msg.text:
                        matched_expected = True
                        break
                if matched_expected:
                    cls.expected_http_console_errors.append(entry)
                else:
                    cls.console_errors.append(entry)

            cls.page.on("request", _on_request)
            cls.page.on("requestfinished", _on_request_done)
            cls.page.on("requestfailed", _on_request_done)
            cls.page.on("pageerror", _on_pageerror)
            cls.page.on("console", _on_console)

            cls.page.goto(cls.base_url, wait_until="networkidle")
            cls.page.wait_for_selector("#view-board.active .board-col", timeout=10000)
        except Exception:
            cls._cleanup_all()
            raise

    @classmethod
    def _cleanup_all(cls) -> None:
        for attr, method in (("context", "close"), ("browser", "close"), ("_pw", "stop")):
            obj = getattr(cls, attr, None)
            if obj is not None:
                with contextlib.suppress(Exception):
                    getattr(obj, method)()
                setattr(cls, attr, None)
        if hasattr(cls, "repo_dir"):
            with contextlib.suppress(Exception):
                cleanup_repo_daemon(cls.repo_dir)
        if hasattr(cls, "_tmp_dir"):
            shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._cleanup_all()
        finally:
            super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        self.close_overlays()
        self.wait_for_api_quiet()

    def tearDown(self) -> None:
        try:
            self.assertEqual(
                self.external_requests,
                [],
                f"Air-gapped SPA invariant violated! External requests detected: {self.external_requests}",
            )
            self.assertEqual(
                self.page_errors,
                [],
                f"Uncaught JS pageerror detected: {self.page_errors}",
            )
            self.assertEqual(
                self.console_errors,
                [],
                f"Unexpected browser console.error detected: {self.console_errors}",
            )
        finally:
            super().tearDown()

    @contextlib.contextmanager
    def allow_expected_console_http_status(self, status_code: int) -> Iterator[None]:
        """Allow a deliberate negative API test (e.g. HTTP 409 cycle check) without failing console_errors."""
        self.__class__._allowed_console_http_statuses.add(status_code)
        try:
            yield
        finally:
            self.__class__._allowed_console_http_statuses.discard(status_code)

    def wait_for_api_quiet(self, quiet_ms: float = 90.0, timeout: float = 10.0) -> None:
        """Pump Playwright's sync loop until all cascading fetch() calls have settled."""
        deadline = time.monotonic() + timeout
        self.page.wait_for_timeout(30)
        while time.monotonic() < deadline:
            idle_ms = (time.monotonic() - self.__class__._last_net_ts) * 1000.0
            if self.__class__._inflight_requests == 0 and idle_ms >= quiet_ms:
                return
            self.page.wait_for_timeout(25)

    def switch_nav(self, view_name: str) -> None:
        self.close_overlays()
        self.page.click(f"#nav-{view_name}")
        self.page.wait_for_selector(f"#view-{view_name}.active", timeout=8000)
        self.wait_for_api_quiet()

    def close_overlays(self) -> None:
        is_open = self.page.evaluate(
            "document.getElementById('create-issue-modal').classList.contains('open') || "
            "document.getElementById('cmd-palette-modal').classList.contains('open') || "
            "document.getElementById('issue-drawer-backdrop').classList.contains('open')"
        )
        if is_open:
            self.page.keyboard.press("Escape")
            self.page.wait_for_function(
                "!document.getElementById('create-issue-modal').classList.contains('open') && "
                "!document.getElementById('cmd-palette-modal').classList.contains('open') && "
                "!document.getElementById('issue-drawer-backdrop').classList.contains('open')"
            )


# ===========================================================================
# 1. CLI -> UI Parity Suite
# ===========================================================================


class TestCliToUiParity(_PlaywrightParityBase):
    """Mutate workspace via `itrack.py` CLI and verify live Headless Chrome DOM rendering."""

    seed_attachment_bytes: bytes
    seed_attachment_sha256: str

    @classmethod
    def _seed_workspace(cls) -> None:
        cls.cli_json_cls("init", "--key", "ITK", "--name", "Parity Core Engine")

        # Users & AI agent
        cls.cli_json_cls("user", "add", "alice", "--name", "Alice Chen", "--email", "alice@itrack.local", "--role", "Staff Engineer")
        cls.cli_json_cls("user", "add", "bob", "--name", "Bob Martinez", "--email", "bob@itrack.local", "--role", "UI Engineer")
        cls.cli_json_cls("user", "add", "swarm-bot", "--name", "Swarm Bot", "--role", "Autonomous Agent", "--agent")

        # Colored labels & system components
        cls.cli_json_cls("label", "add", "security", "--color", "#ef4444", "-d", "Locking & security")
        cls.cli_json_cls("label", "add", "frontend", "--color", "#3b82f6", "-d", "Offline Web SPA")
        cls.cli_json_cls("label", "add", "fts5", "--color", "#10b981", "-d", "SQLite FTS5 indexing")

        cls.cli_json_cls("component", "add", "api", "--lead", "alice", "-d", "FastAPI + flock store")
        cls.cli_json_cls("component", "add", "web-ui", "--lead", "bob", "-d", "Embedded SPA")

        # Milestone & Active Sprint
        cls.cli_json_cls("milestone", "create", "v1.0 GA", "-d", "General availability release", "--target", "2026-12-31")
        cls.cli_json_cls(
            "sprint",
            "create",
            "Sprint 1 — Parity",
            "--goal",
            "Verify CLI to UI bidirectional parity",
            "--start",
            "2026-11-17",
            "--end",
            "2026-12-01",
        )
        cls.cli_json_cls("sprint", "start", "SPR-1")

        # ITK-1: Epic
        cls.cli_json_cls("epic", "create", "Bidirectional Parity Rollout", "-d", "Epic tracking full CLI/UI parity.", "-p", "high", "-a", "alice")
        cls.cli_json_cls("move", "ITK-1", "in_progress", "-m", "Epic active")

        # ITK-2: Done story (8 pts)
        cls.cli_json_cls(
            "create",
            "Implement atomic flock JSON persistence",
            "-d",
            "Exclusive lock + fsync + os.replace.",
            "-t",
            "story",
            "-p",
            "urgent",
            "-a",
            "alice",
            "-l",
            "security",
            "-c",
            "api",
            "--epic",
            "ITK-1",
            "--sprint",
            "SPR-1",
            "--milestone",
            "MIL-1",
            "--points",
            "8",
        )
        cls.cli_json_cls("close", "ITK-2", "--resolution", "fixed", "-m", "Shipped")

        # ITK-3: Claimed bug in_progress (5 pts) — will block ITK-7
        cls.cli_json_cls(
            "create",
            "Fix FTS5 hyphenated token prefix query",
            "-d",
            "Handle `ITK-3` style hyphenated tokens cleanly.",
            "-t",
            "bug",
            "-p",
            "urgent",
            "-a",
            "swarm-bot",
            "-l",
            "fts5,security",
            "-c",
            "api",
            "--epic",
            "ITK-1",
            "--sprint",
            "SPR-1",
            "--milestone",
            "MIL-1",
            "--points",
            "5",
        )
        cls.cli_json_cls("claim", "ITK-3", "--agent", "swarm-bot", "-m", "Claimed by autonomous agent")

        # ITK-4 (done subtask) & ITK-5 (todo subtask) under ITK-3
        cls.cli_json_cls("issue", "subtask-add", "ITK-3", "Add tokenizer unit tests", "-p", "high")
        cls.cli_json_cls("close", "ITK-4", "--resolution", "fixed")
        cls.cli_json_cls("issue", "subtask-add", "ITK-3", "Verify quote escaping in MATCH", "-p", "medium")

        # ITK-6: Unblocked todo task (3 pts) -> in Ready Queue
        cls.cli_json_cls(
            "create",
            "Add keyboard shortcut hints to sidebar",
            "-t",
            "task",
            "-p",
            "high",
            "-s",
            "todo",
            "-a",
            "alice",
            "-l",
            "frontend",
            "-c",
            "web-ui",
            "--epic",
            "ITK-1",
            "--sprint",
            "SPR-1",
            "--points",
            "3",
        )

        # ITK-7: Todo task blocked by open ITK-3 -> excluded from Ready Queue until ITK-3 closes!
        cls.cli_json_cls(
            "create",
            "Wire live FTS5 snippets into Command Palette",
            "-t",
            "task",
            "-p",
            "high",
            "-s",
            "todo",
            "-a",
            "bob",
            "-l",
            "frontend,fts5",
            "-c",
            "web-ui",
            "--epic",
            "ITK-1",
            "--sprint",
            "SPR-1",
            "--points",
            "3",
        )

        # ITK-8: Unassigned backlog spike with story points (tests no leading '· ' on unassigned card)
        cls.cli_json_cls(
            "create",
            "Benchmark 10k issue SQLite WAL reindex",
            "-t",
            "spike",
            "-p",
            "medium",
            "-s",
            "backlog",
            "-l",
            "fts5",
            "--points",
            "2",
        )

        # ITK-9: In-review story
        cls.cli_json_cls(
            "create",
            "Audit offline CSS custom properties",
            "-t",
            "story",
            "-p",
            "low",
            "-s",
            "in_review",
            "-a",
            "bob",
            "-l",
            "frontend",
            "-c",
            "web-ui",
            "--points",
            "1",
        )

        # Links: ITK-3 blocks ITK-7 (active blocker), ITK-2 blocks ITK-9 (resolved blocker), ITK-9 relates_to ITK-8
        cls.cli_json_cls("link", "add", "ITK-3", "blocks", "ITK-7")
        cls.cli_json_cls("link", "add", "ITK-2", "blocks", "ITK-9")
        cls.cli_json_cls("link", "add", "ITK-9", "relates_to", "ITK-8")

        # Markdown comment, binary attachment, and worklog on ITK-3
        cls.cli_json_cls(
            "comment",
            "add",
            "ITK-3",
            "CLI-seeded **Markdown** note verifying `build_fts_match_expr`.",
            "--author",
            "swarm-bot",
        )
        cls.seed_attachment_bytes = b"\x89ITRACK\x00\xff\x10CLI-BINARY-PAYLOAD-PARITY-TEST\n" * 6
        cls.seed_attachment_sha256 = hashlib.sha256(cls.seed_attachment_bytes).hexdigest()
        seed_bin_path = cls.repo_dir / "cli_seed_artifact.bin"
        seed_bin_path.write_bytes(cls.seed_attachment_bytes)
        cls.cli_json_cls("attach", "upload", "ITK-3", str(seed_bin_path), "--by", "alice", "-d", "Binary seed payload")

        cls.cli_json_cls("worklog", "add", "ITK-3", "1h 15m", "-m", "CLI profiling session", "--author", "swarm-bot")

        # Saved iQL view via CLI
        cls.cli_json_cls(
            "view",
            "create",
            "CLI High & Urgent Open",
            "--iql",
            "priority >= high AND status != done",
            "--layout",
            "board",
        )

    def test_01_airgapped_initial_load_and_sidebar_blocked_badge(self) -> None:
        """Initial page load makes 0 external requests, logs 0 console errors, and populates #cnt-blocked immediately."""
        self.assertGreaterEqual(len(self.total_requests), 8)
        self.assertEqual(self.external_requests, [])
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.console_errors, [])

        self.assertEqual(self.page.locator("#workspace-project-name").inner_text().strip(), "Parity Core Engine")
        self.assertEqual(self.page.locator("#workspace-project-key").inner_text().strip(), "ITK")

        # Must reflect blocked count (ITK-7 blocked by ITK-3) on initial load WITHOUT clicking Graph/Stats first!
        self.assertEqual(self.page.locator("#cnt-blocked").inner_text().strip(), "1")
        self.assertEqual(self.page.locator("#cnt-sprints").inner_text().strip(), "1")
        self.assertEqual(self.page.locator("#cnt-epics").inner_text().strip(), "1")
        self.assertIn("CLI High & Urgent Open", self.page.locator("#saved-views-list").inner_text())

    def test_02_kanban_board_columns_badges_unassigned_points_and_sprint_pills(self) -> None:
        """Kanban board renders all 7 columns, claim/blocked/priority badges, clean unassigned points, and sprint filters."""
        self.switch_nav("board")
        cols = self.page.locator("#board-columns .board-col")
        self.assertEqual(cols.count(), 7)

        # ITK-3 card: urgent badge, @swarm-bot claim badge, swarm-bot · 5p
        card_itk3 = self.page.locator("#board-columns .issue-card[data-key='ITK-3']")
        self.assertTrue(card_itk3.is_visible())
        self.assertEqual(card_itk3.locator(".prio-badge").inner_text().strip().lower(), "urgent")
        self.assertIn("@swarm-bot", card_itk3.locator(".tag-claimed").inner_text())
        self.assertIn("swarm-bot · 5p", card_itk3.inner_text())

        # ITK-7 card: blocked badge via open blocker ITK-3
        card_itk7 = self.page.locator("#board-columns .issue-card[data-key='ITK-7']")
        self.assertTrue(card_itk7.locator(".tag-blocked").is_visible())

        # ITK-8 card: unassigned with 2 story points -> must render '2p' WITHOUT leading '· '
        card_itk8 = self.page.locator("#board-columns .issue-card[data-key='ITK-8']")
        meta_right_text = card_itk8.locator(".card-meta > span").inner_text().strip()
        self.assertEqual(meta_right_text, "2p")
        self.assertFalse(meta_right_text.startswith("·"), f"Unexpected leading middle dot: {meta_right_text!r}")

        # Sprint filter pills: filter by SPR-1 excludes unscheduled ITK-8, All Sprints restores it
        self.page.locator("#board-filter-pills .pill[data-sp='SPR-1']").click()
        self.wait_for_api_quiet()
        self.page.wait_for_function(
            "!document.querySelector(\"#board-columns .issue-card[data-key='ITK-8']\")"
        )
        self.assertEqual(self.page.locator("#board-columns .issue-card[data-key='ITK-3']").count(), 1)

        self.page.locator("#board-filter-pills .pill[data-sp='']").click()
        self.page.wait_for_selector("#board-columns .issue-card[data-key='ITK-8']", timeout=5000)

    def test_03_list_view_rows_badges_and_fts5_search(self) -> None:
        """Linear list view renders all CLI-seeded issues, blocked badges, points, and FTS5 comment search."""
        self.switch_nav("list")
        rows = self.page.locator("#list-tbody tr[data-key]")
        self.assertEqual(rows.count(), 9)

        row_itk7 = self.page.locator("#list-tbody tr[data-key='ITK-7']")
        self.assertIn("Blocked", row_itk7.inner_text())

        # FTS5 search matching text inside ITK-3's CLI-seeded comment
        self.page.fill("#list-fts-input", "build_fts_match_expr")
        self.page.click("#btn-list-fts")
        self.page.wait_for_function(
            "document.querySelectorAll('#list-tbody tr[data-key]').length === 1"
        )
        self.assertEqual(
            self.page.locator("#list-tbody tr[data-key]").first.get_attribute("data-key"),
            "ITK-3",
        )

        # Clear FTS search
        self.page.fill("#list-fts-input", "")
        self.page.click("#btn-list-fts")
        self.page.wait_for_function(
            "document.querySelectorAll('#list-tbody tr[data-key]').length === 9"
        )

    def test_04_sprints_epics_graph_stats_and_saved_views_parity(self) -> None:
        """Sprints, Epics/Milestones, Dependency SVG DAG, Analytics/Settings, and Saved Views match CLI state."""
        # Sprints view
        self.switch_nav("sprints")
        sprints_text = self.page.locator("#sprints-container").inner_text()
        self.assertIn("SPR-1", sprints_text)
        self.assertIn("Sprint 1 — Parity", sprints_text)
        backlog_text = self.page.locator("#backlog-planner-list").inner_text()
        self.assertIn("ITK-8", backlog_text)
        self.assertIn("ITK-9", backlog_text)

        # Epics & Milestones view
        self.switch_nav("epics")
        epics_text = self.page.locator("#epics-list-container").inner_text()
        self.assertIn("ITK-1", epics_text)
        self.assertIn("Bidirectional Parity Rollout", epics_text)
        self.assertIn("ITK-2", epics_text)
        ms_text = self.page.locator("#milestones-list-container").inner_text()
        self.assertIn("MIL-1", ms_text)
        self.assertIn("v1.0 GA", ms_text)
        self.assertIn("2026-12-31", ms_text)

        # Dependency DAG SVG view
        self.switch_nav("graph")
        self.page.wait_for_selector("#graph-svg-container svg g.dag-node", timeout=5000)
        self.assertEqual(self.page.locator("#graph-svg-container svg g.dag-node").count(), 9)
        self.assertGreaterEqual(self.page.locator("#graph-svg-container svg path[marker-end]").count(), 3)
        self.assertIn("ITK-7", self.page.locator("#graph-blocked-list").inner_text())
        self.assertNotIn("ITK-7", self.page.locator("#graph-ready-list").inner_text())

        # Analytics & Settings view matches CLI `itrack stats --json`
        cli_stats = self.cli_json("stats")
        self.switch_nav("stats")
        kpi_text = self.page.locator("#stats-kpi-grid").inner_text()
        self.assertIn(str(cli_stats["total_issues"]), kpi_text)
        self.assertEqual(self.page.locator("#settings-labels-list .tag").count(), 3)
        self.assertEqual(self.page.locator("#settings-comps-list .tag").count(), 2)
        self.assertEqual(self.page.locator("#settings-users-list .tag").count(), 3)
        self.assertIn("swarm-bot", self.page.locator("#settings-users-list").inner_text())

        # Clicking CLI-seeded saved view applies iQL and switches layout to board
        self.page.locator("#saved-views-list .saved-view-item .sv-run", has_text="CLI High & Urgent Open").click()
        self.page.wait_for_selector("#view-board.active", timeout=5000)
        self.wait_for_api_quiet()
        self.assertEqual(self.page.locator("#global-iql-input").input_value(), "priority >= high AND status != done")
        cli_iql_keys = sorted(
            i["key"] for i in self.cli_json("iql", "priority >= high AND status != done")["issues"]
        )
        ui_board_keys = sorted(
            self.page.locator("#board-columns .issue-card").evaluate_all("els => els.map(e => e.dataset.key)")
        )
        self.assertEqual(ui_board_keys, cli_iql_keys)

        # Reset iQL filter
        self.page.fill("#global-iql-input", "")
        self.page.click("#btn-run-iql")
        self.wait_for_api_quiet()

    def test_05_issue_drawer_renders_cli_subtasks_comments_attachments_worklogs_links_history(self) -> None:
        """Slide-over drawer displays CLI-created subtasks, links, binary attachment, comments, worklogs, and history."""
        self.switch_nav("board")
        self.page.locator("#board-columns .issue-card[data-key='ITK-3']").click()
        self.page.wait_for_selector("#issue-drawer-backdrop.open #issue-drawer", timeout=5000)

        self.assertEqual(self.page.locator("#dr-key").inner_text().strip(), "ITK-3")
        self.assertIn("Claimed: swarm-bot", self.page.locator("#dr-claimed-badge").inner_text())
        self.assertEqual(self.page.locator("#dr-subtasks-count").inner_text().strip(), "1/2")
        self.assertTrue(self.page.locator("#dr-subtasks-list .sub-toggle[data-key='ITK-4']").is_checked())
        self.assertFalse(self.page.locator("#dr-subtasks-list .sub-toggle[data-key='ITK-5']").is_checked())

        self.assertIn("ITK-7", self.page.locator("#dr-links-list").inner_text())
        self.assertIn("CLI-seeded **Markdown** note", self.page.locator("#dr-comments-list").inner_text())
        self.assertEqual((self.page.locator("#dr-worklog-total").text_content() or "").strip(), "75m")
        self.assertGreaterEqual(self.page.locator("#dr-history-list > div").count(), 3)

        # Verify CLI-uploaded binary attachment link & byte-for-byte SHA-256 download
        att_anchor = self.page.locator("#dr-attachments-list a", has_text="cli_seed_artifact.bin")
        self.assertTrue(att_anchor.is_visible())
        dl_href = att_anchor.get_attribute("href")
        self.assertTrue(dl_href)
        with httpx.Client(base_url=self.base_url, trust_env=False) as client:
            resp = client.get(str(dl_href))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(hashlib.sha256(resp.content).hexdigest(), self.seed_attachment_sha256)

        self.close_overlays()

    def test_06_ready_queue_dynamically_unblocks_when_cli_closes_blocker(self) -> None:
        """Blocked ITK-7 is excluded from #view-ready until ITK-3 is closed via CLI, then immediately appears."""
        self.switch_nav("ready")
        ready_before = self.page.locator("#ready-queue-cards").inner_text()
        self.assertIn("ITK-6", ready_before)
        self.assertIn("ITK-8", ready_before)
        self.assertNotIn("ITK-7", ready_before)
        self.assertIn("ITK-3", self.page.locator("#claimed-active-cards").inner_text())

        # Close blocker ITK-3 via CLI
        self.cli_json("close", "ITK-3", "--resolution", "fixed", "-m", "Resolved blocker via CLI")

        # Refresh Ready Queue in UI and verify ITK-7 is now unblocked & ready, and #cnt-blocked == 0
        self.switch_nav("ready")
        self.page.wait_for_function(
            "document.getElementById('ready-queue-cards').innerText.includes('ITK-7')"
        )
        self.assertIn("ITK-7", self.page.locator("#ready-queue-cards").inner_text())
        self.assertEqual(self.page.locator("#cnt-blocked").inner_text().strip(), "0")


# ===========================================================================
# 2. UI -> CLI Parity Suite
# ===========================================================================


class TestUiToCliParity(_PlaywrightParityBase):
    """Mutate workspace across all 15 interactive SPA capabilities and verify persisted state via `itrack.py --json`."""

    @classmethod
    def _seed_workspace(cls) -> None:
        cls.cli_json_cls("init", "--key", "UIP", "--name", "UI Parity Workspace")
        cls.cli_json_cls("user", "add", "alice", "--name", "Alice")
        cls.cli_json_cls("user", "add", "bob", "--name", "Bob")
        cls.cli_json_cls("milestone", "create", "v1.0 Base", "--target", "2026-12-15")
        # UIP-1: Baseline Epic
        cls.cli_json_cls("epic", "create", "Core UI Parity Epic", "-p", "high", "-a", "alice")

    def _open_drawer(self, key: str) -> None:
        self.switch_nav("list")
        self.page.locator(f"#list-tbody tr[data-key='{key}']").click()
        self.page.wait_for_selector("#issue-drawer-backdrop.open #issue-drawer", timeout=5000)
        self.page.wait_for_function(f"document.getElementById('dr-key').textContent.trim() === '{key}'")
        self.wait_for_api_quiet()

    def _drag_card_to_column(self, key: str, target_status: str) -> None:
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/transition" in r.url and r.request.method == "POST"
        ):
            self.page.evaluate(
                """([issueKey, status]) => {
                    const card = document.querySelector(`#board-columns .issue-card[data-key="${issueKey}"]`);
                    const col = document.querySelector(`#board-columns .board-col[data-status="${status}"]`);
                    if (!card || !col) throw new Error(`Missing card ${issueKey} or column ${status}`);
                    const dt = new DataTransfer();
                    card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }));
                    col.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
                    col.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
                }""",
                [key, target_status],
            )
        self.wait_for_api_quiet()

    def test_01_create_issue_via_modal_and_shortcut_persists_to_cli(self) -> None:
        """1. Create issue via `c` shortcut & `#btn-new-issue` modal -> verify via `itrack show <KEY> --json`."""
        self.switch_nav("board")
        self.page.keyboard.press("c")
        self.page.wait_for_selector("#create-issue-modal.open", timeout=5000)
        self.page.keyboard.press("Escape")
        self.page.wait_for_function("!document.getElementById('create-issue-modal').classList.contains('open')")

        self.page.click("#btn-new-issue")
        self.page.wait_for_selector("#create-issue-modal.open", timeout=5000)

        title = "Implement SPA to CLI parity verification"
        desc = "Created interactively through #create-issue-modal."
        self.page.fill("#ci-title", title)
        self.page.fill("#ci-desc", desc)
        self.page.select_option("#ci-type", "story")
        self.page.select_option("#ci-priority", "urgent")
        self.page.select_option("#ci-status", "todo")
        self.page.fill("#ci-assignee", "alice")
        self.page.fill("#ci-labels", "spa-created, parity")
        self.page.fill("#ci-points", "8")
        self.page.select_option("#ci-epic", "UIP-1")
        self.page.fill("#ci-due", "2026-12-20")
        self.page.fill("#ci-components", "frontend, api")

        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/issues") and r.request.method == "POST") as resp_info:
            self.page.click("#btn-submit-create")
        created_key = resp_info.value.json()["key"]
        self.page.wait_for_function("!document.getElementById('create-issue-modal').classList.contains('open')")
        self.page.wait_for_selector(f"#board-columns .issue-card[data-key='{created_key}']", timeout=5000)

        cli_iss = self.cli_json("show", created_key)
        self.assertEqual(cli_iss["title"], title)
        self.assertEqual(cli_iss["description"], desc)
        self.assertEqual(cli_iss["type"], "story")
        self.assertEqual(cli_iss["priority"], "urgent")
        self.assertEqual(cli_iss["status"], "todo")
        self.assertEqual(cli_iss["assignee"], "alice")
        self.assertEqual(sorted(cli_iss["labels"]), ["parity", "spa-created"])
        self.assertEqual(sorted(cli_iss["components"]), ["api", "frontend"])
        self.assertEqual(cli_iss["story_points"], 8.0)
        self.assertEqual(cli_iss["epic_id"], "UIP-1")
        self.assertEqual(cli_iss["due_date"], "2026-12-20")

    def test_02_transition_issue_via_board_drag_and_drawer_select(self) -> None:
        """2. Transition issue status via Kanban drag-drop & drawer select -> verify via `itrack show` & `itrack board`."""
        self.switch_nav("board")
        # Quick-add an issue directly into `backlog` column
        self.page.locator("#board-columns .col-quick-add[data-status='backlog']").click()
        self.page.wait_for_selector("#create-issue-modal.open", timeout=5000)
        self.page.fill("#ci-title", "Drag and transition test card")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/issues") and r.request.method == "POST") as resp_info:
            self.page.click("#btn-submit-create")
        key = resp_info.value.json()["key"]
        self.page.wait_for_selector(
            f"#board-columns .board-col[data-status='backlog'] .issue-card[data-key='{key}']",
            timeout=5000,
        )
        self.assertEqual(self.cli_json("show", key)["status"], "backlog")

        # Drag card from `backlog` -> `in_progress`
        self._drag_card_to_column(key, "in_progress")
        self.assertEqual(self.cli_json("show", key)["status"], "in_progress")
        board_json = self.cli_json("board")
        in_prog_col = next(c for c in board_json["columns"] if c["status"] == "in_progress")
        self.assertIn(key, [i["key"] for i in in_prog_col["issues"]])

        # Transition via `#issue-drawer` `#dr-status` select -> `in_review`
        self._open_drawer(key)
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/transition" in r.url and r.request.method == "POST"
        ):
            self.page.select_option("#dr-status", "in_review")
        self.wait_for_api_quiet()
        self.assertEqual(self.cli_json("show", key)["status"], "in_review")
        self.close_overlays()

    def test_03_inline_edit_drawer_fields_and_audit_history(self) -> None:
        """3. Inline edit title/desc/priority/assignee/points/milestone/labels/components in drawer -> verify CLI & history."""
        created = self.cli_json("create", "Original Drawer Title", "-d", "Original body", "-p", "low", "-s", "todo")
        key = created["key"]
        self._open_drawer(key)

        # Save updated Title & Description
        self.page.fill("#dr-title", "Edited Drawer Title via UI")
        self.page.fill("#dr-desc", "Updated **Markdown** description from drawer")
        with self.page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{key}") and r.request.method == "PATCH"):
            self.page.click("#dr-btn-save-text")
        self.wait_for_api_quiet()

        # Update select & input metadata fields sequentially
        with self.page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{key}") and r.request.method == "PATCH"):
            self.page.select_option("#dr-priority", "high")
        self.wait_for_api_quiet()

        with self.page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{key}") and r.request.method == "PATCH"):
            self.page.select_option("#dr-milestone", "MIL-1")
        self.wait_for_api_quiet()

        for selector, val in (
            ("#dr-assignee", "bob"),
            ("#dr-points", "13"),
            ("#dr-labels", "alpha, beta"),
            ("#dr-components", "core-engine, ui-shell"),
        ):
            self.page.fill(selector, val)
            with self.page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{key}") and r.request.method == "PATCH"):
                self.page.locator(selector).dispatch_event("change")
            self.wait_for_api_quiet()

        cli_iss = self.cli_json("show", key)
        self.assertEqual(cli_iss["title"], "Edited Drawer Title via UI")
        self.assertEqual(cli_iss["description"], "Updated **Markdown** description from drawer")
        self.assertEqual(cli_iss["priority"], "high")
        self.assertEqual(cli_iss["assignee"], "bob")
        self.assertEqual(cli_iss["story_points"], 13.0)
        self.assertEqual(cli_iss["milestone_id"], "MIL-1")
        self.assertEqual(sorted(cli_iss["labels"]), ["alpha", "beta"])
        self.assertEqual(sorted(cli_iss["components"]), ["core-engine", "ui-shell"])

        hist = self.cli_json("issue", "history", key)["history"]
        actions = [h["action"] for h in hist]
        self.assertIn("updated", actions)
        self.assertGreaterEqual(len(hist), 6)
        self.close_overlays()

    def test_04_add_and_complete_subtask_in_drawer(self) -> None:
        """4. Add child subtask in `#issue-drawer` & toggle completion -> verify via `itrack issue subtasks <KEY> --json`."""
        parent_key = self.cli_json("create", "Parent task for UI subtask")["key"]
        self._open_drawer(parent_key)

        self.page.fill("#dr-subtask-title", "Child subtask created in drawer")
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{parent_key}/subtasks" in r.url and r.request.method == "POST"
        ):
            self.page.click("#dr-btn-add-subtask")
        self.wait_for_api_quiet()

        sub_data = self.cli_json("issue", "subtasks", parent_key)
        self.assertEqual(sub_data["total_count"], 1)
        self.assertEqual(sub_data["completed_count"], 0)
        sub_key = sub_data["subtasks"][0]["key"]
        self.assertEqual(sub_data["subtasks"][0]["title"], "Child subtask created in drawer")
        self.assertEqual(sub_data["subtasks"][0]["parent_id"], parent_key)

        # Toggle subtask checkbox to done in UI
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{sub_key}/transition" in r.url and r.request.method == "POST"
        ):
            self.page.locator(f"#dr-subtasks-list .sub-toggle[data-key='{sub_key}']").check()
        self.wait_for_api_quiet()

        sub_after = self.cli_json("issue", "subtasks", parent_key)
        self.assertEqual(sub_after["completed_count"], 1)
        self.assertEqual(sub_after["subtasks"][0]["status"], "done")
        self.close_overlays()

    def test_05_comment_post_edit_and_delete_in_drawer(self) -> None:
        """5. Post, edit, and delete comment in `#issue-drawer` -> verify via `itrack comment list <KEY> --json`."""
        key = self.cli_json("create", "Issue for UI comment lifecycle")["key"]
        self._open_drawer(key)

        # Post comment
        self.page.fill("#comment-body-input", "Initial UI comment body")
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/comments" in r.url and r.request.method == "POST"
        ):
            self.page.click("#btn-add-comment")
        self.wait_for_api_quiet()

        cmts = self.cli_json("comment", "list", key)["comments"]
        self.assertEqual(len(cmts), 1)
        cid = cmts[0]["id"]
        self.assertEqual(cmts[0]["body"], "Initial UI comment body")

        # Edit comment via prompt dialog
        self.page.once("dialog", lambda d: d.accept("Edited UI comment body v2"))
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/comments/{cid}" in r.url and r.request.method == "PATCH"
        ):
            self.page.locator(f"#dr-comments-list .cmt-edit[data-id='{cid}']").click()
        self.wait_for_api_quiet()

        cmts_edited = self.cli_json("comment", "list", key)["comments"]
        self.assertEqual(len(cmts_edited), 1)
        self.assertEqual(cmts_edited[0]["body"], "Edited UI comment body v2")

        # Delete comment
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/comments/{cid}" in r.url and r.request.method == "DELETE"
        ):
            self.page.locator(f"#dr-comments-list .cmt-del[data-id='{cid}']").click()
        self.wait_for_api_quiet()

        self.assertEqual(self.cli_json("comment", "list", key)["comments"], [])
        self.close_overlays()

    def test_06_attachment_upload_cli_download_sha256_and_ui_delete(self) -> None:
        """6. Upload file in drawer -> CLI `attach download` SHA-256 match -> delete in UI -> verify empty via CLI."""
        key = self.cli_json("create", "Issue for UI attachment roundtrip")["key"]
        payload = b"\x00\x01\xfe\xffITRACK-UI-MULTIPART-UPLOAD-TEST\n" + os.urandom(128)
        expected_sha = hashlib.sha256(payload).hexdigest()
        src_file = self.repo_dir / "ui_upload_sample.bin"
        src_file.write_bytes(payload)

        self._open_drawer(key)
        self.page.set_input_files("#attachment-file-input", str(src_file))
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/attachments" in r.url and r.request.method == "POST"
        ):
            self.page.click("#dr-btn-upload-att")
        self.wait_for_api_quiet()

        att_list = self.cli_json("attach", "list", key)["attachments"]
        self.assertEqual(len(att_list), 1)
        att_id = att_list[0]["id"]
        self.assertEqual(att_list[0]["sha256"], expected_sha)

        dl_dest = self.repo_dir / "cli_downloaded_copy.bin"
        dl_meta = self.cli_json("attach", "download", key, att_id, "-o", str(dl_dest))
        self.assertEqual(dl_meta["size_bytes"], len(payload))
        self.assertEqual(hashlib.sha256(dl_dest.read_bytes()).hexdigest(), expected_sha)

        # Delete attachment in UI drawer
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/attachments/{att_id}" in r.url and r.request.method == "DELETE"
        ):
            self.page.locator(f"#dr-attachments-list .att-del[data-id='{att_id}']").click()
        self.wait_for_api_quiet()

        self.assertEqual(self.cli_json("attach", "list", key)["attachments"], [])
        self.close_overlays()

    def test_07_worklog_add_and_delete_in_drawer(self) -> None:
        """7. Log work (`1h 30m`) in `#issue-drawer` -> verify `+90` minutes via CLI -> delete in UI -> verify CLI."""
        key = self.cli_json("create", "Issue for UI worklog tracking")["key"]
        self._open_drawer(key)

        self.page.fill("#worklog-duration-input", "1h 30m")
        self.page.fill("#worklog-note-input", "Deep UI parity testing")
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/worklogs" in r.url and r.request.method == "POST"
        ):
            self.page.click("#dr-btn-add-worklog")
        self.wait_for_api_quiet()
        self.assertEqual((self.page.locator("#dr-worklog-total").text_content() or "").strip(), "90m")

        wl_data = self.cli_json("worklog", "list", key)
        self.assertEqual(wl_data["total_minutes"], 90)
        self.assertEqual(len(wl_data["worklogs"]), 1)
        wl_id = wl_data["worklogs"][0]["id"]
        self.assertEqual(wl_data["worklogs"][0]["note"], "Deep UI parity testing")

        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/worklogs/{wl_id}" in r.url and r.request.method == "DELETE"
        ):
            self.page.locator(f"#dr-worklogs-list .wl-del[data-id='{wl_id}']").click()
        self.wait_for_api_quiet()

        wl_after = self.cli_json("worklog", "list", key)
        self.assertEqual(wl_after["total_minutes"], 0)
        self.assertEqual(wl_after["worklogs"], [])
        self.close_overlays()

    def test_08_add_blocks_link_and_reject_circular_cycle_in_ui(self) -> None:
        """8. Add `blocks` link in `#view-graph` -> verify CLI `link list` & `ready`; reject circular `blocks` with 409."""
        key_a = self.cli_json("create", "DAG Blocker Node A", "-s", "todo")["key"]
        key_b = self.cli_json("create", "DAG Blocked Node B", "-s", "todo")["key"]

        self.switch_nav("graph")
        self.page.fill("#graph-src-input", key_a)
        self.page.select_option("#graph-type-select", "blocks")
        self.page.fill("#graph-tgt-input", key_b)
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key_a}/links" in r.url and r.request.method == "POST"
        ) as link_resp:
            self.page.click("#btn-graph-add-link")
        self.assertEqual(link_resp.value.status, 201)
        self.wait_for_api_quiet()

        links_a = self.cli_json("link", "list", key_a)["links"]
        self.assertTrue(any(l["link_type"] == "blocks" and l["target_key"] == key_b for l in links_a))
        ready_keys = [i["key"] for i in self.cli_json("ready")["issues"]]
        self.assertIn(key_a, ready_keys)
        self.assertNotIn(key_b, ready_keys)

        # Attempt circular `key_b blocks key_a` -> must return 409, show error toast, and preserve clean DAG
        self.page.fill("#graph-src-input", key_b)
        self.page.select_option("#graph-type-select", "blocks")
        self.page.fill("#graph-tgt-input", key_a)
        with self.allow_expected_console_http_status(409):
            with self.page.expect_response(
                lambda r: f"/api/v1/issues/{key_b}/links" in r.url and r.request.method == "POST"
            ) as cycle_resp:
                self.page.click("#btn-graph-add-link")
            self.assertEqual(cycle_resp.value.status, 409)
            self.page.wait_for_selector("#toast-stack .toast.err", timeout=5000)

        links_b = self.cli_json("link", "list", key_b)["links"]
        self.assertFalse(any(l["link_type"] == "blocks" and l["target_key"] == key_a for l in links_b))
        self.assertGreater(self.page.locator("#graph-svg-container svg g.dag-node").count(), 0)

    def test_09_claim_and_release_issue_via_ready_queue_and_drawer(self) -> None:
        """9. Claim issue from `#view-ready` and Release from `#issue-drawer` -> verify via `itrack show <KEY> --json`."""
        key = self.cli_json("create", "Ready queue claimable task", "-s", "todo", "-p", "high")["key"]

        self.switch_nav("ready")
        self.page.fill("#ready-agent-input", "ui-swarm-agent")
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/claim" in r.url and r.request.method == "POST"
        ):
            self.page.locator(f"#ready-queue-cards .btn-claim-row[data-key='{key}']").click()
        self.wait_for_api_quiet()

        claimed_iss = self.cli_json("show", key)
        self.assertEqual(claimed_iss["claimed_by"], "ui-swarm-agent")
        self.assertEqual(claimed_iss["status"], "in_progress")

        # Release via Drawer button
        self._open_drawer(key)
        with self.page.expect_response(
            lambda r: f"/api/v1/issues/{key}/release" in r.url and r.request.method == "POST"
        ):
            self.page.click("#dr-btn-release")
        self.wait_for_api_quiet()

        released_iss = self.cli_json("show", key)
        self.assertIsNone(released_iss["claimed_by"])
        self.assertEqual(released_iss["status"], "todo")
        self.close_overlays()

    def test_10_list_multiselect_bulk_label_priority_and_status(self) -> None:
        """10. Multi-select issues in `#view-list` and apply bulk label, priority, and status -> verify via `itrack list`."""
        k1 = self.cli_json("create", "Bulk item 1", "-s", "todo", "-p", "low")["key"]
        k2 = self.cli_json("create", "Bulk item 2", "-s", "todo", "-p", "low")["key"]
        k3 = self.cli_json("create", "Bulk item 3", "-s", "todo", "-p", "low")["key"]
        target_keys = [k1, k2, k3]

        self.switch_nav("list")

        def _select_targets() -> None:
            for k in target_keys:
                self.page.locator(f"#list-tbody .row-chk[data-key='{k}']").check()
            self.page.wait_for_selector("#bulk-action-bar.visible", timeout=5000)
            self.assertEqual(self.page.locator("#bulk-selected-count").inner_text().strip(), "3 selected")

        # Bulk Label
        _select_targets()
        self.page.fill("#bulk-label-input", "bulk-verified")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/issues/bulk") and r.request.method == "POST"):
            self.page.click("#btn-bulk-label")
        self.wait_for_api_quiet()

        # Bulk Priority
        _select_targets()
        self.page.select_option("#bulk-priority-select", "urgent")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/issues/bulk") and r.request.method == "POST"):
            self.page.click("#btn-bulk-priority")
        self.wait_for_api_quiet()

        # Bulk Status Transition
        _select_targets()
        self.page.select_option("#bulk-status-select", "in_review")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/issues/bulk") and r.request.method == "POST"):
            self.page.click("#btn-bulk-transition")
        self.wait_for_api_quiet()

        by_key = {i["key"]: i for i in self.cli_json("list", "--limit", "200")["issues"]}
        for k in target_keys:
            self.assertIn("bulk-verified", by_key[k]["labels"])
            self.assertEqual(by_key[k]["priority"], "urgent")
            self.assertEqual(by_key[k]["status"], "in_review")

    def test_11_sprint_create_start_assign_and_complete_with_rollover(self) -> None:
        """11. Create Sprint, Start, assign backlog issue, Complete with rollover in `#view-sprints` -> verify CLI."""
        roll_key = self.cli_json("create", "Unfinished sprint rollover item", "-s", "todo", "-p", "medium")["key"]
        done_key = self.cli_json("create", "Completed sprint item", "-s", "todo", "-p", "medium")["key"]

        self.switch_nav("sprints")

        # Create Sprint 1 & Sprint 2 in UI
        self.page.fill("#sprint-name-input", "UI Sprint One")
        self.page.fill("#sprint-goal-input", "First UI sprint")
        self.page.fill("#sprint-start-input", "2026-11-24")
        self.page.fill("#sprint-end-input", "2026-12-08")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/sprints") and r.request.method == "POST") as sp1_resp:
            self.page.click("#btn-create-sprint")
        sp1_id = sp1_resp.value.json()["id"]
        self.wait_for_api_quiet()

        self.page.fill("#sprint-name-input", "UI Sprint Two")
        self.page.fill("#sprint-goal-input", "Rollover destination sprint")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/sprints") and r.request.method == "POST") as sp2_resp:
            self.page.click("#btn-create-sprint")
        sp2_id = sp2_resp.value.json()["id"]
        self.wait_for_api_quiet()

        # Refresh Sprints view so state.sprints includes newly created sprints
        self.switch_nav("sprints")

        # Start Sprint 1 via UI
        with self.page.expect_response(
            lambda r: f"/api/v1/sprints/{sp1_id}/start" in r.url and r.request.method == "POST"
        ):
            self.page.locator(f"#sprints-container .sp-start[data-id='{sp1_id}']").click()
        self.wait_for_api_quiet()

        sprints_cli = {s["id"]: s for s in self.cli_json("sprint", "list")["sprints"]}
        self.assertEqual(sprints_cli[sp1_id]["state"], "active")

        # Assign both issues to Sprint 1 from Unscheduled Backlog planner dropdown in UI
        for k in (roll_key, done_key):
            with self.page.expect_response(
                lambda r: r.url.endswith(f"/api/v1/issues/{k}") and r.request.method == "PATCH"
            ):
                self.page.locator(f"#backlog-planner-list .bl-assign-sp[data-key='{k}']").select_option(sp1_id)
            self.wait_for_api_quiet()

        self.assertEqual(self.cli_json("show", roll_key)["sprint_id"], sp1_id)
        self.cli_json("close", done_key, "--resolution", "fixed")
        self.switch_nav("sprints")

        # Complete Sprint 1 with rollover -> Sprint 2
        self.page.locator(f"#sprints-container .sp-rollover[data-id='{sp1_id}']").select_option(sp2_id)
        with self.page.expect_response(
            lambda r: f"/api/v1/sprints/{sp1_id}/complete" in r.url and r.request.method == "POST"
        ):
            self.page.locator(f"#sprints-container .sp-complete[data-id='{sp1_id}']").click()
        self.wait_for_api_quiet()

        sprints_after = {s["id"]: s for s in self.cli_json("sprint", "list")["sprints"]}
        self.assertEqual(sprints_after[sp1_id]["state"], "completed")
        self.assertEqual(self.cli_json("show", roll_key)["sprint_id"], sp2_id)
        self.assertEqual(self.cli_json("show", done_key)["sprint_id"], sp1_id)

    def test_12_create_epic_and_milestone_in_ui(self) -> None:
        """12. Create Epic & Milestone in `#view-epics` UI -> verify via `itrack epic list` & `itrack milestone list`."""
        self.switch_nav("epics")

        # Quick Epic button opens Create Modal pre-populated with type='epic'
        self.page.click("#btn-quick-epic")
        self.page.wait_for_selector("#create-issue-modal.open", timeout=5000)
        self.assertEqual(self.page.locator("#ci-type").input_value(), "epic")
        self.page.fill("#ci-title", "UI-Created Next-Gen Architecture Epic")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/issues") and r.request.method == "POST") as ep_resp:
            self.page.click("#btn-submit-create")
        epic_key = ep_resp.value.json()["key"]
        self.wait_for_api_quiet()

        epics_cli = [e["issue"]["key"] for e in self.cli_json("epic", "list")["epics"]]
        self.assertIn(epic_key, epics_cli)

        # Create Milestone in UI
        self.page.fill("#ms-name-input", "v2.0 Enterprise")
        self.page.fill("#ms-date-input", "2027-06-30")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/milestones") and r.request.method == "POST") as ms_resp:
            self.page.click("#btn-create-milestone")
        ms_id = ms_resp.value.json()["id"]
        self.wait_for_api_quiet()

        # Update milestone status -> released in UI
        with self.page.expect_response(
            lambda r: r.url.endswith(f"/api/v1/milestones/{ms_id}") and r.request.method == "PATCH"
        ):
            self.page.locator(f"#milestones-list-container .ms-status[data-id='{ms_id}']").select_option("released")
        self.wait_for_api_quiet()

        ms_by_id = {m["id"]: m for m in self.cli_json("milestone", "list")["milestones"]}
        self.assertEqual(ms_by_id[ms_id]["name"], "v2.0 Enterprise")
        self.assertEqual(ms_by_id[ms_id]["target_date"], "2027-06-30")
        self.assertEqual(ms_by_id[ms_id]["status"], "released")

    def test_13_live_iql_query_invalid_badge_and_save_view(self) -> None:
        """13. Live iQL validation (invalid & valid), 1-for-1 result parity with `itrack iql`, and Save View."""
        self.switch_nav("board")

        # Invalid iQL syntax/value shows `.invalid` (`Syntax Err`)
        self.page.fill("#global-iql-input", "priority >= bogus_priority")
        self.page.wait_for_function(
            "document.getElementById('iql-status-badge').classList.contains('invalid')",
            timeout=5000,
        )
        self.assertEqual(self.page.locator("#iql-status-badge").inner_text().strip(), "Syntax Err")

        # Valid iQL expression
        expr = 'label = "bulk-verified" AND priority = urgent ORDER BY key ASC'
        self.page.fill("#global-iql-input", expr)
        self.page.wait_for_function(
            "document.getElementById('iql-status-badge').classList.contains('valid')",
            timeout=5000,
        )
        self.assertEqual(self.page.locator("#iql-status-badge").inner_text().strip(), "Valid iQL")

        self.page.click("#btn-run-iql")
        self.wait_for_api_quiet()

        cli_keys = sorted(i["key"] for i in self.cli_json("iql", expr)["issues"])
        ui_keys = sorted(
            self.page.locator("#board-columns .issue-card").evaluate_all("els => els.map(e => e.dataset.key)")
        )
        self.assertEqual(len(cli_keys), 3)
        self.assertEqual(ui_keys, cli_keys)

        # Save custom iQL View via `#btn-save-view`
        view_name = "Bulk Verified Urgent"
        self.page.once("dialog", lambda d: d.accept(view_name))
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/views") and r.request.method == "POST"):
            self.page.click("#btn-save-view")
        self.wait_for_api_quiet()

        views_cli = self.cli_json("view", "list")["views"]
        matched_view = next((v for v in views_cli if v["name"] == view_name), None)
        self.assertIsNotNone(matched_view)
        assert matched_view is not None
        self.assertEqual(matched_view["iql"], expr)
        self.assertEqual(matched_view["layout"], "board")

        # Clear global iQL filter
        self.page.fill("#global-iql-input", "")
        self.page.click("#btn-run-iql")
        self.wait_for_api_quiet()

    def test_14_settings_labels_components_users_config_and_reindex(self) -> None:
        """14. Add Label, Component, AI Agent User, update Project Config, and Rebuild FTS5 Index in `#view-stats`."""
        self.switch_nav("stats")

        # Add Label
        self.page.fill("#new-label-name", "ui-audit-pass")
        self.page.fill("#new-label-color", "#10b981")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/labels") and r.request.method == "POST"):
            self.page.click("#btn-add-label")
        self.wait_for_api_quiet()

        labels_cli = {lb["name"]: lb for lb in self.cli_json("label", "list")["labels"]}
        self.assertIn("ui-audit-pass", labels_cli)
        self.assertEqual(labels_cli["ui-audit-pass"]["color"].lower(), "#10b981")

        # Add Component
        self.page.fill("#new-comp-name", "fts5-engine")
        self.page.fill("#new-comp-lead", "alice")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/components") and r.request.method == "POST"):
            self.page.click("#btn-add-comp")
        self.wait_for_api_quiet()

        comps_cli = {c["name"]: c for c in self.cli_json("component", "list")["components"]}
        self.assertIn("fts5-engine", comps_cli)
        self.assertEqual(comps_cli["fts5-engine"]["lead"], "alice")

        # Register AI Agent User
        self.page.fill("#new-user-name", "qa-agent-bot")
        self.page.check("#new-user-agent")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/users") and r.request.method == "POST"):
            self.page.click("#btn-add-user")
        self.wait_for_api_quiet()

        users_cli = {u["username"]: u for u in self.cli_json("user", "list")["users"]}
        self.assertIn("qa-agent-bot", users_cli)
        self.assertTrue(users_cli["qa-agent-bot"]["is_agent"])

        # Update Project Config Name
        self.page.fill("#cfg-project-name", "UI Parity Verified Platform")
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/config") and r.request.method == "PATCH"):
            self.page.click("#btn-save-config")
        self.wait_for_api_quiet()

        cfg_cli = self.cli_json("config", "show")
        self.assertEqual(cfg_cli["project_name"], "UI Parity Verified Platform")
        self.assertEqual(self.page.locator("#workspace-project-name").inner_text().strip(), "UI Parity Verified Platform")

        # Trigger SQLite + FTS5 Reindex button
        stats_before = self.cli_json("stats")["total_issues"]
        with self.page.expect_response(lambda r: r.url.endswith("/api/v1/reindex") and r.request.method == "POST") as reidx_resp:
            self.page.click("#btn-reindex")
        self.assertEqual(reidx_resp.value.json()["indexed_issues"], stats_before)
        self.assertEqual(self.cli_json("stats")["total_issues"], stats_before)

    def test_15_command_palette_search_navigation_and_theme_toggle(self) -> None:
        """15. Command Palette (`Control+k` FTS5 search, view jump, `Escape`) and Dark/Light theme toggle."""
        self.switch_nav("stats")

        # Open Command Palette via Control+k & search live issues
        self.page.keyboard.press("Control+k")
        self.page.wait_for_selector("#cmd-palette-modal.open", timeout=5000)
        self.page.fill("#cmd-palette-input", "Bulk item")
        self.page.wait_for_function(
            "document.getElementById('cmd-palette-results').innerText.includes('Bulk item 1')",
            timeout=5000,
        )

        # Clear search and execute navigation command -> Kanban Board
        self.page.fill("#cmd-palette-input", "Kanban Board")
        self.page.locator("#cmd-palette-results .nav-btn", has_text="Go to Kanban Board").first.click()
        self.page.wait_for_function("!document.getElementById('cmd-palette-modal').classList.contains('open')")
        self.page.wait_for_selector("#view-board.active", timeout=5000)

        # Re-open via button & close via Escape
        self.page.click("#btn-cmd-palette")
        self.page.wait_for_selector("#cmd-palette-modal.open", timeout=5000)
        self.page.keyboard.press("Escape")
        self.page.wait_for_function("!document.getElementById('cmd-palette-modal').classList.contains('open')")

        # Theme toggle dark -> light -> dark
        initial_theme = self.page.evaluate("document.documentElement.getAttribute('data-theme')")
        self.assertEqual(initial_theme, "dark")
        self.page.click("#btn-theme-toggle")
        self.assertEqual(self.page.evaluate("document.documentElement.getAttribute('data-theme')"), "light")
        self.assertEqual(self.page.evaluate("localStorage.getItem('itrack_theme')"), "light")

        self.page.click("#btn-theme-toggle")
        self.assertEqual(self.page.evaluate("document.documentElement.getAttribute('data-theme')"), "dark")
        self.assertEqual(self.page.evaluate("localStorage.getItem('itrack_theme')"), "dark")


if __name__ == "__main__":
    unittest.main(verbosity=2)
