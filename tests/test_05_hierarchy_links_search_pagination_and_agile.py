"""Suite 05: Hierarchy, Bidirectional Links, Cycle Rejection, 80-Issue Scale Search/iQL/Pagination, Board, Agile, Worklogs, History, Bulk & Stats."""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("TERM", "dumb")
os.environ.setdefault("COLUMNS", "140")

from helpers import (  # noqa: E402
    CATEGORIES,
    CLIResult,
    IQL_OPERATORS,
    ITRACK_SERVER_PY,
    ITrackWorkspace,
    LINK_TYPES,
    PRESET_TABS,
    PRIORITIES,
    PRIORITY_RANKS,
    RESOLUTIONS,
    SEVERITIES,
    SEVERITY_RANKS,
    STATES,
    SWIMLANES,
    init_git_repo,
    load_itrack_cli_module,
    load_module_from_path,
    make_png,
    make_text_file,
)

TERMINAL_STATES: set[str] = {"done", "cancelled", "archived"}

_CLI_MOD = load_itrack_cli_module()
_CLI_MOD.stdout_console = Console(highlight=False, no_color=True, width=140)
_CLI_MOD.stderr_console = Console(stderr=True, highlight=False, no_color=True, width=140)
_SRV_MOD = load_module_from_path("itrack_srv_cov", ITRACK_SERVER_PY)


def _cli(
    ws: ITrackWorkspace,
    *args: Any,
    expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
) -> CLIResult:
    """Execute `itrack.py:main(argv)` against `ws.root` talking over HTTP to the live `itrack-server.py` daemon,
    recording full `ITRACK_SURFACE_LOG` CLI/API telemetry and coverage without redundant Python interpreter cold-starts."""
    str_args = [str(a) for a in args]
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    prev_cwd = Path.cwd()
    prev_user = os.environ.get("ITRACK_USER")
    rc = 0
    t0 = time.monotonic()
    try:
        if prev_user is None:
            os.environ["ITRACK_USER"] = "iTrack Test Harness"
        os.chdir(ws.root)
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                _CLI_MOD.main(str_args)
            except SystemExit as exc:
                if exc.code is None:
                    rc = 0
                elif isinstance(exc.code, int):
                    rc = exc.code
                else:
                    rc = 1
    finally:
        if prev_user is None:
            os.environ.pop("ITRACK_USER", None)
        else:
            os.environ["ITRACK_USER"] = prev_user
        os.chdir(prev_cwd)

    res = CLIResult(
        args=str_args,
        returncode=rc,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
        duration_sec=time.monotonic() - t0,
    )
    if expect_rc is not None:
        allowed = {expect_rc} if isinstance(expect_rc, int) else set(expect_rc)
        if res.returncode not in allowed:
            raise AssertionError(
                f"CLI command failed exit-code assertion!\n"
                f"  Args: {str_args}\n"
                f"  CWD:  {ws.root}\n"
                f"  Expected RC: {sorted(allowed)}, Actual RC: {res.returncode}\n"
                f"  STDOUT:\n{res.stdout}\n"
                f"  STDERR:\n{res.stderr}"
            )
    return res


def _cli_json(
    ws: ITrackWorkspace,
    *args: Any,
    expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
) -> Any:
    str_args = [str(a) for a in args]
    if "--json" not in str_args:
        str_args.append("--json")
    return _cli(ws, *str_args, expect_rc=expect_rc).json()


def _read_disk_issue_json(ws: ITrackWorkspace, issue_name: str) -> dict[str, Any]:
    issue_json_path = ws.issue_dir(issue_name) / "issue.json"
    assert issue_json_path.is_file(), f"Missing issue.json on disk at {issue_json_path}"
    return json.loads(issue_json_path.read_text(encoding="utf-8"))


def _init_ws_resilient(repo_dir: Path, *extra_init_args: str) -> ITrackWorkspace:
    """Initialize an isolated ITrackWorkspace via real subprocess CLI, retrying once if parallel CPU load delays startup."""
    ws = ITrackWorkspace(repo_dir)
    res = ws.cli("init", *extra_init_args, expect_rc=None, timeout=45.0)
    if res.rc != 0:
        ws.cli("init", *extra_init_args, timeout=45.0)
    return ws


def _seed_issues_api(ws: ITrackWorkspace, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rapidly seed prerequisite issues via a single persistent HTTP client against the workspace daemon."""
    base_url = ws.ensure_server()
    created: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
        for body in payloads:
            resp = client.post("/api/v1/issues", json=body)
            assert resp.status_code == 201, f"Failed seeding issue {body}: {resp.text}"
            created.append(resp.json())
    return created


@pytest.fixture
def clean_ws(tmp_path: Path) -> Iterator[ITrackWorkspace]:
    """Function-scoped isolated workspace resilient to heavy parallel CPU load, with strict daemon teardown."""
    repo_dir = init_git_repo(tmp_path / "repo")
    ws = _init_ws_resilient(repo_dir)
    try:
        yield ws
    finally:
        ws.stop_server()


# ===========================================================================
# Module-Scoped 80-Issue Scale Fixture (Read-Only Across Scale/Search Tests)
# ===========================================================================


@pytest.fixture(scope="module")
def seeded_scale_ws(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[ITrackWorkspace, dict[str, dict[str, Any]]]]:
    """Seed an isolated workspace with 80 diverse issues across all enums, fields, comments, attachments & links."""
    repo_dir = init_git_repo(tmp_path_factory.mktemp("scale_80_repo"))
    assets_dir = tmp_path_factory.mktemp("scale_80_assets")
    ws = _init_ws_resilient(repo_dir, "--prefix", "ITR", "--title", "Scale Search Suite")

    try:
        # 1. Tags, Sprints (1 active, 1 planned), Milestones (1 open, 1 closed) via CLI
        _cli(ws, "tag", "create", "security", "--color", "#ef4444", "--desc", "Security track")
        _cli(ws, "tag", "create", "backend", "--color", "#3b82f6", "--desc", "Backend services")
        _cli(ws, "tag", "create", "frontend", "--color", "#10b981", "--desc", "Frontend SPA")
        _cli(ws, "tag", "create", "urgent", "--color", "#f59e0b", "--desc", "Urgent attention")

        sp1 = _cli_json(
            ws, "sprint", "create", "Scale Sprint Active",
            "--goal", "Deliver scale features",
            "--start", "2026-11-01",
            "--end", "2026-11-30",
        )
        assert sp1["id"] == "SPR-1"
        _cli(ws, "sprint", "start", "SPR-1")

        sp2 = _cli_json(
            ws, "sprint", "create", "Scale Sprint Planned",
            "--goal", "Next wave",
            "--start", "2026-12-01",
            "--end", "2026-12-15",
        )
        assert sp2["id"] == "SPR-2"

        ms1 = _cli_json(
            ws, "milestone", "create", "Scale Milestone v1",
            "--desc", "First scale release",
            "--due", "2026-12-01",
        )
        assert ms1["id"] == "MIL-1"

        ms2 = _cli_json(
            ws, "milestone", "create", "Scale Milestone v2",
            "--desc", "Closed historical release",
            "--due", "2025-06-01",
        )
        assert ms2["id"] == "MIL-2"
        _cli(ws, "milestone", "close", "MIL-2")

        # Synthetic files for attachments/embeds on ITR-1 (strictly inside tmp_path_factory)
        att_file, _ = make_text_file(
            assets_dir / "HyperionTraceAttachmentToken.log",
            text="TRACE line 1: Hyperion diagnostics\n",
        )
        emb_png, _ = make_png(assets_dir / "NebulaArchitectureDiagram.png", width=24, height=24)

        i1 = _cli_json(
            ws, "issue", "create", "Scale Epic Alpha QuantumZephyrTitleToken",
            "--category", "epic",
            "--state", "in-progress",
            "--priority", "critical",
            "--severity", "sev1",
            "--desc", "Root epic with XylophoneNebulaDescToken architecture details.",
            "--ac", "VelodromeAcToken criterion one",
            "--assignee", "alice",
            "--reporter", "rep_alice",
            "--tag", "security,backend",
            "--component", "auth-service,db-engine",
            "--sprint", "SPR-1",
            "--milestone", "MIL-1",
            "--points", "13",
            "--estimate", "240",
            "--due", "2020-01-15",
            "--field", "env=prod", "tier=p0",
            "--attach", str(att_file),
            "--embed", str(emb_png),
        )
        assert i1["key"] == "ITR-1"
        _cli_json(
            ws, "comment", "add", "ITR-1",
            "--body", "Investigated KrypthoniteCommentBodyToken under heavy concurrency.",
            "--author", "alice",
        )
        _cli_json(ws, "worklog", "add", "ITR-1", "1h 30m", "--author", "alice", "--note", "Epic design")

        # 2. Rapidly seed ITR-2 .. ITR-80 (79 issues) + worklogs + blocking links
        base_url = ws.ensure_server()
        assignees_cycle = ["alice", "bob", "charlie", "diana", None]
        reporters_cycle = ["rep_alice", "rep_bob", "rep_carol"]
        points_cycle = [0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, None]
        estimates_cycle = [15, 30, 45, 60, 120, 240, None]
        dues_cycle = ["2020-03-01", "2022-07-19", "2099-10-20", "2099-12-01", "2021-11-11", None]
        tags_cycle = [["security"], ["backend", "urgent"], ["frontend"], ["urgent"], ["backend"], ["security", "frontend"], []]
        comps_cycle = [["auth-service"], ["ui-web"], ["db-engine", "cli-core"], ["cli-core"], []]
        sprints_cycle = ["SPR-1", "SPR-2", None]
        milestones_cycle = ["MIL-1", "MIL-2", None]
        cf_cycle = [
            {"env": "prod", "tier": "p0"},
            {"env": "staging", "region": "eu-west"},
            {"env": "qa"},
            {},
        ]

        with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
            assert client.post(
                "/api/v1/issues",
                json={
                    "name": "Scale Story Beta",
                    "category": "story",
                    "state": "todo",
                    "priority": "high",
                    "severity": "sev2",
                    "description": "Story Beta user journey specification.",
                    "assignee": "bob",
                    "reporter": "rep_bob",
                    "tags": ["frontend"],
                    "components": ["ui-web"],
                    "sprint": "SPR-1",
                    "milestone": "MIL-1",
                    "story_points": 8,
                    "estimate_minutes": 120,
                    "due_date": "2099-12-31",
                    "custom_fields": {"env": "staging", "tier": "p1"},
                },
            ).status_code == 201
            assert client.post("/api/v1/issues/ITR-2/worklogs", json={"minutes": 45, "author": "bob", "note": "Wireframes"}).status_code == 201

            assert client.post(
                "/api/v1/issues",
                json={
                    "name": "Scale Blocker Bug Active",
                    "category": "bug",
                    "state": "todo",
                    "priority": "critical",
                    "severity": "sev1",
                    "description": "Active blocker bug preventing downstream readiness.",
                    "assignee": "charlie",
                    "reporter": "rep_carol",
                    "tags": ["security", "urgent"],
                    "components": ["auth-service"],
                    "parent": "ITR-1",
                    "sprint": "SPR-2",
                    "milestone": "MIL-1",
                    "story_points": 5,
                    "estimate_minutes": 60,
                    "due_date": "2021-05-10",
                    "custom_fields": {"env": "prod"},
                },
            ).status_code == 201

            assert client.post(
                "/api/v1/issues",
                json={
                    "name": "Scale LinkBlocked Ready Task",
                    "category": "task",
                    "state": "ready",
                    "priority": "medium",
                    "severity": "sev3",
                    "description": "Ready state task blocked solely via inbound blocks link from ITR-3.",
                    "assignee": "diana",
                    "reporter": "rep_alice",
                    "tags": ["backend"],
                    "components": ["cli-core"],
                    "parent": "Scale Epic Alpha QuantumZephyrTitleToken",
                    "sprint": "SPR-2",
                    "milestone": "MIL-2",
                    "story_points": 3,
                    "estimate_minutes": 30,
                    "due_date": "2099-06-15",
                    "custom_fields": {"env": "dev"},
                },
            ).status_code == 201
            assert client.post("/api/v1/issues/ITR-3/links", json={"type": "blocks", "target": "ITR-4"}).status_code == 201

            done_key: str | None = None
            open_unblocked_key: str | None = None
            res_counter = 0

            for seq_num in range(5, 81):
                idx = seq_num - 5
                cat = CATEGORIES[idx % len(CATEGORIES)]
                st = STATES[idx % len(STATES)]
                prio = PRIORITIES[idx % len(PRIORITIES)]
                sev = SEVERITIES[idx % len(SEVERITIES)]
                if st in ("done", "cancelled") or idx % 4 == 0:
                    res_val: str | None = RESOLUTIONS[res_counter % len(RESOLUTIONS)]
                    res_counter += 1
                else:
                    res_val = None

                parent_key: str | None = None
                if seq_num in (5, 6, 7):
                    parent_key = "ITR-1"
                elif seq_num in (8, 9, 10):
                    parent_key = "ITR-2"

                payload: dict[str, Any] = {
                    "name": f"ScaleIssue {seq_num:02d} {cat.capitalize()} {prio.capitalize()}",
                    "category": cat,
                    "state": st,
                    "priority": prio,
                    "severity": sev,
                    "resolution": res_val,
                    "description": f"Synthetic scale issue {seq_num:02d} in category {cat} and state {st}.",
                    "acceptance_criteria": (
                        [{"text": f"Verify requirement for issue {seq_num:02d}", "checked": (seq_num % 2 == 0)}]
                        if seq_num % 3 != 0
                        else []
                    ),
                    "assignee": assignees_cycle[idx % len(assignees_cycle)],
                    "reporter": reporters_cycle[idx % len(reporters_cycle)],
                    "tags": tags_cycle[idx % len(tags_cycle)],
                    "components": comps_cycle[idx % len(comps_cycle)],
                    "parent": parent_key,
                    "sprint": sprints_cycle[idx % len(sprints_cycle)],
                    "milestone": milestones_cycle[idx % len(milestones_cycle)],
                    "story_points": points_cycle[idx % len(points_cycle)],
                    "estimate_minutes": estimates_cycle[idx % len(estimates_cycle)],
                    "due_date": dues_cycle[idx % len(dues_cycle)],
                    "custom_fields": cf_cycle[idx % len(cf_cycle)],
                    "actor": "scale_seeder",
                }
                resp = client.post("/api/v1/issues", json=payload)
                assert resp.status_code == 201
                created = resp.json()
                assert created["key"] == f"ITR-{seq_num}"

                if st == "done" and done_key is None:
                    done_key = created["key"]
                elif st == "todo" and seq_num < 75 and open_unblocked_key is None:
                    open_unblocked_key = created["key"]

                if seq_num in (5, 12, 25, 40, 60):
                    assert client.post(
                        f"/api/v1/issues/{created['key']}/worklogs",
                        json={"minutes": seq_num * 2, "author": "alice", "note": f"Work on {created['key']}"},
                    ).status_code == 201

            assert done_key and open_unblocked_key
            assert client.post(f"/api/v1/issues/{done_key}/links", json={"type": "blocks", "target": open_unblocked_key}).status_code == 201

        _cli(ws, "issue", "state", "ITR-80", "in-review")
        _cli(ws, "issue", "archive", "ITR-80")

        all_resp = _cli_json(ws, "issue", "list", "--tab", "all", "--page-size", "100")
        assert all_resp["total"] == 80
        assert len(all_resp["issues"]) == 80
        by_key = {iss["key"]: iss for iss in all_resp["issues"]}
        assert len(by_key) == 80

        yield ws, by_key
    finally:
        ws.stop_server()


# ===========================================================================
# 1. Links, Bidirectional Disk Sync, Hierarchy Tree & Exit-5 Cycle Rejection
# ===========================================================================


def test_links_all_six_types_bidirectional_disk_sync_and_dual_addressing(clean_ws: ITrackWorkspace) -> None:
    """Verify all 6 link types sync bidirectionally in issue.json on disk, support dual addressing, and clean up on remove."""
    expected_inverses = {
        "blocks": "blocked-by",
        "blocked-by": "blocks",
        "relates-to": "relates-to",
        "duplicates": "duplicated-by",
        "parent-of": "child-of",
        "clones": "cloned-from",
    }
    assert set(LINK_TYPES) == set(expected_inverses.keys())

    seed_specs: list[dict[str, Any]] = []
    for idx, ltype in enumerate(LINK_TYPES, 1):
        seed_specs.append({"name": f"Source Node {idx} For {ltype}", "state": "todo", "priority": "high"})
        seed_specs.append({"name": f"Target Node {idx} For {ltype}", "state": "todo", "priority": "medium"})
    seeded = _seed_issues_api(clean_ws, seed_specs)

    pairs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for idx, ltype in enumerate(LINK_TYPES):
        pairs[ltype] = (seeded[idx * 2], seeded[idx * 2 + 1])

    created_link_ids: dict[str, str] = {}
    for idx, ltype in enumerate(LINK_TYPES, 1):
        src, tgt = pairs[ltype]
        src_ref = src["key"] if idx % 2 == 1 else src["name"]
        tgt_ref = tgt["name"] if idx % 2 == 1 else tgt["key"]

        if idx == 1:
            # Exercise real subprocess invocation alongside fast in-process CLI calls
            lnk = clean_ws.cli_json("link", "add", src_ref, ltype, tgt_ref)
            lid = lnk["id"]
        elif idx % 2 == 1:
            lnk = _cli_json(clean_ws, "link", "add", src_ref, ltype, tgt_ref)
            lid = lnk["id"]
            assert lnk["type"] == ltype and lnk["direction"] == "outbound"
            assert lnk["target_key"] == tgt["key"] and lnk["target_name"] == tgt["name"]
        else:
            human_res = _cli(clean_ws, "link", "add", src_ref, ltype, tgt_ref)
            assert "Linked" in human_res.stdout
            lid = _cli_json(clean_ws, "link", "list", src["key"])["links"][0]["id"]
        created_link_ids[ltype] = lid

        # Verify raw issue.json on disk for BOTH source and target
        src_disk = _read_disk_issue_json(clean_ws, src["name"])
        tgt_disk = _read_disk_issue_json(clean_ws, tgt["name"])

        src_links = [entry for entry in src_disk["links"] if entry["id"] == lid]
        tgt_links = [entry for entry in tgt_disk["links"] if entry["id"] == lid]
        assert len(src_links) == 1 and len(tgt_links) == 1
        assert src_links[0]["type"] == ltype
        assert src_links[0]["direction"] == "outbound"
        assert src_links[0]["target_key"] == tgt["key"]
        assert src_links[0]["target_name"] == tgt["name"]

        assert tgt_links[0]["type"] == expected_inverses[ltype]
        assert tgt_links[0]["direction"] == "inbound"
        assert tgt_links[0]["target_key"] == src["key"]
        assert tgt_links[0]["target_name"] == src["name"]

        rich_list = _cli(clean_ws, "link", "list", tgt["name"])
        assert lid in rich_list.stdout and expected_inverses[ltype] in rich_list.stdout and src["key"] in rich_list.stdout

        json_list = _cli_json(clean_ws, "link", "list", src["name"])
        assert json_list["key"] == src["key"]
        assert any(item["id"] == lid and item["type"] == ltype for item in json_list["links"])

    # Verify blocking semantics on `blocks` and `blocked-by` until blocker is `done`
    blk_src, blk_tgt = pairs["blocks"]
    blk_tgt_show = _cli_json(clean_ws, "issue", "show", blk_tgt["name"])
    assert blk_tgt_show["is_blocked"] is True
    assert blk_src["key"] in blk_tgt_show["unresolved_blockers"]

    _cli(clean_ws, "issue", "state", blk_src["name"], "done", "--resolution", "fixed")
    blk_tgt_after = _cli_json(clean_ws, "issue", "show", blk_tgt["key"])
    assert blk_tgt_after["is_blocked"] is False and blk_tgt_after["unresolved_blockers"] == []

    inv_src, inv_tgt = pairs["blocked-by"]
    inv_src_show = _cli_json(clean_ws, "issue", "show", inv_src["key"])
    assert inv_src_show["is_blocked"] is True and inv_tgt["key"] in inv_src_show["unresolved_blockers"]

    # Verify `parent-of` synced `target["parent"] == source["key"]` on disk AND in subtasks
    par_src, par_tgt = pairs["parent-of"]
    assert _read_disk_issue_json(clean_ws, par_tgt["name"])["parent"] == par_src["key"]
    assert [s["key"] for s in _cli_json(clean_ws, "issue", "show", par_src["key"])["subtasks"]] == [par_tgt["key"]]

    # Remove all 6 links via CLI (alternating removal from source vs target) and verify disk cleanup
    for idx, ltype in enumerate(LINK_TYPES, 1):
        src, tgt = pairs[ltype]
        lid = created_link_ids[ltype]
        remove_from = src["name"] if idx % 2 == 1 else tgt["key"]
        if idx % 2 == 1:
            rem_json = _cli_json(clean_ws, "link", "remove", remove_from, lid)
            assert rem_json == {"deleted": True, "id": lid}
        else:
            rem_human = _cli(clean_ws, "link", "remove", remove_from, lid)
            assert "Removed link" in rem_human.stdout

        src_disk_after = _read_disk_issue_json(clean_ws, src["name"])
        tgt_disk_after = _read_disk_issue_json(clean_ws, tgt["name"])
        assert src_disk_after["links"] == [] and tgt_disk_after["links"] == []
        if ltype == "parent-of":
            assert tgt_disk_after["parent"] is None

    # Also verify removing `parent-of` from the child (`child-of`) side clears `parent` on disk
    p_src, p_tgt = pairs["parent-of"]
    lnk2 = _cli_json(clean_ws, "link", "add", p_src["key"], "parent-of", p_tgt["key"])
    assert _read_disk_issue_json(clean_ws, p_tgt["name"])["parent"] == p_src["key"]
    _cli_json(clean_ws, "link", "remove", p_tgt["name"], lnk2["id"])
    assert _read_disk_issue_json(clean_ws, p_tgt["name"])["parent"] is None


def test_links_and_hierarchy_exit_5_conflicts_and_cycles(clean_ws: ITrackWorkspace) -> None:
    """Verify exit code 5 on self-links, duplicate links, circular blocks/blocked-by, and parent-of cycles."""
    a, b, c, p1, p2, p3 = _seed_issues_api(
        clean_ws,
        [
            {"name": "Cycle Node Alpha", "state": "todo"},
            {"name": "Cycle Node Beta", "state": "todo"},
            {"name": "Cycle Node Gamma", "state": "todo"},
            {"name": "Hierarchy Parent One", "category": "epic"},
            {"name": "Hierarchy Child Two", "category": "story"},
            {"name": "Hierarchy Subtask Three", "category": "subtask"},
        ],
    )

    # 1. Self-link (`A blocks A` and `A relates-to A`) -> exit 5
    assert clean_ws.cli_json("link", "add", a["key"], "blocks", a["name"], expect_rc=5)["exit_code"] == 5
    assert _cli(clean_ws, "link", "add", a["name"], "relates-to", a["key"], expect_rc=5).rc == 5

    # 2. Duplicate link (`A relates-to B` twice) -> exit 5
    _cli_json(clean_ws, "link", "add", a["key"], "relates-to", b["key"])
    assert _cli_json(clean_ws, "link", "add", a["name"], "relates-to", b["name"], expect_rc=5)["exit_code"] == 5

    # 3. Circular `blocks` (`A -> B -> C -> A`) -> exit 5
    _cli_json(clean_ws, "link", "add", a["key"], "blocks", b["key"])
    _cli_json(clean_ws, "link", "add", b["key"], "blocks", c["key"])
    cyc_blocks = _cli_json(clean_ws, "link", "add", c["key"], "blocks", a["key"], expect_rc=5)
    assert cyc_blocks["exit_code"] == 5 and "circular" in cyc_blocks["message"].lower()

    # 4. Inverse `blocked-by` cycle (`A blocks B`, `B blocks C`, then `A blocked-by C`) -> exit 5
    cyc_inv = _cli_json(clean_ws, "link", "add", a["name"], "blocked-by", c["name"], expect_rc=5)
    assert cyc_inv["exit_code"] == 5 and "circular" in cyc_inv["message"].lower()

    # 5. `parent-of` cycle (`P1 -> P2 -> P3 -> P1` via `link add` AND `issue edit P1 --parent P3`, plus self-parent) -> exit 5
    _cli_json(clean_ws, "link", "add", p1["key"], "parent-of", p2["key"])
    _cli_json(clean_ws, "link", "add", p2["name"], "parent-of", p3["name"])

    cyc_p_link = _cli_json(clean_ws, "link", "add", p3["key"], "parent-of", p1["key"], expect_rc=5)
    assert cyc_p_link["exit_code"] == 5 and "cycle" in cyc_p_link["message"].lower()

    cyc_p_edit = _cli_json(clean_ws, "issue", "edit", p1["name"], "--parent", p3["name"], expect_rc=5)
    assert cyc_p_edit["exit_code"] == 5 and "cycle" in cyc_p_edit["message"].lower()

    self_p_edit = _cli_json(clean_ws, "issue", "edit", p1["key"], "--parent", p1["key"], expect_rc=5)
    assert self_p_edit["exit_code"] == 5 and "own parent" in self_p_edit["message"].lower()

    # Invalid link type -> exit 1; missing link remove -> exit 4
    _cli(clean_ws, "link", "add", a["key"], "invalid-link-type", b["key"], expect_rc=1)
    _cli(clean_ws, "link", "remove", a["key"], "LNK-9999", expect_rc=4)


def test_tree_json_hierarchy(clean_ws: ITrackWorkspace) -> None:
    """Verify `tree` and `tree <issue>` (by key and spaced name) in `--json` mode return full nested trees & blockers."""
    epic, story, subtask, blocker = _seed_issues_api(
        clean_ws,
        [
            {"name": "Platform Migration Epic", "category": "epic", "state": "in-progress"},
            {"name": "Database Cutover Story", "category": "story", "state": "todo", "parent": "ITR-1"},
            {"name": "Verify Read Replicas Subtask", "category": "subtask", "state": "ready", "parent": "Database Cutover Story"},
            {"name": "Firewall Rule Approval", "category": "task", "state": "todo"},
        ],
    )
    _cli_json(clean_ws, "link", "add", blocker["key"], "blocks", subtask["key"])

    full_tree = clean_ws.cli_json("tree")
    assert full_tree["root"] is None
    assert [node["key"] for node in full_tree["trees"]] == [epic["key"], blocker["key"]]

    epic_node = full_tree["trees"][0]
    story_node = epic_node["children"][0]
    sub_node = story_node["children"][0]
    assert story_node["key"] == story["key"]
    assert sub_node["key"] == subtask["key"]
    assert sub_node["is_blocked"] is True
    assert sub_node["unresolved_blockers"] == [blocker["key"]]

    by_key_tree = _cli_json(clean_ws, "tree", story["key"])
    assert by_key_tree["root"] == story["key"]
    assert [c["key"] for c in by_key_tree["trees"][0]["children"]] == [subtask["key"]]

    by_name_tree = _cli_json(clean_ws, "tree", "Database Cutover Story")
    assert by_name_tree == by_key_tree

    _cli(clean_ws, "tree", "ITR-9999", expect_rc=4)


def test_tree_human_renders_hierarchy_nodes(clean_ws: ITrackWorkspace) -> None:
    """Verify Rich human `tree` and `tree <issue>` render root and child issue keys/names to stdout."""
    epic, child = _seed_issues_api(
        clean_ws,
        [
            {"name": "Human Tree Root Epic", "category": "epic", "state": "in-progress"},
            {"name": "Human Tree Child Subtask", "category": "subtask", "state": "todo", "parent": "ITR-1"},
        ],
    )

    res_all = clean_ws.cli("tree")
    assert epic["key"] in res_all.stdout
    assert "Human Tree Root Epic" in res_all.stdout
    assert child["key"] in res_all.stdout
    assert "Human Tree Child Subtask" in res_all.stdout

    res_sub = clean_ws.cli("tree", "Human Tree Root Epic")
    assert epic["key"] in res_sub.stdout
    assert child["key"] in res_sub.stdout


# ===========================================================================
# 2. Scale & Search (80 Seeded Issues: Tabs, Filters, Sorts, Pagination, iQL, FTS5)
# ===========================================================================


def test_scale_80_preset_tabs_exact_membership(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
) -> None:
    """Verify all 8 preset tabs return exact expected issue sets across 80 diverse issues in Rich & `--json` modes."""
    ws, by_key = seeded_scale_ws
    all_issues = list(by_key.values())

    expected_by_tab: dict[str, set[str]] = {
        "active": {
            i["key"]
            for i in all_issues
            if i["state"] in {"triage", "backlog", "todo", "ready", "in-progress", "in-review", "in-qa", "on-hold", "blocked"}
            and not i["archived"]
        },
        "open": {
            i["key"]
            for i in all_issues
            if i["state"] in {"triage", "backlog", "todo", "ready"} and not i["archived"]
        },
        "in-progress": {
            i["key"]
            for i in all_issues
            if i["state"] in {"in-progress", "in-review", "in-qa"} and not i["archived"]
        },
        "blocked": {
            i["key"]
            for i in all_issues
            if i["is_blocked"] and i["state"] not in TERMINAL_STATES
        },
        "completed": {i["key"] for i in all_issues if i["state"] == "done"},
        "cancelled": {i["key"] for i in all_issues if i["state"] == "cancelled"},
        "archived": {i["key"] for i in all_issues if i["state"] == "archived" or i["archived"]},
        "all": set(by_key.keys()),
    }

    assert "ITR-4" in expected_by_tab["blocked"]

    for tab in PRESET_TABS:
        expected_keys = expected_by_tab[tab]
        assert len(expected_keys) > 0

        data = _cli_json(ws, "issue", "list", "--tab", tab, "--page-size", "100")
        actual_keys = {i["key"] for i in data["issues"]}
        assert data["tab"] == tab
        assert data["total"] == len(expected_keys)
        assert actual_keys == expected_keys, f"Mismatch on preset tab '{tab}'"

        human = _cli(ws, "issue", "list", "--tab", tab, "--page-size", "100")
        assert f"{data['total']} total" in human.stdout
        for sample_key in sorted(expected_keys)[:3]:
            assert sample_key in human.stdout

    _cli(ws, "issue", "list", "--tab", "nonexistent-tab", expect_rc=1)


def test_scale_80_issue_list_all_filter_flags(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
) -> None:
    """Exercise every individual filter flag on `issue list` against the 80-issue ground truth."""
    ws, by_key = seeded_scale_ws
    all_issues = list(by_key.values())

    cases: list[tuple[list[str], set[str]]] = [
        (["--category", "incident"], {i["key"] for i in all_issues if i["category"] == "incident"}),
        (["--state", "in-qa"], {i["key"] for i in all_issues if i["state"] == "in-qa"}),
        (["--priority", "critical"], {i["key"] for i in all_issues if i["priority"] == "critical"}),
        (["--severity", "sev1"], {i["key"] for i in all_issues if i["severity"] == "sev1"}),
        (["--resolution", "wont-fix"], {i["key"] for i in all_issues if i["resolution"] == "wont-fix"}),
        (["--assignee", "alice"], {i["key"] for i in all_issues if i["assignee"] == "alice"}),
        (["--assignee", "unassigned"], {i["key"] for i in all_issues if i["assignee"] is None}),
        (["--reporter", "rep_bob"], {i["key"] for i in all_issues if i["reporter"] == "rep_bob"}),
        (["--tag", "security"], {i["key"] for i in all_issues if "security" in i["tags"]}),
        (["--component", "db-engine"], {i["key"] for i in all_issues if "db-engine" in i["components"]}),
        (["--parent", "ITR-1"], {i["key"] for i in all_issues if i["parent"] == "ITR-1"}),
        (
            ["--parent", "Scale Epic Alpha QuantumZephyrTitleToken"],
            {i["key"] for i in all_issues if i["parent"] == "ITR-1"},
        ),
        (["--sprint", "SPR-1"], {i["key"] for i in all_issues if i["sprint"] == "SPR-1"}),
        (["--sprint", "Scale Sprint Active"], {i["key"] for i in all_issues if i["sprint"] == "SPR-1"}),
        (["--milestone", "MIL-1"], {i["key"] for i in all_issues if i["milestone"] == "MIL-1"}),
        (["--milestone", "Scale Milestone v1"], {i["key"] for i in all_issues if i["milestone"] == "MIL-1"}),
        (["--overdue"], {i["key"] for i in all_issues if i["is_overdue"]}),
        (
            ["--query", "priority >= high AND sprint = SPR-1 AND assignee = alice"],
            {
                i["key"]
                for i in all_issues
                if i["priority"] in ("critical", "high") and i["sprint"] == "SPR-1" and i["assignee"] == "alice"
            },
        ),
    ]

    for flags, expected in cases:
        assert len(expected) >= 2, f"Expected non-trivial matches for {flags}"
        got = {i["key"] for i in _cli_json(ws, "issue", "list", *flags, "--page-size", "100")["issues"]}
        assert got == expected, f"Mismatch for issue list {flags}"


def test_scale_80_issue_list_all_13_sort_fields_asc_and_desc(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
) -> None:
    """Verify all 13 sort fields in both `asc` and `desc` match exact SQLite rank/null/tie-break ordering."""
    ws, by_key = seeded_scale_ws
    items_by_seq = sorted(by_key.values(), key=lambda x: int(x["seq"]))

    nullable_fields = {"assignee", "story_points", "estimate_minutes", "due_date"}
    value_extractors = {
        "key": lambda x: int(x["seq"]),
        "name": lambda x: str(x["name"]).lower(),
        "category": lambda x: str(x["category"]),
        "state": lambda x: str(x["state"]),
        "priority": lambda x: PRIORITY_RANKS[x["priority"]],
        "severity": lambda x: SEVERITY_RANKS[x["severity"]],
        "assignee": lambda x: str(x["assignee"]).lower(),
        "story_points": lambda x: float(x["story_points"]),
        "estimate_minutes": lambda x: int(x["estimate_minutes"]),
        "logged_minutes": lambda x: int(x["logged_minutes"]),
        "due_date": lambda x: str(x["due_date"]),
        "created_at": lambda x: str(x["created_at"]),
        "updated_at": lambda x: str(x["updated_at"]),
    }
    assert len(value_extractors) == 13

    for sort_field, key_fn in value_extractors.items():
        for direction in ("asc", "desc"):
            rev = direction == "desc"
            if sort_field in nullable_fields:
                present = [x for x in items_by_seq if x.get(sort_field) is not None and x.get(sort_field) != ""]
                missing = [x for x in items_by_seq if x.get(sort_field) is None or x.get(sort_field) == ""]
                expected_keys = [x["key"] for x in sorted(present, key=key_fn, reverse=rev)] + [x["key"] for x in missing]
            else:
                expected_keys = [x["key"] for x in sorted(items_by_seq, key=key_fn, reverse=rev)]

            resp = _cli_json(
                ws, "issue", "list",
                "--tab", "all",
                "--sort", sort_field,
                "--order", direction,
                "--page-size", "100",
            )
            actual_keys = [i["key"] for i in resp["issues"]]
            assert actual_keys == expected_keys, f"Sort order mismatch for --sort {sort_field} --order {direction}"

    _cli(ws, "issue", "list", "--sort", "not_a_field", expect_rc=1)


def test_scale_80_pagination_boundaries_and_invalid_pages(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
) -> None:
    """Verify `--page-size` 1/10/25/50/100, multi-page union without duplicates, out-of-range `--page 999`, and exit 1 on 0."""
    ws, _ = seeded_scale_ws
    ordered_all_keys = [f"ITR-{n}" for n in range(1, 81)]

    for ps, exp_len, exp_pages, exp_next in (
        (1, 1, 80, True),
        (10, 10, 8, True),
        (25, 25, 4, True),
        (50, 50, 2, True),
        (100, 80, 1, False),
    ):
        p1 = _cli_json(ws, "issue", "list", "--tab", "all", "--sort", "key", "--order", "asc", "--page", "1", "--page-size", str(ps))
        assert len(p1["issues"]) == exp_len
        assert p1["total"] == 80
        assert p1["page"] == 1
        assert p1["page_size"] == ps
        assert p1["total_pages"] == exp_pages
        assert p1["has_prev"] is False
        assert p1["has_next"] is exp_next
        assert [i["key"] for i in p1["issues"]] == ordered_all_keys[:exp_len]

    collected: list[str] = []
    for pg in range(1, 5):
        pdata = _cli_json(ws, "issue", "list", "--tab", "all", "--sort", "key", "--order", "asc", "--page", str(pg), "--page-size", "25")
        assert pdata["page"] == pg
        assert pdata["total_pages"] == 4
        assert pdata["has_prev"] is (pg > 1)
        assert pdata["has_next"] is (pg < 4)
        assert len(pdata["issues"]) == (25 if pg < 4 else 5)
        collected.extend(i["key"] for i in pdata["issues"])

    assert len(collected) == 80 and len(set(collected)) == 80
    assert collected == ordered_all_keys

    oor = _cli_json(ws, "issue", "list", "--tab", "all", "--page", "999", "--page-size", "25")
    assert oor["issues"] == []
    assert oor["total"] == 80
    assert oor["has_prev"] is True
    assert oor["has_next"] is False

    _cli(ws, "issue", "list", "--page", "0", expect_rc=1)
    _cli(ws, "issue", "list", "--page-size", "0", expect_rc=1)
    _cli(ws, "issue", "list", "--page-size", "1001", expect_rc=1)


def test_scale_80_search_all_16_iql_operators_custom_fields_and_fts5(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
) -> None:
    """Exercise all 16 iQL operators, custom fields (`cf.*`), special predicates (`blocked`, `overdue`, `sprint = active`),
    FTS5 across title/desc/AC/comment/attachment, pagination/sort flags, and invalid iQL exit 1."""
    ws, by_key = seeded_scale_ws
    all_issues = list(by_key.values())
    assert len(IQL_OPERATORS) == 16

    def run_iql(expr: str) -> list[dict[str, Any]]:
        return _cli_json(ws, "search", expr, "--iql", "--page-size", "100")["issues"]

    # 1 & 2: `=` and `!=` + custom field `cf.env = prod`, `blocked = true`, `overdue = true`, `sprint = active`
    eq_cf = {i["key"] for i in run_iql("cf.env = prod AND state != archived")}
    assert eq_cf == {
        i["key"] for i in all_issues if i["custom_fields"].get("env") == "prod" and i["state"] != "archived"
    } and len(eq_cf) >= 10

    blk_keys = {i["key"] for i in run_iql("blocked = true")}
    assert "ITR-4" in blk_keys
    assert blk_keys == {i["key"] for i in all_issues if i["is_blocked"]}

    ov_keys = {i["key"] for i in run_iql("overdue = true")}
    assert ov_keys == {i["key"] for i in all_issues if i["is_overdue"]} and len(ov_keys) >= 15

    act_sp_keys = {i["key"] for i in run_iql("sprint = active")}
    assert act_sp_keys == {i["key"] for i in all_issues if i["sprint"] == "SPR-1"}

    # 3 & 4: `>` and `>=`
    gt_res = {i["key"] for i in run_iql("points > 5 AND severity >= sev2")}
    assert gt_res == {
        i["key"]
        for i in all_issues
        if (i["story_points"] is not None and i["story_points"] > 5)
        and SEVERITY_RANKS[i["severity"]] >= SEVERITY_RANKS["sev2"]
    } and len(gt_res) >= 5

    # 5 & 6: `<` and `<=`
    lt_res = {i["key"] for i in run_iql("estimate < 60 AND logged <= 90")}
    assert lt_res == {
        i["key"]
        for i in all_issues
        if (i["estimate_minutes"] is not None and i["estimate_minutes"] < 60) and i["logged_minutes"] <= 90
    } and len(lt_res) >= 10

    # 7 & 8: `~` and `!~`
    assert {i["key"] for i in run_iql('name ~ "Scale Epic" AND description !~ "Synthetic"')} == {"ITR-1"}
    tag_tilde = {i["key"] for i in run_iql('tag ~ "sec" AND component !~ "ui-web"')}
    assert tag_tilde == {
        i["key"]
        for i in all_issues
        if any("sec" in t.lower() for t in i["tags"]) and not any("ui-web" in c.lower() for c in i["components"])
    } and len(tag_tilde) >= 10

    # 9 & 10: `IN` and `NOT IN`
    in_res = {i["key"] for i in run_iql("category IN (epic, security, spike) AND priority NOT IN (low, trivial, medium)")}
    assert in_res == {
        i["key"]
        for i in all_issues
        if i["category"] in {"epic", "security", "spike"} and i["priority"] not in {"low", "trivial", "medium"}
    } and len(in_res) >= 3

    # 11 & 12: `IS EMPTY` and `IS NOT EMPTY`
    empty_res = {i["key"] for i in run_iql("assignee IS EMPTY AND due IS NOT EMPTY")}
    assert empty_res == {
        i["key"] for i in all_issues if i["assignee"] is None and i["due_date"] is not None
    } and len(empty_res) >= 5

    tags_empty_cf_present = {i["key"] for i in run_iql("tags IS EMPTY AND cf.env IS NOT EMPTY")}
    assert tags_empty_cf_present == {
        i["key"] for i in all_issues if len(i["tags"]) == 0 and bool(i["custom_fields"].get("env"))
    } and len(tags_empty_cf_present) >= 5

    # 13, 14, 15, 16: `AND`, `OR`, `NOT`, `ORDER BY`
    complex_list = run_iql(
        "NOT (state = archived OR state = cancelled) AND (category = epic OR category = bug) ORDER BY priority DESC, key ASC"
    )
    exp_complex_items = [
        i
        for i in sorted(all_issues, key=lambda x: int(x["seq"]))
        if i["state"] not in ("archived", "cancelled") and i["category"] in ("epic", "bug")
    ]
    exp_complex_ordered = [
        i["key"] for i in sorted(exp_complex_items, key=lambda x: PRIORITY_RANKS[x["priority"]], reverse=True)
    ]
    assert [i["key"] for i in complex_list] == exp_complex_ordered and len(complex_list) >= 8

    # Plain FTS5 search across Title, Description, Acceptance Criteria, Comment Body, and Attachment Filename
    for token in (
        "QuantumZephyrTitleToken",
        "XylophoneNebulaDescToken",
        "VelodromeAcToken",
        "KrypthoniteCommentBodyToken",
        "HyperionTraceAttachmentToken",
        "NebulaArchitectureDiagram",
    ):
        fts_json = _cli_json(ws, "search", token)
        assert [i["key"] for i in fts_json["issues"]] == ["ITR-1"], f"FTS5 failed finding token {token}"

    paged_search = ws.cli_json("search", "ScaleIssue", "--sort", "key", "--order", "desc", "--page", "2", "--page-size", "5")
    assert paged_search["total"] == 76
    assert [i["key"] for i in paged_search["issues"]] == ["ITR-75", "ITR-74", "ITR-73", "ITR-72", "ITR-71"]

    rich_search = _cli(ws, "search", "QuantumZephyrTitleToken")
    assert "ITR-1" in rich_search.stdout and "Search Results" in rich_search.stdout

    _cli(ws, "search", "nonexistent_field = 42", "--iql", expect_rc=1)
    _cli(ws, "search", "priority = ultra_mega", "--iql", expect_rc=1)
    _cli(ws, "search", "category IN ()", "--iql", expect_rc=1)
    _cli(ws, "search", 'name = "unterminated string', "--iql", expect_rc=1)


def test_fts5_immediate_scrub_on_attachment_comment_and_issue_delete(
    clean_ws: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Confirm deleting an attachment, deleting a comment, and deleting an issue immediately removes their tokens from FTS5."""
    att_path, _ = make_text_file(tmp_path / "ZirconiumSpectraAttachmentFile.txt", text="Attachment payload\n")
    iss = _cli_json(
        clean_ws, "issue", "create", "BerylliumQuasarIssueTitle",
        "--desc", "PalladiumVortexDescriptionToken details here.",
        "--attach", str(att_path),
    )
    key = iss["key"]
    att_id = iss["attachments"][0]["id"]

    cmt = _cli_json(
        clean_ws, "comment", "add", key,
        "--body", "TechnetiumPulsarCommentToken analysis complete.",
        "--author", "alice",
    )
    cmt_id = cmt["id"]

    for tok in (
        "ZirconiumSpectraAttachmentFile",
        "TechnetiumPulsarCommentToken",
        "PalladiumVortexDescriptionToken",
        "BerylliumQuasarIssueTitle",
    ):
        assert [i["key"] for i in _cli_json(clean_ws, "search", tok)["issues"]] == [key]

    # 1. Delete attachment -> attachment filename token vanishes immediately
    _cli_json(clean_ws, "attach", "delete", key, att_id, "--yes")
    assert _cli_json(clean_ws, "search", "ZirconiumSpectraAttachmentFile")["issues"] == []
    assert [i["key"] for i in _cli_json(clean_ws, "search", "TechnetiumPulsarCommentToken")["issues"]] == [key]

    # 2. Delete comment -> comment token vanishes immediately
    _cli_json(clean_ws, "comment", "delete", key, cmt_id, "--yes")
    assert _cli_json(clean_ws, "search", "TechnetiumPulsarCommentToken")["issues"] == []
    assert [i["key"] for i in _cli_json(clean_ws, "search", "PalladiumVortexDescriptionToken")["issues"]] == [key]

    # 3. Delete issue -> title & description tokens vanish immediately
    _cli_json(clean_ws, "issue", "delete", key, "--yes")
    assert _cli_json(clean_ws, "search", "PalladiumVortexDescriptionToken")["issues"] == []
    assert _cli_json(clean_ws, "search", "BerylliumQuasarIssueTitle")["issues"] == []


# ===========================================================================
# 3. Board, Agile (Tags, Sprints, Milestones, Views), Worklogs, History, Bulk & Stats
# ===========================================================================


def test_board_all_swimlanes_filters_and_wip_limit_warnings(clean_ws: ITrackWorkspace) -> None:
    """Verify Kanban board across all 5 swimlanes, all 6 filter flags, archived exclusion, and WIP limit warnings."""
    _cli(clean_ws, "tag", "create", "board-tag", "--color", "#ec4899")
    sp = _cli_json(clean_ws, "sprint", "create", "Board Sprint 1", "--start", "2026-11-01", "--end", "2026-11-14")
    _cli(clean_ws, "sprint", "start", sp["id"])

    c1, c1b, c2, c3, c_arch = _seed_issues_api(
        clean_ws,
        [
            {
                "name": "Board Card One", "category": "bug", "state": "in-progress",
                "priority": "critical", "assignee": "alice", "sprint": sp["id"],
                "tags": ["board-tag"], "story_points": 5,
            },
            {
                "name": "Board Card OneB", "category": "bug", "state": "in-progress",
                "priority": "critical", "assignee": "alice", "sprint": sp["id"],
                "story_points": 2,
            },
            {
                "name": "Board Card Two", "category": "feature", "state": "in-progress",
                "priority": "high", "assignee": "bob", "sprint": sp["id"], "story_points": 3,
            },
            {"name": "Board Card Three", "category": "task", "state": "todo", "priority": "low", "story_points": 2},
            {"name": "Board Archived Card", "category": "chore", "state": "archived", "archived": True},
        ],
    )

    _cli_json(clean_ws, "config", "set", "wip_limits.in-progress", "1")

    for sw in SWIMLANES:
        bdata = _cli_json(clean_ws, "board", "--swimlane", sw)
        assert bdata["swimlane"] == sw
        assert bdata["total_issues"] == 4
        board_keys = {iss["key"] for col in bdata["columns"] for iss in col["issues"]}
        assert board_keys == {c1["key"], c1b["key"], c2["key"], c3["key"]}
        assert c_arch["key"] not in board_keys

        in_prog_col = next(c for c in bdata["columns"] if c["state"] == "in-progress")
        assert in_prog_col["count"] == 3
        assert in_prog_col["wip_limit"] == 1
        assert in_prog_col["wip_exceeded"] is True
        assert len(bdata["lanes"]) >= 1

        rich_board = _cli(clean_ws, "board", "--swimlane", sw)
        expected_wip_badge = "[WIP 3/1!]" if sw in ("none", "sprint") else "[WIP 2/1!]"
        assert expected_wip_badge in rich_board.stdout
        assert c1["key"] in rich_board.stdout

    assert _cli_json(clean_ws, "board", "--category", "bug")["total_issues"] == 2
    assert _cli_json(clean_ws, "board", "--priority", "high")["total_issues"] == 1
    assert _cli_json(clean_ws, "board", "--assignee", "alice")["total_issues"] == 2
    assert _cli_json(clean_ws, "board", "--sprint", sp["id"])["total_issues"] == 3
    assert _cli_json(clean_ws, "board", "--tag", "board-tag")["total_issues"] == 1
    assert _cli_json(clean_ws, "board", "--query", "points >= 3 AND assignee = bob")["total_issues"] == 1


def test_tags_lifecycle_rename_propagation_and_delete_scrub(clean_ws: ITrackWorkspace) -> None:
    """Verify `tag create|list|edit|delete` propagates tag renames and deletions onto issue.json on disk & FTS/index."""
    t_created = _cli_json(clean_ws, "tag", "create", "sec-v1", "--color", "#ef4444", "--desc", "Initial security tag")
    assert t_created["name"] == "sec-v1" and t_created["color"] == "#ef4444"

    _cli(clean_ws, "tag", "create", "sec-v1", expect_rc=5)
    _cli(clean_ws, "tag", "create", "bad-color", "--color", "not-a-hex", expect_rc=1)

    iss1, iss2 = _seed_issues_api(
        clean_ws,
        [
            {"name": "Tagged Issue Alpha", "tags": ["sec-v1", "keep-me"]},
            {"name": "Tagged Issue Beta", "tags": ["sec-v1"]},
        ],
    )

    t_list = _cli_json(clean_ws, "tag", "list")["tags"]
    assert next(t for t in t_list if t["name"] == "sec-v1")["issue_count"] == 2
    assert "sec-v1" in _cli(clean_ws, "tag", "list").stdout

    t_edited = _cli_json(
        clean_ws, "tag", "edit", "sec-v1",
        "--new-name", "security-core",
        "--color", "#10b981",
        "--desc", "Renamed security core tag",
    )
    assert t_edited["name"] == "security-core" and t_edited["color"] == "#10b981"

    assert _read_disk_issue_json(clean_ws, iss1["name"])["tags"] == ["security-core", "keep-me"]
    assert _read_disk_issue_json(clean_ws, iss2["name"])["tags"] == ["security-core"]
    assert "security-core" in _cli(clean_ws, "issue", "show", iss1["key"]).stdout
    assert len(_cli_json(clean_ws, "search", "tag = security-core", "--iql")["issues"]) == 2
    assert _cli_json(clean_ws, "search", "tag = sec-v1", "--iql")["issues"] == []

    _cli(clean_ws, "tag", "delete", "security-core", expect_rc=1)
    assert _cli_json(clean_ws, "tag", "delete", "security-core", "--yes") == {"deleted": True, "name": "security-core"}
    assert _read_disk_issue_json(clean_ws, iss1["name"])["tags"] == ["keep-me"]
    assert _read_disk_issue_json(clean_ws, iss2["name"])["tags"] == []


def test_sprint_lifecycle_and_incomplete_rollover_to_sprint_and_backlog(clean_ws: ITrackWorkspace) -> None:
    """Verify `sprint create|list|show|edit|start|complete|assign|remove|delete` and rollover to SPR-2 & backlog."""
    sp1 = _cli_json(
        clean_ws, "sprint", "create", "Sprint One Alpha",
        "--goal", "Initial alpha goal",
        "--start", "2026-11-01",
        "--end", "2026-11-14",
    )
    sp2 = _cli_json(
        clean_ws, "sprint", "create", "Sprint Two Beta",
        "--goal", "Beta rollover target",
        "--start", "2026-11-15",
        "--end", "2026-11-28",
    )
    sp3 = _cli_json(clean_ws, "sprint", "create", "Sprint Three Disposable")
    assert (sp1["id"], sp2["id"], sp3["id"]) == ("SPR-1", "SPR-2", "SPR-3")

    _cli(clean_ws, "sprint", "create", "Sprint One Alpha", expect_rc=5)
    _cli(clean_ws, "sprint", "create", "Bad Dates Sprint", "--start", "2026-12-10", "--end", "2026-12-01", expect_rc=1)

    sp1_edited = _cli_json(
        clean_ws, "sprint", "edit", "SPR-1",
        "--name", "Sprint One Hardened",
        "--goal", "Hardened goal",
        "--start", "2026-11-02",
        "--end", "2026-11-14",
    )
    assert sp1_edited["name"] == "Sprint One Hardened"

    done_iss, inc_a, inc_b, rem_iss = _seed_issues_api(
        clean_ws,
        [
            {"name": "Sprint Done Issue", "state": "done", "story_points": 5},
            {"name": "Sprint Incomplete Todo", "state": "todo", "story_points": 3},
            {"name": "Sprint Incomplete InProgress", "state": "in-progress", "story_points": 8},
            {"name": "Sprint Removable Task", "state": "backlog", "story_points": 2},
        ],
    )

    asn = _cli_json(clean_ws, "sprint", "assign", "SPR-1", done_iss["key"], inc_a["name"], inc_b["key"], rem_iss["name"])
    assert set(asn["assigned_keys"]) == {done_iss["key"], inc_a["key"], inc_b["key"], rem_iss["key"]}

    rem = _cli_json(clean_ws, "sprint", "remove", "Sprint One Hardened", rem_iss["name"])
    assert rem["removed_keys"] == [rem_iss["key"]]
    assert _read_disk_issue_json(clean_ws, rem_iss["name"])["sprint"] is None

    started = _cli_json(clean_ws, "sprint", "start", "SPR-1", "--start", "2026-11-02", "--end", "2026-11-14")
    assert started["state"] == "active"

    assert [s["id"] for s in _cli_json(clean_ws, "sprint", "list", "--state", "active")["sprints"]] == ["SPR-1"]
    assert {s["id"] for s in _cli_json(clean_ws, "sprint", "list", "--state", "planned")["sprints"]} == {"SPR-2", "SPR-3"}
    assert "Sprint One Hardened" in _cli(clean_ws, "sprint", "list").stdout

    shown = _cli_json(clean_ws, "sprint", "show", "Sprint One Hardened")
    assert shown["total_issues"] == 3 and shown["done_issues"] == 1 and shown["incomplete_issues"] == 2
    assert "Sprint Done Issue" in _cli(clean_ws, "sprint", "show", "SPR-1").stdout

    # Complete SPR-1 -> rollover incomplete issues to SPR-2
    comp1 = _cli_json(clean_ws, "sprint", "complete", "SPR-1", "--rollover", "SPR-2")
    assert comp1["sprint"]["state"] == "completed" and comp1["rollover_to"] == "SPR-2"
    assert set(comp1["rolled_over_keys"]) == {inc_a["key"], inc_b["key"]}
    assert _read_disk_issue_json(clean_ws, done_iss["name"])["sprint"] == "SPR-1"
    assert _read_disk_issue_json(clean_ws, inc_a["name"])["sprint"] == "SPR-2"
    assert _read_disk_issue_json(clean_ws, inc_b["name"])["sprint"] == "SPR-2"
    assert [s["id"] for s in _cli_json(clean_ws, "sprint", "list", "--state", "completed")["sprints"]] == ["SPR-1"]

    # Start SPR-2, finish `inc_a`, complete SPR-2 -> rollover `inc_b` to backlog (`None`)
    _cli(clean_ws, "sprint", "start", "SPR-2")
    _cli(clean_ws, "issue", "state", inc_a["key"], "done")
    comp2 = _cli_json(clean_ws, "sprint", "complete", "SPR-2", "--rollover", "backlog")
    assert comp2["rollover_to"] is None and comp2["rolled_over_keys"] == [inc_b["key"]]
    assert _read_disk_issue_json(clean_ws, inc_a["name"])["sprint"] == "SPR-2"
    assert _read_disk_issue_json(clean_ws, inc_b["name"])["sprint"] is None

    # Delete sprint clears sprint on assigned issues
    _cli(clean_ws, "sprint", "assign", "SPR-3", rem_iss["key"])
    _cli(clean_ws, "sprint", "delete", "SPR-3", expect_rc=1)
    assert _cli_json(clean_ws, "sprint", "delete", "SPR-3", "--yes") == {"deleted": True, "id": "SPR-3"}
    assert _read_disk_issue_json(clean_ws, rem_iss["name"])["sprint"] is None


def test_milestone_lifecycle_and_delete_issue_scrub(clean_ws: ITrackWorkspace) -> None:
    """Verify `milestone create|list|show|edit|close|reopen|delete` and issue milestone clearing on delete."""
    m1 = _cli_json(clean_ws, "milestone", "create", "Release v1.0", "--desc", "GA launch", "--due", "2026-12-01")
    m2 = _cli_json(clean_ws, "milestone", "create", "Release v2.0", "--desc", "Next major", "--due", "2027-03-01")
    assert (m1["id"], m2["id"]) == ("MIL-1", "MIL-2")

    _cli(clean_ws, "milestone", "create", "Release v1.0", expect_rc=5)

    iss_done, iss_open = _seed_issues_api(
        clean_ws,
        [
            {"name": "Milestone Done Feature", "state": "done", "milestone": "MIL-1"},
            {"name": "Milestone Open Task", "state": "todo", "milestone": "Release v1.0"},
        ],
    )

    m1_edit = _cli_json(
        clean_ws, "milestone", "edit", "MIL-1",
        "--name", "Release v1.0 Final",
        "--desc", "Updated GA launch",
        "--due", "2026-12-15",
    )
    assert m1_edit["name"] == "Release v1.0 Final" and m1_edit["due_date"] == "2026-12-15"

    shown = _cli_json(clean_ws, "milestone", "show", "Release v1.0 Final")
    assert shown["total_issues"] == 2 and shown["done_issues"] == 1 and shown["progress_pct"] == 50.0
    assert "Release v1.0 Final" in _cli(clean_ws, "milestone", "show", "MIL-1").stdout

    assert _cli_json(clean_ws, "milestone", "close", "MIL-1")["state"] == "closed"
    assert [m["id"] for m in _cli_json(clean_ws, "milestone", "list", "--state", "closed")["milestones"]] == ["MIL-1"]
    assert [m["id"] for m in _cli_json(clean_ws, "milestone", "list", "--state", "open")["milestones"]] == ["MIL-2"]
    assert {m["id"] for m in _cli_json(clean_ws, "milestone", "list", "--state", "all")["milestones"]} == {"MIL-1", "MIL-2"}
    assert "Release v2.0" in _cli(clean_ws, "milestone", "list").stdout

    assert _cli_json(clean_ws, "milestone", "reopen", "MIL-1")["state"] == "open"

    _cli(clean_ws, "milestone", "delete", "MIL-1", expect_rc=1)
    assert _cli_json(clean_ws, "milestone", "delete", "MIL-1", "--yes") == {"deleted": True, "id": "MIL-1"}
    assert _read_disk_issue_json(clean_ws, iss_done["name"])["milestone"] is None
    assert _read_disk_issue_json(clean_ws, iss_open["name"])["milestone"] is None


def test_saved_views_crud_and_paginated_run(clean_ws: ITrackWorkspace) -> None:
    """Verify `view create|list|show|run|edit|delete` executes saved query/tab/sort/order with pagination."""
    i_low, i_high, i_crit, _, _ = _seed_issues_api(
        clean_ws,
        [
            {"name": "View Bug Low", "category": "bug", "state": "todo", "priority": "low", "story_points": 2},
            {"name": "View Bug High", "category": "bug", "state": "in-progress", "priority": "high", "story_points": 5},
            {"name": "View Bug Critical", "category": "bug", "state": "ready", "priority": "critical", "story_points": 8},
            {"name": "View Done Bug", "category": "bug", "state": "done", "priority": "critical"},
            {"name": "View Story Active", "category": "story", "state": "todo", "priority": "critical"},
        ],
    )

    vw = _cli_json(
        clean_ws, "view", "create", "Active Bugs By Priority",
        "--query", "category = bug",
        "--tab", "active",
        "--sort", "priority",
        "--order", "desc",
        "--swimlane", "priority",
        "--desc", "Active bugs ordered highest priority first",
    )
    assert vw["id"] == "VIEW-1"

    _cli(clean_ws, "view", "create", "Active Bugs By Priority", "--query", "category = bug", expect_rc=5)
    _cli(clean_ws, "view", "create", "Broken View", "--query", "bad_field = 1", expect_rc=1)

    assert len(_cli_json(clean_ws, "view", "list")["views"]) == 1
    assert "Active Bugs By Priority" in _cli(clean_ws, "view", "list").stdout
    assert _cli_json(clean_ws, "view", "show", "Active Bugs By Priority")["id"] == "VIEW-1"
    assert "VIEW-1" in _cli(clean_ws, "view", "show", "VIEW-1").stdout

    run_p1 = _cli_json(clean_ws, "view", "run", "VIEW-1", "--page", "1", "--page-size", "2")
    assert run_p1["total"] == 3
    assert [i["key"] for i in run_p1["issues"]] == [i_crit["key"], i_high["key"]]

    run_p2 = _cli_json(clean_ws, "view", "run", "Active Bugs By Priority", "--page", "2", "--page-size", "2")
    assert [i["key"] for i in run_p2["issues"]] == [i_low["key"]]

    rich_run = _cli(clean_ws, "view", "run", "VIEW-1")
    assert i_crit["key"] in rich_run.stdout and i_low["key"] in rich_run.stdout

    edited = _cli_json(
        clean_ws, "view", "edit", "VIEW-1",
        "--name", "High Plus Bugs Asc",
        "--query", "category = bug AND priority >= high",
        "--tab", "active",
        "--sort", "points",
        "--order", "asc",
        "--swimlane", "category",
        "--desc", "Updated saved view",
    )
    assert edited["name"] == "High Plus Bugs Asc"
    assert [i["key"] for i in _cli_json(clean_ws, "view", "run", "High Plus Bugs Asc")["issues"]] == [i_high["key"], i_crit["key"]]

    _cli(clean_ws, "view", "delete", "VIEW-1", expect_rc=1)
    assert _cli_json(clean_ws, "view", "delete", "VIEW-1", "--yes") == {"deleted": True, "id": "VIEW-1"}
    _cli(clean_ws, "view", "show", "VIEW-1", expect_rc=4)


def test_worklogs_duration_math_progress_and_delete(clean_ws: ITrackWorkspace) -> None:
    """Verify `worklog add|list|delete` with `'1h 30m'` and `'45'`, `logged_minutes`, and `time_progress` math."""
    (iss,) = _seed_issues_api(clean_ws, [{"name": "Time Tracked Service Task", "estimate_minutes": 180}])
    key = iss["key"]

    wl1 = _cli_json(clean_ws, "worklog", "add", key, "1h 30m", "--author", "alice", "--note", "Profiling")
    assert wl1["worklog"]["id"] == "WL-1" and wl1["worklog"]["minutes"] == 90
    assert wl1["time_progress"] == {"estimate_minutes": 180, "logged_minutes": 90, "pct": 50.0}

    wl2 = _cli_json(clean_ws, "worklog", "add", iss["name"], "45", "--author", "bob", "--note", "Patching")
    assert wl2["worklog"]["id"] == "WL-2" and wl2["worklog"]["minutes"] == 45
    assert wl2["time_progress"] == {"estimate_minutes": 180, "logged_minutes": 135, "pct": 75.0}

    _cli(clean_ws, "worklog", "add", key, "0", expect_rc=1)
    _cli(clean_ws, "worklog", "add", key, "-15", expect_rc=1)
    _cli(clean_ws, "worklog", "add", key, "invalid_dur", expect_rc=1)

    listed = _cli_json(clean_ws, "worklog", "list", key)
    assert listed["time_progress"]["logged_minutes"] == 135
    assert len(listed["worklogs"]) == 2
    rich_wl = _cli(clean_ws, "worklog", "list", iss["name"])
    assert "135m / 180m" in rich_wl.stdout and "75%" in rich_wl.stdout

    _cli(clean_ws, "worklog", "delete", key, "WL-1", expect_rc=1)
    del_wl = _cli_json(clean_ws, "worklog", "delete", key, "WL-1", "--yes")
    assert del_wl == {"deleted": True, "id": "WL-1", "logged_minutes": 45}

    disk_iss = _read_disk_issue_json(clean_ws, iss["name"])
    assert disk_iss["logged_minutes"] == 45
    assert [w["id"] for w in disk_iss["worklogs"]] == ["WL-2"]


def test_history_chronological_audit_trail_and_field_diffs(clean_ws: ITrackWorkspace) -> None:
    """Verify `history <issue>` records chronological `HST-N` events and field diffs across mutations."""
    iss, peer = _seed_issues_api(
        clean_ws,
        [
            {"name": "Audit Trail Original Name", "category": "task", "state": "backlog", "priority": "low"},
            {"name": "Audit Peer Issue"},
        ],
    )
    key = iss["key"]

    _cli_json(clean_ws, "issue", "edit", key, "--state", "in-progress", "--priority", "critical", "--assignee", "alice")
    _cli_json(clean_ws, "issue", "rename", key, "Audit Trail Renamed Name")
    _cli_json(clean_ws, "comment", "add", "Audit Trail Renamed Name", "--body", "Audit note", "--author", "alice")
    _cli_json(clean_ws, "worklog", "add", key, "30m", "--author", "alice", "--note", "Audit work")
    _cli_json(clean_ws, "link", "add", key, "blocks", peer["key"])
    _cli_json(clean_ws, "issue", "archive", key)
    _cli_json(clean_ws, "issue", "unarchive", key)

    hist_data = _cli_json(clean_ws, "history", "Audit Trail Renamed Name")
    entries = hist_data["history"]
    assert len(entries) == 8
    hst_nums = [int(e["id"].split("-")[1]) for e in entries]
    assert hst_nums == sorted(hst_nums) and len(set(hst_nums)) == 8
    assert [e["action"] for e in entries] == [
        "created",
        "updated",
        "renamed",
        "comment_added",
        "worklog_added",
        "link_added",
        "archived",
        "unarchived",
    ]

    updated_diffs = {d["field"]: (d["old"], d["new"]) for d in entries[1]["diffs"]}
    assert updated_diffs["state"] == ("backlog", "in-progress")
    assert updated_diffs["priority"] == ("low", "critical")
    assert updated_diffs["assignee"] == (None, "alice")
    assert entries[2]["diffs"][0] == {"field": "name", "old": "Audit Trail Original Name", "new": "Audit Trail Renamed Name"}

    raw_lines = (clean_ws.issue_dir("Audit Trail Renamed Name") / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in raw_lines] == entries

    rich_hist = _cli(clean_ws, "history", key)
    assert entries[0]["id"] in rich_hist.stdout and "Audit Trail Original Name -> Audit Trail Renamed Name" in rich_hist.stdout


def test_bulk_update_fields_by_issues_and_query_and_bulk_delete(clean_ws: ITrackWorkspace) -> None:
    """Verify `bulk update` (all field flags except archive) and `bulk delete` via `--issues` and `--query`."""
    sp = _cli_json(clean_ws, "sprint", "create", "Bulk Sprint 1")
    ms = _cli_json(clean_ws, "milestone", "create", "Bulk Milestone 1")

    b1, b2, b3, b4 = _seed_issues_api(
        clean_ws,
        [
            {"name": "Bulk Alpha One", "category": "task", "priority": "low", "tags": ["old-tag"]},
            {"name": "Bulk Beta Two", "category": "task", "priority": "low", "tags": ["old-tag"]},
            {"name": "Bulk Gamma Three", "category": "chore", "priority": "trivial"},
            {"name": "Bulk Untouched Four", "category": "epic", "priority": "high"},
        ],
    )

    _cli(clean_ws, "bulk", "update", "--state", "todo", expect_rc=1)
    _cli(clean_ws, "bulk", "update", "--issues", b1["key"], expect_rc=1)
    _cli(clean_ws, "bulk", "update", "--issues", b1["key"], "--assignee", "a", "--clear-assignee", expect_rc=1)
    _cli(clean_ws, "bulk", "update", "--issues", b1["key"], "--sprint", sp["id"], "--clear-sprint", expect_rc=1)
    _cli(clean_ws, "bulk", "update", "--issues", b1["key"], "--milestone", ms["id"], "--clear-milestone", expect_rc=1)

    upd1 = _cli_json(
        clean_ws, "bulk", "update",
        "--issues", f"{b1['key']},{b2['key']}", b3["name"],
        "--state", "in-progress",
        "--priority", "critical",
        "--severity", "sev1",
        "--category", "bug",
        "--assignee", "alice",
        "--add-tag", "bulk-added,triaged",
        "--remove-tag", "old-tag",
        "--sprint", sp["id"],
        "--milestone", ms["id"],
    )
    assert upd1["updated_count"] == 3 and set(upd1["updated_keys"]) == {b1["key"], b2["key"], b3["key"]}

    for item in (b1, b2, b3):
        disk_doc = _read_disk_issue_json(clean_ws, item["name"])
        assert disk_doc["state"] == "in-progress"
        assert disk_doc["priority"] == "critical"
        assert disk_doc["severity"] == "sev1"
        assert disk_doc["category"] == "bug"
        assert disk_doc["assignee"] == "alice"
        assert set(disk_doc["tags"]) == {"bulk-added", "triaged"}
        assert disk_doc["sprint"] == sp["id"]
        assert disk_doc["milestone"] == ms["id"]

    assert _read_disk_issue_json(clean_ws, b4["name"])["category"] == "epic"

    rich_upd = _cli(
        clean_ws, "bulk", "update",
        "--query", "tag = bulk-added AND assignee = alice",
        "--clear-assignee",
        "--clear-sprint",
        "--clear-milestone",
        "--state", "ready",
    )
    assert "Bulk updated 3 issue(s)" in rich_upd.stdout
    for item in (b1, b2, b3):
        disk_doc = _read_disk_issue_json(clean_ws, item["name"])
        assert disk_doc["assignee"] is None and disk_doc["sprint"] is None and disk_doc["milestone"] is None
        assert disk_doc["state"] == "ready"

    _cli(clean_ws, "bulk", "delete", "--issues", b1["key"], expect_rc=1)
    assert clean_ws.issue_dir(b1["name"]).is_dir()

    assert _cli_json(clean_ws, "bulk", "delete", "--issues", b1["name"], "--yes") == {
        "deleted_count": 1,
        "deleted_keys": [b1["key"]],
    }
    assert not clean_ws.issue_dir(b1["name"]).exists()

    del_query = _cli_json(clean_ws, "bulk", "delete", "--query", "tag = bulk-added", "--yes")
    assert del_query["deleted_count"] == 2 and set(del_query["deleted_keys"]) == {b2["key"], b3["key"]}
    assert not clean_ws.issue_dir(b2["name"]).exists() and not clean_ws.issue_dir(b3["name"]).exists()
    assert clean_ws.issue_dir(b4["name"]).is_dir()


def test_bulk_update_archive_and_unarchive(clean_ws: ITrackWorkspace) -> None:
    """Verify `bulk update --archive` and `--unarchive` transition matched issues to/from archived state."""
    i1, i2 = _seed_issues_api(
        clean_ws,
        [
            {"name": "Bulk Archive Target One", "state": "todo"},
            {"name": "Bulk Archive Target Two", "state": "in-progress"},
        ],
    )

    _cli(clean_ws, "bulk", "update", "--issues", i1["key"], "--archive", "--unarchive", expect_rc=1)

    clean_ws.cli_json("bulk", "update", "--issues", f"{i1['key']},{i2['key']}", "--archive")
    for item in (i1, i2):
        disk_doc = _read_disk_issue_json(clean_ws, item["name"])
        assert disk_doc["archived"] is True
        assert disk_doc["state"] == "archived"

    clean_ws.cli_json("bulk", "update", "--query", "archived = true", "--unarchive")
    assert _read_disk_issue_json(clean_ws, i1["name"])["archived"] is False
    assert _read_disk_issue_json(clean_ws, i1["name"])["state"] == "todo"
    assert _read_disk_issue_json(clean_ws, i2["name"])["archived"] is False
    assert _read_disk_issue_json(clean_ws, i2["name"])["state"] == "in-progress"


def test_stats_json_and_rich_tables(clean_ws: ITrackWorkspace) -> None:
    """Verify `stats --json` and Rich distribution tables compute exact counts, points, minutes & sprint velocity."""
    sp = _cli_json(clean_ws, "sprint", "create", "Stats Sprint 1")
    _cli(clean_ws, "sprint", "start", sp["id"])
    ms = _cli_json(clean_ws, "milestone", "create", "Stats Milestone 1")

    s1, s2, s3, _ = _seed_issues_api(
        clean_ws,
        [
            {
                "name": "Stats Done Story", "category": "story", "state": "done",
                "priority": "critical", "severity": "sev1", "assignee": "alice",
                "tags": ["metrics"], "sprint": sp["id"], "milestone": ms["id"],
                "story_points": 8, "estimate_minutes": 120, "due_date": "2020-01-01",
            },
            {
                "name": "Stats Active Blocker Bug", "category": "bug", "state": "in-progress",
                "priority": "high", "severity": "sev2", "assignee": "alice",
                "tags": ["metrics", "urgent"], "sprint": sp["id"],
                "story_points": 5, "estimate_minutes": 60, "due_date": "2020-05-01",
            },
            {
                "name": "Stats Blocked Todo Task", "category": "task", "state": "todo",
                "priority": "medium", "severity": "sev3",
                "story_points": 3, "estimate_minutes": 30, "due_date": "2099-12-31",
            },
            {
                "name": "Stats Archived Chore", "category": "chore", "state": "archived",
                "archived": True, "priority": "low", "severity": "sev4", "story_points": 2,
            },
        ],
    )
    _cli_json(clean_ws, "link", "add", s2["key"], "blocks", s3["key"])
    _cli_json(clean_ws, "worklog", "add", s1["key"], "90", "--author", "alice")
    _cli_json(clean_ws, "worklog", "add", s2["key"], "45", "--author", "alice")

    st = clean_ws.cli_json("stats")
    assert st["total_issues"] == 4
    assert st["active_issues"] == 2
    assert st["done_issues"] == 1
    assert st["blocked_issues"] == 1
    assert st["overdue_issues"] == 1
    assert st["archived_issues"] == 1
    assert st["completion_rate_pct"] == 25.0
    assert st["total_points"] == 18.0
    assert st["completed_points"] == 8.0
    assert st["total_estimate_minutes"] == 210
    assert st["total_logged_minutes"] == 135
    assert st["by_assignee"] == {"alice": 2, "unassigned": 2}
    assert st["by_tag"] == {"metrics": 2, "urgent": 1}
    assert st["milestone_count"] == 1
    assert st["sprint_velocity"] == [
        {
            "id": sp["id"],
            "name": "Stats Sprint 1",
            "state": "active",
            "total_issues": 2,
            "done_issues": 1,
            "total_points": 13.0,
            "completed_points": 8.0,
        }
    ]

    rich_stats = _cli(clean_ws, "stats")
    assert "Analytics Dashboard" in rich_stats.stdout
    assert "Total Issues: 4" in rich_stats.stdout
    assert "Story Points: 8.0/18.0" in rich_stats.stdout
    assert "Time: 135m logged / 210m est" in rich_stats.stdout
    assert "Issues by State" in rich_stats.stdout
    assert "Category & Priority Breakdown" in rich_stats.stdout


def test_stats_human_summary_open_inprogress_completed_counts(clean_ws: ITrackWorkspace) -> None:
    """Verify Rich `stats` summary header renders non-zero Open, In Progress, and Completed counts matching workspace state."""
    _seed_issues_api(
        clean_ws,
        [
            {"name": "Summary Open Todo", "state": "todo"},
            {"name": "Summary Active InProgress", "state": "in-progress"},
            {"name": "Summary Completed Done", "state": "done"},
        ],
    )

    rich_out = clean_ws.cli("stats").stdout
    assert "Open: 1" in rich_out
    assert "In Progress: 1" in rich_out
    assert "Completed: 1" in rich_out


# ===========================================================================
# 4. Deep Server Coverage: Validators, iQL Engine, Self-Heal, Config, REST & Agile Edges
# ===========================================================================


def test_server_validators_parsers_and_git_root_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise `itrack-server.py` L162-550 & L8673-8715 validators, duration/date/points/estimate parsers, and git-root checks."""
    srv = _SRV_MOD

    # Enum normalizers & error branches
    assert srv.normalize_category(None) == "task"
    assert srv.normalize_category("Feature") == "feature"
    with pytest.raises(ValueError, match="Invalid category"):
        srv.normalize_category("not-a-category")

    assert srv.normalize_state(None) == "backlog"
    assert srv.normalize_state("canceled") == "cancelled"
    assert srv.normalize_state("in_progress") == "in-progress"
    with pytest.raises(ValueError, match="Invalid state"):
        srv.normalize_state("not-a-state")

    assert srv.normalize_priority(None) == ("medium", 3)
    assert srv.normalize_priority("CRITICAL") == ("critical", 5)
    with pytest.raises(ValueError, match="Invalid priority"):
        srv.normalize_priority("ultra-high")

    assert srv.normalize_severity(None) == ("sev3", 3)
    assert srv.normalize_severity("SEV1") == ("sev1", 5)
    with pytest.raises(ValueError, match="Invalid severity"):
        srv.normalize_severity("sev99")

    for empty_res in (None, "", "  ", "none", "null", "unset", "clear"):
        assert srv.normalize_resolution(empty_res) is None
    assert srv.normalize_resolution("wontfix") == "wont-fix"
    assert srv.normalize_resolution("by_design") == "by-design"
    with pytest.raises(ValueError, match="Invalid resolution"):
        srv.normalize_resolution("unknown-res")

    assert srv.normalize_link_type("parent_of") == "parent-of"
    for empty_lt in (None, "", "   "):
        with pytest.raises(ValueError, match="Link type is required"):
            srv.normalize_link_type(empty_lt)
    with pytest.raises(ValueError, match="Invalid link type"):
        srv.normalize_link_type("cousin-of")

    assert srv.normalize_tab(None) == "all"
    assert srv.normalize_tab("   ") == "all"
    assert srv.normalize_tab("canceled") == "cancelled"
    with pytest.raises(ValueError, match="Invalid tab"):
        srv.normalize_tab("bogus-tab")

    assert srv.normalize_swimlane(None) == "none"
    assert srv.normalize_swimlane("  ") == "none"
    assert srv.normalize_swimlane("Priority") == "priority"
    with pytest.raises(ValueError, match="Invalid swimlane"):
        srv.normalize_swimlane("bogus-lane")

    # validate_issue_name branches
    assert srv.validate_issue_name("  Clean Title  ") == "Clean Title"
    for bad_name in (
        None,
        42,
        "   ",
        "x" * 181,
        ".",
        "..",
        "parent..escape",
        "slash/name",
        "back\\slash",
        "null\x00byte",
        "ctrl\x07char",
        "del\x7fchar",
        ".rename_tmp_secret",
    ):
        with pytest.raises(ValueError):
            srv.validate_issue_name(bad_name)

    # validate_calendar_date (due_date)
    for empty_dt in (None, "", "none", "null", "clear"):
        assert srv.validate_calendar_date(empty_dt, "due_date") is None
    assert srv.validate_calendar_date("2026-11-24", "due_date") == "2026-11-24"
    with pytest.raises(ValueError, match="Expected YYYY-MM-DD"):
        srv.validate_calendar_date("11/24/2026", "due_date")
    with pytest.raises(ValueError, match="Invalid calendar due_date"):
        srv.validate_calendar_date("2026-02-30", "due_date")

    # validate_story_points
    for empty_pts in (None, "", "none", "null", "clear"):
        assert srv.validate_story_points(empty_pts) is None
    assert srv.validate_story_points("3.456") == 3.46
    assert srv.validate_story_points(0) == 0.0
    for bad_pts in ("abc", -1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            srv.validate_story_points(bad_pts)

    # validate_estimate_minutes & parse_duration_minutes
    for empty_est in (None, "", "none", "null", "clear"):
        assert srv.validate_estimate_minutes(empty_est) is None
    assert srv.validate_estimate_minutes("1h 30m") == 90
    assert srv.validate_estimate_minutes("45") == 45
    assert srv.validate_estimate_minutes(0) == 0
    for bad_est in ("2.5", "not-an-int", -10):
        with pytest.raises(ValueError):
            srv.validate_estimate_minutes(bad_est)

    assert srv.parse_duration_minutes(90, None) == 90
    assert srv.parse_duration_minutes(None, "1w 2d 3h 30m") == 3570
    assert srv.parse_duration_minutes(None, "1.5h") == 90
    assert srv.parse_duration_minutes(None, "45m") == 45
    assert srv.parse_duration_minutes(None, "90") == 90
    for bad_min, bad_dur in (
        ("abc", None),
        (0, None),
        (-5, None),
        (None, None),
        (None, "   "),
        (None, "0"),
        (None, "-20"),
        (None, "invalid_tokens"),
        (None, "1h trailing_junk"),
        (None, "0h 0m"),
    ):
        with pytest.raises(ValueError):
            srv.parse_duration_minutes(bad_min, bad_dur)

    # Filename & MIME helpers
    assert srv.sanitize_filename(None) == ("attachment.bin", "attachment.bin")
    assert srv.sanitize_filename("..") == ("attachment.bin", "attachment.bin")
    assert srv.sanitize_filename("...")[1] == "attachment.bin"
    assert srv.sanitize_filename("nested\\dir/report final.txt")[0] == "report final.txt"
    assert srv.is_image_file("x.bin", "image/png") is True
    assert srv.is_image_file("diagram.webp", "") is True
    assert srv.is_image_file("archive.zip", "application/zip") is False
    assert srv.is_text_previewable("data.json", "application/json") is True
    assert srv.is_text_previewable("notes.md", "") is True
    assert srv.is_text_previewable("blob.bin", "application/octet-stream") is False

    # User/root resolution, git-root checks, config backfill & arg parser
    git_repo = init_git_repo(tmp_path / "git_root_check")
    sub_dir = git_repo / "nested" / "sub"
    sub_dir.mkdir(parents=True)
    plain_dir = tmp_path / "not_a_git_repo"
    plain_dir.mkdir()

    monkeypatch.setenv("ITRACK_USER", "EnvOverrideUser")
    assert srv.detect_default_user(git_repo) == "EnvOverrideUser"
    monkeypatch.delenv("ITRACK_USER", raising=False)
    assert srv.detect_default_user(git_repo) == "iTrack Test Harness"
    assert isinstance(srv.detect_default_user(plain_dir), str)

    assert srv.resolve_raw_root_arg(str(git_repo)) == git_repo
    monkeypatch.setenv("ITRACK_ROOT", str(git_repo))
    assert srv.resolve_raw_root_arg(None) == git_repo
    monkeypatch.delenv("ITRACK_ROOT", raising=False)
    assert srv.resolve_raw_root_arg(None) == Path.cwd()

    assert srv.check_and_resolve_git_root(git_repo) == git_repo.resolve()

    err_plain = io.StringIO()
    with contextlib.redirect_stderr(err_plain), pytest.raises(SystemExit) as exc_plain:
        srv.check_and_resolve_git_root(plain_dir)
    assert exc_plain.value.code == 2

    err_sub = io.StringIO()
    with contextlib.redirect_stderr(err_sub), pytest.raises(SystemExit) as exc_sub:
        srv.check_and_resolve_git_root(sub_dir)
    assert exc_sub.value.code == 2
    assert "Hint: Run from repository root" in err_sub.getvalue()

    fallback_cfg = srv.default_config_dict(prefix="999", title="   ")
    assert fallback_cfg["project_prefix"] == "ITR"
    assert fallback_cfg["project_title"] == "iTrack Workspace"

    init_repo = init_git_repo(tmp_path / "idempotent_init_repo")
    first_init = srv.init_workspace_on_disk(init_repo, prefix="COV", title="Coverage Init")
    assert first_init["already_initialized"] is False
    cfg_path = init_repo / "itrack-issues" / "config.json"
    cfg_doc = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg_doc.pop("wip_limits", None)
    cfg_path.write_text(json.dumps(cfg_doc), encoding="utf-8")
    second_init = srv.init_workspace_on_disk(init_repo, prefix="COV", title="Coverage Init")
    assert second_init["already_initialized"] is True
    assert "wip_limits" in second_init["config"]

    parsed_cli = srv.build_arg_parser().parse_args(["--root", str(init_repo), "--port", "0", "--init", "--prefix", "COV"])
    assert parsed_cli.init is True and parsed_cli.prefix == "COV"


def test_iql_lexer_parser_compiler_date_macros_empty_and_negative_branches(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
) -> None:
    """Exercise `itrack-server.py` L712-1272 date macros, operators, `IS [NOT] EMPTY`, custom fields & negative errors."""
    ws, by_key = seeded_scale_ws
    srv = _SRV_MOD

    # 1. Direct resolve_iql_date unit coverage (date_only=True & False + errors)
    for macro in ("now", "today", "yesterday", "tomorrow", "+7d", "-3d", "-2w", "-12h", "-30m", "2026-11-24", "2026-11-24T10:30:00Z", "2026-11-24 10:30:00+00:00"):
        d_only = srv.resolve_iql_date(macro, date_only=True)
        dt_full = srv.resolve_iql_date(macro, date_only=False)
        assert len(d_only) == 10 and "T" in dt_full
    for bad_dt in ("2026-02-30", "not-a-date-macro"):
        with pytest.raises(srv.IQLError):
            srv.resolve_iql_date(bad_dt, date_only=True)

    # 2. Lexer escapes, `<>`, FTS match builder, Parser & Compiler edge/error branches
    escaped_ast = srv.parse_iql(r'description ~ "line1\nline2\t\"quoted\"\\end" AND state <> archived ORDER BY story_points DESC, name ASC')
    assert escaped_ast["order_by"] == [
        {"field": "story_points", "direction": "DESC"},
        {"field": "name", "direction": "ASC"},
    ]
    only_order_ast = srv.parse_iql("ORDER BY priority ASC, key DESC")
    assert only_order_ast["where"] is None and len(only_order_ast["order_by"]) == 2
    assert srv.parse_iql("assignee IS NULL AND description IS NOT NULL")["where"]["type"] == "and"

    assert srv.build_fts_match_expr("   ") is None
    assert srv.build_fts_match_expr('"" !!!') is None
    assert "name :" in (srv.build_fts_match_expr('"Scale Epic" alpha-beta', column="name") or "")

    for invalid_iql in (
        "priority = $bad",
        'name = "unterminated',
        "cf. = prod",
        "state = todo trailing_word",
        "ORDER BY ()",
        "ORDER BY tags",
        "()",
        "state = (todo)",
        "AND = 1",
        "assignee IS UNKNOWN",
        "state todo",
        "category = bad_cat",
        "category IN (task, bad_cat)",
        "state = bad_state",
        "priority = ultra",
        "severity = sev9",
        "resolution = bad_res",
        "points = not_a_num",
        "blocked = maybe",
        "overdue = nope",
        "archived = invalid_bool",
        "due = bad_macro",
    ):
        with pytest.raises(srv.IQLError):
            srv.parse_iql(invalid_iql)

    comp_no_active = srv.IQLCompiler(current_user="alice", active_sprint_ids=[])
    assert comp_no_active._compile_cmp("sprint", "=", "active") == ("1 = 0", [])
    assert comp_no_active._compile_cmp("sprint", "!=", "active") == ("1 = 1", [])
    assert "DESC" in comp_no_active.compile_order_by([], sort_field="-priority")
    assert "story_points" in comp_no_active.compile_order_by(escaped_ast["order_by"])
    with pytest.raises(srv.IQLError):
        comp_no_active.compile_where({"type": "unknown_ast"})
    with pytest.raises(srv.IQLError):
        comp_no_active.compile_order_by([], sort_field="priority", sort_order="sideways")
    with pytest.raises(srv.IQLError):
        comp_no_active.compile_order_by([], sort_field="nonexistent_sort")
    for bad_cmp in (
        ("state", ">", "todo"),
        ("category", "~", "bug"),
        ("custom.env", "??", "prod"),
        ("tags", "??", "sec"),
    ):
        with pytest.raises(srv.IQLError):
            comp_no_active._compile_cmp(*bad_cmp)

    # 3. Live daemon iQL execution across all date macros, operators, custom fields, and IS [NOT] EMPTY
    base_url = ws.ensure_server()
    with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
        def iql_keys(expr: str) -> set[str]:
            r = client.get("/api/v1/search", params={"iql": expr, "page_size": 100})
            assert r.status_code == 200, f"iQL failed ({expr}): {r.text}"
            return {item["key"] for item in r.json()["issues"]}

        # Date macros & all 6 comparison operators on date/timestamp fields
        for date_expr in (
            "due = today",
            "due != yesterday",
            "due <= tomorrow",
            "due <= +7d",
            "due > 2020-01-01 AND due < 2099-12-31 AND due >= 2020-01-15",
            "created >= -3d AND created <= +7d",
            "updated >= -2w AND updated != 2000-01-01T00:00:00Z",
            "created >= -12h AND created > 2000-01-01",
            "created >= -30m AND updated < +7d",
        ):
            res_set = iql_keys(date_expr)
            assert isinstance(res_set, set)

        assert "ITR-1" in iql_keys("created >= -12h")
        assert "ITR-2" in iql_keys("due > tomorrow")

        # Numeric operators (=, !=, <, <=, >, >=) on story_points, estimate_minutes, logged_minutes
        assert "ITR-1" in iql_keys("story_points = 13 AND story_points != 5 AND story_points >= 13 AND story_points <= 13")
        assert len(iql_keys("story_points < 5 AND story_points > 0.5")) >= 5
        assert "ITR-1" in iql_keys("estimate_minutes = 240 AND estimate_minutes != 60 AND estimate_minutes >= 120 AND estimate_minutes > 120")
        assert len(iql_keys("estimate_minutes <= 60 AND estimate_minutes < 120")) >= 10
        assert "ITR-1" in iql_keys("logged_minutes = 90 AND logged_minutes != 0 AND logged_minutes >= 90 AND logged_minutes > 45")
        assert len(iql_keys("logged_minutes < 90 AND logged_minutes <= 45")) >= 50

        # Boolean predicates (`is_blocked`, `is_overdue`, `archived`)
        unblocked = iql_keys("is_blocked = false")
        assert unblocked == iql_keys("is_blocked != true") and len(unblocked) >= 40
        not_overdue = iql_keys("is_overdue = false")
        assert not_overdue == iql_keys("is_overdue != true") and len(not_overdue) >= 40
        assert len(iql_keys("is_overdue = true")) >= 15
        arch_true = iql_keys("archived = true")
        arch_not = iql_keys("archived != true")
        assert "ITR-80" in arch_true and len(arch_true) + len(arch_not) == len(by_key)

        # Custom fields, tags, components, state/assignee/resolution/sprint/milestone/text/generic cmp
        assert "ITR-1" in iql_keys("cf.env = prod AND cf.env != staging AND cf.env ~ pr AND cf.env !~ dev")
        assert "ITR-2" in iql_keys("tags != legacy AND tags !~ wip")
        assert "ITR-1" in iql_keys("components = auth-service AND components != ui-web AND components ~ auth AND components !~ legacy")
        assert "ITR-1" in iql_keys("state ~ in-progress AND state !~ in-qa AND priority = 5 AND severity = 5")
        assert len(iql_keys("resolution = none")) + len(iql_keys("resolution != none")) == len(by_key)
        assert len(iql_keys("resolution = fixed")) >= 1 and len(iql_keys("resolution != fixed")) >= 50
        assert len(iql_keys("assignee != alice AND assignee !~ charlie")) >= 30
        assert "ITR-1" in iql_keys("assignee ~ ali AND reporter ~ rep_ali")
        assert len(iql_keys("assignee = unassigned")) + len(iql_keys("assignee != unassigned")) == len(by_key)
        assert isinstance(iql_keys("assignee = @me"), set)
        assert len(iql_keys("sprint != active")) >= 40
        assert len(iql_keys("sprint = backlog")) + len(iql_keys("sprint != backlog")) == len(by_key)
        assert len(iql_keys("sprint != SPR-1")) >= 40
        assert len(iql_keys("milestone = none")) + len(iql_keys("milestone != none")) == len(by_key)
        assert len(iql_keys("milestone != MIL-1")) >= 40
        assert "ITR-1" in iql_keys('text IN ("QuantumZephyrTitleToken", "NonexistentToken")')
        assert len(iql_keys('text !~ "QuantumZephyrTitleToken"')) == 79
        assert iql_keys('text ~ "!!!"') == set()
        assert iql_keys('name = "Scale Story Beta"') == {"ITR-2"}
        assert len(iql_keys('name != "Scale Story Beta"')) == 79
        assert iql_keys('name ~ "!!!"') == set()
        assert "ITR-1" in iql_keys("key = ITR-1 AND key != ITR-2 AND key ~ ITR AND key !~ ZZZ")
        assert len(iql_keys("parent != ITR-1 AND key > ITR-10")) >= 10

        # IS EMPTY & IS NOT EMPTY across all 13 target fields (partitioning all 80 issues!)
        for empty_field in (
            "sprint",
            "milestone",
            "resolution",
            "parent",
            "due_date",
            "story_points",
            "estimate_minutes",
            "logged_minutes",
            "tags",
            "components",
            "comments",
            "description",
            "cf.env",
        ):
            emp = iql_keys(f"{empty_field} IS EMPTY")
            non_emp = iql_keys(f"{empty_field} IS NOT EMPTY")
            assert emp.isdisjoint(non_emp), f"Overlap on {empty_field} IS [NOT] EMPTY"
            assert len(emp) + len(non_emp) == len(by_key), f"Incomplete partition for {empty_field}"

        # Live 400 validation errors on negative iQL expressions
        for bad_live_iql in (
            "category = nope",
            "state IN (todo, bad_state)",
            "priority = super",
            "severity = sev0",
            "resolution = invalid_res",
            "points = abc",
            "blocked = maybe",
            "due = invalid_macro",
            "category ~ bug",
            "state > todo",
        ):
            bad_resp = client.get("/api/v1/search", params={"iql": bad_live_iql})
            assert bad_resp.status_code == 400, f"Expected 400 for {bad_live_iql}"


def test_reindex_self_heal_corrupt_sqlite_and_sequence_resync(
    clean_ws: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Exercise `_rebuild_index_unlocked` (L1719-1836) & SQLite self-heal (L1313-1595) across deleted_attachments,
    worklogs, comment embeds/attachments, folder-name drift, hidden/corrupt dirs, and sequence counter resync."""
    srv = _SRV_MOD

    # 1. Seed agile artifacts + rich issues on clean_ws
    _cli_json(clean_ws, "sprint", "create", "Reindex Sprint")
    _cli_json(clean_ws, "milestone", "create", "Reindex Milestone")
    _cli_json(clean_ws, "view", "create", "Reindex View", "--query", "state != archived")

    i1, i2 = _seed_issues_api(
        clean_ws,
        [
            {"name": "Reindex Rich Issue", "state": "in-progress", "priority": "high"},
            {"name": "Reindex Canonical Folder", "state": "todo", "priority": "medium"},
        ],
    )
    _cli_json(clean_ws, "link", "add", i1["key"], "blocks", i2["key"])
    _cli_json(clean_ws, "worklog", "add", i1["key"], "45m", "--author", "alice")

    att_del_path, _ = make_text_file(tmp_path / "to_delete_reindex.txt", text="temp payload\n")
    emb_png_path, _ = make_png(tmp_path / "desc_embed_reindex.png", width=16, height=16)
    cmt_emb_path, _ = make_png(tmp_path / "cmt_embed_reindex.png", width=16, height=16)
    cmt_att_path, _ = make_text_file(tmp_path / "cmt_attach_reindex.log", text="comment log\n")

    base_url = clean_ws.ensure_server()
    with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
        r_att = client.post(
            f"/api/v1/issues/{i1['key']}/attachments",
            files=[("files", ("to_delete_reindex.txt", att_del_path.read_bytes(), "text/plain"))],
        )
        assert r_att.status_code == 201
        del_att_id = r_att.json()["attachments"][0]["id"]

        r_emb = client.post(
            f"/api/v1/issues/{i1['key']}/embeds",
            files=[("files", ("desc_embed_reindex.png", emb_png_path.read_bytes(), "image/png"))],
        )
        assert r_emb.status_code == 201

        r_cmt = client.post(
            f"/api/v1/issues/{i1['key']}/comments",
            data={"body": "Comment with ![e](embed:cmt_embed_reindex.png)", "linked_attachment_ids": del_att_id},
            files=[
                ("embeds", ("cmt_embed_reindex.png", cmt_emb_path.read_bytes(), "image/png")),
                ("attachments", ("cmt_attach_reindex.log", cmt_att_path.read_bytes(), "text/plain")),
            ],
        )
        assert r_cmt.status_code == 201

        # Delete whole-issue attachment so `deleted_attachments` is populated on ITR-1
        assert client.delete(f"/api/v1/issues/{i1['key']}/attachments/{del_att_id}").status_code == 200

        # Mutate ITR-2's issue.json name & seq on disk to trigger folder-name self-heal & seq regex fallback
        i2_json_path = clean_ws.issue_dir("Reindex Canonical Folder") / "issue.json"
        i2_doc = json.loads(i2_json_path.read_text(encoding="utf-8"))
        i2_doc["name"] = "Slightly Drifted Name Inside JSON"
        i2_doc["seq"] = 0
        i2_json_path.write_text(json.dumps(i2_doc, indent=2), encoding="utf-8")

        # Add hidden `.tmp` dir, dir without issue.json, malformed `bad_folder/issue.json`, and keyless JSON dir
        (clean_ws.issues_dir / ".tmp_reindex_ignored").mkdir(parents=True, exist_ok=True)
        (clean_ws.issues_dir / "folder_without_json").mkdir(parents=True, exist_ok=True)
        bad_dir = clean_ws.issues_dir / "bad_folder"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "issue.json").write_text("{corrupt_json: [unclosed", encoding="utf-8")
        keyless_dir = clean_ws.issues_dir / "keyless_folder"
        keyless_dir.mkdir(parents=True, exist_ok=True)
        (keyless_dir / "issue.json").write_text('{"name": "No Key Here"}', encoding="utf-8")

        # Lower sequence counters in config.json to 1 so reindex resyncs all 8 monotonic counters
        cfg_path = clean_ws.issues_root / "config.json"
        cfg_raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        for seq_k in (
            "next_issue_seq",
            "next_att_seq",
            "next_comment_seq",
            "next_link_seq",
            "next_worklog_seq",
            "next_sprint_seq",
            "next_milestone_seq",
            "next_view_seq",
        ):
            cfg_raw[seq_k] = 1
        cfg_path.write_text(json.dumps(cfg_raw, indent=2), encoding="utf-8")

        re_resp = client.post("/api/v1/reindex")
        assert re_resp.status_code == 200
        re_body = re_resp.json()
        assert re_body["status"] == "reindexed" and re_body["indexed_issues"] == 2

        healed_i2 = _read_disk_issue_json(clean_ws, "Reindex Canonical Folder")
        assert healed_i2["name"] == "Reindex Canonical Folder"
        assert healed_i2["folder"] == "Reindex Canonical Folder"

        healed_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert healed_cfg["next_issue_seq"] >= 3
        assert healed_cfg["next_att_seq"] >= 5
        assert healed_cfg["next_comment_seq"] >= 2
        assert healed_cfg["next_link_seq"] >= 2
        assert healed_cfg["next_worklog_seq"] >= 2
        assert healed_cfg["next_sprint_seq"] >= 2
        assert healed_cfg["next_milestone_seq"] >= 2
        assert healed_cfg["next_view_seq"] >= 2

        # Verify blocking link & deleted linked attachment enrichment survived reindex
        i2_live = client.get(f"/api/v1/issues/{i2['key']}").json()
        assert i2_live["is_blocked"] is True and i1["key"] in i2_live["unresolved_blockers"]
        i1_live = client.get(f"/api/v1/issues/{i1['key']}").json()
        assert i1_live["comments"][0]["linked_attachments"][0]["deleted"] is True

    # 2. Direct StorageEngine self-heal on corrupt SQLite + stray root index.sqlite + uninitialized 503
    uninit_dir = init_git_repo(tmp_path / "uninit_engine_repo")
    uninit_eng = srv.StorageEngine(uninit_dir)
    assert uninit_eng.is_initialized() is False
    assert uninit_eng.issue_count() == 0
    with pytest.raises(srv.HTTPException) as exc_503:
        uninit_eng.require_initialized()
    assert exc_503.value.status_code == 503

    corrupt_repo = init_git_repo(tmp_path / "corrupt_sqlite_repo")
    srv.init_workspace_on_disk(corrupt_repo)
    eng = srv.StorageEngine(corrupt_repo)
    try:
        created_direct = eng.create_issue({"name": "Corrupt Recovery Issue", "state": "todo"})
        eng.close()
        # Write stray root sqlite + corrupt .runtime/index.sqlite bytes
        (corrupt_repo / "itrack-issues" / "index.sqlite").write_bytes(b"stray")
        (corrupt_repo / "itrack-issues" / ".runtime" / "index.sqlite").write_bytes(b"not a valid sqlite3 file!")

        status_after = eng.get_status()
        assert status_after["total_issues"] == 1
        assert not (corrupt_repo / "itrack-issues" / "index.sqlite").exists()
        assert eng.get_issue(f"#{created_direct['key']}")["key"] == created_direct["key"]
        with pytest.raises(srv.HTTPException) as exc_404:
            eng.get_issue("   ")
        assert exc_404.value.status_code == 404
    finally:
        eng.close()


def test_api_issues_filters_tree_and_config_exhaustive(
    seeded_scale_ws: tuple[ITrackWorkspace, dict[str, dict[str, Any]]],
    clean_ws: ITrackWorkspace,
) -> None:
    """Exercise `itrack-server.py` L2080-2720 `GET /api/v1/issues`, `GET /api/v1/tree`, and `PATCH /api/v1/config`."""
    scale_ws, by_key = seeded_scale_ws

    # 1. Read-only `GET /api/v1/issues` & `GET /api/v1/tree` against 80-issue scale workspace
    with httpx.Client(base_url=scale_ws.ensure_server(), timeout=15.0, trust_env=False) as sclient:
        r_multi = sclient.get(
            "/api/v1/issues",
            params={
                "milestone": "MIL-1",
                "severity": "sev1,sev2",
                "priority": "critical,high",
                "category": "epic,bug,story",
                "tag": "security,frontend",
                "component": "auth-service,ui-web",
                "page_size": 100,
            },
        )
        assert r_multi.status_code == 200 and r_multi.json()["total"] >= 2

        r_rep = sclient.get("/api/v1/issues", params={"reporter": "rep_alice", "overdue": "true", "page_size": 100})
        assert r_rep.status_code == 200
        assert {i["key"] for i in r_rep.json()["issues"]} == {
            k for k, v in by_key.items() if v["reporter"] == "rep_alice" and v["is_overdue"]
        }

        r_not_ov = sclient.get("/api/v1/issues", params={"overdue": "false", "resolution": "fixed", "page_size": 100})
        assert r_not_ov.status_code == 200
        assert {i["key"] for i in r_not_ov.json()["issues"]} == {
            k for k, v in by_key.items() if (not v["is_overdue"]) and v["resolution"] == "fixed"
        }

        r_par = sclient.get("/api/v1/issues", params={"parent": "ITR-1", "page_size": 100})
        assert r_par.status_code == 200 and "ITR-3" in {i["key"] for i in r_par.json()["issues"]}

        r_par_none = sclient.get("/api/v1/issues", params={"parent": "none", "page_size": 100})
        assert r_par_none.status_code == 200 and "ITR-1" in {i["key"] for i in r_par_none.json()["issues"]}

        r_par_miss = sclient.get("/api/v1/issues", params={"parent": "ITR-9999"})
        assert r_par_miss.status_code == 200 and r_par_miss.json()["issues"] == []

        # Auto-detected structured iQL inside `q` vs fallback to FTS on invalid iQL syntax
        r_q_iql = sclient.get("/api/v1/issues", params={"q": "priority = critical AND sprint = SPR-1 ORDER BY key ASC"})
        assert r_q_iql.status_code == 200 and "ITR-1" in [i["key"] for i in r_q_iql.json()["issues"]]

        r_q_fallback = sclient.get("/api/v1/issues", params={"q": "QuantumZephyrTitleToken = = broken"})
        assert r_q_fallback.status_code == 200

        r_dash_sort = sclient.get("/api/v1/issues", params={"sort": "-priority", "page_size": 5})
        assert r_dash_sort.status_code == 200 and r_dash_sort.json()["issues"][0]["priority"] == "critical"

        # GET /api/v1/issues 400 error cases
        for bad_params in (
            {"page": 0},
            {"page_size": 0},
            {"page_size": 1001},
            {"tab": "not_a_tab"},
            {"iql": "bad_field = 1"},
            {"category": "not_a_cat"},
            {"sort": "not_sortable"},
            {"sort": "priority", "order": "sideways"},
        ):
            assert sclient.get("/api/v1/issues", params=bad_params).status_code == 400

        # GET /api/v1/tree & ?root=ITR-1 & 404
        r_tree_root = sclient.get("/api/v1/tree", params={"root": "ITR-1"})
        assert r_tree_root.status_code == 200
        assert r_tree_root.json()["root"] == "ITR-1"
        assert len(r_tree_root.json()["trees"][0]["children"]) >= 4
        assert sclient.get("/api/v1/tree", params={"root": "ITR-9999"}).status_code == 404
        assert sclient.get("/api/v1/board", params={"swimlane": "invalid_swimlane"}).status_code == 400

    # 2. Exhaustive `PATCH /api/v1/config` valid keys & 400 error branches on isolated `clean_ws`
    with httpx.Client(base_url=clean_ws.ensure_server(), timeout=15.0, trust_env=False) as cclient:
        valid_patches: list[dict[str, Any]] = [
            {"key": "project_prefix", "value": "PRJ2"},
            {"key": "project_title", "value": "Configured Project Title"},
            {"default_state": "todo", "default_priority": "high", "default_category": "bug", "default_severity": "sev2"},
            {"default_assignee": "alice"},
            {"default_assignee": "none"},
            {"wip_limits": {"in-progress": 4, "todo": 0, "ready": None}},
            {"key": "wip_limits.in-review", "value": 2},
            {"key": "wip_limits.in-review", "value": "0"},
            {"preferences": {"dense_rows": True}},
            {"key": "preferences.theme", "value": "light"},
            {"key": "preferences.page_size", "value": 50},
            {"key": "preferences.default_swimlane", "value": "priority"},
        ]
        for body in valid_patches:
            resp = cclient.patch("/api/v1/config", json=body)
            assert resp.status_code == 200, f"Valid config patch failed ({body}): {resp.text}"

        final_cfg = cclient.get("/api/v1/config").json()
        assert final_cfg["project_prefix"] == "PRJ2"
        assert final_cfg["project_title"] == "Configured Project Title"
        assert final_cfg["default_state"] == "todo"
        assert final_cfg["default_priority"] == "high"
        assert final_cfg["default_category"] == "bug"
        assert final_cfg["default_severity"] == "sev2"
        assert final_cfg["default_assignee"] is None
        assert final_cfg["wip_limits"] == {"in-progress": 4}
        assert final_cfg["preferences"]["theme"] == "light"
        assert final_cfg["preferences"]["page_size"] == 50
        assert final_cfg["preferences"]["default_swimlane"] == "priority"

        invalid_patches: list[dict[str, Any]] = [
            {},
            {"key": "   ", "value": "x"},
            {"project_prefix": "123BAD"},
            {"project_prefix": ""},
            {"project_title": "   "},
            {"default_state": "invalid_state"},
            {"default_priority": "invalid_prio"},
            {"default_category": "invalid_cat"},
            {"default_severity": "invalid_sev"},
            {"wip_limits": "not-an-object"},
            {"wip_limits": {"bad_state": 3}},
            {"wip_limits": {"todo": "abc"}},
            {"wip_limits": {"todo": -1}},
            {"wip_limits.bad_state": 2},
            {"wip_limits.todo": "abc"},
            {"wip_limits.todo": -3},
            {"preferences": "not-an-object"},
            {"preferences.": "val"},
            {"preferences.theme": "solarized"},
            {"preferences.page_size": 0},
            {"preferences.page_size": 501},
            {"preferences.page_size": "abc"},
            {"preferences.default_swimlane": "invalid_lane"},
            {"unknown_readonly_key": 42},
        ]
        for bad_body in invalid_patches:
            r_bad = cclient.patch("/api/v1/config", json=bad_body)
            assert r_bad.status_code == 400, f"Expected 400 for invalid config patch {bad_body}, got {r_bad.status_code}"


def test_api_issue_patch_mutations_context_and_unified_attachment_deletes(
    clean_ws: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Exercise `itrack-server.py` L2930-3965 `PATCH /api/v1/issues/{ref}`, clone collisions, context markdown,
    and unified `DELETE /api/v1/issues/{ref}/attachments/{att_id}` across description embeds & comment files."""
    sp = _cli_json(clean_ws, "sprint", "create", "Patch Sprint")
    ms = _cli_json(clean_ws, "milestone", "create", "Patch Milestone")

    iss, parent_iss = _seed_issues_api(
        clean_ws,
        [
            {
                "name": "Rest Patch Primary Issue",
                "category": "story",
                "state": "in-progress",
                "priority": "high",
                "severity": "sev2",
                "resolution": "fixed",
                "description": "Initial description",
                "assignee": "alice",
                "reporter": "rep_alice",
                "tags": ["t1", "t2"],
                "components": ["c1", "c2"],
                "sprint": sp["id"],
                "milestone": ms["id"],
                "story_points": 5,
                "estimate_minutes": 90,
                "due_date": "2026-12-31",
                "acceptance_criteria": [{"text": "Initial AC", "checked": False}, "- [x] Prechecked AC"],
                "custom_fields": {"k0": "v0", "k2": "to_remove"},
            },
            {"name": "Rest Patch Parent Epic", "category": "epic"},
        ],
    )
    key = iss["key"]
    _cli_json(clean_ws, "link", "add", parent_iss["key"], "parent-of", key)

    desc_emb1, _ = make_png(tmp_path / "patch_desc_emb1.png", width=16, height=16)
    desc_emb2, _ = make_png(tmp_path / "patch_desc_emb2.png", width=16, height=16)
    iss_att_file, _ = make_text_file(tmp_path / "patch_issue_att.txt", text="issue attachment text\n")
    cmt_emb_file, _ = make_png(tmp_path / "patch_cmt_emb.png", width=16, height=16)
    cmt_att_file, _ = make_text_file(tmp_path / "patch_cmt_att.log", text="comment attachment log\n")

    with httpx.Client(base_url=clean_ws.ensure_server(), timeout=15.0, trust_env=False) as client:
        # 1. Rename via PATCH (new name, case-only rename, and no-op identical name)
        r_ren = client.patch(f"/api/v1/issues/{key}", json={"name": "Rest Patch Renamed Issue"})
        assert r_ren.status_code == 200 and r_ren.json()["name"] == "Rest Patch Renamed Issue"
        r_case = client.patch(f"/api/v1/issues/{key}", json={"name": "rest patch renamed issue"})
        assert r_case.status_code == 200 and r_case.json()["name"] == "rest patch renamed issue"
        r_same = client.patch(f"/api/v1/issues/{key}", json={"name": "rest patch renamed issue"})
        assert r_same.status_code == 200

        # 2. Archive & unarchive via PATCH state transitions
        r_arch = client.patch(f"/api/v1/issues/{key}", json={"state": "archived"})
        assert r_arch.status_code == 200 and r_arch.json()["archived"] is True
        r_unarch = client.patch(f"/api/v1/issues/{key}", json={"state": "todo"})
        assert r_unarch.status_code == 200 and r_unarch.json()["archived"] is False

        # 3. Incremental tags, components, AC, custom_fields & set_fields
        r_mut = client.patch(
            f"/api/v1/issues/{key}",
            json={
                "add_tags": ["t3", "t4"],
                "remove_tags": ["t1"],
                "add_components": ["c3"],
                "remove_components": ["c1"],
                "add_ac": ["[x] Checked Added AC", "Unchecked Added AC"],
                "edit_ac": {"1": "Edited Initial AC"},
                "check_ac": ["4"],
                "uncheck_ac": ["2"],
                "remove_ac": ["3"],
                "custom_fields": {"k1": "v1", "k2": None},
                "set_fields": {"k3": "v3"},
            },
        )
        assert r_mut.status_code == 200
        mut_doc = r_mut.json()
        assert mut_doc["tags"] == ["t2", "t3", "t4"]
        assert mut_doc["components"] == ["c2", "c3"]
        assert [a["text"] for a in mut_doc["acceptance_criteria"]] == [
            "Edited Initial AC",
            "Prechecked AC",
            "Unchecked Added AC",
        ]
        assert [a["checked"] for a in mut_doc["acceptance_criteria"]] == [False, False, True]
        assert mut_doc["custom_fields"] == {"k0": "v0", "k1": "v1", "k3": "v3"}

        # Custom field unset, replace, empty-dict clear, and clear_* flags
        r_cf2 = client.patch(f"/api/v1/issues/{key}", json={"unset_fields": ["k0"]})
        assert r_cf2.status_code == 200 and "k0" not in r_cf2.json()["custom_fields"]

        r_cf3 = client.patch(
            f"/api/v1/issues/{key}",
            json={"replace_custom_fields": True, "custom_fields": {"replaced_key": "replaced_val"}},
        )
        assert r_cf3.status_code == 200 and r_cf3.json()["custom_fields"] == {"replaced_key": "replaced_val"}

        r_cf4 = client.patch(f"/api/v1/issues/{key}", json={"custom_fields": {}})
        assert r_cf4.status_code == 200 and r_cf4.json()["custom_fields"] == {}

        r_clears = client.patch(
            f"/api/v1/issues/{key}",
            json={
                "clear_resolution": True,
                "clear_description": True,
                "clear_reporter": True,
                "clear_tags": True,
                "clear_components": True,
                "parent": "none",
                "sprint": "backlog",
                "milestone": "none",
                "clear_points": True,
                "clear_estimate": True,
                "clear_due": True,
                "clear_ac": True,
                "clear_fields": True,
            },
        )
        assert r_clears.status_code == 200
        cl_doc = r_clears.json()
        assert cl_doc["resolution"] is None and cl_doc["description"] == "" and cl_doc["reporter"] is None
        assert cl_doc["tags"] == [] and cl_doc["components"] == []
        assert cl_doc["sprint"] is None and cl_doc["milestone"] is None
        assert cl_doc["story_points"] is None and cl_doc["estimate_minutes"] is None and cl_doc["due_date"] is None
        assert cl_doc["acceptance_criteria"] == []

        # PATCH validation 400 errors (bad custom_fields, bad acceptance_criteria, empty AC text, missing AC ref)
        for bad_patch in (
            {"custom_fields": "not-a-dict"},
            {"acceptance_criteria": "not-a-list"},
            {"add_ac": ["Temp AC"], "edit_ac": {"1": "   "}},
            {"check_ac": ["AC-999"]},
        ):
            assert client.patch(f"/api/v1/issues/{key}", json=bad_patch).status_code == 400

        # 4. Clone twice with default naming (`Copy of ...` and `Copy 2 of ...`)
        cl1 = client.post(f"/api/v1/issues/{parent_iss['key']}/clone", json={})
        cl2 = client.post(f"/api/v1/issues/{parent_iss['key']}/clone", json={})
        assert cl1.status_code == 201 and cl1.json()["name"].startswith("Copy of ")
        assert cl2.status_code == 201 and cl2.json()["name"].startswith("Copy 2 of ")

        # 5. Description embeds, comment embeds/attachments, context pack, and unified attachment deletes
        assert client.post(f"/api/v1/issues/{key}/embeds", data={"actor": "alice"}).status_code == 400
        assert client.post(f"/api/v1/issues/{key}/attachments", data={"actor": "alice"}).status_code == 400
        assert client.post(f"/api/v1/issues/{key}/comments", json={}).status_code == 400

        r_e1 = client.post(
            f"/api/v1/issues/{key}/embeds",
            files=[("files", ("patch_desc_emb1.png", desc_emb1.read_bytes(), "image/png"))],
        )
        r_e2 = client.post(
            f"/api/v1/issues/{key}/embeds",
            files=[("files", ("patch_desc_emb2.png", desc_emb2.read_bytes(), "image/png"))],
        )
        assert r_e1.status_code == 201 and r_e2.status_code == 201
        desc_embed_id = r_e1.json()["embeds"][0]["id"]
        desc_embed_id2 = r_e2.json()["embeds"][0]["id"]

        r_ia = client.post(
            f"/api/v1/issues/{key}/attachments",
            files=[("files", ("patch_issue_att.txt", iss_att_file.read_bytes(), "text/plain"))],
        )
        assert r_ia.status_code == 201
        iss_att_id = r_ia.json()["attachments"][0]["id"]

        r_cm = client.post(
            f"/api/v1/issues/{key}/comments",
            data={"body": "Embed ![pic](embed:patch_cmt_emb.png)", "linked_attachment_ids": iss_att_id},
            files=[
                ("embeds", ("patch_cmt_emb.png", cmt_emb_file.read_bytes(), "image/png")),
                ("attachments", ("patch_cmt_att.log", cmt_att_file.read_bytes(), "text/plain")),
            ],
        )
        assert r_cm.status_code == 201
        cm_json = r_cm.json()
        cid = cm_json["id"]
        comment_embed_id = cm_json["embeds"][0]["id"]
        comment_att_id = cm_json["attachments"][0]["id"]

        # JSON PATCH on comment (`application/json` branch)
        r_cm_patch = client.patch(
            f"/api/v1/issues/{key}/comments/{cid}",
            json={"body": cm_json["body"] + "\nUpdated via JSON PATCH", "actor": "bob"},
        )
        assert r_cm_patch.status_code == 200 and len(r_cm_patch.json()["edit_history"]) == 1

        # Delete issue attachment so linked attachment becomes `[DELETED]`, restore parent/AC, then verify context pack
        assert client.delete(f"/api/v1/issues/{key}/attachments/{iss_att_id}").status_code == 200
        assert client.patch(
            f"/api/v1/issues/{key}",
            json={"parent": parent_iss["key"], "add_ac": ["[x] Verified Context AC"], "custom_fields": {"ctx": "yes"}},
        ).status_code == 200
        ctx_resp = client.get(f"/api/v1/issues/{key}/context")
        assert ctx_resp.status_code == 200
        assert "[DELETED]" in ctx_resp.json()["markdown"]
        assert "## Acceptance Criteria" in ctx_resp.json()["markdown"]
        parent_ctx = client.get(f"/api/v1/issues/{parent_iss['key']}/context")
        assert parent_ctx.status_code == 200 and "## Subtasks" in parent_ctx.json()["markdown"]

        # Unified DELETE /api/v1/issues/{key}/attachments/{id} on description_embed, comment_embed & comment attachment!
        del_de = client.delete(f"/api/v1/issues/{key}/attachments/{desc_embed_id}")
        assert del_de.status_code == 200 and del_de.json()["scope"] == "description_embed"

        del_ce = client.delete(f"/api/v1/issues/{key}/attachments/{comment_embed_id}")
        assert del_ce.status_code == 200 and del_ce.json()["scope"] == "comment_embed"

        del_ca = client.delete(f"/api/v1/issues/{key}/attachments/{comment_att_id}")
        assert del_ca.status_code == 200 and del_ca.json()["scope"] == "comment"

        # Dedicated DELETE /embeds/{id} + 400/404 attachment errors
        assert client.delete(f"/api/v1/issues/{key}/embeds/{desc_embed_id2}").status_code == 200
        assert client.delete(f"/api/v1/issues/{key}/embeds/ATT-9999").status_code == 404
        assert client.get(f"/api/v1/issues/{key}/attachments", params={"scope": "bad_scope"}).status_code == 400
        assert client.get(f"/api/v1/issues/{key}/attachments/ATT-9999").status_code == 404
        assert client.get(f"/api/v1/issues/{key}/attachments/ATT-9999/download").status_code == 404


def test_api_agile_and_saved_views_validation_and_edge_branches(clean_ws: ITrackWorkspace) -> None:
    """Exercise `itrack-server.py` L4190-4966 Tag, Sprint, Milestone, Saved View, Worklog, Bulk & JSON validation branches."""
    (iss,) = _seed_issues_api(clean_ws, [{"name": "Agile Edge Issue", "state": "todo"}])
    key = iss["key"]

    with httpx.Client(base_url=clean_ws.ensure_server(), timeout=15.0, trust_env=False) as client:
        # Static SPA/favicon & malformed JSON body 400s
        assert client.get("/favicon.svg").status_code == 200
        assert client.get("/ui").status_code == 200
        assert client.post("/api/v1/tags", content=b"{invalid json", headers={"content-type": "application/json"}).status_code == 400
        assert client.post("/api/v1/tags", content=b"[1, 2, 3]", headers={"content-type": "application/json"}).status_code == 400

        # Worklog & Bulk edge errors
        assert client.delete(f"/api/v1/issues/{key}/worklogs/WL-9999").status_code == 404
        assert client.post("/api/v1/bulk/update", json={}).status_code == 400
        assert client.post("/api/v1/bulk/update", json={"issues": ["   "], "updates": {"state": "ready"}}).status_code == 400
        assert client.post("/api/v1/bulk/delete", json={}).status_code == 400

        # Tags: 400 / 404 / 409 & non-renaming PATCH
        assert client.post("/api/v1/tags", json={"name": "   "}).status_code == 400
        assert client.post("/api/v1/tags", json={"name": "alpha-tag", "color": "#3b82f6"}).status_code == 201
        assert client.post("/api/v1/tags", json={"name": "beta-tag", "color": "#10b981"}).status_code == 201
        assert client.post("/api/v1/tags", json={"name": "ALPHA-TAG"}).status_code == 409
        assert client.patch("/api/v1/tags/missing-tag", json={"description": "x"}).status_code == 404
        assert client.patch("/api/v1/tags/alpha-tag", json={"new_name": "   "}).status_code == 400
        assert client.patch("/api/v1/tags/alpha-tag", json={"new_name": "beta-tag"}).status_code == 409
        assert client.patch("/api/v1/tags/alpha-tag", json={"color": "not-hex"}).status_code == 400
        r_tag_desc = client.patch("/api/v1/tags/alpha-tag", json={"description": "Updated without rename"})
        assert r_tag_desc.status_code == 200 and r_tag_desc.json()["description"] == "Updated without rename"
        assert client.delete("/api/v1/tags/missing-tag").status_code == 404

        # Sprints: 400 / 404 / 409, start-already-active, complete-not-active, rollover errors, start-completed
        assert client.get("/api/v1/sprints", params={"state": "invalid_state"}).status_code == 400
        assert client.post("/api/v1/sprints", json={"name": ""}).status_code == 400
        assert client.post("/api/v1/sprints", json={"name": "S", "start_date": "bad-date"}).status_code == 400
        assert client.post("/api/v1/sprints", json={"name": "S", "start_date": "2026-12-10", "end_date": "2026-12-01"}).status_code == 400

        sp1 = client.post("/api/v1/sprints", json={"name": "Edge Sprint 1", "start_date": "2026-11-01", "end_date": "2026-11-14"}).json()
        sp2 = client.post("/api/v1/sprints", json={"name": "Edge Sprint 2"}).json()
        assert client.post("/api/v1/sprints", json={"name": "edge sprint 1"}).status_code == 409
        assert client.get("/api/v1/sprints/SPR-999").status_code == 404

        assert client.patch(f"/api/v1/sprints/{sp1['id']}", json={"name": "  "}).status_code == 400
        assert client.patch(f"/api/v1/sprints/{sp1['id']}", json={"start_date": "2026-99-99"}).status_code == 400
        assert client.patch(f"/api/v1/sprints/{sp1['id']}", json={"start_date": "2026-12-20", "end_date": "2026-12-01"}).status_code == 400

        assert client.post(f"/api/v1/sprints/{sp1['id']}/start", json={"start_date": "bad"}).status_code == 400
        s1_start1 = client.post(f"/api/v1/sprints/{sp1['id']}/start", json={})
        assert s1_start1.status_code == 200 and s1_start1.json()["state"] == "active"
        # Start again while already active -> preserves started_at
        s1_start2 = client.post(f"/api/v1/sprints/{sp1['id']}/start", json={"end_date": "2026-11-20"})
        assert s1_start2.status_code == 200 and s1_start2.json()["started_at"] == s1_start1.json()["started_at"]

        # Invalid rollover targets (same sprint -> 400, missing sprint -> 404)
        assert client.post(f"/api/v1/sprints/{sp1['id']}/complete", json={"rollover_to": sp1["id"]}).status_code == 400
        assert client.post(f"/api/v1/sprints/{sp1['id']}/complete", json={"rollover_to": "SPR-999"}).status_code == 404

        # Complete active sprint, then verify starting a completed sprint -> 400
        assert client.post(f"/api/v1/sprints/{sp1['id']}/complete", json={"rollover_to": "backlog"}).status_code == 200
        assert client.post(f"/api/v1/sprints/{sp1['id']}/start", json={}).status_code == 400

        # Complete planned (not-active) sprint sp2 & delete sprint
        assert client.post(f"/api/v1/sprints/{sp2['id']}/assign", json={"issues": []}).status_code == 400
        assert client.post(f"/api/v1/sprints/{sp2['id']}/remove", json={"issues": []}).status_code == 400
        assert client.post(f"/api/v1/sprints/{sp2['id']}/complete", json={}).status_code == 200
        assert client.delete(f"/api/v1/sprints/{sp2['id']}").status_code == 200
        assert client.delete("/api/v1/sprints/SPR-999").status_code == 404

        # Milestones: 400 / 404 / 409, close/reopen/not-found
        assert client.get("/api/v1/milestones", params={"state": "invalid"}).status_code == 400
        assert client.post("/api/v1/milestones", json={"name": "  "}).status_code == 400
        assert client.post("/api/v1/milestones", json={"name": "M", "due_date": "bad"}).status_code == 400
        ms1 = client.post("/api/v1/milestones", json={"name": "Edge Milestone 1", "due_date": "2026-12-31"}).json()
        assert client.post("/api/v1/milestones", json={"name": "edge milestone 1"}).status_code == 409
        assert client.patch(f"/api/v1/milestones/{ms1['id']}", json={"name": ""}).status_code == 400
        assert client.patch(f"/api/v1/milestones/{ms1['id']}", json={"due_date": "2026-13-40"}).status_code == 400
        assert client.post(f"/api/v1/milestones/{ms1['id']}/close").json()["state"] == "closed"
        assert client.post(f"/api/v1/milestones/{ms1['id']}/reopen").json()["state"] == "open"
        assert client.get("/api/v1/milestones/MIL-999").status_code == 404
        assert client.post("/api/v1/milestones/MIL-999/close").status_code == 404
        assert client.post("/api/v1/milestones/MIL-999/reopen").status_code == 404
        assert client.delete("/api/v1/milestones/MIL-999").status_code == 404

        # Saved Views: empty name, invalid tab / swimlane / order / query / sort (with & without query), missing 404
        for bad_view in (
            {"name": ""},
            {"name": "BadTab", "tab": "nonexistent_tab"},
            {"name": "BadSwimlane", "swimlane": "nonexistent_lane"},
            {"name": "BadOrder", "order": "diagonal"},
            {"name": "BadQuery", "query": "unknown_field = 1"},
            {"name": "BadSortWithQuery", "query": "state = todo", "sort": "unknown_sort"},
            {"name": "BadSortEmptyQuery", "query": "", "sort": "unknown_sort"},
        ):
            assert client.post("/api/v1/views", json=bad_view).status_code == 400, f"Expected 400 for {bad_view}"

        vw_ok = client.post("/api/v1/views", json={"name": "Empty Query Sorted View", "query": "", "sort": "priority", "order": "desc"})
        assert vw_ok.status_code == 201
        vid = vw_ok.json()["id"]
        assert client.post("/api/v1/views", json={"name": "empty query sorted view"}).status_code == 409

        for bad_vpatch in (
            {"name": "   "},
            {"tab": "bad_tab"},
            {"swimlane": "bad_lane"},
            {"order": "bad_order"},
            {"query": "invalid_field = 1"},
            {"query": "", "sort": "invalid_sort"},
        ):
            assert client.patch(f"/api/v1/views/{vid}", json=bad_vpatch).status_code == 400

        assert client.get("/api/v1/views/VIEW-999").status_code == 404
        assert client.delete("/api/v1/views/VIEW-999").status_code == 404

