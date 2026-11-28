"""Export (JSON, CSV, Markdown) and JSON import utilities with portable asset support."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from typing import Any, Literal

from itrack_core.models import (
    VALID_EXPORT_FORMATS,
    VALID_RELATIONSHIP_TYPES,
    ValidationError,
    utc_now_iso,
    validate_enum,
)
from itrack_core.richtext import render_rich_html_for_cli
from itrack_core.storage import IssueStore

_ASSET_URL_RE = re.compile(r"/api/assets/([A-Za-z0-9._-]+)")


def _collect_issues_for_export(
    store: IssueStore,
    issue_number: int | str | None = None,
    filter_kwargs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if issue_number is not None:
        return [store.get_issue(issue_number)]

    kwargs = dict(filter_kwargs or {})
    kwargs.pop("page", None)
    kwargs.pop("per_page", None)
    # Default export includes archived issues unless caller specified archived_only/include_archived
    if "include_archived" not in kwargs and "archived_only" not in kwargs:
        kwargs["include_archived"] = True

    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        res = store.list_issues(page=page, per_page=200, **kwargs)
        for item in res["items"]:
            collected.append(store.get_issue(item["number"]))
        if not res["has_next"]:
            break
        page += 1
    return collected


def _collect_referenced_assets(store: IssueStore, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    assets_out: list[dict[str, Any]] = []

    def _add_asset(aid: str) -> None:
        if not aid or aid in seen_ids:
            return
        try:
            path, meta = store.get_asset(aid)
        except Exception:
            return
        seen_ids.add(aid)
        assets_out.append(
            {
                "asset_id": aid,
                "original_filename": meta["original_filename"],
                "mime_type": meta["mime_type"],
                "alt": meta["alt"],
                "data_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )

    for issue in issues:
        for img in issue.get("description_images", []):
            if img.get("asset_id"):
                _add_asset(str(img["asset_id"]))
        for m in _ASSET_URL_RE.finditer(issue.get("description", "")):
            _add_asset(m.group(1))
        for c in issue.get("comments", []):
            for img in c.get("images", []):
                if img.get("asset_id"):
                    _add_asset(str(img["asset_id"]))
            for m in _ASSET_URL_RE.finditer(c.get("body", "")):
                _add_asset(m.group(1))
    return assets_out


def export_issues(
    store: IssueStore,
    *,
    fmt: Literal["json", "csv", "markdown"] = "json",
    issue_number: int | str | None = None,
    filter_kwargs: dict[str, Any] | None = None,
) -> str:
    """Export single issue, filtered issues, or all issues to JSON, CSV, or Markdown."""
    clean_fmt = validate_enum(fmt, VALID_EXPORT_FORMATS, "format")
    issues = _collect_issues_for_export(store, issue_number=issue_number, filter_kwargs=filter_kwargs)

    if clean_fmt == "json":
        assets = _collect_referenced_assets(store, issues)
        payload = {
            "version": 1,
            "exported_at": utc_now_iso(),
            "count": len(issues),
            "issues": issues,
            "assets": assets,
        }
        return json.dumps(payload, indent=2)

    if clean_fmt == "csv":
        buf = io.StringIO()
        fieldnames = [
            "number",
            "display_id",
            "summary",
            "status",
            "priority",
            "issue_type",
            "labels",
            "assignee",
            "reporter",
            "milestone",
            "due_date",
            "archived",
            "cloned_from",
            "comment_count",
            "created_at",
            "updated_at",
            "closed_at",
            "description_text",
            "description_html",
            "comments_json",
            "relationships_json",
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for issue in issues:
            writer.writerow(
                {
                    "number": issue["number"],
                    "display_id": issue["display_id"],
                    "summary": issue["summary"],
                    "status": issue["status"],
                    "priority": issue["priority"],
                    "issue_type": issue["issue_type"],
                    "labels": ",".join(issue.get("labels", [])),
                    "assignee": issue.get("assignee") or "",
                    "reporter": issue.get("reporter") or "",
                    "milestone": issue.get("milestone") or "",
                    "due_date": issue.get("due_date") or "",
                    "archived": str(bool(issue.get("archived"))).lower(),
                    "cloned_from": issue.get("cloned_from") if issue.get("cloned_from") is not None else "",
                    "comment_count": issue.get("comment_count", 0),
                    "created_at": issue.get("created_at") or "",
                    "updated_at": issue.get("updated_at") or "",
                    "closed_at": issue.get("closed_at") or "",
                    "description_text": issue.get("description_text") or "",
                    "description_html": issue.get("description") or "",
                    "comments_json": json.dumps(issue.get("comments", [])),
                    "relationships_json": json.dumps(issue.get("relationships", [])),
                }
            )
        return buf.getvalue()

    # Markdown export
    lines: list[str] = ["# iTrack Issues Export", f"_Exported at {utc_now_iso()} ({len(issues)} issues)_", ""]
    for issue in issues:
        lines.append(f"## {issue['display_id']}: {issue['summary']}")
        lines.append("")
        lines.append(
            f"- **Status:** `{issue['status']}` | **Priority:** `{issue['priority']}` | **Type:** `{issue['issue_type']}`"
        )
        lines.append(
            f"- **Labels:** {', '.join(issue.get('labels') or []) or 'none'} | "
            f"**Assignee:** {issue.get('assignee') or 'unassigned'} | "
            f"**Reporter:** {issue.get('reporter') or 'unknown'}"
        )
        if issue.get("milestone") or issue.get("due_date"):
            lines.append(
                f"- **Milestone:** {issue.get('milestone') or 'none'} | **Due Date:** {issue.get('due_date') or 'none'}"
            )
        lines.append(
            f"- **Created:** {issue['created_at']} | **Updated:** {issue['updated_at']}"
            + (f" | **Closed:** {issue['closed_at']}" if issue.get("closed_at") else "")
        )
        lines.append("")
        lines.append("### Description")
        lines.append("")
        lines.append(render_rich_html_for_cli(issue.get("description", "")))
        lines.append("")

        rels = issue.get("relationships", [])
        if rels:
            lines.append("### Relationships")
            for rel in rels:
                lines.append(
                    f"- `{rel['relation_type']}` {rel['target_display_id']} ({rel['target_summary']}) [{rel['target_status']}]"
                )
            lines.append("")

        comments = issue.get("comments", [])
        if comments:
            lines.append(f"### Comments ({len(comments)})")
            for c in comments:
                edited_tag = f" *(edited {c['updated_at']})*" if c.get("is_edited") else ""
                lines.append(f"- **Comment #{c['id']}** by **{c['author']}** at {c['created_at']}{edited_tag}:")
                lines.append("")
                lines.append(render_rich_html_for_cli(c.get("body", "")))
                lines.append("")

        activity = issue.get("activity", [])
        if activity:
            lines.append("### Activity History")
            for ev in activity:
                lines.append(f"- [{ev['created_at']}] **{ev['event_type']}** by {ev['actor']}: {ev['summary']}")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def _rewrite_asset_urls(html_text: str, asset_map: dict[str, str]) -> str:
    if not asset_map or not html_text:
        return html_text

    def _repl(match: re.Match[str]) -> str:
        old_id = match.group(1)
        new_id = asset_map.get(old_id, old_id)
        return f"/api/assets/{new_id}"

    rewritten = _ASSET_URL_RE.sub(_repl, html_text)
    for old_id, new_id in asset_map.items():
        if old_id != new_id:
            rewritten = rewritten.replace(f'data-asset-id="{old_id}"', f'data-asset-id="{new_id}"')
    return rewritten


def import_issues_json(
    store: IssueStore,
    raw_json: str | dict[str, Any] | list[Any],
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Import issues, comments, assets, and relationships from JSON string or parsed payload."""
    if isinstance(raw_json, str):
        stripped = raw_json.strip()
        if not stripped:
            raise ValidationError("Import JSON payload cannot be empty.")
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid import JSON: {exc}") from exc
    else:
        parsed = raw_json

    if isinstance(parsed, dict) and "data" in parsed and "issues" not in parsed:
        inner = parsed["data"]
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid import JSON in 'data': {exc}") from exc
        else:
            parsed = inner

    assets_raw: list[dict[str, Any]] = []
    if isinstance(parsed, list):
        issues_raw = parsed
    elif isinstance(parsed, dict):
        issues_raw = parsed.get("issues")
        if not isinstance(issues_raw, list):
            raise ValidationError("Import JSON object must contain an 'issues' list.")
        if isinstance(parsed.get("assets"), list):
            assets_raw = parsed["assets"]
    else:
        raise ValidationError("Import JSON must be an object with 'issues' or a list of issues.")

    if not issues_raw:
        raise ValidationError("Import JSON contains no issues to import.")

    asset_map: dict[str, str] = {}
    for asset in assets_raw:
        if not isinstance(asset, dict):
            continue
        old_id = str(asset.get("asset_id") or "").strip()
        b64 = asset.get("data_base64")
        if not old_id or not isinstance(b64, str):
            continue
        try:
            raw_bytes = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise ValidationError(f"Invalid base64 image data for asset '{old_id}': {exc}") from exc
        saved = store.save_asset(
            raw_bytes,
            str(asset.get("original_filename") or f"{old_id}.png"),
            mime_type=asset.get("mime_type"),
            alt=str(asset.get("alt") or ""),
        )
        asset_map[old_id] = saved["asset_id"]

    old_to_new_number: dict[int, int] = {}
    created_numbers: list[int] = []
    pending_relationships: list[tuple[int, str, int]] = []

    for idx, item in enumerate(issues_raw):
        if not isinstance(item, dict):
            raise ValidationError(f"Issue entry at index {idx} must be a JSON object.")
        desc_raw = item.get("description")
        if isinstance(desc_raw, str):
            desc_raw = _rewrite_asset_urls(desc_raw, asset_map)

        created = store.create_issue(
            summary=item.get("summary"),
            description=desc_raw,
            status=item.get("status") or "open",
            priority=item.get("priority") or "medium",
            issue_type=item.get("issue_type") or "task",
            labels=item.get("labels"),
            assignee=item.get("assignee"),
            reporter=item.get("reporter") or actor,
            milestone=item.get("milestone"),
            due_date=item.get("due_date"),
            actor=actor or item.get("reporter"),
        )
        new_num = int(created["number"])
        created_numbers.append(new_num)
        if isinstance(item.get("number"), int):
            old_to_new_number[int(item["number"])] = new_num

        if item.get("archived"):
            store.archive_issue(new_num, actor=actor)

        for comment in item.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            body_raw = comment.get("body")
            if isinstance(body_raw, str):
                body_raw = _rewrite_asset_urls(body_raw, asset_map)
            store.add_comment(
                new_num,
                body_raw,
                author=comment.get("author") or actor,
            )

        for rel in item.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            if rel.get("direction", "outbound") != "outbound":
                continue
            rt = str(rel.get("relation_type") or "")
            tgt = rel.get("target_number")
            if rt in VALID_RELATIONSHIP_TYPES and isinstance(tgt, int):
                pending_relationships.append((new_num, rt, tgt))

    for src_new, rt, old_tgt in pending_relationships:
        tgt_new = old_to_new_number.get(old_tgt)
        if tgt_new and tgt_new != src_new:
            try:
                store.add_relationship(src_new, rt, tgt_new, actor=actor)
            except Exception:
                pass

    imported_issues = [store.get_issue(num) for num in created_numbers]
    return {
        "imported_count": len(created_numbers),
        "imported_numbers": created_numbers,
        "issues": imported_issues,
    }
