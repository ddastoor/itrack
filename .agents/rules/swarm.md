---
trigger: always_on
description: Default /swarm multi-agent execution mode for the itrack project
---
# Project Rule: Default `/swarm` Mode (`itrack`)

- **Default `/swarm` Mode**: Across the `itrack` repository, `/swarm` mode (multi-agent execution via parallel subagents or `teamwork` for large multi-track efforts) is the **default agent mode** for all **medium**, **difficult**, and **large** tasks unless the user explicitly asks to work single-threaded.
- **Disjoint File Ownership**:
  - Assign strictly non-overlapping write targets across parallel workers (for example, `itrack-server.py`, `itrack.py`, and distinct `tests/test_*.py` modules).
  - Run isolated verification suites in temporary Git workspaces (or via `pytest` / `unittest` fixtures) so concurrent workers never collide on a shared `itrack-issues/.runtime/daemon.json`.
- **UI ↔ CLI Feature Parity**: Whenever adding or modifying features in `itrack-server.py` (REST/multipart API or embedded offline SPA) or `itrack.py` (CLI + `--json`), verify bidirectional parity across CLI, API, and headless browser UI tests in `tests/`.
