#!/usr/bin/env python3
"""Master test runner, parallel subprocess/daemon coverage combiner, 100% surface gate, and Clean-Root auditor."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import (  # noqa: E402
    _terminate_pid_gracefully,
    assert_clean_repo_root,
    compute_surface_audit,
    find_headless_browsers,
    find_itrack_daemons,
    reap_all_test_daemons,
)

MIN_LINE_COVERAGE_PCT = 95.0
STAGE1_MAX_WORKERS = 4
SHARD_FLUSH_WAIT_SEC = 0.5


def _compress_line_ranges(lines: list[int], max_ranges: int = 18) -> str:
    if not lines:
        return "-"
    sorted_lines = sorted(set(lines))
    ranges: list[str] = []
    start = prev = sorted_lines[0]
    for ln in sorted_lines[1:]:
        if ln == prev + 1:
            prev = ln
        else:
            ranges.append(f"L{start}" if start == prev else f"L{start}-{prev}")
            start = prev = ln
    ranges.append(f"L{start}" if start == prev else f"L{start}-{prev}")
    if len(ranges) > max_ranges:
        return ", ".join(ranges[:max_ranges]) + f" ... (+{len(ranges) - max_ranges} more)"
    return ", ".join(ranges)


def _resolve_suite_files(suite_patterns: list[str]) -> list[Path]:
    all_suites = sorted(TESTS_DIR.glob("test_*.py"))
    if not suite_patterns:
        return all_suites

    tokens: list[str] = []
    for raw in suite_patterns:
        for part in raw.split(","):
            p = part.strip()
            if p:
                tokens.append(p)

    matched: list[Path] = []
    for candidate in all_suites:
        stem = candidate.stem.lower()
        name = candidate.name.lower()
        for tok in tokens:
            tl = tok.lower()
            if tl.isdigit():
                padded = f"test_{int(tl):02d}_"
                if stem.startswith(padded) or stem == f"test_{int(tl):02d}":
                    matched.append(candidate)
                    break
            elif tl in name or tl in stem:
                matched.append(candidate)
                break

    # Deduplicate preserving order
    seen: set[Path] = set()
    ordered: list[Path] = []
    for m in matched:
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def _prepare_coverage_dir(cov_dir: Path, wipe: bool) -> tuple[Path, Path, Path, Path]:
    if wipe and cov_dir.exists():
        shutil.rmtree(cov_dir, ignore_errors=True)
    cov_dir.mkdir(parents=True, exist_ok=True)

    cov_file = cov_dir / ".coverage"
    coveragerc = cov_dir / ".coveragerc"
    sitecustomize = cov_dir / "sitecustomize.py"
    surface_log = cov_dir / "surface_events.jsonl"
    core_line = "core = sysmon\n" if sys.version_info >= (3, 12) else ""

    coveragerc.write_text(
        "[run]\n"
        "parallel = true\n"
        f"{core_line}"
        f"data_file = {cov_file}\n"
        "include =\n"
        f"    {REPO_ROOT / 'itrack.py'}\n"
        f"    {REPO_ROOT / 'itrack-server.py'}\n"
        "omit =\n"
        "    */tests/*\n"
        "disable_warnings =\n"
        "    include-ignored\n"
        "    no-data-collected\n"
        "    module-not-measured\n"
        "\n"
        "[report]\n"
        "include =\n"
        f"    {REPO_ROOT / 'itrack.py'}\n"
        f"    {REPO_ROOT / 'itrack-server.py'}\n",
        encoding="utf-8",
    )

    sitecustomize.write_text(
        "try:\n"
        "    import coverage\n"
        "    coverage.process_startup()\n"
        "except Exception:\n"
        "    pass\n",
        encoding="utf-8",
    )
    return cov_file, coveragerc, sitecustomize, surface_log


def _collect_python_coverage(
    cov_dir: Path,
    cov_file: Path,
    coveragerc: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    cov_json_path = cov_dir / "cov.json"
    shards = list(cov_dir.glob(".coverage*"))
    shard_files = [p for p in shards if p.name != ".coveragerc"]

    child_env = env.copy()
    # Prevent coverage commands themselves from spawning nested process_startup shards
    child_env.pop("COVERAGE_PROCESS_START", None)

    if shard_files:
        subprocess.run(
            [sys.executable, "-B", "-m", "coverage", "combine", f"--rcfile={coveragerc}"],
            cwd=str(cov_dir),
            env=child_env,
            capture_output=True,
            text=True,
        )

    if cov_file.exists():
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "coverage",
                "json",
                f"--rcfile={coveragerc}",
                "-o",
                str(cov_json_path),
            ],
            cwd=str(cov_dir),
            env=child_env,
            capture_output=True,
            text=True,
        )

    result: dict[str, Any] = {
        "files": {
            "itrack.py": {"covered": 0, "statements": 0, "pct": 0.0, "missing_lines": []},
            "itrack-server.py": {"covered": 0, "statements": 0, "pct": 0.0, "missing_lines": []},
        },
        "combined": {"covered": 0, "statements": 0, "pct": 0.0},
        "cov_json_path": str(cov_json_path),
    }

    if not cov_json_path.exists():
        return result

    raw = json.loads(cov_json_path.read_text(encoding="utf-8"))
    files_map = raw.get("files") or {}
    total_cov = 0
    total_stmts = 0

    for fkey, fdata in files_map.items():
        bname = Path(fkey).name
        if bname not in ("itrack.py", "itrack-server.py"):
            continue
        summary = fdata.get("summary") or {}
        covered = int(summary.get("covered_lines") or 0)
        stmts = int(summary.get("num_statements") or 0)
        pct = float(summary.get("percent_covered") or 0.0)
        missing = list(fdata.get("missing_lines") or [])
        result["files"][bname] = {
            "covered": covered,
            "statements": stmts,
            "pct": pct,
            "missing_lines": missing,
        }
        total_cov += covered
        total_stmts += stmts

    result["combined"] = {
        "covered": total_cov,
        "statements": total_stmts,
        "pct": (100.0 * total_cov / total_stmts) if total_stmts > 0 else 0.0,
    }
    return result


def _print_summary_report(
    py_cov: dict[str, Any],
    surface: dict[str, Any],
    enforce_gate: bool,
) -> list[str]:
    failures: list[str] = []

    print("\n" + "=" * 92)
    print(" iTrack Master Verification Summary (Python Line Coverage + 100% CLI / API / UI Surface)")
    print("=" * 92)
    print(f"{'Dimension':<38} | {'Covered / Total':<19} | {'Percent':>9} | {'Status':<10}")
    print("-" * 92)

    # 1. Python per-file & combined coverage
    for fname in ("itrack.py", "itrack-server.py"):
        finfo = py_cov["files"][fname]
        pct = finfo["pct"]
        ok = (pct >= MIN_LINE_COVERAGE_PCT and finfo["statements"] > 0) or (not enforce_gate)
        status = "PASS" if (pct >= MIN_LINE_COVERAGE_PCT and finfo["statements"] > 0) else ("SKIP-GATE" if not enforce_gate else "FAIL")
        ratio = f"{finfo['covered']} / {finfo['statements']}"
        print(f"{'Python: ' + fname:<38} | {ratio:<19} | {pct:>8.2f}% | {status:<10}")
        if not ok:
            failures.append(
                f"Python line coverage for {fname} is {pct:.2f}% (< {MIN_LINE_COVERAGE_PCT:.1f}%). "
                f"Missing: {_compress_line_ranges(finfo['missing_lines'])}"
            )

    comb = py_cov["combined"]
    comb_pct = comb["pct"]
    comb_pass = comb_pct >= MIN_LINE_COVERAGE_PCT and comb["statements"] > 0
    comb_status = "PASS" if comb_pass else ("SKIP-GATE" if not enforce_gate else "FAIL")
    comb_ratio = f"{comb['covered']} / {comb['statements']}"
    print(
        f"{'Python: Combined Total':<38} | "
        f"{comb_ratio:<19} | "
        f"{comb_pct:>8.2f}% | {comb_status:<10}"
    )
    if enforce_gate and not comb_pass:
        failures.append(
            f"Combined Python line coverage is {comb_pct:.2f}% (< {MIN_LINE_COVERAGE_PCT:.1f}%)."
        )

    print("-" * 92)

    # 2. Surface 100% categories
    surface_rows = [
        ("API Routes (/api/v1/*)", "api_routes"),
        ("CLI Subcommands", "cli_commands"),
        ("CLI Option Flags", "cli_flags"),
        ("Web UI Views", "ui_views"),
        ("Web UI Modals", "ui_modals"),
    ]
    for label, key in surface_rows:
        bucket = surface[key]
        hit_n = len(bucket["hit"])
        exp_n = len(bucket["expected"])
        pct = bucket["pct"]
        row_pass = (exp_n > 0 and hit_n == exp_n)
        status = "PASS" if row_pass else ("SKIP-GATE" if not enforce_gate else "FAIL")
        print(f"{label:<38} | {f'{hit_n} / {exp_n}':<19} | {pct:>8.2f}% | {status:<10}")
        if enforce_gate and not row_pass:
            missing_preview = bucket["missing"][:15]
            failures.append(
                f"{label} coverage is {hit_n}/{exp_n} ({pct:.2f}% < 100.0%). Missing: {missing_preview}"
            )

    orphan_eps = surface["orphan_spa_endpoints"]
    parity_pass = len(orphan_eps) == 0
    parity_status = "PASS" if parity_pass else ("SKIP-GATE" if not enforce_gate else "FAIL")
    spa_eps_total = len(surface["spa_surface"]["endpoints"])
    print(
        f"{'UI -> Backend API Endpoint Parity':<38} | "
        f"{f'{spa_eps_total - len(orphan_eps)} / {spa_eps_total}':<19} | "
        f"{(100.0 if parity_pass else 0.0):>8.2f}% | {parity_status:<10}"
    )
    if enforce_gate and not parity_pass:
        failures.append(f"Orphan SPA endpoints with no /api/v1/* backend route: {orphan_eps}")

    print("=" * 92)

    # Helpful missing-item breakdown whenever gate fails (or if verbose diagnostics help)
    if enforce_gate and failures:
        print("\n[GATE DIAGNOSTICS]")
        for fname in ("itrack.py", "itrack-server.py"):
            finfo = py_cov["files"][fname]
            if finfo["pct"] < MIN_LINE_COVERAGE_PCT:
                print(f"  * {fname} uncovered lines: {_compress_line_ranges(finfo['missing_lines'], max_ranges=35)}")
        for label, key in surface_rows:
            miss = surface[key]["missing"]
            if miss:
                print(f"  * Missing {label} ({len(miss)}): {miss}")
        if orphan_eps:
            print(f"  * Orphan SPA endpoints: {orphan_eps}")

    return failures


@dataclass
class SuiteRunResult:
    suite_path: Path
    worker_name: str
    stage: str
    returncode: int
    elapsed_sec: float
    stdout: str
    stderr: str
    summary_line: str


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STAGE1_LAUNCH_PRIORITY: dict[str, int] = {
    "test_01": 0,
    "test_02": 1,
    "test_04": 2,
    "test_05": 3,
    "test_06": 4,
    "test_03": 5,
    "test_07": 6,
}


def _worker_name(suite_path: Path, all_suites: list[Path]) -> str:
    m = re.match(r"^(test_\d+)", suite_path.stem)
    if m:
        prefix = m.group(1)
        same_prefix = [
            s for s in all_suites if s.stem == prefix or s.stem.startswith(f"{prefix}_")
        ]
        if len(same_prefix) == 1:
            return prefix
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", suite_path.stem)


def _partition_all_stages(
    target_files: list[Path],
) -> tuple[list[Path], list[Path], list[Path]]:
    stage3 = [
        p for p in target_files if p.stem == "test_08" or p.stem.startswith("test_08_")
    ]
    stage2 = [
        p for p in target_files if p.stem == "test_09" or p.stem.startswith("test_09_")
    ]
    excluded = set(stage2) | set(stage3)
    stage1 = [p for p in target_files if p not in excluded]
    return stage1, stage2, stage3


def _stage1_launch_order(stage1_suites: list[Path]) -> list[Path]:
    def _sort_key(path: Path) -> tuple[int, int, str]:
        m = re.match(r"^(test_\d+)", path.stem)
        prefix = m.group(1) if m else ""
        prio = _STAGE1_LAUNCH_PRIORITY.get(prefix, 50)
        try:
            neg_size = -int(path.stat().st_size)
        except OSError:
            neg_size = 0
        return (prio, neg_size, path.name)

    return sorted(stage1_suites, key=_sort_key)


def _extract_pytest_summary(stdout: str, stderr: str, returncode: int) -> str:
    ignore_banners = {
        "test session starts",
        "short test summary info",
        "failures",
        "errors",
        "warnings summary",
    }
    for blob in (stdout, stderr):
        if not blob:
            continue
        lines = [_ANSI_ESCAPE_RE.sub("", ln).strip() for ln in blob.splitlines()]
        for ln in reversed(lines):
            if not ln:
                continue
            m = re.match(r"^=+\s*(.+?)\s*=+$", ln)
            if m:
                inner = m.group(1).strip()
                if inner.lower() not in ignore_banners:
                    return inner
        for ln in reversed(lines):
            low = ln.lower()
            if any(
                tok in low
                for tok in (" passed", " failed", " error", " skipped", "no tests ran")
            ):
                return ln.strip("=").strip()
    return f"exit code {returncode}"


def _run_single_suite(
    suite_path: Path,
    worker_name: str,
    stage_label: str,
    pytest_cache_root: Path,
    pytest_basetemp_root: Path,
    extra_pytest_args: list[str],
    env: dict[str, str],
) -> SuiteRunResult:
    worker_cache = pytest_cache_root / f"w_{worker_name}"
    worker_basetemp = pytest_basetemp_root / f"w_{worker_name}"
    worker_cache.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-o",
        f"cache_dir={worker_cache}",
        f"--basetemp={worker_basetemp}",
        "-v",
        *extra_pytest_args,
        str(suite_path),
    ]
    t_start = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - t_start
    summary = _extract_pytest_summary(proc.stdout, proc.stderr, proc.returncode)
    return SuiteRunResult(
        suite_path=suite_path,
        worker_name=worker_name,
        stage=stage_label,
        returncode=proc.returncode,
        elapsed_sec=elapsed,
        stdout=proc.stdout,
        stderr=proc.stderr,
        summary_line=summary,
    )


def _print_suite_result(res: SuiteRunResult, idx: int, total: int) -> None:
    status_tag = "PASS" if res.returncode == 0 else f"FAIL(rc={res.returncode})"
    print(
        f"  [{res.stage} {idx}/{total}] {status_tag:<10} "
        f"{res.suite_path.name:<56} ({res.elapsed_sec:6.2f}s) | {res.summary_line}",
        flush=True,
    )
    if res.returncode != 0:
        print("-" * 92, flush=True)
        print(
            f"[SUITE FAILURE TRACEBACK] {res.suite_path.name} (exit code {res.returncode})",
            flush=True,
        )
        print("-" * 92, flush=True)
        if res.stdout.strip():
            print(res.stdout.rstrip(), flush=True)
        if res.stderr.strip():
            print("[STDERR]", flush=True)
            print(res.stderr.rstrip(), flush=True)
        print("-" * 92, flush=True)


def _graceful_reap_and_flush_shards(
    reap_scope: str,
    cov_dir: Path,
    flush_wait_sec: float = SHARD_FLUSH_WAIT_SEC,
) -> list[int]:
    """Gracefully shut down stray test daemons (/api/v1/shutdown + SIGTERM) and wait for .coverage.* shards to flush."""
    reaped = set(reap_all_test_daemons(reap_scope))

    for d in find_itrack_daemons(reap_scope):
        pid = int(d["pid"])
        reaped.add(pid)
        port = int(d.get("port") or 0)
        if port > 0:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/shutdown",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=1.5):
                    pass
            except Exception:
                pass
        _terminate_pid_gracefully(pid, graceful_timeout=3.0)

    if flush_wait_sec > 0:
        time.sleep(flush_wait_sec)
        if cov_dir.exists():
            journal_deadline = time.monotonic() + 0.5
            while time.monotonic() < journal_deadline:
                active_journals = list(cov_dir.glob(".coverage*-journal")) + list(
                    cov_dir.glob(".coverage*-wal")
                )
                if not active_journals:
                    break
                time.sleep(0.05)

    return sorted(reaped)


def _run_staged_all_suites(
    target_files: list[Path],
    pytest_cache_dir: Path,
    pytest_basetemp: Path,
    extra_pytest_args: list[str],
    env: dict[str, str],
    reap_scope: str,
) -> tuple[int, list[SuiteRunResult]]:
    stage1_suites, stage2_suites, stage3_suites = _partition_all_stages(target_files)
    worker_map = {p: _worker_name(p, target_files) for p in target_files}
    exit_first = any(arg in ("-x", "--exitfirst") for arg in extra_pytest_args)

    all_results: list[SuiteRunResult] = []
    aborted_early = False

    # Stage 1: Parallel Suites 01..07 across ThreadPoolExecutor(max_workers=4)
    if stage1_suites:
        workers = min(STAGE1_MAX_WORKERS, len(stage1_suites))
        print(
            f"\n--- Stage 1/3: Parallel Suites ({len(stage1_suites)} file(s), max_workers={workers}) ---",
            flush=True,
        )
        t_s1 = time.monotonic()
        launch_queue = _stage1_launch_order(stage1_suites)
        completed_s1: dict[Path, SuiteRunResult] = {}
        next_print_idx = 0

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_suite = {
                pool.submit(
                    _run_single_suite,
                    suite_path,
                    worker_map[suite_path],
                    "Stage 1",
                    pytest_cache_dir,
                    pytest_basetemp,
                    extra_pytest_args,
                    env,
                ): suite_path
                for suite_path in launch_queue
            }
            for fut in concurrent.futures.as_completed(future_to_suite):
                suite_path = future_to_suite[fut]
                completed_s1[suite_path] = fut.result()
                while (
                    next_print_idx < len(stage1_suites)
                    and stage1_suites[next_print_idx] in completed_s1
                ):
                    ready_suite = stage1_suites[next_print_idx]
                    _print_suite_result(
                        completed_s1[ready_suite],
                        next_print_idx + 1,
                        len(stage1_suites),
                    )
                    next_print_idx += 1
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            reap_all_test_daemons(reap_scope)
            shutil.rmtree(pytest_basetemp, ignore_errors=True)
            raise
        finally:
            pool.shutdown(wait=True)

        stage1_ordered = [completed_s1[p] for p in stage1_suites if p in completed_s1]
        all_results.extend(stage1_ordered)
        print(
            f"--- Stage 1/3 finished in {time.monotonic() - t_s1:.2f}s ---",
            flush=True,
        )
        # Reap any stray Stage 1 daemons so Stage 2 concurrency benchmarks get dedicated CPU
        reap_all_test_daemons(reap_scope)
        if exit_first and any(r.returncode != 0 for r in stage1_ordered):
            aborted_early = True

    # Stage 2: Solo Suite 09 (15-process / 24-process concurrency stress & self-healing)
    if stage2_suites and not aborted_early:
        print(
            f"\n--- Stage 2/3: Solo Concurrency Stress Suite ({len(stage2_suites)} file(s)) ---",
            flush=True,
        )
        t_s2 = time.monotonic()
        for idx, suite_path in enumerate(stage2_suites, 1):
            res = _run_single_suite(
                suite_path,
                worker_map[suite_path],
                "Stage 2",
                pytest_cache_dir,
                pytest_basetemp,
                extra_pytest_args,
                env,
            )
            _print_suite_result(res, idx, len(stage2_suites))
            all_results.append(res)
            if exit_first and res.returncode != 0:
                aborted_early = True
                break
        print(
            f"--- Stage 2/3 finished in {time.monotonic() - t_s2:.2f}s ---",
            flush=True,
        )
        reap_all_test_daemons(reap_scope)

    # Stage 3: Solo Suite 08 last (100% CLI/API/UI surface & parity gate over aggregated ITRACK_SURFACE_LOG)
    if stage3_suites and not aborted_early:
        print(
            f"\n--- Stage 3/3: Bidirectional Parity & 100% Surface Gate ({len(stage3_suites)} file(s)) ---",
            flush=True,
        )
        t_s3 = time.monotonic()
        for idx, suite_path in enumerate(stage3_suites, 1):
            res = _run_single_suite(
                suite_path,
                worker_map[suite_path],
                "Stage 3",
                pytest_cache_dir,
                pytest_basetemp,
                extra_pytest_args,
                env,
            )
            _print_suite_result(res, idx, len(stage3_suites))
            all_results.append(res)
            if exit_first and res.returncode != 0:
                break
        print(
            f"--- Stage 3/3 finished in {time.monotonic() - t_s3:.2f}s ---",
            flush=True,
        )

    failed_results = [r for r in all_results if r.returncode != 0]
    overall_rc = failed_results[0].returncode if failed_results else 0
    return overall_rc, all_results


def parse_runner_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="tests/run_all.py",
        description="iTrack Master Test Runner, Coverage Combiner & Clean-Root Gate",
    )
    parser.add_argument(
        "--all",
        dest="run_all",
        action="store_true",
        help="Run all 9 test suites and enforce >=95%% Python coverage + 100%% surface gate (default)",
    )
    parser.add_argument(
        "--suite",
        dest="suites",
        action="append",
        default=[],
        help="Run specific suite(s) by number (01..09) or substring pattern",
    )
    parser.add_argument(
        "--serial",
        dest="serial",
        action="store_true",
        help="Force single-process serial pytest execution instead of 3-stage parallel --all mode",
    )
    parser.add_argument(
        "--no-coverage-gate",
        dest="no_coverage_gate",
        action="store_true",
        help="Skip 95%%/100%% coverage gate failure (ONLY allowed with --suite, ignored on --all)",
    )
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args, extra_pytest_args = parse_runner_args(raw_argv)

    run_all_mode = args.run_all or (len(args.suites) == 0)
    if run_all_mode and args.no_coverage_gate:
        print("[WARN] --no-coverage-gate is ignored when running --all; full gate is mandatory.")
    enforce_gate = True if run_all_mode else (not args.no_coverage_gate)

    # 1. Pre-flight Clean-Root Law check
    assert_clean_repo_root(REPO_ROOT)

    # Snapshot any pre-existing headless browsers on the host
    initial_browser_pids = {b["pid"] for b in find_headless_browsers()}

    pid = os.getpid()
    if run_all_mode:
        reap_all_test_daemons("/tmp")
        cov_dir = Path("/tmp/itrack_coverage")
        pytest_cache_dir = Path("/tmp/itrack_pytest_cache")
        pytest_basetemp = Path(f"/tmp/itrack_pytest_basetemp_{pid}")
        wipe_cov = True
    else:
        cov_dir = Path(f"/tmp/itrack_coverage_suite_{pid}")
        pytest_cache_dir = Path(f"/tmp/itrack_pytest_cache_{pid}")
        pytest_basetemp = Path(f"/tmp/itrack_pytest_basetemp_{pid}")
        wipe_cov = True

    cov_file, coveragerc, _sitecustomize, surface_log = _prepare_coverage_dir(cov_dir, wipe=wipe_cov)

    target_files = _resolve_suite_files([] if run_all_mode else args.suites)
    if not target_files:
        print(
            f"[ERROR] No test suite files matched in {TESTS_DIR} "
            f"(mode={'--all' if run_all_mode else args.suites}).",
            file=sys.stderr,
        )
        if not run_all_mode:
            shutil.rmtree(cov_dir, ignore_errors=True)
        assert_clean_repo_root(REPO_ROOT)
        return 1

    # Configure subprocess environment for parallel coverage + surface telemetry + zero bytecode
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COVERAGE_FILE"] = str(cov_file)
    env["COVERAGE_PROCESS_START"] = str(coveragerc)
    env["ITRACK_SURFACE_LOG"] = str(surface_log)

    existing_pp = env.get("PYTHONPATH", "")
    pp_parts = [str(cov_dir), str(TESTS_DIR)]
    if existing_pp:
        pp_parts.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(pp_parts)

    reap_scope = "/tmp" if run_all_mode else str(pytest_basetemp)
    use_staged_parallel = run_all_mode and (not args.serial)
    failed_suite_names: list[str] = []

    t0 = time.monotonic()
    if use_staged_parallel:
        shutil.rmtree(pytest_cache_dir, ignore_errors=True)
        pytest_cache_dir.mkdir(parents=True, exist_ok=True)
        pytest_basetemp.mkdir(parents=True, exist_ok=True)

        mode_banner = f"ALL SUITES (3-STAGE PARALLEL, max_workers={STAGE1_MAX_WORKERS}, FULL GATE)"
        print(
            f"=== Running iTrack Test Suite [{mode_banner}] ({len(target_files)} file(s)) ===",
            flush=True,
        )
        pytest_rc, suite_results = _run_staged_all_suites(
            target_files=target_files,
            pytest_cache_dir=pytest_cache_dir,
            pytest_basetemp=pytest_basetemp,
            extra_pytest_args=extra_pytest_args,
            env=env,
            reap_scope=reap_scope,
        )
        failed_suite_names = [
            f"{r.suite_path.name} (rc={r.returncode})"
            for r in suite_results
            if r.returncode != 0
        ]
        elapsed = time.monotonic() - t0
        passed_n = sum(1 for r in suite_results if r.returncode == 0)
        print(
            f"\n=== Staged Pytest finished in {elapsed:.2f}s "
            f"({passed_n}/{len(suite_results)} suite(s) passed, exit code {pytest_rc}) ===",
            flush=True,
        )
    else:
        pytest_cmd = [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            f"cache_dir={pytest_cache_dir}",
            f"--basetemp={pytest_basetemp}",
            "-v",
            *extra_pytest_args,
            *[str(p) for p in target_files],
        ]
        mode_banner = (
            "ALL SUITES (SERIAL, FULL GATE)" if run_all_mode else f"SUITE {args.suites}"
        )
        print(
            f"=== Running iTrack Test Suite [{mode_banner}] ({len(target_files)} file(s)) ===",
            flush=True,
        )
        pytest_proc = subprocess.run(pytest_cmd, cwd=str(REPO_ROOT), env=env)
        pytest_rc = pytest_proc.returncode
        elapsed = time.monotonic() - t0
        print(
            f"=== Pytest finished in {elapsed:.2f}s (exit code {pytest_rc}) ===",
            flush=True,
        )

    # 2. Gracefully reap any stray test daemons (/api/v1/shutdown + SIGTERM) and wait 0.5s
    #    BEFORE combining coverage so all .coverage.* shards flush cleanly!
    flush_wait = SHARD_FLUSH_WAIT_SEC if run_all_mode else 0.15
    reaped_pids = _graceful_reap_and_flush_shards(
        reap_scope=reap_scope,
        cov_dir=cov_dir,
        flush_wait_sec=flush_wait,
    )
    if reaped_pids:
        print(f"[INFO] Reaped {len(reaped_pids)} stray test daemon(s): {reaped_pids}")

    # 3. Combine parallel coverage shards & compute surface audit
    py_cov = _collect_python_coverage(cov_dir, cov_file, coveragerc, env)
    surface = compute_surface_audit(log_path=surface_log)
    gate_failures = _print_summary_report(py_cov, surface, enforce_gate=enforce_gate)

    # 4. Clean up temporary pytest directories (preserving /tmp/itrack_coverage on --all for inspection)
    shutil.rmtree(pytest_basetemp, ignore_errors=True)
    if not run_all_mode:
        shutil.rmtree(cov_dir, ignore_errors=True)
        shutil.rmtree(pytest_cache_dir, ignore_errors=True)

    # 5. Final Clean-Root Law & Zero-Orphan Process Enforcement
    assert_clean_repo_root(REPO_ROOT)

    orphan_daemons = find_itrack_daemons("/tmp" if run_all_mode else str(pytest_basetemp))
    orphan_repo_daemons = find_itrack_daemons(REPO_ROOT)
    leaked_browsers = [
        b for b in find_headless_browsers() if b["pid"] not in initial_browser_pids
    ]
    for b in leaked_browsers:
        _terminate_pid_gracefully(int(b["pid"]), graceful_timeout=1.5)

    if orphan_daemons or orphan_repo_daemons:
        gate_failures.append(
            f"Orphan itrack-server.py daemon(s) still alive: {orphan_daemons + orphan_repo_daemons}"
        )
    if leaked_browsers:
        gate_failures.append(
            f"Orphan headless Chromium process(es) leaked by tests: {[b['pid'] for b in leaked_browsers]}"
        )

    if pytest_rc != 0:
        if failed_suite_names:
            print(
                f"\n[FAIL] {len(failed_suite_names)} suite(s) exited non-zero: "
                f"{', '.join(failed_suite_names)} (overall exit code {pytest_rc}).",
                file=sys.stderr,
            )
        else:
            print(f"\n[FAIL] Pytest exited with code {pytest_rc}.", file=sys.stderr)
        return pytest_rc

    if gate_failures:
        print("\n[FAIL] Coverage / Surface / Process Gate Failed:", file=sys.stderr)
        for idx, msg in enumerate(gate_failures, 1):
            print(f"  {idx}. {msg}", file=sys.stderr)
        return 1

    print("\n[PASS] All executed suites, Clean-Root checks, and active gates succeeded!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
