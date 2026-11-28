"""Comprehensive Playwright Web UI E2E and live bidirectional UI <-> CLI parity tests."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.ui_e2e.conftest import LiveUIEnv


def _set_rich_html_via_source(page: Page, prefix: str, html_value: str) -> None:
    """Populate a reusable rich-text editor via its synchronized HTML source textarea."""
    html_input = page.get_by_test_id(f"{prefix}-html-input")
    if not html_input.is_visible():
        page.get_by_test_id(f"{prefix}-btn-source").click()
        expect(html_input).to_be_visible()
    html_input.fill(html_value)


def _confirm_if_modal_open(page: Page) -> None:
    """Click confirm-delete-modal button if a delete action uses modal confirmation."""
    modal = page.get_by_test_id("confirm-delete-modal")
    if modal.count() > 0 and modal.is_visible():
        page.get_by_test_id("btn-confirm-delete").click()
        expect(modal).to_be_hidden()


def _close_overlay_panel(page: Page, panel_testid: str) -> None:
    """Close an open modal/panel overlay cleanly before interacting with underlying navigation."""
    panel = page.get_by_test_id(panel_testid)
    if panel.count() == 0 or not panel.is_visible():
        return
    close_btn = panel.locator("button").filter(has_text=re.compile(r"^(close|×|✕|done)$", re.I))
    if close_btn.count() > 0 and close_btn.first.is_visible():
        close_btn.first.click()
    if panel.is_visible():
        page.keyboard.press("Escape")
    if panel.is_visible():
        panel.click(position={"x": 5, "y": 5})
    expect(panel).to_be_hidden()


def _wait_for_img_natural_width(img_locator: Locator) -> int:
    """Await browser image load/error completion and return naturalWidth."""
    width = img_locator.evaluate(
        """(img) => new Promise((resolve) => {
            if (img.complete && img.naturalWidth > 0) {
                resolve(img.naturalWidth);
                return;
            }
            const finish = () => resolve(img.naturalWidth || 0);
            img.addEventListener('load', finish, { once: true });
            img.addEventListener('error', finish, { once: true });
            if (img.complete) {
                resolve(img.naturalWidth || 0);
            }
        })"""
    )
    return int(width)


class TestWebUIEndToEndAndCLIPariy:
    """Full spec-anchored Playwright E2E suite covering every UI capability and CLI parity."""

    def test_features_view_and_issue_creation_validation_rules(
        self, ui_env: LiveUIEnv, ui_png_file: Path
    ) -> None:
        page = ui_env.page

        # 1. Supported Features / Help overview in UI (auto-wait for async /api/features load)
        expect(page.get_by_test_id("app-header")).to_be_visible()
        expect(page.get_by_test_id("issue-list-empty")).to_be_visible()

        page.get_by_test_id("nav-features").click()
        expect(page.get_by_test_id("features-view")).to_be_visible()
        expect(page.get_by_test_id("features-list")).to_contain_text(re.compile(r"create", re.I))
        expect(page.get_by_test_id("features-list")).to_contain_text(re.compile(r"comment", re.I))
        expect(page.get_by_test_id("features-list")).to_contain_text(re.compile(r"export", re.I))
        page.get_by_test_id("btn-close-features").click()
        expect(page.get_by_test_id("features-view")).to_be_hidden()

        # 2. Open Create Issue modal and verify mandatory validation rules
        page.get_by_test_id("btn-new-issue").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_visible()

        # 2a. Reject missing / empty summary
        _set_rich_html_via_source(page, "create-desc", "<p>Valid description</p>")
        page.get_by_test_id("create-summary-input").fill("")
        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-validation-error")).to_be_visible()

        # 2b. Reject whitespace-only summary
        page.get_by_test_id("create-summary-input").fill("     ")
        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-validation-error")).to_be_visible()

        # 2c. Reject multi-line summary (via paste/programmatic newline & live server validation)
        page.get_by_test_id("create-summary-input").evaluate(
            """(el) => {
                const dt = new DataTransfer();
                dt.setData('text/plain', 'First line\\nSecond line');
                const evt = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                el.dispatchEvent(evt);
                if (!evt.defaultPrevented) {
                    el.value = 'First line\\nSecond line';
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }"""
        )
        val_after_paste = page.get_by_test_id("create-summary-input").input_value()
        if "\n" in val_after_paste:
            page.get_by_test_id("btn-create-submit").click()
            expect(page.get_by_test_id("create-validation-error")).to_be_visible()
        multiline_status = page.evaluate(
            """async () => {
                const r = await fetch('/api/issues', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ summary: 'Line 1\\nLine 2', description: '<p>Valid</p>' })
                });
                return r.status;
            }"""
        )
        assert multiline_status == 400
        assert ui_env.cli.run_json("list")["total"] == 0

        # 2d. Reject empty / whitespace / empty-HTML-tag description (<p> </p>)
        page.get_by_test_id("create-summary-input").fill("Valid single-line summary")
        _set_rich_html_via_source(page, "create-desc", "<p>   </p>")
        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-validation-error")).to_be_visible()
        assert ui_env.cli.run_json("list")["total"] == 0

        # 2e. Accept image-only rich-text description (uploaded local image) -> auto-generates #1
        _set_rich_html_via_source(page, "create-desc", "")
        if page.get_by_test_id("create-desc-html-input").is_visible():
            page.get_by_test_id("create-desc-btn-source").click()
        img_popover_input = page.get_by_test_id("create-desc-image-file")
        if not img_popover_input.is_visible():
            page.get_by_test_id("create-desc-btn-image").click()
        if page.get_by_test_id("create-desc-image-alt").is_visible():
            page.get_by_test_id("create-desc-image-alt").fill("Image Only Description")
        img_popover_input.set_input_files(str(ui_png_file))
        expect(page.get_by_test_id("create-desc-editor").locator("img")).to_have_count(1)

        page.get_by_test_id("create-summary-input").fill("Image-only description issue")
        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_hidden()

        cli_i1 = ui_env.cli.run_json("show", "1")
        assert cli_i1["number"] == 1
        assert cli_i1["summary"] == "Image-only description issue"
        assert len(cli_i1["description_images"]) == 1

        # 2f. Create #2 with full attributes, relationship to #1, and toolbar formatting
        if page.get_by_test_id("issue-detail-view").is_visible():
            page.get_by_test_id("btn-back-to-list").click()
        page.get_by_test_id("btn-new-issue").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_visible()

        page.get_by_test_id("create-summary-input").fill("Second sequential issue")
        page.get_by_test_id("create-status-select").select_option("in_progress")
        page.get_by_test_id("create-priority-select").select_option("critical")
        page.get_by_test_id("create-type-select").select_option("bug")
        page.get_by_test_id("create-labels-input").fill("auth, urgent")
        page.get_by_test_id("create-assignee-input").fill("alice")
        page.get_by_test_id("create-reporter-input").fill("bob")
        page.get_by_test_id("create-milestone-input").fill("v1.0")
        page.get_by_test_id("create-due-date-input").fill("2026-12-25")
        page.get_by_test_id("create-rel-type-select").select_option("blocks")
        page.get_by_test_id("create-rel-target-input").fill("1")

        editor = page.get_by_test_id("create-desc-editor")
        editor.click()
        page.keyboard.type("Toolbar text ")
        page.keyboard.press("Control+A")
        page.get_by_test_id("create-desc-btn-bold").click()
        page.get_by_test_id("create-desc-btn-underline").click()

        page.get_by_test_id("create-desc-btn-link").click()
        page.get_by_test_id("create-desc-link-text").fill("Spec Link")
        page.get_by_test_id("create-desc-link-url").fill("https://example.com/spec")
        page.get_by_test_id("create-desc-link-insert").click()

        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_hidden()

        cli_i2 = ui_env.cli.run_json("show", "2")
        assert cli_i2["number"] == 2
        assert cli_i2["display_id"] == "#2"
        assert cli_i2["status"] == "in_progress"
        assert cli_i2["priority"] == "critical"
        assert cli_i2["issue_type"] == "bug"
        assert set(cli_i2["labels"]) == {"auth", "urgent"}
        assert cli_i2["assignee"] == "alice"
        assert cli_i2["reporter"] == "bob"
        assert cli_i2["milestone"] == "v1.0"
        assert cli_i2["due_date"] == "2026-12-25"
        assert 'href="https://example.com/spec"' in cli_i2["description"]
        assert any(r["relation_type"] == "blocks" and r["target_number"] == 1 for r in cli_i2["relationships"])

    def test_ui_pagination_filtering_search_and_sorting(self, ui_env: LiveUIEnv) -> None:
        page = ui_env.page

        seed_specs = [
            ("Issue 01 Alpha", "<p>Cache layer timeout</p>", "open", "critical", "bug", "backend,cache", "alice", "dan", "M1"),
            ("Issue 02 Beta", "<p>Login button focus ring</p>", "in_progress", "low", "improvement", "ui", "bob", "dan", "M1"),
            ("Issue 03 Gamma", "<p>Webhook dead letter retry</p>", "blocked", "high", "feature", "backend", "alice", "erin", "M2"),
            ("Issue 04 Delta", "<p>CSV export header order</p>", "resolved", "medium", "task", "export", "carol", "erin", "M2"),
            ("Issue 05 Epsilon", "<p>Session cookie samesite</p>", "closed", "high", "bug", "security", "alice", "dan", "M1"),
            ("Issue 06 Zeta", "<p>Pagination keyboard shortcut</p>", "open", "low", "feature", "ui", "bob", "erin", "M3"),
            ("Issue 07 Eta", "<p>SQLite WAL checkpoint tune</p>", "in_progress", "medium", "task", "db", "carol", "dan", "M3"),
            ("Issue 08 Theta", "<p>Markdown fenced code parser</p>", "open", "high", "bug", "richtext", "alice", "erin", "M2"),
            ("Issue 09 Iota", "<p>Bulk toolbar badge count</p>", "resolved", "low", "improvement", "ui", "bob", "dan", "M3"),
            ("Issue 10 Kappa", "<p>Asset sha256 dedup check</p>", "open", "medium", "task", "storage", "carol", "erin", "M1"),
            ("Issue 11 Lambda", "<p>Audit trail diff view</p>", "blocked", "critical", "feature", "audit", "alice", "dan", "M2"),
            ("Issue 12 Mu", "<p>CLI json error exit codes</p>", "closed", "medium", "task", "cli", "bob", "erin", "M3"),
        ]
        for summary, desc, st, prio, tp, lbls, asg, rep, ms in seed_specs:
            ui_env.cli.run_json(
                "create",
                "--summary",
                summary,
                "--description",
                desc,
                "--status",
                st,
                "--priority",
                prio,
                "--type",
                tp,
                "--labels",
                lbls,
                "--assignee",
                asg,
                "--reporter",
                rep,
                "--milestone",
                ms,
            )
        ui_env.cli.run_json("comment", "add", "7", "--body", "<p>Comment needle <strong>zircon_beacon_77</strong></p>")

        page.reload(wait_until="networkidle")

        page.get_by_test_id("pagination-per-page").select_option("5")
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("12")
        expect(page.get_by_test_id("pagination-info")).to_contain_text("1")
        expect(page.get_by_test_id("pagination-info")).to_contain_text("3")
        expect(page.get_by_test_id("pagination-first")).to_be_disabled()
        expect(page.get_by_test_id("pagination-prev")).to_be_disabled()
        expect(page.get_by_test_id("pagination-next")).to_be_enabled()
        expect(page.get_by_test_id("pagination-last")).to_be_enabled()

        page.get_by_test_id("sort-by-select").select_option("number")
        page.get_by_test_id("sort-order-select").select_option("asc")
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()
        expect(page.get_by_test_id("issue-number-1")).to_contain_text("#1")
        expect(page.get_by_test_id("issue-summary-1")).to_contain_text("Issue 01 Alpha")
        expect(page.get_by_test_id("issue-status-1")).to_be_visible()
        expect(page.get_by_test_id("issue-priority-1")).to_be_visible()
        expect(page.get_by_test_id("issue-type-1")).to_be_visible()
        expect(page.get_by_test_id("issue-labels-1")).to_contain_text("backend")
        expect(page.get_by_test_id("issue-assignee-1")).to_contain_text("alice")

        page.get_by_test_id("pagination-next").click()
        expect(page.get_by_test_id("issue-row-6")).to_be_visible()
        expect(page.get_by_test_id("issue-comments-count-7")).to_contain_text("1")

        page.get_by_test_id("pagination-last").click()
        expect(page.get_by_test_id("issue-row-11")).to_be_visible()
        expect(page.get_by_test_id("issue-row-12")).to_be_visible()
        expect(page.get_by_test_id("pagination-next")).to_be_disabled()
        expect(page.get_by_test_id("pagination-last")).to_be_disabled()

        page.get_by_test_id("pagination-prev").click()
        expect(page.get_by_test_id("issue-row-6")).to_be_visible()

        page.get_by_test_id("pagination-first").click()
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()

        page.get_by_test_id("filter-status").select_option("blocked")
        page.get_by_test_id("btn-apply-filters").click()
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("2")
        expect(page.get_by_test_id("issue-row-3")).to_be_visible()
        expect(page.get_by_test_id("issue-row-11")).to_be_visible()

        page.get_by_test_id("btn-reset-filters").click()
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("12")

        page.get_by_test_id("filter-priority").select_option("critical")
        page.get_by_test_id("filter-type").select_option("bug")
        page.get_by_test_id("btn-apply-filters").click()
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("1")
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()

        page.get_by_test_id("btn-reset-filters").click()
        page.get_by_test_id("filter-label").fill("ui")
        page.get_by_test_id("filter-assignee").fill("bob")
        page.get_by_test_id("filter-reporter").fill("dan")
        page.get_by_test_id("filter-milestone").fill("M1")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        page.get_by_test_id("filter-created-from").fill(today)
        page.get_by_test_id("filter-created-to").fill(today)
        page.get_by_test_id("btn-apply-filters").click()
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("1")
        expect(page.get_by_test_id("issue-row-2")).to_be_visible()

        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        page.get_by_test_id("filter-created-from").fill(future)
        page.get_by_test_id("btn-apply-filters").click()
        expect(page.get_by_test_id("issue-list-empty")).to_be_visible()

        page.get_by_test_id("btn-reset-filters").click()
        page.get_by_test_id("search-input").fill("zircon_beacon_77")
        page.get_by_test_id("btn-search-submit").click()
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("1")
        expect(page.get_by_test_id("issue-row-7")).to_be_visible()

        page.get_by_test_id("btn-search-clear").click()
        expect(page.get_by_test_id("pagination-total-count")).to_contain_text("12")

        page.get_by_test_id("sort-by-select").select_option("number")
        page.get_by_test_id("sort-order-select").select_option("desc")
        first_row = page.get_by_test_id("issue-list-table").locator("tbody tr").first
        expect(first_row).to_contain_text("#12")

    def test_issue_detail_not_found_editing_workflow_relationships_lifecycle_and_bulk(
        self, ui_env: LiveUIEnv
    ) -> None:
        page = ui_env.page
        ui_env.cli.run_json("create", "--summary", "Primary issue", "--description", "<p>Primary body</p>", "--label", "init")
        ui_env.cli.run_json("create", "--summary", "Secondary issue", "--description", "<p>Secondary body</p>")
        ui_env.cli.run_json("create", "--summary", "Tertiary issue", "--description", "<p>Tertiary body</p>")
        page.reload(wait_until="networkidle")

        # 1. Non-existent issue lookup -> explicit error feedback
        page.get_by_test_id("issue-jump-input").fill("999")
        page.get_by_test_id("btn-issue-jump").click()
        expect(page.get_by_test_id("issue-not-found-error")).to_be_visible()

        # 2. Valid issue selection
        page.get_by_test_id("issue-summary-1").click()
        expect(page.get_by_test_id("issue-detail-view")).to_be_visible()
        expect(page.get_by_test_id("detail-number")).to_contain_text("#1")
        expect(page.get_by_test_id("detail-summary")).to_contain_text("Primary issue")

        # 3. Inline edit validation & save
        page.get_by_test_id("btn-edit-issue").click()
        expect(page.get_by_test_id("edit-issue-form")).to_be_visible()

        page.get_by_test_id("edit-summary-input").fill("   ")
        page.get_by_test_id("btn-edit-save").click()
        expect(page.get_by_test_id("edit-validation-error")).to_be_visible()

        page.get_by_test_id("edit-summary-input").fill("Primary issue edited")
        _set_rich_html_via_source(page, "edit-desc", "<p>   </p>")
        page.get_by_test_id("btn-edit-save").click()
        expect(page.get_by_test_id("edit-validation-error")).to_be_visible()

        _set_rich_html_via_source(page, "edit-desc", "<p>Edited <u>underlined</u> description</p>")
        page.get_by_test_id("edit-priority-select").select_option("high")
        page.get_by_test_id("edit-type-select").select_option("feature")
        page.get_by_test_id("edit-assignee-input").fill("dana")
        page.get_by_test_id("edit-milestone-input").fill("v2.1")
        page.get_by_test_id("edit-due-date-input").fill("2026-12-30")
        page.get_by_test_id("btn-edit-save").click()

        expect(page.get_by_test_id("detail-summary")).to_contain_text("Primary issue edited")
        assert "<u>underlined</u>" in page.get_by_test_id("detail-description").inner_html()
        expect(page.get_by_test_id("detail-priority")).to_contain_text("high")
        expect(page.get_by_test_id("detail-assignee")).to_contain_text("dana")

        # Quick add/remove label on issue detail + Labels Manager create, rename, and delete
        page.get_by_test_id("detail-add-label-input").fill("quicktag")
        page.get_by_test_id("btn-add-issue-label").click()
        expect(page.get_by_test_id("detail-label-chip-quicktag")).to_be_visible()
        page.get_by_test_id("btn-remove-issue-label-init").click()
        expect(page.get_by_test_id("detail-label-chip-init")).to_have_count(0)

        page.get_by_test_id("nav-labels").click()
        expect(page.get_by_test_id("labels-manager")).to_be_visible()
        page.get_by_test_id("label-create-name").fill("temp-label")
        page.get_by_test_id("btn-label-create").click()
        expect(page.get_by_test_id("label-item-temp-label")).to_be_visible()
        page.get_by_test_id("btn-label-delete-temp-label").click()
        _confirm_if_modal_open(page)
        expect(page.get_by_test_id("label-item-temp-label")).to_have_count(0)

        page.get_by_test_id("label-rename-input-quicktag").fill("renamed-ui-tag")
        page.get_by_test_id("btn-label-rename-quicktag").click()
        expect(page.get_by_test_id("label-item-renamed-ui-tag")).to_be_visible()
        _close_overlay_panel(page, "labels-manager")

        if not page.get_by_test_id("issue-detail-view").is_visible():
            page.get_by_test_id("nav-issues").click()
            page.get_by_test_id("issue-summary-1").click()
        else:
            page.get_by_test_id("nav-issues").click()
            page.get_by_test_id("issue-summary-1").click()
        expect(page.get_by_test_id("detail-label-chip-renamed-ui-tag")).to_be_visible()

        # 4. Status transitions, close & reopen
        page.get_by_test_id("detail-status-select").select_option("blocked")
        expect(page.get_by_test_id("detail-status")).to_contain_text("blocked")

        page.get_by_test_id("btn-close-issue").click()
        expect(page.get_by_test_id("detail-status")).to_contain_text("closed")

        page.get_by_test_id("btn-reopen-issue").click()
        expect(page.get_by_test_id("detail-status")).to_contain_text("open")

        # 5. Relationships add & remove
        page.get_by_test_id("relationship-type-select").select_option("duplicates")
        page.get_by_test_id("relationship-target-input").fill("2")
        page.get_by_test_id("btn-add-relationship").click()
        expect(page.get_by_test_id("relationships-list")).to_contain_text("#2")

        rel_id = ui_env.cli.run_json("show", "1")["relationships"][0]["id"]
        page.get_by_test_id(f"btn-remove-relationship-{rel_id}").click()
        expect(page.get_by_test_id(f"relationship-item-{rel_id}")).to_have_count(0)

        # 6. Clone -> creates #4, Archive & Restore, Confirmed Delete
        page.get_by_test_id("btn-clone-issue").click()
        expect(page.get_by_test_id("detail-number")).to_contain_text("#4")

        page.get_by_test_id("btn-archive-issue").click()
        expect(page.get_by_test_id("detail-archived-badge")).to_be_visible()

        page.get_by_test_id("btn-back-to-list").click()
        expect(page.get_by_test_id("issue-row-4")).to_have_count(0)

        page.get_by_test_id("filter-archived").select_option("only")
        page.get_by_test_id("btn-apply-filters").click()
        expect(page.get_by_test_id("issue-row-4")).to_be_visible()

        page.get_by_test_id("issue-summary-4").click()
        page.get_by_test_id("btn-restore-issue").click()
        expect(page.get_by_test_id("detail-archived-badge")).to_be_hidden()

        # Delete confirmation cancel then confirm
        page.get_by_test_id("btn-delete-issue").click()
        expect(page.get_by_test_id("confirm-delete-modal")).to_be_visible()
        page.get_by_test_id("btn-cancel-delete").click()
        expect(page.get_by_test_id("detail-number")).to_contain_text("#4")

        page.get_by_test_id("btn-delete-issue").click()
        page.get_by_test_id("btn-confirm-delete").click()
        expect(page.get_by_test_id("confirm-delete-modal")).to_be_hidden()
        expect(page.get_by_test_id("issue-list-table")).to_be_visible()
        assert ui_env.cli.run("show", "4", "--json").returncode == 2

        # 7. Bulk update & bulk delete from list view
        page.get_by_test_id("btn-reset-filters").click()
        page.get_by_test_id("issue-checkbox-2").check()
        page.get_by_test_id("issue-checkbox-3").check()
        expect(page.get_by_test_id("bulk-actions-bar")).to_be_visible()
        expect(page.get_by_test_id("bulk-selected-count")).to_contain_text("2")

        page.get_by_test_id("bulk-status-select").select_option("resolved")
        page.get_by_test_id("bulk-priority-select").select_option("critical")
        page.get_by_test_id("bulk-assignee-input").fill("bulk_owner")
        page.get_by_test_id("bulk-add-labels-input").fill("bulk-tag")
        page.get_by_test_id("btn-bulk-apply").click()

        expect(page.get_by_test_id("issue-status-2")).to_contain_text("resolved")
        expect(page.get_by_test_id("issue-priority-3")).to_contain_text("critical")
        assert ui_env.cli.run_json("show", "2")["assignee"] == "bulk_owner"

        page.get_by_test_id("issue-checkbox-2").check()
        page.get_by_test_id("issue-checkbox-3").check()
        page.get_by_test_id("btn-bulk-delete").click()
        expect(page.get_by_test_id("confirm-delete-modal")).to_be_visible()
        page.get_by_test_id("btn-confirm-delete").click()
        expect(page.get_by_test_id("issue-row-2")).to_have_count(0)
        expect(page.get_by_test_id("issue-row-3")).to_have_count(0)

    def test_rich_text_comments_add_render_edit_delete_and_audit_in_ui(
        self, ui_env: LiveUIEnv, ui_png_file: Path, png_bytes: bytes
    ) -> None:
        page = ui_env.page
        ui_env.cli.run_json("create", "--summary", "Comment showcase issue", "--description", "<p>Host issue</p>")
        page.reload(wait_until="networkidle")
        page.get_by_test_id("issue-summary-1").click()

        # 1. Reject empty / empty-tag comment submission
        _set_rich_html_via_source(page, "new-comment", "<p>   </p>")
        page.get_by_test_id("btn-add-comment").click()
        expect(page.get_by_test_id("comment-validation-error")).to_be_visible()

        # 2. Compose rich-text comment with upload image, data URI image, URL image, links, and all formatting
        if page.get_by_test_id("new-comment-html-input").is_visible():
            page.get_by_test_id("new-comment-btn-source").click()
        page.get_by_test_id("new-comment-editor").fill("")

        file_input = page.get_by_test_id("new-comment-image-file")
        if not file_input.is_visible():
            page.get_by_test_id("new-comment-btn-image").click()
        if page.get_by_test_id("new-comment-image-alt").is_visible():
            page.get_by_test_id("new-comment-image-alt").fill("Uploaded Comment Diagram")
        file_input.set_input_files(str(ui_png_file))
        expect(page.get_by_test_id("new-comment-editor").locator("img")).to_have_count(1)
        uploaded_src = page.get_by_test_id("new-comment-editor").locator("img").first.get_attribute("src")
        assert uploaded_src and uploaded_src.startswith("/api/assets/")

        b64_uri = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
        full_rich_comment = (
            "<h2>Analysis Heading</h2>"
            "<p><strong>Bold text</strong>, <em>italic text</em>, <u>underlined text</u>, "
            "<s>strikethrough text</s>, and <code>inline_fn()</code>.</p>"
            "<pre><code>SELECT * FROM issues WHERE id = 1;</code></pre>"
            "<blockquote>Quoted engineering note</blockquote>"
            "<ul><li>Unordered point</li></ul>"
            "<ol><li>Ordered step</li></ol>"
            '<p><a href="https://example.com/ticket">External Ticket</a> and '
            '<a href="mailto:eng@example.com">Email Eng</a></p>'
            f'<p><img src="{uploaded_src}" alt="Uploaded Comment Diagram" />'
            f'<img src="{b64_uri}" alt="Inline Base64 Icon" />'
            '<img src="https://example.com/remote-chart.png" alt="Remote Chart" /></p>'
        )
        page.get_by_test_id("comment-author-input").fill("qa_engineer")
        _set_rich_html_via_source(page, "new-comment", full_rich_comment)
        page.get_by_test_id("btn-add-comment").click()

        expect(page.get_by_test_id("comment-item-1")).to_be_visible()
        expect(page.get_by_test_id("comment-author-1")).to_contain_text("qa_engineer")
        body_loc = page.get_by_test_id("comment-body-1")
        body_html = body_loc.inner_html()

        assert ("<strong>Bold text</strong>" in body_html) or ("<b>Bold text</b>" in body_html)
        assert ("<em>italic text</em>" in body_html) or ("<i>italic text</i>" in body_html)
        assert "<u>underlined text</u>" in body_html
        assert ("<s>strikethrough text</s>" in body_html) or ("<del>strikethrough text</del>" in body_html)
        assert "<code>inline_fn()</code>" in body_html
        assert "<pre>" in body_html
        assert "<blockquote>" in body_html
        assert "<ul>" in body_html and "<ol>" in body_html
        assert 'href="https://example.com/ticket"' in body_html
        assert 'href="mailto:eng@example.com"' in body_html

        expect(body_loc.locator("img")).to_have_count(3)
        assert _wait_for_img_natural_width(body_loc.locator(f'img[src="{uploaded_src}"]')) > 0

        # 3. Edit existing comment by ID -> reject empty edit, then persist rich-text update & edited indicator
        page.get_by_test_id("btn-edit-comment-1").click()
        _set_rich_html_via_source(page, "edit-comment-1", "<p>   </p>")
        page.get_by_test_id("btn-save-comment-1").click()
        expect(page.get_by_test_id("edit-comment-error-1")).to_be_visible()

        _set_rich_html_via_source(
            page,
            "edit-comment-1",
            '<p>Revised with <strong>updated bold</strong>, <u>fresh underline</u>, '
            '<a href="https://example.com/revised">Revised Link</a>, and '
            f'<img src="{uploaded_src}" alt="Kept Diagram" /></p>',
        )
        page.get_by_test_id("btn-save-comment-1").click()

        expect(page.get_by_test_id("comment-edited-indicator-1")).to_be_visible()
        updated_html = page.get_by_test_id("comment-body-1").inner_html()
        assert "<u>fresh underline</u>" in updated_html
        assert 'href="https://example.com/revised"' in updated_html

        # 4. Delete comment by ID & verify activity history records full audit trail
        page.get_by_test_id("btn-delete-comment-1").click()
        _confirm_if_modal_open(page)
        expect(page.get_by_test_id("comment-item-1")).to_have_count(0)

        expect(page.get_by_test_id("activity-history-list")).to_contain_text(re.compile(r"comment", re.I))

    def test_xss_sanitization_neutralizes_scripts_handlers_and_js_urls_in_ui(
        self, ui_env: LiveUIEnv
    ) -> None:
        page = ui_env.page

        # Create issue via UI with XSS vectors in description -> must NEVER execute script/onerror in composer or detail view
        page.get_by_test_id("btn-new-issue").click()
        page.get_by_test_id("create-summary-input").fill("XSS hardening verification")
        xss_desc = (
            "<p>Preserved <strong>safe formatting</strong></p>"
            "<script>window.__xss_executed = true;</script>"
            '<img src="invalid-src" onerror="window.__xss_executed = true" />'
            '<a href="javascript:window.__xss_executed = true">SafeAnchorText</a>'
        )
        _set_rich_html_via_source(page, "create-desc", xss_desc)
        page.get_by_test_id("btn-create-submit").click()
        expect(page.get_by_test_id("create-issue-modal")).to_be_hidden()

        if not page.get_by_test_id("issue-detail-view").is_visible():
            page.get_by_test_id("issue-summary-1").click()

        desc_html = page.get_by_test_id("detail-description").inner_html()
        assert "<strong>safe formatting</strong>" in desc_html or "<b>safe formatting</b>" in desc_html
        assert "<script" not in desc_html.lower()
        assert "onerror" not in desc_html.lower()
        assert "javascript:" not in desc_html.lower()

        # Also add a comment containing XSS payloads in UI
        _set_rich_html_via_source(
            page,
            "new-comment",
            "<p><u>Safe comment</u></p>"
            "<script>window.__xss_executed = true;</script>"
            '<img src="bad" onerror="window.__xss_executed = true" />'
            '<a href="javascript:window.__xss_executed = true">CommentLink</a>',
        )
        page.get_by_test_id("btn-add-comment").click()
        expect(page.get_by_test_id("comment-item-1")).to_be_visible()

        comment_html = page.get_by_test_id("comment-body-1").inner_html()
        assert "<u>Safe comment</u>" in comment_html
        assert "<script" not in comment_html.lower()
        assert "onerror" not in comment_html.lower()
        assert "javascript:" not in comment_html.lower()

        # Neither editor synchronization nor rendered view may execute injected scripts/event handlers
        assert page.evaluate("() => Boolean(window.__xss_executed)") is False

    def test_export_import_ui_and_live_bidirectional_cli_ui_parity(
        self, tmp_path: Path, ui_env: LiveUIEnv, ui_png_file: Path
    ) -> None:
        page = ui_env.page

        # 1. CLI mutations -> immediately reflected in live Web UI (including CLI uploaded image asset)
        ui_env.cli.run_json(
            "create",
            "--summary",
            "CLI to UI live sync issue",
            "--description",
            "<p>CLI authored <u>underlined</u> spec</p>",
            "--image",
            str(ui_png_file),
            "--image-alt",
            "CLI Shared Asset",
            "--priority",
            "high",
            "--label",
            "parity",
            "--assignee",
            "cli_agent",
        )
        cli_cid = ui_env.cli.run_json(
            "comment",
            "add",
            "1",
            "--body",
            '<p>CLI rich comment with <strong>bold</strong> and <a href="https://example.com/cli-ref">CLI Ref</a></p>',
            "--author",
            "cli_agent",
        )["id"]

        page.reload(wait_until="networkidle")
        expect(page.get_by_test_id("issue-row-1")).to_be_visible()
        expect(page.get_by_test_id("issue-summary-1")).to_contain_text("CLI to UI live sync issue")

        page.get_by_test_id("issue-summary-1").click()
        expect(page.get_by_test_id("detail-description").locator("img")).to_have_count(1)
        assert _wait_for_img_natural_width(page.get_by_test_id("detail-description").locator("img").first) > 0
        expect(page.get_by_test_id(f"comment-body-{cli_cid}")).to_contain_text("CLI rich comment")

        # 2. UI mutations (edit CLI comment, close issue) -> immediately reflected in CLI
        page.get_by_test_id(f"btn-edit-comment-{cli_cid}").click()
        _set_rich_html_via_source(
            page,
            f"edit-comment-{cli_cid}",
            "<p>UI edited CLI comment with <u>UI underline</u></p>",
        )
        page.get_by_test_id(f"btn-save-comment-{cli_cid}").click()
        expect(page.get_by_test_id(f"comment-edited-indicator-{cli_cid}")).to_be_visible()

        page.get_by_test_id("btn-close-issue").click()
        expect(page.get_by_test_id("detail-status")).to_contain_text("closed")

        cli_synced_1 = ui_env.cli.run_json("show", "1")
        assert cli_synced_1["status"] == "closed"
        assert cli_synced_1["closed_at"] is not None
        assert cli_synced_1["comments"][0]["is_edited"] is True
        assert "<u>UI underline</u>" in cli_synced_1["comments"][0]["body"]

        # 3. UI Export (JSON, CSV, Markdown) & JSON Import
        page.get_by_test_id("nav-export-import").click()
        expect(page.get_by_test_id("export-import-panel")).to_be_visible()

        preview = page.get_by_test_id("export-preview-output")
        page.get_by_test_id("export-scope-select").select_option("all")
        page.get_by_test_id("export-format-select").select_option("json")
        page.get_by_test_id("btn-export-submit").click()
        expect(preview).to_have_value(re.compile(r"\{", re.S))
        parsed_export = json.loads(preview.input_value())
        assert parsed_export is not None

        page.get_by_test_id("export-format-select").select_option("csv")
        page.get_by_test_id("btn-export-submit").click()
        expect(preview).to_have_value(re.compile(r"^(?!\s*\{).+", re.S))
        assert len(list(csv.DictReader(io.StringIO(preview.input_value())))) == 1

        page.get_by_test_id("export-format-select").select_option("markdown")
        page.get_by_test_id("btn-export-submit").click()
        expect(preview).to_have_value(re.compile(r"^#", re.S))
        assert "CLI to UI live sync issue" in preview.input_value()

        # Import new issue via UI JSON import textarea -> verify CLI immediately sees #2
        import_payload = json.dumps(
            {
                "issues": [
                    {
                        "summary": "Imported via UI panel",
                        "description": "<p>Imported <strong>rich</strong> body</p>",
                        "status": "open",
                        "priority": "medium",
                        "issue_type": "feature",
                        "labels": ["imported"],
                    }
                ]
            }
        )
        page.get_by_test_id("import-json-textarea").fill(import_payload)
        page.get_by_test_id("btn-import-submit").click()
        expect(page.get_by_test_id("import-result-message")).to_be_visible()
        expect(page.get_by_test_id("import-result-message")).to_contain_text("1")

        all_after_import = ui_env.cli.run_json("list", "--sort-by", "number", "--sort-order", "asc")
        assert all_after_import["total"] == 2
        assert all_after_import["items"][1]["summary"] == "Imported via UI panel"
