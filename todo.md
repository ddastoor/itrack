# iTrack v2.0.0 — Cloud-to-Local Handoff & Execution Roadmap (`todo.md`)

---

## 1. Handoff Snapshot ("Where We Left Off")

- **Snapshot Date**: `2026-11-24`
- **Repository**: `ddastoor/itrack` (`https://github.com/ddastoor/itrack`)
- **Active Branch**: `main` (tracking `origin/main`)
- **Version**: `2.0.0` (`itrack.py:31`, `itrack-server.py:51`)
- **Clean-Root Repository Footprint (8 Allowed Entries)**:
  - `itrack.py` (`3,632` lines / `159,547` B) — Git-rooted CLI client, Rich TUI renderer, flock-guarded auto-daemon spawner & programmatic zero-arg manual generator.
  - `itrack-server.py` (`8,767` lines / `411,736` B) — Per-repository FastAPI/Uvicorn local HTTP daemon, canonical folder-per-issue JSON storage engine, SQLite + FTS5 + iQL compiler/query engine & self-contained zero-CDN single-page Web UI (`SPA_HTML`).
  - `itrack-requirements.txt` (`9` lines / `149` B) — Runtime (`fastapi`, `uvicorn`, `pydantic`, `python-multipart`, `httpx`, `rich`) and verification (`pytest`, `coverage`, `playwright`) dependencies.
  - `tests/` (`12` files / `15,185` lines / `88` test functions) — `conftest.py`, `helpers.py`, `run_all.py`, and suites `test_01` through `test_09`.
  - `SPEC.md`, `todo.md`, `AGENTS.md`, `.env.example` — Cloud-to-Local handoff specification, execution roadmap, agent rules & environment variable reference.

### Recent Git Commit History (`main`)
- `HEAD` (handoff commit) — `docs(handoff): add SPEC.md, todo.md, AGENTS.md, .env.example and allowlist in Law 1`
- `ecaf937` (`2026-11-24 20:47:18 +0000`) — `feat: implement iTrack Addendum Specification and 9-suite 100% parity gate`
- `a28ec12` (`2026-11-24 07:14:15 +0000`) — `chore(agents): enforce universal /swarm mode and zero-loss auto-resume on every prompt`
- `11c9126` (`2026-11-24 07:11:25 +0000`) — `feat: build itrack server, CLI, offline SPA, /swarm rules, and full UI-CLI parity test suite`
- `ed01971` (`2026-11-23 19:10:27 +0000`) — `chore: phase 1 sync point — requirements.txt and strict 3-file repo prep`
- `eb83e93` (`2026-11-24 00:12:38 +0530`) — `Initial commit`

### Completed Milestones
- [x] **Law 1 (Clean-Root) & Law 2 (Git-Root Pre-Flight) Guardrails**: Implemented `.git` directory/worktree/symlink verification with subdirectory `Hint: cd <toplevel>` (`exit 2`) in `itrack.py:370-427` and `itrack-server.py:415-440`, pre-init `exit 3` guard in `itrack.py:430-439`, and strict root/cache auditor in `tests/helpers.py:228-261`.
- [x] **Flock-Synchronized Per-Repository Daemon Lifecycle**: Implemented cold-start `fcntl.flock` spawner (`itrack.py:527-607`), `/api/v1/health` `repo_root` identity probe (`itrack.py:486-506`), graceful `/api/v1/shutdown` + `SIGTERM` + stale `daemon.json` recovery (`itrack.py:619-666`), and `server start|stop|restart|status` + `ui [--foreground] [--no-browser]` (`itrack.py:1255-1392`).
- [x] **Canonical Git-Friendly Folder-Per-Issue Storage & Atomic Spaced Folder Renames**: Implemented `itrack-issues/issues/<Exact Issue Name>/issue.json` + `history.jsonl` + `attachments/` + `comments/<CMT-N>/` (`itrack-server.py:1872-1935`), dual addressing (`ITR-N` key or exact spaced/Unicode folder name), and collision-safe atomic folder renames updating peer link `target_name` and FTS5 immediately (`itrack-server.py:3030-3165`, `tests/test_03_folder_renames_with_spaces.py:49-756`).
- [x] **SQLite + FTS5 Derived Index, 16-Operator iQL Compiler & Self-Healing**: Implemented `itrack-issues/.runtime/index.sqlite` with SHA-256 disk-signature auto-sync on out-of-band `git pull` (`itrack-server.py:1406-1439`), automatic rebuild on corrupt SQLite bytes (`itrack-server.py:1313-1595`), and full iQL lexer/parser/compiler (`itrack-server.py:712-1272`).
- [x] **100% CLI Surface (`72` Leaf Commands, `12` Group Commands, `90` Flags) & Zero-Drift Manual**: Implemented full CRUD across issues, rich Markdown comments, inline image embeds (`PNG`/`SVG`/`JPG`/`WEBP`), comment & whole-issue attachments, cross-linked attachment tombstones, bidirectional links & cycle detection (`exit 5`), tags, sprints + rollover, milestones, saved views, worklogs, history, bulk ops, stats, and AST-walked zero-arg manual (`itrack.py:2933-3566`).
- [x] **Offline Zero-CDN Single-Page Web UI (`8` Views, `10` Modals)**: Built self-contained `SPA_HTML` in `itrack-server.py:5340-8665` with dark/light theme persistence, slide-over drawer + `#/issues/<ref>` permalinks, Kanban drag-and-drop + 5 swimlanes + WIP limits, 3 inline image embed methods (file picker, HTML5 drag-drop, `Ctrl+V` clipboard paste), lightbox/text preview, and XSS sanitization.
- [x] **3-Stage Parallel Master Runner, `>=95%` Coverage Gate & `100%` Surface Parity Gate**: Built `tests/run_all.py:1-879` combining parallel subprocess/daemon coverage via `sitecustomize.py` + `sysmon` and enforcing `88/88` passing tests across `test_01`..`test_09`.

### Live Verification Baseline (`tests/run_all.py --all`)
- **Verified Environment**: Ubuntu 24.04 Linux (`2` vCPUs), `Python 3.12.16`, `fastapi==0.165.0`, `uvicorn==0.54.0`, `pydantic==2.14.4`, `python-multipart==0.0.34`, `httpx==0.28.1`, `rich==15.0.0`, `pytest==9.2.0`, `coverage==7.17.5` (`sysmon`), `playwright==1.65.2` (Headless Chromium `159.0.8154.6`).
- **AST & In-Memory Compile Check**: `14/14` `.py` files passed (`0` errors, `0` `.pyc` written).
- **Full Master Gate (`PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all`)**: **`9/9` suites passed, `88/88` tests passed (`0` failed, `0` skipped, exit `0`)** in `538.92s` (`test_01`: 17, `test_02`: 14, `test_03`: 5, `test_04`: 9, `test_05`: 27, `test_06`: 3, `test_07`: 5, `test_08`: 3, `test_09`: 5).
- **Python Line Coverage (`>=95.0%` Gate)**: `itrack.py` **`2,392 / 2,401 = 99.63%`**, `itrack-server.py` **`3,603 / 3,682 = 97.85%`**, Combined **`5,995 / 6,083 = 98.55%`** (`PASS`).
- **100% Surface & Parity Gate**: API routes **`67/67 = 100.00%`**, CLI subcommands **`72/72 = 100.00%`**, CLI option flags **`90/90 = 100.00%`**, Web UI views **`8/8 = 100.00%`**, Web UI modals **`10/10 = 100.00%`**, UI->Backend API parity **`14/14 = 100.00%`** (`0` orphans, `PASS`).

---

## 2. Immediate Post-Clone Local Bootstrap Checklist (Step 0)

Execute these steps in order on your local Linux or macOS machine immediately after cloning:

- [ ] **Step 0.1 — Clone Repository & Confirm Clean-Root Footprint**:
  ```bash
  git clone https://github.com/ddastoor/itrack.git
  cd itrack
  git status --short --branch
  ls -la
  ```
  Verify the directory contains **strictly** `.git`, `.env.example`, `AGENTS.md`, `SPEC.md`, `itrack-requirements.txt`, `itrack-server.py`, `itrack.py`, `tests`, and `todo.md`.

- [ ] **Step 0.2 — Create an EXTERNAL Python 3.12+ Virtual Environment & Disable Bytecode**:
  > **CRITICAL (Law 1 Clean-Root)**: Do **NOT** create `.venv` or `venv` inside `./itrack`! `assert_clean_repo_root()` (`tests/helpers.py:228-261`) fails immediately if any unlisted directory or `__pycache__` exists inside the repository root or `tests/`.
  ```bash
  export PYTHONDONTWRITEBYTECODE=1
  python3 -m venv ~/.venvs/itrack
  source ~/.venvs/itrack/bin/activate
  python3 --version   # Must be Python 3.10+ (Python 3.12+ recommended for sysmon coverage)
  ```

- [ ] **Step 0.3 — Install Python Runtime & Test Dependencies**:
  ```bash
  pip install --upgrade pip
  pip install -r itrack-requirements.txt
  ```

- [ ] **Step 0.4 — Install Headless Chromium for Playwright Web UI Suites (`test_07` & `test_08`)**:
  ```bash
  python3 -m playwright install chromium
  # On a fresh Ubuntu/Debian machine missing system shared libraries, also run:
  # python3 -m playwright install-deps chromium
  ```

- [ ] **Step 0.5 — Review `.env.example` (Keep `.env` Outside Repo Root)**:
  ```bash
  cat .env.example
  ```
  All environment variables (`ITRACK_ROOT`, `ITRACK_USER`, `ITRACK_AGENT`, `ITRACK_SERVER_SCRIPT`, `ITRACK_SURFACE_LOG`) are **optional** and have automatic local defaults. Do **not** create an un-allowlisted `.env` file inside the `itrack` repository root; export variables in your shell profile if desired.

- [ ] **Step 0.6 — *(Optional)* Install Global `/swarm` Rules for Local Antigravity Desktop**:
  On the Cloud VM, universal `/swarm` orchestration lived in `~/.gemini/GEMINI.md` and `~/.gemini/skills/swarm/SKILL.md` (outside Git). If using Antigravity locally and you want `/swarm` active across all projects globally, copy the rules documented in `AGENTS.md` into `~/.gemini/GEMINI.md` and `~/.gemini/skills/swarm/SKILL.md`.

- [ ] **Step 0.7 — Run the Master Verification Suite (`tests/run_all.py`)**:
  - **On Linux**: Run the full 3-stage parallel suite + `>=95%` Python coverage gate + `100%` CLI/API/UI surface gate (add `--serial` on `<=2`-vCPU machines):
    ```bash
    PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all
    ```
  - **On macOS (prior to completing Task `P0-1` below)**: `tests/helpers.py:390` currently scans Linux `/proc` for daemon PID count assertions in `test_09`. Verify core suites `01..08` first, then implement `P0-1` to unlock `test_09` on macOS:
    ```bash
    PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --suite 01,02,03,04,05,06,07,08 --no-coverage-gate
    ```

- [ ] **Step 0.8 — Smoke-Test CLI & Offline Web UI in an External Sandbox Git Repo**:
  > **CRITICAL**: Never run `python3 -B itrack.py init` inside the `itrack` source repository itself, because creating `./itrack/itrack-issues/` violates `FORBIDDEN_ARTIFACT_NAMES` / `EXPECTED_ROOT_ENTRIES` (`tests/helpers.py:45-54, 214`). Always target a separate Git repo:
  ```bash
  mkdir -p /tmp/itrack-demo && git -C /tmp/itrack-demo init -q
  git -C /tmp/itrack-demo config user.name "Local Dev"
  git -C /tmp/itrack-demo config user.email "dev@local"

  PYTHONDONTWRITEBYTECODE=1 python3 -B itrack.py --root /tmp/itrack-demo init --prefix DEMO --title "Local Demo"
  PYTHONDONTWRITEBYTECODE=1 python3 -B itrack.py --root /tmp/itrack-demo issue create "Verify Local Handoff" --category task --priority high
  PYTHONDONTWRITEBYTECODE=1 python3 -B itrack.py --root /tmp/itrack-demo ui
  # When finished inspecting the browser UI:
  PYTHONDONTWRITEBYTECODE=1 python3 -B itrack.py --root /tmp/itrack-demo server stop
  ```

---

## 3. In-Flight / Partially Completed Work (Immediate Resume Queue)

**Ground-Truth Status**: All v2.0.0 specification requirements, CLI commands (`72` leaf / `12` group / `90` flags), FastAPI routes (`67` `/api/v1/*` + `4` SPA/static routes), and all `88` tests across `test_01`..`test_09` are **100% complete and passing on Linux** with zero uncommitted work left mid-stream.

When resuming development locally, pick up these three immediate continuity tracks:

- [ ] **Track 3.1 — macOS Local Test-Runner Parity (`ps` Fallback When `/proc` Is Absent)**
  - **What is already built**: `itrack.py` (`L459-L470`, `L544-L607`) and `itrack-server.py` (`L504-L539`) use standard POSIX `os.kill(pid, 0)` and `fcntl.flock`, so the CLI, daemon, and Web UI already run natively on both Linux and macOS.
  - **What remains**: `tests/helpers.py` (`_is_pid_running` at `L344-L366`, `find_itrack_daemons` at `L377-L450`, and `find_headless_browsers` at `L453-L481`) inspects Linux `/proc/<pid>/stat` and `/proc/<pid>/cmdline`, returning `[]` when `/proc` does not exist on macOS. Add a `subprocess.run(["ps", "-axo", "pid=,stat=,command="])` fallback when `not Path("/proc").exists()`.
  - **Gotchas / Watch-outs**: On macOS, `/tmp` is a symlink to `/private/tmp`; preserve `find_itrack_daemons`'s `Path(tmp_prefix).resolve()` comparison (`tests/helpers.py:385, 429-437`) and filter out zombie (`stat.startswith("Z")`) processes so `test_09` daemon assertions remain exact.

- [ ] **Track 3.2 — Interactive Edit Forms for Sprints, Milestones & Saved Views in `SPA_HTML`**
  - **What is already built**: Backend `PATCH /api/v1/sprints/{ref}` (`itrack-server.py:4564`), `PATCH /api/v1/milestones/{ref}` (`itrack-server.py:4753`), and `PATCH /api/v1/views/{ref}` (`itrack-server.py:4846`) support full field editing (name, goal/description, dates, query, tab, sort, swimlane), and CLI `sprint edit`, `milestone edit`, and `view edit` expose all flags (`itrack.py:3262-3337`).
  - **What remains**: In `SPA_HTML`, clicking `.btn-sprint-edit` (`itrack-server.py:7339-7347`), `.btn-ms-edit` (`itrack-server.py:7431-7435`), or `.btn-view-edit` (`itrack-server.py:7506-7510`) currently performs a one-click `PATCH` appending `' (updated)'` instead of opening an inline form or reusing `#modal-save-view`.
  - **Gotchas / Watch-outs**:
    1. `tests/test_07_playwright_full_web_ui_every_feature.py` (`L1193-1195`, `L1246-1248`, `L1478-1480`) explicitly clicks those three buttons and asserts `"(updated)"` appears on the card. Update `test_07` in lockstep.
    2. If you add any new `data-modal="..."` element to `SPA_HTML`, you **must** also add its name to `UI_MODALS` (`tests/helpers.py:167-178`), update `assert len(UI_MODALS) == ...` in `tests/test_08_bidirectional_cli_ui_parity_and_100pct_gate.py:1415`, and exercise it in Playwright so the 100% UI modal surface gate passes. Reusing inline card edit inputs (like `.ac-inline-edit-input` at `itrack-server.py:7911-7924`) or reusing `data-modal="save-view"` avoids changing the modal count.

- [ ] **Track 3.3 — `config.json` Initialization Schema Unification (`itrack.py` vs `itrack-server.py`)**
  - **What is already built**: `itrack-server.py:443-474` (`default_config_dict`) defines canonical sequence keys `next_issue_seq`, `next_att_seq`, `next_comment_seq`, `next_link_seq`, `next_worklog_seq`, `next_history_seq`, `next_sprint_seq`, `next_milestone_seq`, `next_view_seq` (all `1`) + `"default_assignee": None`, and `init_workspace_on_disk` (`L517-L526`) backfills missing keys into existing `config.json` files.
  - **What remains**: Align `default_cfg` in `itrack.py:1109-1136` (`cmd_init`) with `default_config_dict` so CLI-initialized workspaces do not carry a vestigial nested `"sequences": {...}` object alongside `next_*_seq`.
  - **Gotchas / Watch-outs**: Keep `"version": VERSION` if desired, or ensure `test_01:469-487` and `test_05:2000-2033` continue to pass with `>=95%` line coverage on both files.

---

## 4. Bugs, Failing Tests & Codebase `TODO`/`FIXME` Triage

- **Failing Tests on Linux**: **`0`** (`88/88` passing across `test_01`..`test_09`).
- **Codebase `TODO` / `FIXME` / `XXX` / `HACK` / `NotImplemented` Comments**: **`0`** across `itrack.py`, `itrack-server.py`, and `tests/*.py`.

### Concrete Portability, Edge-Case & Durability Triage (`filepath:line`)

- [ ] **[Portability — High] Linux `/proc` Hardcoding Breaks `test_09` & Process Reapers on macOS (`tests/helpers.py:355, 390, 457`)**:
  - `find_itrack_daemons()` (`tests/helpers.py:390-392`) and `find_headless_browsers()` (`tests/helpers.py:457-459`) return `[]` immediately when `Path("/proc")` does not exist.
  - Impact: On macOS, `tests/test_09_concurrent_agent_stress_and_self_healing.py` fails at `L82-83`, `L569-570`, `L644`, and `L706-707` (`assert len(daemons) == 1`), and orphan-daemon/browser leak checks in `tests/run_all.py:840-855` silently become no-ops.

- [ ] **[Test Timing — Medium on `<=2`-vCPU Hosts] 4-Worker CPU Starvation Window on `#detail-title-input` `Enter` Rename (`tests/test_07_playwright_full_web_ui_every_feature.py:414-427`)**:
  - Under `STAGE1_MAX_WORKERS = 4` (`tests/run_all.py:38`) on a `2`-vCPU machine, `_FETCH_IDLE_SCRIPT` (`test_07:57-83`) decrements `window.__itrackInFlight` via `setTimeout(..., 0)` when `origFetch` resolves; if CPU contention delays `renderIssueDetailDOM(iss)` (`itrack-server.py:7807, 7833`) from the `#btn-save-title` click rename, it can overwrite `.fill(renamed_by_enter)` on `#detail-title-input` just before `.press("Enter")`, causing `submitTitleRename()` (`itrack-server.py:8182`) to treat `newName === appState.activeIssue.name` as a no-op.
  - Mitigation / Fix: Run `PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all --serial` on `<=2`-vCPU hosts, or assert `ws.api("GET", ...)` / DOM settle on `detail-title-input` and cap `STAGE1_MAX_WORKERS = min(4, os.cpu_count() or 2)`.

- [ ] **[Test Harness Warning — Low] `PytestConfigWarning: Unknown config option: cache_dir` Under `pytest==9.2.0` (`tests/run_all.py:434-437, 790-793`)**:
  - Passing `-p no:cacheprovider` unregisters the `cache_dir` ini option in `pytest 9.2.0`, causing `-o cache_dir=...` to emit 1 `PytestConfigWarning` per suite (harmless by default, but fails if `--strict-config` or `-W error` is enabled; dropping `-o cache_dir=...` when `-p no:cacheprovider` is active removes the warning cleanly).

- [ ] **[Schema Consistency — Low] Vestigial `"sequences"` Dict in `cmd_init` (`itrack.py:1109-1137` vs `itrack-server.py:449-474`)**:
  - `cmd_init` writes `"sequences": {"issue": 0, ...}` and omits `"default_assignee": None` before spawning the daemon (`itrack.py:1141`), which then backfills `next_*_seq: 1` and `"default_assignee": None`. Harmless at runtime, but leaves unused keys in `itrack-issues/config.json`.

- [ ] **[CLI Argument Edge Cases — Low] `extract_global_flags_and_surface` Corner Cases (`itrack.py:271-273, 313-317`)**:
  - `itrack.py:271-273`: Stops scanning at `--`, so `python3 itrack.py issue create -- "Name" --json` does not strip `--json` and fails `argparse` positional parsing.
  - `itrack.py:313-317`: Single-value flags (`action.nargs is None`) unconditionally consume `argv[i]` if `!= "--"`, so `python3 itrack.py issue create "X" --assignee --json` consumes `"--json"` as the assignee string instead of enabling JSON mode.

- [ ] **[Observability — Low] `cmd_ui --foreground` Drops Server Startup Logs (`itrack.py:1334`)**:
  - `subprocess.Popen(..., stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)` discards `itrack-server.py` stderr in `--foreground` mode, whereas `spawn_daemon` (`itrack.py:573-595`) appends to `.runtime/daemon.log` and includes `read_log_tail(..., 25)` on failure.

- [ ] **[Process Lifecycle — Low] `stop_daemon` Has No `SIGKILL` Escalation After `SIGTERM` (`itrack.py:645-653`)**:
  - `stop_daemon` waits `3.5s` after `POST /api/v1/shutdown`, sends `signal.SIGTERM` and polls `2.0s`, then unlinks `daemon.json` even if the process ignored `SIGTERM` (contrast with `tests/helpers.py:499-506` `_terminate_pid_gracefully`, which escalates to `signal.SIGKILL`).

- [ ] **[Crash Durability — Low] Server `_atomic_write_bytes` Omits `os.fsync` (`itrack-server.py:477-483`)**:
  - Writes temp file, calls `f.flush()`, and executes `os.replace(tmp, path)` without `os.fsync(f.fileno())` before rename (whereas CLI `_atomic_write_json` at `itrack.py:1052` does call `os.fsync`).

- [ ] **[Networking — Trivial] IPv4 Socket Pre-Bind vs `"::"` Host Check (`itrack-server.py:8736` vs `itrack-server.py:4899`)**:
  - `itrack-server.py:4899` checks `if host in ("0.0.0.0", "::")`, but `main()` (`itrack-server.py:8736`) binds `socket.socket(socket.AF_INET, socket.SOCK_STREAM)`, which rejects IPv6 `"::"` addresses.

- [ ] **[Test Hygiene — Trivial] Out-of-Order Test Naming in `test_04` (`tests/test_04_rich_comments_embeds_and_attachments_matrix.py:1038, 1073`)**:
  - `test_09_rich_tui_deleted_linked_attachment_tombstone_text_preserved` (`L1038`) is placed above `test_08_full_comment_edit_history_fts5_purge_and_comment_delete_isolation` (`L1073`).

---

## 5. Stubbed, Mocked, or Placeholder Functionality

All backend storage methods, REST endpoints, iQL operators, and CLI commands are 100% real. The only simplified/unpopulated behaviors in the repository are inside the embedded Web UI (`SPA_HTML` in `itrack-server.py`):

- [ ] **SPA Sprint "Edit Goal" Button Appends `' (updated)'` (`itrack-server.py:7339-7347`)**:
  - Clicking `.btn-sprint-edit` on `#view-sprints` sends `PATCH /api/v1/sprints/{id}` with `{ goal: card.dataset.editedGoal || (b.dataset.goal + ' (updated)') }` instead of rendering an interactive goal/name/date editor.
- [ ] **SPA Milestone "Edit" Button Appends `' (updated)'` (`itrack-server.py:7431-7435`)**:
  - Clicking `.btn-ms-edit` on `#view-milestones` sends `PATCH /api/v1/milestones/{id}` with `{ description: (b.dataset.desc || 'Milestone') + ' (updated)' }` instead of opening an interactive description/due-date/name editor.
- [ ] **SPA Saved View "Edit" Button Appends `' (updated)'` (`itrack-server.py:7506-7510`)**:
  - Clicking `.btn-view-edit` on `#view-views` sends `PATCH /api/v1/views/{id}` with `{ description: (b.dataset.desc || 'Saved view') + ' (updated)' }` instead of opening `#modal-save-view` pre-populated with the view's `name`, `query`, `tab`, `sort`, `order`, `swimlane`, and `description`.
- [ ] **Unpopulated `window.__itrackTelemetry.actions` Bucket (`itrack-server.py:6422, 6427`)**:
  - `window.__itrackTelemetry` initializes `{ views: new Set(), modals: new Set(), actions: new Set() }`. While `views` (`L6859`) and `modals` (`L6715, L7787`) are actively recorded and enforced at 100% by `compute_surface_audit` (`tests/helpers.py:1554-1664`), `actions` has zero `.add(...)` calls in `SPA_HTML` and always serializes as `[]`.
- [ ] **Decorative GFM Task-List Checkboxes in Rendered Markdown (`itrack-server.py:6680`)**:
  - `- [ ]` / `- [x]` lines inside Markdown descriptions and comments render `<input type="checkbox" class="md-task-checkbox">` without a `change` handler to rewrite Markdown source (whereas structured Acceptance Criteria `.ac-check-toggle` at `L7897-L7909` and Subtask `.subtask-toggle` at `L7930-L7944` persist immediately via `PATCH`).

---

## 6. Prioritized Engineering Roadmap (`P0` -> `P1` -> `P2`)

### `P0` — Cross-OS Portability, Crash Durability & Schema Hygiene

- [ ] **`P0-1`: Cross-Platform macOS + Linux Process & Daemon Discovery in Test Harness**
  - **Objective**: Enable `find_itrack_daemons()`, `find_headless_browsers()`, and `_is_pid_running()` to work identically on macOS (`darwin`) and Linux when `/proc` is absent so all 9 suites (including `test_09` and `run_all.py` orphan gates) pass 100% natively on macOS.
  - **Target Files**: `tests/helpers.py` (`L344-L481`).
  - **Dependencies**: None.
  - **Verification Criteria**: Run `PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all` on both Linux and macOS (or simulate `Path("/proc")` absence in a unit test); all `88` tests pass including `test_09` daemon count assertions (`L83, L570, L644, L707`) and `run_all.py` exits `0`.

- [ ] **`P0-2`: Add `os.fsync` Durability in Server Atomic Writes & `SIGKILL` Fallback in CLI `stop_daemon`**
  - **Objective**: Guarantee power-loss file durability in `_atomic_write_bytes` by calling `os.fsync(f.fileno())` prior to `os.replace`, and ensure `stop_daemon` escalates to `signal.SIGKILL` if a wedged daemon survives `SIGTERM` after `2.0s`.
  - **Target Files**: `itrack-server.py` (`L477-L484`), `itrack.py` (`L645-L659`), `tests/test_01_guardrails_config_server_and_manual.py` (`L1435-L1466`).
  - **Dependencies**: None.
  - **Verification Criteria**: Add mocked wedged-PID unit branch in `test_01` verifying `SIGKILL` escalation; run `PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all` with `>=95.0%` coverage on both `itrack.py` and `itrack-server.py`.

- [ ] **`P0-3`: Unify `cmd_init` `config.json` Schema & Log Foreground UI Server Output**
  - **Objective**: Align `cmd_init`'s `default_cfg` (`itrack.py:1109-1136`) with `itrack-server.py:443-474` (`next_*_seq: 1`, `"default_assignee": None`, removing vestigial `"sequences"`), and route `cmd_ui --foreground` subprocess output (`itrack.py:1334`) to `.runtime/daemon.log` with `read_log_tail` error reporting on crash/timeout.
  - **Target Files**: `itrack.py` (`L1109-L1137`, `L1320-L1366`), `tests/test_01_guardrails_config_server_and_manual.py` (`L464-L507`, `L1592-L1640`).
  - **Dependencies**: None.
  - **Verification Criteria**: Fresh `itrack.py init` and `itrack-server.py --init` produce identical `config.json` key sets; `PYTHONDONTWRITEBYTECODE=1 python3 -B tests/run_all.py --all` exits `0`.

### `P1` — Web UI Interactive Parity & CLI Ergonomics

- [ ] **`P1-1`: Replace One-Click `' (updated)'` Sprint, Milestone & Saved View Edit Handlers with Interactive Editors**
  - **Objective**: Replace the 3 placeholder `' (updated)'` `PATCH` handlers on `#view-sprints`, `#view-milestones`, and `#view-views` with real inline edit rows (for Sprint name/goal/dates and Milestone name/description/due_date) and edit-mode reuse of `#modal-save-view` (for Saved Views).
  - **Target Files**: `itrack-server.py` (`L7339-L7347`, `L7431-L7435`, `L7506-L7510`, `L8520-L8565`), `tests/test_07_playwright_full_web_ui_every_feature.py` (`L1193-L1196`, `L1246-L1249`, `L1477-L1481`).
  - **Dependencies**: None (keep `UI_MODALS` count at `10` by reusing `data-modal="save-view"`, or update `tests/helpers.py:167-178` and `tests/test_08_bidirectional_cli_ui_parity_and_100pct_gate.py:1415` if adding modals).
  - **Verification Criteria**: Playwright tests in `test_07` edit Sprint goal/dates, Milestone description/due date, and Saved View query/name via real UI inputs, verify persisted JSON via `/api/v1/*`, and pass `run_all.py --all` with 0 console errors and 100% modal/view surface coverage.

- [ ] **`P1-2`: Instrument `window.__itrackTelemetry.actions` Across Key SPA User Interactions**
  - **Objective**: Populate `window.__itrackTelemetry.actions` on core SPA mutations (`issue-create`, `issue-rename`, `kanban-drag`, `comment-add`, `embed-paste`, `bulk-update`, `sprint-rollover`, `view-run`, `settings-save`) and optionally include `ui_actions` in `compute_surface_audit`.
  - **Target Files**: `itrack-server.py` (`L6422-L6430` and corresponding event handlers), `tests/helpers.py` (`L647-L707`, `L1554-L1664`), `tests/test_07_playwright_full_web_ui_every_feature.py` (`L1502-L1510`).
  - **Dependencies**: `P1-1`.
  - **Verification Criteria**: `live_ui.harvest_telemetry()["actions"]` is non-empty and exercises 100% of registered UI action keys during `run_all.py --all`.

- [ ] **`P1-3`: Harden CLI Global Flag Extraction (`--` & Missing-Value Lookahead) & Optional Markdown Task Toggle**
  - **Objective**: Refine `extract_global_flags_and_surface` (`itrack.py:270-330`) so `--assignee --json` raises `invalid_arguments` in JSON mode instead of consuming `"--json"` as a value, and wire `.md-task-checkbox` (`itrack-server.py:6680`) inside `#detail-desc-rendered` / `.comment-rendered-body` to toggle `- [ ]` / `- [x]` via `PATCH`.
  - **Target Files**: `itrack.py` (`L253-L343`), `itrack-server.py` (`L6675-L6690`, `L7830-L8150`), `tests/test_01_guardrails_config_server_and_manual.py`, `tests/test_07_playwright_full_web_ui_every_feature.py`.
  - **Dependencies**: None.
  - **Verification Criteria**: Unit + Playwright tests verify `--assignee --json` emits JSON `exit_code: 1` and clicking a rendered Markdown task checkbox persists `[x]` to `issue.json`/`comment.json`.

- [ ] **`P1-4`: Workspace Snapshot Export/Import & Optional UI Daemon Shutdown Button**
  - **Objective**: Add portable single-file JSON/ZIP workspace export/import (`itrack export` / `itrack import` + `/api/v1/export` & Settings UI button) and an optional "Stop Background Server" control in `#view-settings` calling `POST /api/v1/shutdown`.
  - **Target Files**: `itrack-server.py`, `itrack.py`, `tests/test_08_bidirectional_cli_ui_parity_and_100pct_gate.py` (update route `67`, command `72`, and flag `90` assertions at `L1416-L1418`).
  - **Dependencies**: Remember **Rule 7.9** (every new `/api/v1/*` route, CLI command, and CLI flag must have 100% test execution and updated `test_08` counts).
  - **Verification Criteria**: Export from Workspace A -> Import into Workspace B preserves all issues, comments, attachments (SHA-256 identical), links, sprints, milestones, tags, views, and history; `run_all.py --all` passes 100% surface gate.

### `P2` — Large-Scale Performance (`1,000+` Issues) & Network Polish

- [ ] **`P2-1`: Incremental Disk Signature & SQLite-Backed Peer Link Lookups for `1,000+` Issue Repositories**
  - **Objective**: Replace full $O(N)$ directory tree walk in `StorageEngine._compute_disk_signature` (`itrack-server.py:1406-1439`) with cached directory mtime/manifest tracking, use SQLite `issue_links` index to target only affected peer folders during renames in `_save_issue_unlocked` (`itrack-server.py:1872-1895`), and lift `page_size=1000` internal caps in `get_board` (`L2730`) and `bulk_update`/`bulk_delete` (`L4232`).
  - **Target Files**: `itrack-server.py` (`L1406-L1439`, `L1872-L1895`, `L2725-L2740`, `L4225-L4295`), `tests/test_05_hierarchy_links_search_pagination_and_agile.py`, `tests/test_09_concurrent_agent_stress_and_self_healing.py`.
  - **Dependencies**: None.
  - **Verification Criteria**: Benchmark with 2,000 synthetic issues completes `issue list`, `board`, `bulk update`, and `issue rename` in `<250ms` while preserving out-of-band `git pull` detection in `test_09:539-647`.

- [ ] **`P2-2`: Dual-Stack IPv6 Socket Binding & Configurable Runner Temp Directory**
  - **Objective**: Support IPv6 (`--host ::1` / `--host ::`) in `itrack-server.py:8736` by selecting `socket.AF_INET6` when `":" in host`, and replace hardcoded `/tmp/itrack_*` paths in `tests/run_all.py:712-720` with `Path(tempfile.gettempdir())`.
  - **Target Files**: `itrack-server.py` (`L4898-L4905`, `L8730-L8755`), `tests/run_all.py` (`L710-L755`, `L840`).
  - **Dependencies**: `P0-1`.
  - **Verification Criteria**: `itrack-server.py --host ::1 --port 0` binds and passes health check; `run_all.py --all` respects custom `TMPDIR`.

---

## 7. Architectural Decisions, Constraints & "Do Not Repeat" Log

1. **Law 1 — Strict Clean-Root Allowlist (`tests/helpers.py:45-54`, `tests/test_01_guardrails_config_server_and_manual.py:180-191`)**:
   - `assert_clean_repo_root()` (`tests/helpers.py:228-261`) asserts that `set(os.listdir(REPO_ROOT)) - {".git"}` equals **strictly** `EXPECTED_ROOT_ENTRIES` (`{"itrack.py", "itrack-server.py", "itrack-requirements.txt", "tests", "SPEC.md", "todo.md", "AGENTS.md", ".env.example"}`) and walks `tests/` to reject `__pycache__`, `.pytest_cache`, `.coverage*`, `.pyc`, `itrack-issues`, `requirements.txt`, `pyproject.toml`, `pytest.ini`, and `setup.cfg`.
   - **Do Not Repeat**: Never create `.venv` inside the repo, never rename `itrack-requirements.txt` to `requirements.txt` (`test_01:178, 1029`), never run `itrack.py init` against the `itrack` repo itself, and always run Python/pytest with `PYTHONDONTWRITEBYTECODE=1` and `-B`.

2. **Law 2 — Git-Root Pre-Flight & Exit Code `2` Exclusivity (`itrack.py:391-427`, `itrack.py:2926-2930`)**:
   - Exit code `2` means **exclusively** "not at a Git repository root" (checked before zero-arg manual, `--help`, `--version`, and subcommands). Standard `argparse.ArgumentParser` exits `2` on invalid flags, which would violate the exit-code contract (`0..5`); `CLIArgumentParser.error()` (`itrack.py:2926-2930`) intentionally overrides this to raise `CLIError(..., exit_code=1)`.
   - **Do Not Repeat**: Never replace `CLIArgumentParser` with `argparse.ArgumentParser`, and always check `.exists()` (not `.is_dir()`) on `.git` so Git worktrees (where `.git` is a file) continue passing Law 2 (`test_01:372-392`).

3. **Exact Human-Readable Issue Folder Names + Immutable `ITR-N` Keys (`itrack-server.py:1872-1935, 3030-3165`)**:
   - On disk, issues live at `itrack-issues/issues/<Exact Issue Name>/issue.json` (preserving spaces and Unicode, e.g., `itrack-issues/issues/OAuth2 Token Refresh Race/issue.json`), while `ITR-N` keys remain immutable across renames.
   - **Do Not Repeat**: Never slugify folder names or prefix folders with `ITR-N_`. Case-only renames (e.g., `fix ui layout` -> `Fix UI Layout`) must continue routing through `.rename_tmp_<uuid>` two-step atomic moves so case-insensitive filesystems (macOS APFS default) do not silently no-op (`test_03:504-608`).

4. **Canonical JSON Truth vs Derived Ephemeral SQLite (`itrack-issues/.gitignore`, `itrack-server.py:1313-1595`)**:
   - `itrack-issues/.runtime/` (`index.sqlite*`, `daemon.json`, `daemon.log`, `*.lock`) is 100% ephemeral and gitignored (`test_02:1253-1302`). Every read/search verifies `_compute_disk_signature()` so `git pull` / branch switches auto-reindex without daemon restarts, and corrupt SQLite bytes trigger automatic delete-and-rebuild (`test_09:539-673`).
   - **Do Not Repeat**: Never store canonical state only in SQLite without writing to `issue.json` / `comment.json` / `tags.json` / `sprints.json` / `milestones.json` / `views.json`.

5. **100% Offline Air-Gapped SPA (`tests/conftest.py:61-67, 183-186`)**:
   - `live_ui` intercepts every browser network request and fails teardown if any URL targets a non-loopback host (`external_requests`).
   - **Do Not Repeat**: Never link external CDNs (Google Fonts, Tailwind/unpkg/jsdelivr, external avatars, or remote icons) inside `SPA_HTML`.

6. **Proxy-Safe Loopback HTTP Clients (`trust_env=False` at `itrack.py:496, 634, 674, 1370`)**:
   - Cloud VMs and corporate workstations frequently set `HTTP_PROXY` / `HTTPS_PROXY`, which causes `httpx` to route `http://127.0.0.1:<port>` through an external proxy and fail with `502`/`503`.
   - **Do Not Repeat**: Every `httpx.Client` talking to the local daemon in `itrack.py` or `tests/` must set `trust_env=False`.

7. **Dual HTML5 DragEvent + Mouse Pointer Kanban Drag-and-Drop (`itrack-server.py:7206-7254`)**:
   - `SPA_HTML` supports both native HTML5 `dragstart`/`drop` and `mousedown`/`mousemove`/`mouseup` (`document.elementFromPoint`) with a `moveInFlight` mutex so both human desktop dragging and headless Chromium Playwright `.drag_to()` work reliably across swimlanes.
   - **Do Not Repeat**: Do not remove either listener path without running `test_07` (`L1270-L1288`) and `test_08` (`L244-L256`).

8. **Escaped Rich BBCode Brackets for Deleted-Attachment Tombstones (`itrack.py:894, 905`)**:
   - Rich interprets unescaped `[deleted attachment: ...]` as a style tag and strips it from terminal output. `itrack.py:894, 905` escapes `\\[deleted attachment: ...]`.
   - **Do Not Repeat**: Never emit unescaped `[...]` literal status brackets to `stdout_console`; `test_04:1038-1069` explicitly guards against this regression.

9. **100% Surface Audit & Exact Count Assertions (`tests/test_08_bidirectional_cli_ui_parity_and_100pct_gate.py:1385-1457`)**:
   - `run_all.py` and `test_08` enforce **100% dynamic execution** of every `/api/v1/*` route (`67`), CLI leaf command (`72`), CLI canonical flag (`90`), UI view (`8`), and UI modal (`10`), plus `0` orphan SPA endpoints and `0` SPA endpoints lacking a CLI counterpart in `itrack.py`.
   - **Do Not Repeat**: Never add an API route, CLI flag/subcommand, `data-view`, or `data-modal` without wiring CLI + SPA parity, exercising it in tests, and updating the exact count assertions in `test_08:1412-1418`.

10. **POSIX `fcntl.flock` Requirement (`itrack.py:8, 355, 545, 1089`, `itrack-server.py:505`)**:
    - Daemon spawn serialization (`.runtime/daemon-spawn.lock`), storage mutation locking (`.runtime/storage.lock`), and surface telemetry logging (`ITRACK_SURFACE_LOG`) rely on POSIX `fcntl.flock`. Run on Linux, macOS, or WSL2 (not native Windows `cmd.exe`/`PowerShell`).
