---
name: swarm
description: 'PROJECT DEFAULT FOR ITRACK: /swarm mode is the default execution mode for all medium, difficult, and large tasks in itrack. Always follow this skill for multi-file edits, API/CLI/UI parity work, complex debugging, refactors, and test suite expansion.'
---
# `/swarm` Mode — Default Agent Mode for `itrack`

In `ddastoor/itrack`, `/swarm` (parallel multi-agent orchestration) is the **default execution mode** for all moderate, difficult, and large tasks unless explicitly overridden by the user.

## 1. Complexity Routing
- **Small / Trivial** (quick question, single-line tweak): Execute directly in the main agent.
- **Medium & Difficult** (changes spanning `itrack-server.py`, `itrack.py`, and/or `tests/`, iQL compiler changes, SPA views, concurrency/locking debugging, UI ↔ CLI parity verification): Spawn **parallel subagents** via `invoke_subagent` (`Model: "inherit"`, `Workspace: "inherit"`).
- **Large Multi-Milestone Efforts**: Orchestrate via `teamwork` (`invoke_subagent` with `TypeName: "teamwork"`).

## 2. `itrack` Disjoint Swarm Boundaries
1. **Contract First**: Agree on `/api/v1/*` schemas, iQL semantics, and CLI flags before launching parallel writers.
2. **Strict File Ownership**:
   - Backend / Storage / iQL / Offline SPA worker owns `itrack-server.py`.
   - CLI / Agent Manual / Auto-Daemon worker owns `itrack.py`.
   - Test & Verification workers own distinct files under `tests/` (`test_laws_and_daemon.py`, `test_storage_iql_and_api.py`, `test_cli_parity.py`, `test_ui_cli_parity.py`).
3. **Isolated Test Daemons**: Each concurrent verification worker creates its own temporary Git-initialized directory (`tempfile.TemporaryDirectory()` + `git init`) so per-repo `itrack-issues/.runtime/daemon.json` instances bind independent ephemeral ports on `127.0.0.1` without interference.
4. **Independent Verification**: Follow implementation passes with independent CLI + headless Chrome Playwright verification checking zero external network requests, zero console/page errors, and 100% UI ↔ CLI parity.
