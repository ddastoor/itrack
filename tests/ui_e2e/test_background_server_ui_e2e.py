"""Playwright Chromium E2E tests for background server managed by `itrack.py`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, expect

REPO_ROOT = Path(__file__).resolve().parents[2]
ITRACK_CLI = REPO_ROOT / "itrack.py"
PYTHON_BIN = sys.executable


def _run_cli(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ITRACK_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [PYTHON_BIN, "-B", str(ITRACK_CLI), "--data-dir", str(data_dir), *args],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _new_monitored_page(
    browser_instance: Browser,
) -> tuple[BrowserContext, Any, list[str], list[str]]:
    context = browser_instance.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page_errors: list[str] = []
    server_5xx: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on(
        "response",
        lambda resp: server_5xx.append(f"{resp.status} {resp.request.method} {resp.url}")
        if resp.status >= 500
        else None,
    )
    return context, page, page_errors, server_5xx


def test_background_server_start_ui_parity_and_stop(
    tmp_path: Path, browser_instance: Browser
) -> None:
    data_dir = tmp_path / "bg_e2e_store"
    context: BrowserContext | None = None
    try:
        # 1. Start detached background server from itrack.py
        start_proc = _run_cli(data_dir, "server", "start", "--host", "127.0.0.1", "--port", "0", "--json")
        assert start_proc.returncode == 0, f"STDOUT={start_proc.stdout}\nSTDERR={start_proc.stderr}"
        start_info = json.loads(start_proc.stdout)
        assert start_info["running"] is True
        assert start_info["healthy"] is True
        base_url = start_info["url"]

        # 2. Check status via CLI
        status_proc = _run_cli(data_dir, "server", "status", "--json")
        assert status_proc.returncode == 0
        status_info = json.loads(status_proc.stdout)
        assert status_info["running"] is True
        assert status_info["healthy"] is True
        assert status_info["pid"] == start_info["pid"]

        # 3. Open live Web UI in headless Chromium
        context, page, page_errors, server_5xx = _new_monitored_page(browser_instance)
        page.goto(base_url, wait_until="networkidle")

        expect(page.get_by_test_id("app-header")).to_be_visible()
        expect(page.get_by_test_id("issue-list-empty")).to_be_visible()

        # Verify Features modal displays new background-server-management feature
        page.get_by_test_id("nav-features").click()
        expect(page.get_by_test_id("features-view")).to_be_visible()
        expect(page.get_by_test_id("features-list")).to_contain_text("Background Server Lifecycle Management")
        page.get_by_test_id("btn-close-features").click()
        expect(page.get_by_test_id("features-view")).to_be_hidden()

        # Create issue #1 via CLI -> verify live Web UI displays it after reload
        create_proc = _run_cli(
            data_dir,
            "create",
            "--summary",
            "Background daemon issue parity",
            "--description",
            "Verified across **CLI** and **Web UI**.",
            "--json",
        )
        assert create_proc.returncode == 0
        created = json.loads(create_proc.stdout)
        assert created["number"] == 1

        page.reload(wait_until="networkidle")
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()
        expect(page.get_by_test_id("issue-row-1")).to_contain_text("Background daemon issue parity")

        # Create issue #2 via Web UI -> verify CLI reads it immediately from shared store
        page.get_by_test_id("btn-new-issue").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_visible()
        page.get_by_test_id("create-summary-input").fill("UI created while server detached")
        html_input = page.get_by_test_id("create-desc-html-input")
        if not html_input.is_visible():
            page.get_by_test_id("create-desc-btn-source").click()
            expect(html_input).to_be_visible()
        html_input.fill("<p>Created inside Playwright against background daemon.</p>")
        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_hidden()
        expect(page.get_by_test_id("issue-detail-view")).to_be_visible()
        page.get_by_test_id("btn-back-to-list").click()
        expect(page.get_by_test_id("issue-row-2")).to_be_visible()

        show_2_proc = _run_cli(data_dir, "show", "2", "--json")
        assert show_2_proc.returncode == 0
        show_2 = json.loads(show_2_proc.stdout)
        assert show_2["summary"] == "UI created while server detached"

        assert not page_errors, f"Unexpected page errors: {page_errors}"
        assert not server_5xx, f"Unexpected 5xx responses: {server_5xx}"

        # 4. Stop server cleanly from itrack.py
        stop_proc = _run_cli(data_dir, "server", "stop", "--json")
        assert stop_proc.returncode == 0
        stop_info = json.loads(stop_proc.stdout)
        assert stop_info["stopped"] is True
        assert stop_info["status"] == "stopped"

        # 5. Verify status reports stopped and HTTP endpoint is unreachable
        status_after = json.loads(_run_cli(data_dir, "server", "status", "--json").stdout)
        assert status_after["running"] is False
        assert status_after["healthy"] is False
        assert status_after["status"] == "stopped"

        with pytest.raises(httpx.HTTPError):
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                client.get(f"{base_url}/api/health")
    finally:
        if context is not None:
            context.close()
        _run_cli(data_dir, "server", "stop", "--force", "--json")


def test_background_ui_flag_restart_and_convenience_stop(
    tmp_path: Path, browser_instance: Browser
) -> None:
    data_dir = tmp_path / "bg_restart_e2e_store"
    context: BrowserContext | None = None
    try:
        # Start via `itrack.py ui --background`
        start_proc = _run_cli(data_dir, "ui", "--background", "--port", "0", "--json")
        assert start_proc.returncode == 0, f"STDOUT={start_proc.stdout}\nSTDERR={start_proc.stderr}"
        start_info = json.loads(start_proc.stdout)
        old_pid = start_info["pid"]
        old_port = start_info["port"]
        base_url = start_info["url"]

        seed_proc = _run_cli(
            data_dir,
            "create",
            "--summary",
            "Persisted across background restart",
            "--description",
            "<p>Survives restart</p>",
            "--json",
        )
        assert seed_proc.returncode == 0

        context, page, page_errors, server_5xx = _new_monitored_page(browser_instance)
        page.goto(base_url, wait_until="networkidle")
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()

        # Restart background server via CLI preserving port and data
        restart_proc = _run_cli(data_dir, "server", "restart", "--json")
        assert restart_proc.returncode == 0, f"STDOUT={restart_proc.stdout}\nSTDERR={restart_proc.stderr}"
        restart_info = json.loads(restart_proc.stdout)
        assert restart_info["restarted"] is True
        assert restart_info["was_running"] is True
        assert restart_info["previous_pid"] == old_pid
        assert restart_info["pid"] != old_pid
        assert restart_info["port"] == old_port
        assert restart_info["healthy"] is True

        # Reload browser at same URL and confirm persisted issue renders cleanly
        page.reload(wait_until="networkidle")
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()
        expect(page.get_by_test_id("issue-row-1")).to_contain_text("Persisted across background restart")
        assert not page_errors, f"Unexpected page errors: {page_errors}"
        assert not server_5xx, f"Unexpected 5xx responses: {server_5xx}"

        # Stop via top-level convenience `itrack.py stop`
        stop_proc = _run_cli(data_dir, "stop", "--json")
        assert stop_proc.returncode == 0
        stop_info = json.loads(stop_proc.stdout)
        assert stop_info["stopped"] is True
        assert stop_info["pid"] == restart_info["pid"]

        with pytest.raises(httpx.HTTPError):
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                client.get(f"{base_url}/api/health")
    finally:
        if context is not None:
            context.close()
        _run_cli(data_dir, "server", "stop", "--force", "--json")
