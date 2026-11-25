# AGENTS.md — iTrack Operating Rules & Auto-Bootstrap Guide

Welcome to **iTrack** (`v2.0.0`) — a local-first, Git-backed, offline-capable issue tracker, Agile board, recursive-descent iQL search engine, FastAPI/SQLite+FTS5 loopback daemon, Rich CLI, and zero-CDN Single-Page Web Application with 100% tri-surface parity across CLI, REST API, and Web UI.

This file is automatically loaded by Google Antigravity (`always_on` root `AGENTS.md`). Follow every rule below without exception.

---

## 1. Context Bootstrapping Protocol

At the start of **every** new session, before proposing plans or editing code:

1. **Read `SPEC.md` First (System Ground Truth)**:
   - Consult `SPEC.md` for the complete as-is architecture, module boundaries, canonical `<Exact Issue Name>/` disk schema, SQLite3 WAL + FTS5 tables, recursive-descent iQL grammar, all 67 `/api/v1/*` HTTP endpoints, all 12 CLI command groups / 72 leaf commands / 90 canonical flags, embedded `SPA_HTML` views/modals, environment variables, and handoff verification baseline.
2. **Read `todo.md` Second (Active Continuation Queue)**:
   - Consult `todo.md` for the latest handoff snapshot, Step 0 local bootstrap checklist, active work items, platform caveats, architectural "Do Not Repeat" decisions, and prioritized engineering roadmap (`P0` -> `P1` -> `P2`).
3. **Mandatory Living-Documentation Sync**:
   - Whenever you add, modify, or remove any feature, schema field, CLI command/flag, REST endpoint, UI view/modal, environment variable, test suite, or roadmap item, you **must** update both `SPEC.md` and `todo.md` in the same unit of work before declaring completion.
4. **Subagent / `/swarm` Orchestration Compatibility**:
   - Note that `.agents/` is intentionally absent from this repository to honor Law 1 (Clean-Root). If your host Google Antigravity environment defines global `/swarm` rules in `~/.gemini/GEMINI.md` and `~/.gemini/skills/swarm/SKILL.md`, delegate substantive research, edits, and test runs to background subagents (`Model: "inherit"`, `Workspace: "inherit"`) with strictly disjoint file ownership and isolated `/tmp` test workspaces/ports.

---

## 2. Canonical Workspace Commands

> **CRITICAL**: Always create virtual environments, scratch repos, coverage data, and pytest caches **outside** the `itrack` repository root (e.g., `~/.venvs/itrack` and `/tmp/...`), and always run Python with `PYTHONDONTWRITEBYTECODE=1` (`python3 -B`).

### Environment & Dependency Setup (One-Time Local Bootstrap)

```bash
# 1. Create and activate virtualenv OUTSIDE the repository root (Law 1 Clean-Root)
python3 -m venv ~/.venvs/itrack
source ~/.venvs/itrack/bin/activate

# 2. Prevent .pyc / __pycache__ creation anywhere in the repo or tests/
export PYTHONDONTWRITEBYTECODE=1

# 3. Install runtime + test dependencies and headless Chromium for Playwright UI parity tests
pip install -r itrack-requirements.txt
playwright install chromium
```

### Self-Teaching Agent Manual & Sandbox Dev Server

**Never** run `itrack.py init` against the `itrack` source repository itself (that would create `itrack-issues/` inside the repo root and break Law 1). Always target an external Git repository via `--root` or `ITRACK_ROOT`:

```bash
# Print full human-readable or structured JSON Agent Manual (works from any directory, exit 0)
python3 -B itrack.py
python3 -B itrack.py --json

# Create an isolated scratch Git workspace in /tmp for manual CLI + Web UI testing
mkdir -p /tmp/itrack-demo && git -C /tmp/itrack-demo init
python3 -B itrack.py --root /tmp/itrack-demo init --prefix DEMO --title "Demo Workspace"

# Start loopback daemon + print Web UI URL without opening a browser (or omit --no-browser locally)
python3 -B itrack.py --root /tmp/itrack-demo ui --no-browser --json

# Inspect daemon health / gracefully stop sandbox daemon
python3 -B itrack.py --root /tmp/itrack-demo server status --json
python3 -B itrack.py --root /tmp/itrack-demo server stop --json
```

### Syntax Check, Targeted Suites & Full Release Gate

iTrack uses zero external linters or bundlers; static syntax verification, Clean-Root auditing, `>=95.0%` Python line coverage, and `100.0%` API/CLI/UI surface parity are enforced directly via Python AST inspection and `tests/run_all.py`:

```bash
# Fast in-memory AST syntax check across all source & test files (writes ZERO .pyc files)
python3 -B -c "import ast, pathlib; [ast.parse(pathlib.Path(p).read_text('utf-8'), filename=str(p)) for p in [pathlib.Path('itrack.py'), pathlib.Path('itrack-server.py'), *sorted(pathlib.Path('tests').glob('*.py'))]]; print('AST syntax OK')"

# Run a single suite (01..09 or name substring) quickly without the full-repo coverage gate
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --suite 01 --no-coverage-gate
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --suite 07 --no-coverage-gate

# Run a single test function via pytest with all caches redirected to /tmp
PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest \
  -p no:cacheprovider \
  -o cache_dir=/tmp/itrack_pytest_cache \
  --basetemp=/tmp/itrack_pytest_tmp \
  -v tests/test_01_guardrails_config_server_and_manual.py -k "test_law1"

# MANDATORY FULL GATE: Run all 9 suites (88 tests, 3-stage parallel), >=95% line coverage,
# 100% API/CLI/Flag/UI-View/UI-Modal surface gate, Clean-Root check, and 0-orphan process audit
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all
# (On <=2-vCPU hosts where 4-worker parallel Stage 1 starves Chromium, pass --all --serial)
```

---

## 3. Codebase Architecture & Non-Negotiable Invariants

### Law 1: Strict 8-Entry Clean Repository Root
`assert_clean_repo_root()` (`tests/helpers.py:228`) and `test_law1_clean_repository_root_and_requirements_footprint()` (`tests/test_01_guardrails_config_server_and_manual.py:169`) assert before and after test runs that non-`.git` entries at the repository root match **strictly**:

- `itrack.py` — CLI client, auto-daemon spawner, Rich formatter, and zero-arg Agent Manual (`3,632` lines).
- `itrack-server.py` — FastAPI/Uvicorn loopback daemon, recursive-descent iQL compiler, `StorageEngine`, 67 `/api/v1/*` routes, and embedded `SPA_HTML` (`8,767` lines).
- `itrack-requirements.txt` — Canonical Python dependency manifest (`fastapi`, `uvicorn`, `pydantic`, `python-multipart`, `httpx`, `rich`, `pytest`, `coverage`, `playwright`).
- `tests/` — 12-file test harness (`helpers.py`, `conftest.py`, `run_all.py`, and `test_01`..`test_09`).
- `SPEC.md` — Definitive as-is system specification.
- `todo.md` — Actionable continuation tracker and prioritized engineering roadmap.
- `AGENTS.md` — Always-on Google Antigravity bootstrap rules (plain Markdown, **no YAML frontmatter**).
- `.env.example` — Sanitized environment variable reference template.

**Forbidden in repo root and `tests/` (`FORBIDDEN_ARTIFACT_NAMES`)**: `.venv`, `venv`, `.env`, `__pycache__`, `*.pyc`, `*.pyo`, `.pytest_cache`, `.coverage*`, `htmlcov`, `test-results`, `itrack-issues`, `requirements.txt`, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `.coveragerc`, or `.agents/`.

### Law 2: Git-Root Pre-Flight & Deterministic Exit Codes (`0..5`)
Both `itrack.py` (`resolve_and_check_git_root`) and `itrack-server.py` (`check_and_resolve_git_root`) require the target workspace to be a Git repository root (`.git` directory, worktree `.git` file, or direct symlink to one). Running inside a non-Git folder or a Git subdirectory fails immediately with exit code `2` and prints a `cd <toplevel>` hint. (Exception: Law 3 zero-arg `itrack.py`, `--help`, and `--version` succeed anywhere with exit `0`.)

| Exit Code | Meaning | HTTP / Condition Mapping |
| :---: | :--- | :--- |
| `0` | Success | `200` / `201` command succeeded |
| `1` | Validation / Usage Error | Invalid flag/enum/iQL, missing `--yes` on destructive op, HTTP `400` / `422` |
| `2` | Git-Root Pre-Flight Failure | Target path is not a Git repository root (Law 2) |
| `3` | Workspace / Daemon Unavailable | `itrack-issues/config.json` missing prior to `init`, daemon timeout, HTTP `503` |
| `4` | Resource Not Found | Issue, comment, attachment, sprint, milestone, tag, or view missing (HTTP `404`) |
| `5` | Conflict / Cycle / Collision | Duplicate name/key, dependency cycle, active-sprint conflict (HTTP `409`) |

### Module Ownership, Storage & Concurrency Rules
- **Strict Client/Server Boundary**: `itrack.py` never imports `itrack-server.py` or queries SQLite directly. It talks over `127.0.0.1` HTTP via `httpx.Client(..., trust_env=False)` (auto-spawning `itrack-server.py` under exclusive `fcntl.flock` on `itrack-issues/.runtime/daemon-spawn.lock` when needed). Always keep `trust_env=False` on every `httpx` client so proxy env vars never hijack loopback calls.
- **Position-Independent Global Flags**: `--json` and `--root <path>` (or `--root=<path>`) must work at **any** argv position (`extract_global_flags` in `itrack.py`) and emit clean JSON on both success and error paths.
- **Canonical Files vs. Derived Index**:
  - Inside any initialized user repo, `<git-root>/itrack-issues/` stores Git-trackable canonical JSON/JSONL + binary attachments: `config.json`, `tags.json`, `sprints.json`, `milestones.json`, `views.json`, `.gitignore` (ignoring `.runtime/`), and `issues/<Exact Issue Name>/` (`issue.json`, `history.jsonl`, `attachments/`, `comments/CMT-<seq>/comment.json` + `attachments/`).
  - Issue folder names preserve spaces and Unicode from the summary, sanitizing only filesystem-illegal characters, control chars, reserved Windows device names, and trailing dots/spaces. Renames use atomic two-step temp directories (`os.replace`) so case-only renames work safely on macOS APFS and Windows NTFS as well as Linux ext4.
  - `<git-root>/itrack-issues/.runtime/index.sqlite` (SQLite3 WAL + FTS5) is strictly **derived**. `StorageEngine` serializes mutations with `threading.RLock` + `fcntl.flock` on `.runtime/storage.lock`, writes JSON files atomically via temp-file + `os.replace`, and automatically rebuilds `index.sqlite` whenever the live filesystem SHA-256 `disk_sig` differs from `meta.disk_sig`.

### 100% Tri-Surface Parity & Air-Gapped SPA Rules
- **100% Surface Gate**: `compute_surface_audit()` (`tests/helpers.py`) introspects every `/api/v1/*` route in `itrack-server.py`, every CLI leaf command and flag in `itrack.py`, and all 8 UI views + 10 UI modals + `apiFetch` calls in `SPA_HTML`. `tests/run_all.py --all` fails if **any** route, CLI command, CLI flag, UI view, or UI modal is left unexercised (`<100.0%`) or if `SPA_HTML` calls a non-existent `/api/v1/*` endpoint.
- **Zero-CDN Offline SPA**: `SPA_HTML` inside `itrack-server.py` must be 100% self-contained HTML/CSS/JS/SVG. `tests/conftest.py` (`live_ui`) fails immediately if Chromium makes any request outside `127.0.0.1` / `localhost` / `::1` / `data:` / `blob:` / `about:`. Never reference external fonts, scripts, icons, or CDNs.
- **Linux vs. macOS `/proc` Note**: `find_itrack_daemons()` and `find_headless_browsers()` (`tests/helpers.py:377-482`) inspect `/proc`, which is Linux-specific and returns `[]` on macOS. Fixture teardown (`ws.stop_server()`) works cross-platform via `daemon.json`, while host-wide orphan process sweeping on macOS is tracked in `todo.md`.

---

## 4. Mandatory Execution & Verification Loop

Follow this exact 5-step loop for every task before reporting completion:

1. **Explore**: Read `SPEC.md`, `todo.md`, and the exact target sections in `itrack.py`, `itrack-server.py`, and `tests/`. Never guess schemas, flag names, or status codes.
2. **Plan**: Identify every affected surface (REST API, CLI human + `--json` + Agent Manual, embedded `SPA_HTML`, and `tests/` parity/coverage suites). Confirm Law 1 Clean-Root compliance.
3. **Implement**: Make surgical, atomic changes while keeping `PYTHONDONTWRITEBYTECODE=1`, `fcntl.flock` safety, `trust_env=False`, deterministic exit codes (`0..5`), and zero external CDN dependencies intact.
4. **Verify**:
   - Run the in-memory AST syntax check across `itrack.py`, `itrack-server.py`, and `tests/*.py`.
   - Run targeted test suite(s) (`python3 -B tests/run_all.py --suite <NN> --no-coverage-gate`) for fast iteration.
   - Run the full gate (`PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all`) and confirm **0 test failures**, **`>=95.0%` Python line coverage** (per-file and combined), **`100.0%` surface coverage** (API routes, CLI commands, CLI flags, UI views, UI modals), **0 orphan daemons/browsers**, and **Clean-Root pass**.
5. **Sync Docs**: Update `SPEC.md` and `todo.md` to reflect all code, schema, CLI/API/UI surface, and task-status changes.
