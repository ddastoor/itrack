"""Tests for Architectural Laws (Law 2 Git-Root, Law 3 Agent Manual), global flag parsing, and Daemon lifecycle/concurrency."""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from rich.console import Console

sys.dont_write_bytecode = True

REPO_ROOT_PATH = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_PATH))

from tests.helpers import (  # noqa: E402
    ITRACK_PY,
    ITRACK_SERVER_PY,
    IsolatedRepoTestCase,
    cleanup_repo_daemon,
    init_git_repo,
    load_module_from_path,
    run_itrack_cli,
    run_itrack_json,
)

ITRACK_CLI_MOD = load_module_from_path("itrack_cli", ITRACK_PY)


def invoke_inproc_cli(
    argv: list[str],
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run `itrack.py`'s `main(argv)` in-process while capturing stdout/stderr and Rich consoles."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    prev_out_console = ITRACK_CLI_MOD.stdout_console
    prev_err_console = ITRACK_CLI_MOD.stderr_console
    prev_cwd = Path.cwd()
    ITRACK_CLI_MOD.stdout_console = Console(file=out_buf, force_terminal=False, width=120, highlight=False)
    ITRACK_CLI_MOD.stderr_console = Console(file=err_buf, stderr=True, force_terminal=False, width=120, highlight=False)

    rc = 0
    env_patch = mock.patch.dict(os.environ, env_extra or {}, clear=False)
    try:
        with env_patch, contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            if cwd is not None:
                os.chdir(cwd)
            try:
                ITRACK_CLI_MOD.main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else (1 if exc.code else 0)
    finally:
        if cwd is not None:
            try:
                os.chdir(prev_cwd)
            except OSError:
                pass
        ITRACK_CLI_MOD.stdout_console = prev_out_console
        ITRACK_CLI_MOD.stderr_console = prev_err_console

    return rc, out_buf.getvalue(), err_buf.getvalue()


def list_repo_daemon_pids(repo_dir: Path) -> list[int]:
    """Return PIDs of live `itrack-server.py` processes bound to `repo_dir` without column truncation."""
    target = str(repo_dir.resolve())
    pids: list[int] = []
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
            if "itrack-server.py" in cmdline and target in cmdline:
                pids.append(int(entry.name))
        return sorted(pids)

    proc = subprocess.run(["ps", "-eo", "pid=,args=", "-ww"], capture_output=True, text=True, check=False)
    for line in proc.stdout.splitlines():
        if "itrack-server.py" in line and target in line:
            parts = line.split()
            if parts and parts[0].isdigit():
                pids.append(int(parts[0]))
    return sorted(pids)


class TestLaw2GitRootEnforcement(unittest.TestCase):
    """Law 2: Reject non-Git directories and Git subdirectories with exit code 2 and zero disk writes."""

    def setUp(self) -> None:
        super().setUp()
        self.nongit_dir = Path(tempfile.mkdtemp(prefix="itrack_nongit_"))
        self.git_dir = init_git_repo(Path(tempfile.mkdtemp(prefix="itrack_gitroot_")))
        self.subdir = self.git_dir / "packages" / "nested"
        self.subdir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        try:
            cleanup_repo_daemon(self.git_dir)
        finally:
            shutil.rmtree(self.nongit_dir, ignore_errors=True)
            shutil.rmtree(self.git_dir, ignore_errors=True)
        super().tearDown()

    def test_nongit_directory_rejected_by_cli_and_server(self) -> None:
        # 1. Subprocess CLI text mode
        p_txt = run_itrack_cli(self.nongit_dir)
        self.assertEqual(p_txt.returncode, 2)
        self.assertIn("Law 2", p_txt.stderr)
        self.assertFalse((self.nongit_dir / "itrack-issues").exists())

        # 2. Subprocess CLI --json mode
        rc_json, payload, _ = run_itrack_json(self.nongit_dir, expect_rc=2)
        self.assertEqual(rc_json, 2)
        self.assertEqual(payload["error"], "not_git_root")
        self.assertEqual(Path(payload["path"]).resolve(), self.nongit_dir.resolve())
        self.assertFalse((self.nongit_dir / "itrack-issues").exists())

        # 3. Subprocess itrack-server.py
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p_srv = subprocess.run(
            [sys.executable, "-B", str(ITRACK_SERVER_PY)],
            cwd=str(self.nongit_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        self.assertEqual(p_srv.returncode, 2)
        self.assertIn("Law 2", p_srv.stderr)
        self.assertFalse((self.nongit_dir / "itrack-issues").exists())

        # 4. In-process CLI check (both text and --json)
        rc_in_txt, _, err_in_txt = invoke_inproc_cli(["--root", str(self.nongit_dir), "list"])
        self.assertEqual(rc_in_txt, 2)
        self.assertIn("Law 2", err_in_txt)

        rc_in_json, out_in_json, _ = invoke_inproc_cli(["--root", str(self.nongit_dir), "list", "--json"])
        self.assertEqual(rc_in_json, 2)
        self.assertEqual(json.loads(out_in_json)["error"], "not_git_root")
        self.assertFalse((self.nongit_dir / "itrack-issues").exists())

    def test_git_subdirectory_rejected_with_cd_hint(self) -> None:
        expected_root = str(self.git_dir.resolve())
        expected_hint = f"Hint: cd {expected_root}"

        # Subprocess text mode
        p_txt = run_itrack_cli(self.subdir, "list", cwd=self.subdir)
        self.assertEqual(p_txt.returncode, 2)
        self.assertIn(expected_hint, p_txt.stderr)
        self.assertFalse((self.subdir / "itrack-issues").exists())
        self.assertFalse((self.git_dir / "itrack-issues").exists())

        # Subprocess --json mode
        rc_json, payload, _ = run_itrack_json(self.subdir, "list", cwd=self.subdir, expect_rc=2)
        self.assertEqual(rc_json, 2)
        self.assertEqual(payload["error"], "not_git_root")
        self.assertEqual(payload["hint_git_root"], expected_root)
        self.assertIn(expected_hint, payload["message"])

        # Subprocess itrack-server.py in subdirectory
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p_srv = subprocess.run(
            [sys.executable, "-B", str(ITRACK_SERVER_PY)],
            cwd=str(self.subdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        self.assertEqual(p_srv.returncode, 2)
        self.assertIn(expected_hint, p_srv.stderr)
        self.assertFalse((self.subdir / "itrack-issues").exists())
        self.assertFalse((self.git_dir / "itrack-issues").exists())

    def test_root_flag_and_itrack_root_env_from_outside_repo(self) -> None:
        expected_root = str(self.git_dir.resolve())

        # --root <path> from non-git cwd
        rc1, out1, _ = invoke_inproc_cli(["--root", expected_root, "--json"], cwd=self.nongit_dir)
        self.assertEqual(rc1, 0)
        self.assertEqual(json.loads(out1)["repo_root"], expected_root)

        # --root=<path> from non-git cwd
        rc2, out2, _ = invoke_inproc_cli([f"--root={expected_root}", "--json"], cwd=self.nongit_dir)
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(out2)["repo_root"], expected_root)

        # ITRACK_ROOT env var from non-git cwd
        rc3, out3, _ = invoke_inproc_cli(["--json"], cwd=self.nongit_dir, env_extra={"ITRACK_ROOT": expected_root})
        self.assertEqual(rc3, 0)
        self.assertEqual(json.loads(out3)["repo_root"], expected_root)

        # --root overrides invalid ITRACK_ROOT
        rc4, out4, _ = invoke_inproc_cli(
            ["--root", expected_root, "--json"],
            cwd=self.nongit_dir,
            env_extra={"ITRACK_ROOT": str(self.nongit_dir)},
        )
        self.assertEqual(rc4, 0)
        self.assertEqual(json.loads(out4)["repo_root"], expected_root)
        self.assertFalse((self.nongit_dir / "itrack-issues").exists())


class TestLaw3ZeroArgAgentManual(IsolatedRepoTestCase):
    """Law 3: Zero-arg, `manual`, and `--manual` emit rich/JSON manual without spawning daemon or touching disk."""

    def test_zero_arg_and_manual_modes_never_spawn_daemon_or_create_storage(self) -> None:
        # 1. Subprocess zero-arg text mode
        p_txt = self.cli()
        self.assertEqual(p_txt.returncode, 0)
        self.assertIn("Agent & Operator Manual", p_txt.stdout)
        self.assertIn("iQL", p_txt.stdout)
        self.assertFalse((self.repo_dir / "itrack-issues").exists())
        self.assertEqual(list_repo_daemon_pids(self.repo_dir), [])

        # 2. Subprocess zero-arg --json mode
        data = self.cli_json()
        for key in (
            "name",
            "version",
            "repo_root",
            "summary",
            "laws",
            "global_flags",
            "storage",
            "daemon",
            "workflows",
            "iql_syntax",
            "commands",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["name"], "itrack")
        self.assertEqual(Path(data["repo_root"]).resolve(), self.repo_dir.resolve())
        self.assertFalse((self.repo_dir / "itrack-issues").exists())
        self.assertEqual(list_repo_daemon_pids(self.repo_dir), [])

        # 3. In-process `manual`, `--manual`, and `--version` / `-V`
        for argv in (
            ["--root", str(self.repo_dir)],
            ["--root", str(self.repo_dir), "manual"],
            ["--root", str(self.repo_dir), "--manual"],
            ["--root", str(self.repo_dir), "manual", "--json"],
            ["--root", str(self.repo_dir), "--manual", "--json"],
        ):
            rc, out, _ = invoke_inproc_cli(argv)
            self.assertEqual(rc, 0)
            self.assertTrue(out.strip())
            if "--json" in argv:
                parsed = json.loads(out)
                self.assertIn("laws", parsed)
                self.assertIn("commands", parsed)

        rc_v_txt, out_v_txt, _ = invoke_inproc_cli(["--root", str(self.repo_dir), "--version"])
        self.assertEqual(rc_v_txt, 0)
        self.assertIn("itrack", out_v_txt)

        rc_v_json, out_v_json, _ = invoke_inproc_cli(["--root", str(self.repo_dir), "-V", "--json"])
        self.assertEqual(rc_v_json, 0)
        self.assertEqual(json.loads(out_v_json)["name"], "itrack")

        self.assertFalse((self.repo_dir / "itrack-issues").exists())
        self.assertEqual(list_repo_daemon_pids(self.repo_dir), [])


class TestExtractGlobalFlagsMatrix(unittest.TestCase):
    """Exhaustive unit matrix for `extract_global_flags` position independence and value protection."""

    def test_json_and_root_in_all_positions(self) -> None:
        extract = ITRACK_CLI_MOD.extract_global_flags

        j, r, rem = extract(["--json", "--root", "/tmp/repo", "issue", "list", "-s", "todo"])
        self.assertTrue(j)
        self.assertEqual(r, "/tmp/repo")
        self.assertEqual(rem, ["issue", "list", "-s", "todo"])

        j, r, rem = extract(["issue", "--json", "list", "--root=/tmp/repo2", "-p", "high"])
        self.assertTrue(j)
        self.assertEqual(r, "/tmp/repo2")
        self.assertEqual(rem, ["issue", "list", "-p", "high"])

        j, r, rem = extract(["issue", "list", "-s", "done", "--root", "/tmp/repo3", "--json"])
        self.assertTrue(j)
        self.assertEqual(r, "/tmp/repo3")
        self.assertEqual(rem, ["issue", "list", "-s", "done"])

    def test_double_dash_and_option_values_preserve_literal_tokens(self) -> None:
        extract = ITRACK_CLI_MOD.extract_global_flags

        # `--` separator preserves literal `--json` and `--root`
        j, r, rem = extract(["create", "--", "--json", "--root", "/not/global"])
        self.assertFalse(j)
        self.assertIsNone(r)
        self.assertEqual(rem, ["create", "--", "--json", "--root", "/not/global"])

        # Option taking a value (`-d`, `-m`, `--title`) preserves `--json` / `--root` as its argument
        j2, r2, rem2 = extract(["create", "My Issue", "-d", "--json", "--root", "/real/root"])
        self.assertFalse(j2)
        self.assertEqual(r2, "/real/root")
        self.assertEqual(rem2, ["create", "My Issue", "-d", "--json"])

        j3, r3, rem3 = extract(["comment", "add", "ITK-1", "-d", "--root"])
        self.assertFalse(j3)
        self.assertIsNone(r3)
        self.assertEqual(rem3, ["comment", "add", "ITK-1", "-d", "--root"])

    def test_boolean_vs_value_iql_and_agent_disambiguation(self) -> None:
        extract = ITRACK_CLI_MOD.extract_global_flags

        # `search` uses boolean `--iql`, so following `--json` MUST be extracted as global json_mode=True
        j, r, rem = extract(["search", "status = todo", "--iql", "--json"])
        self.assertTrue(j)
        self.assertIsNone(r)
        self.assertEqual(rem, ["search", "status = todo", "--iql"])

        # `list`, `board`, and `view` use value-taking `--iql <EXPR>`, so literal `"--json"` is kept as EXPR
        for cmd_prefix in (["list"], ["board"], ["view", "create", "V1"]):
            j_val, _, rem_val = extract([*cmd_prefix, "--iql", "--json"])
            self.assertFalse(j_val)
            self.assertEqual(rem_val, [*cmd_prefix, "--iql", "--json"])

            j_both, _, rem_both = extract([*cmd_prefix, "--iql", "status = todo", "--json"])
            self.assertTrue(j_both)
            self.assertEqual(rem_both, [*cmd_prefix, "--iql", "status = todo"])

        # `user` uses boolean `--agent`, so following `--json` MUST be extracted as global json_mode=True
        j_usr, _, rem_usr = extract(["user", "add", "bot-1", "--agent", "--json"])
        self.assertTrue(j_usr)
        self.assertEqual(rem_usr, ["user", "add", "bot-1", "--agent"])

        # `claim` and `release` use value-taking `--agent <ID>`, so literal `"--json"` is kept as agent ID
        for cmd in ("claim", "release"):
            j_ag, _, rem_ag = extract([cmd, "ITK-1", "--agent", "--json"])
            self.assertFalse(j_ag)
            self.assertEqual(rem_ag, [cmd, "ITK-1", "--agent", "--json"])

            j_ag2, _, rem_ag2 = extract([cmd, "ITK-1", "--agent", "coder-1", "--json"])
            self.assertTrue(j_ag2)
            self.assertEqual(rem_ag2, [cmd, "ITK-1", "--agent", "coder-1"])

    def test_malformed_root_flag_raises_cli_error(self) -> None:
        extract = ITRACK_CLI_MOD.extract_global_flags
        with self.assertRaises(ITRACK_CLI_MOD.CLIError) as ctx1:
            extract(["list", "--root"])
        self.assertEqual(ctx1.exception.status_code, 400)

        with self.assertRaises(ITRACK_CLI_MOD.CLIError) as ctx2:
            extract(["--root=", "list"])
        self.assertEqual(ctx2.exception.status_code, 400)


class TestDaemonLifecycleAndConcurrency(IsolatedRepoTestCase):
    """Auto-daemon cold-start flock race, multi-repo isolation, proxy resilience, and daemon/ui commands."""

    def test_cold_start_6_way_simultaneous_spawn_produces_single_daemon_zero_orphans(self) -> None:
        self.assertEqual(list_repo_daemon_pids(self.repo_dir), [])
        barrier = threading.Barrier(6)

        def _worker(_idx: int) -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=10.0)
            return run_itrack_cli(self.repo_dir, "list", "--json")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            procs = list(pool.map(_worker, range(6)))

        for proc in procs:
            self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}")
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["issues"], [])
            self.assertEqual(payload["total"], 0)

        live_pids = list_repo_daemon_pids(self.repo_dir)
        self.assertEqual(len(live_pids), 1, f"Expected strictly 1 daemon PID, got {live_pids}")

        stop_data = self.cli_json("daemon", "stop")
        self.assertTrue(stop_data["stopped"])
        self.assertEqual(list_repo_daemon_pids(self.repo_dir), [])
        self.assertFalse((self.repo_dir / "itrack-issues" / ".runtime" / "daemon.json").exists())

    def test_two_simultaneous_isolated_repos_have_distinct_ports_and_zero_crosstalk(self) -> None:
        repo_b = init_git_repo(Path(tempfile.mkdtemp(prefix="itrack_repob_")))
        self.addCleanup(lambda: shutil.rmtree(repo_b, ignore_errors=True))
        self.addCleanup(lambda: cleanup_repo_daemon(repo_b))

        init_a = self.cli_json("init", "--key", "RPA", "--name", "Repo Alpha")
        _, init_b, _ = run_itrack_json(repo_b, "init", "--key", "RPB", "--name", "Repo Beta")

        self.assertNotEqual(init_a["daemon_pid"], init_b["daemon_pid"])
        self.assertNotEqual(init_a["daemon_url"], init_b["daemon_url"])

        iss_a = self.cli_json("create", "Alpha Exclusive Issue")
        _, iss_b, _ = run_itrack_json(repo_b, "create", "Beta Exclusive Issue")
        self.assertEqual(iss_a["key"], "RPA-1")
        self.assertEqual(iss_b["key"], "RPB-1")

        list_a = self.cli_json("list")
        _, list_b, _ = run_itrack_json(repo_b, "list")
        self.assertEqual([i["key"] for i in list_a["issues"]], ["RPA-1"])
        self.assertEqual([i["key"] for i in list_b["issues"]], ["RPB-1"])

        # Stopping repo_a leaves repo_b healthy
        self.cli_json("daemon", "stop")
        _, status_b, _ = run_itrack_json(repo_b, "daemon", "status")
        self.assertTrue(status_b["running"])
        run_itrack_json(repo_b, "daemon", "stop")

    def test_poisoned_proxy_environment_resilience(self) -> None:
        poison_env = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
        }
        init_res = self.cli_json("init", "--key", "PRX", env_extra=poison_env)
        self.assertEqual(init_res["config"]["project_key"], "PRX")

        created = self.cli_json("create", "Works Under Poisoned Proxy", env_extra=poison_env)
        self.assertEqual(created["key"], "PRX-1")

        status = self.cli_json("daemon", "status", env_extra=poison_env)
        self.assertTrue(status["running"])

        stopped = self.cli_json("daemon", "stop", env_extra=poison_env)
        self.assertTrue(stopped["stopped"])

    def test_daemon_server_alias_and_ui_commands_in_text_and_json(self) -> None:
        root = str(self.repo_dir)

        # 1. Stopped status (text + json)
        rc, out_stopped, _ = invoke_inproc_cli(["--root", root, "daemon", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("stopped", out_stopped.lower())

        rc, out_stopped_j, _ = invoke_inproc_cli(["--root", root, "daemon", "status", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out_stopped_j)["running"])

        # 2. Start daemon (text) + idempotent second start (json & text via `server` alias)
        rc, out_start, _ = invoke_inproc_cli(["--root", root, "daemon", "start"])
        self.assertEqual(rc, 0)
        self.assertIn("Started daemon", out_start)

        rc, out_start2_j, _ = invoke_inproc_cli(["--root", root, "server", "start", "--json"])
        self.assertEqual(rc, 0)
        start2_data = json.loads(out_start2_j)
        self.assertTrue(start2_data["already_running"])
        first_pid = start2_data["pid"]

        rc, out_start2_txt, _ = invoke_inproc_cli(["--root", root, "server", "start"])
        self.assertEqual(rc, 0)
        self.assertIn("already running", out_start2_txt.lower())

        # 3. Running status & logs (text + json)
        rc, out_run_txt, _ = invoke_inproc_cli(["--root", root, "server", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("Daemon running", out_run_txt)

        rc, out_logs_txt, _ = invoke_inproc_cli(["--root", root, "daemon", "logs", "-n", "10"])
        self.assertEqual(rc, 0)
        self.assertTrue(out_logs_txt.strip())

        rc, out_logs_j, _ = invoke_inproc_cli(["--root", root, "daemon", "logs", "-n", "10", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn("log_path", json.loads(out_logs_j))

        # 4. Restart daemon (text + json)
        rc, out_rst_txt, _ = invoke_inproc_cli(["--root", root, "daemon", "restart"])
        self.assertEqual(rc, 0)
        self.assertIn("Restarted daemon", out_rst_txt)

        rc, out_rst_j, _ = invoke_inproc_cli(["--root", root, "daemon", "restart", "--json"])
        self.assertEqual(rc, 0)
        rst_data = json.loads(out_rst_j)
        self.assertTrue(rst_data["restarted"])
        self.assertNotEqual(rst_data["pid"], first_pid)

        # 5. UI launcher (`ui --no-open` in text + json + explicit ephemeral port switch)
        rc, out_ui_txt, _ = invoke_inproc_cli(["--root", root, "ui", "--no-open"])
        self.assertEqual(rc, 0)
        self.assertIn("Web UI URL", out_ui_txt)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            custom_port = s.getsockname()[1]

        rc, out_ui_j, _ = invoke_inproc_cli(["--root", root, "ui", "--port", str(custom_port), "--no-open", "--json"])
        self.assertEqual(rc, 0)
        ui_data = json.loads(out_ui_j)
        self.assertFalse(ui_data["opened_browser"])
        self.assertEqual(ui_data["port"], custom_port)

        with httpx.Client(timeout=5.0, trust_env=False) as client:
            resp = client.get(ui_data["url"])
            self.assertEqual(resp.status_code, 200)
            ext_refs = re.findall(r'(?:src|href)=["\'](https?://[^"\']+)["\']', resp.text, flags=re.I)
            self.assertEqual(ext_refs, [])

        # 6. Stop daemon (text) + idempotent stop when already stopped (text + json)
        rc, out_stop_txt, _ = invoke_inproc_cli(["--root", root, "daemon", "stop"])
        self.assertEqual(rc, 0)
        self.assertIn("Stopped daemon", out_stop_txt)

        rc, out_stop_again_txt, _ = invoke_inproc_cli(["--root", root, "daemon", "stop"])
        self.assertEqual(rc, 0)
        self.assertIn("not running", out_stop_again_txt.lower())

        rc, out_stop_again_j, _ = invoke_inproc_cli(["--root", root, "daemon", "stop", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out_stop_again_j)["stopped"])


if __name__ == "__main__":
    unittest.main()
