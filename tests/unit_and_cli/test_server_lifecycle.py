"""Unit, edge-case, and CLI subprocess tests for background server lifecycle management."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

import itrack_core
from itrack_core import (
    ConflictError,
    ITrackError,
    ServerError,
    ServerPaths,
    ValidationError,
    get_server_paths,
    get_server_status,
    restart_server,
    run_cli,
    start_server,
    stop_server,
)
from itrack_core import server_ctl
from tests.unit_and_cli.conftest import CLIRunner, ITRACK_CLI, PYTHON_BIN, REPO_ROOT

START_JSON_KEYS = {
    "ok",
    "status",
    "started",
    "running",
    "healthy",
    "stale_cleaned",
    "pid",
    "host",
    "port",
    "url",
    "data_dir",
    "pid_file",
    "meta_file",
    "log_file",
    "started_at",
}

RESTART_JSON_KEYS = START_JSON_KEYS | {"restarted", "was_running", "previous_pid"}

STATUS_JSON_KEYS = {
    "ok",
    "status",
    "running",
    "healthy",
    "stale_cleaned",
    "pid",
    "host",
    "port",
    "url",
    "data_dir",
    "pid_file",
    "meta_file",
    "log_file",
    "started_at",
}

STOP_JSON_KEYS = {
    "ok",
    "status",
    "running",
    "stopped",
    "was_running",
    "forced",
    "signal",
    "stale_cleaned",
    "pid",
    "host",
    "port",
    "url",
    "data_dir",
    "pid_file",
    "meta_file",
    "log_file",
}

META_FILE_KEYS = {
    "pid",
    "host",
    "port",
    "url",
    "data_dir",
    "pid_file",
    "meta_file",
    "log_file",
    "started_at",
    "command",
}


def _reserve_free_port(host: str = "127.0.0.1") -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _safe_kill_pid(pid: int | None) -> None:
    if pid is None or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _assert_iso_utc_z(ts: Any) -> None:
    assert isinstance(ts, str) and ts.endswith("Z")
    parsed = datetime.fromisoformat(ts[:-1] + "+00:00")
    assert parsed.utcoffset() is not None


class TestServerLifecycleUnitAndCLI:
    """Verify background start, stop, status, restart, isolation, and error semantics."""

    def test_exports_constants_exception_health_and_features_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api_client: Any, cli_runner: CLIRunner
    ) -> None:
        for sym in (
            "ServerError",
            "ServerPaths",
            "get_server_paths",
            "get_server_status",
            "start_server",
            "stop_server",
            "restart_server",
        ):
            assert hasattr(itrack_core, sym)
            assert sym in itrack_core.__all__

        assert server_ctl.SERVER_PID_FILENAME == "server.pid"
        assert server_ctl.SERVER_META_FILENAME == "server.json"
        assert server_ctl.SERVER_LOG_FILENAME == "server.log"
        assert server_ctl.SERVER_LOCK_FILENAME == "server.lock"
        assert server_ctl.DEFAULT_SERVER_HOST == "127.0.0.1"
        assert server_ctl.DEFAULT_SERVER_PORT == 8000
        assert server_ctl.DEFAULT_START_TIMEOUT == 15.0
        assert server_ctl.DEFAULT_STOP_TIMEOUT == 5.0

        err = ServerError("boom")
        assert isinstance(err, ITrackError)
        assert err.code == "server_error"
        assert err.exit_code == 1
        assert err.status_code == 500

        custom_dir = tmp_path / "env_paths_dir"
        monkeypatch.setenv("ITRACK_DATA_DIR", str(custom_dir))
        paths = get_server_paths(None)
        assert isinstance(paths, ServerPaths)
        assert paths.data_dir == custom_dir.resolve()
        assert paths.pid_file == custom_dir.resolve() / "server.pid"
        assert paths.meta_file == custom_dir.resolve() / "server.json"
        assert paths.log_file == custom_dir.resolve() / "server.log"
        assert paths.lock_file == custom_dir.resolve() / "server.lock"
        assert not custom_dir.exists()
        with pytest.raises(FrozenInstanceError):
            paths.pid_file = custom_dir / "other.pid"  # type: ignore[misc]

        health = api_client.get("/api/health")
        assert health.status_code == 200
        h_json = health.json()
        assert h_json["status"] == "ok"
        assert h_json["pid"] == os.getpid()
        assert h_json["version"] == itrack_core.VERSION
        assert Path(h_json["data_dir"]).resolve() == cli_runner.default_data_dir.resolve()

        feats_api = api_client.get("/api/features").json()
        assert feats_api["total"] == 17
        slugs = {f["slug"] for f in feats_api["features"]}
        assert "background-server-management" in slugs

        feats_cli = cli_runner.run_json("features")
        assert feats_cli["total"] == 17
        assert any(f["slug"] == "background-server-management" for f in feats_cli["features"])

        feats_human = cli_runner.run_ok("features").stdout
        assert "Background Server Lifecycle Management" in feats_human

    def test_status_and_stop_do_not_create_missing_data_dir(self, tmp_path: Path) -> None:
        missing_dir = tmp_path / "never_created_dir"
        assert not missing_dir.exists()

        runner = CLIRunner(missing_dir)
        status_res = runner.run_json("server", "status")
        assert set(status_res.keys()) == STATUS_JSON_KEYS
        assert status_res["ok"] is True
        assert status_res["status"] == "stopped"
        assert status_res["running"] is False
        assert status_res["healthy"] is False
        assert status_res["stale_cleaned"] is False
        assert status_res["pid"] is None
        assert status_res["host"] is None
        assert status_res["port"] is None
        assert status_res["url"] is None
        assert status_res["started_at"] is None
        assert not missing_dir.exists()

        stop_res = runner.run_json("server", "stop")
        assert set(stop_res.keys()) == STOP_JSON_KEYS
        assert stop_res["ok"] is True
        assert stop_res["status"] == "not_running"
        assert stop_res["running"] is False
        assert stop_res["stopped"] is False
        assert stop_res["was_running"] is False
        assert stop_res["forced"] is False
        assert stop_res["signal"] is None
        assert stop_res["stale_cleaned"] is False
        assert stop_res["pid"] is None
        assert stop_res["host"] is None
        assert stop_res["port"] is None
        assert stop_res["url"] is None
        assert not missing_dir.exists()

    def test_start_status_stop_detached_lifecycle_schemas_and_live_http_parity(
        self, data_dir: Path, cli_runner: CLIRunner
    ) -> None:
        paths = get_server_paths(data_dir)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("ITRACK_DATA_DIR", None)

        try:
            # Direct subprocess.run(..., capture_output=True) must exit 0 promptly without hanging on pipes.
            proc = subprocess.run(
                [
                    PYTHON_BIN,
                    "-B",
                    str(ITRACK_CLI),
                    "--data-dir",
                    str(data_dir),
                    "server",
                    "start",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--json",
                ],
                cwd=str(REPO_ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            assert proc.returncode == 0, f"STDOUT={proc.stdout}\nSTDERR={proc.stderr}"
            start_res = json.loads(proc.stdout)
            assert set(start_res.keys()) == START_JSON_KEYS
            assert start_res["ok"] is True
            assert start_res["status"] == "running"
            assert start_res["started"] is True
            assert start_res["running"] is True
            assert start_res["healthy"] is True
            assert start_res["stale_cleaned"] is False
            pid = start_res["pid"]
            assert isinstance(pid, int) and pid > 0
            assert start_res["host"] == "127.0.0.1"
            assert isinstance(start_res["port"], int) and start_res["port"] > 0
            url = start_res["url"]
            assert url == f"http://127.0.0.1:{start_res['port']}"
            assert Path(start_res["data_dir"]).resolve() == data_dir.resolve()
            assert Path(start_res["pid_file"]).resolve() == paths.pid_file.resolve()
            assert Path(start_res["meta_file"]).resolve() == paths.meta_file.resolve()
            assert Path(start_res["log_file"]).resolve() == paths.log_file.resolve()
            _assert_iso_utc_z(start_res["started_at"])

            # Detached in its own session, distinct from caller session/process group.
            assert os.getsid(pid) == pid
            assert os.getsid(pid) != os.getsid(os.getpid())

            # Inspect /proc/<pid>/fd when readable: stdin=/dev/null, stdout & stderr=server.log
            fd_dir = Path(f"/proc/{pid}/fd")
            if fd_dir.exists():
                try:
                    assert os.readlink(fd_dir / "0") == os.devnull
                    assert Path(os.readlink(fd_dir / "1")).resolve() == paths.log_file.resolve()
                    assert Path(os.readlink(fd_dir / "2")).resolve() == paths.log_file.resolve()
                except PermissionError:
                    pass

            assert paths.pid_file.read_text(encoding="utf-8").strip() == str(pid)
            meta = json.loads(paths.meta_file.read_text(encoding="utf-8"))
            assert set(meta.keys()) == META_FILE_KEYS
            assert meta["pid"] == pid
            assert meta["host"] == "127.0.0.1"
            assert meta["port"] == start_res["port"]
            assert meta["url"] == url
            assert Path(meta["data_dir"]).resolve() == data_dir.resolve()
            assert meta["started_at"] == start_res["started_at"]
            assert isinstance(meta["command"], list)
            assert str(REPO_ROOT / "itrack-server.py") in meta["command"]
            assert str(start_res["port"]) in meta["command"]
            assert paths.log_file.is_file()

            # Live HTTP endpoints (/api/health, /api/features, /, /ui, /api/issues) & shared CLI/API storage
            with httpx.Client(trust_env=False, timeout=3.0) as client:
                h_resp = client.get(f"{url}/api/health")
                assert h_resp.status_code == 200
                h_data = h_resp.json()
                assert h_data["status"] == "ok"
                assert h_data["pid"] == pid
                assert h_data["version"] == itrack_core.VERSION
                assert Path(h_data["data_dir"]).resolve() == data_dir.resolve()

                feat_resp = client.get(f"{url}/api/features")
                assert feat_resp.status_code == 200
                assert feat_resp.json()["total"] == 17

                ui_resp = client.get(f"{url}/")
                assert ui_resp.status_code == 200
                assert "iTrack" in ui_resp.text

                ui_alias_resp = client.get(f"{url}/ui")
                assert ui_alias_resp.status_code == 200
                assert "iTrack" in ui_alias_resp.text

                list_empty = client.get(f"{url}/api/issues")
                assert list_empty.status_code == 200
                assert list_empty.json()["total"] == 0

                # CLI create -> visible over HTTP API
                cli_created = cli_runner.run_json(
                    "create",
                    "--summary",
                    "CLI created while bg running",
                    "--description",
                    "<p>Shared store verification</p>",
                )
                assert cli_created["number"] == 1

                api_list = client.get(f"{url}/api/issues").json()
                assert api_list["total"] == 1
                assert api_list["items"][0]["summary"] == "CLI created while bg running"

                api_show = client.get(f"{url}/api/issues/1")
                assert api_show.status_code == 200
                assert api_show.json()["summary"] == "CLI created while bg running"

                # HTTP POST issue & comment -> visible via CLI
                http_created = client.post(
                    f"{url}/api/issues",
                    json={"summary": "HTTP created while bg running", "description": "<p>API body</p>"},
                )
                assert http_created.status_code == 201
                assert http_created.json()["number"] == 2

                http_comment = client.post(
                    f"{url}/api/issues/1/comments",
                    json={"body": "<p>Comment via live background HTTP</p>", "author": "api_bot"},
                )
                assert http_comment.status_code == 201

            cli_show_2 = cli_runner.run_json("show", "2")
            assert cli_show_2["summary"] == "HTTP created while bg running"
            cli_comments_1 = cli_runner.run_json("comment", "list", "1")
            assert len(cli_comments_1["comments"]) == 1
            assert "Comment via live background HTTP" in cli_comments_1["comments"][0]["body"]

            log_text = paths.log_file.read_text(encoding="utf-8", errors="replace")
            assert log_text.strip() != ""
            assert "/api/health" in log_text or "Uvicorn running" in log_text

            # Status reports running & healthy.
            stat_res = cli_runner.run_json("server", "status")
            assert set(stat_res.keys()) == STATUS_JSON_KEYS
            assert stat_res["ok"] is True
            assert stat_res["status"] == "running"
            assert stat_res["running"] is True
            assert stat_res["healthy"] is True
            assert stat_res["stale_cleaned"] is False
            assert stat_res["pid"] == pid
            assert stat_res["port"] == start_res["port"]
            assert stat_res["url"] == url

            # Stop server gracefully via SIGTERM.
            stop_res = cli_runner.run_json("server", "stop")
            assert set(stop_res.keys()) == STOP_JSON_KEYS
            assert stop_res["ok"] is True
            assert stop_res["status"] == "stopped"
            assert stop_res["running"] is False
            assert stop_res["stopped"] is True
            assert stop_res["was_running"] is True
            assert stop_res["forced"] is False
            assert stop_res["signal"] == "SIGTERM"
            assert stop_res["pid"] == pid
            assert not paths.pid_file.exists()
            assert not paths.meta_file.exists()
            assert paths.log_file.is_file()

            # Verify process is gone and port/HTTP endpoint are unreachable.
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                assert sock.connect_ex(("127.0.0.1", start_res["port"])) != 0
            with pytest.raises(httpx.HTTPError):
                with httpx.Client(trust_env=False, timeout=0.5) as client:
                    client.get(f"{url}/api/health")

            # Idempotent second stop.
            stop_again = cli_runner.run_json("stop")
            assert set(stop_again.keys()) == STOP_JSON_KEYS
            assert stop_again["status"] == "not_running"
            assert stop_again["stopped"] is False
            assert stop_again["was_running"] is False
            assert stop_again["signal"] is None
            assert stop_again["pid"] is None
        finally:
            stop_server(data_dir, force=True)

    def test_aliases_env_data_dir_position_independent_flags_isolation_and_human_output(
        self, tmp_path: Path
    ) -> None:
        dir_a = tmp_path / "store_a"
        dir_b = tmp_path / "store_b"
        dir_c = tmp_path / "store_c"
        runner_a = CLIRunner(dir_a)
        runner_b = CLIRunner(dir_b)
        runner_c = CLIRunner(dir_c)
        try:
            # Top-level `start` using ITRACK_DATA_DIR (no --data-dir flag) + human output
            res_start = runner_a.run_ok("start", "--port", "0")
            assert "iTrack server started:" in res_start.stdout
            assert "http://127.0.0.1:" in res_start.stdout

            stat_human = runner_a.run_ok("--json", "server", "status")
            stat_a_init = stat_human.json()
            assert stat_a_init["running"] is True

            stat_human_text = runner_a.run_ok("server", "status").stdout
            assert "running (healthy)" in stat_human_text.lower()

            # Second independent server via `serve -d --data-dir <dir_b>` (position-independent --data-dir after subcommand)
            res_b = runner_b.run_json("serve", "-d", "--port", "0", "--data-dir", str(dir_b), use_env_data_dir=False)
            assert res_b["started"] is True
            assert res_b["running"] is True
            assert Path(res_b["data_dir"]).resolve() == dir_b.resolve()

            # Third server via `ui --background` / `ui -d` and `serve --detach`
            res_c = runner_c.run_json("ui", "-d", "--port", "0")
            assert res_c["started"] is True
            stop_c = runner_c.run_json("stop")
            assert stop_c["stopped"] is True

            res_c2 = runner_c.run_json("serve", "--detach", "--port", "0")
            assert res_c2["started"] is True
            runner_c.run_ok("stop")

            # Servers A and B remain healthy simultaneously on different ports
            stat_a = runner_a.run_json("server", "status")
            stat_b = runner_b.run_json("server", "status")
            assert stat_a["pid"] != stat_b["pid"]
            assert stat_a["port"] != stat_b["port"]
            assert stat_a["healthy"] is True
            assert stat_b["healthy"] is True

            # Stopping A does not disturb B
            stop_a = runner_a.run_ok("stop")
            assert "stopped" in stop_a.stdout.lower()
            stat_a_after = runner_a.run_ok("server", "status")
            assert "stopped" in stat_a_after.stdout.lower()
            assert "not running" in runner_a.run_ok("server", "stop").stdout.lower()

            assert runner_b.run_json("server", "status")["healthy"] is True
        finally:
            stop_server(dir_a, force=True)
            stop_server(dir_b, force=True)
            stop_server(dir_c, force=True)

    def test_double_start_conflict_across_all_entrypoints(
        self, data_dir: Path, cli_runner: CLIRunner
    ) -> None:
        paths = get_server_paths(data_dir)
        try:
            start_info = cli_runner.run_json("server", "start", "--port", "0")
            orig_pid = start_info["pid"]

            for cmd_args in (
                ("server", "start", "--port", "0", "--json"),
                ("start", "--port", "0", "--json"),
                ("ui", "--background", "--port", "0", "--json"),
                ("serve", "-d", "--port", "0", "--json"),
            ):
                dup = cli_runner.run(*cmd_args)
                assert dup.returncode == 1
                err_json = dup.json()
                assert err_json["ok"] is False
                assert err_json["error"] == "server_already_running"
                assert err_json["exit_code"] == 1
                assert "Error:" in dup.stderr

            # Human-readable conflict error
            dup_human = cli_runner.run("server", "start", "--port", "0")
            assert dup_human.returncode == 1
            assert "Error:" in dup_human.stderr
            assert paths.pid_file.read_text(encoding="utf-8").strip() == str(orig_pid)
            assert get_server_status(data_dir)["healthy"] is True
        finally:
            stop_server(data_dir, force=True)

    def test_server_restart_preserves_and_overrides_host_port(
        self, data_dir: Path, cli_runner: CLIRunner
    ) -> None:
        try:
            # Restart when stopped starts a new server
            r1 = cli_runner.run_json("server", "restart", "--port", "0")
            assert set(r1.keys()) == RESTART_JSON_KEYS
            assert r1["restarted"] is True
            assert r1["was_running"] is False
            assert r1["previous_pid"] is None
            assert r1["running"] is True
            assert r1["healthy"] is True
            first_pid = r1["pid"]
            first_port = r1["port"]

            # Create issue before restart to verify persistence across restart
            cli_runner.run_json("create", "--summary", "Persist across restart", "--description", "<p>Data</p>")

            # Restart without --port preserves previous port and replaces PID
            r2 = cli_runner.run_json("server", "restart")
            assert set(r2.keys()) == RESTART_JSON_KEYS
            assert r2["restarted"] is True
            assert r2["was_running"] is True
            assert r2["previous_pid"] == first_pid
            assert r2["pid"] != first_pid
            assert r2["port"] == first_port
            assert r2["host"] == "127.0.0.1"
            assert r2["healthy"] is True
            with pytest.raises(ProcessLookupError):
                os.kill(first_pid, 0)

            with httpx.Client(trust_env=False, timeout=2.0) as client:
                issues = client.get(f"{r2['url']}/api/issues").json()
                assert issues["total"] == 1
                assert issues["items"][0]["summary"] == "Persist across restart"

            # Restart with new --port and human output
            new_port = _reserve_free_port()
            r3_human = cli_runner.run_ok("server", "restart", "--port", str(new_port))
            assert "iTrack server restarted" in r3_human.stdout
            stat_r3 = cli_runner.run_json("server", "status")
            assert stat_r3["port"] == new_port
            assert stat_r3["pid"] != r2["pid"]
            assert stat_r3["healthy"] is True
        finally:
            stop_server(data_dir, force=True)

    def test_wildcard_ipv6_hosts_and_proxy_bypass(
        self, tmp_path: Path
    ) -> None:
        dir_v4 = tmp_path / "wildcard_v4"
        dir_v6_loop = tmp_path / "ipv6_loop"
        dir_v6_wild = tmp_path / "ipv6_wild"
        try:
            res = CLIRunner(dir_v4).run_json(
                "server",
                "start",
                "--host",
                "0.0.0.0",
                "--port",
                "0",
                env_overrides={
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9",
                },
            )
            assert res["host"] == "0.0.0.0"
            assert res["url"].startswith("http://127.0.0.1:")
            assert res["healthy"] is True

            res_v6 = CLIRunner(dir_v6_loop).run_json("server", "start", "--host", "::1", "--port", "0")
            assert res_v6["host"] == "::1"
            assert res_v6["url"] == f"http://[::1]:{res_v6['port']}"
            assert res_v6["healthy"] is True

            res_v6_wild = CLIRunner(dir_v6_wild).run_json("server", "start", "--host", "::", "--port", "0")
            assert res_v6_wild["host"] == "::"
            assert res_v6_wild["url"] == f"http://[::1]:{res_v6_wild['port']}"
            assert res_v6_wild["healthy"] is True
        finally:
            stop_server(dir_v4, force=True)
            stop_server(dir_v6_loop, force=True)
            stop_server(dir_v6_wild, force=True)

    def test_stale_pid_corrupt_files_zombie_and_unhealthy_live_pid(
        self, data_dir: Path, cli_runner: CLIRunner
    ) -> None:
        paths = get_server_paths(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 1. Dead PID cleanup via status (and cleanup_stale=False preserves files)
            paths.pid_file.write_text("99999999\n", encoding="utf-8")
            paths.meta_file.write_text('{"pid": 99999999, "host": "127.0.0.1", "port": 8000}\n', encoding="utf-8")
            no_clean = get_server_status(data_dir, cleanup_stale=False)
            assert no_clean["running"] is False
            assert no_clean["stale_cleaned"] is False
            assert paths.pid_file.exists()

            stat = cli_runner.run_json("server", "status")
            assert stat["running"] is False
            assert stat["stale_cleaned"] is True
            assert not paths.pid_file.exists()
            assert not paths.meta_file.exists()

            # 2. Negative / zero / empty / corrupt PID + corrupt meta cleanup via stop and restart
            for bad_pid_text in ("-5\n", "0\n", "   \n", "not-a-pid\n"):
                paths.pid_file.write_text(bad_pid_text, encoding="utf-8")
                paths.meta_file.write_text("{broken-json", encoding="utf-8")
                stop_res = cli_runner.run_json("server", "stop")
                assert stop_res["status"] == "not_running"
                assert stop_res["stale_cleaned"] is True
                assert not paths.pid_file.exists()
                assert not paths.meta_file.exists()

            # Stale PID cleaned on restart
            paths.pid_file.write_text("99999998\n", encoding="utf-8")
            paths.meta_file.write_text('{"pid": 99999998, "host": "127.0.0.1", "port": 8000}\n', encoding="utf-8")
            restarted = cli_runner.run_json("server", "restart", "--port", "0")
            assert restarted["restarted"] is True
            assert restarted["was_running"] is False
            assert restarted["stale_cleaned"] is True
            assert restarted["previous_pid"] is None
            cli_runner.run_ok("server", "stop")

            # 3. Zombie process (unreaped exited child) treated as not running & cleaned on start
            zombie_proc = subprocess.Popen([PYTHON_BIN, "-c", "pass"])
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                stat_path = Path(f"/proc/{zombie_proc.pid}/stat")
                if stat_path.exists() and ") Z " in stat_path.read_text(encoding="utf-8", errors="replace"):
                    break
                time.sleep(0.02)
            try:
                paths.pid_file.write_text(f"{zombie_proc.pid}\n", encoding="utf-8")
                start_res = cli_runner.run_json("server", "start", "--port", "0")
                assert start_res["started"] is True
                assert start_res["stale_cleaned"] is True
                assert start_res["pid"] != zombie_proc.pid
                cli_runner.run_ok("server", "stop")
            finally:
                zombie_proc.wait(timeout=2)

            # 4. Live PID in server.json when server.pid is missing/corrupt + unhealthy status when non-server process
            sleeper = subprocess.Popen([PYTHON_BIN, "-c", "import time; time.sleep(60)"])
            try:
                paths.pid_file.unlink(missing_ok=True)
                unused_port = _reserve_free_port()
                paths.meta_file.write_text(
                    json.dumps({"pid": sleeper.pid, "host": "127.0.0.1", "port": unused_port}),
                    encoding="utf-8",
                )
                stat_unhealthy = cli_runner.run_json("server", "status")
                assert stat_unhealthy["running"] is True
                assert stat_unhealthy["healthy"] is False
                assert stat_unhealthy["pid"] == sleeper.pid
                assert "unhealthy" in cli_runner.run_ok("server", "status").stdout.lower()

                stopped_from_meta = cli_runner.run_json("server", "stop")
                assert stopped_from_meta["stopped"] is True
                assert stopped_from_meta["pid"] == sleeper.pid
                assert not paths.meta_file.exists()
                sleeper.wait(timeout=2)
            finally:
                _safe_kill_pid(sleeper.pid)
        finally:
            stop_server(data_dir, force=True)

    def test_sigkill_fallback_cli_force_pgroup_safety_and_permission_denied(
        self, data_dir: Path, cli_runner: CLIRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = get_server_paths(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        # Spawn helper in the SAME process group as pytest that ignores SIGTERM
        stubborn = subprocess.Popen(
            [
                PYTHON_BIN,
                "-c",
                (
                    "import signal, sys, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "sys.stdout.write('ready\\n'); sys.stdout.flush(); "
                    "time.sleep(60)"
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert stubborn.stdout is not None
            assert stubborn.stdout.readline().strip() == "ready"
            paths.pid_file.write_text(f"{stubborn.pid}\n", encoding="utf-8")
            res = stop_server(data_dir, timeout=0.3, poll_interval=0.05)
            assert res["stopped"] is True
            assert res["forced"] is True
            assert res["signal"] == "SIGKILL"
            assert res["pid"] == stubborn.pid
            assert not paths.pid_file.exists()
            stubborn.wait(timeout=2)
        finally:
            _safe_kill_pid(stubborn.pid)

        # Verify CLI `server stop --force` and `stop --force` send SIGKILL immediately
        for force_cmd in (("server", "stop", "--force"), ("stop", "--force")):
            sleeper = subprocess.Popen([PYTHON_BIN, "-c", "import time; time.sleep(60)"])
            try:
                paths.pid_file.write_text(f"{sleeper.pid}\n", encoding="utf-8")
                res_force = cli_runner.run_json(*force_cmd)
                assert res_force["stopped"] is True
                assert res_force["forced"] is True
                assert res_force["signal"] == "SIGKILL"
                assert res_force["pid"] == sleeper.pid
                sleeper.wait(timeout=2)
            finally:
                _safe_kill_pid(sleeper.pid)

        # PermissionError during stop raises ServerError(code="permission_denied") & CLI exit 1
        sleeper2 = subprocess.Popen([PYTHON_BIN, "-c", "import time; time.sleep(60)"])
        try:
            paths.pid_file.write_text(f"{sleeper2.pid}\n", encoding="utf-8")
            real_kill = os.kill

            def _deny_sigterm(pid: int, sig: int) -> None:
                if sig != 0 and pid == sleeper2.pid:
                    raise PermissionError("Operation not permitted")
                real_kill(pid, sig)

            monkeypatch.setattr(os, "kill", _deny_sigterm)
            with pytest.raises(ServerError) as exc_info:
                stop_server(data_dir)
            assert exc_info.value.code == "permission_denied"
            rc = run_cli(["--data-dir", str(data_dir), "server", "stop", "--json"])
            assert rc == 1
        finally:
            monkeypatch.undo()
            _safe_kill_pid(sleeper2.pid)
            sleeper2.wait(timeout=2)
            paths.pid_file.unlink(missing_ok=True)
            paths.meta_file.unlink(missing_ok=True)

    def test_validation_server_script_log_offset_mismatch_timeout_browser_and_foreground(
        self, tmp_path: Path, data_dir: Path, cli_runner: CLIRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invalid CLI invocations -> exit 1
        assert cli_runner.run("server").returncode == 1
        srv_err = cli_runner.run("server", "--json")
        assert srv_err.returncode == 1
        assert srv_err.json()["ok"] is False

        assert cli_runner.run("server", "status", "unexpected_arg").returncode == 1

        for bad_args in (
            ("server", "start", "--port", "-1", "--json"),
            ("server", "start", "--port", "70000", "--json"),
            ("start", "--port", "notanumber", "--json"),
            ("server", "start", "--timeout", "0", "--json"),
            ("server", "stop", "--timeout", "0", "--json"),
            ("stop", "--timeout", "-1", "--json"),
            ("ui", "--background", "--timeout", "0", "--json"),
            ("server", "restart", "--port", "70000", "--json"),
            ("server", "restart", "--stop-timeout", "0", "--json"),
            ("server", "start", "--host", "bad host", "--json"),
        ):
            bad_res = cli_runner.run(*bad_args)
            assert bad_res.returncode == 1, f"Expected exit 1 for {bad_args}"
            err_json = bad_res.json()
            assert err_json["ok"] is False
            assert err_json["error"] in ("validation_error", "usage_error")
            assert "Error:" in bad_res.stderr

        with pytest.raises(ValidationError):
            start_server(data_dir, host="")
        with pytest.raises(ValidationError):
            restart_server(data_dir, host="bad host")

        # Missing ITRACK_SERVER_SCRIPT -> exit 1, code server_script_missing
        missing_script = tmp_path / "missing_server_script.py"
        miss_res = cli_runner.run(
            "server",
            "start",
            "--port",
            "0",
            "--json",
            env_overrides={"ITRACK_SERVER_SCRIPT": str(missing_script)},
        )
        assert miss_res.returncode == 1
        assert miss_res.json()["error"] == "server_script_missing"

        # Custom ITRACK_SERVER_SCRIPT verifies env vars + stdin /dev/null
        custom_script = tmp_path / "env_probe_server.py"
        probe_out = tmp_path / "probe.json"
        custom_script.write_text(
            f"import argparse, json, os, pathlib, sys\n"
            f"from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            f"p = argparse.ArgumentParser()\n"
            f"p.add_argument('--host'); p.add_argument('--port', type=int); p.add_argument('--data-dir')\n"
            f"a = p.parse_args()\n"
            f"pathlib.Path({str(probe_out)!r}).write_text(json.dumps({{\n"
            f"  'py_no_bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE'),\n"
            f"  'py_unbuffered': os.environ.get('PYTHONUNBUFFERED'),\n"
            f"  'data_dir_env': os.environ.get('ITRACK_DATA_DIR'),\n"
            f"  'stdin_isatty': sys.stdin.isatty(),\n"
            f"}}), encoding='utf-8')\n"
            f"class H(BaseHTTPRequestHandler):\n"
            f"  def do_GET(self):\n"
            f"    body = json.dumps({{'status': 'ok', 'data_dir': a.data_dir, 'pid': os.getpid()}}).encode()\n"
            f"    self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)\n"
            f"  def log_message(self, *args): pass\n"
            f"HTTPServer((a.host, a.port), H).serve_forever()\n",
            encoding="utf-8",
        )
        probe_dir = tmp_path / "probe_store"
        try:
            probe_res = CLIRunner(probe_dir).run_json(
                "server",
                "start",
                "--port",
                "0",
                env_overrides={"ITRACK_SERVER_SCRIPT": str(custom_script)},
            )
            assert probe_res["healthy"] is True
            probe_data = json.loads(probe_out.read_text(encoding="utf-8"))
            assert probe_data["py_no_bytecode"] == "1"
            assert probe_data["py_unbuffered"] == "1"
            assert Path(probe_data["data_dir_env"]).resolve() == probe_dir.resolve()
            assert probe_data["stdin_isatty"] is False
        finally:
            stop_server(probe_dir, force=True)

        # Port already in use -> early exit with server_start_failed & log_offset diagnostics only
        paths = get_server_paths(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        paths.log_file.write_text("OLD_LOG_SENTINEL_SHOULD_NOT_APPEAR\n", encoding="utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = int(blocker.getsockname()[1])

            fail_res = cli_runner.run("server", "start", "--port", str(busy_port), "--timeout", "5", "--json")
            assert fail_res.returncode == 1
            fail_json = fail_res.json()
            assert fail_json["ok"] is False
            assert fail_json["error"] == "server_start_failed"
            assert "OLD_LOG_SENTINEL_SHOULD_NOT_APPEAR" not in fail_json["message"]
            assert not paths.pid_file.exists()
            assert not paths.meta_file.exists()

        # Mismatched /api/health (different server running on port while custom child sleeps) -> server_start_timeout & child killed
        other_dir = tmp_path / "other_healthy_store"
        dummy_script = tmp_path / "never_healthy_server.py"
        marker_pid_file = tmp_path / "dummy_child.pid"
        dummy_script.write_text(
            f"import os, pathlib, time\n"
            f"pathlib.Path({str(marker_pid_file)!r}).write_text(str(os.getpid()))\n"
            f"time.sleep(30)\n",
            encoding="utf-8",
        )
        timeout_dir = tmp_path / "timeout_store"
        try:
            other_info = start_server(other_dir, port=0)
            with pytest.raises(ServerError) as exc_info:
                start_server(
                    timeout_dir,
                    port=other_info["port"],
                    timeout=0.35,
                    server_script=dummy_script,
                    health_poll_interval=0.05,
                )
            assert exc_info.value.code == "server_start_timeout"
            assert not get_server_paths(timeout_dir).pid_file.exists()
            assert not get_server_paths(timeout_dir).meta_file.exists()
            if marker_pid_file.exists():
                orphan_pid = int(marker_pid_file.read_text(encoding="utf-8").strip())
                with pytest.raises(ProcessLookupError):
                    os.kill(orphan_pid, 0)
        finally:
            stop_server(other_dir, force=True)
            stop_server(timeout_dir, force=True)

        # --open-browser calls webbrowser.open after readiness and NOT on failure
        opened_urls: list[str] = []
        monkeypatch.setattr(webbrowser, "open", lambda u: opened_urls.append(u) or True)
        browser_dir = tmp_path / "open_browser_store"
        try:
            assert run_cli(["--data-dir", str(browser_dir), "server", "start", "--port", "0", "--open-browser", "--json"]) == 0
            st = get_server_status(browser_dir)
            assert opened_urls == [f"{st['url']}/"]

            assert run_cli(["--data-dir", str(browser_dir), "server", "restart", "--open-browser", "--json"]) == 0
            st2 = get_server_status(browser_dir)
            assert opened_urls[-1] == f"{st2['url']}/"

            stop_server(browser_dir, force=True)
            assert run_cli(["--data-dir", str(browser_dir), "ui", "--background", "--port", "0", "--open-browser", "--json"]) == 0
            st3 = get_server_status(browser_dir)
            assert opened_urls[-1] == f"{st3['url']}/"

            # Conflict failure must NOT call webbrowser.open
            before_count = len(opened_urls)
            assert run_cli(["--data-dir", str(browser_dir), "server", "start", "--port", "0", "--open-browser", "--json"]) == 1
            assert len(opened_urls) == before_count
        finally:
            stop_server(browser_dir, force=True)

        # --foreground and default `ui` invoke uvicorn.run in-process
        fg_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            uvicorn,
            "run",
            lambda app, host, port, log_level="info": fg_calls.append({"host": host, "port": port, "log_level": log_level}),
        )
        fg_dir = tmp_path / "fg_store"
        assert run_cli(["--data-dir", str(fg_dir), "server", "start", "--foreground", "--host", "127.0.0.1", "--port", "9101"]) == 0
        assert run_cli(["--data-dir", str(fg_dir), "start", "--foreground", "--host", "127.0.0.1", "--port", "9102"]) == 0
        assert run_cli(["--data-dir", str(fg_dir), "ui", "--host", "127.0.0.1", "--port", "9103"]) == 0
        assert [(c["host"], c["port"]) for c in fg_calls] == [
            ("127.0.0.1", 9101),
            ("127.0.0.1", 9102),
            ("127.0.0.1", 9103),
        ]
