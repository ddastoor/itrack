"""Suite 07: Full End-to-End Headless Chromium Playwright Verification of Every Web UI Feature.

Covers 100% of `SPA_HTML` views (`UI_VIEWS` = 8), modals (`UI_MODALS` = 10), interactive controls,
keyboard shortcuts, dark/light theme persistence, slide-over drawer vs `#/issues/<ref>` permalinks,
browser back/forward history, Create Issue validation + drag-and-drop, Issue Detail inline rename +
every field & explicit clear button + lifecycle actions, Rich Markdown comments + XSS sanitization +
all 3 inline image embed methods (file picker, drag-and-drop, `Ctrl+V` clipboard paste) + attachment
linking/preview/lightbox/SHA-256 download/deleted-attachment placeholders, Issues List tabs/filters/
iQL/sorting/pagination/bulk actions, Kanban drag-and-drop + WIP limits + 5 swimlanes, hierarchy/links/
cycle rejection, Tags, Sprints + rollover modal, Milestones, Saved Views, Worklogs, History diffs,
Analytics dashboard, and Workspace Settings — with zero console/page/network errors and zero CDN leaks.
"""

from __future__ import annotations

import base64
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, expect

from conftest import LiveUISession
from helpers import (
    PRESET_TABS,
    SWIMLANES,
    UI_MODALS,
    UI_VIEWS,
    make_json_file,
    make_png,
    make_svg,
    make_text_file,
    read_surface_events,
    sha256_file,
)


# ---------------------------------------------------------------------------
# Deterministic Browser Interaction Helpers
# ---------------------------------------------------------------------------


def _assert_browser_clean(session: LiveUISession) -> None:
    """Assert zero console errors, uncaught page errors, 5xx/failed requests, and external CDN requests."""
    assert session.console_errors == [], f"Unexpected console.error: {session.console_errors}"
    assert session.page_errors == [], f"Unexpected uncaught pageerror: {session.page_errors}"
    assert session.failed_requests == [], f"Unexpected failed/5xx requests: {session.failed_requests}"
    assert session.external_requests == [], f"Unexpected external CDN requests: {session.external_requests}"


_FETCH_IDLE_SCRIPT = """() => {
    if (window.__itrackFetchWrapped) return;
    window.__itrackFetchWrapped = true;
    window.__itrackInFlight = 0;
    const origFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        window.__itrackInFlight++;
        try {
            const resp = await origFetch(...args);
            const origJson = resp.json.bind(resp);
            resp.json = async () => {
                window.__itrackInFlight++;
                try {
                    return await origJson();
                } finally {
                    setTimeout(() => { window.__itrackInFlight--; }, 0);
                }
            };
            const origText = resp.text.bind(resp);
            resp.text = async () => {
                window.__itrackInFlight++;
                try {
                    return await origText();
                } finally {
                    setTimeout(() => { window.__itrackInFlight--; }, 0);
                }
            };
            return resp;
        } finally {
            setTimeout(() => { window.__itrackInFlight--; }, 0);
        }
    };
}"""


def _install_fetch_idle_tracker(session: LiveUISession) -> None:
    """Install in-flight fetch/JSON/DOM-render tracker across current document and reloads."""
    session.context.add_init_script(_FETCH_IDLE_SCRIPT)
    session.page.evaluate(_FETCH_IDLE_SCRIPT)


def _wait_ui_settled(page: Page) -> None:
    """Wait until all chained `api(...)` fetches, `resp.json()` parses, and synchronous DOM renders finish."""
    page.wait_for_function("() => !window.__itrackFetchWrapped || window.__itrackInFlight === 0")


def _wait_img_natural_width(img_locator: Any) -> int:
    """Await `<img>` network load + Chromium decode completion and return `naturalWidth`."""
    return int(
        img_locator.evaluate(
            """img => new Promise(resolve => {
                if (img.complete && img.naturalWidth > 0) return resolve(img.naturalWidth);
                img.addEventListener('load', () => resolve(img.naturalWidth), { once: true });
                img.addEventListener('error', () => resolve(0), { once: true });
            })"""
        )
    )


def _wait_issue_detail_refresh(page: Page, issue_key: str, action: Callable[[], Any]) -> None:
    """Execute a UI mutation inside `#issue-drawer` and wait for `openIssueDetail` + active view refresh."""
    encoded_key = urllib.parse.quote(issue_key, safe="")
    target_suffix = f"/api/v1/issues/{encoded_key}"
    with page.expect_response(
        lambda r: r.url.split("?", 1)[0].endswith(target_suffix)
        and r.request.method == "GET"
        and r.status == 200
    ):
        action()
    _wait_ui_settled(page)


def _set_detail_input(page: Page, issue_key: str, selector: str, value: str) -> None:
    """Commit a sidebar `<input>` value with exactly one `change` event and zero residual focus-blur races."""
    def _act() -> None:
        page.locator(selector).evaluate(
            """(el, val) => {
                if (document.activeElement && document.activeElement !== document.body) {
                    document.activeElement.blur();
                }
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )

    _wait_issue_detail_refresh(page, issue_key, _act)


def _dispatch_file_drop(page: Page, selector: str, file_path: Path, mime_type: str) -> None:
    """Synthesize a standards-compliant HTML5 DragEvent('drop') carrying real file bytes in DataTransfer."""
    b64_data = base64.b64encode(file_path.read_bytes()).decode("ascii")
    page.locator(selector).evaluate(
        """(el, payload) => {
            const bin = atob(payload.b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], payload.name, { type: payload.mime });
            const dt = new DataTransfer();
            dt.items.add(file);
            el.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
            el.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
        }""",
        {"b64": b64_data, "name": file_path.name, "mime": mime_type},
    )


def _dispatch_clipboard_image_paste(page: Page, selector: str, png_path: Path) -> None:
    """Synthesize a real ClipboardEvent('paste') carrying an image/png Blob (`name: 'image.png'`)."""
    b64_data = base64.b64encode(png_path.read_bytes()).decode("ascii")
    page.locator(selector).evaluate(
        """(el, b64) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], 'image.png', { type: 'image/png' });
            const dt = new DataTransfer();
            dt.items.add(file);
            el.focus();
            const evt = new ClipboardEvent('paste', {
                bubbles: true,
                cancelable: true,
                clipboardData: dt,
            });
            el.dispatchEvent(evt);
        }""",
        b64_data,
    )


# ---------------------------------------------------------------------------
# 1. App Shell, Sidebar, All 8 Views, Theme Persistence, Shortcuts & Permalinks
# ---------------------------------------------------------------------------


def test_01_app_shell_navigation_theme_shortcuts_and_permalink_routing(live_ui: LiveUISession) -> None:
    page = live_ui.page
    ws = live_ui.ws

    # Seed two issues (including spaces, symbols & Unicode in folder/issue name)
    unicode_name = "Auth Gateway — Café & Ω Retry!"
    r1 = ws.api("POST", "/api/v1/issues", json={"name": unicode_name, "category": "bug", "priority": "critical"})
    assert r1.status_code == 201
    iss1 = r1.json()
    r2 = ws.api("POST", "/api/v1/issues", json={"name": "Secondary Navigation Check", "category": "task"})
    assert r2.status_code == 201

    page.reload(wait_until="networkidle")

    # Verify initial shell & sidebar collapse/expand toggle
    sidebar = page.locator("#sidebar")
    expect(sidebar).to_be_visible()
    expect(sidebar).not_to_have_class("collapsed")
    page.locator("#btn-toggle-sidebar").click()
    expect(sidebar).to_have_class("collapsed")
    page.locator("#btn-toggle-sidebar").click()
    expect(sidebar).not_to_have_class("collapsed")
    expect(page.locator("#brand-title")).to_be_visible()

    # Visit all 8 primary views via sidebar buttons & verify hash + active pane
    for view_name in UI_VIEWS:
        page.locator(f'button.nav-item[data-view="{view_name}"]').click()
        expect(page.locator(f"section.view-pane#view-{view_name}")).to_have_class("view-pane active")
        assert page.evaluate("() => location.hash") == f"#/views/{view_name}"

    telemetry = live_ui.harvest_telemetry()
    assert set(UI_VIEWS).issubset(set(telemetry["views"]))

    # Switch back to Issues List & test Reindex button
    page.locator('button.nav-item[data-view="issues"]').click()
    expect(page.locator("#view-issues")).to_have_class("view-pane active")
    with page.expect_response(lambda r: "/api/v1/reindex" in r.url and r.request.method == "POST"):
        page.locator("#btn-reindex").click()
    expect(page.locator("#toast-container .toast").last).to_contain_text("Reindexed")

    # Dark / Light theme toggle + localStorage persistence across reload
    html_root = page.locator("html")
    expect(html_root).to_have_attribute("data-theme", "dark")
    page.locator("#btn-theme-toggle").click()
    expect(html_root).to_have_attribute("data-theme", "light")
    assert page.evaluate("() => localStorage.getItem('itrack_theme')") == "light"

    page.reload(wait_until="networkidle")
    expect(html_root).to_have_attribute("data-theme", "light")
    # Toggle back to dark and confirm
    page.locator("#btn-theme-toggle").click()
    expect(html_root).to_have_attribute("data-theme", "dark")
    assert page.evaluate("() => localStorage.getItem('itrack_theme')") == "dark"

    # Keyboard shortcuts: `c` opens Create modal, `Escape` closes modal, `/` focuses global search
    page.locator("#brand-title").click()
    page.keyboard.press("c")
    expect(page.locator('#modal-create-issue[data-modal="create-issue"]')).to_have_class("modal-backdrop open")
    page.keyboard.press("Escape")
    expect(page.locator('#modal-create-issue[data-modal="create-issue"]')).not_to_have_class("modal-backdrop open")

    page.locator("#brand-title").click()
    page.keyboard.press("/")
    expect(page.locator("#global-search-input")).to_be_focused()
    # Blur search input so non-input shortcuts work afterward
    page.locator("#brand-title").click()

    # Slide-over Drawer vs Full-Page Permalink (`#/issues/<key>` and `#/issues/<encoded-name>`)
    drawer = page.locator('#issue-drawer[data-modal="issue-detail"]')
    page.locator(f'.issue-open-link[data-key="{iss1["key"]}"]').first.click()
    expect(drawer).to_have_class("open")
    expect(page.locator("#detail-key-badge")).to_have_text(iss1["key"])
    assert drawer.evaluate("el => el.style.inset") == ""

    # Escape closes slide-over drawer
    page.keyboard.press("Escape")
    expect(drawer).not_to_have_class("open")

    # Re-open drawer, click Permalink -> switches to full-page `#view-issue-permalink`
    page.locator(f'.issue-open-link[data-key="{iss1["key"]}"]').first.click()
    expect(drawer).to_have_class("open")
    page.locator("#btn-open-permalink").click()
    expect(page.locator("#view-issue-permalink")).to_have_class("view-pane active")
    assert page.evaluate("() => location.hash") == f"#/issues/{iss1['key']}"
    assert drawer.evaluate("el => el.style.inset") != ""

    # Escape does NOT close full-page permalink mode
    page.keyboard.press("Escape")
    expect(drawer).to_have_class("open")
    expect(page.locator("#view-issue-permalink")).to_have_class("view-pane active")

    # Close button inside permalink header exits full-page mode back to `#/views/issues`
    page.locator("#btn-close-drawer").click()
    expect(page.locator("#view-issues")).to_have_class("view-pane active")
    expect(drawer).not_to_have_class("open")
    assert page.evaluate("() => location.hash") == "#/views/issues"

    # Direct permalink navigation using URL-encoded human-readable folder name (spaces & Unicode)
    encoded_name = urllib.parse.quote(unicode_name, safe="")
    page.goto(f"{live_ui.base_url}/#/issues/{encoded_name}", wait_until="networkidle")
    expect(page.locator("#view-issue-permalink")).to_have_class("view-pane active")
    expect(drawer).to_have_class("open")
    expect(page.locator("#detail-key-badge")).to_have_text(iss1["key"])
    expect(page.locator("#detail-title-input")).to_have_value(unicode_name)

    # Browser back/forward navigation across views & permalinks
    page.locator('button.nav-item[data-view="board"]').click()
    expect(page.locator("#view-board")).to_have_class("view-pane active")
    page.locator('button.nav-item[data-view="analytics"]').click()
    expect(page.locator("#view-analytics")).to_have_class("view-pane active")

    page.go_back(wait_until="networkidle")
    expect(page.locator("#view-board")).to_have_class("view-pane active")
    page.go_back(wait_until="networkidle")
    expect(page.locator("#view-issue-permalink")).to_have_class("view-pane active")
    expect(drawer).to_have_class("open")

    page.go_forward(wait_until="networkidle")
    expect(page.locator("#view-board")).to_have_class("view-pane active")
    page.go_forward(wait_until="networkidle")
    expect(page.locator("#view-analytics")).to_have_class("view-pane active")

    live_ui.harvest_telemetry()
    _assert_browser_clean(live_ui)


# ---------------------------------------------------------------------------
# 2. Create Issue Modal & Full Issue Detail (Every Field, Clear, Rename, Lifecycle)
# ---------------------------------------------------------------------------


def test_02_create_modal_and_issue_detail_every_field_clear_rename_lifecycle(
    live_ui: LiveUISession, tmp_path: Path
) -> None:
    page = live_ui.page
    ws = live_ui.ws

    # Seed reference entities (epic parent, sprint, milestone) so Create & Detail dropdowns have options
    epic_resp = ws.api("POST", "/api/v1/issues", json={"name": "Platform Reliability Epic", "category": "epic"})
    assert epic_resp.status_code == 201
    epic_key = epic_resp.json()["key"]

    sp_resp = ws.api("POST", "/api/v1/sprints", json={"name": "Sprint Create-1", "goal": "Core stability"})
    assert sp_resp.status_code == 201
    sprint_id = sp_resp.json()["id"]

    ms_resp = ws.api("POST", "/api/v1/milestones", json={"name": "v1.0 Launch", "due_date": "2027-06-01"})
    assert ms_resp.status_code == 201
    milestone_id = ms_resp.json()["id"]

    embed_png, _ = make_png(tmp_path / "create-inline.png", width=40, height=28, color=(16, 185, 129))
    desc_extra_png, _ = make_png(tmp_path / "desc-extra.png", width=36, height=24, color=(59, 130, 246))
    drop_txt, _ = make_text_file(tmp_path / "boot-trace.txt", "WARN token skew=42ms\n")
    pick_json, _ = make_json_file(tmp_path / "env-config.json", {"region": "eu-central", "retries": 3})

    _install_fetch_idle_tracker(live_ui)
    page.reload(wait_until="networkidle")
    _wait_ui_settled(page)

    # Open Create Issue Modal & test client/server validation errors
    page.locator("#btn-open-create").click()
    create_modal = page.locator('#modal-create-issue[data-modal="create-issue"]')
    expect(create_modal).to_have_class("modal-backdrop open")

    # 1) Empty required name -> error toast
    page.locator("#create-name").fill("")
    page.locator("#btn-submit-create-issue").click()
    expect(page.locator("#toast-container .toast.toast-error").last).to_be_visible()
    expect(create_modal).to_have_class("modal-backdrop open")

    # 2) Negative story points -> error toast
    page.locator("#create-name").fill("Invalid Negative Points Check")
    page.locator("#create-points").fill("-5")
    page.locator("#btn-submit-create-issue").click()
    expect(page.locator("#toast-container .toast.toast-error").last).to_be_visible()
    expect(create_modal).to_have_class("modal-backdrop open")

    # 3) Populate EVERY field in Create Modal + inline embed + drag-and-drop & picker attachments
    initial_title = "OAuth2 Token Refresh — Race & Café Fix #1"
    page.locator("#create-name").fill(initial_title)
    page.locator("#create-category").select_option("bug")
    page.locator("#create-state").select_option("in-progress")
    page.locator("#create-priority").select_option("critical")
    page.locator("#create-severity").select_option("sev1")
    page.locator("#create-assignee").fill("alice")
    page.locator("#create-parent").fill(epic_key)
    page.locator("#create-due").fill("2020-01-10")
    page.locator("#create-sprint").select_option(sprint_id)
    page.locator("#create-milestone").select_option(milestone_id)
    page.locator("#create-points").fill("5")
    page.locator("#create-estimate").fill("120")
    page.locator("#create-tags").fill("auth, oauth2")
    page.locator("#create-components").fill("gateway, token-service")
    page.locator("#create-description").fill("Investigating **401 race** under concurrent refresh.")
    page.set_input_files("#create-embed-input", str(embed_png))
    expect(page.locator("#create-embeds-queued")).to_have_text("1 embed(s) queued")
    assert "![create-inline.png](embed:create-inline.png)" in page.locator("#create-description").input_value()

    page.locator("#create-ac").fill("Single flight lock acquired\nConcurrent callers await shared promise")
    page.locator("#create-custom-fields").fill("env=prod\nservice_tier=tier-0")

    _dispatch_file_drop(page, "#create-attach-dropzone", drop_txt, "text/plain")
    expect(page.locator("#create-attach-queued")).to_contain_text("boot-trace.txt")
    page.set_input_files("#create-attach-input", str(pick_json))
    expect(page.locator("#create-attach-queued")).to_contain_text("env-config.json")

    with page.expect_response(lambda r: r.url.endswith("/api/v1/issues") and r.request.method == "POST" and r.status == 201) as create_info:
        page.locator("#btn-submit-create-issue").click()
    created_key = create_info.value.json()["key"]

    # Create modal closes and `#issue-drawer` opens automatically with created issue
    expect(create_modal).not_to_have_class("modal-backdrop open")
    drawer = page.locator('#issue-drawer[data-modal="issue-detail"]')
    expect(drawer).to_have_class("open")
    expect(page.locator("#detail-key-badge")).to_have_text(created_key)
    expect(page.locator("#detail-title-input")).to_have_value(initial_title)
    assert (ws.issues_dir / initial_title / "issue.json").is_file()

    # Wait until initial embeds + attachments uploaded during create finish rendering in drawer
    expect(page.locator("#detail-attachments-list .issue-att-row")).to_have_count(2)
    expect(page.locator("#detail-desc-embeds-bar .btn-del-desc-embed")).to_have_count(1)
    _wait_ui_settled(page)
    assert _wait_img_natural_width(page.locator("#detail-desc-rendered img.md-inline-embed").first) == 40

    # Inline Title Rename: (a) via `#btn-save-title` button, (b) via `Enter` key, verifying folder rename on disk
    renamed_by_btn = "OAuth2 Token Refresh — Renamed Button Ω"
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: (
            page.locator("#detail-title-input").fill(renamed_by_btn),
            page.locator("#btn-save-title").click(),
        ),
    )
    expect(page.locator("#detail-title-input")).to_have_value(renamed_by_btn)
    assert not (ws.issues_dir / initial_title).exists()
    assert (ws.issues_dir / renamed_by_btn / "issue.json").is_file()

    renamed_by_enter = "OAuth2 Token Refresh — Renamed Enter Key"
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: (
            page.locator("#detail-title-input").fill(renamed_by_enter),
            page.locator("#detail-title-input").press("Enter"),
        ),
    )
    expect(page.locator("#detail-title-input")).to_have_value(renamed_by_enter)
    assert not (ws.issues_dir / renamed_by_btn).exists()
    assert (ws.issues_dir / renamed_by_enter / "issue.json").is_file()

    # Description edit / cancel / save / embed image / delete embed / clear description
    page.locator("#btn-edit-desc").click()
    expect(page.locator("#detail-desc-editor")).to_be_visible()
    page.locator("#detail-desc-textarea").fill("Unsaved draft text")
    page.locator("#btn-cancel-desc").click()
    expect(page.locator("#detail-desc-editor")).to_be_hidden()

    page.locator("#btn-edit-desc").click()
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: (
            page.locator("#detail-desc-textarea").fill("Updated **Markdown** description body."),
            page.locator("#btn-save-desc").click(),
        ),
    )
    expect(page.locator("#detail-desc-rendered")).to_contain_text("Updated Markdown description body.")

    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: page.set_input_files("#detail-desc-embed-input", str(desc_extra_png)),
    )
    expect(page.locator("#detail-desc-embeds-bar .btn-del-desc-embed")).to_have_count(2)
    expect(page.locator("#detail-desc-rendered img.md-inline-embed")).to_have_count(1)
    assert _wait_img_natural_width(page.locator("#detail-desc-rendered img.md-inline-embed").first) == 36

    # Remove newly added description embed, then clear entire description
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: page.locator("#detail-desc-embeds-bar .btn-del-desc-embed").last.click(),
    )
    expect(page.locator("#detail-desc-embeds-bar .btn-del-desc-embed")).to_have_count(1)

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-desc").click())
    expect(page.locator("#detail-desc-rendered")).to_contain_text("No content.")

    # Exercise EVERY metadata sidebar control + EVERY explicit Clear button
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-category").select_option("security"))
    expect(page.locator("#detail-category")).to_have_value("security")

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-state").select_option("in-review"))
    expect(page.locator("#detail-state")).to_have_value("in-review")

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-priority").select_option("high"))
    expect(page.locator("#detail-priority")).to_have_value("high")

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-severity").select_option("sev2"))
    expect(page.locator("#detail-severity")).to_have_value("sev2")

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-resolution").select_option("fixed"))
    expect(page.locator("#detail-resolution")).to_have_value("fixed")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-resolution").click())
    expect(page.locator("#detail-resolution")).to_have_value("")

    _set_detail_input(page, created_key, "#detail-assignee", "charlie")
    expect(page.locator("#detail-assignee")).to_have_value("charlie")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-assignee").click())
    expect(page.locator("#detail-assignee")).to_have_value("")

    _set_detail_input(page, created_key, "#detail-reporter", "qa-lead")
    expect(page.locator("#detail-reporter")).to_have_value("qa-lead")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-reporter").click())
    expect(page.locator("#detail-reporter")).to_have_value("")

    expect(page.locator("#detail-parent")).to_have_value(epic_key)
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-parent").click())
    expect(page.locator("#detail-parent")).to_have_value("")
    _set_detail_input(page, created_key, "#detail-parent", epic_key)
    expect(page.locator("#detail-parent")).to_have_value(epic_key)

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-sprint").click())
    expect(page.locator("#detail-sprint")).to_have_value("")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-sprint").select_option(sprint_id))
    expect(page.locator("#detail-sprint")).to_have_value(sprint_id)

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-milestone").click())
    expect(page.locator("#detail-milestone")).to_have_value("")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#detail-milestone").select_option(milestone_id))
    expect(page.locator("#detail-milestone")).to_have_value(milestone_id)

    _set_detail_input(page, created_key, "#detail-points", "8")
    expect(page.locator("#detail-points")).to_have_value("8")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-points").click())
    expect(page.locator("#detail-points")).to_have_value("")

    _set_detail_input(page, created_key, "#detail-estimate", "180")
    expect(page.locator("#detail-estimate")).to_have_value("180")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-estimate").click())
    expect(page.locator("#detail-estimate")).to_have_value("")

    # Overdue badge active from past due date `2020-01-10`; clear due date and verify overdue badge clears
    expect(page.locator("#detail-overdue-badge")).to_contain_text("Overdue")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-due").click())
    expect(page.locator("#detail-due")).to_have_value("")
    expect(page.locator("#detail-overdue-badge")).to_have_text("")
    _set_detail_input(page, created_key, "#detail-due", "2020-05-01")
    expect(page.locator("#detail-overdue-badge")).to_contain_text("Overdue")

    _set_detail_input(page, created_key, "#detail-tags", "sec, token, urgent")
    expect(page.locator("#detail-tags")).to_have_value("sec, token, urgent")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-tags").click())
    expect(page.locator("#detail-tags")).to_have_value("")

    _set_detail_input(page, created_key, "#detail-components", "auth-core, proxy")
    expect(page.locator("#detail-components")).to_have_value("auth-core, proxy")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-components").click())
    expect(page.locator("#detail-components")).to_have_value("")

    # Custom Fields: add key=value, unset single key, clear all custom fields
    expect(page.locator("#detail-cf-list")).to_contain_text("env")
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: (
            page.locator("#detail-cf-key").fill("cluster"),
            page.locator("#detail-cf-val").fill("us-east-1"),
            page.locator("#btn-add-cf").click(),
        ),
    )
    expect(page.locator("#detail-cf-list")).to_contain_text("cluster")
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: page.locator('#detail-cf-list .btn-unset-cf[data-cf-key="env"]').click(),
    )
    expect(page.locator("#detail-cf-list")).not_to_contain_text("env")
    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-cf").click())
    expect(page.locator("#detail-cf-list")).to_have_text("")

    # Acceptance Criteria: add, check/uncheck + progress bar, inline edit, delete single, clear all
    expect(page.locator("#detail-ac-summary")).to_have_text("0/2")
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: (
            page.locator("#detail-ac-input").fill("Zero duplicate refresh tokens emitted"),
            page.locator("#btn-add-ac").click(),
        ),
    )
    expect(page.locator("#detail-ac-summary")).to_have_text("0/3")

    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: page.locator("#detail-ac-list .ac-check-toggle").first.check(),
    )
    expect(page.locator("#detail-ac-summary")).to_have_text("1/3")
    assert page.locator("#detail-ac-progress").evaluate("el => parseFloat(el.style.width)") > 30.0

    page.locator("#detail-ac-list .btn-edit-ac").first.click()
    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: (
            page.locator(".ac-inline-edit-input").fill("Single flight mutex verified under load"),
            page.locator(".btn-save-ac-inline").click(),
        ),
    )
    expect(page.locator("#detail-ac-list")).to_contain_text("Single flight mutex verified under load")

    _wait_issue_detail_refresh(
        page,
        created_key,
        lambda: page.locator("#detail-ac-list .btn-del-ac").last.click(),
    )
    expect(page.locator("#detail-ac-summary")).to_have_text("1/2")

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-clear-ac").click())
    expect(page.locator("#detail-ac-summary")).to_have_text("0/0")

    # Header lifecycle actions: Copy Agent Context, Archive / Unarchive, Clone, Delete (Cancel + Confirm)
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{created_key}/context") and r.status == 200):
        page.locator("#btn-copy-context").click()
    page.wait_for_function("() => typeof window.__lastAgentContextPack === 'string' && window.__lastAgentContextPack.length > 0")
    ctx_pack = page.evaluate("() => window.__lastAgentContextPack")
    assert isinstance(ctx_pack, str) and created_key in ctx_pack and renamed_by_enter in ctx_pack

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-archive-issue").click())
    expect(page.locator("#detail-status-banners")).to_contain_text("ARCHIVED")
    expect(page.locator("#btn-unarchive-issue")).to_be_visible()

    _wait_issue_detail_refresh(page, created_key, lambda: page.locator("#btn-unarchive-issue").click())
    expect(page.locator("#detail-status-banners")).not_to_contain_text("ARCHIVED")
    expect(page.locator("#btn-archive-issue")).to_be_visible()

    # Clone issue -> opens cloned issue in `#issue-drawer`
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{created_key}/clone") and r.status == 201) as clone_resp_info:
        page.locator("#btn-clone-issue").click()
    cloned_data = clone_resp_info.value.json()
    cloned_key = cloned_data["key"]
    cloned_name = cloned_data["name"]
    expect(page.locator("#detail-key-badge")).to_have_text(cloned_key)
    assert (ws.issues_dir / cloned_name / "issue.json").is_file()

    # Delete Confirm Modal on cloned issue: Cancel first, then Confirm Delete
    del_modal = page.locator('#modal-delete-confirm[data-modal="delete-confirm"]')
    page.locator("#btn-delete-issue").click()
    expect(del_modal).to_have_class("modal-backdrop open")
    page.locator("#btn-cancel-delete").click()
    expect(del_modal).not_to_have_class("modal-backdrop open")
    assert (ws.issues_dir / cloned_name).is_dir()

    page.locator("#btn-delete-issue").click()
    expect(del_modal).to_have_class("modal-backdrop open")
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{cloned_key}") and r.request.method == "DELETE"):
        page.locator("#btn-confirm-delete").click()
    expect(del_modal).not_to_have_class("modal-backdrop open")
    expect(drawer).not_to_have_class("open")
    assert not (ws.issues_dir / cloned_name).exists()

    _assert_browser_clean(live_ui)


# ---------------------------------------------------------------------------
# 3. Rich Comments, 3 Inline Embed Methods (incl. Clipboard Paste) & Attachments
# ---------------------------------------------------------------------------


def test_03_rich_comments_three_embed_methods_clipboard_paste_and_attachments(
    live_ui: LiveUISession, tmp_path: Path
) -> None:
    page = live_ui.page
    ws = live_ui.ws

    ref_issue = ws.api("POST", "/api/v1/issues", json={"name": "Parent Context Reference", "category": "epic"}).json()
    target_issue = ws.api("POST", "/api/v1/issues", json={"name": "Rich Media & XSS Audit Issue", "category": "bug"}).json()
    issue_key = target_issue["key"]

    # Create synthetic standards-compliant files with known dimensions & SHA-256 digests
    whole_log_text = "2026-11-24T12:00:00Z ERROR Panic in worker pool\nStack frame #0\n"
    whole_log_path, whole_log_sha = make_text_file(tmp_path / "crash-trace.log", whole_log_text)
    whole_png_path, whole_png_sha = make_png(tmp_path / "architecture-shot.png", width=48, height=32, color=(37, 99, 235))
    whole_svg_path, _ = make_svg(tmp_path / "topology.svg", label="mesh-v2", width=120, height=60)

    emb_input_png, _ = make_png(tmp_path / "method1-input.png", width=28, height=28, color=(220, 38, 38))
    emb_drop_png, _ = make_png(tmp_path / "method2-drop.png", width=36, height=24, color=(245, 158, 11))
    emb_paste_png, _ = make_png(tmp_path / "method3-clipboard-source.png", width=44, height=30, color=(139, 92, 246))
    cmt_att_txt, _ = make_text_file(tmp_path / "repro-steps.txt", "1. Send concurrent POST\n2. Observe status\n")

    _install_fetch_idle_tracker(live_ui)
    page.reload(wait_until="networkidle")
    _wait_ui_settled(page)
    page.locator(f'.issue-open-link[data-key="{issue_key}"]').first.click()
    expect(page.locator("#issue-drawer")).to_have_class("open")
    _wait_ui_settled(page)

    # Upload whole-issue attachments via Drag-and-Drop (`#detail-attach-dropzone`) + file picker (`#detail-attach-input`)
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: _dispatch_file_drop(page, "#detail-attach-dropzone", whole_log_path, "text/plain"),
    )
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: _dispatch_file_drop(page, "#detail-attach-dropzone", whole_png_path, "image/png"),
    )
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: page.set_input_files("#detail-attach-input", str(whole_svg_path)),
    )
    expect(page.locator("#detail-attachments-list .issue-att-row")).to_have_count(3)

    # Preview `.log` text attachment in `[data-modal="attachment-preview"]`
    preview_modal = page.locator('#modal-attachment-preview[data-modal="attachment-preview"]')
    page.locator("#detail-attachments-list .issue-att-row", has_text="crash-trace.log").locator(".btn-att-preview").click()
    expect(preview_modal).to_have_class("modal-backdrop open")
    expect(page.locator("#att-preview-content")).to_have_text(whole_log_text)
    expect(page.locator("#att-preview-meta")).to_contain_text(whole_log_sha)
    page.locator('[data-close-modal="attachment-preview"]').first.click()
    expect(preview_modal).not_to_have_class("modal-backdrop open")

    # Preview `.png` whole-issue image in `[data-modal="image-lightbox"]`
    lightbox_modal = page.locator('#modal-image-lightbox[data-modal="image-lightbox"]')
    page.locator("#detail-attachments-list .issue-att-row", has_text="architecture-shot.png").locator(".btn-att-lightbox").click()
    expect(lightbox_modal).to_have_class("modal-backdrop open")
    expect(page.locator("#lightbox-title")).to_have_text("architecture-shot.png")
    assert _wait_img_natural_width(page.locator("#lightbox-img")) == 48
    page.locator('[data-close-modal="image-lightbox"]').first.click()
    expect(lightbox_modal).not_to_have_class("modal-backdrop open")

    # Download `architecture-shot.png` via browser download & verify exact SHA-256 byte integrity
    with page.expect_download() as dl_info:
        page.locator("#detail-attachments-list .issue-att-row", has_text="architecture-shot.png").locator(".btn-att-download").click()
    dl_target = tmp_path / "downloaded-architecture-shot.png"
    dl_info.value.save_as(str(dl_target))
    assert sha256_file(dl_target) == whole_png_sha

    # Compose Rich Markdown Comment with XSS payloads + ALL 3 Inline Image Embed Methods + Comment Attachment + Linked Issue Attachments
    rich_markdown = (
        "### Root Cause Summary\n"
        f"Cross-referencing #{ref_issue['key']} with **bold**, *italic*, ~~strike~~, and `inline_code()`.\n\n"
        "| Component | Status |\n"
        "| --- | --- |\n"
        "| TokenPool | Verified |\n\n"
        "- [x] Captured heap dump\n"
        "- [ ] Deployed canary\n\n"
        "```python\n"
        "def verify_lock():\n"
        "    return True\n"
        "```\n\n"
        '<script>window.__xss = 1;</script><img src="x" onerror="window.__xss = 1" />'
        "[malicious](javascript:window.__xss=1)"
    )
    page.locator("#cmt-body-input").fill(rich_markdown)

    # Embed Method (a): File input `#cmt-embed-input`
    page.set_input_files("#cmt-embed-input", str(emb_input_png))
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Embed: method1-input.png")

    # Embed Method (b): Drag-and-drop image onto `#cmt-composer-dropzone`
    _dispatch_file_drop(page, "#cmt-composer-dropzone", emb_drop_png, "image/png")
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Embed: method2-drop.png")

    # Embed Method (c): Clipboard `paste` event carrying `image/png` File (`name: "image.png"`) onto `#cmt-body-input`
    _dispatch_clipboard_image_paste(page, "#cmt-body-input", emb_paste_png)
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Embed: pasted-image-3.png")
    assert "![pasted-image-3.png](embed:pasted-image-3.png)" in page.locator("#cmt-body-input").input_value()

    # Comment-scoped file attachment via `#cmt-attach-input`
    page.set_input_files("#cmt-attach-input", str(cmt_att_txt))
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Attach: repro-steps.txt")

    # Link existing whole-issue attachments (`crash-trace.log` and `architecture-shot.png`) via `[data-modal="link-attachment"]`
    link_att_modal = page.locator('#modal-link-attachment[data-modal="link-attachment"]')
    page.locator("#btn-cmt-link-att").click()
    expect(link_att_modal).to_have_class("modal-backdrop open")
    page.locator("#link-att-picker-list label", has_text="crash-trace.log").locator("input.link-att-check").check()
    page.locator("#link-att-picker-list label", has_text="architecture-shot.png").locator("input.link-att-check").check()
    page.locator("#btn-confirm-link-att").click()
    expect(link_att_modal).not_to_have_class("modal-backdrop open")
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Linked: ATT-")

    # Switch to Preview tab (`#cmt-tab-preview`) -> verify live Markdown, staged blob embeds & XSS neutralization
    page.locator("#cmt-tab-preview").click()
    preview_box = page.locator("#cmt-preview-box")
    expect(preview_box).to_be_visible()
    expect(preview_box.locator("table")).to_have_count(1)
    expect(preview_box.locator("pre code")).to_have_count(1)
    expect(preview_box.locator("input.md-task-checkbox")).to_have_count(2)
    expect(preview_box.locator(f'a.md-issue-mention[data-issue-ref="{ref_issue["key"]}"]')).to_have_count(1)
    expect(preview_box.locator("img.md-inline-embed")).to_have_count(3)
    assert preview_box.locator("script").count() == 0
    assert page.evaluate("() => window.__xss") is None

    # Switch back to Write tab & submit comment
    page.locator("#cmt-tab-write").click()
    _wait_issue_detail_refresh(page, issue_key, lambda: page.locator("#btn-submit-comment").click())

    cmt_card = page.locator("#detail-comments-list .comment-card").first
    expect(cmt_card).to_be_visible()
    expect(page.locator("#detail-comment-count")).to_have_text("1")

    # Verify rendered Markdown + XSS safety + all 3 inline embedded images loaded with naturalWidth > 0
    rendered_body = cmt_card.locator(".comment-rendered-body")
    expect(rendered_body.locator("table")).to_have_count(1)
    expect(rendered_body.locator("pre code")).to_have_count(1)
    expect(rendered_body.locator("input.md-task-checkbox")).to_have_count(2)
    expect(rendered_body.locator("img.md-inline-embed")).to_have_count(3)
    assert rendered_body.locator("script").count() == 0
    assert page.evaluate("() => window.__xss") is None

    widths = rendered_body.locator("img.md-inline-embed").evaluate_all(
        """imgs => Promise.all(imgs.map(img => new Promise(resolve => {
            if (img.complete && img.naturalWidth > 0) return resolve(img.naturalWidth);
            img.addEventListener('load', () => resolve(img.naturalWidth), { once: true });
            img.addEventListener('error', () => resolve(0), { once: true });
        })))"""
    )
    assert widths == [28, 36, 44], f"Expected decoded inline image widths [28, 36, 44], got {widths}"

    # Click rendered inline image inside comment -> opens `[data-modal="image-lightbox"]`
    rendered_body.locator("img.md-inline-embed").nth(2).click()
    expect(lightbox_modal).to_have_class("modal-backdrop open")
    assert _wait_img_natural_width(page.locator("#lightbox-img")) == 44
    page.locator('[data-close-modal="image-lightbox"]').first.click()
    expect(lightbox_modal).not_to_have_class("modal-backdrop open")

    # Preview comment-scoped attachment (`repro-steps.txt`) via `.btn-preview-cmt-att`, then delete it via `.btn-del-cmt-att`
    cmt_card.locator(".btn-preview-cmt-att").click()
    expect(preview_modal).to_have_class("modal-backdrop open")
    expect(page.locator("#att-preview-content")).to_contain_text("1. Send concurrent POST")
    page.locator('[data-close-modal="attachment-preview"]').first.click()
    expect(preview_modal).not_to_have_class("modal-backdrop open")

    _wait_issue_detail_refresh(page, issue_key, lambda: cmt_card.locator(".btn-del-cmt-att").click())
    expect(cmt_card).not_to_contain_text("repro-steps.txt")
    expect(cmt_card.locator(".comment-rendered-body img.md-inline-embed")).to_have_count(3)

    # Delete a single inline embedded image (`method1-input.png`) from the comment via `.btn-del-cmt-embed`
    _wait_issue_detail_refresh(page, issue_key, lambda: cmt_card.locator(".btn-del-cmt-embed").first.click())
    expect(cmt_card.locator(".comment-rendered-body img.md-inline-embed")).to_have_count(2)
    expect(cmt_card.locator(".comment-rendered-body")).not_to_contain_text("attachment://")

    # Unlink `architecture-shot.png` from comment via `.btn-unlink-cmt-att` (confirm whole-issue attachment stays intact)
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: cmt_card.locator(".badge", has_text="Linked: architecture-shot.png").locator(".btn-unlink-cmt-att").click(),
    )
    expect(cmt_card).not_to_contain_text("Linked: architecture-shot.png")
    expect(page.locator("#detail-attachments-list .issue-att-row", has_text="architecture-shot.png")).to_have_count(1)

    # Delete whole-issue attachment `crash-trace.log` (still linked to the comment) -> graceful `[Deleted attachment: crash-trace.log]`
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: page.locator("#detail-attachments-list .issue-att-row", has_text="crash-trace.log").locator(".btn-att-delete").click(),
    )
    expect(cmt_card.locator(".deleted-att-badge")).to_contain_text("[Deleted attachment: crash-trace.log]")

    # Edit existing comment via `.btn-edit-comment` -> verify `edited` badge & updated Markdown
    cmt_card.locator(".btn-edit-comment").click()
    edit_ta = cmt_card.locator(".comment-edit-textarea")
    expect(edit_ta).to_be_visible()
    updated_body = edit_ta.input_value() + "\n\nPost-fix verification complete."
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: (
            edit_ta.fill(updated_body),
            cmt_card.locator(".btn-save-comment-edit").click(),
        ),
    )
    expect(cmt_card).to_contain_text("Post-fix verification complete.")
    expect(cmt_card.locator('.badge[title="Edited"]')).to_have_text("edited")

    # Post a second disposable comment and delete it via `.btn-delete-comment`
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: (
            page.locator("#cmt-body-input").fill("Temporary comment to test deletion"),
            page.locator("#btn-submit-comment").click(),
        ),
    )
    expect(page.locator("#detail-comment-count")).to_have_text("2")
    _wait_issue_detail_refresh(
        page,
        issue_key,
        lambda: page.locator("#detail-comments-list .comment-card").last.locator(".btn-delete-comment").click(),
    )
    expect(page.locator("#detail-comment-count")).to_have_text("1")

    # Click `#ITR-1` mention link inside comment -> navigates drawer directly to referenced issue
    cmt_card.locator(f'a.md-issue-mention[data-issue-ref="{ref_issue["key"]}"]').click()
    expect(page.locator("#detail-key-badge")).to_have_text(ref_issue["key"])

    _assert_browser_clean(live_ui)


# ---------------------------------------------------------------------------
# 4. Issues List: Preset Tabs, Filters, iQL Search, Sorting, Pagination & Bulk Bar
# ---------------------------------------------------------------------------


def test_04_issues_list_tabs_filters_iql_search_sorting_pagination_and_bulk_ops(live_ui: LiveUISession) -> None:
    page = live_ui.page
    ws = live_ui.ws

    sp = ws.api("POST", "/api/v1/sprints", json={"name": "Sprint List-1", "goal": "Pagination & Filters"}).json()
    ms = ws.api("POST", "/api/v1/milestones", json={"name": "Milestone List-1"}).json()

    # Seed 32 diverse issues across states, categories, priorities, severities, assignees, tags, points, due dates
    states_cycle = ["triage", "backlog", "todo", "ready", "in-progress", "in-review", "blocked", "done", "cancelled", "archived"]
    cats_cycle = ["bug", "feature", "task", "story", "security", "chore"]
    prios_cycle = ["critical", "high", "medium", "low", "trivial"]
    sevs_cycle = ["sev1", "sev2", "sev3", "sev4", "sev5"]

    created_keys: list[str] = []
    for idx in range(1, 33):
        st = states_cycle[(idx - 1) % len(states_cycle)]
        payload: dict[str, Any] = {
            "name": f"Catalog Item {idx:02d} — ZetaSearchToken" if idx == 7 else f"Catalog Item {idx:02d}",
            "category": cats_cycle[(idx - 1) % len(cats_cycle)],
            "state": st,
            "priority": prios_cycle[(idx - 1) % len(prios_cycle)],
            "severity": sevs_cycle[(idx - 1) % len(sevs_cycle)],
            "assignee": "alice" if idx % 3 == 0 else ("bob" if idx % 3 == 1 else None),
            "story_points": float((idx % 8) + 1),
            "tags": ["alpha-tag"] if idx <= 5 else (["beta-tag"] if idx <= 10 else []),
            "sprint": sp["id"] if idx <= 6 else None,
            "milestone": ms["id"] if idx <= 4 else None,
            "due_date": "2020-03-01" if (idx in (2, 3) and st not in ("done", "cancelled", "archived")) else None,
        }
        resp = ws.api("POST", "/api/v1/issues", json=payload)
        assert resp.status_code == 201
        created_keys.append(resp.json()["key"])

    page.reload(wait_until="networkidle")
    expect(page.locator("#page-summary")).to_contain_text("of 32")

    # Click all 8 preset state tabs (`PRESET_TABS`)
    for tab_name in PRESET_TABS:
        chip = page.locator(f'#issues-preset-tabs .tab-chip[data-tab="{tab_name}"]')
        with page.expect_response(lambda r: "/api/v1/issues?" in r.url and f"tab={tab_name}" in r.url):
            chip.click()
        expect(chip).to_have_class("tab-chip active")

    # Exercise filter dropdowns/inputs & Clear Filters
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "category=bug" in r.url):
        page.locator("#filter-category").select_option("bug")
    expect(page.locator("#issues-tbody tr.issue-row").first).to_contain_text("bug")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "priority=critical" in r.url):
        page.locator("#filter-priority").select_option("critical")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url):
        page.locator("#btn-clear-filters").click()
    expect(page.locator("#page-summary")).to_contain_text("of 32")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "state=done" in r.url):
        page.locator("#filter-state").select_option("done")
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url):
        page.locator("#btn-clear-filters").click()

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "severity=sev1" in r.url):
        page.locator("#filter-severity").select_option("sev1")
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "assignee=alice" in r.url):
        page.locator("#filter-assignee").fill("alice")
        page.locator("#filter-assignee").dispatch_event("change")
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url):
        page.locator("#btn-clear-filters").click()

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "tag=alpha-tag" in r.url):
        page.locator("#filter-tag").select_option("alpha-tag")
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(5)

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and f"sprint={sp['id']}" in r.url):
        page.locator("#filter-sprint").select_option(sp["id"])
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and f"milestone={ms['id']}" in r.url):
        page.locator("#filter-milestone").select_option(ms["id"])
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(4)

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url):
        page.locator("#btn-clear-filters").click()

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "overdue=true" in r.url):
        page.locator("#filter-overdue").check()
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(2)
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url):
        page.locator("#btn-clear-filters").click()

    # Global search bar: full-text query, structured iQL query, and zero-match empty state
    with page.expect_response(lambda r: "/api/v1/search?" in r.url):
        page.locator("#global-search-input").fill("ZetaSearchToken")
        page.locator("#btn-run-search").click()
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(1)
    expect(page.locator("#issues-tbody tr.issue-row").first).to_contain_text("Catalog Item 07")

    with page.expect_response(lambda r: "/api/v1/search?" in r.url):
        page.locator("#global-search-input").fill(f"sprint = {sp['id']} AND priority IN (critical, high)")
        page.locator("#global-search-input").press("Enter")
    expect(page.locator("#issues-tbody tr.issue-row").nth(1)).to_be_visible()

    with page.expect_response(lambda r: "/api/v1/search?" in r.url):
        page.locator("#global-search-input").fill("no_matching_issue_xyz_98765")
        page.locator("#btn-run-search").click()
    expect(page.locator("#issues-empty-state")).to_be_visible()
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url):
        page.locator("#btn-empty-clear-filters").click()
    expect(page.locator("#issues-empty-state")).to_be_hidden()
    expect(page.locator("#page-summary")).to_contain_text("of 32")

    # Column header sorting (`asc` and `desc`)
    for sort_col in ("priority", "story_points", "name", "due_date", "key"):
        th = page.locator(f'table.issues-table th[data-sort="{sort_col}"]')
        with page.expect_response(lambda r: "/api/v1/issues?" in r.url and f"sort={sort_col}" in r.url):
            th.click()
        with page.expect_response(lambda r: "/api/v1/issues?" in r.url and f"sort={sort_col}" in r.url):
            th.click()
    # Leave sorted by `key` asc
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "order=asc" in r.url):
        page.locator('table.issues-table th[data-sort="key"]').click()

    # Pagination controls (`10` per page -> 4 pages across 32 issues)
    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "page_size=10" in r.url):
        page.locator("#page-size-select").select_option("10")
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(10)
    expect(page.locator("#page-summary")).to_have_text("10 of 32 (Page 1/4)")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "page=2" in r.url):
        page.locator('.page-num-btn[data-page="2"]').click()
    expect(page.locator("#page-summary")).to_have_text("10 of 32 (Page 2/4)")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "page=3" in r.url):
        page.locator("#btn-page-next").click()
    expect(page.locator("#page-summary")).to_have_text("10 of 32 (Page 3/4)")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "page=4" in r.url):
        page.locator("#btn-page-last").click()
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(2)
    expect(page.locator("#page-summary")).to_have_text("2 of 32 (Page 4/4)")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "page=3" in r.url):
        page.locator("#btn-page-prev").click()
    expect(page.locator("#page-summary")).to_have_text("10 of 32 (Page 3/4)")

    with page.expect_response(lambda r: "/api/v1/issues?" in r.url and "page=1" in r.url):
        page.locator("#btn-page-first").click()
    expect(page.locator("#page-summary")).to_have_text("10 of 32 (Page 1/4)")

    for ps in ("25", "50", "100"):
        with page.expect_response(lambda r: "/api/v1/issues?" in r.url and f"page_size={ps}" in r.url):
            page.locator("#page-size-select").select_option(ps)
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(32)

    # Bulk Operations Bar: check-all toggle, multi-row bulk update, bulk archive, bulk delete
    bulk_bar = page.locator("#bulk-action-bar")
    page.locator("#check-all-issues").check()
    expect(bulk_bar).to_have_class("bulk-bar open")
    expect(page.locator("#bulk-selected-count")).to_have_text("32")
    page.locator("#check-all-issues").uncheck()
    expect(bulk_bar).not_to_have_class("bulk-bar open")

    k1, k2, k3 = created_keys[0], created_keys[1], created_keys[2]
    for k in (k1, k2, k3):
        page.locator(f'.issue-row-check[data-key="{k}"]').check()
    expect(bulk_bar).to_have_class("bulk-bar open")
    expect(page.locator("#bulk-selected-count")).to_have_text("3")

    page.locator("#bulk-state").select_option("in-qa")
    page.locator("#bulk-priority").select_option("high")
    page.locator("#bulk-assignee").fill("dana")
    page.locator("#bulk-tag").fill("bulk-verified")
    page.locator("#bulk-sprint").select_option(sp["id"])
    with page.expect_response(lambda r: r.url.endswith("/api/v1/bulk/update") and r.request.method == "POST"):
        page.locator("#btn-bulk-apply").click()
    expect(bulk_bar).not_to_have_class("bulk-bar open")
    for k in (k1, k2, k3):
        updated = ws.api("GET", f"/api/v1/issues/{k}").json()
        assert updated["state"] == "in-qa"
        assert updated["priority"] == "high"
        assert updated["assignee"] == "dana"
        assert "bulk-verified" in updated["tags"]
        assert updated["sprint"] == sp["id"]

    # Bulk Archive 2 issues
    for k in (created_keys[28], created_keys[29]):
        page.locator(f'.issue-row-check[data-key="{k}"]').check()
    with page.expect_response(lambda r: r.url.endswith("/api/v1/bulk/update") and r.request.method == "POST"):
        page.locator("#btn-bulk-archive").click()
    assert ws.api("GET", f"/api/v1/issues/{created_keys[28]}").json()["archived"] is True

    # Bulk Delete 2 disposable issues
    for k in (created_keys[30], created_keys[31]):
        page.locator(f'.issue-row-check[data-key="{k}"]').check()
    with page.expect_response(lambda r: r.url.endswith("/api/v1/bulk/delete") and r.request.method == "POST"):
        page.locator("#btn-bulk-delete").click()
    expect(page.locator("#page-summary")).to_contain_text("of 30")
    assert ws.api("GET", f"/api/v1/issues/{created_keys[30]}").status_code == 404

    _assert_browser_clean(live_ui)


# ---------------------------------------------------------------------------
# 5. Kanban, Hierarchy/Links, Tags, Sprints+Rollover, Milestones, Views, Worklogs, History, Analytics, Settings
# ---------------------------------------------------------------------------


def test_05_kanban_hierarchy_tags_sprints_milestones_views_worklogs_history_analytics_settings(
    live_ui: LiveUISession,
) -> None:
    page = live_ui.page
    ws = live_ui.ws

    # Seed core issues for Kanban, Subtasks/Links, Sprint Rollover & Analytics
    iss_a = ws.api(
        "POST",
        "/api/v1/issues",
        json={
            "name": "Kanban Drag Card Alpha",
            "category": "feature",
            "state": "todo",
            "priority": "critical",
            "assignee": "alice",
            "estimate_minutes": 120,
            "story_points": 5,
        },
    ).json()
    iss_b = ws.api(
        "POST",
        "/api/v1/issues",
        json={
            "name": "Kanban Drag Card Beta",
            "category": "bug",
            "state": "ready",
            "priority": "high",
            "assignee": "bob",
            "story_points": 3,
        },
    ).json()
    iss_done = ws.api(
        "POST",
        "/api/v1/issues",
        json={"name": "Completed Sprint Deliverable", "category": "task", "state": "done", "story_points": 8},
    ).json()

    _install_fetch_idle_tracker(live_ui)
    page.reload(wait_until="networkidle")
    _wait_ui_settled(page)

    # 1) Workspace Settings (`#view-settings`): set project title, defaults, and `in-progress` WIP limit = 1
    page.locator('button.nav-item[data-view="settings"]').click()
    expect(page.locator("#view-settings")).to_have_class("view-pane active")
    page.locator("#cfg-project-title").fill("iTrack Enterprise Suite")
    page.locator("#cfg-project-prefix").fill("ITR")
    page.locator("#cfg-default-category").select_option("feature")
    page.locator("#cfg-default-state").select_option("todo")
    page.locator("#cfg-default-priority").select_option("high")
    page.locator("#cfg-default-severity").select_option("sev2")
    page.locator("#cfg-wip-in-progress").fill("1")
    page.locator("#cfg-default-assignee").fill("alice")
    with page.expect_response(lambda r: r.url.endswith("/api/v1/config") and r.request.method == "PATCH"):
        page.locator("#btn-save-settings").click()
    expect(page.locator("#brand-title")).to_have_text("iTrack Enterprise Suite")

    # 2) Tags View (`#view-tags`): create colored tag, edit color, delete disposable tag
    page.locator('button.nav-item[data-view="tags"]').click()
    expect(page.locator("#view-tags")).to_have_class("view-pane active")

    for tname, tcolor, tdesc in (("platform", "#10b981", "Platform engineering"), ("temp-tag", "#f59e0b", "Disposable")):
        page.locator("#tag-create-name").fill(tname)
        page.locator("#tag-create-color").fill(tcolor)
        page.locator("#tag-create-desc").fill(tdesc)
        with page.expect_response(lambda r: r.url.endswith("/api/v1/tags") and r.request.method == "POST"):
            page.locator("#btn-create-tag").click()
        expect(page.locator(f'[data-tag-card="{tname}"]')).to_be_visible()

    page.locator('.tag-edit-color[data-name="platform"]').fill("#ef4444")
    with page.expect_response(lambda r: r.url.endswith("/api/v1/tags/platform") and r.request.method == "PATCH"):
        page.locator('.btn-tag-save[data-name="platform"]').click()
    assert ws.api("GET", "/api/v1/tags").json()["tags"][0]["color"] == "#ef4444"

    with page.expect_response(lambda r: r.url.endswith("/api/v1/tags/temp-tag") and r.request.method == "DELETE"):
        page.locator('.btn-tag-delete[data-name="temp-tag"]').click()
    expect(page.locator('[data-tag-card="temp-tag"]')).to_have_count(0)

    # 3) Milestones View (`#view-milestones`): create, edit, close, reopen, delete
    page.locator('button.nav-item[data-view="milestones"]').click()
    expect(page.locator("#view-milestones")).to_have_class("view-pane active")

    page.locator("#ms-create-name").fill("v2.0 GA")
    page.locator("#ms-create-desc").fill("General availability")
    page.locator("#ms-create-due").fill("2027-09-30")
    with page.expect_response(lambda r: r.url.endswith("/api/v1/milestones") and r.request.method == "POST") as ms1_info:
        page.locator("#btn-create-milestone").click()
    ms1_id = ms1_info.value.json()["id"]
    ms1_card = page.locator(f'[data-milestone-id="{ms1_id}"]')
    expect(ms1_card).to_be_visible()

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/milestones/{ms1_id}") and r.request.method == "PATCH"):
        ms1_card.locator(".btn-ms-edit").click()
    expect(ms1_card).to_contain_text("(updated)")

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/milestones/{ms1_id}/close") and r.request.method == "POST"):
        ms1_card.locator(".btn-ms-close").click()
    expect(ms1_card).to_contain_text("closed")

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/milestones/{ms1_id}/reopen") and r.request.method == "POST"):
        ms1_card.locator(".btn-ms-reopen").click()
    expect(ms1_card).to_contain_text("open")

    page.locator("#ms-create-name").fill("Disposable Milestone")
    with page.expect_response(lambda r: r.url.endswith("/api/v1/milestones") and r.request.method == "POST") as ms2_info:
        page.locator("#btn-create-milestone").click()
    ms2_id = ms2_info.value.json()["id"]
    expect(page.locator(f'[data-milestone-id="{ms2_id}"]')).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/milestones/{ms2_id}") and r.request.method == "DELETE"):
        page.locator(f'[data-milestone-id="{ms2_id}"] .btn-ms-delete').click()
    expect(page.locator(f'[data-milestone-id="{ms2_id}"]')).to_have_count(0)

    # 4) Sprints View (`#view-sprints`) & Rollover Modal (`[data-modal="sprint-rollover"]`)
    page.locator('button.nav-item[data-view="sprints"]').click()
    expect(page.locator("#view-sprints")).to_have_class("view-pane active")

    sprint_ids: list[str] = []
    for sname, sgoal in (("Sprint Wave-1", "Initial rollout"), ("Sprint Wave-2", "Rollover destination"), ("Sprint Temp", "Delete me")):
        page.locator("#sprint-create-name").fill(sname)
        page.locator("#sprint-create-goal").fill(sgoal)
        page.locator("#sprint-create-start").fill("2026-11-01")
        page.locator("#sprint-create-end").fill("2026-11-14")
        with page.expect_response(lambda r: r.url.endswith("/api/v1/sprints") and r.request.method == "POST") as sp_info:
            page.locator("#btn-create-sprint").click()
        created_sp_id = sp_info.value.json()["id"]
        expect(page.locator(f'.sprint-card[data-sprint-id="{created_sp_id}"]')).to_be_visible()
        sprint_ids.append(created_sp_id)

    sp1_id, sp2_id, sp_temp_id = sprint_ids
    sp1_card = page.locator(f'.sprint-card[data-sprint-id="{sp1_id}"]')
    expect(sp1_card).to_be_visible()

    # Assign 3 issues (`iss_a`, `iss_b`, `iss_done`) to Sprint Wave-1, waiting for DOM re-render after each assign
    for k in (iss_a["key"], iss_b["key"], iss_done["key"]):
        sp1_card.locator(".sprint-assign-input").fill(k)
        with page.expect_response(lambda r: r.url.endswith(f"/api/v1/sprints/{sp1_id}/assign") and r.request.method == "POST"):
            sp1_card.locator(".btn-sprint-assign").click()
        expect(sp1_card).to_contain_text(k)

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/sprints/{sp1_id}/remove") and r.request.method == "POST"):
        sp1_card.locator(f'.btn-sprint-remove-issue[data-issue="{iss_b["key"]}"]').click()
    expect(sp1_card).not_to_contain_text(iss_b["key"])

    # Edit Sprint Goal & Start Sprint Wave-1
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/sprints/{sp1_id}") and r.request.method == "PATCH"):
        sp1_card.locator(".btn-sprint-edit").click()
    expect(sp1_card).to_contain_text("(updated)")

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/sprints/{sp1_id}/start") and r.request.method == "POST"):
        sp1_card.locator(".btn-sprint-start").click()
    expect(sp1_card).to_contain_text("active")

    # Complete Sprint Wave-1 -> Rollover incomplete `iss_a` into Sprint Wave-2
    rollover_modal = page.locator('#modal-sprint-rollover[data-modal="sprint-rollover"]')
    sp1_card.locator(".btn-sprint-complete").click()
    expect(rollover_modal).to_have_class("modal-backdrop open")
    page.locator("#rollover-target-select").select_option(sp2_id)
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/sprints/{sp1_id}/complete") and r.request.method == "POST"):
        page.locator("#btn-confirm-sprint-complete").click()
    expect(rollover_modal).not_to_have_class("modal-backdrop open")
    expect(sp1_card).to_contain_text("completed")
    expect(page.locator(f'.sprint-card[data-sprint-id="{sp2_id}"]')).to_contain_text(iss_a["key"])

    # Delete disposable Sprint Temp
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/sprints/{sp_temp_id}") and r.request.method == "DELETE"):
        page.locator(f'.sprint-card[data-sprint-id="{sp_temp_id}"] .btn-sprint-delete').click()
    expect(page.locator(f'.sprint-card[data-sprint-id="{sp_temp_id}"]')).to_have_count(0)

    # 5) Kanban Board (`#view-board`): real drag-and-drop across columns, WIP limit highlight & all 5 swimlanes
    page.locator('button.nav-item[data-view="board"]').click()
    expect(page.locator("#view-board")).to_have_class("view-pane active")

    col_in_prog = page.locator('.kanban-column[data-state="in-progress"]')
    # Drag `iss_a` (from `todo`) -> `in-progress` (count 1/1, within WIP limit)
    with page.expect_response(lambda r: "/api/v1/board?" in r.url and r.status == 200):
        page.locator(f'.kanban-card[data-key="{iss_a["key"]}"]').drag_to(col_in_prog)
    expect(col_in_prog.locator(f'.kanban-card[data-key="{iss_a["key"]}"]')).to_be_visible()
    expect(col_in_prog.locator(".kanban-col-head")).to_contain_text("1/1")
    expect(col_in_prog.locator(".kanban-col-head")).not_to_have_class("kanban-col-head wip-exceeded")

    # Drag `iss_b` (from `ready`) -> `in-progress` (count 2/1 -> exceeds WIP limit 1 -> `.wip-exceeded`!)
    with page.expect_response(lambda r: "/api/v1/board?" in r.url and r.status == 200):
        page.locator(f'.kanban-card[data-key="{iss_b["key"]}"]').drag_to(col_in_prog)
    expect(col_in_prog.locator(f'.kanban-card[data-key="{iss_b["key"]}"]')).to_be_visible()
    expect(col_in_prog.locator(".kanban-col-head")).to_contain_text("2/1")
    expect(col_in_prog.locator(".kanban-col-head")).to_have_class("kanban-col-head wip-exceeded")

    # Switch all 5 swimlanes (`none`, `category`, `priority`, `assignee`, `sprint`)
    for mode in SWIMLANES:
        with page.expect_response(lambda r: "/api/v1/board?" in r.url and f"swimlane={mode}" in r.url):
            page.locator("#board-swimlane-select").select_option(mode)
        if mode == "none":
            expect(page.locator("#board-lanes-container .swimlane-header")).to_have_count(0)
        else:
            expect(page.locator("#board-lanes-container .swimlane-header").first).to_be_visible()
    with page.expect_response(lambda r: "/api/v1/board?" in r.url and "swimlane=none" in r.url):
        page.locator("#board-swimlane-select").select_option("none")
    expect(page.locator("#board-lanes-container .swimlane-header")).to_have_count(0)

    # Board filters
    with page.expect_response(lambda r: "/api/v1/board?" in r.url and "category=feature" in r.url):
        page.locator("#board-filter-category").select_option("feature")
    with page.expect_response(lambda r: "/api/v1/board?" in r.url and "priority=critical" in r.url):
        page.locator("#board-filter-priority").select_option("critical")
    page.locator("#board-filter-assignee").fill("alice")
    with page.expect_response(lambda r: "/api/v1/board?" in r.url and f"sprint={sp2_id}" in r.url):
        page.locator("#board-filter-sprint").select_option(sp2_id)
    page.locator("#board-filter-q").fill("Alpha")
    with page.expect_response(lambda r: "/api/v1/board?" in r.url):
        page.locator("#btn-refresh-board").click()
    expect(page.locator("#board-lanes-container .kanban-card")).to_have_count(1)

    # Reset board filters
    page.locator("#board-filter-category").select_option("")
    page.locator("#board-filter-priority").select_option("")
    page.locator("#board-filter-assignee").fill("")
    page.locator("#board-filter-sprint").select_option("")
    page.locator("#board-filter-q").fill("")
    with page.expect_response(lambda r: "/api/v1/board?" in r.url):
        page.locator("#btn-refresh-board").click()

    # 6) Subtasks, Issue Links, Circular `blocks` Rejection & Hierarchy Tree (`[data-modal="link-issue"]`)
    page.locator(f'.kanban-card[data-key="{iss_a["key"]}"]').click()
    expect(page.locator("#issue-drawer")).to_have_class("open")

    for sub_title in ("Subtask One — Schema Migration", "Subtask Two — Canary Rollout"):
        _wait_issue_detail_refresh(
            page,
            iss_a["key"],
            lambda st=sub_title: (
                page.locator("#detail-subtask-input").fill(st),
                page.locator("#btn-add-subtask").click(),
            ),
        )
    expect(page.locator("#detail-subtask-summary")).to_have_text("0/2")

    _wait_issue_detail_refresh(
        page,
        iss_a["key"],
        lambda: page.locator("#detail-subtasks-list .subtask-toggle").first.check(),
    )
    expect(page.locator("#detail-subtask-summary")).to_have_text("1/2")
    assert page.locator("#detail-subtask-progress").evaluate("el => parseFloat(el.style.width)") == 50.0

    # Link `iss_a` blocks `iss_b` via `[data-modal="link-issue"]`
    link_modal = page.locator('#modal-link-issue[data-modal="link-issue"]')
    page.locator("#btn-open-link-modal").click()
    expect(link_modal).to_have_class("modal-backdrop open")
    page.locator("#link-modal-type").select_option("blocks")
    _wait_issue_detail_refresh(
        page,
        iss_a["key"],
        lambda: (
            page.locator("#link-modal-target").fill(iss_b["key"]),
            page.locator("#btn-submit-link").click(),
        ),
    )
    expect(page.locator("#modal-tree-hierarchy")).to_contain_text(f"blocks->{iss_b['key']}")
    expect(page.locator("#modal-tree-hierarchy")).to_contain_text("Subtask One — Schema Migration")
    page.locator('[data-close-modal="link-issue"]').first.click()
    expect(link_modal).not_to_have_class("modal-backdrop open")

    # Navigate to `iss_b` via link target click and verify circular `blocks` (`iss_b` -> `iss_a`) is rejected with error toast!
    page.locator(f'#detail-links-list .link-target-open[data-key="{iss_b["key"]}"]').click()
    expect(page.locator("#detail-key-badge")).to_have_text(iss_b["key"])
    expect(page.locator("#detail-status-banners")).to_contain_text(f"BLOCKED by {iss_a['key']}")

    page.locator("#btn-open-link-modal").click()
    expect(link_modal).to_have_class("modal-backdrop open")
    page.locator("#link-modal-type").select_option("blocks")
    page.locator("#link-modal-target").fill(iss_a["key"])
    page.locator("#btn-submit-link").click()
    expect(page.locator("#toast-container .toast.toast-error").last).to_be_visible()
    page.locator('[data-close-modal="link-issue"]').first.click()
    expect(link_modal).not_to_have_class("modal-backdrop open")

    # Remove the link from `iss_b` via `.btn-remove-link`
    _wait_issue_detail_refresh(
        page,
        iss_b["key"],
        lambda: page.locator("#detail-links-list .btn-remove-link").first.click(),
    )
    expect(page.locator("#detail-links-list")).to_contain_text("No linked issues.")

    # Switch back to `iss_a` to test Worklogs (`[data-modal="log-work"]`) & Chronological History Diffs
    page.locator("#btn-close-drawer").click()
    page.locator('button.nav-item[data-view="issues"]').click()
    # Also exercise global Hierarchy Tree button in `#view-issues`
    page.locator("#btn-open-global-tree").click()
    expect(link_modal).to_have_class("modal-backdrop open")
    expect(page.locator("#modal-tree-hierarchy")).to_contain_text(iss_a["key"])
    page.locator('[data-close-modal="link-issue"]').first.click()

    page.locator(f'.issue-open-link[data-key="{iss_a["key"]}"]').first.click()
    expect(page.locator("#issue-drawer")).to_have_class("open")

    worklog_modal = page.locator('#modal-log-work[data-modal="log-work"]')
    page.locator("#btn-open-worklog-modal").click()
    expect(worklog_modal).to_have_class("modal-backdrop open")
    _wait_issue_detail_refresh(
        page,
        iss_a["key"],
        lambda: (
            page.locator("#worklog-modal-duration").fill("1h 30m"),
            page.locator("#worklog-modal-note").fill("Implemented distributed lock"),
            page.locator("#btn-submit-worklog").click(),
        ),
    )
    expect(worklog_modal).not_to_have_class("modal-backdrop open")
    expect(page.locator("#detail-time-summary")).to_have_text("90m / 120m")
    assert page.locator("#detail-time-progress").evaluate("el => parseFloat(el.style.width)") == 75.0

    # Add a second worklog and delete it via `.btn-del-worklog`
    page.locator("#btn-open-worklog-modal").click()
    _wait_issue_detail_refresh(
        page,
        iss_a["key"],
        lambda: (
            page.locator("#worklog-modal-duration").fill("15m"),
            page.locator("#worklog-modal-note").fill("Scratch benchmark"),
            page.locator("#btn-submit-worklog").click(),
        ),
    )
    expect(page.locator("#detail-time-summary")).to_have_text("105m / 120m")
    _wait_issue_detail_refresh(
        page,
        iss_a["key"],
        lambda: page.locator("#detail-worklogs-list .btn-del-worklog").last.click(),
    )
    expect(page.locator("#detail-time-summary")).to_have_text("90m / 120m")

    # History Diffs Tab (`#tab-btn-history`)
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/issues/{iss_a['key']}/history") and r.status == 200):
        page.locator("#tab-btn-history").click()
    expect(page.locator("#detail-history-pane")).to_be_visible()
    expect(page.locator("#detail-history-list")).to_contain_text("in-progress")
    expect(page.locator("#detail-history-list")).to_contain_text("created")
    page.locator("#tab-btn-comments").click()
    page.locator("#btn-close-drawer").click()

    # 7) Saved Views (`#view-views` + `#sidebar-saved-views` + `[data-modal="save-view"]`)
    save_view_modal = page.locator('#modal-save-view[data-modal="save-view"]')
    page.locator("#btn-save-current-view").click()
    expect(save_view_modal).to_have_class("modal-backdrop open")
    page.locator("#save-view-name").fill("Critical Active Queue")
    page.locator("#save-view-query").fill("priority = critical")
    page.locator("#save-view-tab").select_option("active")
    page.locator("#save-view-desc").fill("Critical items in active states")
    with page.expect_response(lambda r: r.url.endswith("/api/v1/views") and r.request.method == "POST") as vw1_info:
        page.locator("#btn-submit-save-view").click()
    vw1_id = vw1_info.value.json()["id"]
    expect(save_view_modal).not_to_have_class("modal-backdrop open")

    # Click quick-access saved view button in left sidebar (`#sidebar-saved-views .sidebar-view-btn`)
    sidebar_vw_btn = page.locator(f'#sidebar-saved-views .sidebar-view-btn[data-view-id="{vw1_id}"]')
    expect(sidebar_vw_btn).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/views/{vw1_id}/run") and r.request.method == "POST"):
        sidebar_vw_btn.click()
    expect(page.locator("#view-issues")).to_have_class("view-pane active")
    expect(page.locator("#global-search-input")).to_have_value("priority = critical")
    expect(page.locator("#issues-tbody tr.issue-row")).to_have_count(1)

    # Open `#view-views`, create disposable view, edit, run, and delete
    page.locator('button.nav-item[data-view="views"]').click()
    expect(page.locator("#view-views")).to_have_class("view-pane active")

    page.locator("#btn-views-open-save-modal").click()
    expect(save_view_modal).to_have_class("modal-backdrop open")
    page.locator("#save-view-name").fill("Disposable Saved View")
    page.locator("#save-view-query").fill("state = done")
    with page.expect_response(lambda r: r.url.endswith("/api/v1/views") and r.request.method == "POST") as vw2_info:
        page.locator("#btn-submit-save-view").click()
    vw2_id = vw2_info.value.json()["id"]
    expect(page.locator(f'[data-saved-view="{vw2_id}"]')).to_be_visible()

    vw1_card = page.locator(f'[data-saved-view="{vw1_id}"]')
    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/views/{vw1_id}") and r.request.method == "PATCH"):
        vw1_card.locator(".btn-view-edit").click()
    expect(vw1_card).to_contain_text("(updated)")

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/views/{vw2_id}") and r.request.method == "DELETE"):
        page.locator(f'[data-saved-view="{vw2_id}"] .btn-view-delete').click()
    expect(page.locator(f'[data-saved-view="{vw2_id}"]')).to_have_count(0)

    with page.expect_response(lambda r: r.url.endswith(f"/api/v1/views/{vw1_id}/run") and r.request.method == "POST"):
        vw1_card.locator(".btn-view-run").click()
    expect(page.locator("#view-issues")).to_have_class("view-pane active")

    # 8) Analytics Dashboard (`#view-analytics`)
    page.locator('button.nav-item[data-view="analytics"]').click()
    expect(page.locator("#view-analytics")).to_have_class("view-pane active")
    # 3 seeded issues + 2 created subtasks = 5 total issues (3 active, 2 done)
    expect(page.locator("#stat-total-issues")).to_have_text("5")
    expect(page.locator("#stat-active-issues")).to_have_text("3")
    expect(page.locator("#stat-done-issues")).to_contain_text("2 (40%)")
    expect(page.locator("#analytics-distributions")).to_contain_text("By State")
    expect(page.locator("#analytics-distributions")).to_contain_text("By Priority")
    expect(page.locator("#analytics-distributions")).to_contain_text("By Category")
    expect(page.locator("#analytics-distributions")).to_contain_text("By Assignee")

    # Harvest telemetry & verify 100% of all 8 UI views and all 10 UI modals were exercised across Suite 07
    live_ui.harvest_telemetry()
    events = read_surface_events()
    if events:
        hit_views = {v for ev in events for v in (ev.get("views") or [])}
        hit_modals = {m for ev in events for m in (ev.get("modals") or [])}
        assert set(UI_VIEWS).issubset(hit_views), f"Missing UI views: {set(UI_VIEWS) - hit_views}"
        assert set(UI_MODALS).issubset(hit_modals), f"Missing UI modals: {set(UI_MODALS) - hit_modals}"

    _assert_browser_clean(live_ui)
