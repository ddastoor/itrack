---
name: swarm
description: 'UNIVERSAL DEFAULT FOR EVERY PROMPT IN ITRACK: /swarm mode is the default execution mode for every single prompt so all work runs in background subagents, immediate mid-flight prompts never stop prior work, and unfinished tasks automatically resume.'
---
# `/swarm` Mode — Universal Default & Auto-Resume for `itrack`

In `ddastoor/itrack`, `/swarm` (background subagent orchestration) is the **default execution mode for every single prompt** unless explicitly overridden by the user.

## 1. Zero-Loss Immediate-Prompt Protocol
1. **Always Delegate Execution to Background Subagents**: Use `invoke_subagent` (`Model: "inherit"`, `Workspace: "inherit"`) for all edits, commands, research, and tests (including small single-track tasks) so the main agent stays non-blocking and mid-flight **"Run Immediately"** prompts never interrupt active work.
2. **Never Stop Prior Work**: When an immediate prompt arrives mid-flight, keep all existing non-conflicting subagents and background tasks running. Only redirect (`send_message`) or terminate (`manage_subagents` `kill`) a worker if the new prompt contradicts its task.
3. **Automatic Resume Without Prompting**: Process the new prompt AND automatically pick up and finish all prior in-progress tracks across turns and reactive wakeups—even when the user does not append `"then continue"`.

## 2. `itrack` Disjoint Swarm Boundaries
- **Strict File Ownership**: Never assign overlapping write targets across concurrent workers (`itrack-server.py`, `itrack.py`, and distinct `tests/test_*.py` files).
- **Isolated Test Daemons**: Each concurrent test worker uses an isolated temporary `git init` workspace so per-repo `itrack-issues/.runtime/daemon.json` ephemeral ports never collide.
- **UI ↔ CLI Parity**: Verify new or modified functionality across CLI (`--json` + Rich), REST/multipart API, and headless Chrome Playwright tests (`tests/test_ui_cli_parity.py`).
