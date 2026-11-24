"""Comprehensive CLI <-> Server 100% Parity Tests (Rich Human TUI + `--json` Mode, In-Process & Subprocess)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from rich.console import Console

sys.dont_write_bytecode = True

REPO_ROOT_PATH = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_PATH))

from tests.helpers import (  # noqa: E402
    ITRACK_PY,
    IsolatedRepoTestCase,
    init_git_repo,
    load_module_from_path,
)

ITRACK_CLI_MOD = load_module_from_path("itrack_cli", ITRACK_PY)


class InProcessCLIMixin:
    """Helper mixin executing `itrack.py` in-process against `self.repo_dir` for speed and full coverage."""

    repo_dir: Path

    def run_inproc(self, *args: str, expect_rc: int = 0) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        prev_out = ITRACK_CLI_MOD.stdout_console
        prev_err = ITRACK_CLI_MOD.stderr_console
        ITRACK_CLI_MOD.stdout_console = Console(file=out_buf, force_terminal=False, width=130, highlight=False)
        ITRACK_CLI_MOD.stderr_console = Console(file=err_buf, stderr=True, force_terminal=False, width=130, highlight=False)

        argv = ["--root", str(self.repo_dir), *args]
        rc = 0
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                try:
                    ITRACK_CLI_MOD.main(argv)
                except SystemExit as exc:
                    rc = int(exc.code) if isinstance(exc.code, int) else (1 if exc.code else 0)
        finally:
            ITRACK_CLI_MOD.stdout_console = prev_out
            ITRACK_CLI_MOD.stderr_console = prev_err

        out_text = out_buf.getvalue()
        err_text = err_buf.getvalue()
        if expect_rc is not None and rc != expect_rc:
            raise AssertionError(
                f"Expected rc={expect_rc}, got rc={rc} for args={argv}\nSTDOUT:\n{out_text}\nSTDERR:\n{err_text}"
            )
        return rc, out_text, err_text

    def run_inproc_json(self, *args: str, expect_rc: int = 0) -> Any:
        final_args = list(args)
        if "--json" not in final_args:
            final_args.append("--json")
        _, out_text, err_text = self.run_inproc(*final_args, expect_rc=expect_rc)
        try:
            return json.loads(out_text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Invalid JSON for {final_args}: {exc}\nSTDOUT:\n{out_text}\nSTDERR:\n{err_text}") from exc


class TestSystemConfigWorkflowExportImportParity(IsolatedRepoTestCase, InProcessCLIMixin):
    """Tests for `init`, `config`, `workflow`, `stats`, `reindex`, `export`, and `import` in text and `--json`."""

    def test_init_config_workflow_stats_reindex_export_import(self) -> None:
        # 1. `init` in text & `--json`
        _, init_txt, _ = self.run_inproc("init", "--key", "E2E", "--name", "E2E Project")
        self.assertIn("Workspace Ready", init_txt)
        init_j = self.run_inproc_json("init", "--key", "E2E", "--name", "E2E Updated")
        self.assertEqual(init_j["config"]["project_key"], "E2E")
        self.assertEqual(init_j["config"]["project_name"], "E2E Updated")

        # Empty board, empty list, and empty graph renderers before any issues exist
        _, empty_list_txt, _ = self.run_inproc("list")
        self.assertIn("0 issues matched", empty_list_txt)
        _, empty_board_txt, _ = self.run_inproc("board")
        self.assertIn("Kanban Board", empty_board_txt)
        _, empty_graph_txt, _ = self.run_inproc("graph")
        self.assertIn("No explicit dependency links", empty_graph_txt)

        # 2. `config show` & `config set` (text & `--json` + invalid JSON error)
        _, cfg_show_txt, _ = self.run_inproc("config", "show")
        self.assertIn("Project Configuration", cfg_show_txt)
        cfg_show_j = self.run_inproc_json("config", "show")
        self.assertEqual(cfg_show_j["project_key"], "E2E")

        _, cfg_set_txt, _ = self.run_inproc(
            "config", "set", "--default-type", "bug", "--default-priority", "high", "--wip-limits", '{"in_progress": 1}'
        )
        self.assertIn("in_progress", cfg_set_txt)
        cfg_set_j = self.run_inproc_json("config", "set", "--default-type", "task", "--default-priority", "medium")
        self.assertEqual(cfg_set_j["default_type"], "task")

        bad_wip = self.run_inproc_json("config", "set", "--wip-limits", "{not-json", expect_rc=1)
        self.assertEqual(bad_wip["error"], "invalid_json")

        # 3. `workflow show` & `workflow set` (inline JSON + file path + disallowed transition + `--force`)
        _, wf_show_txt, _ = self.run_inproc("workflow", "show")
        self.assertIn("Workflow State Machine", wf_show_txt)
        orig_wf = self.run_inproc_json("workflow", "show")
        self.assertIn("todo", orig_wf["statuses"])

        strict_wf = {
            "statuses": ["backlog", "todo", "in_progress", "done"],
            "transitions": {
                "backlog": ["todo"],
                "todo": ["in_progress"],
                "in_progress": ["done"],
                "done": ["todo"],
            },
        }
        compact_inline_wf = json.dumps(strict_wf, separators=(",", ":"))
        _, wf_set_txt, _ = self.run_inproc("workflow", "set", "--file", compact_inline_wf)
        self.assertIn("Workflow State Machine", wf_set_txt)

        iss1 = self.run_inproc_json("create", "Workflow Guard Test", "-p", "high", "--points", "3")
        self.assertEqual(iss1["key"], "E2E-1")

        # Direct `todo` -> `done` is disallowed without `--force`
        _, _, err_disallowed = self.run_inproc("transition", "E2E-1", "done", expect_rc=1)
        self.assertIn("Invalid workflow transition", err_disallowed)
        err_disallowed_j = self.run_inproc_json("transition", "E2E-1", "done", expect_rc=1)
        self.assertEqual(err_disallowed_j["status_code"], 400)

        # `--force` overrides workflow guard cleanly
        forced = self.run_inproc_json("transition", "E2E-1", "done", "--force", "--resolution", "fixed", "-m", "Forced")
        self.assertEqual(forced["status"], "done")

        # Restore original workflow via JSON file path
        orig_wf_file = self.repo_dir / "orig_workflow.json"
        orig_wf_file.write_text(json.dumps(orig_wf), encoding="utf-8")
        restored_wf = self.run_inproc_json("workflow", "set", "--file", str(orig_wf_file))
        self.assertEqual(restored_wf["statuses"], orig_wf["statuses"])

        # 4. `stats` & direct disk edit + `reindex`
        _, stats_txt, _ = self.run_inproc("stats")
        self.assertIn("Workspace Statistics", stats_txt)
        stats_j = self.run_inproc_json("stats")
        self.assertEqual(stats_j["total_issues"], 1)

        issue_disk_path = self.repo_dir / "itrack-issues" / "issues" / "E2E-1.json"
        raw_doc = json.loads(issue_disk_path.read_text(encoding="utf-8"))
        raw_doc["title"] = "Disk Edited Title UniqueTokenAlpha99"
        issue_disk_path.write_text(json.dumps(raw_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        _, reidx_txt, _ = self.run_inproc("reindex")
        self.assertIn("Reindexed", reidx_txt)
        reidx_j = self.run_inproc_json("reindex")
        self.assertEqual(reidx_j["indexed_issues"], 1)

        srch = self.run_inproc_json("search", "UniqueTokenAlpha99")
        self.assertEqual([i["key"] for i in srch["issues"]], ["E2E-1"])

        # 5. `export` (stdout + `-o` file) & `import` (`merge` + `replace`) via real subprocess + in-process
        raw_export = self.cli_json("export")
        self.assertEqual(len(raw_export["issues"]), 1)

        bundle_file = self.repo_dir / "backup_bundle.json"
        _, exp_txt, _ = self.run_inproc("export", "-o", str(bundle_file))
        self.assertIn("Exported workspace", exp_txt)
        exp_j = self.run_inproc_json("export", "-o", str(bundle_file))
        self.assertEqual(exp_j["issue_count"], 1)

        # Delete E2E-1, create E2E-2, then `import --mode merge` keeps E2E-2 and restores E2E-1
        self.run_inproc_json("issue", "delete", "E2E-1")
        iss2 = self.run_inproc_json("create", "Second Issue Kept Under Merge")
        self.assertEqual(iss2["key"], "E2E-2")

        _, imp_merge_txt, _ = self.run_inproc("import", str(bundle_file), "--mode", "merge")
        self.assertIn("Imported workspace", imp_merge_txt)
        after_merge_keys = {i["key"] for i in self.run_inproc_json("list")["issues"]}
        self.assertEqual(after_merge_keys, {"E2E-1", "E2E-2"})

        # `import --mode replace` replaces workspace back to strictly the bundle's E2E-1
        imp_rep_j = self.run_inproc_json("import", str(bundle_file), "--mode", "replace")
        self.assertEqual(imp_rep_j["imported_issues"], 1)
        after_rep_keys = {i["key"] for i in self.run_inproc_json("list")["issues"]}
        self.assertEqual(after_rep_keys, {"E2E-1"})


class TestFullResourcesAndShortcutsParity(IsolatedRepoTestCase, InProcessCLIMixin):
    """End-to-end parity across users, labels, components, epics, milestones, sprints, issues, links, claims, comments, worklogs, attachments, iQL, search, views, board, and graph."""

    def test_agile_resources_users_labels_components_epics_milestones_sprints(self) -> None:
        self.run_inproc_json("init", "--key", "E2E", "--name", "Parity Suite")

        # --- Users (human & `--agent`) ---
        _, u_add_txt, _ = self.run_inproc("user", "add", "alice", "--name", "Alice Eng", "--email", "a@x.dev", "--role", "lead")
        self.assertIn("alice", u_add_txt)
        bot = self.run_inproc_json("user", "add", "agent-x", "--name", "Agent X", "--role", "coder", "--agent", "--avatar-color", "#10b981")
        self.assertTrue(bot["is_agent"])

        _, u_list_txt, _ = self.run_inproc("user", "list")
        self.assertIn("agent-x", u_list_txt)
        u_list_j = self.run_inproc_json("user", "list")
        self.assertEqual(len(u_list_j["users"]), 2)

        _, u_show_txt, _ = self.run_inproc("user", "show", "agent-x")
        self.assertIn("Agent", u_show_txt)
        u_show_j = self.run_inproc_json("user", "show", "agent-x")
        self.assertTrue(u_show_j["user"]["is_agent"])

        _, u_upd_txt, _ = self.run_inproc("user", "update", "alice", "--role", "staff", "--agent")
        self.assertIn("Updated user", u_upd_txt)
        u_upd_j = self.run_inproc_json("user", "update", "alice", "--human")
        self.assertFalse(u_upd_j["is_agent"])

        self.run_inproc_json("user", "add", "temp-u")
        _, u_del_txt, _ = self.run_inproc("user", "delete", "temp-u")
        self.assertIn("Deleted user", u_del_txt)
        self.run_inproc_json("user", "add", "temp-u2")
        self.assertTrue(self.run_inproc_json("user", "delete", "temp-u2")["deleted"])

        # --- Labels & Components ---
        _, lb_txt, _ = self.run_inproc("label", "add", "auth", "--color", "#ef4444", "-d", "Authentication")
        self.assertIn("auth", lb_txt)
        self.run_inproc_json("label", "add", "backend", "--color", "#3b82f6")
        self.run_inproc_json("label", "add", "obsolete")
        _, lb_list_txt, _ = self.run_inproc("label", "list")
        self.assertIn("auth", lb_list_txt)
        self.assertEqual(len(self.run_inproc_json("label", "list")["labels"]), 3)
        _, lb_del_txt, _ = self.run_inproc("label", "delete", "obsolete")
        self.assertIn("Deleted label", lb_del_txt)

        _, cp_txt, _ = self.run_inproc("component", "add", "gateway", "--lead", "alice", "-d", "API Gateway")
        self.assertIn("gateway", cp_txt)
        self.run_inproc_json("component", "add", "legacy-comp")
        _, cp_list_txt, _ = self.run_inproc("component", "list")
        self.assertIn("gateway", cp_list_txt)
        self.assertEqual(len(self.run_inproc_json("component", "list")["components"]), 2)
        self.assertTrue(self.run_inproc_json("component", "delete", "legacy-comp")["deleted"])

        # --- Epics & Milestones ---
        _, ep_add_txt, _ = self.run_inproc("epic", "create", "Zero Trust Epic", "-d", "Security roadmap", "-p", "high", "-a", "alice")
        self.assertIn("E2E-1", ep_add_txt)
        ep2 = self.run_inproc_json("epic", "create", "Observability Epic", "-p", "medium")
        self.assertEqual(ep2["type"], "epic")

        _, ms_add_txt, _ = self.run_inproc("milestone", "create", "v1.0", "-d", "GA Release", "--target", "2026-12-31")
        self.assertIn("MIL-1", ms_add_txt)
        ms2 = self.run_inproc_json("milestone", "create", "v1.1", "--target", "2027-03-01")
        _, ms_upd_txt, _ = self.run_inproc("milestone", "update", "MIL-1", "--status", "released", "--name", "v1.0.0")
        self.assertIn("MIL-1", ms_upd_txt)
        ms_upd_j = self.run_inproc_json("milestone", "update", "mil-1", "-d", "Shipped")
        self.assertEqual(ms_upd_j["status"], "released")
        _, ms_list_txt, _ = self.run_inproc("milestone", "list")
        self.assertIn("v1.0.0", ms_list_txt)
        self.assertEqual(len(self.run_inproc_json("milestone", "list")["milestones"]), 2)
        _, ms_del_txt, _ = self.run_inproc("milestone", "delete", ms2["id"])
        self.assertIn("Deleted milestone", ms_del_txt)

        # --- Sprints & Rollover ---
        _, sp_add_txt, _ = self.run_inproc("sprint", "create", "Sprint Alpha", "--goal", "Auth MVP", "--start", "2026-11-01", "--end", "2026-11-15")
        self.assertIn("SPR-1", sp_add_txt)
        sp2 = self.run_inproc_json("sprint", "create", "Sprint Beta")
        _, sp_upd_txt, _ = self.run_inproc("sprint", "update", "SPR-1", "--goal", "Updated Auth MVP")
        self.assertIn("SPR-1", sp_upd_txt)
        _, sp_start_txt, _ = self.run_inproc("sprint", "start", "SPR-1")
        self.assertIn("Started sprint", sp_start_txt)

        # Populate sprint with 1 done issue and 1 unfinished issue under Epic E2E-1
        done_iss = self.run_inproc_json(
            "create", "Completed Sprint Story", "-t", "story", "-p", "high", "--epic", "E2E-1", "--sprint", "SPR-1", "--milestone", "MIL-1", "--points", "5"
        )
        open_iss = self.run_inproc_json(
            "issue", "create", "Unfinished Sprint Task", "-t", "task", "-p", "medium", "--epic", "E2E-1", "--sprint", "SPR-1", "--points", "3"
        )
        _, close_txt, _ = self.run_inproc("close", done_iss["key"], "--resolution", "fixed", "-m", "Done in Sprint Alpha")
        self.assertIn("Transitioned", close_txt)

        # Epic rollup check (1 of 2 children done -> 50%)
        _, ep_list_txt, _ = self.run_inproc("epic", "list")
        self.assertIn("E2E-1", ep_list_txt)
        ep_list_j = self.run_inproc_json("epic", "list")
        ep1_entry = next(e for e in ep_list_j["epics"] if e["issue"]["key"] == "E2E-1")
        self.assertEqual(ep1_entry["done_count"], 1)
        self.assertEqual(ep1_entry["child_count"], 2)

        # Sprint show & list (text & `--json`)
        _, sp_list_txt, _ = self.run_inproc("sprint", "list", "--state", "active")
        self.assertIn("Sprint Alpha", sp_list_txt)
        _, sp_show_txt, _ = self.run_inproc("sprint", "show", "SPR-1")
        self.assertIn("Sprint Overview", sp_show_txt)
        sp_show_j = self.run_inproc_json("sprint", "show", "spr-1")
        self.assertEqual(sp_show_j["metrics"]["done_issues"], 1)
        self.assertEqual(sp_show_j["metrics"]["total_issues"], 2)

        # Complete SPR-1 with `--rollover backlog`: unfinished rolls over, done stays in SPR-1!
        sp_comp_j = self.run_inproc_json("sprint", "complete", "SPR-1", "--rollover", "backlog")
        self.assertEqual(sp_comp_j["rolled_over_keys"], [open_iss["key"]])
        self.assertEqual(self.run_inproc_json("show", done_iss["key"])["sprint_id"], "SPR-1")
        self.assertIsNone(self.run_inproc_json("show", open_iss["key"])["sprint_id"])

        # Delete unused Sprint Beta
        _, sp_del_txt, _ = self.run_inproc("sprint", "delete", sp2["id"])
        self.assertIn("Deleted sprint", sp_del_txt)

    def test_issues_links_claims_comments_worklogs_attachments_iql_views_board_graph(self) -> None:
        self.run_inproc_json("init", "--key", "E2E", "--name", "Deep Issue Parity")
        self.run_inproc_json("config", "set", "--wip-limits", '{"in_progress": 1}')

        # Create E2E-1, E2E-2, E2E-3
        _, c1_txt, _ = self.run_inproc(
            "create", "OAuth2 Token Verification",
            "-d", "Verify JWT token signature and claims",
            "-t", "story", "-p", "urgent", "-a", "alice",
            "-l", "auth,backend", "-c", "api", "--points", "8", "--due", "2026-12-20", "--environment", "prod",
        )
        self.assertIn("E2E-1", c1_txt)
        i2 = self.run_inproc_json("issue", "create", "Refresh Token Rotation", "-d", "Rotate token", "-p", "high", "-l", "auth", "--points", "5")
        i3 = self.run_inproc_json("create", "Session Revocation Endpoint", "-p", "medium", "-l", "backend", "--points", "2")
        self.assertEqual((i2["key"], i3["key"]), ("E2E-2", "E2E-3"))

        # --- Update with lowercase key + atomic `--add-label` / `--remove-label` ---
        _, upd_txt, _ = self.run_inproc("issue", "update", "e2e-3", "--title", "Session Revocation API", "--add-label", "auth,security", "--remove-label", "backend", "-p", "high")
        self.assertIn("E2E-3", upd_txt)
        e2e3 = self.run_inproc_json("issue", "show", "e2e-3")
        self.assertEqual(set(e2e3["labels"]), {"auth", "security"})
        self.assertEqual(e2e3["title"], "Session Revocation API")

        # --- Subtasks & Clone (`--include-subtasks`) ---
        _, sub_txt, _ = self.run_inproc("issue", "subtask-add", "E2E-1", "Write JWKS cache tests", "-p", "high", "--points", "2")
        self.assertIn("E2E-4", sub_txt)
        _, subs_list_txt, _ = self.run_inproc("issue", "subtasks", "E2E-1")
        self.assertIn("E2E-4", subs_list_txt)
        self.assertEqual(self.run_inproc_json("issue", "subtasks", "E2E-1")["total_count"], 1)

        _, clone_txt, _ = self.run_inproc("issue", "clone", "E2E-1", "--include-subtasks", "--title-prefix", "[Copy] ")
        self.assertIn("E2E-5", clone_txt)
        cloned_subs = self.run_inproc_json("issue", "subtasks", "E2E-5")
        self.assertEqual(cloned_subs["total_count"], 1)

        # --- Assign, Watch/Unwatch, Transition/Move/Close/Reopen, History ---
        _, asn_txt, _ = self.run_inproc("issue", "assign", "E2E-5", "bob")
        self.assertIn("bob", asn_txt)
        self.assertIsNone(self.run_inproc_json("issue", "assign", "E2E-5", "none")["assignee"])

        _, w_txt, _ = self.run_inproc("issue", "watch", "E2E-1", "alice")
        self.assertIn("alice", w_txt)
        self.assertIn("bob", self.run_inproc_json("issue", "watch", "E2E-1", "bob")["watchers"])
        _, uw_txt, _ = self.run_inproc("issue", "unwatch", "E2E-1", "bob")
        self.assertIn("Removed watcher", uw_txt)

        _, mv_txt, _ = self.run_inproc("move", "E2E-5", "in_progress", "-m", "Working")
        self.assertIn("in_progress", mv_txt)
        self.assertEqual(self.run_inproc_json("close", "E2E-5")["status"], "done")
        _, reop_txt, _ = self.run_inproc("reopen", "E2E-5")
        self.assertIn("todo", reop_txt)

        _, hist_txt, _ = self.run_inproc("issue", "history", "E2E-5")
        self.assertIn("History for E2E-5", hist_txt)
        self.assertGreaterEqual(len(self.run_inproc_json("issue", "history", "E2E-5")["history"]), 4)

        # --- Bulk actions (update, label_add, label_remove, transition, delete + error item) ---
        clone_sub_key = cloned_subs["subtasks"][0]["key"]
        bulk_pair = f"E2E-5,{clone_sub_key}"
        self.assertEqual(len(self.run_inproc_json("issue", "bulk", "--keys", bulk_pair, "--action", "update", "-p", "low")["updated"]), 2)
        self.assertEqual(len(self.run_inproc_json("issue", "bulk", "--keys", bulk_pair, "--action", "label_add", "-l", "temp-bulk")["updated"]), 2)
        self.assertEqual(len(self.run_inproc_json("issue", "bulk", "--keys", bulk_pair, "--action", "label_remove", "-l", "temp-bulk")["updated"]), 2)
        _, bulk_tr_txt, _ = self.run_inproc("issue", "bulk", "--keys", f"{bulk_pair},E2E-999", "--action", "transition", "-s", "done")
        self.assertIn("updated=2", bulk_tr_txt)
        bulk_del = self.run_inproc_json("issue", "bulk", "--keys", bulk_pair, "--action", "delete")
        self.assertEqual(len(bulk_del["deleted"]), 2)

        # Standalone `issue delete` in text mode
        tmp_del = self.run_inproc_json("create", "Temp Delete Me")
        _, del_txt, _ = self.run_inproc("issue", "delete", tmp_del["key"])
        self.assertIn("Deleted", del_txt)

        # --- Links, DAG Cycle 409 & `ready` Queue ---
        _, lnk1_txt, _ = self.run_inproc("link", "add", "E2E-1", "blocks", "E2E-2")
        self.assertIn("blocks", lnk1_txt)
        lnk2 = self.run_inproc_json("link", "add", "E2E-2", "blocks", "E2E-3")
        rel_lnk = self.run_inproc_json("link", "add", "E2E-1", "relates_to", "E2E-3")

        # Cycle E2E-3 -> E2E-1 MUST fail with 409!
        cyc_err = self.run_inproc_json("link", "add", "E2E-3", "blocks", "E2E-1", expect_rc=1)
        self.assertEqual(cyc_err["status_code"], 409)
        self.assertIn("cycle", cyc_err["detail"].lower())

        _, lnk_list_txt, _ = self.run_inproc("link", "list", "E2E-1")
        self.assertIn("E2E-2", lnk_list_txt)
        self.assertEqual(len(self.run_inproc_json("link", "list", "E2E-1")["links"]), 2)

        # Ready queue excludes E2E-2 & E2E-3 while E2E-1 is open
        _, rdy_txt, _ = self.run_inproc("ready")
        self.assertIn("E2E-1", rdy_txt)
        rdy_keys = {i["key"] for i in self.run_inproc_json("ready")["issues"]}
        self.assertIn("E2E-1", rdy_keys)
        self.assertNotIn("E2E-2", rdy_keys)
        self.assertNotIn("E2E-3", rdy_keys)

        # Remove `relates_to` link via `link remove`
        _, rm_lnk_txt, _ = self.run_inproc("link", "remove", "E2E-1", rel_lnk["id"])
        self.assertIn("Removed link", rm_lnk_txt)
        self.assertTrue(lnk2["id"].startswith("LNK-"))

        # --- Atomic Claim & Release ---
        _, clm_txt, _ = self.run_inproc("claim", "E2E-2", "--agent", "agent-1", "-m", "Claiming E2E-2")
        self.assertIn("agent-1", clm_txt)
        conflict_clm = self.run_inproc_json("claim", "E2E-2", "--agent", "agent-2", expect_rc=1)
        self.assertEqual(conflict_clm["status_code"], 409)
        override_clm = self.run_inproc_json("issue", "claim", "E2E-2", "--agent", "agent-2", "--force")
        self.assertEqual(override_clm["claimed_by"], "agent-2")

        wrong_rel = self.run_inproc_json("release", "E2E-2", "--agent", "agent-1", expect_rc=1)
        self.assertEqual(wrong_rel["status_code"], 409)
        _, rel_txt, _ = self.run_inproc("release", "E2E-2", "--agent", "agent-2", "-s", "todo", "-m", "Handing back")
        self.assertIn("Released", rel_txt)

        # --- Comments & Worklogs ---
        cmt = self.run_inproc_json("comment", "add", "E2E-1", "Initial comment with token keyword", "--author", "alice")
        _, cmt_edit_txt, _ = self.run_inproc("comment", "edit", "E2E-1", cmt["id"], "Edited comment with token keyword")
        self.assertIn("Updated comment", cmt_edit_txt)
        _, cmt_list_txt, _ = self.run_inproc("comment", "list", "E2E-1")
        self.assertIn("Edited comment", cmt_list_txt)
        cmt_tmp = self.run_inproc_json("comment", "add", "E2E-1", "Delete me")
        _, cmt_del_txt, _ = self.run_inproc("comment", "delete", "E2E-1", cmt_tmp["id"])
        self.assertIn("Deleted comment", cmt_del_txt)

        wl1 = self.run_inproc_json("worklog", "add", "E2E-1", "1h 45m", "-m", "Auth implementation", "--author", "alice")
        self.assertEqual(wl1["minutes"], 105)
        _, wl2_txt, _ = self.run_inproc("worklog", "add", "E2E-1", "15", "-m", "Quick cleanup")
        self.assertIn("15m", wl2_txt)
        _, wl_list_txt, _ = self.run_inproc("worklog", "list", "E2E-1")
        self.assertIn("2h", wl_list_txt)
        wl_list_j = self.run_inproc_json("worklog", "list", "E2E-1")
        self.assertEqual(wl_list_j["total_minutes"], 120)
        wl_last_id = wl_list_j["worklogs"][-1]["id"]
        _, wl_del_txt, _ = self.run_inproc("worklog", "delete", "E2E-1", wl_last_id)
        self.assertIn("Deleted worklog", wl_del_txt)

        # --- Multipart Binary Attachments (`attach` & `attachment` alias) ---
        raw_payload = os.urandom(65536)
        expected_sha = hashlib.sha256(raw_payload).hexdigest()
        src_bin = self.repo_dir / "payload_64kb.bin"
        dst_bin = self.repo_dir / "downloaded_64kb.bin"
        src_bin.write_bytes(raw_payload)

        att_up = self.cli_json("attach", "upload", "E2E-1", str(src_bin), "--by", "alice", "-d", "Binary artifact")
        self.assertEqual(att_up["size_bytes"], 65536)
        self.assertEqual(att_up["sha256"], expected_sha)

        _, att_list_txt, _ = self.run_inproc("attachment", "list", "E2E-1")
        self.assertIn("payload_64kb.bin", att_list_txt)
        self.assertEqual(len(self.run_inproc_json("attach", "list", "E2E-1")["attachments"]), 1)

        # Rich `show` now exercises description, subtasks, links, attachments, worklogs, and comments all at once!
        _, rich_show_e2e1, _ = self.run_inproc("show", "E2E-1")
        self.assertIn("Attachments", rich_show_e2e1)
        self.assertIn("Worklogs", rich_show_e2e1)
        self.assertIn("Comments", rich_show_e2e1)
        _, rich_show_blocked, _ = self.run_inproc("show", "E2E-2")
        self.assertIn("BLOCKED", rich_show_blocked)

        _, dl_txt, _ = self.run_inproc("attach", "download", "E2E-1", att_up["id"].lower(), "-o", str(dst_bin))
        self.assertIn("Downloaded", dl_txt)
        self.assertEqual(hashlib.sha256(dst_bin.read_bytes()).hexdigest(), expected_sha)

        _, att_del_txt, _ = self.run_inproc("attach", "delete", "E2E-1", att_up["id"])
        self.assertIn("Deleted attachment", att_del_txt)
        self.assertEqual(len(self.run_inproc_json("attach", "list", "E2E-1")["attachments"]), 0)

        # --- Search, iQL (query + `--validate` valid & invalid exit code 1 in BOTH text & `--json`) & Saved Views ---
        _, srch_txt, _ = self.run_inproc("search", "signature")
        self.assertIn("E2E-1", srch_txt)
        srch_iql = self.run_inproc_json("search", "label = auth AND points >= 5", "--iql")
        self.assertEqual({i["key"] for i in srch_iql["issues"]}, {"E2E-1", "E2E-2"})

        _, iql_txt, _ = self.run_inproc("iql", "priority >= high AND status != done ORDER BY priority DESC")
        self.assertIn("E2E-1", iql_txt)
        iql_fts = self.run_inproc_json("iql", "text ~ 'token'")
        self.assertTrue({"E2E-1", "E2E-2"}.issubset({i["key"] for i in iql_fts["issues"]}))

        # `--validate`: valid exits 0; invalid exits 1 in BOTH text and `--json`!
        _, val_ok_txt, _ = self.run_inproc("iql", "priority >= high AND blocked = false", "--validate", expect_rc=0)
        self.assertIn("Valid iQL", val_ok_txt)
        val_ok_j = self.run_inproc_json("iql", "priority >= high AND blocked = false", "--validate", expect_rc=0)
        self.assertTrue(val_ok_j["valid"])

        self.run_inproc("iql", "status === broken", "--validate", expect_rc=1)
        val_bad_j = self.run_inproc_json("iql", "status === broken", "--validate", expect_rc=1)
        self.assertFalse(val_bad_j["valid"])
        self.assertIsNotNone(val_bad_j["error"])

        # Virtual field edge cases tested in audit (`text IS EMPTY`, `comment IN (...)`, `blocked IS EMPTY`)
        self.assertGreaterEqual(self.run_inproc_json("iql", "text IS NOT EMPTY")["total"], 1)
        self.assertGreaterEqual(self.run_inproc_json("iql", "comment IN ('Edited comment with token keyword')")["total"], 0)
        self.run_inproc_json("iql", "blocked IS EMPTY", "--validate", expect_rc=1)

        # Saved Views CRUD
        _, vw_c_txt, _ = self.run_inproc("view", "create", "Urgent Auth", "--iql", "label = auth AND priority >= high", "--layout", "board", "-d", "Auth board")
        self.assertIn("VIEW-1", vw_c_txt)
        _, vw_l_txt, _ = self.run_inproc("view", "list")
        self.assertIn("Urgent Auth", vw_l_txt)
        vw_u_j = self.run_inproc_json("view", "update", "view-1", "--layout", "list", "--name", "Urgent Auth List")
        self.assertEqual(vw_u_j["layout"], "list")
        _, vw_d_txt, _ = self.run_inproc("view", "delete", "VIEW-1")
        self.assertIn("Deleted view", vw_d_txt)

        # --- Board & Graph (Rich TUI + `--json`, including over-WIP column highlight) ---
        self.run_inproc_json("move", "E2E-1", "in_progress")
        self.run_inproc_json("move", "E2E-3", "in_progress")
        _, brd_txt, _ = self.run_inproc("board")
        self.assertIn("IN_PROGRESS", brd_txt)
        brd_j = self.run_inproc_json("board")
        self.assertGreaterEqual(brd_j["total_issues"], 4)

        _, grp_txt, _ = self.run_inproc("graph")
        self.assertIn("Dependency Graph", grp_txt)
        grp_j = self.run_inproc_json("graph")
        self.assertIn("E2E-2", grp_j["blocked_keys"])


if __name__ == "__main__":
    unittest.main()
