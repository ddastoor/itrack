"""Suite 04: Rich Markdown Comments, Inline Embedded Images (PNG/SVG/JPG/WEBP), Comment & Whole-Issue Attachments, Cross-Links, Tombstones, Edit History & Full Comment Deletion Matrix."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import (
    ITrackWorkspace,
    assert_clean_repo_root,
    make_jpg,
    make_json_file,
    make_pdf,
    make_png,
    make_svg,
    make_text_file,
    make_webp,
    make_zero_byte_file,
    make_zip,
    sha256_file,
)


def test_01_comment_add_and_edit_input_modes_rich_markdown_xss_and_dual_addressing(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Exercise `comment add` & `comment edit` across `--body`, `--body-file`, and `--stdin`,
    with `--author` / `--actor`, dual addressing (ITR-N & exact spaced folder name),
    complex GFM Markdown, Unicode/emojis, XSS payloads, and Rich + `--json` output modes."""
    spec_file, spec_sha = make_text_file(
        tmp_path / "design_spec.txt",
        text="Canonical design specification v1.0\n",
    )

    target_name = "Rich Discussion Target Issue"
    peer_name = "Mentioned Peer Issue"

    target = workspace.cli_json(
        "issue",
        "create",
        target_name,
        "--category",
        "feature",
        "--priority",
        "high",
        "--attach",
        str(spec_file),
    )
    peer = workspace.cli_json("issue", "create", peer_name, "--category", "bug")
    assert target["key"] == "ITR-1"
    assert peer["key"] == "ITR-2"
    assert len(target["attachments"]) == 1
    att1_id = target["attachments"][0]["id"]
    assert att1_id == "ATT-1"
    assert target["attachments"][0]["sha256"] == spec_sha

    # Empty comment list before any comments exist (both Rich & --json, dual addressing)
    empty_list_json = workspace.cli_json("comment", "list", target_name)
    assert empty_list_json == {"key": "ITR-1", "comments": []}
    empty_list_rich = workspace.cli("comment", "list", "ITR-1")
    assert "No comments." in empty_list_rich.stdout

    # 1. `comment add --body` addressed by ITR-1 with --author, full GFM + Unicode + XSS
    body_mode_md = (
        "# Heading Level 1\n"
        "## Heading Level 2\n"
        "### Heading Level 3\n\n"
        "| Subsystem | Latency | Status |\n"
        "| --- | ---: | :---: |\n"
        "| Auth Core | 12ms | OK |\n"
        "| FTS5 Index | 3ms | 🚀 |\n\n"
        "```python\n"
        "def verify_token(tok: str) -> bool:\n"
        "    return tok.startswith('itr_')\n"
        "```\n\n"
        "```json\n"
        '{"env": "prod", "unicode": "🚀 日本語 café Ω"}\n'
        "```\n\n"
        "- [ ] Add chaos failover test\n"
        "- [x] Verify deterministic SHA-256\n\n"
        f"Cross-refs: #ITR-1 and #ITR-2 plus [Spec Doc](attachment://{att1_id}).\n"
        "Unicode suite: 🚀 日本語 café Ω\n"
        "XSS vectors: <script>alert(1)</script> and <img src=x onerror=alert(1)>\n"
    )
    cmt1 = workspace.cli_json(
        "comment",
        "add",
        "ITR-1",
        "--body",
        body_mode_md,
        "--author",
        "alice",
    )
    assert cmt1["id"] == "CMT-1"
    assert cmt1["seq"] == 1
    assert cmt1["issue_key"] == "ITR-1"
    assert cmt1["author"] == "alice"
    assert cmt1["body"] == body_mode_md
    assert cmt1["edit_history"] == []

    # 2. `comment add --body-file` addressed by exact spaced name in Rich mode
    body_file_path = tmp_path / "comment_from_file.md"
    body_file_md = (
        "## File Input Mode Analysis\n"
        "- [ ] Check #ITR-2 regression\n"
        "- [x] Validate [Attachment Ref](attachment://ATT-1)\n"
        "Emoji & CJK: 🚀 日本語 café Ω | Payload: <script>alert(1)</script>\n"
    )
    body_file_path.write_text(body_file_md, encoding="utf-8")
    cmt2_rich = workspace.cli(
        "comment",
        "add",
        target_name,
        "--body-file",
        str(body_file_path),
        "--author",
        "bob",
    )
    assert "CMT-2" in cmt2_rich.stdout
    assert "bob" in cmt2_rich.stdout
    assert "File Input Mode Analysis" in cmt2_rich.stdout

    # 3. `comment add --stdin` addressed by exact spaced name with --json
    stdin_md = (
        "### Stdin Input Mode\n"
        "```json\n"
        '{"mentions": ["#ITR-1", "#ITR-2"], "i18n": "🚀 日本語 café Ω"}\n'
        "```\n"
        "Untrusted HTML: <img src=x onerror=alert(1)> <script>alert(1)</script>\n"
    )
    cmt3 = workspace.cli_json(
        "comment",
        "add",
        target_name,
        "--stdin",
        "--author",
        "carol",
        stdin=stdin_md,
    )
    assert cmt3["id"] == "CMT-3"
    assert cmt3["author"] == "carol"
    assert cmt3["body"] == stdin_md

    # 4. `comment edit` across all 3 input modes (`--body`, `--body-file`, `--stdin`) + `--actor` + dual addressing
    edited_cmt1_md = body_mode_md + "\nEdited via --body by dave.\n"
    cmt1_edited = workspace.cli_json(
        "comment",
        "edit",
        target_name,
        "CMT-1",
        "--body",
        edited_cmt1_md,
        "--actor",
        "dave",
    )
    assert cmt1_edited["body"] == edited_cmt1_md
    assert len(cmt1_edited["edit_history"]) == 1
    assert cmt1_edited["edit_history"][0]["actor"] == "dave"
    assert cmt1_edited["edit_history"][0]["previous_body"] == body_mode_md

    edit_file_path = tmp_path / "edited_comment_2.md"
    edited_cmt2_md = body_file_md + "\nEdited via --body-file by eve (🚀 日本語 café Ω).\n"
    edit_file_path.write_text(edited_cmt2_md, encoding="utf-8")
    cmt2_edited = workspace.cli_json(
        "comment",
        "edit",
        "ITR-1",
        "CMT-2",
        "--body-file",
        str(edit_file_path),
        "--actor",
        "eve",
    )
    assert cmt2_edited["body"] == edited_cmt2_md
    assert cmt2_edited["edit_history"][0]["actor"] == "eve"
    assert cmt2_edited["edit_history"][0]["previous_body"] == body_file_md

    edited_cmt3_md = stdin_md + "\nEdited via --stdin by frank.\n"
    cmt3_edit_rich = workspace.cli(
        "comment",
        "edit",
        target_name,
        "CMT-3",
        "--stdin",
        "--actor",
        "frank",
        stdin=edited_cmt3_md,
    )
    assert "CMT-3" in cmt3_edit_rich.stdout
    assert "edited 1x" in cmt3_edit_rich.stdout
    assert "Edited via --stdin by frank." in cmt3_edit_rich.stdout

    # Verify on-disk canonical JSON layout under issues/<Exact Name>/comments/<CMT-N>/comment.json
    cmt1_disk_path = workspace.issue_dir(target_name) / "comments" / "CMT-1" / "comment.json"
    assert cmt1_disk_path.is_file()
    cmt1_disk = json.loads(cmt1_disk_path.read_text(encoding="utf-8"))
    assert cmt1_disk["body"] == edited_cmt1_md
    assert "<script>alert(1)</script>" in cmt1_disk["body"]
    assert "<img src=x onerror=alert(1)>" in cmt1_disk["body"]
    assert "🚀 日本語 café Ω" in cmt1_disk["body"]

    # Verify `comment list`, `comment show`, and `issue show` in BOTH Rich text mode and `--json` mode
    clist_json = workspace.cli_json("comment", "list", "ITR-1")
    assert clist_json["key"] == "ITR-1"
    assert [c["id"] for c in clist_json["comments"]] == ["CMT-1", "CMT-2", "CMT-3"]

    clist_rich = workspace.cli("comment", "list", target_name)
    for token in ("CMT-1", "CMT-2", "CMT-3", "alice", "bob", "carol", "🚀 日本語 café Ω", "<script>alert(1)</script>"):
        assert token in clist_rich.stdout

    cshow_json = workspace.cli_json("comment", "show", target_name, "cmt-1")
    assert cshow_json["id"] == "CMT-1"
    assert cshow_json["body"] == edited_cmt1_md

    cshow_rich = workspace.cli("comment", "show", "ITR-1", "CMT-1")
    assert "CMT-1" in cshow_rich.stdout
    assert "Heading Level 1" in cshow_rich.stdout
    assert "edited 1x" in cshow_rich.stdout
    assert "<img src=x onerror=alert(1)>" in cshow_rich.stdout

    ishow_json = workspace.cli_json("issue", "show", target_name)
    assert ishow_json["key"] == "ITR-1"
    assert ishow_json["comment_count"] == 3
    assert [c["id"] for c in ishow_json["comments"]] == ["CMT-1", "CMT-2", "CMT-3"]

    ishow_rich = workspace.cli("issue", "show", "ITR-1")
    assert target_name in ishow_rich.stdout
    assert "CMT-1" in ishow_rich.stdout
    assert "CMT-3" in ishow_rich.stdout
    assert "🚀 日本語 café Ω" in ishow_rich.stdout

    # Also verify application/json REST endpoints for comments (POST & PATCH)
    api_post = workspace.api(
        "POST",
        f"/api/v1/issues/{target['key']}/comments",
        json={"body": "JSON API comment #ITR-2", "author": "api-bot", "linked_attachment_ids": [att1_id]},
    )
    assert api_post.status_code == 201
    cmt4 = api_post.json()
    assert cmt4["id"] == "CMT-4"
    assert cmt4["linked_attachment_ids"] == [att1_id]

    api_patch = workspace.api(
        "PATCH",
        f"/api/v1/issues/{target['key']}/comments/CMT-4",
        json={"body": "JSON API comment updated", "actor": "api-editor"},
    )
    assert api_patch.status_code == 200
    assert api_patch.json()["edit_history"][0]["actor"] == "api-editor"


def test_02_comment_validation_exit_codes_and_spa_api_xss_safety(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify strict CLI/API error exit codes (empty -> 1, conflicting input flags -> 1,
    missing comment/issue -> 4) AND verify API JSON Content-Type + SPA XSS neutralization."""
    att_file, _ = make_text_file(tmp_path / "evidence.log", text="stacktrace line 1\n")

    iss1 = workspace.cli_json("issue", "create", "Rich XSS Safety Issue", "--attach", str(att_file))
    iss2 = workspace.cli_json("issue", "create", "Mentioned Peer Issue")
    assert iss1["key"] == "ITR-1"
    assert iss2["key"] == "ITR-2"

    # Empty comment -> exit 1 (both CLI no-arg guard and server whitespace-only guard)
    err_empty = workspace.cli_json("comment", "add", "ITR-1", expect_rc=1)
    assert err_empty["exit_code"] == 1
    assert err_empty["error"] == "empty_comment"

    err_ws_only = workspace.cli_json("comment", "add", "ITR-1", "--body", "   \n\t  ", expect_rc=1)
    assert err_ws_only["exit_code"] == 1

    # Conflicting input flags -> exit 1
    dummy_md = tmp_path / "dummy.md"
    dummy_md.write_text("hello", encoding="utf-8")

    err_conflict_add = workspace.cli_json(
        "comment",
        "add",
        "ITR-1",
        "--body",
        "inline",
        "--stdin",
        stdin="piped",
        expect_rc=1,
    )
    assert err_conflict_add["exit_code"] == 1
    assert err_conflict_add["error"] == "conflicting_input_flags"

    workspace.cli("comment", "add", "ITR-1", "--body", "inline", "--body-file", str(dummy_md), expect_rc=1)
    workspace.cli("comment", "add", "ITR-1", "--body-file", str(tmp_path / "missing.md"), expect_rc=1)

    # Nonexistent comment / issue -> exit 4
    err_missing_cmt = workspace.cli_json("comment", "show", "ITR-1", "CMT-999", expect_rc=4)
    assert err_missing_cmt["exit_code"] == 4
    workspace.cli("comment", "edit", "ITR-1", "CMT-999", "--body", "updated", expect_rc=4)
    workspace.cli("comment", "delete", "ITR-1", "CMT-999", "--yes", expect_rc=4)
    workspace.cli("comment", "list", "ITR-999", expect_rc=4)
    workspace.cli("comment", "add", "Nonexistent Spaced Issue", "--body", "hello", expect_rc=4)
    workspace.cli("comment", "add", "ITR-1", "--body", "bad link", "--link-attachment", "ATT-999", expect_rc=4)

    # Create valid comment with rich Markdown + XSS payloads, then test conflicting flags on `comment edit` -> exit 1
    rich_xss_md = (
        "# Top Heading\n"
        "## Sub Heading\n"
        "### Leaf Heading\n\n"
        "| Key | Value |\n"
        "| --- | --- |\n"
        "| lang | 🚀 日本語 café Ω |\n\n"
        "```python\n"
        "print('safe')\n"
        "```\n\n"
        "```json\n"
        '{"safe": true}\n'
        "```\n\n"
        "- [ ] Pending task\n"
        "- [x] Completed task\n\n"
        "Links: #ITR-1 #ITR-2 [Evidence Log](attachment://ATT-1) [EvilJS](javascript:alert(1))\n"
        "<script>alert(1)</script>\n"
        "<img src=x onerror=alert(1)>\n"
    )
    cmt = workspace.cli_json(
        "comment",
        "add",
        "Rich XSS Safety Issue",
        "--body",
        rich_xss_md,
        "--author",
        "sec-auditor",
    )
    assert cmt["id"] == "CMT-1"

    workspace.cli(
        "comment",
        "edit",
        "ITR-1",
        "CMT-1",
        "--body",
        "a",
        "--stdin",
        stdin="b",
        expect_rc=1,
    )

    # Verify API responses serve strictly application/json (never raw HTML) and preserve payloads verbatim
    cmts_resp = workspace.api("GET", "/api/v1/issues/ITR-1/comments")
    assert cmts_resp.status_code == 200
    assert "application/json" in cmts_resp.headers.get("content-type", "").lower()
    cmts_payload = cmts_resp.json()
    assert cmts_payload["comments"][0]["body"] == rich_xss_md

    # Verify SPA HTML escapes raw HTML entities before Markdown rendering and blocks javascript: URLs
    spa_resp = workspace.api("GET", "/")
    assert spa_resp.status_code == 200
    spa_html = spa_resp.text
    assert "<script>alert(1)</script>" not in spa_html
    assert "replace(/</g, '&lt;')" in spa_html
    assert "replace(/>/g, '&gt;')" in spa_html
    assert "low.startsWith('javascript:')" in spa_html



def test_03_description_inline_embeds_png_svg_jpg_webp_and_single_embed_deletion(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify inline embedded images across PNG, SVG, JPG, WEBP in `issue create` & `issue edit`
    covering `![Alt](embed:<fn>)`, bare `embed:<fn>`, and auto-append, plus single-embed deletion
    via `issue edit --remove-embed` preserving remaining embeds byte-identically."""
    png_path, png_sha = make_png(tmp_path / "arch_flow.png", width=24, height=24)
    svg_path, svg_sha = make_svg(tmp_path / "topology.svg", label="topo-v1")
    jpg_path, jpg_sha = make_jpg(tmp_path / "photo_shot.jpg", width=16, height=16)
    webp_path, webp_sha = make_webp(tmp_path / "badge_icon.webp", width=8, height=8)

    issue_name = "Description Embeds Matrix Issue"
    initial_desc = (
        "Architecture overview:\n"
        "![Custom Flow Alt](embed:arch_flow.png)\n"
        "Inline bare diagram: embed:topology.svg in prose."
    )

    # 1. `issue create --desc ... --embed ...` with ![Alt](embed:...), bare embed:..., and auto-append (photo_shot.jpg)
    created = workspace.cli_json(
        "issue",
        "create",
        issue_name,
        "--desc",
        initial_desc,
        "--embed",
        str(png_path),
        str(svg_path),
        str(jpg_path),
    )
    assert created["key"] == "ITR-1"
    embeds = created["description_embeds"]
    assert [e["filename"] for e in embeds] == ["arch_flow.png", "topology.svg", "photo_shot.jpg"]
    assert all(e["scope"] == "description_embed" and e["is_image"] is True for e in embeds)
    id_png, id_svg, id_jpg = embeds[0]["id"], embeds[1]["id"], embeds[2]["id"]

    assert f"![Custom Flow Alt](attachment://{id_png})" in created["description"]
    assert f"![topology.svg](attachment://{id_svg})" in created["description"]
    assert created["description"].endswith(f"\n\n![photo_shot.jpg](attachment://{id_jpg})")
    assert "embed:" not in created["description"]

    # 2. `issue edit <ref> --embed ...` (auto-append WEBP without changing --desc)
    edit_append_resp = workspace.cli_json(
        "issue",
        "edit",
        issue_name,
        "--embed",
        str(webp_path),
    )
    assert edit_append_resp["key"] == "ITR-1"
    assert len(edit_append_resp["embeds"]) == 1
    id_webp = edit_append_resp["embeds"][0]["id"]
    assert edit_append_resp["embeds"][0]["filename"] == "badge_icon.webp"

    shown = workspace.cli_json("issue", "show", "ITR-1")
    assert shown["description"].endswith(f"\n\n![badge_icon.webp](attachment://{id_webp})")
    assert len(shown["description_embeds"]) == 4

    # Verify Rich `issue show` renders Description Embeds cleanly
    shown_rich = workspace.cli("issue", "show", issue_name)
    assert "Description Embeds:" in shown_rich.stdout
    for eid in (id_png, id_svg, id_jpg, id_webp):
        assert eid in shown_rich.stdout

    # Verify on-disk files in issues/<Name>/attachments/<ATT-N>_<safe_suffix>
    att_dir = workspace.issue_dir(issue_name) / "attachments"
    by_id = {e["id"]: e for e in shown["description_embeds"]}
    for aid, expected_sha in (
        (id_png, png_sha),
        (id_svg, svg_sha),
        (id_jpg, jpg_sha),
        (id_webp, webp_sha),
    ):
        disk_file = att_dir / by_id[aid]["stored_name"]
        assert disk_file.is_file()
        assert sha256_file(disk_file) == expected_sha

    # 3. Also test `issue edit --desc ... --embed ...` on a second issue covering ![Alt](embed:...), bare embed:..., and auto-append
    second_name = "Edited Description Placeholders Issue"
    workspace.cli("issue", "create", second_name)
    workspace.cli(
        "issue",
        "edit",
        second_name,
        "--desc",
        "Updated ![WebP Badge](embed:badge_icon.webp) plus bare embed:arch_flow.png here.",
        "--embed",
        str(webp_path),
        str(png_path),
        str(svg_path),
    )
    second_shown = workspace.cli_json("issue", "show", second_name)
    sec_embeds = second_shown["description_embeds"]
    assert [e["filename"] for e in sec_embeds] == ["badge_icon.webp", "arch_flow.png", "topology.svg"]
    assert f"![WebP Badge](attachment://{sec_embeds[0]['id']})" in second_shown["description"]
    assert f"![arch_flow.png](attachment://{sec_embeds[1]['id']})" in second_shown["description"]
    assert second_shown["description"].endswith(f"\n\n![topology.svg](attachment://{sec_embeds[2]['id']})")

    # 4. Single-embed deletion via `issue edit <issue> --remove-embed <att_id>`
    svg_disk_path = att_dir / by_id[id_svg]["stored_name"]
    assert svg_disk_path.exists()

    after_del = workspace.cli_json("issue", "edit", issue_name, "--remove-embed", id_svg)
    assert [e["id"] for e in after_del["description_embeds"]] == [id_png, id_jpg, id_webp]
    assert not svg_disk_path.exists()
    assert f"attachment://{id_svg}" not in after_del["description"]
    assert "Inline bare diagram:  in prose." in after_del["description"]
    assert f"![Custom Flow Alt](attachment://{id_png})" in after_del["description"]
    assert f"![photo_shot.jpg](attachment://{id_jpg})" in after_del["description"]
    assert f"![badge_icon.webp](attachment://{id_webp})" in after_del["description"]

    # Deleted embed returns exit 4 on download & repeated remove-embed
    workspace.cli("attach", "download", "ITR-1", id_svg, "-o", str(tmp_path / "gone.svg"), expect_rc=4)
    workspace.cli("issue", "edit", "ITR-1", "--remove-embed", id_svg, expect_rc=4)

    # Remaining 3 embeds survive on disk and download with byte-exact SHA-256
    for aid, expected_sha, ext in (
        (id_png, png_sha, "png"),
        (id_jpg, jpg_sha, "jpg"),
        (id_webp, webp_sha, "webp"),
    ):
        assert (att_dir / by_id[aid]["stored_name"]).is_file()
        dl_out = tmp_path / f"dl_{aid}.{ext}"
        workspace.cli("attach", "download", issue_name, aid, "-o", str(dl_out))
        assert sha256_file(dl_out) == expected_sha


def test_04_comment_inline_embeds_png_svg_jpg_webp_and_dual_deletion_commands(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify inline embedded images (`PNG`, `SVG`, `JPG`, `WEBP`) in `comment add --embed` and
    `comment edit --add-embed` across `![Alt](embed:<fn>)`, bare `embed:<fn>`, and auto-append,
    plus single-embed deletion via BOTH `comment edit --remove-embed` and `comment embed-delete`."""
    png_path, png_sha = make_png(tmp_path / "cmt_chart.png", width=20, height=20)
    svg_path, svg_sha = make_svg(tmp_path / "cmt_seq.svg", label="seq-v2")
    jpg_path, jpg_sha = make_jpg(tmp_path / "cmt_screen.jpg", width=16, height=16)
    webp_path, webp_sha = make_webp(tmp_path / "cmt_avatar.webp", width=12, height=12)

    issue_name = "Comment Embeds Matrix Issue"
    workspace.cli_json("issue", "create", issue_name)

    # 1. `comment add --body ... --embed ...` covering ![Alt](embed:<fn>), bare embed:<fn>, and auto-append
    cmt1 = workspace.cli_json(
        "comment",
        "add",
        issue_name,
        "--body",
        "Perf summary: ![Latency Chart](embed:cmt_chart.png) and bare embed:cmt_seq.svg inline.",
        "--embed",
        str(png_path),
        str(svg_path),
        str(jpg_path),
    )
    assert cmt1["id"] == "CMT-1"
    assert [e["filename"] for e in cmt1["embeds"]] == ["cmt_chart.png", "cmt_seq.svg", "cmt_screen.jpg"]
    assert all(
        e["scope"] == "comment_embed" and e["comment_id"] == "CMT-1" and e["is_image"] is True
        for e in cmt1["embeds"]
    )
    id_png, id_svg, id_jpg = [e["id"] for e in cmt1["embeds"]]

    assert f"![Latency Chart](attachment://{id_png})" in cmt1["body"]
    assert f"![cmt_seq.svg](attachment://{id_svg})" in cmt1["body"]
    assert cmt1["body"].endswith(f"\n\n![cmt_screen.jpg](attachment://{id_jpg})")
    assert "embed:" not in cmt1["body"]

    # 2. `comment edit --add-embed` auto-appending WEBP onto CMT-1
    cmt1_after_webp = workspace.cli_json(
        "comment",
        "edit",
        "ITR-1",
        "CMT-1",
        "--add-embed",
        str(webp_path),
    )
    assert len(cmt1_after_webp["embeds"]) == 4
    id_webp = cmt1_after_webp["embeds"][3]["id"]
    assert cmt1_after_webp["body"].endswith(f"\n\n![cmt_avatar.webp](attachment://{id_webp})")

    # Also test `comment edit --body ... --add-embed ...` on CMT-2 covering ![Alt](embed:<fn>), bare embed:<fn>, and auto-append
    cmt2 = workspace.cli_json("comment", "add", "ITR-1", "--body", "Placeholder draft")
    assert cmt2["id"] == "CMT-2"
    cmt2_edited = workspace.cli_json(
        "comment",
        "edit",
        issue_name,
        "CMT-2",
        "--body",
        "Edited with ![Avatar Alt](embed:cmt_avatar.webp) and bare embed:cmt_chart.png.",
        "--add-embed",
        str(webp_path),
        str(png_path),
        str(svg_path),
    )
    c2_ids = [e["id"] for e in cmt2_edited["embeds"]]
    assert f"![Avatar Alt](attachment://{c2_ids[0]})" in cmt2_edited["body"]
    assert f"![cmt_chart.png](attachment://{c2_ids[1]})" in cmt2_edited["body"]
    assert cmt2_edited["body"].endswith(f"\n\n![cmt_seq.svg](attachment://{c2_ids[2]})")

    # Verify all 4 formats in CMT-1 exist in issues/<Name>/comments/CMT-1/files/ with exact SHA-256
    cmt1_files_dir = workspace.issue_dir(issue_name) / "comments" / "CMT-1" / "files"
    by_id = {e["id"]: e for e in cmt1_after_webp["embeds"]}
    for aid, expected_sha in (
        (id_png, png_sha),
        (id_svg, svg_sha),
        (id_jpg, jpg_sha),
        (id_webp, webp_sha),
    ):
        p = cmt1_files_dir / by_id[aid]["stored_name"]
        assert p.is_file()
        assert sha256_file(p) == expected_sha

    # 3. Delete id_png via `comment edit --remove-embed` AND id_svg via `comment embed-delete` (Rich & --json)
    after_rem1 = workspace.cli_json("comment", "edit", "ITR-1", "CMT-1", "--remove-embed", id_png)
    assert [e["id"] for e in after_rem1["embeds"]] == [id_svg, id_jpg, id_webp]
    assert not (cmt1_files_dir / by_id[id_png]["stored_name"]).exists()
    assert f"attachment://{id_png}" not in after_rem1["body"]

    rem2_rich = workspace.cli("comment", "embed-delete", issue_name, "CMT-1", id_svg)
    assert "Removed embedded image" in rem2_rich.stdout
    assert id_svg in rem2_rich.stdout
    assert not (cmt1_files_dir / by_id[id_svg]["stored_name"]).exists()

    # Also exercise `comment embed-delete --json` on CMT-2
    rem_c2_json = workspace.cli_json("comment", "embed-delete", "ITR-1", "CMT-2", c2_ids[0])
    assert [e["id"] for e in rem_c2_json["embeds"]] == c2_ids[1:]
    assert f"attachment://{c2_ids[0]}" not in rem_c2_json["body"]

    # Nonexistent embed deletion -> exit 4
    workspace.cli("comment", "embed-delete", "ITR-1", "CMT-1", "ATT-999", expect_rc=4)

    # Verify CMT-1 final state: removed tags stripped, prose intact, remaining JPG & WEBP byte-exact
    cmt1_final = workspace.cli_json("comment", "show", issue_name, "CMT-1")
    assert [e["id"] for e in cmt1_final["embeds"]] == [id_jpg, id_webp]
    assert f"attachment://{id_png}" not in cmt1_final["body"]
    assert f"attachment://{id_svg}" not in cmt1_final["body"]
    assert "Perf summary:" in cmt1_final["body"]
    assert f"![cmt_screen.jpg](attachment://{id_jpg})" in cmt1_final["body"]
    assert f"![cmt_avatar.webp](attachment://{id_webp})" in cmt1_final["body"]

    for aid, expected_sha, ext in ((id_jpg, jpg_sha, "jpg"), (id_webp, webp_sha, "webp")):
        assert (cmt1_files_dir / by_id[aid]["stored_name"]).is_file()
        dl_dest = tmp_path / f"surviving_{aid}.{ext}"
        workspace.cli("attach", "download", "ITR-1", aid, "-o", str(dl_dest))
        assert sha256_file(dl_dest) == expected_sha


def test_05_comment_scoped_non_image_attachments_matrix_and_selective_deletion(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify comment-scoped non-image attachments across PDF, ZIP, JSON, TXT, 0-byte file,
    and filenames with spaces (`crash dump final.log`), byte-exact SHA-256 downloads, and
    selective deletion via `comment edit --remove-attach` & `comment attach-delete`."""
    pdf_path, pdf_sha = make_pdf(tmp_path / "runbook.pdf", text="Incident Runbook v2")
    zip_path, zip_sha = make_zip(
        tmp_path / "diagnostics.zip",
        files={"app.log": "WARN timeout\n", "cfg.json": '{"retries": 3}'},
    )
    json_path, json_sha = make_json_file(tmp_path / "metrics.json", data={"p99_ms": 41.5, "ok": True})
    txt_path, txt_sha = make_text_file(tmp_path / "notes.txt", text="Root cause notes\nSecond line\n")
    zero_path, zero_sha = make_zero_byte_file(tmp_path / "empty_marker.dat")
    spaced_path, spaced_sha = make_text_file(
        tmp_path / "crash dump final.log",
        text="FATAL segfault at 0x0040abcd\n",
    )

    issue_name = "Comment Scoped Attachments Issue"
    workspace.cli_json("issue", "create", issue_name)

    preserved_body = "Investigation bundle attached — body text must remain completely unharmed!"

    # Upload 4 files via `comment add --attach`
    cmt = workspace.cli_json(
        "comment",
        "add",
        issue_name,
        "--body",
        preserved_body,
        "--attach",
        str(pdf_path),
        str(zip_path),
        str(json_path),
        str(spaced_path),
    )
    assert cmt["id"] == "CMT-1"
    assert len(cmt["attachments"]) == 4

    # Upload remaining 2 files (TXT + 0-byte) via `comment edit --add-attach`
    cmt = workspace.cli_json(
        "comment",
        "edit",
        "ITR-1",
        "CMT-1",
        "--add-attach",
        str(txt_path),
        str(zero_path),
    )
    assert len(cmt["attachments"]) == 6
    assert cmt["body"] == preserved_body
    assert cmt["edit_history"] == []  # Body did not change!

    expected_by_filename = {
        "runbook.pdf": pdf_sha,
        "diagnostics.zip": zip_sha,
        "metrics.json": json_sha,
        "crash dump final.log": spaced_sha,
        "notes.txt": txt_sha,
        "empty_marker.dat": zero_sha,
    }
    att_by_name = {a["filename"]: a for a in cmt["attachments"]}
    assert set(att_by_name.keys()) == set(expected_by_filename.keys())
    assert att_by_name["empty_marker.dat"]["size_bytes"] == 0

    cmt_files_dir = workspace.issue_dir(issue_name) / "comments" / "CMT-1" / "files"
    for fname, exp_sha in expected_by_filename.items():
        meta = att_by_name[fname]
        assert meta["scope"] == "comment"
        assert meta["comment_id"] == "CMT-1"
        assert meta["sha256"] == exp_sha
        disk_p = cmt_files_dir / meta["stored_name"]
        assert disk_p.is_file()
        assert sha256_file(disk_p) == exp_sha

        dl_p = tmp_path / "downloads" / f"dl_{meta['id']}_{fname}"
        workspace.cli("attach", "download", issue_name, meta["id"], "-o", str(dl_p))
        assert dl_p.is_file()
        assert sha256_file(dl_p) == exp_sha

    # Verify Rich rendering of comment attachments
    cshow_rich = workspace.cli("comment", "show", issue_name, "CMT-1")
    assert "Comment Attachments:" in cshow_rich.stdout
    assert "crash dump final.log" in cshow_rich.stdout
    assert "0B" in cshow_rich.stdout

    # Delete PDF via `comment edit --remove-attach`
    pdf_meta = att_by_name["runbook.pdf"]
    after_del1 = workspace.cli_json(
        "comment",
        "edit",
        "ITR-1",
        "CMT-1",
        "--remove-attach",
        pdf_meta["id"],
    )
    assert not (cmt_files_dir / pdf_meta["stored_name"]).exists()
    assert after_del1["body"] == preserved_body

    # Delete ZIP via `comment attach-delete` (Rich mode) & TXT via `comment attach-delete --json`
    zip_meta = att_by_name["diagnostics.zip"]
    del_zip_rich = workspace.cli("comment", "attach-delete", issue_name, "CMT-1", zip_meta["id"])
    assert "Removed comment attachment" in del_zip_rich.stdout
    assert zip_meta["id"] in del_zip_rich.stdout
    assert not (cmt_files_dir / zip_meta["stored_name"]).exists()

    txt_meta = att_by_name["notes.txt"]
    after_del3 = workspace.cli_json("comment", "attach-delete", "ITR-1", "CMT-1", txt_meta["id"])
    assert not (cmt_files_dir / txt_meta["stored_name"]).exists()
    assert after_del3["body"] == preserved_body

    # Nonexistent comment attachment deletion -> exit 4
    workspace.cli("comment", "attach-delete", "ITR-1", "CMT-1", "ATT-999", expect_rc=4)

    # Remaining 3 attachments (`metrics.json`, `empty_marker.dat`, `crash dump final.log`) survive intact!
    surviving_names = ["metrics.json", "crash dump final.log", "empty_marker.dat"]
    assert [a["filename"] for a in after_del3["attachments"]] == surviving_names
    for fname in surviving_names:
        meta = att_by_name[fname]
        assert (cmt_files_dir / meta["stored_name"]).is_file()
        assert sha256_file(cmt_files_dir / meta["stored_name"]) == expected_by_filename[fname]
    dl_verify = tmp_path / "verify_after_del" / "crash dump final.log"
    workspace.cli("attach", "download", "ITR-1", att_by_name["crash dump final.log"]["id"], "-o", str(dl_verify))
    assert sha256_file(dl_verify) == spaced_sha


def test_06_whole_issue_attachments_full_lifecycle_scopes_info_preview_and_delete(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Exercise `issue create --attach`, `attach add` (multiple files + `--by`),
    `attach list --scope issue|comments|all` (Rich & `--json` + invalid scope error),
    `attach info` (`preview_text` on text/JSON/log/SVG vs `None` on PDF/ZIP),
    `attach download -o`, and `attach delete` (missing `--yes` -> exit 1; `--yes` -> disk removal & 404)."""
    pdf_path, pdf_sha = make_pdf(tmp_path / "architecture.pdf", text="Core Spec")
    txt_path, txt_sha = make_text_file(tmp_path / "readme_notes.txt", text="Previewable line alpha\nLine beta\n")
    svg_path, svg_sha = make_svg(tmp_path / "header_diag.svg", label="hdr-svg")
    zip_path, zip_sha = make_zip(tmp_path / "bundle.zip")
    json_path, json_sha = make_json_file(tmp_path / "schema.json", data={"schema_v": 2, "strict": True})
    log_path, log_sha = make_text_file(tmp_path / "server trace.log", text="INFO server started\nWARN slow rpc\n")
    cmt_png_path, _ = make_png(tmp_path / "cmt_inline.png", width=12, height=12)
    cmt_txt_path, _ = make_text_file(tmp_path / "cmt_only.txt", text="Comment scoped text\n")

    issue_name = "Whole Issue Attachments Lifecycle"
    created = workspace.cli_json(
        "issue",
        "create",
        issue_name,
        "--desc",
        "Header diagram: ![Header](embed:header_diag.svg)",
        "--embed",
        str(svg_path),
        "--attach",
        str(pdf_path),
        str(txt_path),
    )
    assert created["key"] == "ITR-1"
    id_svg = created["description_embeds"][0]["id"]
    id_pdf, id_txt = created["attachments"][0]["id"], created["attachments"][1]["id"]

    # `attach add` with multiple files + `--by` (both `--json` and Rich)
    added_json = workspace.cli_json(
        "attach",
        "add",
        issue_name,
        str(zip_path),
        str(json_path),
        "--by",
        "release-bot",
    )
    assert added_json["key"] == "ITR-1"
    assert len(added_json["attachments"]) == 2
    assert all(a["uploaded_by"] == "release-bot" and a["scope"] == "issue" for a in added_json["attachments"])
    id_zip, id_json = added_json["attachments"][0]["id"], added_json["attachments"][1]["id"]

    add_rich = workspace.cli("attach", "add", "ITR-1", str(log_path), "--by", "ops-user")
    assert "Uploaded 1 attachment(s)" in add_rich.stdout

    # Add comment embed + comment attachment to verify scope partitioning
    cmt = workspace.cli_json(
        "comment",
        "add",
        "ITR-1",
        "--body",
        "Inline embed:cmt_inline.png",
        "--embed",
        str(cmt_png_path),
        "--attach",
        str(cmt_txt_path),
    )
    id_cmt_png = cmt["embeds"][0]["id"]
    id_cmt_txt = cmt["attachments"][0]["id"]

    # `attach list --scope issue|comments|all` in `--json` & Rich
    list_issue = workspace.cli_json("attach", "list", issue_name, "--scope", "issue")
    assert list_issue["scope"] == "issue"
    issue_scope_ids = [a["id"] for a in list_issue["attachments"]]
    id_log = [a["id"] for a in list_issue["attachments"] if a["filename"] == "server trace.log"][0]
    assert set(issue_scope_ids) == {id_pdf, id_txt, id_zip, id_json, id_log, id_svg}
    assert all(a["scope"] in ("issue", "description_embed") for a in list_issue["attachments"])

    list_comments = workspace.cli_json("attach", "list", "ITR-1", "--scope", "comments")
    assert list_comments["scope"] == "comments"
    assert {a["id"] for a in list_comments["attachments"]} == {id_cmt_png, id_cmt_txt}
    assert all(a["scope"] in ("comment_embed", "comment") for a in list_comments["attachments"])

    list_all = workspace.cli_json("attach", "list", issue_name, "--scope", "all")
    assert list_all["scope"] == "all"
    assert {a["id"] for a in list_all["attachments"]} == {
        id_pdf,
        id_txt,
        id_zip,
        id_json,
        id_log,
        id_svg,
        id_cmt_png,
        id_cmt_txt,
    }

    for sc in ("issue", "comments", "all"):
        rich_list = workspace.cli("attach", "list", "ITR-1", "--scope", sc)
        assert f"scope={sc}" in rich_list.stdout

    # Invalid scope -> CLI exit 1 & API HTTP 400
    workspace.cli("attach", "list", "ITR-1", "--scope", "invalid-scope", expect_rc=1)
    bad_scope_resp = workspace.api("GET", "/api/v1/issues/ITR-1/attachments", params={"scope": "invalid-scope"})
    assert bad_scope_resp.status_code == 400

    # `attach info` (Rich & `--json`): preview_text populated for text/JSON/log/SVG, None for PDF/ZIP
    info_txt_json = workspace.cli_json("attach", "info", issue_name, id_txt)
    assert info_txt_json["id"] == id_txt
    assert info_txt_json["issue_key"] == "ITR-1"
    assert info_txt_json["exists_on_disk"] is True
    assert info_txt_json["sha256"] == txt_sha
    assert "Previewable line alpha" in (info_txt_json["preview_text"] or "")

    info_log_rich = workspace.cli("attach", "info", "ITR-1", id_log)
    assert "Preview:" in info_log_rich.stdout
    assert "WARN slow rpc" in info_log_rich.stdout

    for aid, expected_snippet, expected_sha in (
        (id_txt, "Previewable line alpha", txt_sha),
        (id_json, '"schema_v": 2', json_sha),
        (id_log, "WARN slow rpc", log_sha),
        (id_svg, "hdr-svg", svg_sha),
    ):
        resp_info = workspace.api("GET", f"/api/v1/issues/ITR-1/attachments/{aid}")
        assert resp_info.status_code == 200
        info = resp_info.json()
        assert info["exists_on_disk"] is True
        assert info["sha256"] == expected_sha
        assert isinstance(info["preview_text"], str) and expected_snippet in info["preview_text"]

    info_pdf_json = workspace.cli_json("attach", "info", "ITR-1", id_pdf)
    assert info_pdf_json["exists_on_disk"] is True
    assert info_pdf_json["sha256"] == pdf_sha
    assert info_pdf_json["preview_text"] is None

    info_zip_rich = workspace.cli("attach", "info", issue_name, id_zip)
    assert id_zip in info_zip_rich.stdout
    assert "Preview:" not in info_zip_rich.stdout
    resp_zip_info = workspace.api("GET", f"/api/v1/issues/ITR-1/attachments/{id_zip}").json()
    assert resp_zip_info["exists_on_disk"] is True
    assert resp_zip_info["sha256"] == zip_sha
    assert resp_zip_info["preview_text"] is None

    # `attach download -o` (Rich & `--json`) byte-exact SHA-256
    for aid, expected_sha, fname in (
        (id_pdf, pdf_sha, "architecture.pdf"),
        (id_txt, txt_sha, "readme_notes.txt"),
        (id_zip, zip_sha, "bundle.zip"),
        (id_json, json_sha, "schema.json"),
        (id_log, log_sha, "server trace.log"),
    ):
        dest = tmp_path / "whole_dl" / fname
        if aid == id_pdf:
            dl_json = workspace.cli_json("attach", "download", issue_name, aid, "-o", str(dest))
            assert dl_json["id"] == aid
            assert dl_json["size_bytes"] == pdf_path.stat().st_size
        else:
            dl_rich = workspace.cli("attach", "download", "ITR-1", aid, "--output", str(dest))
            assert "Downloaded" in dl_rich.stdout
        assert sha256_file(dest) == expected_sha

    # `attach delete`: missing `--yes` -> exit 1; with `--yes` -> deleted from disk & `attach info` -> exit 4
    att_dir = workspace.issue_dir(issue_name) / "attachments"
    zip_stored = [a["stored_name"] for a in list_issue["attachments"] if a["id"] == id_zip][0]
    pdf_stored = [a["stored_name"] for a in list_issue["attachments"] if a["id"] == id_pdf][0]

    err_no_yes = workspace.cli_json("attach", "delete", "ITR-1", id_zip, expect_rc=1)
    assert err_no_yes["exit_code"] == 1
    assert err_no_yes["error"] == "confirmation_required"
    assert (att_dir / zip_stored).is_file()

    del_zip_json = workspace.cli_json("attach", "delete", issue_name, id_zip, "--yes")
    assert del_zip_json == {"deleted": True, "id": id_zip, "filename": "bundle.zip", "scope": "issue"}
    assert not (att_dir / zip_stored).exists()
    workspace.cli("attach", "info", "ITR-1", id_zip, expect_rc=4)
    workspace.cli("attach", "download", "ITR-1", id_zip, "-o", str(tmp_path / "nope.zip"), expect_rc=4)

    del_pdf_rich = workspace.cli("attach", "delete", "ITR-1", id_pdf, "--yes")
    assert f"Deleted attachment `{id_pdf}`." in del_pdf_rich.stdout
    assert not (att_dir / pdf_stored).exists()
    workspace.cli("attach", "info", issue_name, id_pdf, expect_rc=4)

    shown_after = workspace.cli_json("issue", "show", "ITR-1")
    assert id_zip in shown_after["deleted_attachments"]
    assert shown_after["deleted_attachments"][id_zip]["filename"] == "bundle.zip"
    assert id_pdf in shown_after["deleted_attachments"]


def test_07_cross_linking_issue_attachments_zero_duplication_unlink_and_deleted_tombstones(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify cross-linking whole-issue attachments inside comments (`comment add --link-attachment`
    and `comment edit --link-attachment`), zero file duplication in `comments/<CMT>/files/`,
    unlinking (`--unlink-attachment`) without harming whole-issue files, and graceful tombstone
    reporting across `comment list`, `comment show`, `issue show`, and `issue context` (Rich & `--json`)."""
    pdf_path, pdf_sha = make_pdf(tmp_path / "contract_v1.pdf", text="Master Service Agreement")
    txt_path, txt_sha = make_text_file(tmp_path / "audit report.txt", text="Audit status: PASS\n")

    issue_name = "Cross Link Attachment Issue"
    workspace.cli_json("issue", "create", issue_name)
    added = workspace.cli_json("attach", "add", issue_name, str(pdf_path), str(txt_path))
    att1_id, att2_id = added["attachments"][0]["id"], added["attachments"][1]["id"]
    att1_stored = added["attachments"][0]["stored_name"]
    att2_stored = added["attachments"][1]["stored_name"]

    # 1. Link ATT-1 via `comment add --link-attachment` (and ATT-2 via link-only comment without --body)
    cmt1 = workspace.cli_json(
        "comment",
        "add",
        issue_name,
        "--body",
        "Please review the signed agreement.",
        "--link-attachment",
        att1_id,
    )
    assert cmt1["id"] == "CMT-1"
    assert cmt1["linked_attachment_ids"] == [att1_id]
    assert len(cmt1["linked_attachments"]) == 1
    assert cmt1["linked_attachments"][0]["id"] == att1_id
    assert cmt1["linked_attachments"][0]["linked"] is True
    assert cmt1["linked_attachments"][0]["deleted"] is False

    cmt2 = workspace.cli_json("comment", "add", "ITR-1", "--link-attachment", att2_id)
    assert cmt2["id"] == "CMT-2"
    assert cmt2["linked_attachment_ids"] == [att2_id]

    # 2. Link ATT-2 onto CMT-1 via `comment edit --link-attachment`
    cmt1 = workspace.cli_json("comment", "edit", "ITR-1", "CMT-1", "--link-attachment", att2_id)
    assert cmt1["linked_attachment_ids"] == [att1_id, att2_id]

    # 3. Verify on disk: `comments/<CMT>/files/` has ZERO duplicated copies of whole-issue attachments!
    issue_dir = workspace.issue_dir(issue_name)
    for cid in ("CMT-1", "CMT-2"):
        c_files = issue_dir / "comments" / cid / "files"
        assert not c_files.exists() or list(c_files.iterdir()) == []

    att1_disk = issue_dir / "attachments" / att1_stored
    att2_disk = issue_dir / "attachments" / att2_stored
    assert att1_disk.is_file() and sha256_file(att1_disk) == pdf_sha
    assert att2_disk.is_file() and sha256_file(att2_disk) == txt_sha

    # 4. Unlink ATT-1 via `comment edit --unlink-attachment` -> removed from comment, whole-issue file intact!
    cmt1_unlinked = workspace.cli_json("comment", "edit", issue_name, "CMT-1", "--unlink-attachment", att1_id)
    assert cmt1_unlinked["linked_attachment_ids"] == [att2_id]
    assert [la["id"] for la in cmt1_unlinked["linked_attachments"]] == [att2_id]
    assert att1_disk.is_file() and sha256_file(att1_disk) == pdf_sha
    dl_after_unlink = tmp_path / "after_unlink.pdf"
    workspace.cli("attach", "download", "ITR-1", att1_id, "-o", str(dl_after_unlink))
    assert sha256_file(dl_after_unlink) == pdf_sha

    # 5. Re-link ATT-1, then delete whole-issue attachment `attach delete ITR-1 ATT-1 --yes`
    cmt1_relinked = workspace.cli_json("comment", "edit", "ITR-1", "CMT-1", "--link-attachment", att1_id)
    assert cmt1_relinked["linked_attachment_ids"] == [att2_id, att1_id]

    workspace.cli_json("attach", "delete", issue_name, att1_id, "--yes")
    assert not att1_disk.exists()
    assert att2_disk.is_file() and sha256_file(att2_disk) == txt_sha

    # Verify tombstone in `--json` across `comment list`, `comment show`, `issue show`, `issue context`
    def _assert_linked_tombstone(linked_list: list[dict]) -> None:
        by_lid = {item["id"]: item for item in linked_list}
        assert by_lid[att1_id]["linked"] is True
        assert by_lid[att1_id]["deleted"] is True
        assert by_lid[att1_id]["filename"] == "contract_v1.pdf"
        assert by_lid[att1_id].get("deleted_at")
        assert by_lid[att2_id]["linked"] is True
        assert by_lid[att2_id]["deleted"] is False
        assert by_lid[att2_id]["filename"] == "audit report.txt"

    clist_json = workspace.cli_json("comment", "list", issue_name)
    _assert_linked_tombstone(clist_json["comments"][0]["linked_attachments"])

    cshow_json = workspace.cli_json("comment", "show", "ITR-1", "CMT-1")
    _assert_linked_tombstone(cshow_json["linked_attachments"])

    ishow_json = workspace.cli_json("issue", "show", issue_name)
    assert ishow_json["deleted_attachments"][att1_id]["filename"] == "contract_v1.pdf"
    _assert_linked_tombstone(ishow_json["comments"][0]["linked_attachments"])

    ictx_json = workspace.cli_json("issue", "context", "ITR-1")
    _assert_linked_tombstone(ictx_json["context"]["issue"]["comments"][0]["linked_attachments"])
    assert f"- Linked attachment: `{att1_id}` `contract_v1.pdf` [DELETED]" in ictx_json["markdown"]
    assert f"- Linked attachment: `{att2_id}` `audit report.txt`" in ictx_json["markdown"]

    # Verify Rich text mode across `comment list`, `comment show`, `issue show`, `issue context` does not crash
    for cmd_args in (
        ("comment", "list", "ITR-1"),
        ("comment", "show", issue_name, "CMT-1"),
        ("issue", "show", "ITR-1"),
    ):
        res_rich = workspace.cli(*cmd_args)
        assert f"{att2_id} (audit report.txt)" in res_rich.stdout

    ictx_rich = workspace.cli("issue", "context", issue_name)
    assert att1_id in ictx_rich.stdout
    assert "contract_v1.pdf" in ictx_rich.stdout
    assert "[DELETED]" in ictx_rich.stdout


def test_09_rich_tui_deleted_linked_attachment_tombstone_text_preserved(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Focused test for Rich TUI deleted-attachment tombstone rendering in `comment list`,
    `comment show`, and `issue show`: `[deleted attachment: ATT-1 (...)]` must be visible in
    Rich stdout and not swallowed as an unescaped Rich BBCode style tag."""
    pdf_path, _ = make_pdf(tmp_path / "contract_v1.pdf", text="Master Service Agreement")
    issue_name = "Rich Tombstone Escaping Issue"
    workspace.cli_json("issue", "create", issue_name)
    added = workspace.cli_json("attach", "add", issue_name, str(pdf_path))
    att_id = added["attachments"][0]["id"]

    workspace.cli_json(
        "comment",
        "add",
        issue_name,
        "--body",
        "Linked attachment tombstone check.",
        "--link-attachment",
        att_id,
    )
    workspace.cli_json("attach", "delete", issue_name, att_id, "--yes")

    expected_rich_tombstone = f"[deleted attachment: {att_id} (contract_v1.pdf)]"
    for cmd_args in (
        ("comment", "list", "ITR-1"),
        ("comment", "show", issue_name, "CMT-1"),
        ("issue", "show", "ITR-1"),
    ):
        res_rich = workspace.cli(*cmd_args)
        assert expected_rich_tombstone in res_rich.stdout



def test_08_full_comment_edit_history_fts5_purge_and_comment_delete_isolation(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify multi-revision `edit_history` (`previous_body`, `actor`, `edited_at`), FTS5 indexing
    of comment body & comment attachment/embed filenames, `comment delete` confirmation guard,
    complete disk + FTS5 purge upon `comment delete --yes`, and 100% survival of linked whole-issue attachments."""
    issue_name = "Comment History And Purge Issue"
    workspace.cli_json("issue", "create", issue_name)
    workspace.cli_json("issue", "create", "Unrelated Control Issue")

    shared_pdf, shared_pdf_sha = make_pdf(tmp_path / "shared_blueprint.pdf", text="Blueprint v1")
    att_res = workspace.cli_json("attach", "add", issue_name, str(shared_pdf))
    issue_att_id = att_res["attachments"][0]["id"]
    issue_att_stored = att_res["attachments"][0]["stored_name"]
    issue_att_disk = workspace.issue_dir(issue_name) / "attachments" / issue_att_stored

    cmt_embed_path, _ = make_png(tmp_path / "nebulaflamegraph771.png", width=16, height=16)
    cmt_att_path, _ = make_text_file(
        tmp_path / "zephyrcrashlog992.txt",
        text="Comment owned log content\n",
    )

    cmt1 = workspace.cli_json(
        "comment",
        "add",
        issue_name,
        "--body",
        "Revision zero token revzero111",
        "--author",
        "alice",
        "--embed",
        str(cmt_embed_path),
        "--attach",
        str(cmt_att_path),
        "--link-attachment",
        issue_att_id,
    )
    assert cmt1["id"] == "CMT-1"
    emb_id = cmt1["embeds"][0]["id"]
    c_att_id = cmt1["attachments"][0]["id"]
    rev0_body = cmt1["body"]
    assert f"![nebulaflamegraph771.png](attachment://{emb_id})" in rev0_body

    # Sequential edits across `--body`, `--body-file`, and `--stdin` with distinct actors
    rev1_body = f"Revision one token revone222\n\n![nebulaflamegraph771.png](attachment://{emb_id})"
    workspace.cli_json("comment", "edit", "ITR-1", "CMT-1", "--body", rev1_body, "--actor", "bob")

    rev2_file = tmp_path / "rev2.md"
    rev2_body = f"Revision two token revtwo333\n\n![nebulaflamegraph771.png](attachment://{emb_id})"
    rev2_file.write_text(rev2_body, encoding="utf-8")
    workspace.cli_json("comment", "edit", issue_name, "CMT-1", "--body-file", str(rev2_file), "--actor", "carol")

    rev3_body = f"Revision three final token quasarbodytoken883\n\n![nebulaflamegraph771.png](attachment://{emb_id})"
    cmt1_final = workspace.cli_json(
        "comment",
        "edit",
        "ITR-1",
        "CMT-1",
        "--stdin",
        "--actor",
        "dave",
        stdin=rev3_body,
    )

    # Assert chronological edit_history accuracy
    hist = cmt1_final["edit_history"]
    assert len(hist) == 3
    assert hist[0]["actor"] == "bob"
    assert hist[0]["previous_body"] == rev0_body
    assert hist[0]["edited_at"].endswith("Z")

    assert hist[1]["actor"] == "carol"
    assert hist[1]["previous_body"] == rev1_body
    assert hist[1]["edited_at"].endswith("Z")

    assert hist[2]["actor"] == "dave"
    assert hist[2]["previous_body"] == rev2_body
    assert hist[2]["edited_at"].endswith("Z")

    rich_show = workspace.cli("comment", "show", issue_name, "CMT-1")
    assert "(edited 3x)" in rich_show.stdout

    # Verify FTS5 search matches current comment body & comment attachment/embed filenames (and NOT overwritten revzero111)
    for tok in ("quasarbodytoken883", "zephyrcrashlog992", "nebulaflamegraph771"):
        srch = workspace.cli_json("search", tok)
        assert srch["total"] == 1, f"Expected FTS5 match for {tok}, got {srch}"
        assert srch["issues"][0]["key"] == "ITR-1"

    stale_srch = workspace.cli_json("search", "revzero111")
    assert stale_srch["total"] == 0

    # Verify on-disk comment directory & owned files exist prior to deletion
    cmt1_dir = workspace.issue_dir(issue_name) / "comments" / "CMT-1"
    assert (cmt1_dir / "comment.json").is_file()
    assert len(list((cmt1_dir / "files").iterdir())) == 2

    # `comment delete` without `--yes` -> exit 1 & nothing deleted
    del_err = workspace.cli_json("comment", "delete", "ITR-1", "CMT-1", expect_rc=1)
    assert del_err["exit_code"] == 1
    assert del_err["error"] == "confirmation_required"
    assert (cmt1_dir / "comment.json").is_file()

    # Create a quick CMT-2 as well so we verify `comment delete --yes` in Rich mode too
    cmt2 = workspace.cli_json("comment", "add", "ITR-1", "--body", "Ephemeral second comment")
    del_c2_rich = workspace.cli("comment", "delete", issue_name, cmt2["id"], "--yes")
    assert f"Deleted comment `{cmt2['id']}`." in del_c2_rich.stdout

    # Delete CMT-1 permanently with `--yes`
    del_c1_json = workspace.cli_json("comment", "delete", issue_name, "CMT-1", "--yes")
    assert del_c1_json == {"deleted": True, "id": "CMT-1", "issue_key": "ITR-1"}

    # 1. `comments/CMT-1/` directory and all owned files completely purged from disk
    assert not cmt1_dir.exists()
    workspace.cli("comment", "show", "ITR-1", "CMT-1", expect_rc=4)
    workspace.cli("attach", "info", "ITR-1", emb_id, expect_rc=4)
    workspace.cli("attach", "info", "ITR-1", c_att_id, expect_rc=4)

    # 2. FTS5 `search` immediately returns 0 matches for comment body, attachment, and embed tokens
    for tok in ("quasarbodytoken883", "zephyrcrashlog992", "nebulaflamegraph771"):
        post_srch = workspace.cli_json("search", tok)
        assert post_srch["total"] == 0
        assert post_srch["issues"] == []

    # 3. Linked whole-issue attachment in `issues/<Name>/attachments/` remains 100% intact with exact SHA-256!
    assert issue_att_disk.is_file()
    assert sha256_file(issue_att_disk) == shared_pdf_sha
    dl_shared = tmp_path / "verify_shared_blueprint.pdf"
    workspace.cli("attach", "download", issue_name, issue_att_id, "-o", str(dl_shared))
    assert sha256_file(dl_shared) == shared_pdf_sha

    assert_clean_repo_root()
