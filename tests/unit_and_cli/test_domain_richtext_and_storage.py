"""Spec-anchored unit and domain tests for models, rich-text/XSS engine, IssueStore, and export/import."""

from __future__ import annotations

import base64
import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from itrack_core.exporter import export_issues, import_issues_json
from itrack_core.models import (
     DEFAULT_ISSUE_TYPE,
    DEFAULT_PRIORITY,
    DEFAULT_STATUS,
    VALID_EXPORT_FORMATS,
    VALID_ISSUE_TYPES,
    VALID_PRIORITIES,
    VALID_RELATIONSHIP_TYPES,
    VALID_SORT_FIELDS,
    VALID_SORT_ORDERS,
    VALID_STATUSES,
    ConflictError,
    ITrackError,
    NotFoundError,
    ValidationError,
    parse_issue_number,
    validate_iso_date,
    validate_summary,
)
from itrack_core.richtext import (
    extract_visible_text_and_images,
    has_meaningful_rich_content,
    markdown_or_html_to_safe_html,
    render_rich_html_for_cli,
    sanitize_rich_html,
    validate_mandatory_rich_text,
)
from itrack_core.storage import IssueStore, resolve_data_dir


class TestModelsAndValidators:
    """Verify canonical enums, error hierarchy, and field validators."""

    def test_canonical_enums_and_defaults(self) -> None:
        assert VALID_STATUSES == ("open", "in_progress", "blocked", "resolved", "closed")
        assert DEFAULT_STATUS == "open"
        assert VALID_PRIORITIES == ("low", "medium", "high", "critical")
        assert DEFAULT_PRIORITY == "medium"
        assert VALID_ISSUE_TYPES == ("bug", "feature", "task", "improvement")
        assert DEFAULT_ISSUE_TYPE == "task"
        assert VALID_RELATIONSHIP_TYPES == ("blocks", "blocked_by", "relates_to", "duplicates")
        assert VALID_SORT_FIELDS == ("number", "created_at", "updated_at", "priority", "status", "summary")
        assert VALID_SORT_ORDERS == ("asc", "desc")
        assert VALID_EXPORT_FORMATS == ("json", "csv", "markdown")

    def test_error_exit_and_http_codes(self) -> None:
        v_err = ValidationError("bad input")
        c_err = ConflictError("duplicate link")
        nf_err = NotFoundError("missing issue")

        assert isinstance(v_err, ITrackError)
        assert v_err.exit_code == 1
        assert v_err.status_code == 400

        assert isinstance(c_err, ITrackError)
        assert c_err.exit_code == 1
        assert c_err.status_code == 409

        assert isinstance(nf_err, ITrackError)
        assert nf_err.exit_code == 2
        assert nf_err.status_code == 404

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (1, 1),
            (42, 42),
            ("1", 1),
            ("#1", 1),
            ("  #19  ", 19),
        ],
    )
    def test_parse_issue_number_accepts_positive_int_and_hash_prefix(self, raw: int | str, expected: int) -> None:
        assert parse_issue_number(raw) == expected

    @pytest.mark.parametrize("invalid_id", [0, -1, -99, "0", "#0", "#-3", "", "   ", "abc", "#abc", "1.5"])
    def test_parse_issue_number_rejects_invalid_values(self, invalid_id: int | str) -> None:
        with pytest.raises(ValidationError):
            parse_issue_number(invalid_id)

    @pytest.mark.parametrize("bad_summary", [None, "", " ", "   \t  ", "Line one\nLine two", "Line one\rLine two", "a\r\nb"])
    def test_validate_summary_rejects_empty_whitespace_and_multiline(self, bad_summary: str | None) -> None:
        with pytest.raises(ValidationError):
            validate_summary(bad_summary)

    def test_validate_summary_strips_outer_spaces_and_preserves_single_line(self) -> None:
        assert validate_summary("  Fix OAuth callback race  ") == "Fix OAuth callback race"

    def test_validate_iso_date(self) -> None:
        assert validate_iso_date(None) is None
        assert validate_iso_date("") is None
        assert validate_iso_date("none") is None
        assert validate_iso_date("clear") is None
        assert validate_iso_date("2026-12-31") == "2026-12-31"

        for bad_date in ("2026-02-30", "2026-13-01", "31-12-2026", "not-a-date"):
            with pytest.raises(ValidationError):
                validate_iso_date(bad_date)

    def test_resolve_data_dir_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        explicit = tmp_path / "explicit_dir"
        env_dir = tmp_path / "env_dir"

        monkeypatch.setenv("ITRACK_DATA_DIR", str(env_dir))
        assert resolve_data_dir(explicit) == explicit.resolve() if resolve_data_dir(explicit).is_absolute() else explicit
        assert Path(resolve_data_dir(explicit)).name == "explicit_dir"
        assert Path(resolve_data_dir(None)) == env_dir or Path(resolve_data_dir(None)).resolve() == env_dir.resolve()

        monkeypatch.delenv("ITRACK_DATA_DIR", raising=False)
        default_dir = resolve_data_dir(None)
        assert default_dir.name == ".itrack_data"


class TestRichTextFormattingValidationAndSanitization:
    """Verify full rich-text support, mandatory validation, CLI rendering, and strict XSS neutralization."""

    def test_preserves_all_rich_text_formatting_links_and_images_from_html_and_markdown(self, png_bytes: bytes) -> None:
        b64_img = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
        raw = (
            "# Heading One\n\n"
            "Paragraph with **bold**, *italic*, <u>underline</u>, ~~strikethrough~~, and `inline_code()`.\n\n"
            "```python\ndef check():\n    return True\n```\n\n"
            "> Important blockquote note\n\n"
            "- Bullet alpha\n- Bullet beta\n\n"
            "1. Step one\n2. Step two\n\n"
            "[Project Spec](https://example.com/spec) and <a href=\"mailto:ops@example.com\">Email Ops</a>\n\n"
            "![Remote Chart](https://example.com/chart.png)\n"
            f'<img src="{b64_img}" alt="Inline PNG" />'
            '<img src="/api/assets/asset_123.png" alt="Uploaded Asset" data-asset-id="asset_123.png" />'
        )
        sanitized = markdown_or_html_to_safe_html(raw)

        assert "<h1>" in sanitized
        assert ("<strong>bold</strong>" in sanitized) or ("<b>bold</b>" in sanitized)
        assert ("<em>italic</em>" in sanitized) or ("<i>italic</i>" in sanitized)
        assert "<u>underline</u>" in sanitized
        assert ("<s>strikethrough</s>" in sanitized) or ("<del>strikethrough</del>" in sanitized)
        assert "<code>inline_code()</code>" in sanitized
        assert "<pre>" in sanitized and "def check():" in sanitized
        assert "<blockquote>" in sanitized and "Important blockquote note" in sanitized
        assert "<ul>" in sanitized and "<li>" in sanitized and "Bullet alpha" in sanitized
        assert "<ol>" in sanitized and "Step one" in sanitized
        assert 'href="https://example.com/spec"' in sanitized
        assert 'href="mailto:ops@example.com"' in sanitized

        visible_text, images = extract_visible_text_and_images(sanitized)
        assert "Heading One" in visible_text
        assert "underline" in visible_text
        assert len(images) == 3

        kinds = {img["alt"]: img for img in images}
        assert kinds["Remote Chart"]["kind"] == "url"
        assert kinds["Remote Chart"]["src"] == "https://example.com/chart.png"
        assert kinds["Inline PNG"]["kind"] == "data_uri"
        assert kinds["Inline PNG"]["src"].startswith("data:image/png;base64,")
        assert kinds["Uploaded Asset"]["kind"] == "asset"
        assert kinds["Uploaded Asset"]["src"] == "/api/assets/asset_123.png"

        cli_rendered = render_rich_html_for_cli(sanitized)
        assert "Project Spec" in cli_rendered and "https://example.com/spec" in cli_rendered
        assert "mailto:ops@example.com" in cli_rendered
        assert "[Image: Remote Chart (https://example.com/chart.png)]" in cli_rendered
        assert "[Image: Uploaded Asset (/api/assets/asset_123.png)]" in cli_rendered

    @pytest.mark.parametrize(
        "empty_input",
        [
            None,
            "",
            "   \t\n  ",
            "<p></p>",
            "<p>   </p>",
            "<p>&nbsp; &nbsp;</p>",
            "<div><br><strong>   </strong><u>\t</u></div>",
            "<script>alert('xss')</script>",
            "<style>body { display: none; }</style>",
            '<img src="javascript:alert(1)" alt="bad" />',
        ],
    )
    def test_mandatory_rich_text_rejects_empty_whitespace_and_empty_tags(self, empty_input: str | None) -> None:
        if empty_input is not None:
            sanitized = sanitize_rich_html(empty_input)
            assert has_meaningful_rich_content(sanitized) is False
        with pytest.raises(ValidationError):
            validate_mandatory_rich_text(empty_input, field_label="Description")

    def test_mandatory_rich_text_accepts_embedded_image_without_other_text(self) -> None:
        html_with_only_img = '<p><img src="https://example.com/only-image.png" alt="Crash Screenshot"></p>'
        sanitized, search_text, images = validate_mandatory_rich_text(html_with_only_img)
        assert has_meaningful_rich_content(sanitized) is True
        assert len(images) == 1
        assert images[0]["alt"] == "Crash Screenshot"
        assert "Crash Screenshot" in search_text

    def test_strict_xss_sanitization_neutralizes_scripts_handlers_and_javascript_urls(self) -> None:
        malicious = (
            '<p onclick="evilClick()" style="background:url(javascript:alert(1))">Safe text</p>'
            "<script>window.pwned = 1; alert('xss');</script>"
            "<style>body { background: red; }</style>"
            '<iframe src="https://evil.example/frame"></iframe>'
            '<svg onload="alert(2)"><circle r="10"/></svg>'
            '<img src="https://example.com/ok.png" alt="Valid" onerror="alert(3)" onload="alert(4)" />'
            '<img src="javascript:alert(5)" alt="BadImg" />'
            '<img src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==" alt="BadData" />'
            '<a href="javascript:alert(6)" onmouseover="alert(7)">ClickMe</a>'
            '<a href="java&#x09;script:alert(8)">ObfuscatedLink</a>'
            '<a href="vbscript:msgbox(1)">VBLink</a>'
            '<a href="https://safe.example/doc">SafeLink</a>'
        )
        clean = sanitize_rich_html(malicious)
        lowered = clean.lower()

        assert "<script" not in lowered
        assert "window.pwned" not in lowered
        assert "<style" not in lowered
        assert "<iframe" not in lowered
        assert "<svg" not in lowered
        assert "onclick" not in lowered
        assert "onerror" not in lowered
        assert "onload" not in lowered
        assert "onmouseover" not in lowered
        assert "javascript:" not in lowered
        assert "vbscript:" not in lowered
        assert "data:text/html" not in lowered

        # Valid content, link text, safe link, and safe image are preserved.
        assert "Safe text" in clean
        assert "ClickMe" in clean
        assert "ObfuscatedLink" in clean
        assert 'href="https://safe.example/doc"' in clean
        assert 'src="https://example.com/ok.png"' in clean


class TestIssueStoreLifecycleSearchSortAndAudit:
    """Verify IssueStore sequential numbering, CRUD, filtering, FTS search, sorting, comments, and audit trail."""

    def test_sequential_issue_numbers_never_reuse_deleted_numbers(self, store: IssueStore) -> None:
        i1 = store.create_issue("First summary", "<p>First description</p>")
        i2 = store.create_issue("Second summary", "<p>Second description</p>")
        assert i1["number"] == 1 and i1["display_id"] == "#1"
        assert i2["number"] == 2 and i2["display_id"] == "#2"

        store.delete_issue(2)
        with pytest.raises(NotFoundError):
            store.get_issue(2)

        i3 = store.create_issue("Third summary", "<p>Third description</p>")
        assert i3["number"] == 3 and i3["display_id"] == "#3"

    def test_create_and_edit_enforce_mandatory_summary_and_description(self, store: IssueStore) -> None:
        for bad_sum in ("", "   ", "Multi\nLine"):
            with pytest.raises(ValidationError):
                store.create_issue(bad_sum, "<p>Valid body</p>")

        for bad_desc in ("", "   ", "<p> </p>", "<p>&nbsp;</p>", "<div><br></div>"):
            with pytest.raises(ValidationError):
                store.create_issue("Valid summary", bad_desc)

        created = store.create_issue("Initial valid summary", "<p>Initial valid description</p>")
        num = created["number"]

        for bad_sum in ("", "   ", "Two\nLines"):
            with pytest.raises(ValidationError):
                store.update_issue(num, summary=bad_sum)

        for bad_desc in ("", "   ", "<p>   </p>", "<script>alert(1)</script>"):
            with pytest.raises(ValidationError):
                store.update_issue(num, description=bad_desc)

        # Original issue remains intact after failed updates.
        current = store.get_issue(num)
        assert current["summary"] == "Initial valid summary"
        assert "Initial valid description" in current["description"]

    def test_defaults_attributes_assets_and_workflow_transitions(self, store: IssueStore, png_bytes: bytes) -> None:
        asset = store.save_asset(png_bytes, "screen.png", "image/png", alt="Login Error")
        assert asset["url"] == f"/api/assets/{asset['asset_id']}"
        asset_path, asset_meta = store.get_asset(asset["asset_id"])
        assert asset_path.is_file()
        assert asset_meta["size_bytes"] == len(png_bytes)

        with pytest.raises(NotFoundError):
            store.get_asset("nonexistent_asset_999.png")

        default_issue = store.create_issue("Default fields check", "<p>Body</p>")
        assert default_issue["status"] == "open"
        assert default_issue["priority"] == "medium"
        assert default_issue["issue_type"] == "task"
        assert default_issue["labels"] == []
        assert default_issue["closed_at"] is None
        assert default_issue["archived"] is False

        rich_desc = f'<p>Attached <img src="{asset["url"]}" alt="Login Error" data-asset-id="{asset["asset_id"]}" /></p>'
        full = store.create_issue(
            "Custom attributes issue",
            rich_desc,
            status="in_progress",
            priority="critical",
            issue_type="bug",
            labels=["auth", "security"],
            assignee="alice",
            reporter="bob",
            milestone="v2.0",
            due_date="2026-12-20",
            actor="bob",
        )
        assert full["status"] == "in_progress"
        assert full["priority"] == "critical"
        assert full["issue_type"] == "bug"
        assert set(full["labels"]) == {"auth", "security"}
        assert full["assignee"] == "alice"
        assert full["reporter"] == "bob"
        assert full["milestone"] == "v2.0"
        assert full["due_date"] == "2026-12-20"
        assert len(full["description_images"]) == 1
        assert full["description_images"][0]["src"] == asset["url"]

        closed = store.close_issue(full["number"], actor="alice")
        assert closed["status"] == "closed"
        assert closed["closed_at"] is not None

        reopened = store.reopen_issue(full["number"], actor="alice")
        assert reopened["status"] == "open"
        assert reopened["closed_at"] is None

    def test_pagination_filtering_full_text_search_and_sorting(self, store: IssueStore) -> None:
        # Empty list pagination contract
        empty_page = store.list_issues(page=1, per_page=5)
        assert empty_page["total"] == 0
        assert empty_page["total_pages"] == 1
        assert empty_page["page"] == 1
        assert empty_page["has_prev"] is False
        assert empty_page["has_next"] is False
        assert empty_page["prev_page"] is None
        assert empty_page["next_page"] is None

        configs = [
            ("Alpha cache invalidation", "<p>Redis eviction mismatch</p>", "open", "critical", "bug", ["cache", "backend"], "alice", "dan", "M1"),
            ("Beta onboarding wizard", "<p>Step indicator polish</p>", "in_progress", "low", "improvement", ["ui"], "bob", "dan", "M2"),
            ("Gamma webhook retry", "<p>Exponential backoff queue</p>", "blocked", "high", "feature", ["backend", "webhooks"], "alice", "erin", "M1"),
            ("Delta export formatter", "<p>Markdown table escaping</p>", "resolved", "medium", "task", ["export"], "carol", "erin", "M2"),
            ("Epsilon token rotation", "<p>JWT refresh clock skew</p>", "closed", "high", "bug", ["auth", "backend"], "alice", "dan", "M1"),
            ("Zeta audit timeline", "<p>Chronological event stream</p>", "open", "low", "feature", ["audit"], "bob", "erin", "M3"),
            ("Eta dark theme contrast", "<p>WCAG AA badge ratios</p>", "in_progress", "medium", "improvement", ["ui", "a11y"], "carol", "dan", "M3"),
        ]
        for summary, desc, st, prio, itype, lbls, asg, rep, ms in configs:
            store.create_issue(
                summary,
                desc,
                status=st,
                priority=prio,
                issue_type=itype,
                labels=lbls,
                assignee=asg,
                reporter=rep,
                milestone=ms,
            )

        # Add a comment with a unique token on #4 to verify FTS across comments
        store.add_comment(4, "<p>Verified unique needle <strong>quasar_telemetry_token</strong> in comment</p>", author="carol")

        # Pagination navigation
        p1 = store.list_issues(page=1, per_page=3, sort_by="number", sort_order="asc")
        assert p1["total"] == 7
        assert p1["total_pages"] == 3
        assert [i["number"] for i in p1["items"]] == [1, 2, 3]
        assert p1["has_prev"] is False and p1["has_next"] is True
        assert p1["next_page"] == 2 and p1["last_page"] == 3

        p2 = store.list_issues(page=2, per_page=3, sort_by="number", sort_order="asc")
        assert [i["number"] for i in p2["items"]] == [4, 5, 6]
        assert p2["has_prev"] is True and p2["has_next"] is True
        assert p2["prev_page"] == 1 and p2["next_page"] == 3

        p3 = store.list_issues(page=3, per_page=3, sort_by="number", sort_order="asc")
        assert [i["number"] for i in p3["items"]] == [7]
        assert p3["has_prev"] is True and p3["has_next"] is False

        # Multi-field filtering
        assert [i["number"] for i in store.list_issues(status="open")["items"]] == [1, 6]
        assert [i["number"] for i in store.list_issues(priority="high")["items"]] == [3, 5]
        assert [i["number"] for i in store.list_issues(issue_type="bug")["items"]] == [1, 5]
        assert [i["number"] for i in store.list_issues(labels=["backend", "auth"])["items"]] == [5]
        assert [i["number"] for i in store.list_issues(assignee="carol")["items"]] == [4, 7]
        assert [i["number"] for i in store.list_issues(reporter="erin")["items"]] == [3, 4, 6]
        assert [i["number"] for i in store.list_issues(milestone="M3")["items"]] == [6, 7]

        # Date range filtering (inclusive today vs future range)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        future_str = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")
        assert store.list_issues(created_from=today_str, created_to=today_str)["total"] == 7
        assert store.list_issues(created_from=future_str)["total"] == 0
        assert store.list_issues(updated_to=today_str)["total"] == 7

        # Full-text search across summary, description, comment text, and issue number with pagination
        assert [i["number"] for i in store.list_issues(search="invalidation")["items"]] == [1]
        assert [i["number"] for i in store.list_issues(search="Exponential backoff")["items"]] == [3]
        assert [i["number"] for i in store.list_issues(search="quasar_telemetry_token")["items"]] == [4]
        assert [i["number"] for i in store.list_issues(search="#5")["items"]] == [5]

        backend_search_p1 = store.list_issues(labels=["backend"], page=1, per_page=2, sort_by="number", sort_order="asc")
        assert backend_search_p1["total"] == 3
        assert backend_search_p1["total_pages"] == 2
        assert len(backend_search_p1["items"]) == 2

        # Sorting by priority severity rank and status workflow order
        prio_desc = [i["priority"] for i in store.list_issues(sort_by="priority", sort_order="desc")["items"]]
        assert prio_desc == ["critical", "high", "high", "medium", "medium", "low", "low"]

        prio_asc = [i["priority"] for i in store.list_issues(sort_by="priority", sort_order="asc")["items"]]
        assert prio_asc == ["low", "low", "medium", "medium", "high", "high", "critical"]

        status_asc = [i["status"] for i in store.list_issues(sort_by="status", sort_order="asc")["items"]]
        assert status_asc == ["open", "open", "in_progress", "in_progress", "blocked", "resolved", "closed"]

        summary_desc = [i["summary"].split()[0] for i in store.list_issues(sort_by="summary", sort_order="desc")["items"]]
        assert summary_desc == sorted(["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta"], reverse=True)

    def test_relationships_labels_clone_archive_restore_and_bulk_ops(self, store: IssueStore) -> None:
        a = store.create_issue("Service A", "<p>Core API</p>", labels=["api", "v1"], priority="high")
        b = store.create_issue("Service B", "<p>Worker</p>", labels=["worker"])
        c = store.create_issue("Service C", "<p>Gateway</p>")

        # Self-relationship and duplicate relationship rejected with ConflictError
        with pytest.raises(ConflictError):
            store.add_relationship(a["number"], "blocks", a["number"])

        rel = store.add_relationship(a["number"], "blocks", b["number"], actor="alice")
        assert rel["relation_type"] == "blocks"
        assert rel["target_number"] == b["number"]

        with pytest.raises(ConflictError):
            store.add_relationship(a["number"], "blocks", b["number"])

        # Bidirectional visibility
        a_rels = store.list_relationships(a["number"])
        b_rels = store.list_relationships(b["number"])
        assert any(r["relation_type"] == "blocks" and r["target_number"] == b["number"] for r in a_rels)
        assert any(r["relation_type"] == "blocked_by" and r["target_number"] == a["number"] for r in b_rels)

        # Other relationship types: relates_to & duplicates
        store.add_relationship(b["number"], "relates_to", c["number"])
        store.add_relationship(c["number"], "duplicates", a["number"])
        assert any(r["relation_type"] == "relates_to" for r in store.list_relationships(c["number"]))

        store.remove_relationship(a["number"], rel["id"], actor="alice")
        assert not any(r["target_number"] == b["number"] for r in store.list_relationships(a["number"]))
        assert not any(r["target_number"] == a["number"] for r in store.list_relationships(b["number"]))

        # Labels create, rename (propagates to issues), and delete
        store.create_label("urgent", "#ef4444")
        store.update_issue(a["number"], add_labels=["urgent"])
        store.rename_label("v1", "v1-lts", color="#10b981")
        assert "v1-lts" in store.get_issue(a["number"])["labels"]
        assert "v1" not in store.get_issue(a["number"])["labels"]

        store.delete_label("urgent")
        assert "urgent" not in store.get_issue(a["number"])["labels"]

        # Clone creates new auto-numbered issue
        cloned = store.clone_issue(a["number"], actor="alice")
        assert cloned["number"] == 4
        assert cloned["cloned_from"] == a["number"]
        assert "Core API" in cloned["description"]
        assert "v1-lts" in cloned["labels"]

        # Archive & restore
        archived = store.archive_issue(cloned["number"], actor="alice")
        assert archived["archived"] is True
        assert cloned["number"] not in [i["number"] for i in store.list_issues()["items"]]
        assert cloned["number"] in [i["number"] for i in store.list_issues(include_archived=True)["items"]]
        assert [i["number"] for i in store.list_issues(archived_only=True)["items"]] == [cloned["number"]]

        restored = store.restore_issue(cloned["number"], actor="alice")
        assert restored["archived"] is False
        assert cloned["number"] in [i["number"] for i in store.list_issues()["items"]]

        # Bulk update & bulk delete
        bulk_res = store.bulk_update(
            [b["number"], c["number"]],
            status="resolved",
            priority="critical",
            assignee="dana",
            add_labels=["triaged"],
            actor="dana",
        )
        assert bulk_res["affected_count"] == 2
        for num in (b["number"], c["number"]):
            updated = store.get_issue(num)
            assert updated["status"] == "resolved"
            assert updated["priority"] == "critical"
            assert updated["assignee"] == "dana"
            assert "triaged" in updated["labels"]

        del_res = store.bulk_delete([b["number"], c["number"]], actor="dana")
        assert del_res["affected_count"] == 2
        for num in (b["number"], c["number"]):
            with pytest.raises(NotFoundError):
                store.get_issue(num)

    def test_rich_text_comments_crud_and_activity_history(self, store: IssueStore) -> None:
        issue = store.create_issue("Comment thread host", "<p>Initial spec</p>", actor="reporter_1")
        num = issue["number"]

        # Empty comments rejected
        for empty_body in ("", "   ", "<p></p>", "<p> &nbsp; </p>", "<script>alert(1)</script>"):
            with pytest.raises(ValidationError):
                store.add_comment(num, empty_body, author="alice")

        c1 = store.add_comment(
            num,
            '<p>Found <strong>root cause</strong> in <u>parser</u> — see <a href="https://example.com/pr/12">PR #12</a> '
            'and <img src="https://example.com/trace.png" alt="Stack Trace" /></p>',
            author="alice",
        )
        assert c1["id"] >= 1
        assert c1["author"] == "alice"
        assert c1["is_edited"] is False
        assert "<u>parser</u>" in c1["body"]
        assert len(c1["images"]) == 1
        assert store.get_issue(num)["comment_count"] == 1

        # Reject empty comment edit
        for empty_edit in ("", "  ", "<p>   </p>"):
            with pytest.raises(ValidationError):
                store.update_comment(num, c1["id"], empty_edit, actor="alice")

        time.sleep(0.005)
        edited = store.update_comment(
            num,
            c1["id"],
            "<p>Updated with <em>italic</em>, <s>old hypothesis</s>, and <code>fix_token()</code></p>"
            '<script>evil()</script><img src="x" onerror="evil()">',
            actor="alice",
        )
        assert edited["is_edited"] is True
        assert edited["updated_at"] >= c1["created_at"]
        assert "<script" not in edited["body"].lower()
        assert "onerror" not in edited["body"].lower()
        assert "fix_token()" in edited["body"]

        store.delete_comment(num, c1["id"], actor="alice")
        assert store.get_issue(num)["comment_count"] == 0
        with pytest.raises(NotFoundError):
            store.get_comment(num, c1["id"])

        # Activity audit log recorded creation and comment lifecycle
        history = store.get_activity_log(num)
        event_types = [ev["event_type"] for ev in history]
        assert "issue_created" in event_types
        assert any("comment" in et and "add" in et for et in event_types)
        assert any("comment" in et and ("edit" in et or "update" in et) for et in event_types)
        assert any("comment" in et and ("delete" in et or "remove" in et) for et in event_types)


class TestExportAndImportPortability:
    """Verify JSON, CSV, and Markdown export (all, filtered, single) and lossless JSON import."""

    def test_export_json_csv_markdown_and_roundtrip_import(
        self, tmp_path: Path, store: IssueStore, png_bytes: bytes
    ) -> None:
        asset = store.save_asset(png_bytes, "badge.png", "image/png", alt="Badge")
        i1 = store.create_issue(
            "Exportable bug one",
            f'<p>Rich <strong>description</strong> with <img src="{asset["url"]}" alt="Badge" data-asset-id="{asset["asset_id"]}" /></p>',
            status="open",
            priority="high",
            issue_type="bug",
            labels=["portability"],
            assignee="alice",
        )
        i2 = store.create_issue(
            "Exportable feature two",
            "<p>Second <u>underlined</u> description</p>",
            status="closed",
            priority="low",
            issue_type="feature",
        )
        store.add_relationship(i1["number"], "blocks", i2["number"])
        store.add_comment(i1["number"], "<p>Portability <code>comment</code> check</p>", author="bob")

        # JSON export (all, filtered, single)
        all_json_str = export_issues(store, fmt="json")
        all_payload = json.loads(all_json_str)
        exported_list = all_payload["issues"] if isinstance(all_payload, dict) and "issues" in all_payload else all_payload
        assert len(exported_list) == 2

        single_json_str = export_issues(store, fmt="json", issue_number=i1["number"])
        single_payload = json.loads(single_json_str)
        single_list = single_payload["issues"] if isinstance(single_payload, dict) and "issues" in single_payload else [single_payload]
        assert len(single_list) == 1
        assert single_list[0]["summary"] == "Exportable bug one"

        filtered_json_str = export_issues(store, fmt="json", filter_kwargs={"status": "closed"})
        filtered_payload = json.loads(filtered_json_str)
        filtered_list = filtered_payload["issues"] if isinstance(filtered_payload, dict) and "issues" in filtered_payload else filtered_payload
        assert len(filtered_list) == 1
        assert filtered_list[0]["summary"] == "Exportable feature two"

        # CSV export
        csv_text = export_issues(store, fmt="csv")
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert len(rows) == 2
        csv_blob = "\n".join(str(r) for r in rows)
        assert "Exportable bug one" in csv_blob
        assert "Exportable feature two" in csv_blob

        # Markdown export
        md_text = export_issues(store, fmt="markdown")
        assert "#1" in md_text and "Exportable bug one" in md_text
        assert "Portability" in md_text

        # Round-trip JSON import into a brand-new isolated IssueStore
        dest_store = IssueStore(tmp_path / "imported_store")
        try:
            imp_res = import_issues_json(dest_store, all_json_str, actor="importer")
            assert imp_res["imported_count"] == 2
            assert len(imp_res["imported_numbers"]) == 2

            restored_1 = dest_store.get_issue(imp_res["imported_numbers"][0])
            assert restored_1["summary"] == "Exportable bug one"
            assert "<strong>description</strong>" in restored_1["description"]
            assert len(restored_1["comments"]) == 1
            assert "comment" in restored_1["comments"][0]["body_text"]
            assert len(restored_1["description_images"]) == 1
        finally:
            dest_store.close()
