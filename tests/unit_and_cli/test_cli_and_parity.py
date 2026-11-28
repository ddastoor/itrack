"""End-to-end subprocess CLI tests and bidirectional CLI <-> HTTP REST API parity tests."""

from __future__ import annotations

import base64
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from tests.unit_and_cli.conftest import CLIRunner


class TestCLISubcommandsValidationAndRichText:
    """Verify CLI subcommands, human + --json outputs, exit codes (0/1/2), rich-text comments, and export/import."""

    def test_version_help_features_and_position_independent_global_flags(
        self, tmp_path: Path, cli_runner: CLIRunner
    ) -> None:
        ver = cli_runner.run_ok("--version")
        assert ver.stdout.strip() != ""

        hlp = cli_runner.run_ok("--help")
        assert "create" in hlp.stdout and "list" in hlp.stdout

        feat_human = cli_runner.run_ok("features")
        assert "create" in feat_human.stdout.lower() and "comment" in feat_human.stdout.lower()

        feat_json = cli_runner.run_json("features")
        features_list = feat_json["features"] if isinstance(feat_json, dict) and "features" in feat_json else feat_json
        assert isinstance(features_list, list) and len(features_list) >= 8

        help_cmd_json = cli_runner.run_json("help")
        assert help_cmd_json is not None

        # Position-independent --data-dir, --actor, and --json (before vs after subcommand)
        alt_dir = tmp_path / "alt_cli_store"
        created = cli_runner.run_ok(
            "--data-dir",
            str(alt_dir),
            "--actor",
            "grace",
            "--json",
            "create",
            "--summary",
            "Flag order check",
            "--description",
            "<p>Created with leading global flags</p>",
        ).json()
        assert created["number"] == 1
        assert created["reporter"] == "grace" or created["activity"][0]["actor"] == "grace"

        listed = cli_runner.run_ok("list", "--data-dir", str(alt_dir), "--json", use_env_data_dir=False).json()
        assert listed["total"] == 1
        assert listed["items"][0]["summary"] == "Flag order check"

    def test_cli_create_validation_errors_and_exit_codes(self, cli_runner: CLIRunner) -> None:
        # Empty, whitespace-only, and multi-line summaries -> exit_code 1
        for bad_summary in ("", "    ", "First line\nSecond line"):
            res = cli_runner.run("create", "--summary", bad_summary, "--description", "<p>Body</p>", "--json")
            assert res.returncode == 1
            err_json = res.json()
            assert err_json["ok"] is False
            assert err_json["exit_code"] == 1
            assert "Error:" in res.stderr

        # Empty, whitespace-only, and empty HTML tag descriptions -> exit_code 1
        for bad_desc in ("", "   ", "<p>   </p>", "<p>&nbsp;</p>", "<script>alert(1)</script>"):
            res = cli_runner.run("create", "--summary", "Valid summary", "--description", bad_desc, "--json")
            assert res.returncode == 1
            err_json = res.json()
            assert err_json["ok"] is False
            assert err_json["exit_code"] == 1

        # Non-existent issue -> exit_code 2; invalid ID -> exit_code 1
        not_found = cli_runner.run("show", "999", "--json")
        assert not_found.returncode == 2
        nf_json = not_found.json()
        assert nf_json["ok"] is False
        assert nf_json["exit_code"] == 2
        assert "Error:" in not_found.stderr

        bad_id = cli_runner.run("show", "0", "--json")
        assert bad_id.returncode == 1

    def test_cli_issue_inputs_stdin_file_images_and_issue_subcommand_alias(
        self, tmp_path: Path, cli_runner: CLIRunner, sample_png_file: Path, png_bytes: bytes
    ) -> None:
        desc_file = tmp_path / "desc.md"
        desc_file.write_text("## File Heading\n\nLoaded from **markdown** file with <u>underline</u>.", encoding="utf-8")

        i1 = cli_runner.run_json(
            "create",
            "--summary",
            "Created from file and image upload",
            "--description-file",
            str(desc_file),
            "--image",
            str(sample_png_file),
            "--image-alt",
            "System Topology",
            "--priority",
            "high",
            "--type",
            "bug",
            "--label",
            "infra",
            "--labels",
            "networking,prod",
            "--assignee",
            "alice",
            "--reporter",
            "bob",
            "--milestone",
            "Q4",
            "--due-date",
            "2026-12-15",
        )
        assert i1["number"] == 1
        assert i1["display_id"] == "#1"
        assert "<u>underline</u>" in i1["description"]
        assert len(i1["description_images"]) == 1
        assert i1["description_images"][0]["alt"] == "System Topology"
        assert i1["description_images"][0]["src"].startswith("/api/assets/")
        assert set(i1["labels"]) == {"infra", "networking", "prod"}

        b64_uri = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
        i2 = cli_runner.run_json(
            "issue",
            "create",
            "--summary",
            "Created via stdin and issue alias",
            "--description-stdin",
            "--image-url",
            b64_uri,
            "--image-alt",
            "DataURI Preview",
            "--link",
            "blocks:1",
            stdin_text="Stdin paragraph with [Docs](https://example.com/docs)",
        )
        assert i2["number"] == 2
        assert any(r["relation_type"] == "blocks" and r["target_number"] == 1 for r in i2["relationships"])
        assert any(img["kind"] == "data_uri" for img in i2["description_images"])

        # Verify human-readable `show` / `view` includes #N, summary, link URL, and [Image: ...] marker
        human_show = cli_runner.run_ok("view", "#1").stdout
        assert "#1" in human_show
        assert "Created from file and image upload" in human_show
        assert "[Image: System Topology (" in human_show

        human_show_2 = cli_runner.run_ok("show", "2").stdout
        assert "https://example.com/docs" in human_show_2

    def test_cli_paginated_list_filters_search_and_sorting(self, cli_runner: CLIRunner) -> None:
        rows = [
            ("Issue One Cache", "<p>Alpha body</p>", "open", "low", "task", "core", "alice", "rep1", "M1"),
            ("Issue Two Auth", "<p>Beta oauth flow</p>", "in_progress", "critical", "bug", "security", "bob", "rep1", "M1"),
            ("Issue Three Queue", "<p>Gamma worker pool</p>", "blocked", "high", "feature", "core", "alice", "rep2", "M2"),
            ("Issue Four UI", "<p>Delta layout grid</p>", "resolved", "medium", "improvement", "frontend", "carol", "rep2", "M2"),
            ("Issue Five CLI", "<p>Epsilon parser flag</p>", "closed", "high", "bug", "cli", "bob", "rep1", "M3"),
        ]
        for summary, desc, st, prio, tp, lbl, asg, rep, ms in rows:
            cli_runner.run_json(
                "create",
                "--summary",
                summary,
                "--description",
                desc,
                "--status",
                st,
                "--priority",
                prio,
                "--type",
                tp,
                "--label",
                lbl,
                "--assignee",
                asg,
                "--reporter",
                rep,
                "--milestone",
                ms,
            )

        cli_runner.run_json("comment", "add", "3", "--body", "<p>Unique comment phrase <code>nebula_shard_99</code></p>")

        # Human list displays #N and one-line summary
        human_p1 = cli_runner.run_ok("list", "--page", "1", "--per-page", "2").stdout
        assert "#1" in human_p1 and "Issue One Cache" in human_p1
        assert "#2" in human_p1 and "Issue Two Auth" in human_p1

        # JSON paginated list
        p2 = cli_runner.run_json("list", "--page", "2", "--per-page", "2", "--sort-by", "number", "--sort-order", "asc")
        assert p2["total"] == 5
        assert p2["total_pages"] == 3
        assert p2["page"] == 2
        assert p2["has_prev"] is True and p2["has_next"] is True
        assert [item["number"] for item in p2["items"]] == [3, 4]

        # Filtering by status, priority, type, label, assignee, reporter, milestone, date range
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert [i["number"] for i in cli_runner.run_json("list", "--status", "blocked")["items"]] == [3]
        assert [i["number"] for i in cli_runner.run_json("list", "--priority", "critical")["items"]] == [2]
        assert [i["number"] for i in cli_runner.run_json("list", "--type", "bug")["items"]] == [2, 5]
        assert [i["number"] for i in cli_runner.run_json("list", "--label", "core")["items"]] == [1, 3]
        assert [i["number"] for i in cli_runner.run_json("list", "--assignee", "carol")["items"]] == [4]
        assert [i["number"] for i in cli_runner.run_json("list", "--reporter", "rep2")["items"]] == [3, 4]
        assert [i["number"] for i in cli_runner.run_json("list", "--milestone", "M3")["items"]] == [5]
        assert cli_runner.run_json("list", "--created-from", today, "--created-to", today)["total"] == 5

        # Search subcommand across comments, descriptions, summaries, and numbers with pagination
        search_comment = cli_runner.run_json("search", "nebula_shard_99", "--page", "1", "--per-page", "5")
        assert [i["number"] for i in search_comment["items"]] == [3]

        # Sorting asc/desc
        sorted_prio = cli_runner.run_json("list", "--sort-by", "priority", "--sort-order", "desc")
        assert sorted_prio["items"][0]["number"] == 2
        assert sorted_prio["items"][-1]["number"] == 1

    def test_cli_edit_workflow_links_labels_clone_archive_delete_and_bulk(self, cli_runner: CLIRunner) -> None:
        cli_runner.run_json("create", "--summary", "Alpha item", "--description", "<p>Body A</p>", "--assignee", "alice", "--milestone", "M1", "--due-date", "2026-12-01", "--label", "oldtag")
        cli_runner.run_json("create", "--summary", "Beta item", "--description", "<p>Body B</p>")
        cli_runner.run_json("create", "--summary", "Gamma item", "--description", "<p>Body C</p>")

        # Edit validation rejections
        assert cli_runner.run("edit", "1", "--summary", "   ", "--json").returncode == 1
        assert cli_runner.run("edit", "1", "--description", "<p> </p>", "--json").returncode == 1

        edited = cli_runner.run_json(
            "edit",
            "1",
            "--summary",
            "Alpha item updated",
            "--description",
            "<p>Updated <strong>rich</strong> body</p>",
            "--priority",
            "critical",
            "--type",
            "feature",
            "--unassign",
            "--clear-milestone",
            "--clear-due-date",
            "--add-label",
            "newtag",
            "--remove-label",
            "oldtag",
        )
        assert edited["summary"] == "Alpha item updated"
        assert edited["priority"] == "critical"
        assert edited["issue_type"] == "feature"
        assert edited["assignee"] is None
        assert edited["milestone"] is None
        assert edited["due_date"] is None
        assert edited["labels"] == ["newtag"]

        # Status workflow, close, reopen
        assert cli_runner.run_json("status", "1", "in_progress")["status"] == "in_progress"
        closed = cli_runner.run_json("close", "1")
        assert closed["status"] == "closed" and closed["closed_at"] is not None
        reopened = cli_runner.run_json("reopen", "1")
        assert reopened["status"] == "open" and reopened["closed_at"] is None

        # Relationships
        rel = cli_runner.run_json("link", "add", "1", "blocks", "2")
        rel_id = rel["id"]
        rels_1 = cli_runner.run_json("link", "list", "1")
        rel_items = rels_1["relationships"] if isinstance(rels_1, dict) and "relationships" in rels_1 else rels_1
        assert any(r["id"] == rel_id and r["relation_type"] == "blocks" for r in rel_items)
        cli_runner.run_json("link", "remove", "1", str(rel_id))
        rels_after = cli_runner.run_json("link", "list", "1")
        rel_items_after = rels_after["relationships"] if isinstance(rels_after, dict) and "relationships" in rels_after else rels_after
        assert len(rel_items_after) == 0

        # Labels CLI management & confirmation check
        cli_runner.run_json("label", "create", "platform", "--color", "#2563eb")
        cli_runner.run_json("label", "rename", "newtag", "renamedtag")
        assert "renamedtag" in cli_runner.run_json("show", "1")["labels"]
        assert cli_runner.run("label", "delete", "platform", "--json").returncode == 1
        cli_runner.run_json("label", "delete", "platform", "--yes")

        # Clone, archive, restore, and confirmed delete
        cloned = cli_runner.run_json("clone", "1", "--summary", "Cloned from Alpha")
        assert cloned["number"] == 4 and cloned["cloned_from"] == 1

        cli_runner.run_json("archive", "4")
        assert 4 not in [i["number"] for i in cli_runner.run_json("list")["items"]]
        assert [i["number"] for i in cli_runner.run_json("list", "--archived-only")["items"]] == [4]
        cli_runner.run_json("restore", "4")
        assert 4 in [i["number"] for i in cli_runner.run_json("list")["items"]]

        # Delete requires --yes
        assert cli_runner.run("delete", "4", "--json").returncode == 1
        cli_runner.run_json("delete", "4", "--yes")
        assert cli_runner.run("show", "4", "--json").returncode == 2

        # Bulk update & bulk delete
        cli_runner.run_json("bulk", "update", "2", "3", "--status", "resolved", "--priority", "high", "--assignee", "erin", "--add-label", "batch")
        for num in ("2", "3"):
            item = cli_runner.run_json("show", num)
            assert item["status"] == "resolved"
            assert item["priority"] == "high"
            assert item["assignee"] == "erin"
            assert "batch" in item["labels"]

        assert cli_runner.run("bulk", "delete", "2", "3", "--json").returncode == 1
        cli_runner.run_json("bulk", "delete", "2", "3", "--yes")
        assert cli_runner.run("show", "2", "--json").returncode == 2

    def test_cli_rich_text_comments_add_edit_delete_xss_history_and_export_import(
        self, tmp_path: Path, cli_runner: CLIRunner, sample_png_file: Path
    ) -> None:
        cli_runner.run_json("create", "--summary", "Rich comment target", "--description", "<p>Base issue</p>")

        # Add rich-text comment with formatting, hyperlink, local image upload, URL image, and XSS vector
        c1 = cli_runner.run_json(
            "comment",
            "add",
            "1",
            "--author",
            "reviewer",
            "--body",
            (
                "<p><b>Bold</b> <i>italic</i> <u>underline</u> <s>strike</s> <code>x=1</code></p>"
                "<pre><code>print('hi')</code></pre>"
                "<h3>Subheading</h3><blockquote>Quoted line</blockquote>"
                "<ul><li>Bullet 1</li></ul><ol><li>Num 1</li></ol>"
                '<p><a href="https://example.com/runbook">Runbook</a> and <a href="javascript:alert(1)">BadHref</a></p>'
                '<script>alert("xss")</script><img src="x" onerror="alert(2)">'
            ),
            "--image",
            str(sample_png_file),
            "--image-alt",
            "CLI Uploaded Screenshot",
            "--image-url",
            "https://example.com/diagram.png",
        )
        cid = c1["id"]
        assert c1["is_edited"] is False
        assert "<u>underline</u>" in c1["body"]
        assert 'href="https://example.com/runbook"' in c1["body"]
        assert "<script" not in c1["body"].lower()
        assert "onerror" not in c1["body"].lower()
        assert "javascript:" not in c1["body"].lower()
        assert len(c1["images"]) == 2

        # Human CLI output shows formatted text, link URL, and inline [Image: ...] markers
        human_comment = cli_runner.run_ok("comment", "show", "1", str(cid)).stdout
        assert "https://example.com/runbook" in human_comment
        assert "[Image: CLI Uploaded Screenshot (" in human_comment

        # Reject empty comment edit -> exit 1
        assert cli_runner.run("comment", "edit", "1", str(cid), "--body", "<p>   </p>", "--json").returncode == 1

        # Edit existing comment by ID
        edited_c = cli_runner.run_json(
            "comment",
            "edit",
            "1",
            str(cid),
            "--body",
            '<p>Edited comment with <strong>new bold</strong>, <u>updated underline</u>, and <a href="mailto:team@example.com">Mail</a></p>',
        )
        assert edited_c["id"] == cid
        assert edited_c["is_edited"] is True
        assert "<u>updated underline</u>" in edited_c["body"]
        assert 'href="mailto:team@example.com"' in edited_c["body"]

        # Delete comment requires --yes; missing comment returns exit 2
        assert cli_runner.run("comment", "delete", "1", str(cid), "--json").returncode == 1
        cli_runner.run_json("comment", "delete", "1", str(cid), "--yes")
        assert cli_runner.run("comment", "show", "1", str(cid), "--json").returncode == 2

        # History command
        hist = cli_runner.run_json("history", "1")
        hist_items = hist["activity"] if isinstance(hist, dict) and "activity" in hist else hist
        assert len(hist_items) >= 4

        # Export JSON/CSV/Markdown & Import into clean store
        export_path = tmp_path / "bundle.json"
        cli_runner.run_ok("export", "--format", "json", "--output", str(export_path))
        assert export_path.is_file()

        csv_out = cli_runner.run_ok("export", "--format", "csv").stdout
        assert len(list(csv.DictReader(io.StringIO(csv_out)))) == 1

        md_out = cli_runner.run_ok("export", "--format", "markdown").stdout
        assert "#1" in md_out and "Rich comment target" in md_out

        fresh_dir = tmp_path / "fresh_import_dir"
        imp = cli_runner.run_ok("--data-dir", str(fresh_dir), "import", str(export_path), "--json", use_env_data_dir=False).json()
        assert imp["imported_count"] == 1
        restored = cli_runner.run_ok("--data-dir", str(fresh_dir), "show", "1", "--json", use_env_data_dir=False).json()
        assert restored["summary"] == "Rich comment target"


class TestRESTAPIAndBidirectionalCLIPariy:
    """Verify FastAPI HTTP routes and shared-storage parity between CLI and HTTP/UI server."""

    def test_http_routes_and_bidirectional_cli_api_shared_store_parity(
        self, api_client: TestClient, cli_runner: CLIRunner, sample_png_file: Path, png_bytes: bytes
    ) -> None:
        # 1. Health, SPA HTML shell, features, and meta routes
        assert api_client.get("/api/health").status_code == 200
        for ui_path in ("/", "/ui"):
            html_resp = api_client.get(ui_path)
            assert html_resp.status_code == 200
            assert 'data-testid="app-header"' in html_resp.text

        feat_resp = api_client.get("/api/features")
        assert feat_resp.status_code == 200
        assert feat_resp.json()["total"] >= 8

        # 2. Create issue + rich-text comment + uploaded image via CLI -> verify immediately via HTTP API
        cli_created = cli_runner.run_json(
            "create",
            "--summary",
            "CLI originated issue",
            "--description",
            "<p>CLI <u>rich</u> description</p>",
            "--image",
            str(sample_png_file),
            "--image-alt",
            "CLI Asset",
            "--priority",
            "high",
            "--label",
            "shared",
        )
        asset_url = cli_created["description_images"][0]["src"]
        cli_comment = cli_runner.run_json(
            "comment",
            "add",
            "1",
            "--body",
            '<p>CLI comment with <a href="https://example.com/cli">CLI Link</a></p>',
            "--author",
            "cli_user",
        )

        # HTTP API immediately reads CLI issue, comment, and binary image asset from shared store
        api_get_1 = api_client.get("/api/issues/1")
        assert api_get_1.status_code == 200
        detail_1 = api_get_1.json()
        assert detail_1["summary"] == "CLI originated issue"
        assert "<u>rich</u>" in detail_1["description"]
        assert len(detail_1["comments"]) == 1
        assert detail_1["comments"][0]["id"] == cli_comment["id"]

        asset_resp = api_client.get(asset_url)
        assert asset_resp.status_code == 200
        assert asset_resp.content == png_bytes

        # 3. Upload asset, create issue #2, edit #1, and edit CLI comment via HTTP API -> verify in CLI
        up_resp = api_client.post(
            "/api/assets",
            files={"file": ("ui_upload.png", png_bytes, "image/png")},
            data={"alt": "UI Uploaded Diagram"},
        )
        assert up_resp.status_code == 201
        ui_asset = up_resp.json()

        post_2 = api_client.post(
            "/api/issues",
            json={
                "summary": "API/UI originated issue",
                "description": f'<p>Created from UI with <img src="{ui_asset["url"]}" alt="UI Uploaded Diagram" /></p>',
                "status": "in_progress",
                "priority": "critical",
                "issue_type": "bug",
                "labels": ["ui-created"],
                "assignee": "ui_dev",
            },
        )
        assert post_2.status_code == 201
        assert post_2.json()["number"] == 2

        # Validation & 404 error contracts on HTTP API
        assert api_client.post("/api/issues", json={"summary": "Bad\nLine", "description": "<p>ok</p>"}).status_code == 400
        assert api_client.post("/api/issues", json={"summary": "Valid", "description": "<p>   </p>"}).status_code == 400
        assert api_client.get("/api/issues/999").status_code == 404

        patch_comment = api_client.patch(
            f"/api/issues/1/comments/{cli_comment['id']}",
            json={"body": "<p>Edited over HTTP with <strong>bold update</strong></p>", "actor": "ui_dev"},
        )
        assert patch_comment.status_code == 200
        assert patch_comment.json()["is_edited"] is True

        # CLI immediately sees issue #2, its embedded UI image asset, and the edited comment on #1
        cli_list = cli_runner.run_json("list", "--sort-by", "number", "--sort-order", "asc")
        assert [i["number"] for i in cli_list["items"]] == [1, 2]

        cli_issue_2 = cli_runner.run_json("show", "2")
        assert cli_issue_2["summary"] == "API/UI originated issue"
        assert cli_issue_2["description_images"][0]["src"] == ui_asset["url"]

        cli_human_2 = cli_runner.run_ok("show", "2").stdout
        assert "[Image: UI Uploaded Diagram (" in cli_human_2

        cli_issue_1 = cli_runner.run_json("show", "1")
        assert cli_issue_1["comments"][0]["is_edited"] is True
        assert "<strong>bold update</strong>" in cli_issue_1["comments"][0]["body"]
