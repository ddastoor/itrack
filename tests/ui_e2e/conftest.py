"""Live ephemeral HTTP/UI server + headless Playwright Chromium + shared-store CLI fixtures."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.unit_and_cli.conftest import CLIRunner  # noqa: E402

PYTHON_BIN = sys.executable
SERVER_ENTRY = REPO_ROOT / "itrack-server.py"


def make_png_bytes(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (16, 185, 129)) -> bytes:
    """Create a deterministic valid RGB PNG binary via Python stdlib."""

    def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)

    raw_rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw_rows)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, proc: subprocess.Popen[str], timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(f"itrack-server.py exited prematurely ({proc.returncode}):\n{out}\n{err}")
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_err = exc
            time.sleep(0.08)
    raise TimeoutError(f"Server at {base_url} did not become healthy within {timeout_sec}s: {last_err}")


@dataclass
class LiveUIEnv:
    """Isolated per-test UI + CLI test environment sharing a temporary backing store."""

    data_dir: Path
    base_url: str
    page: Page
    context: BrowserContext
    cli: CLIRunner
    page_errors: list[str]
    server_5xx_errors: list[str]


@pytest.fixture(scope="module")
def browser_instance() -> Iterator[Browser]:
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


@pytest.fixture
def png_bytes() -> bytes:
    return make_png_bytes()


@pytest.fixture
def ui_png_file(tmp_path: Path, png_bytes: bytes) -> Path:
    img_file = tmp_path / "ui_attachment.png"
    img_file.write_bytes(png_bytes)
    return img_file


@pytest.fixture
def ui_env(tmp_path: Path, browser_instance: Browser, png_bytes: bytes) -> Iterator[LiveUIEnv]:
    data_dir = tmp_path / "shared_ui_cli_store"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ITRACK_DATA_DIR"] = str(data_dir)

    server_proc = subprocess.Popen(
        [
            PYTHON_BIN,
            "-B",
            str(SERVER_ENTRY),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    context: BrowserContext | None = None
    page_errors: list[str] = []
    server_5xx_errors: list[str] = []

    try:
        _wait_for_health(base_url, server_proc)

        context = browser_instance.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        # Fulfill mock https://example.com/*.png image URLs locally so offline tests never hit external networks.
        page.route(
            "https://example.com/**",
            lambda route: route.fulfill(status=200, content_type="image/png", body=png_bytes),
        )

        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "response",
            lambda resp: server_5xx_errors.append(f"{resp.status} {resp.request.method} {resp.url}")
            if resp.status >= 500
            else None,
        )

        # Fail immediately if any blocking browser dialog (alert/confirm/prompt) is ever opened.
        def _reject_native_dialog(dialog: Any) -> None:
            page_errors.append(f"Unexpected blocking native dialog ({dialog.type}): {dialog.message}")
            dialog.dismiss()

        page.on("dialog", _reject_native_dialog)

        page.goto(base_url, wait_until="networkidle")

        yield LiveUIEnv(
            data_dir=data_dir,
            base_url=base_url,
            page=page,
            context=context,
            cli=CLIRunner(data_dir),
            page_errors=page_errors,
            server_5xx_errors=server_5xx_errors,
        )

        assert not page_errors, f"Uncaught page errors or native dialogs in UI: {page_errors}"
        assert not server_5xx_errors, f"Unexpected HTTP 5xx server errors during UI test: {server_5xx_errors}"
    finally:
        if context is not None:
            context.close()
        if server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=3)
