"""Pytest configuration and shared fixtures for isolated Git repos, iTrack workspaces, and Playwright Live UI."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import (  # noqa: E402
    ITrackWorkspace,
    cleanup_repo_daemon,
    harvest_browser_telemetry,
    init_git_repo,
)


@dataclass
class LiveUISession:
    """Active headless Chromium Playwright session bound to an isolated iTrack workspace daemon."""

    workspace: ITrackWorkspace
    base_url: str
    browser: Any
    context: Any
    page: Any
    console_errors: list[str]
    page_errors: list[str]
    failed_requests: list[str]
    external_requests: list[str]

    @property
    def ws(self) -> ITrackWorkspace:
        return self.workspace

    @property
    def url(self) -> str:
        return self.base_url

    def harvest_telemetry(self) -> dict[str, list[str]]:
        return harvest_browser_telemetry(self.page)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)


def _is_allowed_local_url(url: str) -> bool:
    if url.startswith(("data:", "blob:", "about:")):
        return True
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return True
    return False


def _is_expected_api_4xx_resource_notice(text: str, location: dict[str, Any] | None) -> bool:
    """Ignore Chromium's built-in 4xx resource status log on intentional API validation/conflict responses,
    while still failing on any JS console.error(), 5xx server error, or broken /download image/file."""
    if not text.startswith("Failed to load resource: the server responded with a status of 4"):
        return False
    loc = location or {}
    loc_url = str(loc.get("url") or "")
    line_num = int(loc.get("lineNumber") or 0)
    if line_num != 0:
        return False
    if "/api/v1/" not in loc_url:
        return False
    if loc_url.split("?", 1)[0].endswith("/download"):
        return False
    return True


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    """Yield a fresh isolated Git repository (NOT yet `itrack init`ed), reaping any daemon on teardown."""
    repo_dir = init_git_repo(tmp_path / "repo")
    try:
        yield repo_dir
    finally:
        cleanup_repo_daemon(repo_dir)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[ITrackWorkspace]:
    """Yield an initialized (`git init` + `itrack init`) isolated ITrackWorkspace with daemon teardown."""
    repo_dir = init_git_repo(tmp_path / "repo")
    ws = ITrackWorkspace(repo_dir)
    ws.cli("init")
    try:
        yield ws
    finally:
        ws.stop_server()


@pytest.fixture
def live_ui(workspace: ITrackWorkspace) -> Iterator[LiveUISession]:
    """Start `itrack ui --no-browser --json`, launch headless Chromium (1440x900), attach error/CDN guards,
    and harvest UI surface telemetry into ITRACK_SURFACE_LOG upon teardown."""
    from playwright.sync_api import sync_playwright

    ui_info = workspace.cli_json("ui", "--no-browser")
    base_url = ""
    if isinstance(ui_info, dict):
        base_url = str(ui_info.get("base_url") or ui_info.get("url") or "").rstrip("/")
    if not base_url:
        base_url = workspace.ensure_server()

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    external_requests: list[str] = []

    pw = sync_playwright().start()
    browser = None
    context = None
    page = None
    teardown_check_error: AssertionError | None = None

    try:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            permissions=["clipboard-read", "clipboard-write"],
        )

        # Preserve visited views/modals across any `page.reload()` calls inside tests.
        context.add_init_script(
            """() => {
                window.addEventListener('beforeunload', () => {
                    try {
                        const out = { views: new Set(), modals: new Set(), actions: new Set() };
                        const merge = (src) => {
                            if (!src || typeof src !== 'object') return;
                            for (const k of ['views', 'modals', 'actions']) {
                                if (src[k]) {
                                    for (const v of Array.from(src[k])) {
                                        if (typeof v === 'string' && v) out[k].add(v);
                                    }
                                }
                            }
                        };
                        const saved = sessionStorage.getItem('__itrack_accum_telemetry');
                        if (saved) merge(JSON.parse(saved));
                        if (typeof window.getItrackTelemetry === 'function') merge(window.getItrackTelemetry());
                        if (window.__itrackTelemetry) merge(window.__itrackTelemetry);
                        sessionStorage.setItem('__itrack_accum_telemetry', JSON.stringify({
                            views: Array.from(out.views),
                            modals: Array.from(out.modals),
                            actions: Array.from(out.actions),
                        }));
                    } catch (e) {}
                });
            }"""
        )

        page = context.new_page()

        def _on_console(msg: Any) -> None:
            if msg.type == "error":
                txt = msg.text
                loc = msg.location if hasattr(msg, "location") else None
                if not _is_expected_api_4xx_resource_notice(txt, loc):
                    console_errors.append(f"{txt} (location={loc})")

        def _on_pageerror(exc: Any) -> None:
            page_errors.append(str(exc))

        def _on_request(req: Any) -> None:
            if not _is_allowed_local_url(req.url):
                external_requests.append(f"{req.method} {req.url}")

        def _on_requestfailed(req: Any) -> None:
            failure = req.failure
            fail_str = str(failure) if failure is not None else "unknown"
            if "ERR_ABORTED" in fail_str:
                return
            failed_requests.append(f"{req.method} {req.url} -> {fail_str}")

        def _on_response(resp: Any) -> None:
            if resp.status >= 500:
                failed_requests.append(f"HTTP {resp.status} {resp.request.method} {resp.url}")

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("request", _on_request)
        page.on("requestfailed", _on_requestfailed)
        page.on("response", _on_response)

        page.goto(base_url, wait_until="networkidle")

        session = LiveUISession(
            workspace=workspace,
            base_url=base_url,
            browser=browser,
            context=context,
            page=page,
            console_errors=console_errors,
            page_errors=page_errors,
            failed_requests=failed_requests,
            external_requests=external_requests,
        )
        yield session

        harvest_browser_telemetry(page)

        if console_errors or page_errors or failed_requests or external_requests:
            teardown_check_error = AssertionError(
                "Browser integrity violation during live_ui session:\n"
                f"  console_errors ({len(console_errors)}): {console_errors}\n"
                f"  page_errors ({len(page_errors)}): {page_errors}\n"
                f"  failed_requests ({len(failed_requests)}): {failed_requests}\n"
                f"  external_requests ({len(external_requests)}): {external_requests}"
            )
    finally:
        if page is not None:
            try:
                harvest_browser_telemetry(page)
            except Exception:
                pass
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            pw.stop()
        except Exception:
            pass
        workspace.stop_server()

    if teardown_check_error is not None:
        raise teardown_check_error
