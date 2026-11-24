"""Suite 03: Atomic Folder Renames With Spaces, Chained & Case-Only Moves, and Collision Rollback."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from helpers import (
    ITrackWorkspace,
    assert_clean_repo_root,
    make_pdf,
    make_png,
    make_svg,
    make_text_file,
    make_zip,
    sha256_file,
)


def _read_issue_json(ws: ITrackWorkspace, folder_name: str) -> dict[str, Any]:
    issue_json_path = ws.issue_dir(folder_name) / "issue.json"
    assert issue_json_path.is_file(), f"Expected issue.json at {issue_json_path}"
    data = json.loads(issue_json_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _read_history_jsonl(ws: ITrackWorkspace, folder_name: str) -> list[dict[str, Any]]:
    hist_path = ws.issue_dir(folder_name) / "history.jsonl"
    assert hist_path.is_file(), f"Expected history.jsonl at {hist_path}"
    raw_lines = hist_path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) > 0, f"Expected non-empty history.jsonl at {hist_path}"
    parsed: list[dict[str, Any]] = []
    for idx, line in enumerate(raw_lines, 1):
        assert line.strip(), f"Unexpected blank line {idx} in {hist_path}"
        obj = json.loads(line)
        assert isinstance(obj, dict), f"Line {idx} in {hist_path} did not decode to a JSON object"
        parsed.append(obj)
    return parsed


def _assert_no_rename_tmp_dirs(ws: ITrackWorkspace) -> None:
    tmp_entries = [name for name in os.listdir(ws.issues_dir) if name.startswith(".rename_tmp_")]
    assert tmp_entries == [], f"Found stray .rename_tmp_* entries in {ws.issues_dir}: {tmp_entries}"


def test_01_heavily_populated_issue_rename_preserves_all_state_files_links_fts_and_history(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Rename a heavily populated issue ('Initial Bug' -> 'OAuth2 Token Refresh Race Condition')
    and verify atomic folder move, immutable ITR-N key, parent/subtask hierarchy, bidirectional
    peer target_name updates on disk & API, byte-for-byte SHA-256 survival of all 4 files,
    immediate FTS5 re-indexing, and valid history.jsonl audit logging."""
    ws = workspace

    # 1. Create peer issues (strictly avoiding the word "Initial" anywhere so FTS5 "Initial" tests old title only)
    downstream = ws.cli_json(
        "issue",
        "create",
        "Downstream API Gateway",
        "--category",
        "feature",
        "--priority",
        "high",
        "--desc",
        "Edge proxy service awaiting upstream token fix",
    )
    epic = ws.cli_json(
        "issue",
        "create",
        "Auth Audit Epic",
        "--category",
        "epic",
        "--priority",
        "high",
        "--desc",
        "Umbrella security review for session rotation",
    )
    downstream_key = downstream["key"]
    epic_key = epic["key"]

    # 2. Generate synthetic assets with recorded SHA-256 digests
    assets_dir = tmp_path / "assets"
    png_path, png_sha = make_png(assets_dir / "flamegraph.png", width=48, height=32, color=(37, 99, 235))
    pdf_path, pdf_sha = make_pdf(assets_dir / "incident report.pdf", text="OAuth trace")
    svg_path, svg_sha = make_svg(assets_dir / "sequence diagram.svg", label="oauth-refresh", width=140, height=70)
    zip_path, zip_sha = make_zip(
        assets_dir / "debug logs.zip",
        files={"worker.log": "401 concurrent refresh retry\n", "summary.json": '{"retries": 3}'},
    )

    # 3. Create "Initial Bug" with rich Markdown description + inline embedded PNG
    created = ws.cli_json(
        "issue",
        "create",
        "Initial Bug",
        "--category",
        "bug",
        "--priority",
        "high",
        "--severity",
        "sev1",
        "--desc",
        "Race under load\n\n![flame](embed:flamegraph.png)",
        "--embed",
        str(png_path),
    )
    orig_key = created["key"]
    assert orig_key.startswith("ITR-")
    assert len(created["description_embeds"]) == 1
    png_att_id = created["description_embeds"][0]["id"]
    assert created["description_embeds"][0]["sha256"] == png_sha

    # Whole-issue PDF attachment
    att_added = ws.cli_json("attach", "add", "Initial Bug", str(pdf_path))
    assert len(att_added["attachments"]) == 1
    pdf_att_id = att_added["attachments"][0]["id"]
    assert att_added["attachments"][0]["sha256"] == pdf_sha

    # Rich comment with embedded SVG AND comment-scoped ZIP attachment
    cmt_added = ws.cli_json(
        "comment",
        "add",
        "Initial Bug",
        "--body",
        "Captured SVG diagram and logs\n\n![seq](embed:sequence diagram.svg)",
        "--embed",
        str(svg_path),
        "--attach",
        str(zip_path),
    )
    assert len(cmt_added["embeds"]) == 1
    assert len(cmt_added["attachments"]) == 1
    svg_att_id = cmt_added["embeds"][0]["id"]
    zip_att_id = cmt_added["attachments"][0]["id"]
    assert cmt_added["embeds"][0]["sha256"] == svg_sha
    assert cmt_added["attachments"][0]["sha256"] == zip_sha

    # Child subtask
    subtask = ws.cli_json(
        "issue",
        "create",
        "Refresh Mutex Unit Tests",
        "--category",
        "subtask",
        "--parent",
        "Initial Bug",
    )
    subtask_key = subtask["key"]
    assert subtask["parent"] == orig_key

    # Outbound `blocks` link & inbound `relates-to` link
    ws.cli("link", "add", "Initial Bug", "blocks", "Downstream API Gateway")
    ws.cli_json("link", "add", "Auth Audit Epic", "relates-to", "Initial Bug")

    # Worklog (1h30m == 90 minutes)
    ws.cli("worklog", "add", "Initial Bug", "1h30m", "--note", "Analyzed token refresh concurrency")

    # 4. Pre-rename FTS5 verification for "Initial"
    pre_search = ws.cli_json("search", "Initial")
    assert pre_search["total"] == 1
    assert [i["key"] for i in pre_search["issues"]] == [orig_key]
    assert (ws.issues_dir / "Initial Bug").is_dir()

    # 5. Execute rename (exercising human Rich output here)
    new_title = "OAuth2 Token Refresh Race Condition"
    ren_res = ws.cli("issue", "rename", "Initial Bug", new_title)
    assert orig_key in ren_res.stdout
    assert new_title in ren_res.stdout

    # --- Post-rename Verification ---

    # (a) Old folder gone & new folder exists with zero .rename_tmp_* leftovers
    assert not (ws.issues_dir / "Initial Bug").exists()
    new_folder_path = ws.issues_dir / new_title
    assert new_folder_path.is_dir()
    assert (new_folder_path / "issue.json").is_file()
    _assert_no_rename_tmp_dirs(ws)

    raw_renamed = _read_issue_json(ws, new_title)
    assert raw_renamed["key"] == orig_key
    assert raw_renamed["name"] == new_title
    assert raw_renamed["folder"] == new_title

    # (b) Immutable ITR-N key & dual addressing (both key and new folder name work; old name exits 4)
    shown_by_key = ws.cli_json("issue", "show", orig_key)
    shown_by_name = ws.cli_json("issue", "show", new_title)
    shown_human = ws.cli("issue", "show", new_title)
    assert new_title in shown_human.stdout

    for doc in (shown_by_key, shown_by_name):
        assert doc["key"] == orig_key
        assert doc["name"] == new_title
        assert doc["folder"] == new_title
        assert doc["logged_minutes"] == 90

    ws.cli("issue", "show", "Initial Bug", expect_rc=4)
    old_err = ws.cli_json("issue", "show", "Initial Bug", expect_rc=4)
    assert old_err["exit_code"] == 4
    assert old_err["error"] == "not_found"

    # (c) Parent/subtask & bidirectional issue.json link target_name updates
    sub_after = ws.cli_json("issue", "show", "Refresh Mutex Unit Tests")
    assert sub_after["key"] == subtask_key
    assert sub_after["parent"] == orig_key
    assert [s["name"] for s in shown_by_name["subtasks"]] == ["Refresh Mutex Unit Tests"]

    tree_json = ws.cli_json("tree", new_title)
    assert tree_json["root"] == orig_key
    assert len(tree_json["trees"]) == 1
    tree_root = tree_json["trees"][0]
    assert tree_root["key"] == orig_key
    assert tree_root["name"] == new_title
    assert [ch["name"] for ch in tree_root["children"]] == ["Refresh Mutex Unit Tests"]
    tree_human = ws.cli("tree", new_title)
    assert "Issue Hierarchy & Dependency Tree" in tree_human.stdout

    # Inspect BOTH raw on-disk issue.json AND `issue show --json` for "Downstream API Gateway"
    raw_ds = _read_issue_json(ws, "Downstream API Gateway")
    cli_ds = ws.cli_json("issue", "show", "Downstream API Gateway")
    for source_label, ds_doc in (("disk", raw_ds), ("cli", cli_ds)):
        matching = [lnk for lnk in ds_doc["links"] if lnk["target_key"] == orig_key]
        assert len(matching) == 1, f"Expected 1 link to {orig_key} in {source_label} Downstream API Gateway"
        assert matching[0]["type"] == "blocked-by"
        assert matching[0]["direction"] == "inbound"
        assert matching[0]["target_name"] == new_title
        assert matching[0]["target_name"] != "Initial Bug"

    # Inspect BOTH raw on-disk issue.json AND `issue show --json` for "Auth Audit Epic"
    raw_ep = _read_issue_json(ws, "Auth Audit Epic")
    cli_ep = ws.cli_json("issue", "show", "Auth Audit Epic")
    for source_label, ep_doc in (("disk", raw_ep), ("cli", cli_ep)):
        matching = [lnk for lnk in ep_doc["links"] if lnk["target_key"] == orig_key]
        assert len(matching) == 1, f"Expected 1 link to {orig_key} in {source_label} Auth Audit Epic"
        assert matching[0]["type"] == "relates-to"
        assert matching[0]["target_name"] == new_title
        assert matching[0]["target_name"] != "Initial Bug"

    # Renamed issue's own links still contain both links intact
    own_links = {(lnk["type"], lnk["target_key"], lnk["target_name"]) for lnk in shown_by_name["links"]}
    assert ("blocks", downstream_key, "Downstream API Gateway") in own_links
    assert ("relates-to", epic_key, "Auth Audit Epic") in own_links

    # (d) Byte-for-byte SHA-256 survival of ALL 4 embedded images & attachments
    att_list = ws.cli_json("attach", "list", new_title, "--scope", "all")
    assert att_list["key"] == orig_key
    assert att_list["scope"] == "all"
    listed_ids = {item["id"] for item in att_list["attachments"]}
    assert listed_ids == {png_att_id, pdf_att_id, svg_att_id, zip_att_id}

    dl_dir = tmp_path / "downloads"
    expected_files = [
        (png_att_id, "dl_flamegraph.png", png_sha, True),
        (pdf_att_id, "dl_incident_report.pdf", pdf_sha, False),
        (svg_att_id, "dl_sequence_diagram.svg", svg_sha, True),
        (zip_att_id, "dl_debug_logs.zip", zip_sha, False),
    ]
    for att_id, out_name, expected_sha, use_json in expected_files:
        out_file = dl_dir / out_name
        if use_json:
            dl_meta = ws.cli_json("attach", "download", new_title, att_id, "-o", str(out_file))
            assert dl_meta["id"] == att_id
            assert dl_meta["size_bytes"] == out_file.stat().st_size
        else:
            ws.cli("attach", "download", new_title, att_id, "-o", str(out_file))
        assert out_file.is_file()
        assert sha256_file(out_file) == expected_sha

    # (e) Immediate FTS5 update
    fts_new = ws.cli_json("search", "OAuth2 Token Refresh")
    assert fts_new["total"] == 1
    assert [i["key"] for i in fts_new["issues"]] == [orig_key]

    fts_old_word = ws.cli_json("search", "Initial")
    assert fts_old_word["total"] == 0
    assert fts_old_word["issues"] == []

    fts_old_phrase = ws.cli_json("search", "Initial Bug")
    assert fts_old_phrase["total"] == 0
    assert fts_old_phrase["issues"] == []

    fts_comment = ws.cli_json("search", "Captured SVG diagram")
    assert fts_comment["total"] == 1
    assert [i["key"] for i in fts_comment["issues"]] == [orig_key]

    fts_att = ws.cli_json("search", "incident report.pdf")
    assert fts_att["total"] == 1
    assert [i["key"] for i in fts_att["issues"]] == [orig_key]

    # (f) history.jsonl valid JSONL rename audit entry
    disk_history = _read_history_jsonl(ws, new_title)
    cli_history = ws.cli_json("history", new_title)
    ws.cli("history", new_title)

    assert cli_history["key"] == orig_key
    assert cli_history["name"] == new_title
    assert cli_history["history"] == disk_history

    rename_events = [ev for ev in disk_history if ev.get("action") == "renamed"]
    assert len(rename_events) == 1
    assert {
        "field": "name",
        "old": "Initial Bug",
        "new": new_title,
    } in rename_events[0]["diffs"]


def test_02_rename_via_issue_edit_standalone_and_combined_with_metadata_and_key_rename(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify `issue edit --name` both combined with metadata edits (--priority, --state)
    and standalone, plus `issue rename` addressed by ITR-N key with `--json` output."""
    ws = workspace

    peer = ws.cli_json("issue", "create", "Billing Ledger Reconciliation", "--priority", "medium")
    peer_key = peer["key"]

    txt_path, txt_sha = make_text_file(
        tmp_path / "runbook notes.txt",
        text="Idempotency key rotation steps\nVerify ledger locks\n",
    )

    old_name = "Legacy Payment Retry"
    created = ws.cli_json(
        "issue",
        "create",
        old_name,
        "--category",
        "task",
        "--priority",
        "low",
        "--state",
        "todo",
        "--attach",
        str(txt_path),
    )
    orig_key = created["key"]
    att_id = created["attachments"][0]["id"]
    ws.cli_json("link", "add", old_name, "blocks", peer_key)

    # 1. Combined rename + metadata update via `issue edit --name ... --priority critical --state "in progress"`
    mid_name = "Idempotent Stripe Retry Worker"
    edited = ws.cli_json(
        "issue",
        "edit",
        old_name,
        "--name",
        mid_name,
        "--priority",
        "critical",
        "--state",
        "in progress",
    )
    assert edited["key"] == orig_key
    assert edited["name"] == mid_name
    assert edited["folder"] == mid_name
    assert edited["priority"] == "critical"
    assert edited["state"] == "in-progress"

    assert not (ws.issues_dir / old_name).exists()
    assert (ws.issues_dir / mid_name).is_dir()
    _assert_no_rename_tmp_dirs(ws)

    peer_disk_mid = _read_issue_json(ws, "Billing Ledger Reconciliation")
    assert peer_disk_mid["links"][0]["target_key"] == orig_key
    assert peer_disk_mid["links"][0]["target_name"] == mid_name

    dl_mid = tmp_path / "dl_mid.txt"
    ws.cli("attach", "download", mid_name, att_id, "-o", str(dl_mid))
    assert sha256_file(dl_mid) == txt_sha

    assert ws.cli_json("search", "Idempotent Stripe Retry")["total"] == 1
    assert ws.cli_json("search", "Legacy Payment")["total"] == 0

    hist_mid = _read_history_jsonl(ws, mid_name)
    actions_mid = [ev["action"] for ev in hist_mid]
    assert "renamed" in actions_mid
    assert "updated" in actions_mid
    ren_ev = next(ev for ev in hist_mid if ev["action"] == "renamed")
    assert {"field": "name", "old": old_name, "new": mid_name} in ren_ev["diffs"]
    upd_ev = next(ev for ev in hist_mid if ev["action"] == "updated")
    upd_diffs = {(d["field"], d["old"], d["new"]) for d in upd_ev["diffs"]}
    assert ("priority", "low", "critical") in upd_diffs
    assert ("state", "todo", "in-progress") in upd_diffs

    # 2. Standalone `issue edit <ITR-N> --name` (human output) followed by `issue rename <ITR-N> --json`
    standalone_name = "Idempotent Queue Dispatcher"
    human_edit = ws.cli("issue", "edit", orig_key, "--name", standalone_name)
    assert orig_key in human_edit.stdout
    assert standalone_name in human_edit.stdout
    assert not (ws.issues_dir / mid_name).exists()
    assert (ws.issues_dir / standalone_name).is_dir()

    final_name = "Resilient Payment Queue Engine"
    renamed_by_key = ws.cli_json("issue", "rename", orig_key, final_name)
    assert renamed_by_key["key"] == orig_key
    assert renamed_by_key["name"] == final_name
    assert renamed_by_key["folder"] == final_name
    assert renamed_by_key["priority"] == "critical"
    assert renamed_by_key["state"] == "in-progress"

    assert not (ws.issues_dir / standalone_name).exists()
    assert (ws.issues_dir / final_name).is_dir()
    _assert_no_rename_tmp_dirs(ws)

    peer_disk_final = _read_issue_json(ws, "Billing Ledger Reconciliation")
    assert peer_disk_final["links"][0]["target_name"] == final_name

    dl_final = tmp_path / "dl_final.txt"
    ws.cli_json("attach", "download", orig_key, att_id, "-o", str(dl_final))
    assert sha256_file(dl_final) == txt_sha

    assert ws.cli_json("search", "Resilient Payment Queue")["total"] == 1
    assert ws.cli_json("search", "Idempotent Stripe")["total"] == 0
    assert ws.cli_json("search", "Queue Dispatcher")["total"] == 0


def test_03_chained_cyclic_renames_alpha_to_beta_to_gamma_to_alpha(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Execute full cyclic rename chain ('Alpha Stage' -> 'Beta Stage' -> 'Gamma Stage' -> 'Alpha Stage')
    verifying active folder exclusivity, stable ITR-N key, peer target_name sync, FTS5 accuracy,
    attachment SHA-256 integrity, and all 3 chronological `renamed` entries in history.jsonl."""
    ws = workspace

    ws.cli_json("issue", "create", "Peer Tracker", "--desc", "Monitors stage transitions")
    png_path, png_sha = make_png(tmp_path / "pipeline_chart.png", width=24, height=24, color=(16, 185, 129))

    alpha = ws.cli_json(
        "issue",
        "create",
        "Alpha Stage",
        "--category",
        "story",
        "--desc",
        "Cyclic pipeline verification item",
        "--attach",
        str(png_path),
    )
    orig_key = alpha["key"]
    att_id = alpha["attachments"][0]["id"]

    ws.cli_json("comment", "add", "Alpha Stage", "--body", "Pipeline checkpoint telemetry note")
    ws.cli_json("link", "add", "Alpha Stage", "relates-to", "Peer Tracker")

    all_stage_names = {"Alpha Stage", "Beta Stage", "Gamma Stage"}
    transitions = [
        ("Alpha Stage", "Beta Stage"),
        ("Beta Stage", "Gamma Stage"),
        ("Gamma Stage", "Alpha Stage"),
    ]

    for src_name, dst_name in transitions:
        res = ws.cli_json("issue", "rename", src_name, dst_name)
        assert res["key"] == orig_key
        assert res["name"] == dst_name
        assert res["folder"] == dst_name

        # Only active stage folder exists on disk
        assert (ws.issues_dir / dst_name).is_dir()
        for inactive in all_stage_names - {dst_name}:
            assert not (ws.issues_dir / inactive).exists(), f"Stale folder '{inactive}' should not exist"
        _assert_no_rename_tmp_dirs(ws)

        # Peer Tracker issue.json target_name updated at every step
        peer_raw = _read_issue_json(ws, "Peer Tracker")
        peer_links = [lnk for lnk in peer_raw["links"] if lnk["target_key"] == orig_key]
        assert len(peer_links) == 1
        assert peer_links[0]["target_name"] == dst_name

        # FTS5 finds active stage and returns 0 for inactive stages
        active_hits = ws.cli_json("search", dst_name)
        assert active_hits["total"] == 1
        assert active_hits["issues"][0]["key"] == orig_key
        for inactive in all_stage_names - {dst_name}:
            inactive_hits = ws.cli_json("search", inactive)
            assert inactive_hits["total"] == 0, f"FTS5 returned stale hit for '{inactive}'"

    # Final verification back at "Alpha Stage"
    assert sorted(os.listdir(ws.issues_dir)) == ["Alpha Stage", "Peer Tracker"]

    dl_out = tmp_path / "alpha_roundtrip.png"
    ws.cli_json("attach", "download", "Alpha Stage", att_id, "-o", str(dl_out))
    assert sha256_file(dl_out) == png_sha

    history_entries = _read_history_jsonl(ws, "Alpha Stage")
    cli_hist = ws.cli_json("history", orig_key)
    assert cli_hist["history"] == history_entries

    renamed_entries = [ev for ev in history_entries if ev.get("action") == "renamed"]
    assert len(renamed_entries) == 3
    actual_pairs = [
        (ev["diffs"][0]["old"], ev["diffs"][0]["new"])
        for ev in renamed_entries
    ]
    assert actual_pairs == transitions


def test_04_case_only_renames_and_noop_same_name_rename(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify case-only folder renames ('fix ui layout' -> 'Fix UI Layout' -> 'FIX UI LAYOUT' -> 'Fix UI Layout')
    via both `issue rename` and `issue edit --name`, plus idempotent no-op same-name rename."""
    ws = workspace

    ws.cli_json("issue", "create", "Responsive Grid Audit")

    png_path, png_sha = make_png(tmp_path / "layout_diff.png", width=36, height=20, color=(234, 88, 12))
    pdf_path, pdf_sha = make_pdf(tmp_path / "spec_sheet.pdf", text="Flexbox spacing spec")

    orig_lower = "fix ui layout"
    created = ws.cli_json(
        "issue",
        "create",
        orig_lower,
        "--category",
        "bug",
        "--desc",
        "Navbar alignment bug\n\n![layout](embed:layout_diff.png)",
        "--embed",
        str(png_path),
        "--attach",
        str(pdf_path),
    )
    orig_key = created["key"]
    png_att_id = created["description_embeds"][0]["id"]
    pdf_att_id = created["attachments"][0]["id"]
    ws.cli_json("link", "add", orig_lower, "blocks", "Responsive Grid Audit")

    assert orig_lower in os.listdir(ws.issues_dir)

    # 1. Case-only rename via `issue rename`: "fix ui layout" -> "Fix UI Layout"
    title_case = "Fix UI Layout"
    ren_1 = ws.cli_json("issue", "rename", orig_lower, title_case)
    assert ren_1["key"] == orig_key
    assert ren_1["name"] == title_case
    assert ren_1["folder"] == title_case

    entries_after_1 = os.listdir(ws.issues_dir)
    assert title_case in entries_after_1
    assert orig_lower not in entries_after_1
    _assert_no_rename_tmp_dirs(ws)

    raw_1 = _read_issue_json(ws, title_case)
    assert raw_1["key"] == orig_key
    assert raw_1["name"] == title_case
    assert raw_1["folder"] == title_case

    peer_1 = _read_issue_json(ws, "Responsive Grid Audit")
    assert peer_1["links"][0]["target_key"] == orig_key
    assert peer_1["links"][0]["target_name"] == title_case

    dl_png_1 = tmp_path / "dl1.png"
    dl_pdf_1 = tmp_path / "dl1.pdf"
    ws.cli_json("attach", "download", title_case, png_att_id, "-o", str(dl_png_1))
    ws.cli_json("attach", "download", title_case, pdf_att_id, "-o", str(dl_pdf_1))
    assert sha256_file(dl_png_1) == png_sha
    assert sha256_file(dl_pdf_1) == pdf_sha

    hist_1 = _read_history_jsonl(ws, title_case)
    ren_events_1 = [ev for ev in hist_1 if ev.get("action") == "renamed"]
    assert len(ren_events_1) == 1
    assert ren_events_1[0]["diffs"] == [{"field": "name", "old": orig_lower, "new": title_case}]

    # 2. Case-only rename via `issue edit --name`: "Fix UI Layout" -> "FIX UI LAYOUT" -> "Fix UI Layout"
    upper_case = "FIX UI LAYOUT"
    ren_2 = ws.cli_json("issue", "edit", title_case, "--name", upper_case)
    assert ren_2["key"] == orig_key
    assert ren_2["name"] == upper_case
    entries_after_2 = os.listdir(ws.issues_dir)
    assert upper_case in entries_after_2
    assert title_case not in entries_after_2
    _assert_no_rename_tmp_dirs(ws)
    assert _read_issue_json(ws, "Responsive Grid Audit")["links"][0]["target_name"] == upper_case

    ren_3 = ws.cli_json("issue", "edit", upper_case, "--name", title_case)
    assert ren_3["key"] == orig_key
    assert ren_3["name"] == title_case
    entries_after_3 = os.listdir(ws.issues_dir)
    assert title_case in entries_after_3
    assert upper_case not in entries_after_3
    _assert_no_rename_tmp_dirs(ws)
    assert _read_issue_json(ws, "Responsive Grid Audit")["links"][0]["target_name"] == title_case

    # 3. No-op same-name rename: "Fix UI Layout" -> "Fix UI Layout" (rc == 0, folder & files untouched)
    hist_before_noop = _read_history_jsonl(ws, title_case)
    noop_res = ws.cli_json("issue", "rename", title_case, title_case)
    assert noop_res["key"] == orig_key
    assert noop_res["name"] == title_case
    assert noop_res["folder"] == title_case
    assert title_case in os.listdir(ws.issues_dir)
    _assert_no_rename_tmp_dirs(ws)

    hist_after_noop = _read_history_jsonl(ws, title_case)
    assert hist_after_noop == hist_before_noop

    dl_png_2 = tmp_path / "dl2.png"
    dl_pdf_2 = tmp_path / "dl2.pdf"
    ws.cli("attach", "download", title_case, png_att_id, "-o", str(dl_png_2))
    ws.cli("attach", "download", title_case, pdf_att_id, "-o", str(dl_pdf_2))
    assert sha256_file(dl_png_2) == png_sha
    assert sha256_file(dl_pdf_2) == pdf_sha


def test_05_collision_rollback_and_invalid_or_missing_rename_rejection(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify exact & case-variant rename collisions exit 5 (HTTP 409) with zero folder/history/file
    corruption, non-existent issue renames exit 4, and invalid/path-traversal target names exit 1."""
    ws = workspace

    file_1, sha_1 = make_pdf(tmp_path / "retry_policy.pdf", text="Exponential jitter policy")
    file_2, sha_2 = make_zip(tmp_path / "hmac_vectors.zip", files={"vectors.txt": "sha256=deadbeef\n"})

    name_1 = "Checkout Retry Handler"
    name_2 = "Webhook Signature Validator"

    iss_1 = ws.cli_json(
        "issue",
        "create",
        name_1,
        "--category",
        "feature",
        "--priority",
        "high",
        "--desc",
        "Idempotent checkout retry logic",
        "--attach",
        str(file_1),
    )
    iss_2 = ws.cli_json(
        "issue",
        "create",
        name_2,
        "--category",
        "security",
        "--priority",
        "critical",
        "--desc",
        "Constant-time HMAC webhook verification",
        "--attach",
        str(file_2),
    )
    key_1 = iss_1["key"]
    key_2 = iss_2["key"]
    att_1 = iss_1["attachments"][0]["id"]
    att_2 = iss_2["attachments"][0]["id"]

    ws.cli_json("comment", "add", name_1, "--body", "Checkout retry baseline comment")
    ws.cli_json("comment", "add", name_2, "--body", "Webhook validator baseline comment")

    snap_json_1 = _read_issue_json(ws, name_1)
    snap_json_2 = _read_issue_json(ws, name_2)
    snap_hist_1 = _read_history_jsonl(ws, name_1)
    snap_hist_2 = _read_history_jsonl(ws, name_2)

    # 1. Exact-name collision via `issue rename` (human & --json), `issue edit --name`, and HTTP 409 API
    hum_conflict = ws.cli("issue", "rename", name_1, name_2, expect_rc=5)
    assert name_2 in hum_conflict.stderr

    json_conflict = ws.cli_json("issue", "rename", name_1, name_2, expect_rc=5)
    assert json_conflict["error"] == "conflict"
    assert json_conflict["exit_code"] == 5

    edit_conflict = ws.cli_json(
        "issue",
        "edit",
        name_1,
        "--name",
        name_2,
        "--priority",
        "trivial",
        expect_rc=5,
    )
    assert edit_conflict["error"] == "conflict"
    assert edit_conflict["exit_code"] == 5

    api_conflict = ws.api(
        "POST",
        f"/api/v1/issues/{key_1}/rename",
        json={"new_name": name_2},
    )
    assert api_conflict.status_code == 409

    # 2. Case-variant collision via `issue rename` and `issue edit --name` -> exits 5
    low_conflict = ws.cli_json("issue", "rename", name_1, name_2.lower(), expect_rc=5)
    assert low_conflict["error"] == "conflict"
    assert low_conflict["exit_code"] == 5

    up_conflict = ws.cli_json("issue", "rename", name_1, name_2.upper(), expect_rc=5)
    assert up_conflict["error"] == "conflict"
    assert up_conflict["exit_code"] == 5

    ws.cli("issue", "edit", name_1, "--name", "wEbHoOk SiGnAtUrE vAlIdAtOr", expect_rc=5)

    # 3. Non-existent issue rename -> exits 4
    nf_by_name = ws.cli_json("issue", "rename", "Non Existent Ghost Issue", "Fresh Valid Name", expect_rc=4)
    assert nf_by_name["error"] == "not_found"
    assert nf_by_name["exit_code"] == 4

    nf_by_key = ws.cli_json("issue", "rename", "ITR-9999", "Fresh Valid Name", expect_rc=4)
    assert nf_by_key["error"] == "not_found"
    assert nf_by_key["exit_code"] == 4

    ws.cli("issue", "edit", "Non Existent Ghost Issue", "--name", "Fresh Valid Name", expect_rc=4)

    # 4. Invalid / path-traversal / empty target names -> CLI exits 1 & API returns HTTP 400
    for bad_cli_target in ("../escape", "bad/slash", "   "):
        bad_res = ws.cli_json("issue", "rename", name_1, bad_cli_target, expect_rc=1)
        assert bad_res["exit_code"] == 1

    ws.cli("issue", "edit", name_1, "--name", "../escape", expect_rc=1)

    for bad_api_target in (None, "", ".", "..", "..\\escape", "bad\\slash", "bad\x7fname", ".rename_tmp_injected", "Z" * 181):
        resp_bad = ws.api("POST", f"/api/v1/issues/{key_1}/rename", json={"new_name": bad_api_target})
        assert resp_bad.status_code == 400

    assert not (ws.issues_root / "escape").exists()

    # 5. Assert zero folder, metadata, history, or attachment corruption across both issues
    assert sorted(os.listdir(ws.issues_dir)) == sorted([name_1, name_2])
    _assert_no_rename_tmp_dirs(ws)

    after_json_1 = _read_issue_json(ws, name_1)
    after_json_2 = _read_issue_json(ws, name_2)
    assert after_json_1 == snap_json_1
    assert after_json_2 == snap_json_2
    assert after_json_1["key"] == key_1
    assert after_json_1["name"] == name_1
    assert after_json_1["priority"] == "high"
    assert after_json_2["key"] == key_2
    assert after_json_2["name"] == name_2
    assert after_json_2["priority"] == "critical"

    after_hist_1 = _read_history_jsonl(ws, name_1)
    after_hist_2 = _read_history_jsonl(ws, name_2)
    assert after_hist_1 == snap_hist_1
    assert after_hist_2 == snap_hist_2
    assert [ev for ev in after_hist_1 if ev.get("action") == "renamed"] == []
    assert [ev for ev in after_hist_2 if ev.get("action") == "renamed"] == []

    dl_1 = tmp_path / "verify_1.pdf"
    dl_2 = tmp_path / "verify_2.zip"
    ws.cli_json("attach", "download", name_1, att_1, "-o", str(dl_1))
    ws.cli_json("attach", "download", name_2, att_2, "-o", str(dl_2))
    assert sha256_file(dl_1) == sha_1
    assert sha256_file(dl_2) == sha_2

    assert_clean_repo_root()
