"""Deterministic non-interactive CLI for iTrack with full UI parity."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, NoReturn, Sequence

import uvicorn

from itrack_core.api import create_app
from itrack_core.exporter import export_issues, import_issues_json
from itrack_core.models import (
    FEATURES_CATALOG,
    ITrackError,
    VERSION,
    ValidationError,
    normalize_label_list,
    parse_issue_number,
)
from itrack_core.richtext import render_rich_html_for_cli
from itrack_core.server_ctl import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    _validate_host,
    _validate_port,
    get_server_status,
    restart_server,
    start_server,
    stop_server,
)
from itrack_core.storage import IssueStore, resolve_data_dir


class _ITrackArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises ValidationError (exit code 1) instead of argparse's exit code 2."""

    def error(self, message: str) -> NoReturn:
        raise ValidationError(message, code="usage_error")


def _build_global_parent() -> _ITrackArgumentParser:
    parent = _ITrackArgumentParser(add_help=False)
    parent.add_argument(
        "--data-dir",
        dest="data_dir",
        default=argparse.SUPPRESS,
        help="Override backing data directory (default: ITRACK_DATA_DIR or ./.itrack_data).",
    )
    parent.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Output machine-readable JSON to stdout.",
    )
    parent.add_argument(
        "--actor",
        dest="actor",
        default=argparse.SUPPRESS,
        help="Actor/author identity (default: ITRACK_USER or 'anonymous').",
    )
    return parent


def _add_list_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--page", type=int, default=1, help="Page number (default: 1).")
    parser.add_argument("--per-page", "--page-size", dest="per_page", type=int, default=10, help="Items per page (default: 10).")
    parser.add_argument("--status", action="append", default=None, help="Filter by status (repeatable or comma-separated).")
    parser.add_argument("--priority", action="append", default=None, help="Filter by priority (repeatable or comma-separated).")
    parser.add_argument("--type", dest="issue_type", action="append", default=None, help="Filter by issue type.")
    parser.add_argument("--label", dest="labels", action="append", default=None, help="Filter by label(s).")
    parser.add_argument("--assignee", default=None, help="Filter by assignee.")
    parser.add_argument("--reporter", default=None, help="Filter by reporter.")
    parser.add_argument("--milestone", default=None, help="Filter by milestone.")
    parser.add_argument("--created-from", default=None, help="Created-at lower bound (YYYY-MM-DD or ISO).")
    parser.add_argument("--created-to", default=None, help="Created-at upper bound (YYYY-MM-DD or ISO).")
    parser.add_argument("--updated-from", default=None, help="Updated-at lower bound (YYYY-MM-DD or ISO).")
    parser.add_argument("--updated-to", default=None, help="Updated-at upper bound (YYYY-MM-DD or ISO).")
    parser.add_argument("--sort-by", default="number", help="Sort field (number|created_at|updated_at|priority|status|summary).")
    parser.add_argument("--sort-order", default="asc", help="Sort order (asc|desc).")
    parser.add_argument("--include-archived", action="store_true", help="Include archived issues.")
    parser.add_argument("--archived-only", action="store_true", help="Show archived issues only.")


def _add_server_start_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_SERVER_HOST, help=f"Bind host (default: {DEFAULT_SERVER_HOST}).")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help=f"Bind port (default: {DEFAULT_SERVER_PORT}, 0 for ephemeral).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_START_TIMEOUT, help=f"Startup health wait timeout in seconds (default: {DEFAULT_START_TIMEOUT}).")
    parser.add_argument("--open-browser", action="store_true", help="Open Web UI in default browser after startup.")
    parser.add_argument("--foreground", action="store_true", help="Run server in foreground instead of detaching.")


def _add_server_stop_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=DEFAULT_STOP_TIMEOUT, help=f"Graceful shutdown wait timeout in seconds (default: {DEFAULT_STOP_TIMEOUT}).")
    parser.add_argument("--force", action="store_true", help="Send SIGKILL immediately instead of SIGTERM.")


def _register_issue_subcommands(
    subparsers: argparse._SubParsersAction[Any],
    global_parent: _ITrackArgumentParser,
) -> None:
    # create
    p_create = subparsers.add_parser("create", parents=[global_parent], help="Create a new auto-numbered issue.")
    p_create.add_argument("--summary", default=None, help="Mandatory single-line summary.")
    p_create.add_argument("--description", default=None, help="Mandatory rich-text description (HTML or Markdown).")
    p_create.add_argument("--description-file", default=None, help="Read description from file path.")
    p_create.add_argument("--description-stdin", action="store_true", help="Read description from stdin.")
    p_create.add_argument("--status", default="open", help="Initial status (default: open).")
    p_create.add_argument("--priority", default="medium", help="Priority (default: medium).")
    p_create.add_argument("--type", dest="issue_type", default="task", help="Issue type (default: task).")
    p_create.add_argument("--label", dest="label_list", action="append", default=None, help="Add label (repeatable).")
    p_create.add_argument("--labels", dest="labels_csv", default=None, help="Comma-separated labels.")
    p_create.add_argument("--assignee", default=None, help="Assignee username.")
    p_create.add_argument("--reporter", default=None, help="Reporter username.")
    p_create.add_argument("--milestone", default=None, help="Milestone name.")
    p_create.add_argument("--due-date", default=None, help="Due date (YYYY-MM-DD).")
    p_create.add_argument("--link", action="append", default=None, help="Initial link <REL_TYPE>:<TARGET_NUM>.")
    p_create.add_argument("--image", action="append", default=None, help="Upload & embed local image file.")
    p_create.add_argument("--image-alt", action="append", default=None, help="Alt text for embedded image.")
    p_create.add_argument("--image-url", action="append", default=None, help="Embed image by URL or data URI.")
    p_create.set_defaults(_handler="create")

    # list
    p_list = subparsers.add_parser("list", parents=[global_parent], help="List issues with pagination, filters, search, and sorting.")
    _add_list_filter_args(p_list)
    p_list.add_argument("--search", default=None, help="Full-text search query.")
    p_list.set_defaults(_handler="list")

    # show / view
    for cmd_name in ("show", "view"):
        p_show = subparsers.add_parser(cmd_name, parents=[global_parent], help="Show full issue details.")
        p_show.add_argument("issue_number", help="Issue number (e.g. 1 or #1).")
        p_show.set_defaults(_handler="show")

    # edit
    p_edit = subparsers.add_parser("edit", parents=[global_parent], help="Edit an existing issue.")
    p_edit.add_argument("issue_number", help="Issue number (e.g. 1 or #1).")
    p_edit.add_argument("--summary", default=None, help="Updated single-line summary.")
    p_edit.add_argument("--description", default=None, help="Updated rich-text description.")
    p_edit.add_argument("--description-file", default=None, help="Read updated description from file.")
    p_edit.add_argument("--description-stdin", action="store_true", help="Read updated description from stdin.")
    p_edit.add_argument("--image", action="append", default=None, help="Upload & embed local image file.")
    p_edit.add_argument("--image-alt", action="append", default=None, help="Alt text for embedded image.")
    p_edit.add_argument("--image-url", action="append", default=None, help="Embed image by URL or data URI.")
    p_edit.add_argument("--status", default=None, help="New status.")
    p_edit.add_argument("--priority", default=None, help="New priority.")
    p_edit.add_argument("--type", dest="issue_type", default=None, help="New issue type.")
    p_edit.add_argument("--assignee", default=None, help="New assignee.")
    p_edit.add_argument("--unassign", action="store_true", help="Clear assignee.")
    p_edit.add_argument("--reporter", default=None, help="New reporter.")
    p_edit.add_argument("--milestone", default=None, help="New milestone.")
    p_edit.add_argument("--clear-milestone", action="store_true", help="Clear milestone.")
    p_edit.add_argument("--due-date", default=None, help="New due date (YYYY-MM-DD).")
    p_edit.add_argument("--clear-due-date", action="store_true", help="Clear due date.")
    p_edit.add_argument("--set-labels", default=None, help="Replace labels with CSV list.")
    p_edit.add_argument("--clear-labels", action="store_true", help="Remove all labels.")
    p_edit.add_argument("--add-label", action="append", default=None, help="Add label(s).")
    p_edit.add_argument("--remove-label", action="append", default=None, help="Remove label(s).")
    p_edit.set_defaults(_handler="edit")

    # status
    p_status = subparsers.add_parser("status", parents=[global_parent], help="Transition issue workflow status.")
    p_status.add_argument("issue_number", help="Issue number.")
    p_status.add_argument("status_value", help="Target status (open|in_progress|blocked|resolved|closed).")
    p_status.set_defaults(_handler="status")

    # close / reopen / archive / restore
    p_close = subparsers.add_parser("close", parents=[global_parent], help="Close an issue.")
    p_close.add_argument("issue_number", help="Issue number.")
    p_close.set_defaults(_handler="close")

    p_reopen = subparsers.add_parser("reopen", parents=[global_parent], help="Reopen a closed issue.")
    p_reopen.add_argument("issue_number", help="Issue number.")
    p_reopen.set_defaults(_handler="reopen")

    p_clone = subparsers.add_parser("clone", parents=[global_parent], help="Clone an issue into a new sequential issue.")
    p_clone.add_argument("issue_number", help="Source issue number.")
    p_clone.add_argument("--summary", default=None, help="Optional summary for cloned issue.")
    p_clone.set_defaults(_handler="clone")

    p_archive = subparsers.add_parser("archive", parents=[global_parent], help="Archive an issue.")
    p_archive.add_argument("issue_number", help="Issue number.")
    p_archive.set_defaults(_handler="archive")

    p_restore = subparsers.add_parser(
        "restore", aliases=["unarchive"], parents=[global_parent], help="Restore an archived issue."
    )
    p_restore.add_argument("issue_number", help="Issue number.")
    p_restore.set_defaults(_handler="restore")

    p_delete = subparsers.add_parser("delete", parents=[global_parent], help="Permanently delete an issue (requires --yes).")
    p_delete.add_argument("issue_number", help="Issue number.")
    p_delete.add_argument("--yes", "-y", action="store_true", help="Confirm permanent deletion.")
    p_delete.set_defaults(_handler="delete")


def build_parser() -> _ITrackArgumentParser:
    """Build full CLI ArgumentParser."""
    global_parent = _build_global_parent()
    parser = _ITrackArgumentParser(
        prog="itrack.py",
        description="iTrack — Unified CLI & Web UI Issue Tracker",
        parents=[global_parent],
    )
    parser.add_argument("-V", "--version", action="store_true", help="Show version and exit.")

    subparsers = parser.add_subparsers(dest="command", metavar="<subcommand>")

    # Top-level features & help
    p_features = subparsers.add_parser("features", parents=[global_parent], help="List all supported issue tracker features.")
    p_features.set_defaults(_handler="features")

    p_help = subparsers.add_parser("help", parents=[global_parent], help="Display CLI help overview.")
    p_help.set_defaults(_handler="help")

    # Top-level issue subcommands
    _register_issue_subcommands(subparsers, global_parent)

    # Alias group: itrack.py issue <subcommand>
    p_issue_grp = subparsers.add_parser("issue", parents=[global_parent], help="Issue management commands.")
    issue_sub = p_issue_grp.add_subparsers(dest="issue_subcommand", metavar="<issue_subcommand>")
    _register_issue_subcommands(issue_sub, global_parent)
    p_issue_grp.set_defaults(_handler="issue_help")

    # search
    p_search = subparsers.add_parser("search", parents=[global_parent], help="Full-text search issues with pagination.")
    p_search.add_argument("query", help="Search query string.")
    _add_list_filter_args(p_search)
    p_search.set_defaults(_handler="search")

    # link
    p_link = subparsers.add_parser("link", parents=[global_parent], help="Manage issue relationships.")
    link_sub = p_link.add_subparsers(dest="link_action", metavar="<add|remove|list>")
    p_link_add = link_sub.add_parser("add", parents=[global_parent], help="Add a relationship.")
    p_link_add.add_argument("issue_number", help="Source issue number.")
    p_link_add.add_argument("relation_type", help="Relationship type (blocks|blocked_by|relates_to|duplicates).")
    p_link_add.add_argument("target_number", help="Target issue number.")
    p_link_add.set_defaults(_handler="link_add")

    p_link_rem = link_sub.add_parser("remove", parents=[global_parent], help="Remove a relationship.")
    p_link_rem.add_argument("issue_number", help="Issue number.")
    p_link_rem.add_argument("relationship_id", nargs="?", type=int, default=None, help="Relationship ID.")
    p_link_rem.add_argument("--type", dest="relation_type", default=None, help="Relationship type.")
    p_link_rem.add_argument("--target", dest="target_number", default=None, help="Target issue number.")
    p_link_rem.set_defaults(_handler="link_remove")

    p_link_list = link_sub.add_parser("list", parents=[global_parent], help="List issue relationships.")
    p_link_list.add_argument("issue_number", help="Issue number.")
    p_link_list.set_defaults(_handler="link_list")
    p_link.set_defaults(_handler="link_help")

    # label
    p_label = subparsers.add_parser("label", parents=[global_parent], help="Manage labels.")
    label_sub = p_label.add_subparsers(dest="label_action", metavar="<list|create|rename|delete>")
    p_lbl_list = label_sub.add_parser("list", parents=[global_parent], help="List all labels.")
    p_lbl_list.set_defaults(_handler="label_list")

    p_lbl_create = label_sub.add_parser("create", parents=[global_parent], help="Create a label.")
    p_lbl_create.add_argument("name", help="Label name.")
    p_lbl_create.add_argument("--color", default="#3b82f6", help="Hex color (default: #3b82f6).")
    p_lbl_create.set_defaults(_handler="label_create")

    p_lbl_rename = label_sub.add_parser("rename", parents=[global_parent], help="Rename or recolor a label.")
    p_lbl_rename.add_argument("old_name", help="Existing label name.")
    p_lbl_rename.add_argument("new_name", help="New label name.")
    p_lbl_rename.add_argument("--color", default=None, help="Optional new hex color.")
    p_lbl_rename.set_defaults(_handler="label_rename")

    p_lbl_del = label_sub.add_parser("delete", parents=[global_parent], help="Delete a label (requires --yes).")
    p_lbl_del.add_argument("name", help="Label name.")
    p_lbl_del.add_argument("--yes", "-y", action="store_true", help="Confirm label deletion.")
    p_lbl_del.set_defaults(_handler="label_delete")
    p_label.set_defaults(_handler="label_help")

    # bulk
    p_bulk = subparsers.add_parser("bulk", parents=[global_parent], help="Bulk update or delete issues.")
    bulk_sub = p_bulk.add_subparsers(dest="bulk_action", metavar="<update|delete>")
    p_bulk_upd = bulk_sub.add_parser("update", parents=[global_parent], help="Bulk update issues.")
    p_bulk_upd.add_argument("issue_numbers", nargs="+", help="Issue numbers.")
    p_bulk_upd.add_argument("--status", default=None, help="Target status.")
    p_bulk_upd.add_argument("--priority", default=None, help="Target priority.")
    p_bulk_upd.add_argument("--assignee", default=None, help="Target assignee.")
    p_bulk_upd.add_argument("--unassign", action="store_true", help="Clear assignee.")
    p_bulk_upd.add_argument("--add-label", action="append", default=None, help="Add label(s).")
    p_bulk_upd.add_argument("--remove-label", action="append", default=None, help="Remove label(s).")
    p_bulk_upd.add_argument("--set-labels", default=None, help="Replace labels with CSV list.")
    p_bulk_upd.add_argument("--clear-labels", action="store_true", help="Clear all labels.")
    p_bulk_upd.set_defaults(_handler="bulk_update")

    p_bulk_del = bulk_sub.add_parser("delete", parents=[global_parent], help="Bulk delete issues (requires --yes).")
    p_bulk_del.add_argument("issue_numbers", nargs="+", help="Issue numbers.")
    p_bulk_del.add_argument("--yes", "-y", action="store_true", help="Confirm permanent deletion.")
    p_bulk_del.set_defaults(_handler="bulk_delete")
    p_bulk.set_defaults(_handler="bulk_help")

    # comment
    p_comment = subparsers.add_parser("comment", parents=[global_parent], help="Manage rich-text comments.")
    cmt_sub = p_comment.add_subparsers(dest="comment_action", metavar="<add|list|show|edit|delete>")

    p_cmt_add = cmt_sub.add_parser("add", parents=[global_parent], help="Add a rich-text comment.")
    p_cmt_add.add_argument("issue_number", help="Issue number.")
    p_cmt_add.add_argument("--body", default=None, help="Comment body (HTML or Markdown).")
    p_cmt_add.add_argument("--body-file", default=None, help="Read comment body from file.")
    p_cmt_add.add_argument("--stdin", action="store_true", help="Read comment body from stdin.")
    p_cmt_add.add_argument("--author", default=None, help="Comment author.")
    p_cmt_add.add_argument("--image", action="append", default=None, help="Upload & embed local image file.")
    p_cmt_add.add_argument("--image-alt", action="append", default=None, help="Alt text for embedded image.")
    p_cmt_add.add_argument("--image-url", action="append", default=None, help="Embed image by URL or data URI.")
    p_cmt_add.set_defaults(_handler="comment_add")

    p_cmt_list = cmt_sub.add_parser("list", parents=[global_parent], help="List comments on an issue.")
    p_cmt_list.add_argument("issue_number", help="Issue number.")
    p_cmt_list.set_defaults(_handler="comment_list")

    p_cmt_show = cmt_sub.add_parser("show", parents=[global_parent], help="Show a specific comment.")
    p_cmt_show.add_argument("issue_number", help="Issue number.")
    p_cmt_show.add_argument("comment_id", help="Comment ID.")
    p_cmt_show.set_defaults(_handler="comment_show")

    p_cmt_edit = cmt_sub.add_parser("edit", parents=[global_parent], help="Edit an existing rich-text comment.")
    p_cmt_edit.add_argument("issue_number", help="Issue number.")
    p_cmt_edit.add_argument("comment_id", help="Comment ID.")
    p_cmt_edit.add_argument("--body", default=None, help="Updated comment body (HTML or Markdown).")
    p_cmt_edit.add_argument("--body-file", default=None, help="Read updated body from file.")
    p_cmt_edit.add_argument("--stdin", action="store_true", help="Read updated body from stdin.")
    p_cmt_edit.add_argument("--author", default=None, help="Editor/actor identity.")
    p_cmt_edit.add_argument("--image", action="append", default=None, help="Upload & embed local image file.")
    p_cmt_edit.add_argument("--image-alt", action="append", default=None, help="Alt text for embedded image.")
    p_cmt_edit.add_argument("--image-url", action="append", default=None, help="Embed image by URL or data URI.")
    p_cmt_edit.set_defaults(_handler="comment_edit")

    p_cmt_del = cmt_sub.add_parser("delete", parents=[global_parent], help="Delete a comment (requires --yes).")
    p_cmt_del.add_argument("issue_number", help="Issue number.")
    p_cmt_del.add_argument("comment_id", help="Comment ID.")
    p_cmt_del.add_argument("--yes", "-y", action="store_true", help="Confirm comment deletion.")
    p_cmt_del.set_defaults(_handler="comment_delete")
    p_comment.set_defaults(_handler="comment_help")

    # history
    p_history = subparsers.add_parser("history", parents=[global_parent], help="View per-issue activity audit log.")
    p_history.add_argument("issue_number", help="Issue number.")
    p_history.set_defaults(_handler="history")

    # export
    p_export = subparsers.add_parser("export", parents=[global_parent], help="Export issues to JSON, CSV, or Markdown.")
    p_export.add_argument("--format", dest="fmt", default="json", help="Export format (json|csv|markdown).")
    p_export.add_argument("--output", default=None, help="Write export output to file path.")
    p_export.add_argument("--issue", default=None, help="Export a single issue by number.")
    _add_list_filter_args(p_export)
    p_export.add_argument("--search", default=None, help="Filter export by search query.")
    p_export.set_defaults(_handler="export")

    # import
    p_import = subparsers.add_parser("import", parents=[global_parent], help="Import issues from JSON file or stdin.")
    p_import.add_argument("path", nargs="?", default=None, help="Path to JSON export file.")
    p_import.add_argument("--stdin", action="store_true", help="Read JSON import payload from stdin.")
    p_import.set_defaults(_handler="import")

    # ui / serve
    p_ui = subparsers.add_parser("ui", aliases=["serve"], parents=[global_parent], help="Start the local Web UI server.")
    p_ui.add_argument("--host", default=DEFAULT_SERVER_HOST, help=f"Bind host (default: {DEFAULT_SERVER_HOST}).")
    p_ui.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help=f"Bind port (default: {DEFAULT_SERVER_PORT}).")
    p_ui.add_argument("--open-browser", action="store_true", help="Open Web UI in default browser.")
    p_ui.add_argument("--background", "-d", "--detach", dest="background", action="store_true", help="Run server in the background.")
    p_ui.add_argument("--timeout", type=float, default=DEFAULT_START_TIMEOUT, help=f"Background startup timeout in seconds (default: {DEFAULT_START_TIMEOUT}).")
    p_ui.set_defaults(_handler="ui")

    # server group: server start|stop|status|restart
    p_server = subparsers.add_parser("server", parents=[global_parent], help="Manage the background iTrack Web UI & API server.")
    server_sub = p_server.add_subparsers(dest="server_action", metavar="<start|stop|status|restart>")

    p_srv_start = server_sub.add_parser("start", parents=[global_parent], help="Start the background server.")
    _add_server_start_args(p_srv_start)
    p_srv_start.set_defaults(_handler="server_start")

    p_srv_stop = server_sub.add_parser("stop", parents=[global_parent], help="Stop the background server.")
    _add_server_stop_args(p_srv_stop)
    p_srv_stop.set_defaults(_handler="server_stop")

    p_srv_status = server_sub.add_parser("status", parents=[global_parent], help="Show background server status.")
    p_srv_status.set_defaults(_handler="server_status")

    p_srv_restart = server_sub.add_parser("restart", parents=[global_parent], help="Restart the background server.")
    p_srv_restart.add_argument("--host", default=None, help="Bind host (defaults to previous host or 127.0.0.1).")
    p_srv_restart.add_argument("--port", type=int, default=None, help="Bind port (defaults to previous port or 8000).")
    p_srv_restart.add_argument("--timeout", type=float, default=DEFAULT_START_TIMEOUT, help=f"Startup health wait timeout in seconds (default: {DEFAULT_START_TIMEOUT}).")
    p_srv_restart.add_argument("--stop-timeout", type=float, default=DEFAULT_STOP_TIMEOUT, help=f"Graceful shutdown wait timeout in seconds (default: {DEFAULT_STOP_TIMEOUT}).")
    p_srv_restart.add_argument("--open-browser", action="store_true", help="Open Web UI in default browser after restart.")
    p_srv_restart.set_defaults(_handler="server_restart")
    p_server.set_defaults(_handler="server_help")

    # Top-level convenience start / stop
    p_start = subparsers.add_parser("start", parents=[global_parent], help="Start the background server (alias for `server start`).")
    _add_server_start_args(p_start)
    p_start.set_defaults(_handler="server_start")

    p_stop = subparsers.add_parser("stop", parents=[global_parent], help="Stop the background server (alias for `server stop`).")
    _add_server_stop_args(p_stop)
    p_stop.set_defaults(_handler="server_stop")

    return parser


def _read_text_source(
    inline_text: str | None,
    file_path: str | None,
    from_stdin: bool,
    label: str,
) -> str | None:
    provided = sum(bool(x) for x in (inline_text is not None, file_path is not None, from_stdin))
    if provided > 1:
        raise ValidationError(f"Specify only one {label} source (inline, file, or stdin).")
    if file_path is not None:
        p = Path(file_path)
        if not p.is_file():
            raise ValidationError(f"{label.capitalize()} file not found: {file_path}")
        return p.read_text(encoding="utf-8")
    if from_stdin:
        return sys.stdin.read()
    return inline_text


def _build_embedded_images_html(
    store: IssueStore,
    image_files: Sequence[str] | None,
    image_urls: Sequence[str] | None,
    image_alts: Sequence[str] | None,
) -> str:
    alts = list(image_alts or [])
    alt_idx = 0
    snippets: list[str] = []

    for img_path_str in image_files or []:
        p = Path(img_path_str)
        if not p.is_file():
            raise ValidationError(f"Image file not found: {img_path_str}")
        alt_text = alts[alt_idx] if alt_idx < len(alts) else p.name
        alt_idx += 1
        saved = store.save_asset(p.read_bytes(), filename=p.name, alt=alt_text)
        snippets.append(
            f'<p><img src="{html.escape(saved["url"], quote=True)}" '
            f'alt="{html.escape(alt_text, quote=True)}" '
            f'data-asset-id="{html.escape(saved["asset_id"], quote=True)}" /></p>'
        )

    for url_str in image_urls or []:
        alt_text = alts[alt_idx] if alt_idx < len(alts) else "image"
        alt_idx += 1
        snippets.append(
            f'<p><img src="{html.escape(url_str, quote=True)}" alt="{html.escape(alt_text, quote=True)}" /></p>'
        )

    return "\n".join(snippets)


def _combine_rich_text_and_images(base_text: str | None, images_html: str) -> str | None:
    if base_text is None and not images_html:
        return None
    if base_text is None:
        return images_html
    if not images_html:
        return base_text
    if base_text.strip() == "":
        return images_html
    return f"{base_text}\n\n{images_html}"


def _parse_link_specs(raw_links: Sequence[str] | None) -> list[dict[str, Any]]:
    rels: list[dict[str, Any]] = []
    for spec in raw_links or []:
        if ":" not in spec:
            raise ValidationError(f"Invalid --link '{spec}': expected <REL_TYPE>:<TARGET_NUM>.")
        rt, target = spec.split(":", 1)
        rels.append({"relation_type": rt.strip(), "target_number": parse_issue_number(target)})
    return rels


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


def _print_issue_detail_human(issue: dict[str, Any]) -> None:
    lines: list[str] = [
        f"{issue['display_id']} — {issue['summary']}",
        f"Status: {issue['status']} | Priority: {issue['priority']} | Type: {issue['issue_type']}"
        + (" | ARCHIVED" if issue.get("archived") else ""),
        f"Labels: {', '.join(issue.get('labels') or []) or 'none'}",
        f"Assignee: {issue.get('assignee') or 'unassigned'} | Reporter: {issue.get('reporter') or 'unknown'}",
        f"Milestone: {issue.get('milestone') or 'none'} | Due Date: {issue.get('due_date') or 'none'}",
        f"Created: {issue['created_at']} | Updated: {issue['updated_at']}"
        + (f" | Closed: {issue['closed_at']}" if issue.get("closed_at") else ""),
    ]
    if issue.get("cloned_from"):
        lines.append(f"Cloned From: #{issue['cloned_from']}")
    lines.append("")
    lines.append("Description:")
    lines.append(render_rich_html_for_cli(issue.get("description", "")))

    rels = issue.get("relationships", [])
    lines.append("")
    lines.append(f"Relationships ({len(rels)}):")
    if rels:
        for rel in rels:
            lines.append(
                f"  [{rel['id']}] {rel['relation_type']} {rel['target_display_id']} "
                f"({rel['target_summary']}) [{rel['target_status']}]"
            )
    else:
        lines.append("  (none)")

    comments = issue.get("comments", [])
    lines.append("")
    lines.append(f"Comments ({len(comments)}):")
    if comments:
        for c in comments:
            edited = f" [edited {c['updated_at']}]" if c.get("is_edited") else ""
            lines.append(f"  Comment #{c['id']} by {c['author']} ({c['created_at']}){edited}:")
            rendered = render_rich_html_for_cli(c.get("body", ""))
            for rline in rendered.splitlines() or [""]:
                lines.append(f"    {rline}")
    else:
        lines.append("  (none)")

    activity = issue.get("activity", [])
    lines.append("")
    lines.append(f"Activity ({len(activity)}):")
    if activity:
        for ev in activity:
            lines.append(f"  [{ev['id']}] {ev['created_at']} {ev['event_type']} by {ev['actor']}: {ev['summary']}")
    else:
        lines.append("  (none)")

    sys.stdout.write("\n".join(lines) + "\n")


def _print_paginated_list_human(res: dict[str, Any]) -> None:
    items = res["items"]
    sys.stdout.write(
        f"Issues (Page {res['page']} of {res['total_pages']}, showing {len(items)} of {res['total']} total):\n"
    )
    if not items:
        sys.stdout.write("  No matching issues found.\n")
        return
    for item in items:
        lbls = f" [{', '.join(item['labels'])}]" if item.get("labels") else ""
        assignee = item.get("assignee") or "-"
        arch = " [ARCHIVED]" if item.get("archived") else ""
        sys.stdout.write(
            f"  {item['display_id']}  {item['summary']}  "
            f"({item['status']} | {item['priority']} | {item['issue_type']}{lbls} | "
            f"assignee:{assignee} | comments:{item['comment_count']} | updated:{item['updated_at']}{arch})\n"
        )


def _print_server_result_human(res: dict[str, Any], action: str) -> None:
    if action == "start":
        stale_note = " (cleaned stale PID/metadata)" if res.get("stale_cleaned") else ""
        sys.stdout.write(
            f"iTrack server started{stale_note}: {res['url']} (pid={res['pid']}, data_dir={res['data_dir']}, log={res['log_file']})\n"
        )
        return
    if action == "restart":
        prev = f"previous_pid={res['previous_pid']}" if res.get("previous_pid") else "was stopped"
        sys.stdout.write(
            f"iTrack server restarted ({prev}): {res['url']} (pid={res['pid']}, data_dir={res['data_dir']}, log={res['log_file']})\n"
        )
        return
    if action == "stop":
        if res.get("stopped"):
            forced = " [forced]" if res.get("forced") else ""
            sys.stdout.write(
                f"iTrack server stopped ({res['signal']}{forced}, pid={res['pid']}, data_dir={res['data_dir']}).\n"
            )
        else:
            stale_note = " (cleaned stale PID/metadata)" if res.get("stale_cleaned") else ""
            sys.stdout.write(
                f"iTrack server is not running{stale_note} (data_dir={res['data_dir']}).\n"
            )
        return
    if action == "status":
        if res.get("running"):
            health = "healthy" if res.get("healthy") else "unhealthy"
            sys.stdout.write(
                f"iTrack server is running ({health}): {res.get('url') or 'unknown'} "
                f"(pid={res['pid']}, data_dir={res['data_dir']}, log={res['log_file']})\n"
            )
        else:
            stale_note = " (cleaned stale PID/metadata)" if res.get("stale_cleaned") else ""
            sys.stdout.write(
                f"iTrack server is stopped{stale_note} (data_dir={res['data_dir']}).\n"
            )


def _run_foreground_server(data_dir_arg: str | None, host: str, port: int, open_browser: bool) -> int:
    eff_host = _validate_host(host)
    eff_port = _validate_port(port)
    resolved = resolve_data_dir(data_dir_arg)
    app = create_app(resolved)
    url = f"http://{eff_host}:{eff_port}/"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=eff_host, port=eff_port, log_level="info")
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Execute CLI command and return POSIX exit code (0, 1, or 2)."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    wants_json_hint = "--json" in argv_list
    parser = build_parser()

    try:
        args = parser.parse_args(argv_list)
        if getattr(args, "version", False):
            if getattr(args, "json_output", False):
                _print_json({"version": VERSION})
            else:
                sys.stdout.write(f"itrack {VERSION}\n")
            return 0

        json_mode = bool(getattr(args, "json_output", False))
        data_dir_arg = getattr(args, "data_dir", None)
        actor = getattr(args, "actor", None) or os.environ.get("ITRACK_USER") or "anonymous"
        handler = getattr(args, "_handler", None)

        if handler in (None, "help"):
            if json_mode:
                _print_json({"version": VERSION, "features": FEATURES_CATALOG, "usage": parser.format_help()})
            else:
                sys.stdout.write(parser.format_help())
            return 0

        if handler == "features":
            if json_mode:
                _print_json({"features": FEATURES_CATALOG, "total": len(FEATURES_CATALOG)})
            else:
                sys.stdout.write("Supported iTrack Features:\n")
                for feat in FEATURES_CATALOG:
                    sys.stdout.write(f"- {feat['title']} ({feat['slug']})\n")
                    sys.stdout.write(f"  {feat['description']}\n")
                    sys.stdout.write(f"  CLI: {feat['cli_command']}\n")
                    sys.stdout.write(f"  UI:  {feat['ui_location']}\n")
            return 0

        if handler in ("issue_help", "link_help", "label_help", "bulk_help", "comment_help", "server_help"):
            raise ValidationError("Incomplete subcommand. Run with --help to see available subcommands.")

        if handler == "ui":
            if args.background:
                res = start_server(
                    data_dir_arg,
                    host=args.host,
                    port=args.port,
                    timeout=args.timeout,
                )
                if args.open_browser:
                    webbrowser.open(f"{res['url']}/")
                if json_mode:
                    _print_json(res)
                else:
                    _print_server_result_human(res, "start")
                return 0
            return _run_foreground_server(data_dir_arg, args.host, args.port, args.open_browser)

        if handler == "server_start":
            if args.foreground:
                return _run_foreground_server(data_dir_arg, args.host, args.port, args.open_browser)
            res = start_server(
                data_dir_arg,
                host=args.host,
                port=args.port,
                timeout=args.timeout,
            )
            if args.open_browser:
                webbrowser.open(f"{res['url']}/")
            if json_mode:
                _print_json(res)
            else:
                _print_server_result_human(res, "start")
            return 0

        if handler == "server_stop":
            res = stop_server(
                data_dir_arg,
                timeout=args.timeout,
                force=args.force,
            )
            if json_mode:
                _print_json(res)
            else:
                _print_server_result_human(res, "stop")
            return 0

        if handler == "server_status":
            res = get_server_status(data_dir_arg)
            if json_mode:
                _print_json(res)
            else:
                _print_server_result_human(res, "status")
            return 0

        if handler == "server_restart":
            res = restart_server(
                data_dir_arg,
                host=args.host,
                port=args.port,
                timeout=args.timeout,
                stop_timeout=args.stop_timeout,
            )
            if args.open_browser:
                webbrowser.open(f"{res['url']}/")
            if json_mode:
                _print_json(res)
            else:
                _print_server_result_human(res, "restart")
            return 0

        store = IssueStore(data_dir_arg)
        try:
            if handler == "create":
                raw_desc = _read_text_source(
                    args.description, args.description_file, args.description_stdin, "description"
                )
                imgs_html = _build_embedded_images_html(store, args.image, args.image_url, args.image_alt)
                full_desc = _combine_rich_text_and_images(raw_desc, imgs_html)
                all_labels = normalize_label_list([*(args.label_list or []), *( [args.labels_csv] if args.labels_csv else [] )])
                rels = _parse_link_specs(args.link)
                issue = store.create_issue(
                    summary=args.summary,
                    description=full_desc,  # type: ignore[arg-type]
                    status=args.status,
                    priority=args.priority,
                    issue_type=args.issue_type,
                    labels=all_labels,
                    assignee=args.assignee,
                    reporter=args.reporter or actor,
                    milestone=args.milestone,
                    due_date=args.due_date,
                    relationships=rels,
                    actor=actor,
                )
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler in ("list", "search"):
                search_q = args.query if handler == "search" else args.search
                res = store.list_issues(
                    page=args.page,
                    per_page=args.per_page,
                    status=args.status,
                    priority=args.priority,
                    issue_type=args.issue_type,
                    labels=args.labels,
                    assignee=args.assignee,
                    reporter=args.reporter,
                    milestone=args.milestone,
                    created_from=args.created_from,
                    created_to=args.created_to,
                    updated_from=args.updated_from,
                    updated_to=args.updated_to,
                    search=search_q,
                    sort_by=args.sort_by,
                    sort_order=args.sort_order,
                    include_archived=args.include_archived,
                    archived_only=args.archived_only,
                )
                if json_mode:
                    _print_json(res)
                else:
                    _print_paginated_list_human(res)
                return 0

            if handler == "show":
                issue = store.get_issue(args.issue_number)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "edit":
                raw_desc = _read_text_source(
                    args.description, args.description_file, args.description_stdin, "description"
                )
                imgs_html = _build_embedded_images_html(store, args.image, args.image_url, args.image_alt)
                if imgs_html and raw_desc is None:
                    existing = store.get_issue(args.issue_number)
                    desc_arg: str | None = f"{existing['description']}\n\n{imgs_html}"
                else:
                    desc_arg = _combine_rich_text_and_images(raw_desc, imgs_html)

                assignee_arg: Any = ...
                if args.unassign:
                    assignee_arg = None
                elif args.assignee is not None:
                    assignee_arg = args.assignee

                milestone_arg: Any = ...
                if args.clear_milestone:
                    milestone_arg = None
                elif args.milestone is not None:
                    milestone_arg = args.milestone

                due_arg: Any = ...
                if args.clear_due_date:
                    due_arg = None
                elif args.due_date is not None:
                    due_arg = args.due_date

                set_labels_arg: list[str] | None = None
                if args.clear_labels:
                    set_labels_arg = []
                elif args.set_labels is not None:
                    set_labels_arg = normalize_label_list(args.set_labels)

                issue = store.update_issue(
                    args.issue_number,
                    summary=args.summary,
                    description=desc_arg,
                    status=args.status,
                    priority=args.priority,
                    issue_type=args.issue_type,
                    assignee=assignee_arg,
                    reporter=args.reporter,
                    milestone=milestone_arg,
                    due_date=due_arg,
                    set_labels=set_labels_arg,
                    add_labels=args.add_label,
                    remove_labels=args.remove_label,
                    actor=actor,
                )
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "status":
                issue = store.update_issue(args.issue_number, status=args.status_value, actor=actor)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "close":
                issue = store.close_issue(args.issue_number, actor=actor)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "reopen":
                issue = store.reopen_issue(args.issue_number, actor=actor)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "clone":
                issue = store.clone_issue(args.issue_number, summary=args.summary, actor=actor)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "archive":
                issue = store.archive_issue(args.issue_number, actor=actor)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "restore":
                issue = store.restore_issue(args.issue_number, actor=actor)
                if json_mode:
                    _print_json(issue)
                else:
                    _print_issue_detail_human(issue)
                return 0

            if handler == "delete":
                if not args.yes:
                    raise ValidationError("Confirmation required: pass --yes to delete an issue.")
                res = store.delete_issue(args.issue_number, actor=actor)
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(f"Deleted issue #{res['number']}.\n")
                return 0

            if handler == "link_add":
                rel = store.add_relationship(
                    args.issue_number, args.relation_type, args.target_number, actor=actor
                )
                if json_mode:
                    _print_json(rel)
                else:
                    sys.stdout.write(
                        f"Added relationship [{rel['id']}]: #{rel['source_number']} {rel['relation_type']} {rel['target_display_id']}\n"
                    )
                return 0

            if handler == "link_remove":
                res = store.remove_relationship(
                    args.issue_number,
                    relationship_id=args.relationship_id,
                    relation_type=args.relation_type,
                    target_number=args.target_number,
                    actor=actor,
                )
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(f"Removed relationship [{res['id']}].\n")
                return 0

            if handler == "link_list":
                rels = store.list_relationships(args.issue_number)
                num = parse_issue_number(args.issue_number)
                if json_mode:
                    _print_json({"issue_number": num, "relationships": rels})
                else:
                    sys.stdout.write(f"Relationships for #{num} ({len(rels)}):\n")
                    for rel in rels:
                        sys.stdout.write(
                            f"  [{rel['id']}] {rel['relation_type']} {rel['target_display_id']} "
                            f"({rel['target_summary']}) [{rel['target_status']}]\n"
                        )
                return 0

            if handler == "label_list":
                labels = store.list_labels()
                if json_mode:
                    _print_json({"labels": labels})
                else:
                    sys.stdout.write(f"Labels ({len(labels)}):\n")
                    for lbl in labels:
                        sys.stdout.write(f"  {lbl['name']} ({lbl['color']}) — {lbl['issue_count']} issues\n")
                return 0

            if handler == "label_create":
                lbl = store.create_label(args.name, color=args.color)
                if json_mode:
                    _print_json(lbl)
                else:
                    sys.stdout.write(f"Created label '{lbl['name']}' ({lbl['color']}).\n")
                return 0

            if handler == "label_rename":
                lbl = store.rename_label(args.old_name, args.new_name, color=args.color, actor=actor)
                if json_mode:
                    _print_json(lbl)
                else:
                    sys.stdout.write(f"Updated label '{lbl['name']}' ({lbl['color']}).\n")
                return 0

            if handler == "label_delete":
                if not args.yes:
                    raise ValidationError("Confirmation required: pass --yes to delete a label.")
                res = store.delete_label(args.name, actor=actor)
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(f"Deleted label '{res['name']}'.\n")
                return 0

            if handler == "bulk_update":
                assignee_arg = ...
                if args.unassign:
                    assignee_arg = None
                elif args.assignee is not None:
                    assignee_arg = args.assignee
                set_lbls: list[str] | None = None
                if args.clear_labels:
                    set_lbls = []
                elif args.set_labels is not None:
                    set_lbls = normalize_label_list(args.set_labels)
                res = store.bulk_update(
                    args.issue_numbers,
                    status=args.status,
                    priority=args.priority,
                    assignee=assignee_arg,
                    add_labels=args.add_label,
                    remove_labels=args.remove_label,
                    set_labels=set_lbls,
                    actor=actor,
                )
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(
                        f"Bulk updated {res['affected_count']} issues: {', '.join(f'#{n}' for n in res['issue_numbers'])}\n"
                    )
                return 0

            if handler == "bulk_delete":
                if not args.yes:
                    raise ValidationError("Confirmation required: pass --yes to bulk delete issues.")
                res = store.bulk_delete(args.issue_numbers, actor=actor)
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(
                        f"Bulk deleted {res['affected_count']} issues: {', '.join(f'#{n}' for n in res['issue_numbers'])}\n"
                    )
                return 0

            if handler == "comment_add":
                raw_body = _read_text_source(args.body, args.body_file, args.stdin, "comment body")
                imgs_html = _build_embedded_images_html(store, args.image, args.image_url, args.image_alt)
                full_body = _combine_rich_text_and_images(raw_body, imgs_html)
                cmt = store.add_comment(
                    args.issue_number,
                    full_body,  # type: ignore[arg-type]
                    author=args.author or actor,
                )
                if json_mode:
                    _print_json(cmt)
                else:
                    sys.stdout.write(
                        f"Comment #{cmt['id']} added to #{cmt['issue_number']} by {cmt['author']}:\n"
                        f"{render_rich_html_for_cli(cmt['body'])}\n"
                    )
                return 0

            if handler == "comment_list":
                num = parse_issue_number(args.issue_number)
                cmts = store.list_comments(num)
                if json_mode:
                    _print_json({"issue_number": num, "comments": cmts})
                else:
                    sys.stdout.write(f"Comments for #{num} ({len(cmts)}):\n")
                    for c in cmts:
                        edited = f" [edited {c['updated_at']}]" if c.get("is_edited") else ""
                        sys.stdout.write(f"  Comment #{c['id']} by {c['author']} ({c['created_at']}){edited}:\n")
                        for line in render_rich_html_for_cli(c["body"]).splitlines() or [""]:
                            sys.stdout.write(f"    {line}\n")
                return 0

            if handler == "comment_show":
                cmt = store.get_comment(args.issue_number, args.comment_id)
                if json_mode:
                    _print_json(cmt)
                else:
                    edited = f" [edited {cmt['updated_at']}]" if cmt.get("is_edited") else ""
                    sys.stdout.write(
                        f"Comment #{cmt['id']} on #{cmt['issue_number']} by {cmt['author']} ({cmt['created_at']}){edited}:\n"
                        f"{render_rich_html_for_cli(cmt['body'])}\n"
                    )
                return 0

            if handler == "comment_edit":
                raw_body = _read_text_source(args.body, args.body_file, args.stdin, "comment body")
                imgs_html = _build_embedded_images_html(store, args.image, args.image_url, args.image_alt)
                if imgs_html and raw_body is None:
                    existing_cmt = store.get_comment(args.issue_number, args.comment_id)
                    body_arg: str | None = f"{existing_cmt['body']}\n\n{imgs_html}"
                else:
                    body_arg = _combine_rich_text_and_images(raw_body, imgs_html)
                cmt = store.update_comment(
                    args.issue_number,
                    args.comment_id,
                    body_arg,  # type: ignore[arg-type]
                    actor=args.author or actor,
                )
                if json_mode:
                    _print_json(cmt)
                else:
                    sys.stdout.write(
                        f"Comment #{cmt['id']} on #{cmt['issue_number']} updated ({cmt['updated_at']}):\n"
                        f"{render_rich_html_for_cli(cmt['body'])}\n"
                    )
                return 0

            if handler == "comment_delete":
                if not args.yes:
                    raise ValidationError("Confirmation required: pass --yes to delete a comment.")
                res = store.delete_comment(args.issue_number, args.comment_id, actor=actor)
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(f"Deleted comment #{res['id']} on issue #{res['issue_number']}.\n")
                return 0

            if handler == "history":
                num = parse_issue_number(args.issue_number)
                events = store.get_activity_log(num)
                if json_mode:
                    _print_json({"issue_number": num, "activity": events})
                else:
                    sys.stdout.write(f"Activity history for #{num} ({len(events)}):\n")
                    for ev in events:
                        sys.stdout.write(
                            f"  [{ev['id']}] {ev['created_at']} {ev['event_type']} by {ev['actor']}: {ev['summary']}\n"
                        )
                return 0

            if handler == "export":
                filter_kwargs: dict[str, Any] = {
                    "status": args.status,
                    "priority": args.priority,
                    "issue_type": args.issue_type,
                    "labels": args.labels,
                    "assignee": args.assignee,
                    "reporter": args.reporter,
                    "milestone": args.milestone,
                    "created_from": args.created_from,
                    "created_to": args.created_to,
                    "updated_from": args.updated_from,
                    "updated_to": args.updated_to,
                    "search": args.search,
                    "sort_by": args.sort_by,
                    "sort_order": args.sort_order,
                    "archived_only": args.archived_only,
                }
                if args.include_archived:
                    filter_kwargs["include_archived"] = True
                exported = export_issues(
                    store,
                    fmt=args.fmt,
                    issue_number=args.issue,
                    filter_kwargs=filter_kwargs,
                )
                if args.output:
                    out_path = Path(args.output)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(exported, encoding="utf-8")
                    if json_mode:
                        _print_json({"exported": True, "format": args.fmt, "output": str(out_path)})
                    else:
                        sys.stdout.write(f"Exported ({args.fmt}) to {out_path}\n")
                else:
                    if json_mode and args.fmt != "json":
                        _print_json({"format": args.fmt, "content": exported})
                    else:
                        sys.stdout.write(exported + ("" if exported.endswith("\n") else "\n"))
                return 0

            if handler == "import":
                if args.stdin:
                    raw_payload = sys.stdin.read()
                elif args.path:
                    p = Path(args.path)
                    if not p.is_file():
                        raise ValidationError(f"Import file not found: {args.path}")
                    raw_payload = p.read_text(encoding="utf-8")
                else:
                    raise ValidationError("Provide an import JSON file path or pass --stdin.")
                res = import_issues_json(store, raw_payload, actor=actor)
                if json_mode:
                    _print_json(res)
                else:
                    sys.stdout.write(
                        f"Imported {res['imported_count']} issues: {', '.join(f'#{n}' for n in res['imported_numbers'])}\n"
                    )
                return 0

            raise ValidationError(f"Unknown command handler: {handler}")
        finally:
            store.close()

    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    except ITrackError as err:
        if wants_json_hint:
            sys.stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "error": err.code,
                        "message": err.message,
                        "exit_code": err.exit_code,
                    }
                )
                + "\n"
            )
        sys.stderr.write(f"Error: {err.message}\n")
        return err.exit_code
