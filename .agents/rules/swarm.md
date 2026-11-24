---
trigger: always_on
description: Universal default /swarm mode for every prompt in itrack with non-interruption and automatic resume of prior work
---
# Project Rule: Universal `/swarm` Mode & Auto-Resume (`itrack`)

- **Default `/swarm` Mode on Every Prompt**: Across the `itrack` repository, `/swarm` mode (background subagent execution via `invoke_subagent`, or `teamwork` for large multi-milestone efforts) is the **default agent mode for every single prompt**—regardless of task size—unless the user explicitly asks to work single-threaded.
- **Immediate Prompts Never Stop Prior Work**:
  1. Keep the main agent as a lightweight orchestrator and delegate all code edits, CLI/API runs, builds, and tests to background subagents (`Model: "inherit"`, `Workspace: "inherit"`) so mid-flight **"Run Immediately"** prompts never cancel active work.
  2. Whenever a new prompt arrives while previous work is in progress, **never** kill or abandon running subagents/tasks unless the new prompt contradicts them (in which case redirect via `send_message` or kill & reconcile conflicting edits cleanly).
  3. **Always Pick Up & Resume Automatically**: Handle the new prompt AND automatically resume and complete all unfinished work from earlier prompts without requiring the user to say `"continue"`.
- **Disjoint Ownership & Isolated Daemons**:
  - Assign strictly non-overlapping write targets across concurrent subagents (e.g., `itrack-server.py`, `itrack.py`, and separate `tests/test_*.py` modules).
  - Run concurrent verification workers in isolated temporary Git directories so per-repo `itrack-issues/.runtime/daemon.json` instances never collide.
- **UI ↔ CLI Feature Parity**: Whenever adding or modifying features in `itrack-server.py` or `itrack.py`, maintain bidirectional parity across CLI, API, and Playwright browser tests in `tests/`.
