"""Suite 02: Complete Issue CRUD — Every Enum, Every Mutable Field (Set, Edit, Clear), Validation, Lifecycle & Git Cleanliness."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

from helpers import (
    CATEGORIES,
    CLIResult,
    PRIORITIES,
    PRIORITY_RANKS,
    RESOLUTIONS,
    SEVERITIES,
    SEVERITY_RANKS,
    STATES,
    ITrackWorkspace,
    assert_clean_repo_root,
    load_itrack_cli_module,
    make_pdf,
    make_png,
    make_svg,
    make_text_file,
    sha256_file,
)

_CLI_MOD = load_itrack_cli_module()
_CLI_MOD.stdout_console = Console(highlight=False, no_color=True, width=140)
_CLI_MOD.stderr_console = Console(stderr=True, highlight=False, no_color=True, width=140)


def _fcli(
    ws: ITrackWorkspace,
    *args: Any,
    stdin: str | None = None,
    expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
) -> CLIResult:
    """Execute `itrack.py`'s `main(argv)` against `ws.root` (talking over HTTP to the live `itrack-server.py` daemon)."""
    str_args = [str(a) for a in args]
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    prev_cwd = os.getcwd()
    prev_stdin = sys.stdin
    rc = 0
    try:
        os.chdir(ws.root)
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                _CLI_MOD.main(str_args)
            except SystemExit as exc:
                if exc.code is None:
                    rc = 0
                elif isinstance(exc.code, int):
                    rc = exc.code
                else:
                    rc = 1
    finally:
        sys.stdin = prev_stdin
        os.chdir(prev_cwd)

    res = CLIResult(
        args=str_args,
        returncode=rc,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
    )
    if expect_rc is not None:
        allowed = {expect_rc} if isinstance(expect_rc, int) else set(expect_rc)
        if res.returncode not in allowed:
            raise AssertionError(
                f"CLI command failed exit-code assertion!\n"
                f"  Args: {str_args}\n"
                f"  Expected RC: {sorted(allowed)}, Actual RC: {res.returncode}\n"
                f"  STDOUT:\n{res.stdout}\n"
                f"  STDERR:\n{res.stderr}"
            )
    return res


def _fcli_json(
    ws: ITrackWorkspace,
    *args: Any,
    stdin: str | None = None,
    expect_rc: int | tuple[int, ...] | list[int] | set[int] | None = 0,
) -> Any:
    str_args = [str(a) for a in args]
    if "--json" not in str_args:
        str_args.append("--json")
    return _fcli(ws, *str_args, stdin=stdin, expect_rc=expect_rc).json()


def _read_disk_issue(ws: ITrackWorkspace, issue_name: str) -> dict[str, Any]:
    """Read raw canonical on-disk `itrack-issues/issues/<name>/issue.json`."""
    issue_json = ws.issue_dir(issue_name) / "issue.json"
    assert issue_json.is_file(), f"Expected on-disk issue.json at {issue_json}"
    data = json.loads(issue_json.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _assert_error_both_modes(
    ws: ITrackWorkspace,
    *args: Any,
    expected_rc: int = 1,
    expected_substr: str | None = None,
    use_subprocess: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Verify CLI command fails with `expected_rc` and clear error in both human stderr and `--json` stdout."""
    if use_subprocess:
        human_res = ws.cli(*args, expect_rc=expected_rc)
        json_payload = ws.cli_json(*args, expect_rc=expected_rc)
    else:
        human_res = _fcli(ws, *args, expect_rc=expected_rc)
        json_payload = _fcli_json(ws, *args, expect_rc=expected_rc)

    err_text = human_res.stderr.strip()
    assert err_text, f"Expected non-empty human stderr for {args}, got empty stderr (stdout={human_res.stdout!r})"

    assert isinstance(json_payload, dict)
    assert json_payload.get("exit_code") == expected_rc
    assert json_payload.get("error"), f"Expected non-empty 'error' code in {json_payload}"
    msg = str(json_payload.get("message") or "").strip()
    assert msg, f"Expected non-empty 'message' in {json_payload}"

    if expected_substr:
        low_sub = expected_substr.lower()
        assert low_sub in err_text.lower(), f"Expected {expected_substr!r} in stderr: {err_text!r}"
        assert low_sub in msg.lower(), f"Expected {expected_substr!r} in JSON message: {msg!r}"

    return err_text, json_payload


# ---------------------------------------------------------------------------
# 1. Exhaustive Enum Coverage (Categories, States, Spaced States, Priorities, Severities, Resolutions)
# ---------------------------------------------------------------------------


def test_02_exhaustive_categories_create_and_edit(workspace: ITrackWorkspace) -> None:
    """Create issues across all 13 categories and edit a single issue across all 13 categories."""
    ws = workspace
    assert len(CATEGORIES) == 13

    for cat in CATEGORIES:
        name = f"Category Create {cat}"
        created = _fcli_json(ws, "issue", "create", name, "--category", cat)
        assert created["category"] == cat
        assert created["name"] == name
        assert _read_disk_issue(ws, name)["category"] == cat
        assert _fcli_json(ws, "issue", "show", created["key"])["category"] == cat

    # Cycle a single issue across all 13 categories via `issue edit --category`
    pivot_name = "Category Morph Target"
    pivot = _fcli_json(ws, "issue", "create", pivot_name, "--category", "task")
    for cat in reversed(CATEGORIES):
        edited = _fcli_json(ws, "issue", "edit", pivot["key"], "--category", cat)
        assert edited["category"] == cat
        assert _read_disk_issue(ws, pivot_name)["category"] == cat
        assert _fcli_json(ws, "issue", "show", pivot_name)["category"] == cat


def test_02_exhaustive_workflow_states_hyphenated_and_spaced(workspace: ITrackWorkspace) -> None:
    """Test all 12 canonical workflow states on create, `issue state`, and `issue edit --state`, plus spaced state normalization."""
    ws = workspace
    assert len(STATES) == 12

    # (a) Create an issue directly in each of the 12 canonical states
    for st in STATES:
        name = f"Initial State {st}"
        created = _fcli_json(ws, "issue", "create", name, "--state", st)
        assert created["state"] == st
        assert created["archived"] is (st == "archived")
        assert _read_disk_issue(ws, name)["state"] == st
        assert _fcli_json(ws, "issue", "show", created["key"])["state"] == st

    # (b) Transition across all 12 states via `issue state <issue> <st>` (including `done --resolution fixed`)
    walker_name = "State Command Walker"
    walker = _fcli_json(ws, "issue", "create", walker_name, "--state", "triage")
    for st in STATES:
        if st == "done":
            updated = _fcli_json(ws, "issue", "state", walker["key"], "done", "--resolution", "fixed")
            assert updated["resolution"] == "fixed"
            assert _read_disk_issue(ws, walker_name)["resolution"] == "fixed"
        else:
            updated = _fcli_json(ws, "issue", "state", walker["key"], st)
        assert updated["state"] == st
        assert _read_disk_issue(ws, walker_name)["state"] == st
        assert _fcli_json(ws, "issue", "show", walker_name)["state"] == st

    # (c) Transition across all 12 states via `issue edit <issue> --state <st>`
    editor_name = "Edit Flag State Walker"
    editor = _fcli_json(ws, "issue", "create", editor_name, "--state", "backlog")
    for st in reversed(STATES):
        edited = _fcli_json(ws, "issue", "edit", editor["key"], "--state", st)
        assert edited["state"] == st
        assert _read_disk_issue(ws, editor_name)["state"] == st
        assert _fcli_json(ws, "issue", "show", editor_name)["state"] == st

    # (d) Explicitly verify spaced state inputs normalize to canonical hyphenated states
    spaced_pairs = [
        ("in progress", "in-progress"),
        ("in review", "in-review"),
        ("in qa", "in-qa"),
        ("on hold", "on-hold"),
    ]
    for idx, (spaced_in, expected_norm) in enumerate(spaced_pairs, 1):
        cname = f"Spaced Create {idx}"
        c_iss = _fcli_json(ws, "issue", "create", cname, "--state", spaced_in)
        assert c_iss["state"] == expected_norm
        assert _read_disk_issue(ws, cname)["state"] == expected_norm
        assert _fcli_json(ws, "issue", "show", c_iss["key"])["state"] == expected_norm

    spaced_target = _fcli_json(ws, "issue", "create", "Spaced Transition Target", "--state", "todo")
    for spaced_in, expected_norm in spaced_pairs:
        s_res = _fcli_json(ws, "issue", "state", spaced_target["key"], spaced_in)
        assert s_res["state"] == expected_norm
        assert _read_disk_issue(ws, "Spaced Transition Target")["state"] == expected_norm

        _fcli_json(ws, "issue", "state", spaced_target["key"], "todo")
        e_res = _fcli_json(ws, "issue", "edit", "Spaced Transition Target", "--state", spaced_in)
        assert e_res["state"] == expected_norm
        assert _read_disk_issue(ws, "Spaced Transition Target")["state"] == expected_norm
        assert _fcli_json(ws, "issue", "show", spaced_target["key"])["state"] == expected_norm


def test_02_exhaustive_priorities_severities_and_resolutions(workspace: ITrackWorkspace) -> None:
    """Verify all 5 priorities (+ ranks), all 5 severities (+ ranks), and all 6 resolutions (+ `--clear-resolution`)."""
    ws = workspace
    assert len(PRIORITIES) == 5
    assert len(SEVERITIES) == 5
    assert len(RESOLUTIONS) == 6

    # Priorities (create + edit)
    for prio in PRIORITIES:
        expected_rank = PRIORITY_RANKS[prio]
        name = f"Priority Create {prio}"
        created = _fcli_json(ws, "issue", "create", name, "--priority", prio)
        assert created["priority"] == prio and created["priority_rank"] == expected_rank
        disk = _read_disk_issue(ws, name)
        shown = _fcli_json(ws, "issue", "show", created["key"])
        assert disk["priority"] == prio and disk["priority_rank"] == expected_rank
        assert shown["priority"] == prio and shown["priority_rank"] == expected_rank

    prio_target = _fcli_json(ws, "issue", "create", "Priority Edit Target", "--priority", "trivial")
    for prio in PRIORITIES:
        expected_rank = PRIORITY_RANKS[prio]
        edited = _fcli_json(ws, "issue", "edit", prio_target["key"], "--priority", prio)
        assert edited["priority"] == prio and edited["priority_rank"] == expected_rank
        disk = _read_disk_issue(ws, "Priority Edit Target")
        shown = _fcli_json(ws, "issue", "show", prio_target["key"])
        assert disk["priority"] == prio and disk["priority_rank"] == expected_rank
        assert shown["priority"] == prio and shown["priority_rank"] == expected_rank

    # Severities (create + edit)
    for sev in SEVERITIES:
        expected_srank = SEVERITY_RANKS[sev]
        name = f"Severity Create {sev}"
        created = _fcli_json(ws, "issue", "create", name, "--severity", sev)
        assert created["severity"] == sev and created["severity_rank"] == expected_srank
        disk = _read_disk_issue(ws, name)
        shown = _fcli_json(ws, "issue", "show", created["key"])
        assert disk["severity"] == sev and disk["severity_rank"] == expected_srank
        assert shown["severity"] == sev and shown["severity_rank"] == expected_srank

    sev_target = _fcli_json(ws, "issue", "create", "Severity Edit Target", "--severity", "sev5")
    for sev in SEVERITIES:
        expected_srank = SEVERITY_RANKS[sev]
        edited = _fcli_json(ws, "issue", "edit", sev_target["key"], "--severity", sev)
        assert edited["severity"] == sev and edited["severity_rank"] == expected_srank
        disk = _read_disk_issue(ws, "Severity Edit Target")
        shown = _fcli_json(ws, "issue", "show", sev_target["key"])
        assert disk["severity"] == sev and disk["severity_rank"] == expected_srank
        assert shown["severity"] == sev and shown["severity_rank"] == expected_srank

    # Resolutions (create + edit + clear)
    for res_val in RESOLUTIONS:
        name = f"Resolution Create {res_val}"
        created = _fcli_json(ws, "issue", "create", name, "--state", "done", "--resolution", res_val)
        assert created["resolution"] == res_val
        assert _read_disk_issue(ws, name)["resolution"] == res_val
        assert _fcli_json(ws, "issue", "show", created["key"])["resolution"] == res_val

        cleared = _fcli_json(ws, "issue", "edit", created["key"], "--clear-resolution")
        assert cleared["resolution"] is None
        assert _read_disk_issue(ws, name)["resolution"] is None
        assert _fcli_json(ws, "issue", "show", created["key"])["resolution"] is None

    res_target = _fcli_json(ws, "issue", "create", "Resolution Cycle Target")
    assert res_target["resolution"] is None
    for res_val in RESOLUTIONS:
        edited = _fcli_json(ws, "issue", "edit", res_target["key"], "--resolution", res_val)
        assert edited["resolution"] == res_val
        assert _read_disk_issue(ws, "Resolution Cycle Target")["resolution"] == res_val
        assert _fcli_json(ws, "issue", "show", res_target["key"])["resolution"] == res_val

    cleared_final = _fcli_json(ws, "issue", "edit", res_target["key"], "--clear-resolution")
    assert cleared_final["resolution"] is None
    assert _read_disk_issue(ws, "Resolution Cycle Target")["resolution"] is None
    assert _fcli_json(ws, "issue", "show", res_target["key"])["resolution"] is None


# ---------------------------------------------------------------------------
# 2. Every Mutable Field — (a) Set at Creation, (b) Modify via `issue edit`, (c) Clear/Unset
# ---------------------------------------------------------------------------


def test_02_description_sources_embeds_and_clear(workspace: ITrackWorkspace, tmp_path: Path) -> None:
    """Test `--desc`, `--desc-file`, `--desc-stdin`, inline `--embed` / `--remove-embed`, and `--clear-desc`."""
    ws = workspace

    desc_file_1, _ = make_text_file(
        tmp_path / "assets" / "spec_v1.md",
        "# Gateway Spec v1\n\n- Supports mTLS\n- Enforces rate limits\n",
    )
    desc_file_2, _ = make_text_file(
        tmp_path / "assets" / "spec_v2.md",
        "# Gateway Spec v2\n\nUpdated via `--desc-file` on edit.\n",
    )
    png_file, png_sha = make_png(tmp_path / "assets" / "arch_flow.png", width=24, height=24)
    svg_file, svg_sha = make_svg(tmp_path / "assets" / "sequence_diag.svg", label="auth-seq")

    # (a) Create via --desc, --desc-file, and --desc-stdin (real OS pipe via `ws.cli_json`)
    i_inline = _fcli_json(ws, "issue", "create", "Desc Inline Issue", "--desc", "Initial **inline** markdown")
    assert i_inline["description"] == "Initial **inline** markdown"
    assert _read_disk_issue(ws, "Desc Inline Issue")["description"] == "Initial **inline** markdown"
    assert _fcli_json(ws, "issue", "show", i_inline["key"])["description"] == "Initial **inline** markdown"

    i_file = _fcli_json(ws, "issue", "create", "Desc File Issue", "--desc-file", str(desc_file_1))
    expected_f1 = desc_file_1.read_text(encoding="utf-8")
    assert i_file["description"] == expected_f1
    assert _read_disk_issue(ws, "Desc File Issue")["description"] == expected_f1
    assert _fcli_json(ws, "issue", "show", i_file["key"])["description"] == expected_f1

    stdin_create_body = "Created from stdin stream\nWith multiple lines & UTF-8: café"
    i_stdin = ws.cli_json("issue", "create", "Desc Stdin Issue", "--desc-stdin", stdin=stdin_create_body)
    assert i_stdin["description"] == stdin_create_body
    assert _read_disk_issue(ws, "Desc Stdin Issue")["description"] == stdin_create_body
    assert _fcli_json(ws, "issue", "show", i_stdin["key"])["description"] == stdin_create_body

    # (b) Modify via `issue edit --desc`, `--desc-file`, and `--desc-stdin` (real OS pipe via `ws.cli_json`)
    _fcli_json(ws, "issue", "edit", i_inline["key"], "--desc", "Replaced via --desc flag")
    assert _read_disk_issue(ws, "Desc Inline Issue")["description"] == "Replaced via --desc flag"
    assert _fcli_json(ws, "issue", "show", i_inline["key"])["description"] == "Replaced via --desc flag"

    _fcli_json(ws, "issue", "edit", i_inline["key"], "--desc-file", str(desc_file_2))
    expected_f2 = desc_file_2.read_text(encoding="utf-8")
    assert _read_disk_issue(ws, "Desc Inline Issue")["description"] == expected_f2
    assert _fcli_json(ws, "issue", "show", i_inline["key"])["description"] == expected_f2

    stdin_edit_body = "Replaced via --desc-stdin pipe\nSecond paragraph."
    ws.cli_json("issue", "edit", i_inline["key"], "--desc-stdin", stdin=stdin_edit_body)
    assert _read_disk_issue(ws, "Desc Inline Issue")["description"] == stdin_edit_body
    assert _fcli_json(ws, "issue", "show", i_inline["key"])["description"] == stdin_edit_body

    # (c) Clear via `issue edit --clear-desc`
    cleared = _fcli_json(ws, "issue", "edit", i_inline["key"], "--clear-desc")
    assert cleared["description"] == ""
    assert _read_disk_issue(ws, "Desc Inline Issue")["description"] == ""
    assert _fcli_json(ws, "issue", "show", i_inline["key"])["description"] == ""

    # (d) Inline description embeds: create `--embed <png>`, edit `--embed <svg>`, edit `--remove-embed <id>`, then `--clear-desc`
    emb_name = "Description Embed Lifecycle"
    emb_created = _fcli_json(
        ws,
        "issue",
        "create",
        emb_name,
        "--desc",
        "Diagram reference below:",
        "--embed",
        str(png_file),
    )
    assert len(emb_created["description_embeds"]) == 1
    png_meta = emb_created["description_embeds"][0]
    png_att_id = png_meta["id"]
    assert png_meta["filename"] == "arch_flow.png"
    assert png_meta["sha256"] == png_sha
    png_on_disk = ws.issue_dir(emb_name) / "attachments" / png_meta["stored_name"]
    assert png_on_disk.is_file() and sha256_file(png_on_disk) == png_sha
    assert f"attachment://{png_att_id}" in emb_created["description"]
    assert f"attachment://{png_att_id}" in _read_disk_issue(ws, emb_name)["description"]

    _fcli_json(ws, "issue", "edit", emb_name, "--embed", str(svg_file))
    after_svg = _fcli_json(ws, "issue", "show", emb_created["key"])
    disk_after_svg = _read_disk_issue(ws, emb_name)
    assert len(after_svg["description_embeds"]) == 2
    assert len(disk_after_svg["description_embeds"]) == 2
    svg_meta = next(e for e in after_svg["description_embeds"] if e["filename"] == "sequence_diag.svg")
    svg_att_id = svg_meta["id"]
    assert svg_meta["sha256"] == svg_sha
    svg_on_disk = ws.issue_dir(emb_name) / "attachments" / svg_meta["stored_name"]
    assert svg_on_disk.is_file() and sha256_file(svg_on_disk) == svg_sha
    assert f"attachment://{png_att_id}" in after_svg["description"]
    assert f"attachment://{svg_att_id}" in after_svg["description"]

    # Remove PNG embed; verify file & markdown tag removed while SVG remains
    after_rm_png = _fcli_json(ws, "issue", "edit", emb_created["key"], "--remove-embed", png_att_id)
    assert not png_on_disk.exists()
    assert svg_on_disk.is_file()
    assert [e["id"] for e in after_rm_png["description_embeds"]] == [svg_att_id]
    assert f"attachment://{png_att_id}" not in after_rm_png["description"]
    assert f"attachment://{svg_att_id}" in after_rm_png["description"]
    disk_after_rm = _read_disk_issue(ws, emb_name)
    assert [e["id"] for e in disk_after_rm["description_embeds"]] == [svg_att_id]
    assert f"attachment://{png_att_id}" not in disk_after_rm["description"]

    # Remove SVG embed and clear description completely
    _fcli_json(ws, "issue", "edit", emb_created["key"], "--remove-embed", svg_att_id)
    assert not svg_on_disk.exists()
    _fcli_json(ws, "issue", "edit", emb_created["key"], "--clear-desc")
    final_shown = _fcli_json(ws, "issue", "show", emb_created["key"])
    final_disk = _read_disk_issue(ws, emb_name)
    assert final_shown["description"] == ""
    assert final_shown["description_embeds"] == []
    assert final_disk["description"] == ""
    assert final_disk["description_embeds"] == []


def test_02_acceptance_criteria_create_mutate_and_clear(workspace: ITrackWorkspace) -> None:
    """Verify full AC lifecycle: `--ac`, `--add-ac`, `--edit-ac` (ID & 1-based), `--check-ac`, `--uncheck-ac`, `--remove-ac`, `--clear-ac`."""
    ws = workspace
    name = "JWT Acceptance Criteria Lifecycle"

    # (a) Create with 2 ACs
    created = _fcli_json(
        ws,
        "issue",
        "create",
        name,
        "--ac",
        "Verify token signature",
        "--ac",
        "Enforce expiry window",
    )
    expected_initial = [
        {"id": "AC-1", "text": "Verify token signature", "checked": False},
        {"id": "AC-2", "text": "Enforce expiry window", "checked": False},
    ]
    assert created["acceptance_criteria"] == expected_initial
    assert created["ac_progress"] == {"total": 2, "checked": 0, "pct": 0.0}
    assert _read_disk_issue(ws, name)["acceptance_criteria"] == expected_initial
    assert _fcli_json(ws, "issue", "show", created["key"])["ac_progress"] == {"total": 2, "checked": 0, "pct": 0.0}

    # (b1) --add-ac -> adds AC-3
    added = _fcli_json(ws, "issue", "edit", created["key"], "--add-ac", "Audit refresh rotation")
    assert len(added["acceptance_criteria"]) == 3
    assert added["acceptance_criteria"][2] == {
        "id": "AC-3",
        "text": "Audit refresh rotation",
        "checked": False,
    }
    assert _read_disk_issue(ws, name)["acceptance_criteria"][2]["id"] == "AC-3"

    # (b2) --edit-ac by ID (AC-1) AND 1-based positional index (2)
    edited = _fcli_json(
        ws,
        "issue",
        "edit",
        name,
        "--edit-ac",
        "AC-1",
        "Verify RS256 signature",
        "--edit-ac",
        "2",
        "Enforce 15m expiry",
    )
    assert edited["acceptance_criteria"][0]["text"] == "Verify RS256 signature"
    assert edited["acceptance_criteria"][1]["text"] == "Enforce 15m expiry"
    disk_edited = _read_disk_issue(ws, name)
    assert disk_edited["acceptance_criteria"][0]["text"] == "Verify RS256 signature"
    assert disk_edited["acceptance_criteria"][1]["text"] == "Enforce 15m expiry"

    # (b3) --check-ac AC-1 2 -> both checked
    checked = _fcli_json(ws, "issue", "edit", created["key"], "--check-ac", "AC-1", "2")
    assert checked["acceptance_criteria"][0]["checked"] is True
    assert checked["acceptance_criteria"][1]["checked"] is True
    assert checked["acceptance_criteria"][2]["checked"] is False
    assert checked["ac_progress"] == {"total": 3, "checked": 2, "pct": 66.7}
    disk_checked = _read_disk_issue(ws, name)
    assert [a["checked"] for a in disk_checked["acceptance_criteria"]] == [True, True, False]
    assert _fcli_json(ws, "issue", "show", created["key"])["ac_progress"] == {"total": 3, "checked": 2, "pct": 66.7}

    # (b4) --uncheck-ac AC-1 -> AC-1 unchecked, AC-2 still checked
    unchecked = _fcli_json(ws, "issue", "edit", created["key"], "--uncheck-ac", "AC-1")
    assert unchecked["acceptance_criteria"][0]["checked"] is False
    assert unchecked["acceptance_criteria"][1]["checked"] is True
    assert unchecked["ac_progress"] == {"total": 3, "checked": 1, "pct": 33.3}
    assert _read_disk_issue(ws, name)["acceptance_criteria"][0]["checked"] is False

    # (b5) --remove-ac 2 (positional) and --remove-ac AC-3 (by ID)
    rem_pos = _fcli_json(ws, "issue", "edit", created["key"], "--remove-ac", "2")
    assert [a["id"] for a in rem_pos["acceptance_criteria"]] == ["AC-1", "AC-3"]
    assert rem_pos["ac_progress"] == {"total": 2, "checked": 0, "pct": 0.0}
    assert [a["id"] for a in _read_disk_issue(ws, name)["acceptance_criteria"]] == ["AC-1", "AC-3"]

    rem_id = _fcli_json(ws, "issue", "edit", created["key"], "--remove-ac", "AC-3")
    assert [a["id"] for a in rem_id["acceptance_criteria"]] == ["AC-1"]
    assert [a["id"] for a in _read_disk_issue(ws, name)["acceptance_criteria"]] == ["AC-1"]

    # (c) --clear-ac -> empty list & zero progress
    cleared = _fcli_json(ws, "issue", "edit", created["key"], "--clear-ac")
    assert cleared["acceptance_criteria"] == []
    assert cleared["ac_progress"] == {"total": 0, "checked": 0, "pct": 0.0}
    assert _read_disk_issue(ws, name)["acceptance_criteria"] == []
    shown_final = _fcli_json(ws, "issue", "show", created["key"])
    assert shown_final["acceptance_criteria"] == []
    assert shown_final["ac_progress"] == {"total": 0, "checked": 0, "pct": 0.0}


def test_02_people_tags_and_components_lifecycle(workspace: ITrackWorkspace) -> None:
    """Test set on create, modify on edit, and clear to null/empty for assignee, reporter, tags, and components."""
    ws = workspace
    name = "Metadata Fields Full Matrix"

    # (a) Set all at creation
    created = _fcli_json(
        ws,
        "issue",
        "create",
        name,
        "--assignee",
        "alice",
        "--reporter",
        "carol",
        "--tag",
        "auth",
        "--tag",
        "security,backend",
        "--component",
        "api",
        "--component",
        "gateway,db",
    )
    for view in (created, _read_disk_issue(ws, name), _fcli_json(ws, "issue", "show", created["key"])):
        assert view["assignee"] == "alice"
        assert view["reporter"] == "carol"
        assert view["tags"] == ["auth", "security", "backend"]
        assert view["components"] == ["api", "gateway", "db"]

    # (b) Modify each field via `issue edit`
    edited = _fcli_json(
        ws,
        "issue",
        "edit",
        created["key"],
        "--assignee",
        "bob",
        "--reporter",
        "dave",
        "--add-tag",
        "urgent",
        "--remove-tag",
        "security",
        "--add-component",
        "worker",
        "--remove-component",
        "gateway",
    )
    for view in (edited, _read_disk_issue(ws, name), _fcli_json(ws, "issue", "show", name)):
        assert view["assignee"] == "bob"
        assert view["reporter"] == "dave"
        assert view["tags"] == ["auth", "backend", "urgent"]
        assert view["components"] == ["api", "db", "worker"]

    # (c) Clear each field back to None / [] and verify both disk & show --json
    _fcli_json(ws, "issue", "edit", created["key"], "--clear-assignee")
    assert _read_disk_issue(ws, name)["assignee"] is None
    assert _fcli_json(ws, "issue", "show", created["key"])["assignee"] is None

    _fcli_json(ws, "issue", "edit", created["key"], "--clear-reporter")
    assert _read_disk_issue(ws, name)["reporter"] is None
    assert _fcli_json(ws, "issue", "show", created["key"])["reporter"] is None

    _fcli_json(ws, "issue", "edit", created["key"], "--clear-tags")
    assert _read_disk_issue(ws, name)["tags"] == []
    assert _fcli_json(ws, "issue", "show", created["key"])["tags"] == []

    _fcli_json(ws, "issue", "edit", created["key"], "--clear-components")
    assert _read_disk_issue(ws, name)["components"] == []
    assert _fcli_json(ws, "issue", "show", created["key"])["components"] == []


def test_02_custom_fields_set_edit_unset_and_clear(workspace: ITrackWorkspace) -> None:
    """Test `custom_fields` set on create (`--field`), modify/unset (`--field` + `--unset-field`), and clear (`--clear-fields`)."""
    ws = workspace
    name = "Custom Fields Full Matrix"

    # (a) Set at creation
    created = _fcli_json(
        ws,
        "issue",
        "create",
        name,
        "--field",
        "env=staging",
        "--field",
        "service=auth",
        "--field",
        "region=us-east",
    )
    for view in (created, _read_disk_issue(ws, name), _fcli_json(ws, "issue", "show", created["key"])):
        assert view["custom_fields"] == {"env": "staging", "service": "auth", "region": "us-east"}

    # (b) Modify via `--field` + `--unset-field`
    edited = _fcli_json(
        ws,
        "issue",
        "edit",
        created["key"],
        "--field",
        "env=production",
        "--field",
        "tier=critical",
        "--unset-field",
        "region",
    )
    disk_edited = _read_disk_issue(ws, name)
    shown_edited = _fcli_json(ws, "issue", "show", name)

    # (c) Clear all via `--clear-fields` (execute prior to asserting `edited` so `--clear-fields` surface telemetry always records)
    cleared = _fcli_json(ws, "issue", "edit", created["key"], "--clear-fields")
    disk_cleared = _read_disk_issue(ws, name)
    shown_cleared = _fcli_json(ws, "issue", "show", created["key"])

    expected_edited = {"env": "production", "service": "auth", "tier": "critical"}
    for view in (edited, disk_edited, shown_edited):
        assert view["custom_fields"] == expected_edited

    for view in (cleared, disk_cleared, shown_cleared):
        assert view["custom_fields"] == {}


def test_02_parent_sprint_milestone_points_estimate_and_due_overdue_lifecycle(
    workspace: ITrackWorkspace,
) -> None:
    """Test set, modify, and clear for parent, sprint, milestone, story_points, estimate_minutes, and due_date + is_overdue."""
    ws = workspace

    # Prepare parents, sprints, and milestones
    epic1 = _fcli_json(ws, "issue", "create", "Auth Platform Epic", "--category", "epic")
    epic2 = _fcli_json(ws, "issue", "create", "Billing Platform Epic", "--category", "epic")
    spr1 = _fcli_json(ws, "sprint", "create", "Sprint Alpha")
    spr2 = _fcli_json(ws, "sprint", "create", "Sprint Beta")
    assert spr1["id"] == "SPR-1" and spr2["id"] == "SPR-2"
    mil1 = _fcli_json(ws, "milestone", "create", "v1.0")
    mil2 = _fcli_json(ws, "milestone", "create", "v2.0")
    assert mil1["id"] == "MIL-1" and mil2["id"] == "MIL-2"

    # (a) Create child issue with parent, sprint, milestone, points, estimate, and past due date in active `todo` state
    child_name = "Token Refresh Worker Task"
    created = _fcli_json(
        ws,
        "issue",
        "create",
        child_name,
        "--state",
        "todo",
        "--parent",
        "Auth Platform Epic",
        "--sprint",
        "Sprint Alpha",
        "--milestone",
        "v1.0",
        "--points",
        "3.5",
        "--estimate",
        "60",
        "--due",
        "2020-05-01",
    )
    disk_created = _read_disk_issue(ws, child_name)
    shown_created = _fcli_json(ws, "issue", "show", created["key"])

    for view in (created, disk_created, shown_created):
        assert view["parent"] == epic1["key"]
        assert view["sprint"] == "SPR-1"
        assert view["milestone"] == "MIL-1"
        assert view["story_points"] == 3.5
        assert view["estimate_minutes"] == 60
        assert view["due_date"] == "2020-05-01"

    assert shown_created["is_overdue"] is True
    overdue_list = _fcli_json(ws, "issue", "list", "--overdue")
    assert any(i["key"] == created["key"] for i in overdue_list["issues"])

    epic1_shown = _fcli_json(ws, "issue", "show", epic1["key"])
    assert [s["key"] for s in epic1_shown["subtasks"]] == [created["key"]]

    # Verify `is_overdue` transitions False when completed (`done`) and True again when reopened (`in-progress`)
    done_view = _fcli_json(ws, "issue", "state", created["key"], "done", "--resolution", "fixed")
    assert done_view["is_overdue"] is False
    assert _fcli_json(ws, "issue", "show", created["key"])["is_overdue"] is False
    overdue_when_done = _fcli_json(ws, "issue", "list", "--overdue")
    assert all(i["key"] != created["key"] for i in overdue_when_done["issues"])

    reopened = _fcli_json(ws, "issue", "state", created["key"], "in-progress")
    assert reopened["is_overdue"] is True
    assert _fcli_json(ws, "issue", "show", created["key"])["is_overdue"] is True

    # (b) Modify parent, sprint, milestone, points, estimate, and due_date (future date -> is_overdue False)
    edited = _fcli_json(
        ws,
        "issue",
        "edit",
        created["key"],
        "--clear-resolution",
        "--parent",
        "Billing Platform Epic",
        "--sprint",
        "SPR-2",
        "--milestone",
        "MIL-2",
        "--points",
        "8",
        "--estimate",
        "150",
        "--due",
        "2099-12-31",
    )
    disk_edited = _read_disk_issue(ws, child_name)
    shown_edited = _fcli_json(ws, "issue", "show", created["key"])

    for view in (edited, disk_edited, shown_edited):
        assert view["parent"] == epic2["key"]
        assert view["sprint"] == "SPR-2"
        assert view["milestone"] == "MIL-2"
        assert view["story_points"] == 8.0
        assert view["estimate_minutes"] == 150
        assert view["due_date"] == "2099-12-31"
    assert shown_edited["is_overdue"] is False

    assert _fcli_json(ws, "issue", "show", epic1["key"])["subtasks"] == []
    assert [s["key"] for s in _fcli_json(ws, "issue", "show", epic2["key"])["subtasks"]] == [created["key"]]

    # (c) Clear parent, sprint, milestone, points, estimate, and due_date back to None
    cleared = _fcli_json(
        ws,
        "issue",
        "edit",
        created["key"],
        "--clear-parent",
        "--clear-sprint",
        "--clear-milestone",
        "--clear-points",
        "--clear-estimate",
        "--clear-due",
    )
    disk_cleared = _read_disk_issue(ws, child_name)
    shown_cleared = _fcli_json(ws, "issue", "show", created["key"])

    for view in (cleared, disk_cleared, shown_cleared):
        assert view["parent"] is None
        assert view["sprint"] is None
        assert view["milestone"] is None
        assert view["story_points"] is None
        assert view["estimate_minutes"] is None
        assert view["due_date"] is None
    assert shown_cleared["is_overdue"] is False
    assert _fcli_json(ws, "issue", "show", epic2["key"])["subtasks"] == []


# ---------------------------------------------------------------------------
# 3. Invalid Value Rejection (`exit 1` with clear message, plus `exit 4` on missing resources)
# ---------------------------------------------------------------------------


def test_02_invalid_dates_numbers_and_enums_rejected(workspace: ITrackWorkspace) -> None:
    """Verify bad dates, negative points/estimates, and unknown enums exit 1 on both create and edit."""
    ws = workspace
    existing = _fcli_json(ws, "issue", "create", "Validation Target Issue")
    key = existing["key"]

    # Bad dates on create & edit (including a real subprocess check to verify OS exit code 1)
    for idx, bad_due in enumerate(("2026-13-45", "05/01/2026", "invalid-date")):
        _assert_error_both_modes(
            ws,
            "issue",
            "create",
            f"Bad Due Case {idx}",
            "--due",
            bad_due,
            expected_rc=1,
            expected_substr="due",
            use_subprocess=(idx == 0),
        )
        _assert_error_both_modes(
            ws, "issue", "edit", key, "--due", bad_due, expected_rc=1, expected_substr="due"
        )

    # Negative story points on create & edit (`--points -5` and `--points=-0.1`)
    _assert_error_both_modes(
        ws, "issue", "create", "Neg Points Space", "--points", "-5", expected_rc=1, expected_substr="story_points"
    )
    _assert_error_both_modes(
        ws, "issue", "create", "Neg Points Eq", "--points=-0.1", expected_rc=1, expected_substr="story_points"
    )
    _assert_error_both_modes(
        ws, "issue", "edit", key, "--points", "-5", expected_rc=1, expected_substr="story_points"
    )
    _assert_error_both_modes(
        ws, "issue", "edit", key, "--points=-0.1", expected_rc=1, expected_substr="story_points"
    )

    # Negative estimate minutes on create & edit (`--estimate -30` and `--estimate=-1`)
    _assert_error_both_modes(
        ws, "issue", "create", "Neg Est Space", "--estimate", "-30", expected_rc=1, expected_substr="estimate"
    )
    _assert_error_both_modes(
        ws, "issue", "create", "Neg Est Eq", "--estimate=-1", expected_rc=1, expected_substr="estimate"
    )
    _assert_error_both_modes(
        ws, "issue", "edit", key, "--estimate", "-30", expected_rc=1, expected_substr="estimate"
    )
    _assert_error_both_modes(
        ws, "issue", "edit", key, "--estimate=-1", expected_rc=1, expected_substr="estimate"
    )

    # Unknown category, state, priority, severity, resolution on create & edit
    enum_invalid_cases = [
        ("--category", "dragon", "category"),
        ("--state", "purgatory", "state"),
        ("--priority", "p0", "priority"),
        ("--severity", "sev0", "severity"),
        ("--resolution", "shrugged", "resolution"),
    ]
    for flag, bad_val, label in enum_invalid_cases:
        _assert_error_both_modes(
            ws, "issue", "create", f"Bad Enum {label}", flag, bad_val, expected_rc=1, expected_substr=label
        )
        _assert_error_both_modes(
            ws, "issue", "edit", key, flag, bad_val, expected_rc=1, expected_substr=label
        )

    _assert_error_both_modes(ws, "issue", "state", key, "purgatory", expected_rc=1, expected_substr="state")


def test_02_illegal_names_duplicates_conflicting_flags_and_not_found(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify path-traversal/illegal names, duplicate names, malformed fields, conflicting flags (exit 1), and missing refs (exit 4)."""
    ws = workspace
    target = _fcli_json(ws, "issue", "create", "Duplicate Issue Name")
    key = target["key"]

    # Illegal / path-traversal / empty names on `issue create` and `issue edit --name`
    illegal_names = ["../evil", "..", ".", "a/b", "a\\b", "", "   "]
    for bad_name in illegal_names:
        _assert_error_both_modes(ws, "issue", "create", bad_name, expected_rc=1)
        _assert_error_both_modes(ws, "issue", "edit", key, "--name", bad_name, expected_rc=1)

    # Duplicate issue name on `issue create` (exact match & case-variant -> exit 1 with "already exists")
    _assert_error_both_modes(
        ws, "issue", "create", "Duplicate Issue Name", expected_rc=1, expected_substr="already exists"
    )
    _assert_error_both_modes(
        ws, "issue", "create", "duplicate issue name", expected_rc=1, expected_substr="already exists"
    )

    # Malformed custom field (`--field missing_equals`) on create & edit
    _assert_error_both_modes(
        ws, "issue", "create", "Malformed Field Create", "--field", "missing_equals", expected_rc=1, expected_substr="KEY=VAL"
    )
    _assert_error_both_modes(
        ws, "issue", "edit", key, "--field", "missing_equals", expected_rc=1, expected_substr="KEY=VAL"
    )

    # Conflicting set + clear flags on `issue edit`
    conflict_flag_groups = [
        ["--assignee", "a", "--clear-assignee"],
        ["--reporter", "r", "--clear-reporter"],
        ["--desc", "x", "--clear-desc"],
        ["--resolution", "fixed", "--clear-resolution"],
        ["--points", "2", "--clear-points"],
        ["--estimate", "10", "--clear-estimate"],
        ["--due", "2026-12-01", "--clear-due"],
        ["--parent", "ITR-1", "--clear-parent"],
        ["--sprint", "SPR-1", "--clear-sprint"],
        ["--milestone", "MIL-1", "--clear-milestone"],
    ]
    for flags in conflict_flag_groups:
        _assert_error_both_modes(ws, "issue", "edit", key, *flags, expected_rc=1, expected_substr="cannot specify both")

    # Conflicting `--desc` + `--desc-file` on create & edit
    dfile, _ = make_text_file(tmp_path / "conflict_desc.md", "Conflict text")
    _assert_error_both_modes(
        ws, "issue", "create", "Conflict Desc Create", "--desc", "inline", "--desc-file", str(dfile), expected_rc=1
    )
    _assert_error_both_modes(
        ws, "issue", "edit", key, "--desc", "inline", "--desc-file", str(dfile), expected_rc=1
    )

    # Non-existent issue reference on show / edit / delete --yes exits 4 (including real OS subprocess check)
    _assert_error_both_modes(
        ws, "issue", "show", "ITR-99999", expected_rc=4, expected_substr="not found", use_subprocess=True
    )
    _assert_error_both_modes(ws, "issue", "show", "Non Existent Issue Name", expected_rc=4, expected_substr="not found")
    _assert_error_both_modes(ws, "issue", "edit", "ITR-99999", "--priority", "high", expected_rc=4, expected_substr="not found")
    _assert_error_both_modes(ws, "issue", "delete", "ITR-99999", "--yes", expected_rc=4, expected_substr="not found")


# ---------------------------------------------------------------------------
# 4. Dual Addressing, Lifecycle (`clone`, `archive`, `unarchive`, `context`, `delete`),
#    Human Rich vs `--json` Output, and `git status --porcelain` Cleanliness
# ---------------------------------------------------------------------------


def test_02_dual_addressing_across_all_issue_commands(workspace: ITrackWorkspace) -> None:
    """Address a multi-word issue interchangeably by exact spaced name, `ITR-N`, and lowercase `itr-n`."""
    ws = workspace
    spaced_name = "Multi Word Gateway Issue"
    created = ws.cli_json("issue", "create", spaced_name, "--state", "todo")
    key_upper = created["key"]
    key_lower = key_upper.lower()

    # `show` by exact name, uppercase key, lowercase key
    assert ws.cli_json("issue", "show", spaced_name)["key"] == key_upper
    assert _fcli_json(ws, "issue", "show", key_upper)["name"] == spaced_name
    assert ws.cli_json("issue", "show", key_lower)["name"] == spaced_name

    # `edit` by exact name and lowercase key
    ws.cli_json("issue", "edit", spaced_name, "--assignee", "nate")
    assert _fcli_json(ws, "issue", "show", key_lower)["assignee"] == "nate"
    ws.cli_json("issue", "edit", key_lower, "--priority", "high")
    assert _fcli_json(ws, "issue", "show", spaced_name)["priority"] == "high"

    # `state` by exact name and lowercase key
    ws.cli_json("issue", "state", spaced_name, "in-progress")
    assert _fcli_json(ws, "issue", "show", key_upper)["state"] == "in-progress"
    ws.cli_json("issue", "state", key_lower, "in-review")
    assert _fcli_json(ws, "issue", "show", spaced_name)["state"] == "in-review"

    # `archive` by exact name, `unarchive` by lowercase key
    arch = ws.cli_json("issue", "archive", spaced_name)
    assert arch["archived"] is True and arch["state"] == "archived"
    unarch = ws.cli_json("issue", "unarchive", key_lower)
    assert unarch["archived"] is False and unarch["state"] == "in-review"

    # `context` by exact name and lowercase key
    assert ws.cli_json("issue", "context", spaced_name)["key"] == key_upper
    assert _fcli_json(ws, "issue", "context", key_lower)["key"] == key_upper

    # `clone` by exact name and lowercase key, then `delete` clones by exact name and lowercase key
    c1 = ws.cli_json("issue", "clone", spaced_name, "--name", "Cloned By Spaced Name")
    c2 = ws.cli_json("issue", "clone", key_lower, "--name", "Cloned By Lowercase Key")
    ws.cli_json("issue", "delete", "Cloned By Spaced Name", "--yes")
    ws.cli_json("issue", "delete", c2["key"].lower(), "--yes")
    assert not ws.issue_dir(c1["name"]).exists()
    assert not ws.issue_dir(c2["name"]).exists()


def test_02_issue_clone_archive_unarchive_and_context_pack(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify `issue clone` (default, 2nd default, custom `--name`), `archive`/`unarchive` state restoration, and `issue context`."""
    ws = workspace

    epic = _fcli_json(ws, "issue", "create", "Core Architecture Epic", "--category", "epic")
    spr = _fcli_json(ws, "sprint", "create", "Sprint Clone")
    mil = _fcli_json(ws, "milestone", "create", "Release 3.0")

    src_name = "Source Feature Issue"
    src = _fcli_json(
        ws,
        "issue",
        "create",
        src_name,
        "--category",
        "feature",
        "--state",
        "in-progress",
        "--priority",
        "high",
        "--severity",
        "sev2",
        "--desc",
        "Rich cloneable specification body",
        "--ac",
        "First acceptance check",
        "--ac",
        "Second acceptance check",
        "--assignee",
        "alice",
        "--tag",
        "clone-tag,platform",
        "--component",
        "router",
        "--parent",
        epic["key"],
        "--sprint",
        spr["id"],
        "--milestone",
        mil["id"],
        "--points",
        "5",
        "--estimate",
        "120",
        "--due",
        "2032-08-15",
        "--field",
        "service=edge",
    )
    # Check AC-1 on source so we can verify clone resets all ACs to unchecked
    _fcli_json(ws, "issue", "edit", src["key"], "--check-ac", "AC-1")
    assert _fcli_json(ws, "issue", "show", src["key"])["ac_progress"]["checked"] == 1

    # 1. Default clone -> "Copy of Source Feature Issue"
    clone1 = _fcli_json(ws, "issue", "clone", src_name)
    assert clone1["name"] == "Copy of Source Feature Issue"
    assert ws.issue_dir("Copy of Source Feature Issue").is_dir()
    assert clone1["cloned_from"] == src["key"]
    assert clone1["category"] == "feature"
    assert clone1["priority"] == "high"
    assert clone1["severity"] == "sev2"
    assert clone1["description"] == "Rich cloneable specification body"
    assert clone1["assignee"] == "alice"
    assert clone1["tags"] == ["clone-tag", "platform"]
    assert clone1["components"] == ["router"]
    assert clone1["parent"] == epic["key"]
    assert clone1["sprint"] == spr["id"]
    assert clone1["milestone"] == mil["id"]
    assert clone1["story_points"] == 5.0
    assert clone1["estimate_minutes"] == 120
    assert clone1["due_date"] == "2032-08-15"
    assert clone1["custom_fields"] == {"service": "edge"}
    assert [a["checked"] for a in clone1["acceptance_criteria"]] == [False, False]
    assert clone1["ac_progress"] == {"total": 2, "checked": 0, "pct": 0.0}
    assert any(l["type"] == "clones" and l["target_key"] == src["key"] for l in clone1["links"])

    src_reloaded = _fcli_json(ws, "issue", "show", src["key"])
    assert any(l["type"] == "cloned-from" and l["target_key"] == clone1["key"] for l in src_reloaded["links"])

    # 2. Second default clone -> "Copy 2 of Source Feature Issue"
    clone2 = _fcli_json(ws, "issue", "clone", src_name)
    assert clone2["name"] == "Copy 2 of Source Feature Issue"
    assert ws.issue_dir("Copy 2 of Source Feature Issue").is_dir()
    assert clone2["cloned_from"] == src["key"]

    # 3. Custom named clone -> "Explicitly Named Clone"
    clone3 = _fcli_json(ws, "issue", "clone", src["key"], "--name", "Explicitly Named Clone")
    assert clone3["name"] == "Explicitly Named Clone"
    assert ws.issue_dir("Explicitly Named Clone").is_dir()
    assert clone3["cloned_from"] == src["key"]

    # 4. `issue archive` & `issue unarchive` restoring prior `in-qa` state
    _fcli_json(ws, "issue", "state", clone3["key"], "in-qa")
    archived = _fcli_json(ws, "issue", "archive", clone3["key"])
    assert archived["state"] == "archived"
    assert archived["archived"] is True
    assert _read_disk_issue(ws, "Explicitly Named Clone")["archived"] is True

    active_keys = {i["key"] for i in _fcli_json(ws, "issue", "list", "--tab", "active")["issues"]}
    archived_keys = {i["key"] for i in _fcli_json(ws, "issue", "list", "--tab", "archived")["issues"]}
    assert clone3["key"] not in active_keys
    assert clone3["key"] in archived_keys

    unarchived = _fcli_json(ws, "issue", "unarchive", clone3["key"])
    assert unarchived["archived"] is False
    assert unarchived["state"] == "in-qa"
    assert _read_disk_issue(ws, "Explicitly Named Clone")["state"] == "in-qa"
    active_keys_after = {i["key"] for i in _fcli_json(ws, "issue", "list", "--tab", "active")["issues"]}
    assert clone3["key"] in active_keys_after

    # 5. `issue context` with rich child subtask, peer link, attachment, comment, worklog, custom fields
    peer = _fcli_json(ws, "issue", "create", "Downstream Consumer Service")
    sub = _fcli_json(ws, "issue", "create", "Source Subtask Checklist", "--category", "subtask", "--parent", src["key"])
    _fcli_json(ws, "link", "add", src["key"], "blocks", peer["key"])
    att_file, _ = make_text_file(tmp_path / "runbook.txt", "Operational runbook details\n")
    _fcli_json(ws, "attach", "add", src["key"], str(att_file))
    _fcli_json(ws, "comment", "add", src["key"], "--body", "Reviewed edge router latency metrics.")
    _fcli_json(ws, "worklog", "add", src["key"], "45m", "--note", "Benchmarked router throughput")

    ctx_human = _fcli(ws, "issue", "context", src_name)
    assert src["key"] in ctx_human.stdout
    assert "Agent Context Pack" in ctx_human.stdout
    assert "Rich cloneable specification body" in ctx_human.stdout

    ctx_json = _fcli_json(ws, "issue", "context", src_name)
    md = ctx_json["markdown"]
    assert src["key"] in md
    assert f"itrack-issues/issues/{src_name}/" in md
    assert "Rich cloneable specification body" in md
    assert "First acceptance check" in md
    assert sub["key"] in md and "Source Subtask Checklist" in md
    assert peer["key"] in md and "blocks" in md
    assert "runbook.txt" in md
    assert "Reviewed edge router latency metrics." in md
    assert "45m" in md
    assert "edge" in md
    assert ctx_json["context"]["issue"]["key"] == src["key"]
    assert len(ctx_json["context"]["recent_history"]) >= 2


def test_02_issue_delete_confirmation_and_cascading_cleanup(
    workspace: ITrackWorkspace,
    tmp_path: Path,
) -> None:
    """Verify `issue delete` without `--yes` exits 1 safely, and with `--yes` scrubs folder, links, child parent, and FTS5."""
    ws = workspace

    pdf_file, _ = make_pdf(tmp_path / "doomed_spec.pdf", text="Doomed PDF")
    png_file, _ = make_png(tmp_path / "doomed_embed.png")
    cmt_txt, _ = make_text_file(tmp_path / "cmt_trace.log", "Trace log attachment")

    peer = _fcli_json(ws, "issue", "create", "Blocked Peer Service", "--state", "todo")
    doomed_name = "Doomed Issue With Spaces"
    doomed = _fcli_json(
        ws,
        "issue",
        "create",
        doomed_name,
        "--desc",
        "Contains unique token xyzzyquuxtoken42 in description",
        "--embed",
        str(png_file),
        "--attach",
        str(pdf_file),
    )
    doomed_key = doomed["key"]

    _fcli_json(
        ws,
        "comment",
        "add",
        doomed_name,
        "--body",
        "Comment also referencing xyzzyquuxtoken42",
        "--attach",
        str(cmt_txt),
    )
    child = _fcli_json(
        ws,
        "issue",
        "create",
        "Child Subtask Of Doomed",
        "--category",
        "subtask",
        "--parent",
        doomed_name,
    )
    _fcli_json(ws, "link", "add", doomed_name, "blocks", peer["key"])

    # Pre-delete assertions: folder + files exist, peer has link, child has parent, FTS5 finds token
    assert ws.issue_dir(doomed_name).is_dir()
    assert len(list((ws.issue_dir(doomed_name) / "attachments").iterdir())) == 2
    assert _fcli_json(ws, "issue", "show", peer["key"])["links"] != []
    assert _fcli_json(ws, "issue", "show", child["key"])["parent"] == doomed_key
    assert _fcli_json(ws, "search", "xyzzyquuxtoken42")["total"] == 1

    # Delete WITHOUT `--yes` -> exit 1 (`confirmation_required`), folder & index untouched
    _, err_json = _assert_error_both_modes(
        ws, "issue", "delete", doomed_name, expected_rc=1, expected_substr="--yes", use_subprocess=True
    )
    assert err_json["error"] == "confirmation_required"
    assert ws.issue_dir(doomed_name).is_dir()
    assert _fcli_json(ws, "search", "xyzzyquuxtoken42")["total"] == 1

    # Delete WITH `--yes` -> exit 0 and complete cleanup
    del_res = ws.cli_json("issue", "delete", doomed_name, "--yes")
    assert del_res["deleted"] is True
    assert del_res["key"] == doomed_key

    assert not ws.issue_dir(doomed_name).exists()

    peer_after = _fcli_json(ws, "issue", "show", peer["key"])
    assert peer_after["links"] == []
    assert peer_after["is_blocked"] is False
    assert _read_disk_issue(ws, "Blocked Peer Service")["links"] == []

    child_after = _fcli_json(ws, "issue", "show", child["key"])
    assert child_after["parent"] is None
    assert _read_disk_issue(ws, "Child Subtask Of Doomed")["parent"] is None

    assert _fcli_json(ws, "search", "xyzzyquuxtoken42")["total"] == 0
    _fcli(ws, "issue", "show", doomed_key, expect_rc=4)
    _fcli(ws, "issue", "show", doomed_name, expect_rc=4)


def test_02_human_rich_output_parity_and_git_status_porcelain_cleanliness(
    workspace: ITrackWorkspace,
) -> None:
    """Exercise human Rich output across all `issue` subcommands and verify `git status --porcelain` stays 100% clean."""
    ws = workspace

    # 1. Exercise every `issue` subcommand in human Rich mode (without `--json`) via real subprocess CLI
    r_create = ws.cli(
        "issue",
        "create",
        "Rich Output Showcase",
        "--category",
        "feature",
        "--state",
        "todo",
        "--priority",
        "high",
        "--severity",
        "sev2",
        "--desc",
        "Human output verification body",
        "--ac",
        "Render panel cleanly",
        "--tag",
        "tui",
        "--points",
        "3",
        "--estimate",
        "45",
        "--due",
        "2020-01-15",
    )
    assert "Created" in r_create.stdout and "Rich Output Showcase" in r_create.stdout

    r_show = ws.cli("issue", "show", "Rich Output Showcase")
    assert "Rich Output Showcase" in r_show.stdout
    assert "Acceptance Criteria" in r_show.stdout
    assert "OVERDUE" in r_show.stdout

    r_list = ws.cli("issue", "list", "--tab", "active")
    assert "Rich Output Showcase" in r_list.stdout

    r_edit = ws.cli("issue", "edit", "Rich Output Showcase", "--assignee", "grace", "--due", "2099-01-15")
    assert "Updated" in r_edit.stdout and "Rich Output Showcase" in r_edit.stdout

    r_state = ws.cli("issue", "state", "Rich Output Showcase", "in-progress")
    assert "State updated" in r_state.stdout and "in-progress" in r_state.stdout

    r_clone = ws.cli("issue", "clone", "Rich Output Showcase", "--name", "Rich Clone Temp")
    assert "Cloned" in r_clone.stdout and "Rich Clone Temp" in r_clone.stdout

    r_arch = ws.cli("issue", "archive", "Rich Clone Temp")
    assert "Archived" in r_arch.stdout

    r_unarch = ws.cli("issue", "unarchive", "Rich Clone Temp")
    assert "Unarchived" in r_unarch.stdout

    r_ctx = ws.cli("issue", "context", "Rich Output Showcase")
    assert "Agent Context Pack" in r_ctx.stdout

    r_del = ws.cli("issue", "delete", "Rich Clone Temp", "--yes")
    assert "Deleted issue" in r_del.stdout
    assert not ws.issue_dir("Rich Clone Temp").exists()

    # 2. `git status --porcelain` cleanliness check across commits, daemon restarts, reindex, and transient deletes
    _fcli_json(ws, "issue", "create", "Committed Issue", "--desc", "Tracked cleanly in Git")
    subprocess.run(["git", "add", "itrack-issues"], cwd=str(ws.root), check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Track issues"],
        cwd=str(ws.root),
        check=True,
        capture_output=True,
        text=True,
    )
    assert ws.git_status_porcelain() == ""

    # Daemon lifecycle + read/index commands must never dirty git status (.runtime/ & index.sqlite* gitignored)
    ws.cli("server", "restart")
    ws.cli("server", "stop")
    ws.cli("server", "start")
    for read_cmd in (
        ("status",),
        ("reindex",),
        ("issue", "list"),
        ("issue", "show", "Committed Issue"),
        ("search", "Tracked"),
        ("board",),
        ("tree",),
        ("stats",),
    ):
        _fcli(ws, *read_cmd)
        assert ws.git_status_porcelain() == "", f"Command {read_cmd} dirtied git status: {ws.git_status_porcelain()!r}"

    # Create & delete a temporary issue; verify zero `.tmp` or `index.sqlite` leaks, restore config.json, reindex -> clean
    _fcli_json(ws, "issue", "create", "Transient Issue", "--desc", "Ephemeral")
    _fcli_json(ws, "issue", "delete", "Transient Issue", "--yes")
    assert not ws.issue_dir("Transient Issue").exists()

    porcelain_after_transient = ws.git_status_porcelain()
    for line in porcelain_after_transient.splitlines():
        assert ".tmp" not in line
        assert "index.sqlite" not in line
        assert ".runtime" not in line
        assert "Transient Issue" not in line

    subprocess.run(
        ["git", "checkout", "--", "itrack-issues/config.json"],
        cwd=str(ws.root),
        check=True,
        capture_output=True,
        text=True,
    )
    _fcli(ws, "reindex")
    assert ws.git_status_porcelain() == ""
    assert_clean_repo_root()
