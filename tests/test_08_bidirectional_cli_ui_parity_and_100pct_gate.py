"""Suite 08: Bidirectional CLI <-> UI Parity & Automated 100% Surface / Endpoint Gate."""

from __future__ import annotations

import ast
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import expect

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from helpers import (  # noqa: E402
    ITRACK_PY,
    UI_MODALS,
    UI_VIEWS,
    ITrackWorkspace,
    _extract_js_api_paths,
    cleanup_repo_daemon,
    compute_surface_audit,
    init_git_repo,
    introspect_api_routes,
    introspect_cli_surface,
    introspect_spa_surface,
    load_itrack_cli_module,
    load_itrack_server_module,
    make_png,
    make_text_file,
    normalize_route_shape,
    run_itrack_cli,
    sha256_file,
)


def _run_parallel_tasks(tasks: dict[str, Callable[[], Any]], max_workers: int = 8) -> dict[str, Any]:
    """Run independent CLI subprocess callables concurrently against an already-started workspace daemon."""
    results: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in concurrent.futures.as_completed(future_map):
            name = future_map[fut]
            results[name] = fut.result()
    return results


def _extract_cli_api_shapes(cli_source: str) -> set[str]:
    """Extract normalized `/api/v1/*` route shapes from `itrack.py` string & f-string AST literals."""
    shapes: set[str] = set()
    tree = ast.parse(cli_source)

    for node in ast.walk(tree):
        candidate: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "/api/v1/" in node.value:
                candidate = node.value
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for val in node.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    parts.append(val.value)
                else:
                    parts.append("{param}")
            joined = "".join(parts)
            if "/api/v1/" in joined:
                candidate = joined

        if candidate:
            for match in re.finditer(r"/api/v1/[A-Za-z0-9_.\-/{}?&=]+", candidate):
                raw_path = match.group(0).split("?", 1)[0].rstrip("/")
                if raw_path and raw_path != "/api/v1":
                    shapes.add(normalize_route_shape(raw_path))

    js_eps, _ = _extract_js_api_paths(cli_source)
    for ep in js_eps:
        shapes.add(normalize_route_shape(ep))

    return shapes


def _dispatch_drop_file(page: Any, selector: str, file_path: Path, mime_type: str) -> None:
    """Dispatch a real HTML5 DragEvent('drop') carrying exact file bytes onto selector."""
    payload = list(file_path.read_bytes())
    page.evaluate(
        """({ selector, fileName, mimeType, byteArray }) => {
            const target = document.querySelector(selector);
            if (!target) throw new Error(`Drop target not found: ${selector}`);
            const dt = new DataTransfer();
            const file = new File([new Uint8Array(byteArray)], fileName, { type: mimeType });
            dt.items.add(file);
            target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
            target.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
        }""",
        {
            "selector": selector,
            "fileName": file_path.name,
            "mimeType": mime_type,
            "byteArray": payload,
        },
    )


def _dispatch_paste_image(page: Any, selector: str, file_path: Path, file_name: str = "image.png") -> None:
    """Dispatch a synthetic ClipboardEvent('paste') carrying an image/png File onto selector."""
    payload = list(file_path.read_bytes())
    page.evaluate(
        """({ selector, fileName, byteArray }) => {
            const target = document.querySelector(selector);
            if (!target) throw new Error(`Paste target not found: ${selector}`);
            target.focus();
            const dt = new DataTransfer();
            const file = new File([new Uint8Array(byteArray)], fileName, { type: 'image/png' });
            dt.items.add(file);
            const evt = new ClipboardEvent('paste', {
                bubbles: true,
                cancelable: true,
                clipboardData: dt,
            });
            if (!evt.clipboardData || evt.clipboardData.files.length === 0) {
                Object.defineProperty(evt, 'clipboardData', { value: dt });
            }
            target.dispatchEvent(evt);
        }""",
        {
            "selector": selector,
            "fileName": file_name,
            "byteArray": payload,
        },
    )


# ---------------------------------------------------------------------------
# Test 1: UI -> CLI Round-Trip
# ---------------------------------------------------------------------------


def test_01_ui_to_cli_round_trip(live_ui: Any, tmp_path: Path) -> None:
    page = live_ui.page
    ws = live_ui.ws

    desc_png_path, desc_png_sha = make_png(
        tmp_path / "webhook_flow.png", width=48, height=32, color=(37, 99, 235)
    )
    paste_png_path, paste_png_sha = make_png(
        tmp_path / "image.png", width=40, height=40, color=(16, 185, 129)
    )
    log_path, log_sha = make_text_file(
        tmp_path / "webhook_audit.log",
        text=(
            "2026-11-24T12:00:00Z INFO Verifying X-Hub-Signature-256\n"
            "2026-11-24T12:00:01Z WARN Replay timestamp skew=412s rejected\n"
        ),
    )

    # 1a. Navigate to Sprints, create SPR-1, and start it
    page.locator('button.nav-item[data-view="sprints"]').click()
    expect(page.locator("#view-sprints")).to_be_visible()

    page.locator("#sprint-create-name").fill("Sprint 1")
    page.locator("#sprint-create-goal").fill("Harden webhook verification")
    page.locator("#sprint-create-start").fill("2026-11-01")
    page.locator("#sprint-create-end").fill("2026-11-15")
    page.locator("#btn-create-sprint").click()

    sprint_card = page.locator('.sprint-card[data-sprint-id="SPR-1"]')
    expect(sprint_card).to_be_visible()
    sprint_card.locator(".btn-sprint-start").click()
    expect(sprint_card).to_contain_text("active")

    # 1b. Open Create Issue modal and populate every field + inline embed + drag-drop .log attachment
    issue_title = "Payment Webhook Signature Verification"
    page.locator("#btn-open-create").click()
    create_modal = page.locator('[data-modal="create-issue"]')
    expect(create_modal).to_be_visible()

    page.locator("#create-name").fill(issue_title)
    page.locator("#create-category").select_option("security")
    page.locator("#create-state").select_option("todo")
    page.locator("#create-priority").select_option("high")
    page.locator("#create-severity").select_option("sev2")
    page.locator("#create-assignee").fill("alice")
    page.locator("#create-points").fill("5")
    page.locator("#create-estimate").fill("120")
    page.locator("#create-due").fill("2026-12-31")
    page.locator("#create-sprint").select_option("SPR-1")
    page.locator("#create-tags").fill("security, backend")
    page.locator("#create-components").fill("webhooks, auth")
    page.locator("#create-custom-fields").fill("env=staging\nowner_team=payments")
    page.locator("#create-ac").fill("Verify HMAC SHA-256\nReject replay timestamps")
    page.locator("#create-description").fill(
        "## Webhook Verification\n\nValidate `X-Signature-256` using constant-time digest comparison."
    )

    page.set_input_files("#create-embed-input", str(desc_png_path))
    expect(page.locator("#create-embeds-queued")).to_contain_text("1 embed(s) queued")

    _dispatch_drop_file(page, "#create-attach-dropzone", log_path, "text/plain")
    expect(page.locator("#create-attach-queued")).to_contain_text("webhook_audit.log")

    page.locator("#btn-submit-create-issue").click()

    drawer = page.locator("#issue-drawer")
    expect(drawer).to_be_visible()
    expect(page.locator("#detail-key-badge")).to_have_text("ITR-1")
    expect(page.locator("#detail-attachments-list")).to_contain_text("webhook_audit.log")

    # 1c. In #issue-drawer comment composer: paste PNG, link existing .log attachment, post Markdown comment
    _dispatch_paste_image(page, "textarea#cmt-body-input", paste_png_path, file_name="image.png")
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Embed: pasted-image-1.png")

    page.locator("#btn-cmt-link-att").click()
    link_att_modal = page.locator('[data-modal="link-attachment"]')
    expect(link_att_modal).to_be_visible()
    link_checkbox = link_att_modal.locator("input.link-att-check").first
    expect(link_checkbox).to_be_visible()
    link_checkbox.check()
    page.locator("#btn-confirm-link-att").click()
    expect(link_att_modal).not_to_be_visible()
    expect(page.locator("#cmt-staged-bar")).to_contain_text("Linked:")

    existing_composer_md = page.locator("#cmt-body-input").input_value()
    page.locator("#cmt-body-input").fill(
        f"Confirmed **HMAC SHA-256** signature check & linked audit log.\n\n{existing_composer_md}".strip()
    )
    page.locator("#btn-submit-comment").click()

    cmt_card = page.locator('.comment-card[data-comment-id="CMT-1"]')
    expect(cmt_card).to_be_visible()
    expect(cmt_card).to_contain_text("Confirmed")
    expect(cmt_card).to_contain_text("webhook_audit.log")

    page.locator("#btn-close-drawer").click()
    expect(drawer).not_to_be_visible()

    # 1d. Navigate to Kanban Board and drag ITR-1 from todo -> in-progress
    page.locator('button.nav-item[data-view="board"]').click()
    expect(page.locator("#view-board")).to_be_visible()

    todo_card = page.locator('.kanban-column[data-state="todo"] .kanban-card[data-key="ITR-1"]')
    in_prog_col = page.locator('.kanban-column[data-state="in-progress"]')
    expect(todo_card).to_be_visible()
    todo_card.drag_to(in_prog_col)

    expect(
        page.locator('.kanban-column[data-state="in-progress"] .kanban-card[data-key="ITR-1"]')
    ).to_be_visible()

    # 1e. Save Named View via modal
    page.locator("#btn-save-current-view").click()
    save_view_modal = page.locator('[data-modal="save-view"]')
    expect(save_view_modal).to_be_visible()

    page.locator("#save-view-name").fill("Active High Webhooks")
    page.locator("#save-view-query").fill("priority = high AND tag = security")
    page.locator("#save-view-tab").select_option("active")
    page.locator("#save-view-desc").fill("UI parity view")
    page.locator("#btn-submit-save-view").click()
    expect(save_view_modal).not_to_be_visible()

    # --- Verify 100% via itrack.py CLI ---
    issue_folder = ws.issues_dir / issue_title
    assert issue_folder.is_dir(), f"Expected exact spaced folder on disk: {issue_folder}"

    read_out = _run_parallel_tasks(
        {
            "by_name": lambda: ws.cli_json("issue", "show", issue_title),
            "by_key": lambda: ws.cli_json("issue", "show", "ITR-1"),
            "cmts_data": lambda: ws.cli_json("comment", "list", "ITR-1"),
            "all_atts": lambda: ws.cli_json("attach", "list", "ITR-1", "--scope", "all"),
            "board_data": lambda: ws.cli_json("board", "--swimlane", "sprint"),
            "spr_show": lambda: ws.cli_json("sprint", "show", "SPR-1"),
            "views_list": lambda: ws.cli_json("view", "list"),
            "view_run": lambda: ws.cli_json("view", "run", "Active High Webhooks"),
        }
    )

    by_name = read_out["by_name"]
    by_key = read_out["by_key"]
    assert by_name["key"] == "ITR-1"
    assert by_key["key"] == "ITR-1"
    assert by_name["name"] == issue_title

    for shown in (by_name, by_key):
        assert shown["category"] == "security"
        assert shown["state"] == "in-progress"
        assert shown["priority"] == "high"
        assert shown["severity"] == "sev2"
        assert shown["assignee"] == "alice"
        assert shown["sprint"] == "SPR-1"
        assert shown["story_points"] == 5.0
        assert shown["estimate_minutes"] == 120
        assert shown["due_date"] == "2026-12-31"
        assert set(shown["tags"]) == {"security", "backend"}
        assert set(shown["components"]) == {"webhooks", "auth"}
        assert shown["custom_fields"] == {"env": "staging", "owner_team": "payments"}
        assert [ac["text"] for ac in shown["acceptance_criteria"]] == [
            "Verify HMAC SHA-256",
            "Reject replay timestamps",
        ]
        assert len(shown["description_embeds"]) == 1
        assert len(shown["attachments"]) == 1
        assert shown["comment_count"] == 1

    desc_emb_id = by_key["description_embeds"][0]["id"]
    issue_log_id = by_key["attachments"][0]["id"]
    assert f"attachment://{desc_emb_id}" in by_key["description"]

    cmts_data = read_out["cmts_data"]
    assert len(cmts_data["comments"]) == 1
    cmt_cli = cmts_data["comments"][0]
    assert len(cmt_cli["embeds"]) == 1
    cmt_emb_id = cmt_cli["embeds"][0]["id"]
    assert f"attachment://{cmt_emb_id}" in cmt_cli["body"]
    assert cmt_cli["linked_attachment_ids"] == [issue_log_id]

    all_atts = read_out["all_atts"]["attachments"]
    assert len(all_atts) == 3
    log_entries = [a for a in all_atts if a["filename"] == "webhook_audit.log"]
    assert len(log_entries) == 1, f"Expected zero duplication of linked .log file, got {log_entries}"

    # Verify zero file duplication on disk inside comments/CMT-1/files
    cmt_files_on_disk = list((issue_folder / "comments" / "CMT-1" / "files").iterdir())
    assert len(cmt_files_on_disk) == 1
    assert cmt_files_on_disk[0].name.endswith(".png")
    disk_logs = list(issue_folder.rglob("*webhook_audit.log*"))
    assert len(disk_logs) == 1

    # Download all 3 files via CLI and assert exact SHA-256 byte equality
    dl_desc_png = tmp_path / "dl_desc.png"
    dl_cmt_png = tmp_path / "dl_cmt.png"
    dl_log = tmp_path / "dl_audit.log"
    _run_parallel_tasks(
        {
            "dl_desc": lambda: ws.cli_json("attach", "download", "ITR-1", desc_emb_id, "-o", str(dl_desc_png)),
            "dl_cmt": lambda: ws.cli_json("attach", "download", "ITR-1", cmt_emb_id, "-o", str(dl_cmt_png)),
            "dl_log": lambda: ws.cli_json("attach", "download", "ITR-1", issue_log_id, "-o", str(dl_log)),
        }
    )

    assert sha256_file(dl_desc_png) == desc_png_sha
    assert sha256_file(dl_cmt_png) == paste_png_sha
    assert sha256_file(dl_log) == log_sha

    # Board swimlane=sprint verification
    board_data = read_out["board_data"]
    spr_lanes = [ln for ln in board_data["lanes"] if ln["key"] == "SPR-1"]
    assert len(spr_lanes) == 1
    in_prog_cols = [c for c in spr_lanes[0]["columns"] if c["state"] == "in-progress"]
    assert len(in_prog_cols) == 1
    assert "ITR-1" in [i["key"] for i in in_prog_cols[0]["issues"]]

    # Sprint show verification
    spr_show = read_out["spr_show"]
    assert spr_show["state"] == "active"
    assert "ITR-1" in [i["key"] for i in spr_show["issues"]]

    # Saved View list & run verification
    views_list = read_out["views_list"]["views"]
    matched_views = [v for v in views_list if v["name"] == "Active High Webhooks"]
    assert len(matched_views) == 1
    assert matched_views[0]["query"] == "priority = high AND tag = security"
    assert matched_views[0]["tab"] == "active"
    assert matched_views[0]["description"] == "UI parity view"

    view_run = read_out["view_run"]
    assert [i["key"] for i in view_run["issues"]] == ["ITR-1"]


# ---------------------------------------------------------------------------
# Test 2: CLI -> UI Round-Trip
# ---------------------------------------------------------------------------


def test_02_cli_to_ui_round_trip(live_ui: Any, tmp_path: Path) -> None:
    page = live_ui.page
    ws = live_ui.ws

    init_desc_png, _ = make_png(tmp_path / "legacy_flow.png", width=36, height=24, color=(220, 38, 38))
    issue_att_txt, _ = make_text_file(
        tmp_path / "session_tokens.txt",
        text="legacy_cookie=sid_v1\nttl_seconds=3600\n",
    )
    png1, _ = make_png(tmp_path / "old_cmt_diagram.png", width=28, height=28, color=(245, 158, 11))
    txt1, _ = make_text_file(tmp_path / "draft_notes.txt", text="Initial token notes\n")
    png2, _ = make_png(tmp_path / "oauth2_rotation.png", width=44, height=30, color=(16, 185, 129))
    txt2, _ = make_text_file(tmp_path / "rotation_policy.txt", text="rotate_on_refresh=true\n")

    # 2a. Create colored tag, sprint, and two spaced-name issues via CLI
    setup_res = _run_parallel_tasks(
        {
            "tag": lambda: ws.cli_json("tag", "create", "urgent", "--color", "#ef4444"),
            "spr": lambda: ws.cli_json("sprint", "create", "Sprint 2"),
        }
    )
    assert setup_res["spr"]["id"] == "SPR-1"

    iss1 = ws.cli_json(
        "issue",
        "create",
        "Legacy Session Token Handler",
        "--category",
        "refactor",
        "--state",
        "todo",
        "--priority",
        "medium",
        "--estimate",
        "180",
        "--desc",
        "Replace legacy cookie tokens with rotated OAuth2 refresh tokens.",
        "--embed",
        str(init_desc_png),
        "--attach",
        str(issue_att_txt),
    )
    assert iss1["key"] == "ITR-1"
    issue_att_id = iss1["attachments"][0]["id"]

    iss2 = ws.cli_json(
        "issue",
        "create",
        "Batch Cleanup Worker",
        "--category",
        "chore",
        "--state",
        "backlog",
        "--priority",
        "low",
    )
    assert iss2["key"] == "ITR-2"

    # 2b & 2c. Add comment on ITR-1, then rename ITR-1 folder with spaces
    cmt1 = ws.cli_json(
        "comment",
        "add",
        "ITR-1",
        "--body",
        "Audited legacy token validator paths.",
        "--embed",
        str(png1),
        "--attach",
        str(txt1),
    )
    assert cmt1["id"] == "CMT-1"
    emb1_id = cmt1["embeds"][0]["id"]

    renamed = ws.cli_json(
        "issue",
        "rename",
        "Legacy Session Token Handler",
        "Modernized OAuth2 Session Manager",
    )
    assert renamed["name"] == "Modernized OAuth2 Session Manager"
    assert (ws.issues_dir / "Modernized OAuth2 Session Manager").is_dir()
    assert not (ws.issues_dir / "Legacy Session Token Handler").exists()

    # 2d. Edit comment: replace body, add png2, remove emb1_id, add txt2, link issue_att_id
    cmt1_edited = ws.cli_json(
        "comment",
        "edit",
        "ITR-1",
        "CMT-1",
        "--body",
        "Updated OAuth2 session rotation flow diagram and linked dump.",
        "--add-embed",
        str(png2),
        "--remove-embed",
        emb1_id,
        "--add-attach",
        str(txt2),
        "--link-attachment",
        issue_att_id,
    )
    emb_ids_after = [e["id"] for e in cmt1_edited["embeds"]]
    assert emb1_id not in emb_ids_after
    assert len(emb_ids_after) == 1
    emb2_id = emb_ids_after[0]

    # 2e, 2f, 2g. Bulk update ITR-1 + ITR-2, add worklog to ITR-1, archive ITR-2
    ws.cli_json(
        "bulk",
        "update",
        "--issues",
        "ITR-1",
        "ITR-2",
        "--state",
        "in-review",
        "--priority",
        "critical",
        "--assignee",
        "bob",
        "--add-tag",
        "urgent",
        "--sprint",
        "SPR-1",
    )
    ws.cli_json("issue", "archive", "ITR-2")
    ws.cli_json(
        "worklog",
        "add",
        "ITR-1",
        "1h 30m",
        "--author",
        "bob",
        "--note",
        "Rotated signing keys",
    )

    # --- Reload & verify in Playwright Web UI ---
    page.reload(wait_until="networkidle")
    expect(page.locator("#view-issues")).to_be_visible()

    row_itr1 = page.locator('tr.issue-row[data-key="ITR-1"]')
    expect(row_itr1).to_be_visible()
    expect(row_itr1).to_contain_text("Modernized OAuth2 Session Manager")
    expect(row_itr1).to_contain_text("in-review")
    expect(row_itr1).to_contain_text("critical")
    expect(row_itr1).to_contain_text("bob")
    expect(row_itr1).to_contain_text("urgent")
    expect(page.locator("#issues-tbody")).not_to_contain_text("Legacy Session Token Handler")

    # Switch to Kanban Board: ITR-1 in in-review, archived ITR-2 absent from board
    page.locator('button.nav-item[data-view="board"]').click()
    expect(page.locator("#view-board")).to_be_visible()
    board_card_itr1 = page.locator('.kanban-column[data-state="in-review"] .kanban-card[data-key="ITR-1"]')
    expect(board_card_itr1).to_be_visible()
    expect(board_card_itr1).to_contain_text("Modernized OAuth2 Session Manager")
    expect(page.locator('#view-board .kanban-card[data-key="ITR-2"]')).to_have_count(0)

    # Switch to Issues List -> archived preset tab -> inspect ITR-2 drawer
    page.locator('button.nav-item[data-view="issues"]').click()
    page.locator('#issues-preset-tabs .tab-chip[data-tab="archived"]').click()
    row_itr2 = page.locator('tr.issue-row[data-key="ITR-2"]')
    expect(row_itr2).to_be_visible()
    expect(row_itr2).to_contain_text("Batch Cleanup Worker")
    expect(page.locator('tr.issue-row[data-key="ITR-1"]')).to_have_count(0)

    row_itr2.locator("a.issue-open-link").first.click()
    expect(page.locator("#issue-drawer")).to_be_visible()
    expect(page.locator("#detail-status-banners")).to_contain_text("ARCHIVED")
    expect(page.locator("#btn-unarchive-issue")).to_be_visible()
    page.locator("#btn-close-drawer").click()
    expect(page.locator("#issue-drawer")).not_to_be_visible()

    # Navigate to permalink #/issues/ITR-1 and verify full detail, worklogs, edited comment, embeds & history
    page.goto(f"{live_ui.base_url}/#/issues/ITR-1", wait_until="networkidle")
    expect(page.locator("#issue-drawer")).to_be_visible()
    expect(page.locator("#detail-key-badge")).to_have_text("ITR-1")
    expect(page.locator("#detail-title-input")).to_have_value("Modernized OAuth2 Session Manager")
    expect(page.locator("#detail-time-summary")).to_have_text("90m / 180m")
    assert page.locator("#detail-time-progress").evaluate("el => el.style.width") == "50%"
    expect(page.locator("#detail-worklogs-list")).to_contain_text("Rotated signing keys")

    cmt_card = page.locator('.comment-card[data-comment-id="CMT-1"]')
    expect(cmt_card).to_be_visible()
    expect(cmt_card.locator(".badge", has_text="edited")).to_be_visible()

    # Verify png2 renders with naturalWidth > 0 and removed emb1_id is absent
    embed_img = cmt_card.locator(f'img.md-inline-embed[data-att-id="{emb2_id}"]')
    expect(embed_img).to_be_visible()
    page.wait_for_function(
        "(attId) => {"
        "  const img = document.querySelector(`.comment-card[data-comment-id='CMT-1'] img.md-inline-embed[data-att-id='${attId}']`);"
        "  return Boolean(img && img.complete && img.naturalWidth > 0);"
        "}",
        arg=emb2_id,
    )
    assert embed_img.evaluate("img => img.naturalWidth") == 44
    expect(cmt_card.locator(f'[data-att-id="{emb1_id}"], [data-aid="{emb1_id}"]')).to_have_count(0)
    expect(cmt_card).not_to_contain_text("old_cmt_diagram.png")

    # Verify txt2 comment attachment & linked whole-issue attachment badges render
    expect(cmt_card).to_contain_text("rotation_policy.txt")
    expect(cmt_card).to_contain_text("Linked:")
    expect(cmt_card).to_contain_text("session_tokens.txt")
    expect(cmt_card).to_contain_text(issue_att_id)

    # Click #tab-btn-history and verify rename & update diffs render
    page.locator("#tab-btn-history").click()
    history_list = page.locator("#detail-history-list")
    expect(history_list).to_be_visible()
    expect(history_list).to_contain_text("renamed")
    expect(history_list).to_contain_text("Legacy Session Token Handler")
    expect(history_list).to_contain_text("Modernized OAuth2 Session Manager")
    expect(history_list).to_contain_text("updated")
    expect(history_list).to_contain_text("in-review")


# ---------------------------------------------------------------------------
# Helpers for Test 3 Dynamic 100% Surface Completion
# ---------------------------------------------------------------------------


def _exercise_missing_ui_surfaces(
    live_ui: Any,
    tmp_path: Path,
    missing_views: set[str],
    missing_modals: set[str],
) -> None:
    """Exercise any unvisited UI views and modals via genuine browser interactions on live_ui."""
    page = live_ui.page
    ws = live_ui.ws

    if not missing_views and not missing_modals:
        return

    ui_png, _ = make_png(tmp_path / "gate_embed.png", width=32, height=32, color=(59, 130, 246))
    ui_txt, _ = make_text_file(tmp_path / "gate_preview.txt", text="Gate attachment preview line 1\n")

    sprints = ws.api("GET", "/api/v1/sprints").json()["sprints"]
    active_or_planned = [s for s in sprints if s.get("state") != "completed"]
    if not active_or_planned:
        sp = ws.api("POST", "/api/v1/sprints", json={"name": "Gate Sprint", "goal": "100pct UI gate"}).json()
        spr_id = sp["id"]
        ws.api("POST", f"/api/v1/sprints/{spr_id}/start", json={})
    else:
        spr_id = active_or_planned[0]["id"]

    created_resp = ws.api(
        "POST",
        "/api/v1/issues",
        json={
            "name": "UI Surface Gate Verification Issue",
            "category": "task",
            "state": "todo",
            "priority": "high",
            "sprint": spr_id,
            "description": "Gate modal verification issue.",
        },
    )
    assert created_resp.status_code == 201
    gate_key = created_resp.json()["key"]

    with open(ui_png, "rb") as fh_png:
        ws.api(
            "POST",
            f"/api/v1/issues/{gate_key}/embeds",
            files=[("files", ("gate_embed.png", fh_png, "image/png"))],
        )
    with open(ui_txt, "rb") as fh_txt:
        ws.api(
            "POST",
            f"/api/v1/issues/{gate_key}/attachments",
            files=[("files", ("gate_preview.txt", fh_txt, "text/plain"))],
        )

    page.reload(wait_until="networkidle")

    # 1. Visit all needed UI views (8/8)
    for view_name in UI_VIEWS:
        if view_name in missing_views:
            page.locator(f'button.nav-item[data-view="{view_name}"]').click()
            expect(page.locator(f"#view-{view_name}")).to_be_visible()

    # 2. Open all needed UI modals (10/10)
    if "create-issue" in missing_modals:
        page.locator("#btn-open-create").click()
        expect(page.locator('[data-modal="create-issue"]')).to_be_visible()
        page.locator('[data-close-modal="create-issue"]').first.click()
        expect(page.locator('[data-modal="create-issue"]')).not_to_be_visible()

    if "save-view" in missing_modals:
        page.locator("#btn-save-current-view").click()
        expect(page.locator('[data-modal="save-view"]')).to_be_visible()
        page.locator('[data-close-modal="save-view"]').first.click()
        expect(page.locator('[data-modal="save-view"]')).not_to_be_visible()

    if "sprint-rollover" in missing_modals:
        page.locator('button.nav-item[data-view="sprints"]').click()
        expect(page.locator("#view-sprints")).to_be_visible()
        complete_btn = page.locator(f'.btn-sprint-complete[data-id="{spr_id}"]')
        expect(complete_btn).to_be_visible()
        complete_btn.click()
        expect(page.locator('[data-modal="sprint-rollover"]')).to_be_visible()
        page.locator('[data-close-modal="sprint-rollover"]').first.click()
        expect(page.locator('[data-modal="sprint-rollover"]')).not_to_be_visible()

    drawer_modals = {
        "issue-detail",
        "link-issue",
        "log-work",
        "image-lightbox",
        "attachment-preview",
        "link-attachment",
        "delete-confirm",
    }
    if missing_modals & drawer_modals:
        page.locator('button.nav-item[data-view="issues"]').click()
        expect(page.locator("#view-issues")).to_be_visible()
        page.locator('#issues-preset-tabs .tab-chip[data-tab="all"]').click()
        issue_link = page.locator(f'a.issue-open-link[data-key="{gate_key}"]').first
        expect(issue_link).to_be_visible()
        issue_link.click()
        expect(page.locator("#issue-drawer")).to_be_visible()

        if "link-issue" in missing_modals:
            page.locator("#btn-open-link-modal").click()
            expect(page.locator('[data-modal="link-issue"]')).to_be_visible()
            page.locator('[data-close-modal="link-issue"]').first.click()
            expect(page.locator('[data-modal="link-issue"]')).not_to_be_visible()

        if "log-work" in missing_modals:
            page.locator("#btn-open-worklog-modal").click()
            expect(page.locator('[data-modal="log-work"]')).to_be_visible()
            page.locator('[data-close-modal="log-work"]').first.click()
            expect(page.locator('[data-modal="log-work"]')).not_to_be_visible()

        if "image-lightbox" in missing_modals:
            desc_img = page.locator("#detail-desc-rendered img.md-inline-embed").first
            expect(desc_img).to_be_visible()
            desc_img.click()
            expect(page.locator('[data-modal="image-lightbox"]')).to_be_visible()
            page.locator('[data-close-modal="image-lightbox"]').first.click()
            expect(page.locator('[data-modal="image-lightbox"]')).not_to_be_visible()

        if "attachment-preview" in missing_modals:
            prev_btn = page.locator("#detail-attachments-list .btn-att-preview").first
            expect(prev_btn).to_be_visible()
            prev_btn.click()
            expect(page.locator('[data-modal="attachment-preview"]')).to_be_visible()
            page.locator('[data-close-modal="attachment-preview"]').first.click()
            expect(page.locator('[data-modal="attachment-preview"]')).not_to_be_visible()

        if "link-attachment" in missing_modals:
            page.locator("#btn-cmt-link-att").click()
            expect(page.locator('[data-modal="link-attachment"]')).to_be_visible()
            page.locator('[data-close-modal="link-attachment"]').first.click()
            expect(page.locator('[data-modal="link-attachment"]')).not_to_be_visible()

        if "delete-confirm" in missing_modals:
            page.locator("#btn-delete-issue").click()
            expect(page.locator('[data-modal="delete-confirm"]')).to_be_visible()
            page.locator("#btn-cancel-delete").click()
            expect(page.locator('[data-modal="delete-confirm"]')).not_to_be_visible()

        page.locator("#btn-close-drawer").click()
        expect(page.locator("#issue-drawer")).not_to_be_visible()


def _exercise_full_cli_and_api_surface(tmp_path: Path) -> None:
    """Execute real itrack.py subprocesses on an isolated temp Git repo covering all 72 commands, 90 flags & 67 API routes."""
    extra_repo = init_git_repo(tmp_path / "cli_api_surface_repo")
    ws = ITrackWorkspace(extra_repo)

    assets_dir = tmp_path / "cli_assets"
    png1, _ = make_png(assets_dir / "emb1.png", width=20, height=20, color=(37, 99, 235))
    png2, _ = make_png(assets_dir / "emb2.png", width=24, height=24, color=(16, 185, 129))
    png3, _ = make_png(assets_dir / "emb3.png", width=28, height=28, color=(245, 158, 11))
    att1, _ = make_text_file(assets_dir / "trace1.log", text="trace line 1\n")
    att2, _ = make_text_file(assets_dir / "trace2.log", text="trace line 2\n")
    att3, _ = make_text_file(assets_dir / "trace3.log", text="trace line 3\n")
    desc_file, _ = make_text_file(assets_dir / "desc.md", text="### Description from file\n\n- Bullet 1\n")
    cmt_file, _ = make_text_file(assets_dir / "comment.md", text="Comment body loaded from file.\n")

    try:
        # Phase 0: Sequential daemon lifecycle commands (init, server status/restart/start, ui, ui --foreground)
        ws.cli_json("init", "--prefix", "ITR", "--title", "Surface Gate Repo", use_root_flag=True)
        ws.cli_json("server", "status")
        ws.cli_json("server", "restart", "--port", "0")
        ws.cli_json("ui", "--port", "0", "--no-browser")

        fg_env = os.environ.copy()
        fg_env["PYTHONDONTWRITEBYTECODE"] = "1"
        fg_proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(ITRACK_PY),
                "ui",
                "--foreground",
                "--no-browser",
                "--port",
                "0",
                "--json",
            ],
            cwd=str(extra_repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=fg_env,
        )
        fg_json_holder: dict[str, Any] = {}

        def _read_fg_json() -> None:
            assert fg_proc.stdout is not None
            buf: list[str] = []
            for line in fg_proc.stdout:
                buf.append(line)
                joined = "".join(buf).strip()
                if joined.startswith("{") and joined.endswith("}"):
                    try:
                        fg_json_holder["data"] = json.loads(joined)
                        return
                    except json.JSONDecodeError:
                        continue

        reader = threading.Thread(target=_read_fg_json, daemon=True)
        reader.start()
        reader.join(timeout=15.0)
        assert "data" in fg_json_holder, "Timed out reading JSON from `itrack ui --foreground --json`"
        fg_base_url = str(fg_json_holder["data"]["base_url"]).rstrip("/")
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            resp = client.post(f"{fg_base_url}/api/v1/shutdown")
            assert resp.status_code == 200
        fg_proc.wait(timeout=10.0)
        cleanup_repo_daemon(extra_repo)

        # Ensure background daemon is active once so all parallel waves reuse it cleanly
        ws.cli_json("server", "start", "--port", "0")

        # Wave 1: Global flags + config + base tags, sprints & milestones
        w1 = _run_parallel_tasks(
            {
                "help": lambda: run_itrack_cli("-h", cwd=extra_repo, expect_rc=0),
                "ver": lambda: run_itrack_cli("-V", "--json", cwd=extra_repo, expect_rc=0),
                "status": lambda: ws.cli_json("status"),
                "cfg_show": lambda: ws.cli_json("config", "show"),
                "cfg_set": lambda: ws.cli_json("config", "set", "project_title", "Surface Gate Updated"),
                "tag_alpha": lambda: ws.cli_json("tag", "create", "alpha", "--color", "#ef4444", "--desc", "Alpha tag"),
                "tag_temp": lambda: ws.cli_json("tag", "create", "temp-tag", "--color", "#10b981", "--desc", "Temp"),
                "sp1": lambda: ws.cli_json(
                    "sprint",
                    "create",
                    "Gate Sprint 1",
                    "--goal",
                    "Complete parity",
                    "--start",
                    "2026-11-01",
                    "--end",
                    "2026-11-14",
                ),
                "sp2": lambda: ws.cli_json(
                    "sprint",
                    "create",
                    "Gate Sprint 2",
                    "--goal",
                    "Rollover destination",
                    "--start",
                    "2026-11-15",
                    "--end",
                    "2026-11-28",
                ),
                "ms1": lambda: ws.cli_json(
                    "milestone", "create", "Release 1.0", "--desc", "Initial GA", "--due", "2026-12-01"
                ),
                "ms2": lambda: ws.cli_json(
                    "milestone", "create", "Release 2.0", "--desc", "Next GA", "--due", "2026-12-31"
                ),
            }
        )
        sp1_id = w1["sp1"]["id"]
        sp2_id = w1["sp2"]["id"]
        ms1_id = w1["ms1"]["id"]
        ms2_id = w1["ms2"]["id"]

        # Wave 2: Agile list/show/edit + create primary/stdin/throwaway issues + create saved view
        w2 = _run_parallel_tasks(
            {
                "tag_list": lambda: ws.cli_json("tag", "list"),
                "tag_edit": lambda: ws.cli_json(
                    "tag",
                    "edit",
                    "temp-tag",
                    "--new-name",
                    "temp-tag-renamed",
                    "--color",
                    "#3b82f6",
                    "--desc",
                    "Renamed",
                ),
                "sp_list": lambda: ws.cli_json("sprint", "list", "--state", "planned"),
                "sp_show": lambda: ws.cli_json("sprint", "show", sp1_id),
                "sp_edit": lambda: ws.cli_json(
                    "sprint",
                    "edit",
                    sp1_id,
                    "--name",
                    "Gate Sprint 1 Edited",
                    "--goal",
                    "Updated goal",
                    "--start",
                    "2026-11-02",
                    "--end",
                    "2026-11-14",
                ),
                "ms_list": lambda: ws.cli_json("milestone", "list", "--state", "open"),
                "ms_show": lambda: ws.cli_json("milestone", "show", ms1_id),
                "ms_edit": lambda: ws.cli_json(
                    "milestone",
                    "edit",
                    ms1_id,
                    "--name",
                    "Release 1.0 GA",
                    "--desc",
                    "Updated GA",
                    "--due",
                    "2026-12-05",
                ),
                "iss1": lambda: ws.cli_json(
                    "issue",
                    "create",
                    "Primary Gate Epic Issue",
                    "--category",
                    "epic",
                    "--state",
                    "todo",
                    "--priority",
                    "critical",
                    "--severity",
                    "sev1",
                    "--resolution",
                    "fixed",
                    "--desc",
                    "Epic description body",
                    "--embed",
                    str(png1),
                    "--attach",
                    str(att1),
                    "--ac",
                    "Criterion One",
                    "Criterion Two",
                    "--assignee",
                    "alice",
                    "--reporter",
                    "bob",
                    "--tag",
                    "alpha",
                    "--component",
                    "core",
                    "--sprint",
                    sp1_id,
                    "--milestone",
                    ms1_id,
                    "--points",
                    "8",
                    "--estimate",
                    "240",
                    "--due",
                    "2020-01-01",
                    "--field",
                    "env=prod",
                ),
                "iss3": lambda: ws.cli_json(
                    "issue",
                    "create",
                    "Tertiary Stdin Issue",
                    "--desc-stdin",
                    stdin="Created via --desc-stdin\n",
                ),
                "del_a": lambda: ws.cli_json("issue", "create", "Bulk Delete Target One"),
                "del_b": lambda: ws.cli_json("issue", "create", "Bulk Delete Target Two"),
                "vw": lambda: ws.cli_json(
                    "view",
                    "create",
                    "Gate Saved View",
                    "--query",
                    "priority = critical",
                    "--tab",
                    "all",
                    "--sort",
                    "priority",
                    "--order",
                    "desc",
                    "--swimlane",
                    "priority",
                    "--desc",
                    "Gate saved view",
                ),
            }
        )
        iss1_key = w2["iss1"]["key"]
        iss1_att_id = w2["iss1"]["attachments"][0]["id"]
        iss3_key = w2["iss3"]["key"]
        del_a_key = w2["del_a"]["key"]
        del_b_key = w2["del_b"]["key"]
        vw_id = w2["vw"]["id"]

        # Wave 3: Child issue create, clone, clear-flags edit, comments/attachments/worklogs, views, bulk delete
        w3 = _run_parallel_tasks(
            {
                "tag_del": lambda: ws.cli_json("tag", "delete", "temp-tag-renamed", "--yes"),
                "sp_start": lambda: ws.cli_json(
                    "sprint", "start", sp1_id, "--start", "2026-11-02", "--end", "2026-11-14"
                ),
                "iss2": lambda: ws.cli_json(
                    "issue",
                    "create",
                    "Secondary Child Bug Issue",
                    "--category",
                    "bug",
                    "--parent",
                    iss1_key,
                    "--desc-file",
                    str(desc_file),
                ),
                "cloned": lambda: ws.cli_json("issue", "clone", iss1_key, "--name", "Cloned Gate Ephemeral"),
                "iss3_clear": lambda: ws.cli_json(
                    "issue",
                    "edit",
                    iss3_key,
                    "--clear-resolution",
                    "--clear-desc",
                    "--clear-ac",
                    "--clear-assignee",
                    "--clear-reporter",
                    "--clear-tags",
                    "--clear-components",
                    "--clear-parent",
                    "--clear-sprint",
                    "--clear-milestone",
                    "--clear-points",
                    "--clear-estimate",
                    "--clear-due",
                    "--clear-fields",
                ),
                "c1": lambda: ws.cli_json(
                    "comment",
                    "add",
                    iss1_key,
                    "--body",
                    "Rich gate comment",
                    "--author",
                    "alice",
                    "--embed",
                    str(png1),
                    str(png2),
                    "--attach",
                    str(att1),
                    str(att2),
                    "--link-attachment",
                    iss1_att_id,
                ),
                "c2": lambda: ws.cli_json("comment", "add", iss1_key, "--body-file", str(cmt_file)),
                "c3": lambda: ws.cli_json("comment", "add", iss1_key, "--stdin", stdin="Stdin comment body\n"),
                "atts_add": lambda: ws.cli_json("attach", "add", iss1_key, str(att2), str(att3), "--by", "alice"),
                "vw_list": lambda: ws.cli_json("view", "list"),
                "vw_show": lambda: ws.cli_json("view", "show", vw_id),
                "vw_run": lambda: ws.cli_json("view", "run", vw_id, "--page", "1", "--page-size", "10"),
                "bd_1": lambda: ws.cli_json("bulk", "delete", "--issues", del_a_key, "--yes"),
                "bd_2": lambda: ws.cli_json("bulk", "delete", "--query", f"key = {del_b_key}", "--yes"),
            }
        )
        iss2_key = w3["iss2"]["key"]
        cloned_key = w3["cloned"]["key"]
        c1 = w3["c1"]
        c1_id = c1["id"]
        c1_emb1, c1_emb2 = c1["embeds"][0]["id"], c1["embeds"][1]["id"]
        c1_att1, c1_att2 = c1["attachments"][0]["id"], c1["attachments"][1]["id"]
        c3_id = w3["c3"]["id"]
        extra_att_a = w3["atts_add"]["attachments"][0]["id"]
        extra_att_b = w3["atts_add"]["attachments"][1]["id"]

        # Wave 4: Issue edit 1 on iss2, state/rename/delete, comment list/show/edit/delete, attach ops, view edit
        w4 = _run_parallel_tasks(
            {
                "edited_iss2": lambda: ws.cli_json(
                    "issue",
                    "edit",
                    iss2_key,
                    "--name",
                    "Secondary Child Bug Edited",
                    "--category",
                    "bug",
                    "--state",
                    "in-progress",
                    "--priority",
                    "critical",
                    "--severity",
                    "sev1",
                    "--resolution",
                    "fixed",
                    "--desc",
                    "Edited description text",
                    "--embed",
                    str(png2),
                    "--add-ac",
                    "Child AC 1",
                    "Child AC 2",
                    "--assignee",
                    "alice",
                    "--reporter",
                    "carol",
                    "--add-tag",
                    "alpha",
                    "beta",
                    "--add-component",
                    "api",
                    "worker",
                    "--parent",
                    iss1_key,
                    "--sprint",
                    sp1_id,
                    "--milestone",
                    ms1_id,
                    "--points",
                    "5",
                    "--estimate",
                    "120",
                    "--due",
                    "2020-06-01",
                    "--field",
                    "k1=v1",
                    "k2=v2",
                ),
                "iss1_state": lambda: ws.cli_json("issue", "state", iss1_key, "in-progress", "--resolution", "fixed"),
                "iss3_ren": lambda: ws.cli_json("issue", "rename", iss3_key, "Tertiary Issue Renamed"),
                "clone_del": lambda: ws.cli_json("issue", "delete", cloned_key, "--yes"),
                "cmt_list": lambda: ws.cli_json("comment", "list", iss1_key),
                "cmt_show": lambda: ws.cli_json("comment", "show", iss1_key, c1_id),
                "cmt_edit1": lambda: ws.cli_json(
                    "comment",
                    "edit",
                    iss1_key,
                    c1_id,
                    "--body",
                    "Edited comment body",
                    "--actor",
                    "bob",
                    "--add-embed",
                    str(png3),
                    "--remove-embed",
                    c1_emb1,
                    "--add-attach",
                    str(att3),
                    "--remove-attach",
                    c1_att1,
                    "--unlink-attachment",
                    iss1_att_id,
                ),
                "cmt_del": lambda: ws.cli_json("comment", "delete", iss1_key, c3_id, "--yes"),
                "att_list": lambda: ws.cli_json("attach", "list", iss1_key, "--scope", "all"),
                "att_info": lambda: ws.cli_json("attach", "info", iss1_key, extra_att_a),
                "att_dl": lambda: ws.cli_json(
                    "attach", "download", iss1_key, extra_att_a, "--output", str(tmp_path / "gate_dl.log")
                ),
                "att_del": lambda: ws.cli_json("attach", "delete", iss1_key, extra_att_b, "--yes"),
                "vw_edit": lambda: ws.cli_json(
                    "view",
                    "edit",
                    vw_id,
                    "--name",
                    "Gate Saved View Edited",
                    "--query",
                    "state != archived",
                    "--tab",
                    "active",
                    "--sort",
                    "updated_at",
                    "--order",
                    "desc",
                    "--swimlane",
                    "sprint",
                    "--desc",
                    "Edited description",
                ),
            }
        )
        edited_iss2 = w4["edited_iss2"]
        iss2_embeds = (
            edited_iss2.get("embeds")
            or edited_iss2.get("description_embeds")
            or edited_iss2.get("issue", {}).get("description_embeds")
            or []
        )
        iss2_emb_id = iss2_embeds[0]["id"]

        # Wave 5: Issue edit 2 (remove-embed/AC/tags/comps/fields), archive, link add, list/search/board/tree/stats
        w5 = _run_parallel_tasks(
            {
                "iss2_edit2": lambda: ws.cli_json(
                    "issue",
                    "edit",
                    iss2_key,
                    "--desc-file",
                    str(desc_file),
                    "--remove-embed",
                    iss2_emb_id,
                    "--edit-ac",
                    "AC-1",
                    "Child AC 1 Revised",
                    "--check-ac",
                    "AC-1",
                    "--uncheck-ac",
                    "AC-2",
                    "--remove-ac",
                    "AC-2",
                    "--remove-tag",
                    "beta",
                    "--remove-component",
                    "worker",
                    "--unset-field",
                    "k2",
                ),
                "iss3_arch": lambda: ws.cli_json("issue", "archive", iss3_key),
                "lnk_add": lambda: ws.cli_json("link", "add", iss1_key, "blocks", iss2_key),
                "iss_show": lambda: ws.cli_json("issue", "show", iss1_key),
                "iss_ctx": lambda: ws.cli_json("issue", "context", iss1_key),
                "iss_list": lambda: ws.cli_json(
                    "issue",
                    "list",
                    "--tab",
                    "all",
                    "--query",
                    "priority = critical",
                    "--category",
                    "bug",
                    "--state",
                    "in-progress",
                    "--priority",
                    "critical",
                    "--severity",
                    "sev1",
                    "--resolution",
                    "fixed",
                    "--assignee",
                    "alice",
                    "--reporter",
                    "carol",
                    "--tag",
                    "alpha",
                    "--component",
                    "api",
                    "--parent",
                    iss1_key,
                    "--sprint",
                    sp1_id,
                    "--milestone",
                    ms1_id,
                    "--overdue",
                    "--sort",
                    "priority",
                    "--order",
                    "desc",
                    "--page",
                    "1",
                    "--page-size",
                    "10",
                ),
                "cmt_edit2": lambda: ws.cli_json(
                    "comment",
                    "edit",
                    iss1_key,
                    c1_id,
                    "--body-file",
                    str(cmt_file),
                    "--link-attachment",
                    iss1_att_id,
                ),
                "tree_all": lambda: ws.cli_json("tree"),
                "tree_one": lambda: ws.cli_json("tree", iss1_key),
                "search": lambda: ws.cli_json(
                    "search",
                    "priority = critical",
                    "--iql",
                    "--page",
                    "1",
                    "--page-size",
                    "10",
                    "--sort",
                    "priority",
                    "--order",
                    "desc",
                ),
                "board": lambda: ws.cli_json(
                    "board",
                    "--swimlane",
                    "sprint",
                    "--category",
                    "bug",
                    "--priority",
                    "critical",
                    "--assignee",
                    "alice",
                    "--sprint",
                    sp1_id,
                    "--tag",
                    "alpha",
                    "--query",
                    "state != cancelled",
                ),
                "hist": lambda: ws.cli_json("history", iss1_key),
                "stats": lambda: ws.cli_json("stats"),
                "vw_del": lambda: ws.cli_json("view", "delete", vw_id, "--yes"),
                "ms_close": lambda: ws.cli_json("milestone", "close", ms1_id),
            }
        )
        lnk_id = w5["lnk_add"]["id"]

        # Wave 6: Stdin edits, unarchive, comment embed-delete, link list, milestone reopen & delete
        _run_parallel_tasks(
            {
                "iss2_stdin": lambda: ws.cli_json(
                    "issue", "edit", iss2_key, "--desc-stdin", stdin="Updated ITR-2 description from stdin\n"
                ),
                "iss3_unarch": lambda: ws.cli_json("issue", "unarchive", iss3_key),
                "cmt_emb_del": lambda: ws.cli_json("comment", "embed-delete", iss1_key, c1_id, c1_emb2),
                "lnk_list": lambda: ws.cli_json("link", "list", iss1_key),
                "ms_reopen": lambda: ws.cli_json("milestone", "reopen", ms1_id),
                "ms_del": lambda: ws.cli_json("milestone", "delete", ms2_id, "--yes"),
            }
        )

        # Wave 7: comment attach-delete, link remove, bulk update 1, sprint assign, reindex
        _run_parallel_tasks(
            {
                "cmt_att_del": lambda: ws.cli_json("comment", "attach-delete", iss1_key, c1_id, c1_att2),
                "lnk_rem": lambda: ws.cli_json("link", "remove", iss1_key, lnk_id),
                "bu_1": lambda: ws.cli_json(
                    "bulk",
                    "update",
                    "--issues",
                    iss1_key,
                    iss2_key,
                    "--state",
                    "ready",
                    "--priority",
                    "high",
                    "--severity",
                    "sev2",
                    "--category",
                    "task",
                    "--assignee",
                    "dave",
                    "--add-tag",
                    "alpha",
                    "--remove-tag",
                    "unused-tag",
                    "--sprint",
                    sp1_id,
                    "--milestone",
                    ms1_id,
                    "--archive",
                ),
                "reindex": lambda: ws.cli_json("reindex"),
            }
        )

        # Wave 8: comment edit stdin, bulk update 2, sprint assign/remove/complete/delete
        _run_parallel_tasks(
            {
                "cmt_stdin": lambda: ws.cli_json(
                    "comment", "edit", iss1_key, c1_id, "--stdin", stdin="Comment edited from stdin\n"
                ),
                "bu_2": lambda: ws.cli_json(
                    "bulk",
                    "update",
                    "--query",
                    f"key = {iss1_key} OR key = {iss2_key}",
                    "--clear-assignee",
                    "--clear-sprint",
                    "--clear-milestone",
                    "--unarchive",
                ),
            }
        )

        ws.cli_json("sprint", "assign", sp1_id, iss1_key, iss2_key)
        ws.cli_json("sprint", "remove", sp1_id, iss1_key)
        ws.cli_json("sprint", "complete", sp1_id, "--rollover", sp2_id)
        ws.cli_json("sprint", "delete", sp2_id, "--yes")

        wl = ws.cli_json("worklog", "add", iss1_key, "45m", "--author", "alice", "--note", "Gate")
        ws.cli_json("worklog", "list", iss1_key)
        ws.cli_json("worklog", "delete", iss1_key, wl["worklog"]["id"], "--yes")

        ws.cli_json("server", "stop")
    finally:
        cleanup_repo_daemon(extra_repo)


# ---------------------------------------------------------------------------
# Test 3: Automated 100% Surface & Endpoint Parity Gate
# ---------------------------------------------------------------------------


def test_03_automated_100pct_surface_and_endpoint_parity_gate(
    live_ui: Any,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    # --- Part 1: Static Endpoint & Surface Parity Audit ---
    server_mod = load_itrack_server_module()
    cli_mod = load_itrack_cli_module()

    spa_surface = introspect_spa_surface(getattr(server_mod, "SPA_HTML", ""))
    api_routes = introspect_api_routes(server_mod)
    cli_surface = introspect_cli_surface(cli_mod)

    backend_shapes = {normalize_route_shape(path) for _, path in api_routes}
    cli_api_shapes = _extract_cli_api_shapes(ITRACK_PY.read_text(encoding="utf-8"))

    orphan_spa_endpoints = sorted(
        ep for ep in spa_surface["endpoints"] if normalize_route_shape(ep) not in backend_shapes
    )
    unmapped_spa_endpoints = sorted(
        ep for ep in spa_surface["endpoints"] if normalize_route_shape(ep) not in cli_api_shapes
    )

    assert orphan_spa_endpoints == [], f"SPA calls missing backend routes: {orphan_spa_endpoints}"
    assert unmapped_spa_endpoints == [], (
        f"SPA calls endpoints with no CLI counterpart in itrack.py: {unmapped_spa_endpoints}"
    )
    assert set(spa_surface["views"]) == set(UI_VIEWS)
    assert len(UI_VIEWS) == 8
    assert set(spa_surface["modals"]) == set(UI_MODALS)
    assert len(UI_MODALS) == 10
    assert len(api_routes) == 67
    assert len(cli_surface["commands"]) == 72
    assert len(cli_surface["all_flags"]) == 90

    # --- Part 2: Dynamic 100% Surface Completion & Gate Assertion ---
    if not os.environ.get("ITRACK_SURFACE_LOG"):
        monkeypatch.setenv("ITRACK_SURFACE_LOG", str(tmp_path / "surface.jsonl"))

    live_ui.harvest_telemetry()
    audit = compute_surface_audit(cli_mod=cli_mod, server_mod=server_mod)

    missing_views = set(audit["ui_views"]["missing"])
    missing_modals = set(audit["ui_modals"]["missing"])
    if missing_views or missing_modals:
        _exercise_missing_ui_surfaces(live_ui, tmp_path, missing_views, missing_modals)
        live_ui.harvest_telemetry()

    if (
        audit["api_routes"]["missing"]
        or audit["cli_commands"]["missing"]
        or audit["cli_flags"]["missing"]
    ):
        _exercise_full_cli_and_api_surface(tmp_path)

    final_audit = compute_surface_audit(cli_mod=cli_mod, server_mod=server_mod)

    assert final_audit["orphan_spa_endpoints"] == []
    assert final_audit["api_routes"]["missing"] == [], (
        f"Uncovered /api/v1/* routes: {final_audit['api_routes']['missing']}"
    )
    assert final_audit["cli_commands"]["missing"] == [], (
        f"Uncovered CLI leaf commands: {final_audit['cli_commands']['missing']}"
    )
    assert final_audit["cli_flags"]["missing"] == [], (
        f"Uncovered CLI option flags: {final_audit['cli_flags']['missing']}"
    )
    assert final_audit["ui_views"]["missing"] == [], (
        f"Uncovered Web UI views: {final_audit['ui_views']['missing']}"
    )
    assert final_audit["ui_modals"]["missing"] == [], (
        f"Uncovered Web UI modals: {final_audit['ui_modals']['missing']}"
    )
