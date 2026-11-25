"""Suite 01: Clean-Root Footprint, Git-Root & Pre-Init Guardrails, Config/Server/UI Lifecycle & 100% CLI Manual Audit."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from rich.console import Console

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import (  # noqa: E402
    CATEGORIES,
    CLIResult,
    EXIT_CODES,
    EXPECTED_ROOT_ENTRIES,
    IQL_OPERATORS,
    ITRACK_PY,
    ITRACK_REQUIREMENTS,
    ITRACK_SERVER_PY,
    LINK_TYPES,
    PRESET_TABS,
    PRIORITIES,
    REPO_ROOT,
    RESOLUTIONS,
    SEVERITIES,
    STATES,
    SWIMLANES,
    ITrackWorkspace,
    _is_pid_running,
    assert_clean_repo_root,
    cleanup_repo_daemon,
    create_git_worktree,
    find_itrack_daemons,
    init_git_repo,
    introspect_cli_surface,
    load_itrack_cli_module,
    load_module_from_path,
    run_itrack_cli,
)


# ---------------------------------------------------------------------------
# Local Port, Dead-PID & CPU-Load-Resilient Cold-Start Helpers
# ---------------------------------------------------------------------------


def _allocate_free_port() -> int:
    """Bind an ephemeral localhost TCP socket to obtain a guaranteed-free port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_dead_pid() -> int:
    """Obtain a PID guaranteed not to be running."""
    clean_env = {k: v for k, v in os.environ.items() if k != "COVERAGE_PROCESS_START"}
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen([sys.executable, "-B", "-c", "pass"], env=clean_env)
    pid = int(proc.pid)
    proc.wait(timeout=10.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _is_pid_running(pid):
        time.sleep(0.02)
    if _is_pid_running(pid):
        for candidate in range(999_991, 999_000, -1):
            if not _is_pid_running(candidate):
                return candidate
    assert not _is_pid_running(pid), f"Expected PID {pid} to have exited"
    return pid


def _infer_target_repo(
    args: tuple[Any, ...],
    cwd: Path | str | None,
    env_extra: dict[str, str | None] | None,
) -> Path:
    str_args = [str(a) for a in args]
    for idx, tok in enumerate(str_args):
        if tok == "--root" and idx + 1 < len(str_args):
            return Path(str_args[idx + 1])
        if tok.startswith("--root="):
            return Path(tok.split("=", 1)[1])
    if env_extra and env_extra.get("ITRACK_ROOT"):
        return Path(str(env_extra["ITRACK_ROOT"]))
    return Path(cwd) if cwd is not None else REPO_ROOT


def _run_cli(
    *args: Any,
    cwd: Path | str | None = None,
    expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
    env_extra: dict[str, str | None] | None = None,
    timeout: float = 60.0,
) -> CLIResult:
    """Invoke `run_itrack_cli` with grace recovery ONLY if daemon cold-start exceeds 12s under multi-suite CPU saturation."""
    target_repo = _infer_target_repo(args, cwd, env_extra)
    was_uninitialized = not (target_repo / "itrack-issues" / "config.json").exists()
    is_fresh_init = was_uninitialized and ("init" in [str(a) for a in args])

    for attempt in range(3):
        res = run_itrack_cli(
            *args,
            cwd=cwd,
            expect_rc=None if (expect_rc == 0 and attempt < 2) else expect_rc,
            env_extra=env_extra,
            timeout=timeout,
        )
        if expect_rc == 0 and res.returncode == 3 and "Timed out waiting for itrack-server.py" in res.output:
            time.sleep(3.5)
            if is_fresh_init:
                cleanup_repo_daemon(target_repo)
                shutil.rmtree(target_repo / "itrack-issues", ignore_errors=True)
            continue
        if expect_rc is not None:
            allowed = {expect_rc} if isinstance(expect_rc, int) else set(expect_rc)
            if res.returncode not in allowed:
                raise AssertionError(
                    f"CLI command failed exit-code assertion!\n"
                    f"  Args: {[str(a) for a in args]}\n"
                    f"  CWD:  {cwd}\n"
                    f"  Expected RC: {sorted(allowed)}, Actual RC: {res.returncode}\n"
                    f"  STDOUT:\n{res.stdout}\n"
                    f"  STDERR:\n{res.stderr}"
                )
        return res
    return res


def _ws_cli(ws: ITrackWorkspace, *args: Any, expect_rc: int = 0) -> CLIResult:
    return _run_cli(*args, cwd=ws.root, expect_rc=expect_rc)


def _ws_cli_json(ws: ITrackWorkspace, *args: Any, expect_rc: int = 0) -> Any:
    str_args = [str(a) for a in args]
    if "--json" not in str_args:
        str_args.append("--json")
    return _ws_cli(ws, *str_args, expect_rc=expect_rc).json()


# ===========================================================================
# 1. Repository Root Footprint (Law 1)
# ===========================================================================


def test_law1_clean_repository_root_and_requirements_footprint() -> None:
    """Verify strict Clean-Root footprint and required runtime + test dependencies in itrack-requirements.txt."""
    assert_clean_repo_root(REPO_ROOT)

    assert ITRACK_PY.is_file()
    assert ITRACK_SERVER_PY.is_file()
    assert ITRACK_REQUIREMENTS.is_file()
    for doc_name in ("SPEC.md", "todo.md", "AGENTS.md", ".env.example"):
        assert (REPO_ROOT / doc_name).is_file()
    assert not (REPO_ROOT / "requirements.txt").exists()

    non_git_entries = set(os.listdir(REPO_ROOT)) - {".git"}
    assert non_git_entries == {
        "itrack.py",
        "itrack-server.py",
        "itrack-requirements.txt",
        "tests",
        "SPEC.md",
        "todo.md",
        "AGENTS.md",
        ".env.example",
    }
    assert non_git_entries == EXPECTED_ROOT_ENTRIES

    req_lines = [
        ln.strip()
        for ln in ITRACK_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    declared_pkgs = {
        re.split(r"[><=!~\[\s;]", ln, maxsplit=1)[0].strip().lower()
        for ln in req_lines
    }
    required_pkgs = {
        "fastapi",
        "uvicorn",
        "pydantic",
        "python-multipart",
        "httpx",
        "rich",
        "pytest",
        "coverage",
        "playwright",
    }
    assert required_pkgs.issubset(declared_pkgs), (
        f"Missing required dependencies in itrack-requirements.txt: {sorted(required_pkgs - declared_pkgs)}"
    )


# ===========================================================================
# 2. Git-Root Pre-Flight Guardrails (Exit 2) & Pre-Init Guardrails (Exit 3)
# ===========================================================================


def test_law2_outside_git_root_exits_2_cli_and_server(tmp_path: Path) -> None:
    """Verify running outside any Git repository exits 2 for both itrack.py (human & --json) and itrack-server.py."""
    non_git = tmp_path / "non_git_folder"
    non_git.mkdir(parents=True, exist_ok=True)

    try:
        for subcmd in ((), ("init",), ("status",)):
            res_h = run_itrack_cli(*subcmd, cwd=non_git, expect_rc=2, timeout=60.0)
            assert res_h.stdout == ""
            assert "not a git repository root" in res_h.stderr.lower()

            res_j = run_itrack_cli(*subcmd, "--json", cwd=non_git, expect_rc=2, timeout=60.0)
            payload = res_j.json()
            assert payload["error"] == "not_git_root"
            assert payload["exit_code"] == 2
            assert "not a git repository root" in payload["message"].lower()

        res_flag = run_itrack_cli(
            "--root", str(non_git), "issue", "list", "--json", cwd=REPO_ROOT, expect_rc=2, timeout=60.0
        ).json()
        assert res_flag["error"] == "not_git_root"
        assert res_flag["exit_code"] == 2

        srv_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        srv_proc = subprocess.run(
            [sys.executable, "-B", str(ITRACK_SERVER_PY), "--root", str(non_git)],
            cwd=str(non_git),
            capture_output=True,
            text=True,
            env=srv_env,
            timeout=60.0,
        )
        assert srv_proc.returncode == 2
        assert "not a git repository root" in srv_proc.stderr.lower()
        assert not (non_git / "itrack-issues").exists()
        assert find_itrack_daemons(non_git) == []
    finally:
        cleanup_repo_daemon(non_git)


def test_law2_git_subdirectory_hint_and_root_overrides_and_version(git_repo: Path) -> None:
    """Verify subdirectory rejection with toplevel hint (exit 2), --root / ITRACK_ROOT overrides, and -V/--version."""
    sub_dir = git_repo / "services" / "api"
    sub_dir.mkdir(parents=True, exist_ok=True)
    expected_root_str = str(git_repo.resolve())

    try:
        res_h = run_itrack_cli("status", cwd=sub_dir, expect_rc=2, timeout=60.0)
        assert "Hint: cd" in res_h.stderr
        assert expected_root_str in res_h.stderr

        res_zero_h = run_itrack_cli(cwd=sub_dir, expect_rc=2, timeout=60.0)
        assert "Hint: cd" in res_zero_h.stderr
        assert expected_root_str in res_zero_h.stderr

        res_j = run_itrack_cli("status", "--json", cwd=sub_dir, expect_rc=2, timeout=60.0).json()
        assert res_j["error"] == "not_git_root"
        assert res_j["exit_code"] == 2
        assert res_j["hint_git_root"] == expected_root_str

        srv_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        srv_sub = subprocess.run(
            [sys.executable, "-B", str(ITRACK_SERVER_PY)],
            cwd=str(sub_dir),
            capture_output=True,
            text=True,
            env=srv_env,
            timeout=60.0,
        )
        assert srv_sub.returncode == 2
        assert expected_root_str in srv_sub.stderr

        # Missing --root value and empty --root= exit 1
        run_itrack_cli("--root", cwd=git_repo, expect_rc=1, timeout=60.0)
        err_missing = run_itrack_cli("--json", "--root", cwd=git_repo, expect_rc=1, timeout=60.0).json()
        assert err_missing["exit_code"] == 1
        assert err_missing["error"] == "invalid_arguments"

        run_itrack_cli("--root=", cwd=git_repo, expect_rc=1, timeout=60.0)
        err_empty = run_itrack_cli("--root=", "--json", cwd=git_repo, expect_rc=1, timeout=60.0).json()
        assert err_empty["exit_code"] == 1
        assert err_empty["error"] == "invalid_arguments"

        # -V and --version (human & --json) exit 0 without initializing workspace
        v_short = run_itrack_cli("-V", cwd=git_repo, expect_rc=0, timeout=60.0)
        assert "itrack" in v_short.stdout.lower()
        v_long = run_itrack_cli("--version", cwd=git_repo, expect_rc=0, timeout=60.0)
        assert v_long.stdout.strip() == v_short.stdout.strip()

        v_short_j = run_itrack_cli("-V", "--json", cwd=git_repo, expect_rc=0, timeout=60.0).json()
        v_long_j = run_itrack_cli("--version", "--json", cwd=git_repo, expect_rc=0, timeout=60.0).json()
        assert v_short_j["name"] == "itrack"
        assert v_short_j["version"]
        assert v_long_j == v_short_j
        assert not (git_repo / "itrack-issues").exists()

        # Running from subdirectory WITH --root <git_repo>, --root=<git_repo>, and ITRACK_ROOT succeeds (rc == 0)
        init_res = _run_cli("--root", str(git_repo), "init", "--json", cwd=sub_dir, expect_rc=0).json()
        assert init_res["initialized"] is True

        st_space_flag = _run_cli("status", "--root", str(git_repo), "--json", cwd=sub_dir, expect_rc=0).json()
        assert st_space_flag["initialized"] is True

        st_eq_flag = _run_cli(f"--root={git_repo}", "status", "--json", cwd=sub_dir, expect_rc=0).json()
        assert st_eq_flag["initialized"] is True

        st_env = _run_cli(
            "status",
            "--json",
            cwd=sub_dir,
            env_extra={"ITRACK_ROOT": str(git_repo)},
            expect_rc=0,
        ).json()
        assert st_env["initialized"] is True

        _run_cli("--root", str(git_repo), "server", "stop", cwd=sub_dir, expect_rc=0)
    finally:
        cleanup_repo_daemon(git_repo)


def test_spaces_and_unicode_in_repo_path(tmp_path: Path) -> None:
    """Verify full workspace init, issue creation, dual addressing, and server stop when repo path has spaces & Unicode."""
    unicode_repo = init_git_repo(tmp_path / "My Project 🚀 Repo (2026)")
    try:
        _run_cli("init", cwd=unicode_repo, expect_rc=0)

        st = _run_cli("status", "--json", cwd=unicode_repo, expect_rc=0).json()
        assert st["initialized"] is True
        assert Path(st["repo_root"]).resolve() == unicode_repo.resolve()

        created = _run_cli("issue", "create", "Unicode Space Task ✨", "--json", cwd=unicode_repo, expect_rc=0).json()
        assert created["key"] == "ITR-1"
        assert created["name"] == "Unicode Space Task ✨"
        assert (unicode_repo / "itrack-issues" / "issues" / "Unicode Space Task ✨" / "issue.json").is_file()

        shown_key = _run_cli("issue", "show", "ITR-1", "--json", cwd=unicode_repo, expect_rc=0).json()
        assert shown_key["key"] == "ITR-1"
        assert shown_key["name"] == "Unicode Space Task ✨"

        shown_name = _run_cli(
            "issue", "show", "Unicode Space Task ✨", "--json", cwd=unicode_repo, expect_rc=0
        ).json()
        assert shown_name["key"] == "ITR-1"

        _run_cli("server", "stop", cwd=unicode_repo, expect_rc=0)
    finally:
        cleanup_repo_daemon(unicode_repo)


def test_git_worktree_dot_git_file_support(git_repo: Path, tmp_path: Path) -> None:
    """Verify a Git worktree (where .git is a file rather than a directory) passes Law 2 and operates cleanly."""
    wt = create_git_worktree(git_repo, tmp_path / "Worktree Feature 🚀", branch="feat/wt")
    assert (wt / ".git").is_file()
    assert not (wt / ".git").is_dir()

    try:
        _run_cli("init", cwd=wt, expect_rc=0)

        st = _run_cli("status", "--json", cwd=wt, expect_rc=0).json()
        assert st["initialized"] is True
        assert Path(st["repo_root"]).resolve() == wt.resolve()

        iss = _run_cli("issue", "create", "Worktree Issue", "--json", cwd=wt, expect_rc=0).json()
        assert iss["key"] == "ITR-1"
        assert iss["name"] == "Worktree Issue"
        assert (wt / "itrack-issues" / "issues" / "Worktree Issue" / "issue.json").is_file()

        _run_cli("server", "stop", cwd=wt, expect_rc=0)
    finally:
        cleanup_repo_daemon(wt)


def test_symlink_to_git_root_support(git_repo: Path, tmp_path: Path) -> None:
    """Verify symlink pointing to a Git root works seamlessly via both cwd=sym and --root sym."""
    sym = tmp_path / "symlink_to_repo"
    sym.symlink_to(git_repo)
    assert sym.is_symlink()

    try:
        _run_cli("init", cwd=sym, expect_rc=0)
        _run_cli("status", cwd=sym, expect_rc=0)
        _run_cli("issue", "create", "Symlink Task", cwd=sym, expect_rc=0)

        listed_cwd = _run_cli("issue", "list", "--json", cwd=sym, expect_rc=0).json()
        assert listed_cwd["total"] == 1
        assert listed_cwd["issues"][0]["name"] == "Symlink Task"

        listed_flag = _run_cli("--root", str(sym), "issue", "list", "--json", cwd=tmp_path, expect_rc=0).json()
        assert listed_flag["total"] == 1
        assert listed_flag["issues"][0]["key"] == "ITR-1"

        _run_cli("--root", str(sym), "server", "stop", cwd=tmp_path, expect_rc=0)
        _run_cli("server", "stop", cwd=sym, expect_rc=0)
    finally:
        cleanup_repo_daemon(sym)
        cleanup_repo_daemon(git_repo)


def test_exit_code_3_before_init_creates_nothing_and_spawns_no_daemon(git_repo: Path) -> None:
    """Verify operational subcommands exit 3 in an uninitialized Git repo without creating files or daemons."""
    pre_init_commands = [
        ("status",),
        ("issue", "list"),
        ("server", "start"),
        ("config", "show"),
        ("stats",),
        ("reindex",),
        ("board",),
    ]

    def _verify_cmd_exit_3(cmd: tuple[str, ...]) -> None:
        res_h = run_itrack_cli(*cmd, cwd=git_repo, expect_rc=3, timeout=60.0)
        assert "not initialized" in res_h.stderr.lower()

        res_j = run_itrack_cli(*cmd, "--json", cwd=git_repo, expect_rc=3, timeout=60.0).json()
        assert res_j["error"] == "not_initialized"
        assert res_j["exit_code"] == 3

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_verify_cmd_exit_3, pre_init_commands))

    srv_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    srv_res = subprocess.run(
        [sys.executable, "-B", str(ITRACK_SERVER_PY), "--root", str(git_repo)],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
        env=srv_env,
        timeout=60.0,
    )
    assert srv_res.returncode == 3

    assert not (git_repo / "itrack-issues").exists()
    assert find_itrack_daemons(git_repo) == []


# ===========================================================================
# 3. init, status, reindex, config, server, and ui Lifecycle
# ===========================================================================


def test_init_layout_idempotency_and_prefix_title_validation(tmp_path: Path) -> None:
    """Verify fresh init on-disk structure, idempotent re-init preserving issues/sequences, and prefix/title checks."""
    # 1. Fresh JSON init + on-disk structure + idempotent re-run (JSON & human) preserving existing issue & sequence
    repo_idem = init_git_repo(tmp_path / "init_idempotent")
    try:
        init1 = _run_cli("init", "--json", cwd=repo_idem, expect_rc=0).json()
        assert init1["initialized"] is True
        assert init1["already_initialized"] is False
        assert init1["config"]["project_prefix"] == "ITR"
        assert init1["config"]["project_title"] == "iTrack Workspace"

        issues_root = repo_idem / "itrack-issues"
        assert (issues_root / "config.json").is_file()
        assert (issues_root / "tags.json").is_file()
        assert (issues_root / "sprints.json").is_file()
        assert (issues_root / "milestones.json").is_file()
        assert (issues_root / "views.json").is_file()
        assert (issues_root / "issues").is_dir()
        assert (issues_root / ".runtime" / "index.sqlite").is_file()

        gitignore_txt = (issues_root / ".gitignore").read_text(encoding="utf-8")
        assert ".runtime/" in gitignore_txt
        assert "index.sqlite*" in gitignore_txt

        first_issue = _run_cli("issue", "create", "First Preserved Issue", "--json", cwd=repo_idem, expect_rc=0).json()
        assert first_issue["key"] == "ITR-1"

        init2 = _run_cli("init", "--json", cwd=repo_idem, expect_rc=0).json()
        assert init2["initialized"] is True
        assert init2["already_initialized"] is True

        reinit_h = _run_cli("init", cwd=repo_idem, expect_rc=0)
        assert "Ready" in reinit_h.stdout

        fetched = _run_cli("issue", "show", "ITR-1", "--json", cwd=repo_idem, expect_rc=0).json()
        assert fetched["name"] == "First Preserved Issue"

        second_issue = _run_cli("issue", "create", "Second Sequence Issue", "--json", cwd=repo_idem, expect_rc=0).json()
        assert second_issue["key"] == "ITR-2"

        _run_cli("server", "stop", cwd=repo_idem, expect_rc=0)
    finally:
        cleanup_repo_daemon(repo_idem)

    # 2. Invalid --prefix / --title exit 1, then fresh human init --prefix CORE --title "Core Engine" + re-run APP
    repo_custom = init_git_repo(tmp_path / "init_custom")
    try:
        for bad_prefix in ("1BAD", "X"):
            err_p = run_itrack_cli(
                "init", "--prefix", bad_prefix, "--json", cwd=repo_custom, expect_rc=1, timeout=60.0
            ).json()
            assert err_p["exit_code"] == 1
            assert err_p["error"] == "invalid_prefix"

        res_bad_title = run_itrack_cli("init", "--title", "   ", cwd=repo_custom, expect_rc=1, timeout=60.0)
        assert "invalid project title" in res_bad_title.stderr.lower()
        assert not (repo_custom / "itrack-issues").exists()

        c1_h = _run_cli("init", "--prefix", "CORE", "--title", "Core Engine", cwd=repo_custom, expect_rc=0)
        assert "Core Engine" in c1_h.stdout
        assert "CORE" in c1_h.stdout

        iss_core = _run_cli("issue", "create", "Core Task", "--json", cwd=repo_custom, expect_rc=0).json()
        assert iss_core["key"] == "CORE-1"

        c2 = _run_cli(
            "init", "--prefix", "APP", "--title", "App Engine", "--json", cwd=repo_custom, expect_rc=0
        ).json()
        assert c2["already_initialized"] is True
        assert c2["config"]["project_prefix"] == "APP"
        assert c2["config"]["project_title"] == "App Engine"

        iss_app = _run_cli("issue", "create", "App Task", "--json", cwd=repo_custom, expect_rc=0).json()
        assert iss_app["key"] == "APP-2"

        _run_cli("server", "stop", cwd=repo_custom, expect_rc=0)
    finally:
        cleanup_repo_daemon(repo_custom)


def test_status_and_reindex_including_deleted_sqlite_rebuild(workspace: ITrackWorkspace) -> None:
    """Verify status and reindex (human & --json), including full SQLite rebuild after deleting index.sqlite."""
    _ws_cli_json(workspace, "issue", "create", "Status Issue One", "--priority", "high")
    _ws_cli_json(workspace, "issue", "create", "Status Issue Two", "--desc", "RebuildSearchSentinel99")

    st_h = _ws_cli(workspace, "status", expect_rc=0)
    assert "Workspace Status" in st_h.stdout
    assert "ITR" in st_h.stdout

    st_j = _ws_cli_json(workspace, "status", expect_rc=0)
    assert st_j["initialized"] is True
    assert st_j["project_prefix"] == "ITR"
    assert st_j["project_title"] == "iTrack Workspace"
    assert st_j["total_issues"] == 2
    assert isinstance(st_j["daemon"], dict)
    assert st_j["daemon"]["status"] == "ok"

    rx_h = _ws_cli(workspace, "reindex", expect_rc=0)
    assert "Reindexed" in rx_h.stdout

    rx_j = _ws_cli_json(workspace, "reindex", expect_rc=0)
    assert rx_j["status"] == "reindexed"
    assert rx_j["indexed_issues"] == 2

    # Stop server, delete index.sqlite (+ WAL/SHM if present), and verify `reindex` fully reconstructs SQLite + FTS5
    _ws_cli(workspace, "server", "stop", expect_rc=0)
    for sqlite_file in workspace.runtime_dir.glob("index.sqlite*"):
        sqlite_file.unlink()
    assert not (workspace.runtime_dir / "index.sqlite").exists()

    rebuilt = _ws_cli_json(workspace, "reindex", expect_rc=0)
    assert rebuilt["status"] == "reindexed"
    assert rebuilt["indexed_issues"] == 2
    assert (workspace.runtime_dir / "index.sqlite").is_file()

    found = _ws_cli_json(workspace, "search", "RebuildSearchSentinel99", expect_rc=0)
    assert found["total"] == 1
    assert found["issues"][0]["name"] == "Status Issue Two"


def test_config_show_set_all_keys_and_reject_invalid_values(workspace: ITrackWorkspace) -> None:
    """Verify config show & config set across all supported keys/nested paths and exit 1 on invalid keys/values."""
    show_h = _ws_cli(workspace, "config", "show", expect_rc=0)
    assert "project_prefix" in show_h.stdout

    show_j = _ws_cli_json(workspace, "config", "show", expect_rc=0)
    assert show_j["project_prefix"] == "ITR"
    assert show_j["project_title"] == "iTrack Workspace"

    # 1. project_title (human + show check)
    set_h = _ws_cli(workspace, "config", "set", "project_title", "Platform Ops", expect_rc=0)
    assert "project_title" in set_h.stdout
    assert _ws_cli_json(workspace, "config", "show")["project_title"] == "Platform Ops"

    # 2. project_prefix -> verify subsequent issue key starts with OPS-
    cfg = _ws_cli_json(workspace, "config", "set", "project_prefix", "OPS", expect_rc=0)
    assert cfg["project_prefix"] == "OPS"
    ops_issue = _ws_cli_json(workspace, "issue", "create", "Ops Prefixed Issue", expect_rc=0)
    assert ops_issue["key"].startswith("OPS-")

    # 3. default_state ("in progress" -> normalized "in-progress")
    cfg = _ws_cli_json(workspace, "config", "set", "default_state", "in progress", expect_rc=0)
    assert cfg["default_state"] == "in-progress"

    # 4. default_priority ("high")
    cfg = _ws_cli_json(workspace, "config", "set", "default_priority", "high", expect_rc=0)
    assert cfg["default_priority"] == "high"

    # 5. default_category ("bug")
    cfg = _ws_cli_json(workspace, "config", "set", "default_category", "bug", expect_rc=0)
    assert cfg["default_category"] == "bug"

    # 6. default_severity ("sev2")
    cfg = _ws_cli_json(workspace, "config", "set", "default_severity", "sev2", expect_rc=0)
    assert cfg["default_severity"] == "sev2"

    # 7. default_assignee ("alice", verify new issue inherits all defaults, then clear with "none" -> None)
    cfg = _ws_cli_json(workspace, "config", "set", "default_assignee", "alice", expect_rc=0)
    assert cfg["default_assignee"] == "alice"

    inherited = _ws_cli_json(workspace, "issue", "create", "Issue With Configured Defaults", expect_rc=0)
    assert inherited["key"].startswith("OPS-")
    assert inherited["state"] == "in-progress"
    assert inherited["priority"] == "high"
    assert inherited["category"] == "bug"
    assert inherited["severity"] == "sev2"
    assert inherited["assignee"] == "alice"

    cfg = _ws_cli_json(workspace, "config", "set", "default_assignee", "none", expect_rc=0)
    assert cfg["default_assignee"] is None

    # 8. wip_limits JSON + dotted wip_limits.<state> + clearing via 0 and null
    cfg = _ws_cli_json(workspace, "config", "set", "wip_limits", '{"in-progress": 3, "in-review": 2}', expect_rc=0)
    assert cfg["wip_limits"] == {"in-progress": 3, "in-review": 2}

    cfg = _ws_cli_json(workspace, "config", "set", "wip_limits.todo", "4", expect_rc=0)
    assert cfg["wip_limits"] == {"in-progress": 3, "in-review": 2, "todo": 4}

    cfg = _ws_cli_json(workspace, "config", "set", "wip_limits.in-review", "0", expect_rc=0)
    assert "in-review" not in cfg["wip_limits"]

    # Clear wip_limits.todo via null (JSON object with null on CLI + dotted wip_limits.todo null via PATCH /api/v1/config)
    api_resp = workspace.api("PATCH", "/api/v1/config", json={"key": "wip_limits.todo", "value": "null"})
    assert api_resp.status_code == 200
    assert api_resp.json()["wip_limits"] == {"in-progress": 3}

    cfg = _ws_cli_json(workspace, "config", "set", "wip_limits.todo", "4", expect_rc=0)
    assert cfg["wip_limits"]["todo"] == 4
    cfg = _ws_cli_json(workspace, "config", "set", "wip_limits", '{"in-progress": 3, "todo": null}', expect_rc=0)
    assert cfg["wip_limits"] == {"in-progress": 3}

    # 9. preferences JSON + dotted preferences.theme / page_size / default_swimlane
    cfg = _ws_cli_json(
        workspace,
        "config",
        "set",
        "preferences",
        '{"theme": "light", "page_size": 50, "default_swimlane": "priority"}',
        expect_rc=0,
    )
    assert cfg["preferences"]["theme"] == "light"
    assert cfg["preferences"]["page_size"] == 50
    assert cfg["preferences"]["default_swimlane"] == "priority"

    cfg = _ws_cli_json(workspace, "config", "set", "preferences.theme", "dark", expect_rc=0)
    assert cfg["preferences"]["theme"] == "dark"

    cfg = _ws_cli_json(workspace, "config", "set", "preferences.page_size", "100", expect_rc=0)
    assert cfg["preferences"]["page_size"] == 100

    cfg = _ws_cli_json(workspace, "config", "set", "preferences.default_swimlane", "category", expect_rc=0)
    assert cfg["preferences"]["default_swimlane"] == "category"

    # 10. Invalid keys & values MUST exit 1 (exercising both human and --json error formatting)
    invalid_cases = [
        ("nonexistent_key", "val"),
        ("project_prefix", "99BAD"),
        ("project_title", "   "),
        ("default_state", "bogus_state"),
        ("default_priority", "ultra"),
        ("default_category", "mystery"),
        ("default_severity", "sev99"),
        ("wip_limits", '{"not-a-state": 3}'),
        ("wip_limits", "{broken_json"),
        ("wip_limits.todo", "-5"),
        ("wip_limits.todo", "abc"),
        ("preferences.theme", "neon"),
        ("preferences.page_size", "0"),
        ("preferences.default_swimlane", "invalidlane"),
    ]

    def _check_invalid_case(item: tuple[int, tuple[str, str]]) -> None:
        idx, (bad_key, bad_val) = item
        if idx % 2 == 0:
            err_j = run_itrack_cli(
                "config", "set", bad_key, bad_val, "--json", cwd=workspace.root, expect_rc=1, timeout=60.0
            ).json()
            assert err_j["exit_code"] == 1
        else:
            err_h = run_itrack_cli(
                "config", "set", bad_key, bad_val, cwd=workspace.root, expect_rc=1, timeout=60.0
            )
            assert "error:" in err_h.stderr.lower()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_check_invalid_case, enumerate(invalid_cases)))


def test_server_lifecycle_custom_ports_and_stale_dead_pid_recovery(workspace: ITrackWorkspace) -> None:
    """Verify server stop/status/start/restart, custom --port transitions, invalid port exit 1, and stale dead-PID recovery."""
    # 1. Stop server -> verify stopped status (human & --json)
    _ws_cli(workspace, "server", "stop", expect_rc=0)
    stop_again = _ws_cli_json(workspace, "server", "stop", expect_rc=0)
    assert stop_again["status"] == "not_running"
    assert stop_again["stopped"] is False

    stat_stopped_h = _ws_cli(workspace, "server", "status", expect_rc=0)
    assert "stopped" in stat_stopped_h.stdout.lower()

    stat_stopped_j = _ws_cli_json(workspace, "server", "status", expect_rc=0)
    assert stat_stopped_j["running"] is False
    assert stat_stopped_j["stale_recovered"] is False

    # 2. Start server (human & --json) -> verify status & live /api/v1/health
    start_h = _ws_cli(workspace, "server", "start", expect_rc=0)
    assert "http://127.0.0.1:" in start_h.stdout

    start_j = _ws_cli_json(workspace, "server", "start", expect_rc=0)
    assert start_j["running"] is True
    base_url_0 = str(start_j["base_url"]).rstrip("/")
    pid_0 = int(start_j["pid"])

    stat_live_h = _ws_cli(workspace, "server", "status", expect_rc=0)
    assert "running" in stat_live_h.stdout.lower()

    stat_live_j = _ws_cli_json(workspace, "server", "status", expect_rc=0)
    assert stat_live_j["running"] is True
    assert int(stat_live_j["pid"]) == pid_0

    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r0 = hc.get(f"{base_url_0}/api/v1/health")
        assert r0.status_code == 200
        assert int(r0.json()["pid"]) == pid_0

    # 3. Start on custom --port P1 -> verify daemon moves to P1
    p1 = _allocate_free_port()
    start_p1 = _ws_cli_json(workspace, "server", "start", "--port", str(p1), expect_rc=0)
    assert start_p1["running"] is True
    assert int(start_p1["port"]) == p1
    assert str(start_p1["base_url"]).rstrip("/") == f"http://127.0.0.1:{p1}"
    pid_1 = int(start_p1["pid"])

    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r1 = hc.get(f"http://127.0.0.1:{p1}/api/v1/health")
        assert r1.status_code == 200
        assert int(r1.json()["port"]) == p1

    # 4. Restart (human) and restart --port P2 (--json) -> verify PID changes and responds on P2
    restart_h = _ws_cli(workspace, "server", "restart", expect_rc=0)
    assert "restarted" in restart_h.stdout.lower()
    pid_after_h = int(_ws_cli_json(workspace, "server", "status", expect_rc=0)["pid"])
    assert pid_after_h != pid_1

    p2 = _allocate_free_port()
    restart_p2 = _ws_cli_json(workspace, "server", "restart", "--port", str(p2), expect_rc=0)
    assert restart_p2["running"] is True
    assert restart_p2["restarted"] is True
    assert int(restart_p2["port"]) == p2
    assert int(restart_p2["pid"]) != pid_after_h

    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r2 = hc.get(f"http://127.0.0.1:{p2}/api/v1/health")
        assert r2.status_code == 200
        assert int(r2.json()["port"]) == p2

    # 5. Invalid port -> exits 1
    run_itrack_cli("server", "start", "--port", "70000", cwd=workspace.root, expect_rc=1, timeout=60.0)
    bad_port_j = run_itrack_cli(
        "server", "start", "--port", "70000", "--json", cwd=workspace.root, expect_rc=1, timeout=60.0
    ).json()
    assert bad_port_j["exit_code"] == 1
    assert bad_port_j["error"] == "invalid_port"

    # 6. Stale daemon.json dead-PID recovery
    _ws_cli(workspace, "server", "stop", expect_rc=0)
    dead_pid = _get_dead_pid()
    unused_port = _allocate_free_port()
    stale_meta = {
        "pid": dead_pid,
        "port": unused_port,
        "base_url": f"http://127.0.0.1:{unused_port}",
        "repo_root": str(workspace.root),
    }

    workspace.daemon_json_path.write_text(json.dumps(stale_meta), encoding="utf-8")
    recovered_status = _ws_cli_json(workspace, "server", "status", expect_rc=0)
    assert recovered_status["running"] is False
    assert recovered_status["stale_recovered"] is True
    assert not workspace.daemon_json_path.exists()

    # Write stale daemon.json again and verify `status --json` & `server start --json` auto-recover with a live daemon
    workspace.daemon_json_path.write_text(json.dumps(stale_meta), encoding="utf-8")
    auto_status = _ws_cli_json(workspace, "status", expect_rc=0)
    assert auto_status["initialized"] is True
    live_pid = int(auto_status["daemon"]["pid"])
    assert live_pid != dead_pid
    assert _is_pid_running(live_pid)
    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r_rec = hc.get(f"{str(auto_status['base_url']).rstrip('/')}/api/v1/health")
        assert r_rec.status_code == 200

    _ws_cli(workspace, "server", "stop", expect_rc=0)
    workspace.daemon_json_path.write_text(json.dumps(stale_meta), encoding="utf-8")
    started_after_stale = _ws_cli_json(workspace, "server", "start", expect_rc=0)
    assert started_after_stale["running"] is True
    assert int(started_after_stale["pid"]) != dead_pid
    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        assert hc.get(f"{str(started_after_stale['base_url']).rstrip('/')}/api/v1/health").status_code == 200


def test_ui_no_browser_custom_port_and_foreground_sigint(workspace: ITrackWorkspace) -> None:
    """Verify ui --no-browser (human & --json), ui --port, and ui --no-browser --foreground SIGINT shutdown."""
    # 1. Background ui --no-browser (human & --json)
    ui_h = _ws_cli(workspace, "ui", "--no-browser", expect_rc=0)
    assert "http://127.0.0.1:" in ui_h.stdout

    ui_j = _ws_cli_json(workspace, "ui", "--no-browser", expect_rc=0)
    assert ui_j["browser_opened"] is False
    ui_url = str(ui_j["url"]).rstrip("/")

    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r_html = hc.get(f"{ui_url}/")
        assert r_html.status_code == 200
        assert "<!doctype html>" in r_html.text.lower()
        assert "iTrack" in r_html.text

        r_health = hc.get(f"{ui_url}/api/v1/health")
        assert r_health.status_code == 200

    # 2. Background ui --port P3 --no-browser --json
    p3 = _allocate_free_port()
    ui_p3 = _ws_cli_json(workspace, "ui", "--port", str(p3), "--no-browser", expect_rc=0)
    assert int(ui_p3["port"]) == p3
    assert ui_p3["browser_opened"] is False
    assert str(ui_p3["url"]).rstrip("/") == f"http://127.0.0.1:{p3}"

    with httpx.Client(timeout=5.0, trust_env=False) as hc:
        r_p3 = hc.get(f"http://127.0.0.1:{p3}/")
        assert r_p3.status_code == 200
        assert "iTrack" in r_p3.text

    # 3. Foreground ui --no-browser --foreground --port <fg_port> + SIGINT clean shutdown
    _ws_cli(workspace, "server", "stop", expect_rc=0)
    fg_env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "200",
    }

    stdout_text = ""
    stderr_text = ""
    fg_url = ""
    for attempt in range(2):
        fg_port = _allocate_free_port()
        fg_url = f"http://127.0.0.1:{fg_port}"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(ITRACK_PY),
                "ui",
                "--no-browser",
                "--foreground",
                "--port",
                str(fg_port),
            ],
            cwd=str(workspace.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=fg_env,
        )
        try:
            ready = False
            deadline = time.monotonic() + 20.0
            with httpx.Client(timeout=1.0, trust_env=False) as hc:
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        rh = hc.get(f"{fg_url}/api/v1/health")
                        if rh.status_code == 200:
                            rr = hc.get(f"{fg_url}/")
                            assert rr.status_code == 200
                            assert "iTrack" in rr.text
                            ready = True
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)

            if not ready and attempt == 0:
                cleanup_repo_daemon(workspace.root)
                continue

            assert ready, f"Foreground UI server failed to become healthy at {fg_url} (rc={proc.poll()})"

            if proc.stdout is not None:
                select.select([proc.stdout], [], [], 4.0)

            proc.send_signal(signal.SIGINT)
            stdout_text, stderr_text = proc.communicate(timeout=15.0)
            break
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)

    assert fg_url in stdout_text, (
        f"Expected {fg_url} in foreground stdout, got:\nSTDOUT: {stdout_text}\nSTDERR: {stderr_text}"
    )
    after_fg = _ws_cli_json(workspace, "server", "status", expect_rc=0)
    assert after_fg["running"] is False


# ===========================================================================
# 4. 100% CLI Manual Audit & Zero-Drift Executable Quickstart
# ===========================================================================


@pytest.fixture(scope="module")
def cli_surface() -> dict:
    """Cached programmatic introspection of itrack.py subcommands, flags, and option strings."""
    mod = load_itrack_cli_module()
    return introspect_cli_surface(mod)


def test_zero_arg_manual_completeness_and_all_help_subcommands(git_repo: Path, cli_surface: dict) -> None:
    """Verify zero-arg manual (human & --json), 100% AST surface/enum/iQL/exit-code parity, and --help across every command."""
    # 1. Zero-arg manual in fresh uninitialized git_repo spawns no daemon and creates no files
    man_h = run_itrack_cli(cwd=git_repo, expect_rc=0, timeout=60.0)
    manual_md = man_h.stdout
    assert len(manual_md) > 1000
    assert not (git_repo / "itrack-issues").exists()
    assert find_itrack_daemons(git_repo) == []

    man_j = run_itrack_cli("--json", cwd=git_repo, expect_rc=0, timeout=60.0)
    manual_json = man_j.json()
    assert manual_json["name"] == "itrack"
    assert manual_json["markdown"] == manual_md
    assert not (git_repo / "itrack-issues").exists()
    assert find_itrack_daemons(git_repo) == []

    # 2. 100% of subcommands (group + leaf) appear in manual Markdown and manual_json["commands"]
    json_cmd_map = {entry["command"]: entry for entry in manual_json["commands"]}
    assert "" in json_cmd_map

    for cmd in cli_surface["all_commands"]:
        assert f"python itrack.py {cmd}" in manual_md, f"Missing subcommand '{cmd}' in manual Markdown"
        assert cmd in json_cmd_map, f"Missing subcommand '{cmd}' in manual_json['commands']"

    # 3. 100% of canonical flags & option_map option strings (-V, -h, -o, --json, --root, etc.) appear in both
    json_all_flags = {fl for entry in manual_json["commands"] for fl in entry.get("flags", [])}

    for canon_flag in cli_surface["all_flags"]:
        assert f"`{canon_flag}`" in manual_md, f"Missing canonical flag '{canon_flag}' in manual Markdown"
        assert canon_flag in json_all_flags, f"Missing canonical flag '{canon_flag}' in manual_json['commands']"

    for opt_str in cli_surface["option_map"]:
        assert f"`{opt_str}`" in manual_md, f"Missing option string '{opt_str}' in manual Markdown"
        assert opt_str in json_all_flags, f"Missing option string '{opt_str}' in manual_json['commands']"

    for leaf_cmd, leaf_flags in cli_surface["command_flags"].items():
        recorded = set(json_cmd_map[leaf_cmd]["flags"])
        assert set(leaf_flags).issubset(recorded), (
            f"Missing flags on '{leaf_cmd}' in manual_json: {sorted(set(leaf_flags) - recorded)}"
        )

    # 4. 100% of canonical enum values match manual Markdown and manual_json["enums"]
    assert len(CATEGORIES) == 13
    assert len(STATES) == 12
    assert len(PRIORITIES) == 5
    assert len(SEVERITIES) == 5
    assert len(RESOLUTIONS) == 6
    assert len(LINK_TYPES) == 6
    assert len(PRESET_TABS) == 8
    assert len(SWIMLANES) == 5

    expected_enums = {
        "categories": CATEGORIES,
        "states": STATES,
        "priorities": PRIORITIES,
        "severities": SEVERITIES,
        "resolutions": RESOLUTIONS,
        "link_types": LINK_TYPES,
        "preset_tabs": PRESET_TABS,
        "swimlanes": SWIMLANES,
    }
    assert manual_json["enums"] == expected_enums
    for enum_name, enum_values in expected_enums.items():
        for val in enum_values:
            assert f"`{val}`" in manual_md, f"Missing {enum_name} value '{val}' in manual Markdown"

    # 5. 100% of IQL_OPERATORS (16) appear in manual Markdown and match manual_json["iql_operators"]
    assert len(IQL_OPERATORS) == 16
    assert manual_json["iql_operators"] == IQL_OPERATORS
    for op in IQL_OPERATORS:
        assert f"`{op}`" in manual_md, f"Missing iQL operator '{op}' in manual Markdown"

    # 6. 100% of EXIT_CODES (0..5) appear in manual Markdown and manual_json["exit_codes"]
    assert set(EXIT_CODES.keys()) == {0, 1, 2, 3, 4, 5}
    assert set(manual_json["exit_codes"].keys()) == {str(k) for k in EXIT_CODES}
    for code in EXIT_CODES:
        assert f"`{code}`" in manual_md, f"Missing exit code '{code}' in manual Markdown"
        assert manual_json["exit_codes"][str(code)]

    # 7. Setup uses itrack-requirements.txt and NEVER unprefixed requirements.txt
    assert "itrack-requirements.txt" in manual_md
    assert "itrack-requirements.txt" in manual_json["setup"]
    assert re.findall(r"(?<![A-Za-z0-9_-])requirements\.txt", manual_md) == []
    assert re.findall(r"(?<![A-Za-z0-9_-])requirements\.txt", json.dumps(manual_json)) == []

    # 8. --help on root (-h and --help) AND on every command in cli_surface["all_commands"]
    root_help = run_itrack_cli("--help", cwd=git_repo, expect_rc=0, timeout=60.0)
    assert "usage:" in root_help.stdout.lower()
    short_help = run_itrack_cli("-h", cwd=git_repo, expect_rc=0, timeout=60.0)
    assert "usage:" in short_help.stdout.lower()

    def _check_subcmd_help(cmd: str) -> None:
        res = run_itrack_cli(*cmd.split(), "--help", cwd=git_repo, expect_rc=0, timeout=60.0)
        assert "usage:" in res.stdout.lower(), f"Expected 'usage:' in --help output for '{cmd}'"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_check_subcmd_help, cli_surface["all_commands"]))

    assert not (git_repo / "itrack-issues").exists()
    assert find_itrack_daemons(git_repo) == []


def test_manual_executable_quickstart_runs_end_to_end_zero_drift(git_repo: Path, tmp_path: Path) -> None:
    """Extract all `python itrack.py ...` commands from manual ```bash``` block and execute sequentially with rc == 0."""
    manual_md = run_itrack_cli(cwd=git_repo, expect_rc=0, timeout=60.0).stdout
    bash_blocks = re.findall(r"```bash\s*\n(.*?)```", manual_md, flags=re.DOTALL)
    assert bash_blocks, "Expected at least one ```bash code block in zero-arg manual"

    parsed_commands: list[list[str]] = []
    for block in bash_blocks:
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            tokens = shlex.split(line)
            if len(tokens) >= 2 and tokens[0] in ("python", "python3") and tokens[1].endswith("itrack.py"):
                parsed_commands.append(tokens)

    assert len(parsed_commands) >= 15, (
        f"Expected >= 15 executable quickstart commands in manual, extracted {len(parsed_commands)}"
    )

    quickstart_repo = init_git_repo(tmp_path / "manual_quickstart_repo")
    try:
        for tokens in parsed_commands:
            _run_cli(*tokens[2:], cwd=quickstart_repo, expect_rc=0)

        assert (quickstart_repo / "itrack-issues" / "config.json").is_file()
        assert (quickstart_repo / "itrack-issues" / "issues" / "OAuth2 Token Refresh Race" / "issue.json").is_file()
    finally:
        cleanup_repo_daemon(quickstart_repo)


# ===========================================================================
# 5. Direct Server --init Lifecycle & Deep CLI Branch Coverage
# ===========================================================================


def test_server_direct_init_idempotency_and_preinit_exit3(tmp_path: Path) -> None:
    """Verify `itrack-server.py --init` (fresh + idempotent re-run), pre-init exit 3, bind error exit 1, and config backfill."""
    srv_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    # 1. Uninitialized Git repo without --init exits 3 without creating itrack-issues/
    uninit_repo = init_git_repo(tmp_path / "server_uninitialized_repo")
    pre_proc = subprocess.run(
        [sys.executable, "-B", str(ITRACK_SERVER_PY), "--root", str(uninit_repo)],
        cwd=str(uninit_repo),
        capture_output=True,
        text=True,
        env=srv_env,
        timeout=60.0,
    )
    assert pre_proc.returncode == 3
    assert "not initialized" in pre_proc.stderr.lower()
    assert not (uninit_repo / "itrack-issues").exists()

    # 2. Direct `itrack-server.py --root <repo> --init --prefix SRV --title "Server Direct Init"` + idempotent re-run
    srv_repo = init_git_repo(tmp_path / "server_direct_init_repo")

    def _run_direct_server_init(create_sample_issue: bool) -> dict[str, Any]:
        last_err = ""
        for attempt in range(2):
            port = _allocate_free_port()
            base_url = f"http://127.0.0.1:{port}"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(ITRACK_SERVER_PY),
                    "--root",
                    str(srv_repo),
                    "--init",
                    "--prefix",
                    "SRV",
                    "--title",
                    "Server Direct Init",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=str(srv_repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=srv_env,
            )
            try:
                ready = False
                deadline = time.monotonic() + 20.0
                with httpx.Client(timeout=2.0, trust_env=False) as hc:
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            break
                        try:
                            rh = hc.get(f"{base_url}/api/v1/health")
                            if rh.status_code == 200:
                                ready = True
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.05)

                    if not ready:
                        out_s, err_s = proc.communicate(timeout=5.0) if proc.poll() is not None else ("", "")
                        last_err = f"rc={proc.poll()} stdout={out_s} stderr={err_s}"
                        if attempt == 0:
                            cleanup_repo_daemon(srv_repo)
                            continue
                        raise AssertionError(f"itrack-server.py --init failed to become healthy: {last_err}")

                    cfg_resp = hc.get(f"{base_url}/api/v1/config")
                    assert cfg_resp.status_code == 200
                    cfg_data = cfg_resp.json()

                    if create_sample_issue:
                        created = hc.post(
                            f"{base_url}/api/v1/issues",
                            json={"name": "Direct Server Init Issue", "actor": "srv-tester"},
                        )
                        assert created.status_code in (200, 201)
                        assert created.json()["key"] == "SRV-1"
                    else:
                        existing = hc.get(f"{base_url}/api/v1/issues/SRV-1")
                        assert existing.status_code == 200
                        assert existing.json()["name"] == "Direct Server Init Issue"

                    hc.post(f"{base_url}/api/v1/shutdown")

                proc.wait(timeout=10.0)
                return cfg_data
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)
        raise AssertionError(f"Unreachable: {last_err}")

    try:
        cfg_first = _run_direct_server_init(create_sample_issue=True)
        assert cfg_first["project_prefix"] == "SRV"
        assert cfg_first["project_title"] == "Server Direct Init"
        issues_dir = srv_repo / "itrack-issues"
        assert (issues_dir / "config.json").is_file()
        assert (issues_dir / "tags.json").is_file()
        assert (issues_dir / "sprints.json").is_file()
        assert (issues_dir / "milestones.json").is_file()
        assert (issues_dir / "views.json").is_file()
        assert (issues_dir / ".runtime" / "index.sqlite").is_file()

        # Idempotent second subprocess run preserving SRV-1 and config
        cfg_second = _run_direct_server_init(create_sample_issue=False)
        assert cfg_second["project_prefix"] == "SRV"
        assert cfg_second["project_title"] == "Server Direct Init"

        # 3. Socket bind conflict exits 1 cleanly
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy_sock:
            busy_sock.bind(("127.0.0.1", 0))
            busy_sock.listen(1)
            busy_port = int(busy_sock.getsockname()[1])
            bind_fail = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ITRACK_SERVER_PY),
                    "--root",
                    str(srv_repo),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(busy_port),
                ],
                cwd=str(srv_repo),
                capture_output=True,
                text=True,
                env=srv_env,
                timeout=30.0,
            )
            assert bind_fail.returncode == 1
            assert "failed to bind" in bind_fail.stderr.lower()

        # 4. In-process `default_config_dict` non-alpha fallback & `init_workspace_on_disk` missing-key backfill
        srv_mod = load_module_from_path("itrack_server_init_cov", ITRACK_SERVER_PY)
        fallback_cfg = srv_mod.default_config_dict(prefix="999", title="   ")
        assert fallback_cfg["project_prefix"] == "ITR"
        assert fallback_cfg["project_title"] == "iTrack Workspace"

        cfg_path = issues_dir / "config.json"
        raw_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw_cfg.pop("wip_limits", None)
        cfg_path.write_text(json.dumps(raw_cfg), encoding="utf-8")
        (issues_dir / ".gitignore").write_text("outdated\n", encoding="utf-8")

        backfilled = srv_mod.init_workspace_on_disk(srv_repo, prefix="IGNORED", title="Ignored")
        assert backfilled["initialized"] is True
        assert backfilled["already_initialized"] is True
        assert backfilled["config"]["project_prefix"] == "SRV"
        assert backfilled["config"]["wip_limits"] == {}
    finally:
        cleanup_repo_daemon(srv_repo)


def test_cli_inprocess_unit_branches_daemon_spawner_stop_and_renderers(tmp_path: Path) -> None:
    """Cover defensive CLI helpers, daemon probe/spawn/stop fallbacks, UI/board/tree renderers, and main() branches in-process."""
    cli_mod = load_module_from_path("itrack_cli_cov", ITRACK_PY)

    # 1. Enum & HTTP status helpers
    assert cli_mod.http_status_to_exit_code(400) == 1
    assert cli_mod.http_status_to_exit_code(404) == 4
    assert cli_mod.http_status_to_exit_code(409) == 5
    assert cli_mod.http_status_to_exit_code(503) == 3
    assert cli_mod.http_status_to_exit_code(502) == 1
    assert cli_mod.normalize_hyphen_enum(None) is None
    assert cli_mod.normalize_hyphen_enum("canceled") == "cancelled"

    # 2. `default_actor` fallback when git user.name and ITRACK_USER/ITRACK_AGENT are unset -> USER/LOGNAME -> "user"
    bare_actor_repo = tmp_path / "actor_no_git_name"
    bare_actor_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(bare_actor_repo), check=True, capture_output=True, text=True)
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(parents=True, exist_ok=True)

    clean_actor_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ITRACK_USER", "ITRACK_AGENT", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")
    }
    clean_actor_env.update(
        {
            "HOME": str(empty_home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "USER": "fallback_os_user",
            "LOGNAME": "fallback_os_user",
        }
    )
    with mock.patch.dict(os.environ, clean_actor_env, clear=True):
        assert cli_mod.default_actor(bare_actor_repo) == "fallback_os_user"
        with (
            mock.patch.object(cli_mod.subprocess, "run", side_effect=OSError("git unavailable")),
            mock.patch.object(cli_mod.getpass, "getuser", side_effect=KeyError("no pwent")),
        ):
            assert cli_mod.default_actor(bare_actor_repo) == "user"

    # 3. Flag extraction, surface telemetry & git-root helper branches
    short_only_parser = argparse.ArgumentParser()
    short_act = short_only_parser.add_argument("-x")
    assert cli_mod._canonical_flag_name(short_act) == "-x"

    custom_parser = cli_mod.build_parser()
    custom_parser.add_argument("--maybe-val", nargs="?")
    j_mode, r_arg, rem, _cmd_p, u_flags = cli_mod.extract_global_flags_and_surface(
        ["--maybe-val", "chosen", "--unrecognized-opt", "--", "--literal-tail"],
        custom_parser,
    )
    assert j_mode is False
    assert r_arg is None
    assert "--maybe-val" in u_flags
    assert "--unrecognized-opt" in u_flags
    assert rem[-2:] == ["--", "--literal-tail"]

    with mock.patch.dict(os.environ, {"ITRACK_SURFACE_LOG": ""}, clear=False):
        cli_mod.record_cli_surface("status", [])
    with mock.patch.dict(os.environ, {"ITRACK_SURFACE_LOG": str(tmp_path)}, clear=False):
        cli_mod.record_cli_surface("status", [])

    assert cli_mod.detect_git_toplevel_hint(tmp_path / "no_such_parent" / "child") is None
    with mock.patch.object(cli_mod.subprocess, "run", side_effect=OSError("git fail")):
        assert cli_mod.detect_git_toplevel_hint(tmp_path) is None

    with pytest.raises(SystemExit) as missing_root_exit:
        cli_mod.resolve_and_check_git_root(str(tmp_path / "missing_repo_dir"), json_mode=True)
    assert missing_root_exit.value.code == 2

    # 4. `is_pid_alive`, `read_daemon_json`, `probe_daemon`, `locate_server_script`, `read_log_tail`
    assert cli_mod.is_pid_alive(0) is False
    assert cli_mod.is_pid_alive(-9) is False
    with mock.patch.object(cli_mod.os, "kill", side_effect=PermissionError("owned by other uid")):
        assert cli_mod.is_pid_alive(99999) is True
    with mock.patch.object(cli_mod.os, "kill", side_effect=RuntimeError("unexpected")):
        assert cli_mod.is_pid_alive(99999) is False

    fake_repo = init_git_repo(tmp_path / "fake_daemon_repo")
    rdir = fake_repo / "itrack-issues" / ".runtime"
    rdir.mkdir(parents=True, exist_ok=True)
    (fake_repo / "itrack-issues" / "config.json").write_text(
        json.dumps({"project_prefix": "ITR", "project_title": "Fake"}), encoding="utf-8"
    )
    djson = rdir / "daemon.json"

    djson.write_text("{corrupt_json", encoding="utf-8")
    assert cli_mod.read_daemon_json(fake_repo) is None
    djson.write_text(json.dumps({"pid": 123}), encoding="utf-8")
    assert cli_mod.read_daemon_json(fake_repo) is None

    djson.write_text(json.dumps({"pid": os.getpid(), "base_url": "http://127.0.0.1:9"}), encoding="utf-8")
    mock_http_ctx = mock.MagicMock()
    with mock.patch.object(cli_mod.httpx, "Client", return_value=mock_http_ctx):
        mock_client = mock_http_ctx.__enter__.return_value
        mock_client.get.return_value = httpx.Response(503, text="starting")
        assert cli_mod.probe_daemon(fake_repo) is None

        mock_client.get.return_value = httpx.Response(200, json={"repo_root": str(tmp_path / "different_repo")})
        assert cli_mod.probe_daemon(fake_repo) is None

        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        assert cli_mod.probe_daemon(fake_repo) is None

    custom_srv = tmp_path / "override_server.py"
    with mock.patch.dict(os.environ, {"ITRACK_SERVER_SCRIPT": str(custom_srv)}, clear=False):
        assert cli_mod.locate_server_script() == custom_srv.resolve()
        with pytest.raises(cli_mod.CLIError) as missing_srv_err:
            cli_mod.spawn_daemon(fake_repo)
        assert missing_srv_err.value.error_code == "server_script_missing"
        assert missing_srv_err.value.exit_code == 1

    log_path = rdir / "daemon.log"
    assert cli_mod.read_log_tail(fake_repo) == ""
    log_path.write_text("l1\nl2\nl3\nl4\nFATAL: boom\n", encoding="utf-8")
    assert cli_mod.read_log_tail(fake_repo, lines=2) == "l4\nFATAL: boom"
    log_path.unlink()
    log_path.mkdir()
    assert cli_mod.read_log_tail(fake_repo) == ""
    log_path.rmdir()
    log_path.write_text("boot line 1\nFATAL: simulated crash\n", encoding="utf-8")

    # 5. `spawn_daemon` premature exit, timeout, unlink OSError & port-mismatch restart
    with pytest.raises(cli_mod.CLIError) as bad_port_err:
        cli_mod.spawn_daemon(fake_repo, requested_port=-1)
    assert bad_port_err.value.error_code == "invalid_port"

    crashed_proc = mock.MagicMock()
    crashed_proc.poll.return_value = 2
    orig_unlink = Path.unlink

    def _flaky_djson_unlink(self_path: Path, *u_args: Any, **u_kwargs: Any) -> None:
        if self_path.name == "daemon.json":
            raise OSError("unlink race")
        orig_unlink(self_path, *u_args, **u_kwargs)

    djson.write_text(json.dumps({"pid": 0, "base_url": "http://127.0.0.1:9"}), encoding="utf-8")
    with (
        mock.patch.object(cli_mod, "probe_daemon", return_value=None),
        mock.patch.object(cli_mod.subprocess, "Popen", return_value=crashed_proc),
        mock.patch.object(Path, "unlink", _flaky_djson_unlink),
    ):
        with pytest.raises(cli_mod.CLIError) as spawn_crash_err:
            cli_mod.spawn_daemon(fake_repo)
        assert spawn_crash_err.value.error_code == "daemon_spawn_failed"
        assert spawn_crash_err.value.exit_code == 1
        assert "FATAL: simulated crash" in spawn_crash_err.value.message

    hung_proc = mock.MagicMock()
    hung_proc.poll.return_value = None
    clock_ticks = iter([10.0, 14.0, 25.0])
    with (
        mock.patch.object(cli_mod, "probe_daemon", return_value=None),
        mock.patch.object(cli_mod.subprocess, "Popen", return_value=hung_proc),
        mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(clock_ticks)),
        mock.patch.object(cli_mod.time, "sleep", return_value=None),
    ):
        with pytest.raises(cli_mod.CLIError) as spawn_timeout_err:
            cli_mod.spawn_daemon(fake_repo)
        assert spawn_timeout_err.value.error_code == "daemon_timeout"
        assert spawn_timeout_err.value.exit_code == 3
        assert "FATAL: simulated crash" in spawn_timeout_err.value.message

    # Port mismatch in ensure_daemon -> spawn_daemon stops existing daemon and spawns on new port
    probe_seq = iter(
        [
            ("http://127.0.0.1:45001", {"port": 45001, "pid": 111}),
            ("http://127.0.0.1:45001", {"port": 45001, "pid": 111}),
            ("http://127.0.0.1:45002", {"port": 45002, "pid": 222}),
        ]
    )
    with (
        mock.patch.object(cli_mod, "probe_daemon", side_effect=lambda _r: next(probe_seq)),
        mock.patch.object(cli_mod, "stop_daemon", return_value={"status": "stopped"}) as stop_mock,
        mock.patch.object(cli_mod.subprocess, "Popen", return_value=hung_proc),
    ):
        new_url, new_health = cli_mod.ensure_daemon(fake_repo, requested_port=45002)
        assert new_url == "http://127.0.0.1:45002"
        assert new_health["port"] == 45002
        stop_mock.assert_called_once_with(fake_repo)

    # 6. `stop_daemon` HTTP /shutdown failure -> SIGTERM fallback, and already-dead PID `ProcessLookupError`
    djson.write_text(json.dumps({"pid": 543210, "base_url": "http://127.0.0.1:45099"}), encoding="utf-8")
    shutdown_err_ctx = mock.MagicMock()
    shutdown_err_ctx.__enter__.return_value.post.side_effect = httpx.ConnectError("shutdown refused")
    stop_clock_1 = iter([0.0, 1.0, 5.0, 6.0, 6.1, 9.0])
    alive_seq_1 = iter([True, True, True, False])
    with (
        mock.patch.object(cli_mod.httpx, "Client", return_value=shutdown_err_ctx),
        mock.patch.object(cli_mod, "probe_daemon", return_value=("http://127.0.0.1:45099", {"pid": 543210})),
        mock.patch.object(cli_mod, "is_pid_alive", side_effect=lambda _p: next(alive_seq_1, False)),
        mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(stop_clock_1, 99.0)),
        mock.patch.object(cli_mod.time, "sleep", return_value=None),
        mock.patch.object(cli_mod.os, "kill") as kill_mock_1,
    ):
        stop_res_1 = cli_mod.stop_daemon(fake_repo)
        assert stop_res_1["status"] == "stopped"
        kill_mock_1.assert_called_once_with(543210, signal.SIGTERM)

    djson.write_text(json.dumps({"pid": 543211, "base_url": "http://127.0.0.1:45099"}), encoding="utf-8")
    stop_clock_2 = iter([0.0, 5.0, 6.0, 9.0])
    with (
        mock.patch.object(cli_mod.httpx, "Client", return_value=shutdown_err_ctx),
        mock.patch.object(cli_mod, "probe_daemon", return_value=("http://127.0.0.1:45099", {"pid": 543211})),
        mock.patch.object(cli_mod, "is_pid_alive", side_effect=[True, False]),
        mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(stop_clock_2, 99.0)),
        mock.patch.object(cli_mod.os, "kill", side_effect=ProcessLookupError("already exited")),
        mock.patch.object(Path, "unlink", _flaky_djson_unlink),
    ):
        stop_res_2 = cli_mod.stop_daemon(fake_repo)
        assert stop_res_2["pid"] == 543211
    djson.unlink(missing_ok=True)

    # 7. `ITrackClient`, `validate_upload_paths`, `make_bar`, and `_atomic_write_json` error cleanup
    with cli_mod.ITrackClient("http://127.0.0.1:9") as iclient:
        with pytest.raises(cli_mod.CLIError) as e_503:
            iclient._raise_api_error(httpx.Response(503, text="Plain 503 Unavailable"))
        assert e_503.value.exit_code == 3

        with pytest.raises(cli_mod.CLIError) as e_list:
            iclient._raise_api_error(httpx.Response(400, json=["bad_payload"]))
        assert e_list.value.exit_code == 1

        with mock.patch.object(iclient._client, "request", side_effect=httpx.ConnectError("down")):
            with pytest.raises(cli_mod.CLIError) as e_conn:
                iclient.request("GET", "/api/v1/status")
            assert e_conn.value.exit_code == 3

        with mock.patch.object(iclient._client, "request", return_value=httpx.Response(204)):
            assert iclient.request("DELETE", "/api/v1/x") == {"ok": True}

        with mock.patch.object(iclient._client, "get", side_effect=httpx.ConnectError("dl down")):
            with pytest.raises(cli_mod.CLIError) as e_dl_conn:
                iclient.download_bytes("/api/v1/x")
            assert e_dl_conn.value.exit_code == 3

        with mock.patch.object(iclient._client, "get", return_value=httpx.Response(404, json={"detail": "gone"})):
            with pytest.raises(cli_mod.CLIError) as e_dl_404:
                iclient.download_bytes("/api/v1/x")
            assert e_dl_404.value.exit_code == 4

    with pytest.raises(cli_mod.CLIError) as e_up:
        cli_mod.validate_upload_paths([str(tmp_path / "missing_upload.png")])
    assert e_up.value.exit_code == 1
    assert "0%" in cli_mod.make_bar(0, 0)

    with mock.patch.object(cli_mod.os, "replace", side_effect=OSError("simulated replace error")):
        with pytest.raises(OSError):
            cli_mod._atomic_write_json(tmp_path / "atomic_fail.json", {"ok": False})
    assert not (tmp_path / "atomic_fail.json").exists()

    # 8. `render_comment_panel` fallback `elif linked_ids:` & `render_issue_detail` combined badges
    buf = io.StringIO()
    capture_console = Console(file=buf, force_terminal=False, width=140, highlight=False)
    panel = cli_mod.render_comment_panel(
        {
            "id": "CMT-7",
            "author": "alice",
            "created_at": "2026-11-24T00:00:00Z",
            "body": "Fallback attachment links check",
            "linked_attachment_ids": ["ATT-LIVE", "ATT-GONE"],
        },
        deleted_map={"ATT-GONE": {"filename": "old_trace.log"}},
        issue_att_map={"ATT-LIVE": {"filename": "architecture.pdf"}},
    )
    capture_console.print(panel)
    rendered_cmt = buf.getvalue()
    assert "ATT-LIVE (architecture.pdf)" in rendered_cmt
    assert "deleted attachment: ATT-GONE (old_trace.log)" in rendered_cmt

    buf_detail = io.StringIO()
    with mock.patch.object(
        cli_mod,
        "stdout_console",
        Console(file=buf_detail, force_terminal=False, width=140, highlight=False),
    ):
        cli_mod.render_issue_detail(
            {
                "key": "ITR-99",
                "name": "Multi Badge Issue",
                "category": "bug",
                "state": "done",
                "priority": "critical",
                "severity": "sev1",
                "resolution": "fixed",
                "archived": True,
                "is_overdue": True,
                "is_blocked": True,
                "unresolved_blockers": ["ITR-100"],
            }
        )
    detail_txt = buf_detail.getvalue()
    assert "res:fixed" in detail_txt
    assert "ARCHIVED" in detail_txt
    assert "OVERDUE" in detail_txt
    assert "BLOCKED (ITR-100)" in detail_txt

    # 9. `cmd_ui` (invalid port, DISPLAY=:0 mocked webbrowser.open, UI HTTP 500, foreground branches)
    with pytest.raises(cli_mod.CLIError) as ui_port_err:
        cli_mod.cmd_ui(argparse.Namespace(port=-1, no_browser=True, foreground=False), fake_repo, json_mode=True)
    assert ui_port_err.value.error_code == "invalid_port"
    assert ui_port_err.value.exit_code == 1

    ui_http_ok = mock.MagicMock()
    ui_http_ok.__enter__.return_value.get.return_value = httpx.Response(200, text="<!doctype html>iTrack")
    with (
        mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
        mock.patch.object(cli_mod, "ensure_daemon", return_value=("http://127.0.0.1:46001", {"port": 46001, "pid": 321})),
        mock.patch.object(cli_mod.httpx, "Client", return_value=ui_http_ok),
        mock.patch.object(cli_mod.webbrowser, "open", return_value=True) as wb_open,
    ):
        out_ui = io.StringIO()
        with mock.patch.object(cli_mod.sys, "stdout", out_ui):
            cli_mod.cmd_ui(argparse.Namespace(port=0, no_browser=False, foreground=False), fake_repo, json_mode=True)
        wb_open.assert_called_once_with("http://127.0.0.1:46001")
        assert json.loads(out_ui.getvalue())["browser_opened"] is True

    with (
        mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
        mock.patch.object(cli_mod, "ensure_daemon", return_value=("http://127.0.0.1:46001", {"port": 46001, "pid": 321})),
        mock.patch.object(cli_mod.httpx, "Client", return_value=ui_http_ok),
        mock.patch.object(cli_mod.webbrowser, "open", side_effect=OSError("no browser")),
    ):
        out_ui_err = io.StringIO()
        with mock.patch.object(cli_mod.sys, "stdout", out_ui_err):
            cli_mod.cmd_ui(argparse.Namespace(port=0, no_browser=False, foreground=False), fake_repo, json_mode=True)
        assert json.loads(out_ui_err.getvalue())["browser_opened"] is False

    ui_http_500 = mock.MagicMock()
    ui_http_500.__enter__.return_value.get.return_value = httpx.Response(500, text="err")
    with (
        mock.patch.object(cli_mod, "ensure_daemon", return_value=("http://127.0.0.1:46001", {"port": 46001, "pid": 321})),
        mock.patch.object(cli_mod.httpx, "Client", return_value=ui_http_500),
    ):
        with pytest.raises(cli_mod.CLIError) as ui_500_err:
            cli_mod.cmd_ui(argparse.Namespace(port=0, no_browser=True, foreground=False), fake_repo, json_mode=True)
        assert ui_500_err.value.error_code == "ui_error"

    # Foreground cmd_ui: premature exit, timeout + terminate, and --json + browser + KeyboardInterrupt
    fg_crashed = mock.MagicMock()
    fg_crashed.poll.return_value = 1
    with (
        mock.patch.object(cli_mod, "stop_daemon", return_value={"status": "not_running"}),
        mock.patch.object(cli_mod, "probe_daemon", return_value=None),
        mock.patch.object(cli_mod.subprocess, "Popen", return_value=fg_crashed),
    ):
        with pytest.raises(cli_mod.CLIError) as fg_crash_err:
            cli_mod.cmd_ui(argparse.Namespace(port=0, no_browser=True, foreground=True), fake_repo, json_mode=False)
        assert fg_crash_err.value.error_code == "daemon_spawn_failed"

    fg_hung = mock.MagicMock()
    fg_hung.poll.return_value = None
    fg_ticks = iter([0.0, 20.0])
    with (
        mock.patch.object(cli_mod, "stop_daemon", return_value={"status": "not_running"}),
        mock.patch.object(cli_mod, "probe_daemon", return_value=None),
        mock.patch.object(cli_mod.subprocess, "Popen", return_value=fg_hung),
        mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(fg_ticks, 30.0)),
    ):
        with pytest.raises(cli_mod.CLIError) as fg_to_err:
            cli_mod.cmd_ui(argparse.Namespace(port=0, no_browser=True, foreground=True), fake_repo, json_mode=False)
        assert fg_to_err.value.error_code == "daemon_timeout"
        fg_hung.terminate.assert_called_once()

    fg_live = mock.MagicMock()
    fg_live.poll.return_value = 0
    fg_live.wait.side_effect = KeyboardInterrupt()
    for wb_side_effect, expected_opened in ((None, True), (OSError("display err"), False)):
        fg_out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
            mock.patch.object(cli_mod, "stop_daemon", return_value={"status": "stopped"}),
            mock.patch.object(cli_mod, "probe_daemon", return_value=("http://127.0.0.1:46009", {"pid": 777})),
            mock.patch.object(cli_mod.subprocess, "Popen", return_value=fg_live),
            mock.patch.object(
                cli_mod.webbrowser,
                "open",
                return_value=True,
                side_effect=wb_side_effect,
            ),
            mock.patch.object(cli_mod.sys, "stdout", fg_out),
        ):
            cli_mod.cmd_ui(argparse.Namespace(port=0, no_browser=False, foreground=True), fake_repo, json_mode=True)
        fg_payload = json.loads(fg_out.getvalue())
        assert fg_payload["foreground"] is True
        assert fg_payload["browser_opened"] is expected_opened

    # 10. `cmd_tree` alternative shapes & `cmd_board` invalid swimlane + dict-shaped `columns` + WIP overflow
    for tree_shape in ({"key": "ITR-1", "name": "Single Root"}, [{"key": "ITR-2", "name": "List Root"}], "other"):
        tree_buf = io.StringIO()
        fake_ic = mock.MagicMock()
        fake_ic.__enter__.return_value.request.return_value = tree_shape
        with (
            mock.patch.object(cli_mod, "ensure_daemon", return_value=("http://127.0.0.1:9", {})),
            mock.patch.object(cli_mod, "ITrackClient", return_value=fake_ic),
            mock.patch.object(
                cli_mod, "stdout_console", Console(file=tree_buf, force_terminal=False, width=120, highlight=False)
            ),
        ):
            cli_mod.cmd_tree(argparse.Namespace(issue=None), fake_repo, json_mode=False)
        assert "Issue Hierarchy" in tree_buf.getvalue()

    with pytest.raises(cli_mod.CLIError) as bad_lane_err:
        cli_mod.cmd_board(
            argparse.Namespace(
                swimlane="invalid_lane",
                category=None,
                priority=None,
                assignee=None,
                sprint=None,
                tag=None,
                query=None,
            ),
            fake_repo,
            json_mode=False,
        )
    assert bad_lane_err.value.error_code == "invalid_swimlane"

    board_buf = io.StringIO()
    board_ic = mock.MagicMock()
    board_ic.__enter__.return_value.request.return_value = {
        "wip_limits": {"todo": 1},
        "columns": {
            "todo": [
                {"key": "ITR-1", "name": "Card Alpha", "priority": "high", "assignee": "alice"},
                {"key": "ITR-2", "name": "Card Beta", "priority": "low", "assignee": None},
            ],
            "done": {"issues": []},
        },
    }
    with (
        mock.patch.object(cli_mod, "ensure_daemon", return_value=("http://127.0.0.1:9", {})),
        mock.patch.object(cli_mod, "ITrackClient", return_value=board_ic),
        mock.patch.object(
            cli_mod, "stdout_console", Console(file=board_buf, force_terminal=False, width=140, highlight=False)
        ),
    ):
        cli_mod.cmd_board(
            argparse.Namespace(
                swimlane="none",
                category=None,
                priority=None,
                assignee=None,
                sprint=None,
                tag=None,
                query=None,
            ),
            fake_repo,
            json_mode=False,
        )
    board_out = board_buf.getvalue()
    assert "All Issues" in " ".join(board_out.split())
    assert "WIP 2/1!" in board_out
    assert "Card Alpha" in board_out
    assert "Card Beta" in board_out

    with (
        mock.patch.object(cli_mod, "ensure_daemon", return_value=("http://127.0.0.1:9", {})),
        mock.patch.object(cli_mod, "ITrackClient", return_value=board_ic),
    ):
        with pytest.raises(cli_mod.CLIError) as bad_scope_err:
            cli_mod.cmd_attach(
                argparse.Namespace(attach_action="list", issue="ITR-1", scope="bogus_scope"),
                fake_repo,
                json_mode=False,
            )
        assert bad_scope_err.value.error_code == "invalid_scope"

    # 11. `main()` entrypoint branches (compound --version, `--` zero-command fallback, handler=None, CLIError human & --json)
    with mock.patch.dict(os.environ, {"ITRACK_SURFACE_LOG": ""}, clear=False):
        ver_h = io.StringIO()
        with mock.patch.object(cli_mod.sys, "stdout", ver_h):
            cli_mod.main(["--root", str(fake_repo), "--version", "status"])
        assert "itrack" in ver_h.getvalue()

        ver_j = io.StringIO()
        with mock.patch.object(cli_mod.sys, "stdout", ver_j):
            cli_mod.main(["--root", str(fake_repo), "--version", "status", "--json"])
        assert json.loads(ver_j.getvalue())["name"] == "itrack"

        assert cli_mod._parse_config_value("custom_bool", "true") is True
        assert cli_mod._parse_config_value("custom_bool", "false") is False

        no_cmd_parser = mock.MagicMock()
        no_cmd_parser._actions = []
        no_cmd_parser._option_string_actions = {}
        no_cmd_parser.parse_args.return_value = argparse.Namespace(version=False, command=None)
        nc_out = io.StringIO()
        with (
            mock.patch.object(cli_mod, "build_parser", return_value=no_cmd_parser),
            mock.patch.object(cli_mod, "render_zero_arg_manual", side_effect=lambda *_a: nc_out.write("NO_CMD_MANUAL")),
        ):
            cli_mod.main(["--root", str(fake_repo), "init"])
        assert nc_out.getvalue() == "NO_CMD_MANUAL"

        no_handler_parser = mock.MagicMock()
        no_handler_parser._actions = []
        no_handler_parser._option_string_actions = {}
        no_handler_parser.parse_args.return_value = argparse.Namespace(version=False, command="init", handler=None)
        nh_out = io.StringIO()
        with (
            mock.patch.object(cli_mod, "build_parser", return_value=no_handler_parser),
            mock.patch.object(cli_mod, "render_zero_arg_manual", side_effect=lambda *_a: nh_out.write("MANUAL_OK")),
        ):
            cli_mod.main(["--root", str(fake_repo), "init"])
        assert nh_out.getvalue() == "MANUAL_OK"

        err_stderr = io.StringIO()
        with mock.patch.object(cli_mod.sys, "stderr", err_stderr):
            with pytest.raises(SystemExit) as main_err_h:
                cli_mod.main(["--root", str(fake_repo), "ui", "--port", "-1"])
        assert main_err_h.value.code == 1
        assert "Invalid port" in err_stderr.getvalue()

        err_stdout = io.StringIO()
        with mock.patch.object(cli_mod.sys, "stdout", err_stdout):
            with pytest.raises(SystemExit) as main_err_j:
                cli_mod.main(["--root", str(fake_repo), "ui", "--port", "-1", "--json"])
        assert main_err_j.value.code == 1
        assert json.loads(err_stdout.getvalue())["error"] == "invalid_port"


def test_cli_end_to_end_remaining_branches_show_badges_and_human_outputs(workspace: ITrackWorkspace) -> None:
    """Exercise remaining real CLI validation & human-output paths (show badges, AC/field/scope/port/swimlane errors, CRUD prints)."""
    # 1. Validation error branches on real CLI (invalid port -1, invalid swimlane, empty field key, invalid scope, AC errors)
    ui_neg = run_itrack_cli("ui", "--port", "-1", "--json", cwd=workspace.root, expect_rc=1, timeout=60.0).json()
    assert ui_neg["error"] == "invalid_port"

    bd_bad = run_itrack_cli(
        "board", "--swimlane", "not_a_swimlane", "--json", cwd=workspace.root, expect_rc=1, timeout=60.0
    ).json()
    assert bd_bad["exit_code"] == 1
    assert bd_bad["error"] in ("invalid_swimlane", "invalid_arguments")

    fld_bad = run_itrack_cli(
        "issue", "create", "Bad Custom Field", "--field", "=missing_key", "--json",
        cwd=workspace.root, expect_rc=1, timeout=60.0,
    ).json()
    assert fld_bad["error"] == "invalid_field"

    # 2. Issue with resolution=fixed, past due_date, active blocker, and archived=True rendered in human `issue show`
    iss1 = _ws_cli_json(
        workspace,
        "issue",
        "create",
        "Resolved Archived Blocked Issue",
        "--due",
        "2020-01-01",
        "--resolution",
        "fixed",
        "--ac",
        "Original AC",
    )
    iss2 = _ws_cli_json(workspace, "issue", "create", "Open Blocker Issue", "--state", "in-progress")
    _ws_cli(workspace, "link", "add", iss2["key"], "blocks", iss1["key"], expect_rc=0)

    # `--clear-ac` combined with `--add-ac` + out-of-range / missing AC checks
    edited_ac = _ws_cli_json(
        workspace, "issue", "edit", iss1["key"], "--clear-ac", "--add-ac", "Fresh AC Item"
    )
    assert [a["text"] for a in edited_ac["acceptance_criteria"]] == ["Fresh AC Item"]

    ac_range_err = run_itrack_cli(
        "issue", "edit", iss1["key"], "--check-ac", "99", "--json", cwd=workspace.root, expect_rc=1, timeout=60.0
    ).json()
    assert ac_range_err["error"] == "invalid_ac_index"

    ac_missing_err = run_itrack_cli(
        "issue", "edit", iss1["key"], "--check-ac", "AC-99", "--json", cwd=workspace.root, expect_rc=4, timeout=60.0
    ).json()
    assert ac_missing_err["error"] == "ac_not_found"

    att_scope_err = run_itrack_cli(
        "attach", "list", iss1["key"], "--scope", "bogus_scope", "--json", cwd=workspace.root, expect_rc=1, timeout=60.0
    ).json()
    assert att_scope_err["exit_code"] == 1
    assert att_scope_err["error"] in ("invalid_scope", "invalid_arguments")

    shown_active = _ws_cli(workspace, "issue", "show", iss1["key"], expect_rc=0)
    assert "res:fixed" in shown_active.stdout
    assert "OVERDUE" in shown_active.stdout
    assert "BLOCKED" in shown_active.stdout

    _ws_cli(workspace, "issue", "archive", iss1["key"], expect_rc=0)
    shown_h = _ws_cli(workspace, "issue", "show", iss1["key"], expect_rc=0)
    assert "res:fixed" in shown_h.stdout
    assert "ARCHIVED" in shown_h.stdout

    # 3. `config set wip_limits.todo 0` + human stale `server status` cleanup note
    _ws_cli_json(workspace, "config", "set", "wip_limits.todo", "3", expect_rc=0)
    cleared_wip = _ws_cli_json(workspace, "config", "set", "wip_limits.todo", "0", expect_rc=0)
    assert "todo" not in cleared_wip["wip_limits"]

    # 4. Human-mode outputs for tag/sprint/milestone/view/worklog/bulk edit & delete subcommands
    _ws_cli(workspace, "tag", "create", "cov-tag", "--color", "#22c55e", expect_rc=0)
    assert "Updated tag" in _ws_cli(workspace, "tag", "edit", "cov-tag", "--desc", "Updated", expect_rc=0).stdout
    assert "Deleted tag" in _ws_cli(workspace, "tag", "delete", "cov-tag", "--yes", expect_rc=0).stdout

    sp = _ws_cli_json(workspace, "sprint", "create", "Cov Sprint")
    assert "Updated sprint" in _ws_cli(workspace, "sprint", "edit", sp["id"], "--goal", "Ship", expect_rc=0).stdout
    _ws_cli(workspace, "sprint", "assign", sp["id"], iss2["key"], expect_rc=0)
    assert "Removed 1 issue(s)" in _ws_cli(workspace, "sprint", "remove", sp["id"], iss2["key"], expect_rc=0).stdout
    _ws_cli(workspace, "sprint", "start", sp["id"], expect_rc=0)
    assert "Completed sprint" in _ws_cli(workspace, "sprint", "complete", sp["id"], expect_rc=0).stdout
    assert "Deleted sprint" in _ws_cli(workspace, "sprint", "delete", sp["id"], "--yes", expect_rc=0).stdout

    mil = _ws_cli_json(workspace, "milestone", "create", "Cov Milestone")
    assert "Updated milestone" in _ws_cli(workspace, "milestone", "edit", mil["id"], "--desc", "M1", expect_rc=0).stdout
    _ws_cli(workspace, "milestone", "close", mil["id"], expect_rc=0)
    assert "Reopened milestone" in _ws_cli(workspace, "milestone", "reopen", mil["id"], expect_rc=0).stdout
    assert "Deleted milestone" in _ws_cli(workspace, "milestone", "delete", mil["id"], "--yes", expect_rc=0).stdout

    vw = _ws_cli_json(workspace, "view", "create", "Cov View", "--query", "priority = medium")
    assert "Updated view" in _ws_cli(workspace, "view", "edit", vw["id"], "--desc", "V1", expect_rc=0).stdout
    assert "Deleted view" in _ws_cli(workspace, "view", "delete", vw["id"], "--yes", expect_rc=0).stdout

    wl = _ws_cli_json(workspace, "worklog", "add", iss2["key"], "15m", "--note", "Quick check")
    wl_id = (wl.get("worklog") or wl)["id"]
    assert "Deleted worklog" in _ws_cli(workspace, "worklog", "delete", iss2["key"], wl_id, "--yes", expect_rc=0).stdout

    sacrificial = _ws_cli_json(workspace, "issue", "create", "Bulk Delete Sacrificial")
    assert "Bulk deleted" in _ws_cli(
        workspace, "bulk", "delete", "--issues", sacrificial["key"], "--yes", expect_rc=0
    ).stdout

    # Human `server status` when daemon.json is stale
    _ws_cli(workspace, "server", "stop", expect_rc=0)
    workspace.daemon_json_path.write_text(
        json.dumps({"pid": _get_dead_pid(), "port": 9, "base_url": "http://127.0.0.1:9", "repo_root": str(workspace.root)}),
        encoding="utf-8",
    )
    stale_h = _ws_cli(workspace, "server", "status", expect_rc=0)
    assert "cleaned stale daemon.json" in stale_h.stdout

