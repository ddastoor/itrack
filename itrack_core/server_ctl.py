"""Detached background server lifecycle management for iTrack."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from itrack_core.models import ConflictError, ServerError, ValidationError, utc_now_iso
from itrack_core.storage import DEFAULT_REPO_ROOT, resolve_data_dir

SERVER_PID_FILENAME: str = "server.pid"
SERVER_META_FILENAME: str = "server.json"
SERVER_LOG_FILENAME: str = "server.log"
SERVER_LOCK_FILENAME: str = "server.lock"
DEFAULT_SERVER_HOST: str = "127.0.0.1"
DEFAULT_SERVER_PORT: int = 8000
DEFAULT_START_TIMEOUT: float = 15.0
DEFAULT_STOP_TIMEOUT: float = 5.0


@dataclass(frozen=True)
class ServerPaths:
    """Resolved file locations for a data-directory-scoped background server."""

    data_dir: Path
    pid_file: Path
    meta_file: Path
    log_file: Path
    lock_file: Path


def get_server_paths(data_dir: str | Path | None = None) -> ServerPaths:
    """Return resolved server state/log file paths without creating directories."""
    resolved = resolve_data_dir(data_dir)
    return ServerPaths(
        data_dir=resolved,
        pid_file=resolved / SERVER_PID_FILENAME,
        meta_file=resolved / SERVER_META_FILENAME,
        log_file=resolved / SERVER_LOG_FILENAME,
        lock_file=resolved / SERVER_LOCK_FILENAME,
    )


def _validate_host(host: str) -> str:
    if not isinstance(host, str):
        raise ValidationError("Host must be a non-empty string.")
    cleaned = host.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValidationError(f"Invalid host '{host}': must be a non-empty hostname or IP without whitespace.")
    return cleaned


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise ValidationError(f"Invalid port '{port}': must be an integer between 0 and 65535.")
    return port


def _validate_timeout(timeout: float, field_name: str = "timeout") -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValidationError(f"Invalid {field_name} '{timeout}': must be a positive number.")
    val = float(timeout)
    if not math.isfinite(val) or val <= 0:
        raise ValidationError(f"Invalid {field_name} '{timeout}': must be greater than 0.")
    return val


def _resolve_connect_host(host: str) -> str:
    cleaned = host.strip()
    if cleaned in ("0.0.0.0", ""):
        return "127.0.0.1"
    if cleaned in ("::", "[::]"):
        return "::1"
    if cleaned.startswith("[") and cleaned.endswith("]") and len(cleaned) > 2:
        return cleaned[1:-1]
    return cleaned


def _format_url(host: str, port: int) -> str:
    connect_host = _resolve_connect_host(host)
    if ":" in connect_host:
        return f"http://[{connect_host}]:{port}"
    return f"http://{connect_host}:{port}"


def _allocate_free_port(host: str) -> int:
    bind_host = host.strip()
    if bind_host.startswith("[") and bind_host.endswith("]") and len(bind_host) > 2:
        bind_host = bind_host[1:-1]
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((bind_host, 0))
            return int(sock.getsockname()[1])
    except OSError as exc:
        raise ServerError(
            f"Failed to allocate ephemeral port on host '{host}': {exc}",
            code="server_start_failed",
        ) from exc


def _is_pid_alive(pid: int) -> bool:
    """Return True if pid is a live non-zombie process."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False

    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            text = proc_stat.read_text(encoding="utf-8", errors="replace")
            rparen = text.rfind(")")
            if rparen != -1 and len(text) > rparen + 2:
                state_char = text[rparen + 2]
                if state_char in ("Z", "X", "x"):
                    return False
        except (FileNotFoundError, ProcessLookupError):
            return False
        except OSError:
            pass
    return True


def _read_pid_file(pid_file: Path) -> tuple[int | None, bool]:
    if not pid_file.exists():
        return None, False
    try:
        raw = pid_file.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return None, True
        pid = int(raw)
        if pid <= 0:
            return None, True
        return pid, False
    except (OSError, ValueError):
        return None, True


def _read_meta_file(meta_file: Path) -> tuple[dict[str, Any] | None, bool]:
    if not meta_file.exists():
        return None, False
    try:
        raw = meta_file.read_text(encoding="utf-8", errors="replace")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None, True
        return parsed, False
    except (OSError, json.JSONDecodeError, ValueError):
        return None, True


def _cleanup_state_files(paths: ServerPaths) -> bool:
    removed = False
    for path in (paths.pid_file, paths.meta_file):
        if path.exists():
            try:
                path.unlink(missing_ok=True)
                removed = True
            except OSError:
                pass
    return removed


def _inspect_state(paths: ServerPaths, *, cleanup_stale: bool = True) -> dict[str, Any]:
    """Inspect server.pid and server.json without creating directories."""
    if not paths.data_dir.exists():
        return {
            "alive": False,
            "pid": None,
            "meta": None,
            "stale_cleaned": False,
        }

    pid_from_file, pid_corrupt = _read_pid_file(paths.pid_file)
    meta, meta_corrupt = _read_meta_file(paths.meta_file)

    candidate_pid: int | None = pid_from_file
    if candidate_pid is None and isinstance(meta, dict):
        raw_meta_pid = meta.get("pid")
        if isinstance(raw_meta_pid, int) and not isinstance(raw_meta_pid, bool) and raw_meta_pid > 0:
            candidate_pid = raw_meta_pid

    if candidate_pid is not None and _is_pid_alive(candidate_pid):
        return {
            "alive": True,
            "pid": candidate_pid,
            "meta": meta,
            "stale_cleaned": False,
        }

    has_artifacts = paths.pid_file.exists() or paths.meta_file.exists() or pid_corrupt or meta_corrupt
    stale_cleaned = _cleanup_state_files(paths) if (cleanup_stale and has_artifacts) else False
    return {
        "alive": False,
        "pid": None,
        "meta": meta,
        "stale_cleaned": stale_cleaned,
    }


@contextlib.contextmanager
def _server_lock(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _check_health(
    url: str,
    *,
    timeout: float,
    expected_data_dir: Path | None = None,
    expected_pid: int | None = None,
) -> bool:
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.get(f"{url}/api/health")
        if resp.status_code != 200:
            return False
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False
        if expected_data_dir is not None and "data_dir" in payload:
            if Path(str(payload["data_dir"])).resolve() != expected_data_dir.resolve():
                return False
        if expected_pid is not None and "pid" in payload:
            if payload["pid"] != expected_pid:
                return False
        return True
    except Exception:
        return False


def _read_log_tail(log_file: Path, offset: int = 0, max_chars: int = 2000) -> str:
    if not log_file.is_file():
        return ""
    try:
        with log_file.open("rb") as fh:
            if offset > 0:
                fh.seek(offset)
            data = fh.read()
        text = data.decode("utf-8", errors="replace").strip()
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text
    except OSError:
        return ""


def _resolve_server_script(server_script: str | Path | None) -> Path:
    if server_script is not None:
        candidate = Path(server_script).expanduser().resolve()
    else:
        env_script = os.environ.get("ITRACK_SERVER_SCRIPT", "").strip()
        candidate = Path(env_script).expanduser().resolve() if env_script else (DEFAULT_REPO_ROOT / "itrack-server.py").resolve()
    if not candidate.is_file():
        raise ServerError(
            f"iTrack server script not found: {candidate}",
            code="server_script_missing",
        )
    return candidate


def _terminate_child_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _build_stopped_status_result(paths: ServerPaths, *, stale_cleaned: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "stopped",
        "running": False,
        "healthy": False,
        "stale_cleaned": stale_cleaned,
        "pid": None,
        "host": None,
        "port": None,
        "url": None,
        "data_dir": str(paths.data_dir),
        "pid_file": str(paths.pid_file),
        "meta_file": str(paths.meta_file),
        "log_file": str(paths.log_file),
        "started_at": None,
    }


def _build_not_running_stop_result(paths: ServerPaths, *, stale_cleaned: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "not_running",
        "running": False,
        "stopped": False,
        "was_running": False,
        "forced": False,
        "signal": None,
        "stale_cleaned": stale_cleaned,
        "pid": None,
        "host": None,
        "port": None,
        "url": None,
        "data_dir": str(paths.data_dir),
        "pid_file": str(paths.pid_file),
        "meta_file": str(paths.meta_file),
        "log_file": str(paths.log_file),
    }


def get_server_status(
    data_dir: str | Path | None = None,
    *,
    health_timeout: float = 0.5,
    cleanup_stale: bool = True,
) -> dict[str, Any]:
    """Inspect background server status for the resolved data directory."""
    _validate_timeout(health_timeout, "health_timeout")
    paths = get_server_paths(data_dir)
    state = _inspect_state(paths, cleanup_stale=cleanup_stale)
    if not state["alive"]:
        return _build_stopped_status_result(paths, stale_cleaned=state["stale_cleaned"])

    pid = int(state["pid"])
    meta = state["meta"] if isinstance(state["meta"], dict) else {}
    host = meta.get("host")
    port = meta.get("port")
    url = meta.get("url")
    if url is None and isinstance(host, str) and isinstance(port, int) and not isinstance(port, bool) and port > 0:
        url = _format_url(host, port)
    started_at = meta.get("started_at")

    healthy = False
    if isinstance(url, str) and url:
        healthy = _check_health(
            url,
            timeout=health_timeout,
            expected_data_dir=paths.data_dir,
            expected_pid=pid,
        )

    return {
        "ok": True,
        "status": "running",
        "running": True,
        "healthy": healthy,
        "stale_cleaned": False,
        "pid": pid,
        "host": host,
        "port": port,
        "url": url,
        "data_dir": str(paths.data_dir),
        "pid_file": str(paths.pid_file),
        "meta_file": str(paths.meta_file),
        "log_file": str(paths.log_file),
        "started_at": started_at,
    }


def _start_server_unlocked(
    paths: ServerPaths,
    *,
    host: str = DEFAULT_SERVER_HOST,
    port: int = DEFAULT_SERVER_PORT,
    timeout: float = DEFAULT_START_TIMEOUT,
    server_script: str | Path | None = None,
    python_executable: str | None = None,
    health_poll_interval: float = 0.05,
) -> dict[str, Any]:
    eff_host = _validate_host(host)
    eff_port = _validate_port(port)
    eff_timeout = _validate_timeout(timeout, "timeout")
    eff_poll = _validate_timeout(health_poll_interval, "health_poll_interval")
    script_path = _resolve_server_script(server_script)
    py_exe = python_executable or sys.executable

    state = _inspect_state(paths, cleanup_stale=True)
    stale_cleaned = bool(state["stale_cleaned"])
    if state["alive"]:
        raise ConflictError(
            f"iTrack server is already running (pid={state['pid']}, data_dir={paths.data_dir}).",
            code="server_already_running",
        )

    paths.data_dir.mkdir(parents=True, exist_ok=True)
    bound_port = _allocate_free_port(eff_host) if eff_port == 0 else eff_port
    url = _format_url(eff_host, bound_port)
    log_offset = paths.log_file.stat().st_size if paths.log_file.exists() else 0

    cmd = [
        py_exe,
        "-B",
        str(script_path),
        "--host",
        eff_host,
        "--port",
        str(bound_port),
        "--data-dir",
        str(paths.data_dir),
    ]
    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env["ITRACK_DATA_DIR"] = str(paths.data_dir)

    log_fh = paths.log_file.open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(DEFAULT_REPO_ROOT),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fh.close()

    deadline = time.monotonic() + eff_timeout
    while True:
        rc = proc.poll()
        if rc is not None:
            log_tail = _read_log_tail(paths.log_file, offset=log_offset)
            detail = f"\nServer log:\n{log_tail}" if log_tail else ""
            raise ServerError(
                f"Background server exited prematurely with code {rc}.{detail}",
                code="server_start_failed",
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        if _check_health(
            url,
            timeout=min(0.5, max(0.05, remaining)),
            expected_data_dir=paths.data_dir,
            expected_pid=proc.pid,
        ):
            started_at = utc_now_iso()
            meta = {
                "pid": proc.pid,
                "host": eff_host,
                "port": bound_port,
                "url": url,
                "data_dir": str(paths.data_dir),
                "pid_file": str(paths.pid_file),
                "meta_file": str(paths.meta_file),
                "log_file": str(paths.log_file),
                "started_at": started_at,
                "command": cmd,
            }
            _atomic_write_text(paths.pid_file, f"{proc.pid}\n")
            _atomic_write_text(paths.meta_file, json.dumps(meta, indent=2) + "\n")
            return {
                "ok": True,
                "status": "running",
                "started": True,
                "running": True,
                "healthy": True,
                "stale_cleaned": stale_cleaned,
                "pid": proc.pid,
                "host": eff_host,
                "port": bound_port,
                "url": url,
                "data_dir": str(paths.data_dir),
                "pid_file": str(paths.pid_file),
                "meta_file": str(paths.meta_file),
                "log_file": str(paths.log_file),
                "started_at": started_at,
            }

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(eff_poll, remaining))

    _terminate_child_process(proc)
    log_tail = _read_log_tail(paths.log_file, offset=log_offset)
    detail = f"\nServer log:\n{log_tail}" if log_tail else ""
    raise ServerError(
        f"Timed out after {eff_timeout:.1f}s waiting for server health at {url}/api/health.{detail}",
        code="server_start_timeout",
    )


def start_server(
    data_dir: str | Path | None = None,
    *,
    host: str = DEFAULT_SERVER_HOST,
    port: int = DEFAULT_SERVER_PORT,
    timeout: float = DEFAULT_START_TIMEOUT,
    server_script: str | Path | None = None,
    python_executable: str | None = None,
    health_poll_interval: float = 0.05,
) -> dict[str, Any]:
    """Start `itrack-server.py` as a detached background daemon for `data_dir`."""
    _validate_host(host)
    _validate_port(port)
    _validate_timeout(timeout, "timeout")
    _validate_timeout(health_poll_interval, "health_poll_interval")
    _resolve_server_script(server_script)
    paths = get_server_paths(data_dir)
    with _server_lock(paths.lock_file):
        return _start_server_unlocked(
            paths,
            host=host,
            port=port,
            timeout=timeout,
            server_script=server_script,
            python_executable=python_executable,
            health_poll_interval=health_poll_interval,
        )


def _stop_server_unlocked(
    paths: ServerPaths,
    *,
    timeout: float = DEFAULT_STOP_TIMEOUT,
    force: bool = False,
    poll_interval: float = 0.05,
) -> dict[str, Any]:
    eff_timeout = _validate_timeout(timeout, "timeout")
    eff_poll = _validate_timeout(poll_interval, "poll_interval")

    state = _inspect_state(paths, cleanup_stale=True)
    stale_cleaned = bool(state["stale_cleaned"])
    if not state["alive"]:
        return _build_not_running_stop_result(paths, stale_cleaned=stale_cleaned)

    pid = int(state["pid"])
    meta = state["meta"] if isinstance(state["meta"], dict) else {}
    host = meta.get("host")
    port = meta.get("port")
    url = meta.get("url")
    if url is None and isinstance(host, str) and isinstance(port, int) and not isinstance(port, bool) and port > 0:
        url = _format_url(host, port)

    sig = signal.SIGKILL if force else signal.SIGTERM
    sig_name = "SIGKILL" if force else "SIGTERM"
    forced = bool(force)

    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        _cleanup_state_files(paths)
        return {
            "ok": True,
            "status": "stopped",
            "running": False,
            "stopped": True,
            "was_running": True,
            "forced": forced,
            "signal": sig_name,
            "stale_cleaned": stale_cleaned,
            "pid": pid,
            "host": host,
            "port": port,
            "url": url,
            "data_dir": str(paths.data_dir),
            "pid_file": str(paths.pid_file),
            "meta_file": str(paths.meta_file),
            "log_file": str(paths.log_file),
        }
    except PermissionError as exc:
        raise ServerError(
            f"Permission denied sending {sig_name} to server pid={pid}: {exc}",
            code="permission_denied",
        ) from exc

    deadline = time.monotonic() + eff_timeout
    while _is_pid_alive(pid) and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(eff_poll, remaining))

    if _is_pid_alive(pid) and not force:
        forced = True
        sig_name = "SIGKILL"
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise ServerError(
                f"Permission denied sending SIGKILL to server pid={pid}: {exc}",
                code="permission_denied",
            ) from exc

        kill_deadline = time.monotonic() + 2.0
        while _is_pid_alive(pid) and time.monotonic() < kill_deadline:
            remaining = kill_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(eff_poll, remaining))

    if _is_pid_alive(pid):
        raise ServerError(
            f"Failed to stop background server process (pid={pid}).",
            code="server_stop_failed",
        )

    _cleanup_state_files(paths)
    return {
        "ok": True,
        "status": "stopped",
        "running": False,
        "stopped": True,
        "was_running": True,
        "forced": forced,
        "signal": sig_name,
        "stale_cleaned": stale_cleaned,
        "pid": pid,
        "host": host,
        "port": port,
        "url": url,
        "data_dir": str(paths.data_dir),
        "pid_file": str(paths.pid_file),
        "meta_file": str(paths.meta_file),
        "log_file": str(paths.log_file),
    }


def stop_server(
    data_dir: str | Path | None = None,
    *,
    timeout: float = DEFAULT_STOP_TIMEOUT,
    force: bool = False,
    poll_interval: float = 0.05,
) -> dict[str, Any]:
    """Stop the background `itrack-server.py` process for `data_dir` if running."""
    _validate_timeout(timeout, "timeout")
    _validate_timeout(poll_interval, "poll_interval")
    paths = get_server_paths(data_dir)
    if not paths.data_dir.exists():
        return _build_not_running_stop_result(paths, stale_cleaned=False)
    with _server_lock(paths.lock_file):
        return _stop_server_unlocked(
            paths,
            timeout=timeout,
            force=force,
            poll_interval=poll_interval,
        )


def restart_server(
    data_dir: str | Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    timeout: float = DEFAULT_START_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    server_script: str | Path | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Restart (or start if stopped) the background `itrack-server.py` for `data_dir`."""
    if host is not None:
        _validate_host(host)
    if port is not None:
        _validate_port(port)
    _validate_timeout(timeout, "timeout")
    _validate_timeout(stop_timeout, "stop_timeout")
    _resolve_server_script(server_script)

    paths = get_server_paths(data_dir)
    with _server_lock(paths.lock_file):
        prev_meta, _ = _read_meta_file(paths.meta_file)
        prev_host = prev_meta.get("host") if isinstance(prev_meta, dict) else None
        prev_port = prev_meta.get("port") if isinstance(prev_meta, dict) else None

        eff_host = host if host is not None else (prev_host if isinstance(prev_host, str) and prev_host.strip() else DEFAULT_SERVER_HOST)
        eff_port = (
            port
            if port is not None
            else (prev_port if isinstance(prev_port, int) and not isinstance(prev_port, bool) and prev_port > 0 else DEFAULT_SERVER_PORT)
        )

        stop_res = _stop_server_unlocked(paths, timeout=stop_timeout)
        start_res = _start_server_unlocked(
            paths,
            host=eff_host,
            port=eff_port,
            timeout=timeout,
            server_script=server_script,
            python_executable=python_executable,
        )
        return {
            **start_res,
            "stale_cleaned": bool(stop_res.get("stale_cleaned") or start_res.get("stale_cleaned")),
            "restarted": True,
            "was_running": bool(stop_res["was_running"]),
            "previous_pid": stop_res["pid"],
        }
