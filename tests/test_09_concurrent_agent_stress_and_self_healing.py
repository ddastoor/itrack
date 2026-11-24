"""Suite 9: Concurrent Multi-Agent Torture, Rename-Under-Fire & Crash/Corruption Self-Healing."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import (  # noqa: E402
    CLIResult,
    ITrackWorkspace,
    _is_pid_running,
    find_itrack_daemons,
    make_pdf,
    make_png,
    make_text_file,
    run_itrack_cli,
    sha256_file,
)


def _find_unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_01_fifteen_process_cold_start_daemon_stampede(workspace: ITrackWorkspace) -> None:
    """15-Process Cold-Start Daemon Stampede:
    Stop server, launch 15 simultaneous CLI subprocesses via barrier/pool ->
    assert all 15 exit 0 with valid JSON and strictly 1 itrack-server.py daemon is running for ws.root.
    """
    ws = workspace
    ws.cli_json("issue", "create", "Stampede Seed Alpha", "--category", "task", "--priority", "high")
    ws.cli_json("issue", "create", "Stampede Seed Beta", "--category", "bug", "--priority", "critical")

    ws.stop_server()
    assert find_itrack_daemons(ws.root) == []
    assert not ws.daemon_json_path.exists()

    worker_count = 15
    barrier = threading.Barrier(worker_count, timeout=20.0)

    def _stampede_worker(idx: int) -> CLIResult:
        cmd_args = ("status", "--json") if idx % 2 == 0 else ("issue", "list", "--json")
        barrier.wait()
        return run_itrack_cli(*cmd_args, cwd=ws.root, expect_rc=None, timeout=45.0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_stampede_worker, i) for i in range(worker_count)]
        results = [f.result(timeout=60.0) for f in futures]

    for idx, res in enumerate(results):
        assert res.returncode == 0, (
            f"Cold-start worker {idx} exited with {res.returncode}!\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
        payload = res.json()
        if idx % 2 == 0:
            assert payload["initialized"] is True
            assert payload["total_issues"] == 2
        else:
            assert payload["total"] == 2

    daemons = find_itrack_daemons(ws.root)
    assert len(daemons) == 1, f"Expected strictly 1 daemon for {ws.root}, found: {daemons}"

    daemon_meta = json.loads(ws.daemon_json_path.read_text(encoding="utf-8"))
    assert int(daemon_meta["pid"]) == int(daemons[0]["pid"])
    assert _is_pid_running(int(daemon_meta["pid"]))


def test_02_twenty_four_process_parallel_issue_creation_swarm(workspace: ITrackWorkspace) -> None:
    """24-Process Parallel Issue Creation Swarm:
    Launch 24 simultaneous `issue create "Concurrent Task <i>" ... --json` subprocesses ->
    assert all 24 exit 0, keys form a gapless set ITR-1..ITR-24, 24 distinct folders with valid
    issue.json + history.jsonl exist on disk, and zero SQLite lock errors occur.
    """
    ws = workspace
    ws.ensure_server()

    worker_count = 24
    barrier = threading.Barrier(worker_count, timeout=20.0)

    def _create_worker(idx: int) -> CLIResult:
        name = f"Concurrent Task {idx:02d} With Spaces"
        prio = ("critical", "high", "medium", "low")[idx % 4]
        barrier.wait()
        return run_itrack_cli(
            "issue",
            "create",
            name,
            "--category",
            "task",
            "--priority",
            prio,
            "--assignee",
            f"agent-{idx % 6}",
            "--points",
            str((idx % 8) + 1),
            "--tag",
            "swarm-batch",
            "--json",
            cwd=ws.root,
            expect_rc=None,
            timeout=60.0,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_create_worker, i) for i in range(1, worker_count + 1)]
        results = [f.result(timeout=75.0) for f in futures]

    created_keys: set[str] = set()
    created_names: set[str] = set()
    for res in results:
        combined_err = res.stderr.lower()
        assert "database is locked" not in combined_err
        assert "operationalerror" not in combined_err
        assert res.returncode == 0, f"Worker failed: {res.args}\nSTDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}"
        item = res.json()
        created_keys.add(item["key"])
        created_names.add(item["name"])

    expected_keys = {f"ITR-{i}" for i in range(1, worker_count + 1)}
    expected_names = {f"Concurrent Task {i:02d} With Spaces" for i in range(1, worker_count + 1)}
    assert created_keys == expected_keys
    assert created_names == expected_names

    disk_dirs = {
        p.name for p in ws.issues_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    }
    assert disk_dirs == expected_names

    disk_keys: set[str] = set()
    for folder_name in sorted(disk_dirs):
        idir = ws.issue_dir(folder_name)
        ij_path = idir / "issue.json"
        hj_path = idir / "history.jsonl"
        assert ij_path.is_file()
        assert hj_path.is_file()
        iss_doc = json.loads(ij_path.read_text(encoding="utf-8"))
        assert iss_doc["name"] == folder_name
        assert iss_doc["folder"] == folder_name
        disk_keys.add(iss_doc["key"])

        hist_lines = [ln for ln in hj_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(hist_lines) == 1
        hist_entry = json.loads(hist_lines[0])
        assert hist_entry["action"] == "created"

    assert disk_keys == expected_keys

    listed = ws.cli_json("issue", "list", "--page-size", "100")
    assert listed["total"] == worker_count
    assert {i["key"] for i in listed["issues"]} == expected_keys


def test_03_twenty_worker_single_issue_rich_comment_embed_and_worklog_blitz(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """20-Worker Single-Issue Rich Comment, Embed & Worklog Blitz:
    Against a single shared issue, run 20 concurrent workers (10 posting rich comments with unique
    inline PNG embeds + 10 logging distinct worklog minutes) -> assert zero lost comments,
    10 intact embedded PNG files on disk, exact logged_minutes sum, and 100% valid JSON/JSONL.
    """
    ws = workspace
    ws.ensure_server()
    issue_name = "Shared High Contention Blitz Issue"
    shared = ws.cli_json(
        "issue",
        "create",
        issue_name,
        "--category",
        "bug",
        "--priority",
        "critical",
        "--estimate",
        "600",
    )
    assert shared["key"] == "ITR-1"

    blitz_dir = tmp_path / "blitz_pngs"
    blitz_dir.mkdir(parents=True, exist_ok=True)
    png_specs: list[tuple[Path, str]] = []
    for i in range(10):
        p_path, p_sha = make_png(
            blitz_dir / f"flame_shard_{i:02d}.png",
            width=20 + i * 2,
            height=16 + i,
            color=((30 * i + 25) % 255, (70 * i + 50) % 255, (110 * i + 90) % 255),
        )
        png_specs.append((p_path, p_sha))

    worklog_minutes = [11, 13, 17, 19, 23, 29, 31, 37, 41, 43]
    expected_total_minutes = sum(worklog_minutes)

    barrier = threading.Barrier(20, timeout=20.0)

    def _comment_embed_worker(idx: int) -> CLIResult:
        png_path, _ = png_specs[idx]
        body = (
            f"### Blitz Shard {idx}\n\n"
            f"| Shard | Status |\n|---|---|\n| `{idx}` | Verified |\n\n"
            f"![Shard {idx}](embed:{png_path.name})"
        )
        barrier.wait()
        return run_itrack_cli(
            "comment",
            "add",
            issue_name,
            "--author",
            f"commenter-{idx}",
            "--body",
            body,
            "--embed",
            str(png_path),
            "--json",
            cwd=ws.root,
            expect_rc=None,
            timeout=60.0,
        )

    def _worklog_worker(idx: int) -> CLIResult:
        mins = worklog_minutes[idx]
        barrier.wait()
        return run_itrack_cli(
            "worklog",
            "add",
            "ITR-1",
            str(mins),
            "--author",
            f"logger-{idx}",
            "--note",
            f"Parallel investigation slice {idx} ({mins}m)",
            "--json",
            cwd=ws.root,
            expect_rc=None,
            timeout=60.0,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(_comment_embed_worker, i) for i in range(10)] + [
            pool.submit(_worklog_worker, j) for j in range(10)
        ]
        results = [f.result(timeout=75.0) for f in futs]

    for res in results:
        assert res.returncode == 0, f"Blitz worker failed: {res.args}\nSTDERR:\n{res.stderr}"
        _ = res.json()

    final_issue = ws.cli_json("issue", "show", "ITR-1")
    assert len(final_issue["comments"]) == 10
    assert {c["id"] for c in final_issue["comments"]} == {f"CMT-{k}" for k in range(1, 11)}
    assert {c["author"] for c in final_issue["comments"]} == {f"commenter-{i}" for i in range(10)}

    # Verify all 10 PNG embeds exist on disk with valid PNG headers and exact SHA-256 digests
    disk_embed_shas: set[str] = set()
    comments_root = ws.issue_dir(issue_name) / "comments"
    for cmt in final_issue["comments"]:
        cid = cmt["id"]
        c_json_path = comments_root / cid / "comment.json"
        assert c_json_path.is_file()
        parsed_cmt = json.loads(c_json_path.read_text(encoding="utf-8"))
        assert len(parsed_cmt["embeds"]) == 1
        emb = parsed_cmt["embeds"][0]
        assert f"attachment://{emb['id']}" in parsed_cmt["body"]
        emb_file = comments_root / cid / "files" / emb["stored_name"]
        assert emb_file.is_file()
        assert emb_file.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        disk_embed_shas.add(sha256_file(emb_file))

    assert disk_embed_shas == {sha for _, sha in png_specs}

    # Verify exact worklog sum and individual records
    assert final_issue["logged_minutes"] == expected_total_minutes
    assert len(final_issue["worklogs"]) == 10
    assert sorted(int(w["minutes"]) for w in final_issue["worklogs"]) == sorted(worklog_minutes)

    # Validate raw issue.json & history.jsonl integrity on disk
    raw_issue_doc = json.loads((ws.issue_dir(issue_name) / "issue.json").read_text(encoding="utf-8"))
    assert raw_issue_doc["logged_minutes"] == expected_total_minutes
    hist_lines = [
        ln
        for ln in (ws.issue_dir(issue_name) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(hist_lines) == 21  # 1 created + 10 comment_added + 10 worklog_added
    parsed_hist = [json.loads(ln) for ln in hist_lines]
    assert {h["id"] for h in parsed_hist} == {f"HST-{k}" for k in range(1, 22)}


def test_04_concurrent_folder_rename_with_spaces_under_heavy_cli_and_http_fire(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Concurrent Folder Rename With Spaces Under Heavy CLI + HTTP Read/Write Fire:
    Spawn concurrent readers (`issue show ITR-1`, `search`, HTTP `GET`) and writers
    (`worklog add`, `comment add`) while chaining 6 rapid `issue rename ITR-1 "Stage Name <k> With Spaces"`
    operations -> assert zero ghost folders, zero lost comments/worklogs/attachments,
    and 100% SQLite-to-disk consistency.
    """
    ws = workspace
    base_url = ws.ensure_server()

    fire_assets = tmp_path / "rename_fire_assets"
    fire_assets.mkdir(parents=True, exist_ok=True)
    spec_pdf_path, spec_pdf_sha = make_pdf(fire_assets / "architecture_spec.pdf", text="Rename Fire Spec")
    arch_png_path, arch_png_sha = make_png(fire_assets / "arch_diagram.png", width=28, height=28, color=(16, 185, 129))
    cmt_files: list[tuple[Path, str]] = [
        make_text_file(fire_assets / f"writer_note_{i}.txt", f"Attachment payload from writer {i}\n")
        for i in range(6)
    ]

    initial_name = "Initial Rename Under Heavy Fire Target"
    iss1 = ws.cli_json(
        "issue",
        "create",
        initial_name,
        "--category",
        "feature",
        "--state",
        "in-progress",
        "--priority",
        "critical",
        "--desc",
        "AnchorTokenForConcurrentSearch across rapid folder renames.",
        "--embed",
        str(arch_png_path),
        "--attach",
        str(spec_pdf_path),
    )
    assert iss1["key"] == "ITR-1"
    pdf_att_id = iss1["attachments"][0]["id"]
    png_emb_id = iss1["description_embeds"][0]["id"]

    # Peer issue linked to ITR-1 so every rename also updates peer target_name under lock
    peer = ws.cli_json("issue", "create", "Downstream Peer Service Task", "--category", "task")
    ws.cli_json("link", "add", "ITR-1", "blocks", peer["key"])

    stage_words = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")
    stage_names = [f"Stage {k} {w} With Spaces" for k, w in enumerate(stage_words, 1)]
    final_expected_name = stage_names[-1]

    start_barrier = threading.Barrier(6, timeout=20.0)
    writers_done = threading.Event()
    errors: list[str] = []
    errors_lock = threading.Lock()

    def _record_err(msg: str) -> None:
        with errors_lock:
            errors.append(msg)

    def _renamer_chain() -> None:
        start_barrier.wait()
        for st_name in stage_names:
            res = run_itrack_cli("issue", "rename", "ITR-1", st_name, "--json", cwd=ws.root, expect_rc=None, timeout=45.0)
            if res.returncode != 0:
                _record_err(f"Rename to '{st_name}' failed ({res.returncode}): {res.stderr}")
                return
            data = res.json()
            if data.get("name") != st_name:
                _record_err(f"Rename mismatch: expected '{st_name}', got {data.get('name')}")

    def _comment_writers() -> None:
        start_barrier.wait()
        for i in range(6):
            fpath, _ = cmt_files[i]
            res = run_itrack_cli(
                "comment",
                "add",
                "ITR-1",
                "--author",
                f"fire-writer-{i}",
                "--body",
                f"Concurrent comment #{i} AnchorTokenForConcurrentSearch",
                "--attach",
                str(fpath),
                "--json",
                cwd=ws.root,
                expect_rc=None,
                timeout=45.0,
            )
            if res.returncode != 0:
                _record_err(f"Comment writer {i} failed ({res.returncode}): {res.stderr}")

    def _worklog_writers() -> None:
        start_barrier.wait()
        for i in range(6):
            res = run_itrack_cli(
                "worklog",
                "add",
                "ITR-1",
                "10",
                "--author",
                f"fire-logger-{i}",
                "--note",
                f"Concurrent worklog #{i}",
                "--json",
                cwd=ws.root,
                expect_rc=None,
                timeout=45.0,
            )
            if res.returncode != 0:
                _record_err(f"Worklog writer {i} failed ({res.returncode}): {res.stderr}")

    def _cli_show_reader() -> None:
        start_barrier.wait()
        reads = 0
        while not writers_done.is_set() or reads < 4:
            res = run_itrack_cli("issue", "show", "ITR-1", "--json", cwd=ws.root, expect_rc=None, timeout=45.0)
            if res.returncode != 0:
                _record_err(f"CLI show reader failed ({res.returncode}): {res.stderr}")
                break
            doc = res.json()
            if doc.get("key") != "ITR-1":
                _record_err(f"Unexpected key in CLI show: {doc.get('key')}")
            reads += 1

    def _cli_search_reader() -> None:
        start_barrier.wait()
        reads = 0
        while not writers_done.is_set() or reads < 4:
            res = run_itrack_cli(
                "search",
                "AnchorTokenForConcurrentSearch",
                "--json",
                cwd=ws.root,
                expect_rc=None,
                timeout=45.0,
            )
            if res.returncode != 0:
                _record_err(f"CLI search reader failed ({res.returncode}): {res.stderr}")
                break
            sdata = res.json()
            keys = [x["key"] for x in sdata.get("issues", [])]
            if "ITR-1" not in keys:
                _record_err(f"Search missed ITR-1 mid-rename! keys={keys}")
            reads += 1

    def _http_get_reader() -> None:
        start_barrier.wait()
        reads = 0
        with httpx.Client(base_url=base_url, timeout=20.0, trust_env=False) as client:
            while not writers_done.is_set() or reads < 10:
                r1 = client.get("/api/v1/issues/ITR-1")
                r2 = client.get("/api/v1/issues", params={"tab": "active"})
                r3 = client.get("/api/v1/search", params={"q": "AnchorTokenForConcurrentSearch"})
                for resp in (r1, r2, r3):
                    if resp.status_code != 200:
                        _record_err(f"HTTP GET {resp.url} returned {resp.status_code}: {resp.text}")
                        return
                if r1.json().get("key") != "ITR-1":
                    _record_err("HTTP GET /api/v1/issues/ITR-1 returned wrong key")
                    return
                reads += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        renamer_fut = pool.submit(_renamer_chain)
        cmt_fut = pool.submit(_comment_writers)
        wl_fut = pool.submit(_worklog_writers)
        r_show_fut = pool.submit(_cli_show_reader)
        r_search_fut = pool.submit(_cli_search_reader)
        r_http_fut = pool.submit(_http_get_reader)

        for writer_f in (renamer_fut, cmt_fut, wl_fut):
            writer_f.result(timeout=90.0)
        writers_done.set()

        for reader_f in (r_show_fut, r_search_fut, r_http_fut):
            reader_f.result(timeout=60.0)

    assert errors == [], "Concurrency errors during rename-under-fire:\n" + "\n".join(errors)

    # 1. Zero ghost folders & zero temp folders on disk
    all_entries = sorted(p.name for p in ws.issues_dir.iterdir())
    assert all_entries == sorted([final_expected_name, "Downstream Peer Service Task"]), (
        f"Ghost or temporary folders found in {ws.issues_dir}: {all_entries}"
    )

    # 2. Zero lost comments, worklogs, or attachments (addressable by final spaced name AND ITR-1)
    final_iss = ws.cli_json("issue", "show", final_expected_name)
    assert final_iss["key"] == "ITR-1"
    assert final_iss["name"] == final_expected_name
    assert final_iss["folder"] == final_expected_name
    assert len(final_iss["comments"]) == 6
    assert final_iss["logged_minutes"] == 60
    assert len(final_iss["worklogs"]) == 6

    # Verify byte-exact survival of whole-issue PDF, description PNG embed, and all 6 comment attachments
    dl_dir = tmp_path / "fire_downloads"
    ws.cli("attach", "download", final_expected_name, pdf_att_id, "-o", str(dl_dir / "spec.pdf"))
    ws.cli("attach", "download", "ITR-1", png_emb_id, "-o", str(dl_dir / "arch.png"))
    assert sha256_file(dl_dir / "spec.pdf") == spec_pdf_sha
    assert sha256_file(dl_dir / "arch.png") == arch_png_sha

    downloaded_cmt_shas: set[str] = set()
    for cmt in final_iss["comments"]:
        assert len(cmt["attachments"]) == 1
        ca_id = cmt["attachments"][0]["id"]
        out_p = dl_dir / f"{ca_id}.txt"
        ws.cli("attach", "download", final_expected_name, ca_id, "-o", str(out_p))
        downloaded_cmt_shas.add(sha256_file(out_p))
    assert downloaded_cmt_shas == {sha for _, sha in cmt_files}

    # Peer link target_name matches final stage name
    peer_after = ws.cli_json("issue", "show", peer["key"])
    assert peer_after["links"][0]["target_name"] == final_expected_name

    # 3. 100% SQLite-to-disk consistency
    db_path = ws.runtime_dir / "index.sqlite"
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT key, name, folder, logged_minutes FROM issues ORDER BY seq ASC").fetchall()
    assert rows == [
        ("ITR-1", final_expected_name, final_expected_name, 60),
        ("ITR-2", "Downstream Peer Service Task", "Downstream Peer Service Task", 0),
    ]
    assert ws.cli_json("search", "Stage 6 Foxtrot")["total"] == 1
    assert ws.cli_json("search", "Stage 1 Alpha")["total"] == 0


def test_05_out_of_band_git_pull_corrupt_sqlite_and_stale_pid_self_healing(
    workspace: ITrackWorkspace,
) -> None:
    """Out-of-Band Git Pull Disk Mutation + Corrupt index.sqlite + Stale PID Self-Healing:
    1. While daemon is live, create `itrack-issues/issues/Pulled From Remote Git/issue.json` + `history.jsonl`
       directly on disk -> assert `issue list` & `search "Pulled From Remote"` auto-detect without manual `reindex`.
    2. Overwrite `itrack-issues/.runtime/index.sqlite` with random garbage bytes -> assert the very next
       CLI query transparently self-heals `index.sqlite` from canonical disk JSON with zero data loss.
    3. Write fake dead PID (999999) and dead port into `itrack-issues/.runtime/daemon.json` after killing
       daemon -> assert the next CLI command automatically recovers, spawns a healthy daemon, and exits 0.
    """
    ws = workspace
    ws.ensure_server()

    local_iss = ws.cli_json(
        "issue",
        "create",
        "Local Baseline Issue",
        "--category",
        "bug",
        "--priority",
        "high",
        "--tag",
        "resilience",
        "--desc",
        "Created locally prior to simulated git pull and corruption torture.",
    )
    assert local_iss["key"] == "ITR-1"
    ws.cli_json("comment", "add", "ITR-1", "--body", "Baseline comment preserved across index rebuilds")

    live_daemons_before = find_itrack_daemons(ws.root)
    assert len(live_daemons_before) == 1
    live_pid = int(live_daemons_before[0]["pid"])

    # -------------------------------------------------------------------------
    # Part 1: Out-of-Band `git pull` Disk Mutation While Daemon Is Live
    # -------------------------------------------------------------------------
    pulled_name = "Pulled From Remote Git"
    pulled_dir = ws.issue_dir(pulled_name)
    (pulled_dir / "attachments").mkdir(parents=True, exist_ok=True)
    (pulled_dir / "comments").mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pulled_issue_doc: dict[str, Any] = {
        "key": "ITR-2",
        "seq": 2,
        "name": pulled_name,
        "folder": pulled_name,
        "category": "feature",
        "state": "todo",
        "previous_state": None,
        "archived": False,
        "priority": "critical",
        "priority_rank": 5,
        "severity": "sev2",
        "severity_rank": 4,
        "resolution": None,
        "description": "Synchronized out-of-band from upstream branch via git pull.",
        "description_embeds": [],
        "acceptance_criteria": [
            {"id": "AC-1", "text": "Detected automatically without manual reindex", "checked": True}
        ],
        "assignee": "remote-teammate",
        "reporter": "remote-teammate",
        "tags": ["git-sync"],
        "components": ["sync-core"],
        "parent": None,
        "sprint": None,
        "milestone": None,
        "story_points": 5.0,
        "estimate_minutes": 90,
        "logged_minutes": 0,
        "due_date": None,
        "custom_fields": {"branch": "origin/main"},
        "attachments": [],
        "deleted_attachments": {},
        "links": [],
        "worklogs": [],
        "cloned_from": None,
        "created_at": ts,
        "updated_at": ts,
    }
    (pulled_dir / "issue.json").write_text(json.dumps(pulled_issue_doc, indent=2) + "\n", encoding="utf-8")
    (pulled_dir / "history.jsonl").write_text(
        json.dumps(
            {
                "id": "HST-50",
                "timestamp": ts,
                "actor": "remote-teammate",
                "action": "created",
                "diffs": [{"field": "name", "old": None, "new": pulled_name}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Without calling `reindex`, `issue list` and `search` must auto-detect disk signature change!
    listed_after_pull = ws.cli_json("issue", "list")
    assert {i["key"] for i in listed_after_pull["issues"]} == {"ITR-1", "ITR-2"}

    search_after_pull = ws.cli_json("search", "Pulled From Remote")
    assert [i["key"] for i in search_after_pull["issues"]] == ["ITR-2"]

    # Confirm the same daemon remained live AND sequence counters advanced cleanly past ITR-2
    assert [int(d["pid"]) for d in find_itrack_daemons(ws.root)] == [live_pid]
    post_pull_issue = ws.cli_json("issue", "create", "Created After Git Pull Sync", "--category", "task")
    assert post_pull_issue["key"] == "ITR-3"

    # -------------------------------------------------------------------------
    # Part 2: Corrupt `index.sqlite` Overwritten With Random Garbage Bytes
    # -------------------------------------------------------------------------
    db_file = ws.runtime_dir / "index.sqlite"
    assert db_file.exists()
    for wal_extra in ws.runtime_dir.glob("index.sqlite-*"):
        wal_extra.unlink(missing_ok=True)
    db_file.write_bytes(b"NOT_A_VALID_SQLITE_HEADER!\x00\xff\xfe\xfd" + os.urandom(8192))

    # Very next query must transparently wipe & rebuild index.sqlite from disk JSON with zero data loss
    healed_search = ws.cli_json("search", "Baseline comment preserved")
    assert [i["key"] for i in healed_search["issues"]] == ["ITR-1"]

    healed_list = ws.cli_json("issue", "list")
    assert healed_list["total"] == 3
    assert {i["key"] for i in healed_list["issues"]} == {"ITR-1", "ITR-2", "ITR-3"}

    healed_pulled = ws.cli_json("issue", "show", pulled_name)
    assert healed_pulled["story_points"] == 5.0
    assert healed_pulled["ac_progress"] == {"total": 1, "checked": 1, "pct": 100.0}
    assert healed_pulled["custom_fields"]["branch"] == "origin/main"

    with sqlite3.connect(str(db_file)) as conn:
        qc_row = conn.execute("PRAGMA quick_check;").fetchone()
    assert qc_row is not None and str(qc_row[0]).lower() == "ok"

    # -------------------------------------------------------------------------
    # Part 3: Stale PID (999999) + Dead Port in `daemon.json` Self-Healing
    # -------------------------------------------------------------------------
    ws.stop_server()
    assert find_itrack_daemons(ws.root) == []

    dead_port = _find_unused_local_port()
    stale_daemon_payload = {
        "pid": 999999,
        "host": "127.0.0.1",
        "port": dead_port,
        "base_url": f"http://127.0.0.1:{dead_port}",
        "repo_root": str(ws.root),
        "started_at": "2026-01-01T00:00:00Z",
        "version": "2.0.0",
    }
    ws.runtime_dir.mkdir(parents=True, exist_ok=True)
    ws.daemon_json_path.write_text(json.dumps(stale_daemon_payload, indent=2) + "\n", encoding="utf-8")

    # Next CLI command must automatically clean stale daemon.json, spawn a healthy daemon, and exit 0
    recovered_status = ws.cli_json("status")
    assert recovered_status["initialized"] is True
    assert recovered_status["total_issues"] == 3

    recovered_list = ws.cli_json("issue", "list")
    assert {i["key"] for i in recovered_list["issues"]} == {"ITR-1", "ITR-2", "ITR-3"}

    healed_meta = json.loads(ws.daemon_json_path.read_text(encoding="utf-8"))
    new_pid = int(healed_meta["pid"])
    assert new_pid != 999999
    assert _is_pid_running(new_pid)
    time.sleep(0.05)
    active_daemons = find_itrack_daemons(ws.root)
    assert len(active_daemons) == 1
    assert int(active_daemons[0]["pid"]) == new_pid
