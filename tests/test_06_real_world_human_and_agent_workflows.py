"""Suite 6: Real-World End-to-End Engineering, QA & Security Incident Lifecycles driven via CLI flags."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import (  # noqa: E402
    ITrackWorkspace,
    make_png,
    make_svg,
    make_text_file,
    make_zip,
    sha256_file,
)


def _scan_bytes_under_directory(root_dir: Path, needle: bytes) -> list[str]:
    """Return relative paths of any files under root_dir whose raw bytes contain needle."""
    matches: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            fpath = Path(dirpath) / fn
            try:
                raw = fpath.read_bytes()
            except OSError:
                continue
            if needle in raw:
                matches.append(str(fpath.relative_to(root_dir)))
    return sorted(matches)


def test_workflow_a_sev1_production_bug_triage_agent_rca_embed_swap_qa_reopen_and_archive(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    """Workflow A:
    1. Triage lead initializes workspace, configures WIP limits, creates colored tags & milestone,
       and files a sev1 critical bug with acceptance criteria, trace log attachment, and overdue/upcoming due dates.
    2. AI agent creates a linked spike subtask (via stdin), logs investigation worklogs, and posts a rich
       Markdown root-cause comment (--body-file) with tables, fenced Python code, checklist, #ITR-1 mention,
       an accidentally wrong embedded PNG flamegraph, AND --link-attachment pointing to the issue's trace log.
    3. Agent edits the comment (comment edit --remove-embed <wrong_id> --add-embed <correct_svg>) replacing
       the wrong embedded image cleanly on disk and in Markdown while preserving linked attachments & edit history.
    4. Developer transitions in-review -> in-qa -> done (--resolution fixed) and checks off ACs; QA discovers
       regression, reopens (in-progress, --clear-resolution), unchecks AC, attaches repro ZIP; developer fixes,
       logs final work, closes (done, fixed), and archives (issue archive).
    """
    assets_dir = tmp_path / "workflow_a_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    ws = ITrackWorkspace(git_repo)

    # -------------------------------------------------------------------------
    # Stage 1: Triage Lead Workspace Init, WIP Limits, Tags, Milestone & Bug
    # -------------------------------------------------------------------------
    init_human = ws.cli("init", "--prefix", "ITR", "--title", "Payments Core Engine")
    assert "Payments Core Engine" in init_human.stdout
    assert "ITR" in init_human.stdout

    # Idempotent re-init in JSON mode
    init_again = ws.cli_json("init")
    assert init_again["initialized"] is True
    assert init_again["already_initialized"] is True

    # Configure WIP limits via JSON map + dotted state key
    ws.cli("config", "set", "wip_limits", '{"in-progress": 2, "in-review": 2}')
    cfg_updated = ws.cli_json("config", "set", "wip_limits.in-qa", "1")
    assert cfg_updated["wip_limits"] == {"in-progress": 2, "in-review": 2, "in-qa": 1}
    cfg_human = ws.cli("config", "show")
    assert "Payments Core Engine" in cfg_human.stdout

    # Colored tags & emergency hotfix milestone
    ws.cli("tag", "create", "prod-outage", "--color", "#ef4444", "--desc", "Active production outage")
    ws.cli_json("tag", "create", "payments", "--color", "#f59e0b", "--desc", "Ledger & checkout pipeline")
    ws.cli_json("tag", "create", "regression", "--color", "#8b5cf6", "--desc", "QA regression failure")
    tags_list = ws.cli_json("tag", "list")
    assert {t["name"] for t in tags_list["tags"]} == {"prod-outage", "payments", "regression"}

    ms = ws.cli_json(
        "milestone",
        "create",
        "Hotfix v2.4.1",
        "--desc",
        "Emergency production pool deadlock patch",
        "--due",
        "2027-01-15",
    )
    assert ms["id"] == "MIL-1"
    assert ms["state"] == "open"

    # Trace log attachment + rich bug description file
    trace_log_path, trace_log_sha = make_text_file(
        assets_dir / "prod-gateway-trace.log",
        (
            "2026-11-24T03:58:11Z ERROR [pool-worker-19] Timeout waiting for TxCursor lock\n"
            "2026-11-24T03:58:12Z FATAL [ledger_tx.py:142] Deadlock cycle PoolGuard -> TxCursor -> PoolGuard\n"
        ),
    )
    bug_desc_file = assets_dir / "bug_description.md"
    bug_desc_file.write_text(
        "## Incident Summary\n\n"
        "Checkout latency spiked to 30s with HTTP 503 errors under burst traffic.\n\n"
        "- **Service**: `ledger-core`\n"
        "- **Symptom**: Connection pool starvation under >50 concurrent checkouts.\n",
        encoding="utf-8",
    )

    today = dt.datetime.now(dt.timezone.utc).date()
    overdue_date = (today - dt.timedelta(days=2)).isoformat()
    upcoming_date = (today + dt.timedelta(days=5)).isoformat()

    bug_name = "Ledger Connection Pool Deadlock Under Burst Traffic"
    bug = ws.cli_json(
        "issue",
        "create",
        bug_name,
        "--category",
        "bug",
        "--state",
        "triage",
        "--priority",
        "critical",
        "--severity",
        "sev1",
        "--desc-file",
        str(bug_desc_file),
        "--ac",
        "Reproduce deadlock deterministically under 64 concurrent workers",
        "Enforce strict lock ordering between PoolGuard and TxCursor",
        "Pass 10,000 burst checkout stress suite with zero timeouts",
        "--assignee",
        "triage-lead",
        "--reporter",
        "pagerduty-bot",
        "--tag",
        "prod-outage,payments",
        "--component",
        "ledger-core",
        "db-pool",
        "--milestone",
        "Hotfix v2.4.1",
        "--points",
        "8",
        "--estimate",
        "240",
        "--due",
        overdue_date,
        "--field",
        "env=prod-us-east",
        "impact=checkout_503",
        "--attach",
        str(trace_log_path),
    )
    assert bug["key"] == "ITR-1"
    assert bug["folder"] == bug_name
    assert (ws.issue_dir(bug_name) / "issue.json").is_file()
    assert bug["is_overdue"] is True
    assert len(bug["acceptance_criteria"]) == 3
    assert len(bug["attachments"]) == 1
    trace_att_id = bug["attachments"][0]["id"]
    assert bug["attachments"][0]["sha256"] == trace_log_sha

    # Verify overdue filter in JSON & OVERDUE badge in Rich show output (addressing by exact name with spaces)
    overdue_list = ws.cli_json("issue", "list", "--overdue")
    assert [i["key"] for i in overdue_list["issues"]] == ["ITR-1"]
    rich_bug_show = ws.cli("issue", "show", bug_name)
    assert "OVERDUE" in rich_bug_show.stdout
    assert "prod-gateway-trace.log" in rich_bug_show.stdout

    # Triage lead assigns SLA upcoming target date and moves to active investigation
    bug_triaged = ws.cli_json(
        "issue",
        "edit",
        bug_name,
        "--state",
        "in-progress",
        "--assignee",
        "ai-agent-alpha",
        "--due",
        upcoming_date,
    )
    assert bug_triaged["is_overdue"] is False
    assert bug_triaged["due_date"] == upcoming_date
    assert bug_triaged["state"] == "in-progress"

    # -------------------------------------------------------------------------
    # Stage 2: AI Agent Spike Subtask, Worklog & Rich Root-Cause Comment
    # -------------------------------------------------------------------------
    spike_name = "Spike Flamegraph Analysis of Pool Lock Order"
    spike = ws.cli_json(
        "issue",
        "create",
        spike_name,
        "--category",
        "spike",
        "--state",
        "in-progress",
        "--priority",
        "high",
        "--severity",
        "sev2",
        "--parent",
        bug_name,
        "--assignee",
        "ai-agent-alpha",
        "--estimate",
        "60",
        "--desc-stdin",
        stdin=(
            "### Spike Charter\n"
            "Capture lock contention flamegraphs for #ITR-1 and isolate AB-BA lock inversion.\n"
        ),
    )
    assert spike["key"] == "ITR-2"
    assert spike["parent"] == "ITR-1"

    # Link spike -> bug using human Rich CLI & dual addressing
    link_out = ws.cli("link", "add", spike_name, "relates-to", "ITR-1")
    assert "Linked" in link_out.stdout and "ITR-1" in link_out.stdout
    spike_links = ws.cli_json("link", "list", spike_name)
    assert any(l["type"] == "relates-to" and l["target_key"] == "ITR-1" for l in spike_links["links"])

    # Log investigation work on spike and bug
    ws.cli(
        "worklog",
        "add",
        spike_name,
        "45m",
        "--author",
        "ai-agent-alpha",
        "--note",
        "Captured contended mutex stacks under synthetic burst",
    )
    wl_bug_1 = ws.cli_json(
        "worklog",
        "add",
        bug_name,
        "1h 15m",
        "--author",
        "ai-agent-alpha",
        "--note",
        "Isolated AB-BA lock order inversion in ledger_tx.py",
    )
    assert wl_bug_1["worklog"]["minutes"] == 75
    assert wl_bug_1["time_progress"]["logged_minutes"] == 75

    # Complete spike subtask
    ws.cli("issue", "state", spike_name, "done", "--resolution", "fixed")

    # Prepare accidentally wrong PNG flamegraph and rich Markdown root-cause comment file
    wrong_png_path, _wrong_png_sha = make_png(
        assets_dir / "wrong_cpu_idle_flamegraph.png",
        width=48,
        height=24,
        color=(220, 38, 38),
    )
    rca_md_path = assets_dir / "root_cause_analysis.md"
    rca_md_path.write_text(
        "## Root-Cause Analysis for #ITR-1 (via #ITR-2)\n\n"
        "| Worker Path | First Lock | Second Lock | Outcome |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| `checkout_commit()` | `PoolGuard` | `TxCursor` | Contended |\n"
        "| `background_reaper()` | `TxCursor` | `PoolGuard` | **AB-BA Deadlock** |\n\n"
        "```python\n"
        "def acquire_ordered(pool_guard, tx_cursor):\n"
        "    with pool_guard:\n"
        "        with tx_cursor:\n"
        "            return tx_cursor.flush()\n"
        "```\n\n"
        "### Verification Checklist\n"
        "- [x] Confirmed stack traces in linked gateway log\n"
        "- [ ] Validate TLS retry release path\n\n"
        "![Captured Flamegraph](embed:wrong_cpu_idle_flamegraph.png)\n",
        encoding="utf-8",
    )

    rca_cmt = ws.cli_json(
        "comment",
        "add",
        bug_name,
        "--body-file",
        str(rca_md_path),
        "--author",
        "ai-agent-alpha",
        "--embed",
        str(wrong_png_path),
        "--link-attachment",
        trace_att_id,
    )
    cmt_id = rca_cmt["id"]
    assert cmt_id == "CMT-1"
    assert len(rca_cmt["embeds"]) == 1
    wrong_embed_id = rca_cmt["embeds"][0]["id"]
    wrong_stored_name = rca_cmt["embeds"][0]["stored_name"]
    assert f"![Captured Flamegraph](attachment://{wrong_embed_id})" in rca_cmt["body"]
    assert "embed:wrong_cpu_idle_flamegraph.png" not in rca_cmt["body"]
    assert rca_cmt["linked_attachment_ids"] == [trace_att_id]
    assert rca_cmt["linked_attachments"][0]["id"] == trace_att_id
    assert rca_cmt["linked_attachments"][0]["deleted"] is False

    # Verify cross-linked trace log was NOT duplicated into comment's files/ directory
    cmt_files_dir = ws.issue_dir(bug_name) / "comments" / cmt_id / "files"
    assert (cmt_files_dir / wrong_stored_name).is_file()
    assert len(list(cmt_files_dir.iterdir())) == 1

    # -------------------------------------------------------------------------
    # Stage 3: Agent Replaces Wrong Embedded PNG With Correct SVG Flamegraph
    # -------------------------------------------------------------------------
    correct_svg_path, correct_svg_sha = make_svg(
        assets_dir / "correct_lock_inversion_flamegraph.svg",
        label="PoolGuard -> TxCursor Contention",
        width=240,
        height=80,
    )
    fixed_cmt = ws.cli_json(
        "comment",
        "edit",
        "ITR-1",
        cmt_id,
        "--actor",
        "ai-agent-alpha",
        "--remove-embed",
        wrong_embed_id,
        "--add-embed",
        str(correct_svg_path),
    )
    assert len(fixed_cmt["embeds"]) == 1
    correct_embed_id = fixed_cmt["embeds"][0]["id"]
    correct_stored_name = fixed_cmt["embeds"][0]["stored_name"]
    assert correct_embed_id != wrong_embed_id

    # Wrong PNG must be gone from disk, attachment registry, and Markdown body
    assert not (cmt_files_dir / wrong_stored_name).exists()
    assert f"attachment://{wrong_embed_id}" not in fixed_cmt["body"]
    ws.cli("attach", "info", "ITR-1", wrong_embed_id, expect_rc=4)

    # Correct SVG must exist on disk, be referenced in Markdown, and match SHA-256 on download
    assert (cmt_files_dir / correct_stored_name).is_file()
    assert f"![correct_lock_inversion_flamegraph.svg](attachment://{correct_embed_id})" in fixed_cmt["body"]
    assert "| `background_reaper()` | `TxCursor` | `PoolGuard` | **AB-BA Deadlock** |" in fixed_cmt["body"]
    assert "def acquire_ordered(pool_guard, tx_cursor):" in fixed_cmt["body"]
    assert "#ITR-1" in fixed_cmt["body"]
    assert fixed_cmt["linked_attachment_ids"] == [trace_att_id]
    assert len(fixed_cmt["edit_history"]) == 1

    dl_svg_path = assets_dir / "downloaded_flamegraph.svg"
    ws.cli("attach", "download", bug_name, correct_embed_id, "-o", str(dl_svg_path))
    assert sha256_file(dl_svg_path) == correct_svg_sha

    # -------------------------------------------------------------------------
    # Stage 4: Dev Fix -> QA Reopen (--clear-resolution + ZIP) -> Final Fix -> Archive
    # -------------------------------------------------------------------------
    ws.cli(
        "issue",
        "edit",
        "ITR-1",
        "--assignee",
        "dev-jordan",
        "--check-ac",
        "AC-1",
        "AC-2",
        "AC-3",
        "--state",
        "in-review",
    )
    ws.cli("issue", "state", bug_name, "in-qa")
    closed_first = ws.cli_json("issue", "state", "ITR-1", "done", "--resolution", "fixed")
    assert closed_first["state"] == "done"
    assert closed_first["resolution"] == "fixed"
    assert closed_first["ac_progress"] == {"total": 3, "checked": 3, "pct": 100.0}
    assert closed_first["subtask_progress"] == {"total": 1, "completed": 1, "pct": 100.0}

    # QA discovers edge-case regression on TLS timeout, attaches repro ZIP, unchecks AC-3 & reopens
    repro_zip_path, repro_zip_sha = make_zip(
        assets_dir / "qa-tls-timeout-repro.zip",
        files={
            "repro_steps.txt": "1. Drop outbound TLS ACK\n2. Observe leaked PoolGuard semaphore\n",
            "metrics.json": '{"leaked_connections": 4}',
        },
    )
    reopened = ws.cli_json(
        "issue",
        "edit",
        bug_name,
        "--state",
        "in-progress",
        "--clear-resolution",
        "--uncheck-ac",
        "AC-3",
        "--add-tag",
        "regression",
    )
    assert reopened["state"] == "in-progress"
    assert reopened["resolution"] is None
    assert reopened["ac_progress"] == {"total": 3, "checked": 2, "pct": 66.7}
    assert "regression" in reopened["tags"]

    att_repro = ws.cli_json("attach", "add", bug_name, str(repro_zip_path), "--by", "qa-priya")
    repro_att_id = att_repro["attachments"][0]["id"]
    assert att_repro["attachments"][0]["sha256"] == repro_zip_sha

    ws.cli(
        "comment",
        "add",
        "ITR-1",
        "--author",
        "qa-priya",
        "--link-attachment",
        repro_att_id,
        "--stdin",
        stdin="Reopening #ITR-1: pool semaphore leaks when TLS upstream resets mid-flush.\n",
    )

    # Developer fixes finally-block release, logs work, re-checks AC-3, closes & archives
    ws.cli_json(
        "worklog",
        "add",
        "ITR-1",
        "90",
        "--author",
        "dev-jordan",
        "--note",
        "Wrapped TLS socket flush in try/finally to guarantee PoolGuard release",
    )
    final_done = ws.cli_json(
        "issue",
        "edit",
        "ITR-1",
        "--check-ac",
        "3",
        "--state",
        "done",
        "--resolution",
        "fixed",
    )
    assert final_done["state"] == "done"
    assert final_done["resolution"] == "fixed"
    assert final_done["logged_minutes"] == 165  # 75m + 90m
    assert final_done["ac_progress"] == {"total": 3, "checked": 3, "pct": 100.0}

    archived_human = ws.cli("issue", "archive", bug_name)
    assert "Archived" in archived_human.stdout

    archived_issue = ws.cli_json("issue", "show", "ITR-1")
    assert archived_issue["archived"] is True
    assert archived_issue["state"] == "archived"
    assert archived_issue["previous_state"] == "done"
    assert archived_issue["resolution"] == "fixed"

    # Active tab hides archived bug while archived tab surfaces it
    active_keys = [i["key"] for i in ws.cli_json("issue", "list", "--tab", "active")["issues"]]
    archived_keys = [i["key"] for i in ws.cli_json("issue", "list", "--tab", "archived")["issues"]]
    assert "ITR-1" not in active_keys
    assert archived_keys == ["ITR-1"]

    # Verify complete chronological audit trail in Rich & JSON modes
    hist_human = ws.cli("history", bug_name)
    assert "archived" in hist_human.stdout
    hist_json = ws.cli_json("history", "ITR-1")
    actions = [h["action"] for h in hist_json["history"]]
    for expected_action in (
        "created",
        "updated",
        "worklog_added",
        "comment_added",
        "comment_updated",
        "attachment_added",
        "archived",
    ):
        assert expected_action in actions

    # Verify gitignore keeps runtime files out of git status when issues are committed
    subprocess.run(["git", "add", "itrack-issues"], cwd=str(git_repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Commit archived sev1 postmortem"],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    assert ws.git_status_porcelain() == ""


def test_workflow_b_multi_epic_sprint_planning_blockers_saved_views_and_rollover(
    workspace: ITrackWorkspace,
) -> None:
    """Workflow B:
    1. Create 2 Epics, child Stories/Tasks/Subtasks, blocks dependency chain, and 2 Sprints (Sprint 1, Sprint 2).
    2. Assign issues to Sprint 1, sprint start, create & execute team Saved Views (view create, view run),
       and inspect board --swimlane assignee & tree.
    3. Complete blockers, leave 2 stories unfinished (in-progress), run sprint complete "Sprint 1" --rollover "Sprint 2",
       verify completed stories stay in Sprint 1 while incomplete stories roll into Sprint 2, and verify stats.
    """
    ws = workspace

    # -------------------------------------------------------------------------
    # Stage 1: Epics, Hierarchy, Blocking Chain & Sprints
    # -------------------------------------------------------------------------
    epic1_name = "Zero Downtime Schema Migration Engine"
    epic2_name = "Multi Region Read Replica Routing"

    epic1 = ws.cli_json(
        "issue",
        "create",
        epic1_name,
        "--category",
        "epic",
        "--state",
        "in-progress",
        "--priority",
        "critical",
        "--assignee",
        "lead-maya",
        "--points",
        "13",
    )
    epic2 = ws.cli_json(
        "issue",
        "create",
        epic2_name,
        "--category",
        "epic",
        "--state",
        "todo",
        "--priority",
        "high",
        "--assignee",
        "lead-omar",
        "--points",
        "8",
    )

    story1 = ws.cli_json(
        "issue",
        "create",
        "Dual Write Shadow Table Trigger",
        "--category",
        "story",
        "--state",
        "in-progress",
        "--priority",
        "critical",
        "--parent",
        epic1_name,
        "--assignee",
        "dev-maya",
        "--points",
        "5",
        "--tag",
        "database,schema",
    )
    task1 = ws.cli_json(
        "issue",
        "create",
        "Backfill Checksum Verifier Worker",
        "--category",
        "task",
        "--state",
        "todo",
        "--priority",
        "high",
        "--parent",
        story1["key"],
        "--assignee",
        "dev-maya",
        "--points",
        "3",
        "--tag",
        "database",
    )
    sub1 = ws.cli_json(
        "issue",
        "create",
        "Add CRC32 Batch Cursor Unit Tests",
        "--category",
        "subtask",
        "--state",
        "todo",
        "--priority",
        "medium",
        "--parent",
        task1["name"],
        "--assignee",
        "dev-leo",
        "--points",
        "2",
    )

    story2 = ws.cli_json(
        "issue",
        "create",
        "Replica Lag Health Probe Middleware",
        "--category",
        "story",
        "--state",
        "in-progress",
        "--priority",
        "high",
        "--parent",
        epic2["key"],
        "--assignee",
        "dev-omar",
        "--points",
        "5",
        "--tag",
        "routing",
    )
    story3 = ws.cli_json(
        "issue",
        "create",
        "Automatic Stale Read Fallback Policy",
        "--category",
        "story",
        "--state",
        "in-progress",
        "--priority",
        "medium",
        "--parent",
        epic2_name,
        "--assignee",
        "dev-leo",
        "--points",
        "8",
        "--tag",
        "routing",
    )

    # Establish blocking chain: story1 -> task1 -> story2 -> story3
    ws.cli("link", "add", story1["name"], "blocks", task1["key"])
    ws.cli_json("link", "add", task1["key"], "blocks", story2["name"])
    ws.cli_json("link", "add", story2["key"], "blocks", story3["key"])

    # Verify transitive circular dependency is rejected with exit code 5
    cycle_attempt = ws.cli("link", "add", story3["key"], "blocks", story1["key"], expect_rc=5)
    assert "circular" in cycle_attempt.stderr.lower()

    sp1 = ws.cli_json(
        "sprint",
        "create",
        "Sprint 1",
        "--goal",
        "Deliver dual-write shadow schema & checksum verification",
        "--start",
        "2026-11-01",
        "--end",
        "2026-11-14",
    )
    sp2 = ws.cli_json(
        "sprint",
        "create",
        "Sprint 2",
        "--goal",
        "Deliver multi-region replica health probes & fallback",
        "--start",
        "2026-11-15",
        "--end",
        "2026-11-28",
    )
    assert sp1["id"] == "SPR-1"
    assert sp2["id"] == "SPR-2"

    # -------------------------------------------------------------------------
    # Stage 2: Sprint Assignment, Start, Saved Views, Swimlane Board & Tree
    # -------------------------------------------------------------------------
    ws.cli(
        "sprint",
        "assign",
        "Sprint 1",
        story1["name"],
        task1["key"],
        sub1["key"],
        story2["name"],
        story3["key"],
    )
    started_sp1 = ws.cli_json("sprint", "start", "Sprint 1")
    assert started_sp1["state"] == "active"
    assert started_sp1["total_issues"] == 5

    # Create team Saved Views
    v_blocked = ws.cli_json(
        "view",
        "create",
        "Active Sprint Blockers",
        "--query",
        "sprint = active AND blocked = true ORDER BY priority DESC",
        "--tab",
        "active",
        "--swimlane",
        "assignee",
        "--desc",
        "All currently blocked deliverables in the active sprint",
    )
    assert v_blocked["id"] == "VIEW-1"

    ws.cli(
        "view",
        "create",
        "Maya Database Deliverables",
        "--query",
        'assignee = dev-maya AND tag = "database"',
        "--sort",
        "priority",
        "--order",
        "desc",
        "--desc",
        "Maya's schema migration sprint scope",
    )

    blocked_run = ws.cli_json("view", "run", "Active Sprint Blockers")
    assert [i["key"] for i in blocked_run["issues"]] == [task1["key"], story2["key"], story3["key"]]
    assert all(i["is_blocked"] is True for i in blocked_run["issues"])

    maya_human = ws.cli("view", "run", "Maya Database Deliverables")
    assert story1["key"] in maya_human.stdout
    assert task1["key"] in maya_human.stdout
    maya_json = ws.cli_json("view", "run", "Maya Database Deliverables")
    assert [i["key"] for i in maya_json["issues"]] == [story1["key"], task1["key"]]

    # Inspect Kanban board grouped by assignee swimlane (Rich + JSON)
    board_human = ws.cli("board", "--swimlane", "assignee", "--sprint", "Sprint 1")
    assert "dev-maya" in board_human.stdout
    assert "dev-omar" in board_human.stdout
    board_json = ws.cli_json("board", "--swimlane", "assignee", "--sprint", "Sprint 1")
    assert board_json["swimlane"] == "assignee"
    lane_counts = {lane["key"]: lane["count"] for lane in board_json["lanes"]}
    assert lane_counts == {"dev-leo": 2, "dev-maya": 2, "dev-omar": 1}

    # Inspect Hierarchy & Dependency tree (Rich + JSON)
    tree_human = ws.cli("tree")
    assert "Issue Hierarchy & Dependency Tree" in tree_human.stdout
    tree_e1 = ws.cli_json("tree", epic1_name)
    root_node = tree_e1["trees"][0]
    assert root_node["key"] == epic1["key"]
    assert root_node["children"][0]["key"] == story1["key"]
    assert root_node["children"][0]["children"][0]["key"] == task1["key"]
    assert root_node["children"][0]["children"][0]["children"][0]["key"] == sub1["key"]

    # -------------------------------------------------------------------------
    # Stage 3: Blocker Resolution, Sprint Rollover & Analytics Verification
    # -------------------------------------------------------------------------
    # Completing story1 unblocks task1
    ws.cli("issue", "state", story1["name"], "done", "--resolution", "fixed")
    assert ws.cli_json("issue", "show", task1["key"])["is_blocked"] is False
    assert ws.cli_json("issue", "show", story2["key"])["is_blocked"] is True

    # Completing sub1 and task1 unblocks story2 (while story3 remains blocked by in-progress story2)
    ws.cli("issue", "state", sub1["key"], "done", "--resolution", "fixed")
    ws.cli("issue", "state", task1["key"], "done", "--resolution", "fixed")
    assert ws.cli_json("issue", "show", story2["key"])["is_blocked"] is False
    remaining_blocked = ws.cli_json("view", "run", "Active Sprint Blockers")
    assert [i["key"] for i in remaining_blocked["issues"]] == [story3["key"]]

    # Complete Sprint 1 with rollover of incomplete stories (story2, story3) into Sprint 2
    rollover_res = ws.cli_json("sprint", "complete", "Sprint 1", "--rollover", "Sprint 2")
    assert rollover_res["sprint"]["state"] == "completed"
    assert rollover_res["rollover_to"] == sp2["id"]
    assert set(rollover_res["rolled_over_keys"]) == {story2["key"], story3["key"]}

    # Completed items MUST remain in Sprint 1; incomplete stories MUST now belong to Sprint 2
    sp1_detail = ws.cli_json("sprint", "show", "Sprint 1")
    assert sp1_detail["state"] == "completed"
    assert {i["key"] for i in sp1_detail["issues"]} == {story1["key"], task1["key"], sub1["key"]}
    assert sp1_detail["done_issues"] == 3
    assert sp1_detail["total_issues"] == 3
    assert sp1_detail["completed_points"] == 10.0

    sp2_detail = ws.cli_json("sprint", "show", "Sprint 2")
    assert sp2_detail["state"] == "planned"
    assert {i["key"] for i in sp2_detail["issues"]} == {story2["key"], story3["key"]}
    assert sp2_detail["total_issues"] == 2
    assert sp2_detail["total_points"] == 13.0

    sp2_human = ws.cli("sprint", "show", "Sprint 2")
    assert story2["key"] in sp2_human.stdout
    assert story3["key"] in sp2_human.stdout

    # Verify Analytics Dashboard / stats accuracy
    stats_human = ws.cli("stats")
    assert "Analytics Dashboard" in stats_human.stdout
    stats = ws.cli_json("stats")
    assert stats["total_issues"] == 7
    assert stats["done_issues"] == 3
    assert stats["active_issues"] == 4
    assert stats["blocked_issues"] == 1  # story3 still blocked by story2
    assert stats["completed_points"] == 10.0
    assert stats["total_points"] == 44.0
    vel_by_id = {v["id"]: v for v in stats["sprint_velocity"]}
    assert vel_by_id["SPR-1"]["completed_points"] == 10.0
    assert vel_by_id["SPR-2"]["total_points"] == 13.0


def test_workflow_c_security_incident_sensitive_purge_clones_rename_and_agent_context_pack(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Workflow C:
    1. File security/incident issue with spaces in name; upload a file containing a leaked token
       both as a comment-scoped attachment AND as a whole-issue attachment linked from another comment.
    2. Purge the sensitive comment-scoped attachment (comment attach-delete) AND purge the whole-issue
       attachment (attach delete --yes), verifying zero trace of the sensitive bytes remains anywhere
       under itrack-issues/ or FTS5 while comments remain readable with graceful deleted-attachment tombstones.
    3. Clone follow-up remediation tasks (issue clone), rename the incident issue folder with spaces
       (issue rename), and generate issue context (both Markdown and --json) verifying the AI Agent
       Handoff Pack contains full metadata, AC, links, sanitized comments, worklogs, and history.
    """
    ws = workspace
    sec_assets = tmp_path / "workflow_c_assets"
    sec_assets.mkdir(parents=True, exist_ok=True)

    leaked_secret_str = "sk_live_LEAKED_OAUTH_SIGNING_SECRET_9948172635_CRITICAL_PURGE_ME"
    leaked_secret_bytes = leaked_secret_str.encode("utf-8")

    whole_issue_leak_file, _ = make_text_file(
        sec_assets / "unredacted_edge_env_dump.txt",
        f"EDGE_REGION=us-west-2\nOAUTH_PRIVATE_TOKEN={leaked_secret_str}\n",
    )
    comment_leak_file, _ = make_text_file(
        sec_assets / "raw_session_capture_packet.log",
        f"Authorization: Bearer {leaked_secret_str}\nX-Edge-Node: gw-07\n",
    )
    safe_postmortem_notes, _ = make_text_file(
        sec_assets / "redacted_timeline.txt",
        "04:01Z Alert triggered\n04:05Z Edge logger verbosity disabled\n",
    )

    # -------------------------------------------------------------------------
    # Stage 1: File Security Incident With Spaces & Upload Sensitive Attachments
    # -------------------------------------------------------------------------
    initial_name = "Suspected OAuth Signing Key Exposure in Edge Logs"
    inc = ws.cli_json(
        "issue",
        "create",
        initial_name,
        "--category",
        "security",
        "--state",
        "in-progress",
        "--priority",
        "critical",
        "--severity",
        "sev1",
        "--desc",
        "Edge proxy debug mode accidentally echoed OAuth signing material to diagnostic dumps.",
        "--ac",
        "Purge all unredacted diagnostic dumps from issue tracker",
        "Rotate OAuth signing keys across all edge regions",
        "Deploy automated bearer-token scrubber in edge logger",
        "--assignee",
        "sec-irene",
        "--reporter",
        "siem-sentinel",
        "--tag",
        "security,sec-incident",
        "--component",
        "edge-proxy",
        "auth-signer",
        "--estimate",
        "180",
        "--field",
        "cve_candidate=internal-2026-09",
        "containment=active",
        "--attach",
        str(whole_issue_leak_file),
    )
    assert inc["key"] == "ITR-1"
    issue_leak_att_id = inc["attachments"][0]["id"]

    # Comment 1 uploads a sensitive comment-scoped attachment + a safe redacted timeline attachment
    cmt1 = ws.cli_json(
        "comment",
        "add",
        initial_name,
        "--author",
        "sec-oncall-bot",
        "--body",
        "Initial packet capture uploaded during containment triage (requires scrubbing).",
        "--attach",
        str(comment_leak_file),
        str(safe_postmortem_notes),
    )
    cmt1_id = cmt1["id"]
    cmt1_leak_att = next(a for a in cmt1["attachments"] if a["filename"] == "raw_session_capture_packet.log")
    cmt1_safe_att = next(a for a in cmt1["attachments"] if a["filename"] == "redacted_timeline.txt")

    # Comment 2 cross-links the whole-issue attachment via --link-attachment
    cmt2 = ws.cli_json(
        "comment",
        "add",
        "ITR-1",
        "--author",
        "sec-irene",
        "--body",
        "Confirmed key rotation scope from the linked environment dump; purging raw artifacts next.",
        "--link-attachment",
        issue_leak_att_id,
    )
    cmt2_id = cmt2["id"]
    assert cmt2["linked_attachment_ids"] == [issue_leak_att_id]
    assert cmt2["linked_attachments"][0]["deleted"] is False

    # Confirm pre-purge presence on disk and in FTS5 attachment filename index
    assert len(_scan_bytes_under_directory(ws.issues_root, leaked_secret_bytes)) == 2
    assert ws.cli_json("search", "raw_session_capture_packet")["total"] == 1
    assert ws.cli_json("search", "unredacted_edge_env_dump")["total"] == 1

    # -------------------------------------------------------------------------
    # Stage 2: Purge Both Sensitive Attachments & Verify Zero Trace + Tombstones
    # -------------------------------------------------------------------------
    ws.cli("comment", "attach-delete", initial_name, cmt1_id, cmt1_leak_att["id"])
    ws.cli("attach", "delete", "ITR-1", issue_leak_att_id, "--yes")

    # 1. Zero trace of leaked_secret_bytes anywhere under itrack-issues/ (including .runtime/index.sqlite*)
    leaked_locations = _scan_bytes_under_directory(ws.issues_root, leaked_secret_bytes)
    assert leaked_locations == [], f"Sensitive bytes leaked in files: {leaked_locations}"

    # 2. Purged filenames no longer match in FTS5 search, while safe attachment still matches
    assert ws.cli_json("search", "raw_session_capture_packet")["total"] == 0
    assert ws.cli_json("search", "unredacted_edge_env_dump")["total"] == 0
    assert ws.cli_json("search", "redacted_timeline")["total"] == 1

    # 3. Downloading or inspecting purged attachments returns exit code 4 (not found)
    ws.cli("attach", "info", "ITR-1", cmt1_leak_att["id"], expect_rc=4)
    ws.cli("attach", "info", "ITR-1", issue_leak_att_id, expect_rc=4)

    # 4. Comments remain 100% readable, safe attachment survives, and linked attachment shows tombstone
    after_purge = ws.cli_json("issue", "show", initial_name)
    assert after_purge["attachments"] == []
    cmts_by_id = {c["id"]: c for c in after_purge["comments"]}
    assert "Initial packet capture uploaded" in cmts_by_id[cmt1_id]["body"]
    assert [a["id"] for a in cmts_by_id[cmt1_id]["attachments"]] == [cmt1_safe_att["id"]]
    assert "Confirmed key rotation scope" in cmts_by_id[cmt2_id]["body"]
    assert cmts_by_id[cmt2_id]["linked_attachments"][0]["id"] == issue_leak_att_id
    assert cmts_by_id[cmt2_id]["linked_attachments"][0]["deleted"] is True

    rich_show_purged = ws.cli("issue", "show", initial_name)
    assert "Linked Issue Attachments:" in rich_show_purged.stdout
    assert "redacted_timeline.txt" in rich_show_purged.stdout
    assert leaked_secret_str not in rich_show_purged.stdout

    # Check off AC-1 after successful purge and log incident response time
    ws.cli("issue", "edit", "ITR-1", "--check-ac", "AC-1")
    ws.cli(
        "worklog",
        "add",
        initial_name,
        "1h 10m",
        "--author",
        "sec-irene",
        "--note",
        "Purged sensitive captures and rotated primary signing key",
    )

    # -------------------------------------------------------------------------
    # Stage 3: Clone Follow-Up Remediation Tasks, Rename Incident Folder & Context Pack
    # -------------------------------------------------------------------------
    clone1 = ws.cli_json(
        "issue",
        "clone",
        initial_name,
        "--name",
        "Rotate OAuth Signing Keys Across All Edge Regions",
    )
    clone2 = ws.cli_json(
        "issue",
        "clone",
        "ITR-1",
        "--name",
        "Deploy Automated Bearer Token Scrubber in Edge Logger",
    )
    assert clone1["key"] == "ITR-2"
    assert clone2["key"] == "ITR-3"
    assert clone1["cloned_from"] == "ITR-1"
    assert clone2["cloned_from"] == "ITR-1"

    # Reclassify second follow-up as incident-prevention improvement & link blocks
    ws.cli("issue", "edit", clone2["key"], "--category", "incident", "--priority", "high")
    ws.cli("link", "add", clone1["key"], "blocks", "ITR-1")

    # Rename incident issue folder with spaces
    renamed_title = "Contained OAuth Signing Key Exposure Postmortem"
    rename_out = ws.cli("issue", "rename", initial_name, renamed_title)
    assert renamed_title in rename_out.stdout
    assert not ws.issue_dir(initial_name).exists()
    assert (ws.issue_dir(renamed_title) / "issue.json").is_file()

    # Peer clones automatically reflect renamed target_name in their links
    clone1_fresh = ws.cli_json("issue", "show", clone1["key"])
    target_names = {lnk["target_name"] for lnk in clone1_fresh["links"] if lnk["target_key"] == "ITR-1"}
    assert target_names == {renamed_title}

    # Generate Agent Context Pack in BOTH human Markdown and --json modes
    ctx_human = ws.cli("issue", "context", renamed_title)
    assert "Agent Context Pack" in ctx_human.stdout
    assert renamed_title in ctx_human.stdout
    assert "Linked attachment:" in ctx_human.stdout
    assert leaked_secret_str not in ctx_human.stdout

    ctx_json = ws.cli_json("issue", "context", "ITR-1")
    assert ctx_json["key"] == "ITR-1"
    assert ctx_json["name"] == renamed_title
    md = ctx_json["markdown"]
    assert f"# Agent Context Pack: [ITR-1] {renamed_title}" in md
    assert f"`itrack-issues/issues/{renamed_title}/`" in md
    assert "- [x] (AC-1) Purge all unredacted diagnostic dumps from issue tracker" in md
    assert "- [ ] (AC-2) Rotate OAuth signing keys across all edge regions" in md
    assert clone1["key"] in md and clone2["key"] in md
    assert "[DELETED]" in md
    assert leaked_secret_str not in json.dumps(ctx_json)

    ctx_issue = ctx_json["context"]["issue"]
    assert ctx_issue["logged_minutes"] == 70
    assert ctx_issue["custom_fields"]["containment"] == "active"
    assert len(ctx_issue["comments"]) == 2
    recent_actions = {h["action"] for h in ctx_json["context"]["recent_history"]}
    assert {"created", "comment_added", "comment_updated", "attachment_deleted", "renamed"} <= recent_actions
