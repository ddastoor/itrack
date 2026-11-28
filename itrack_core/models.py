"""Canonical constants, exceptions, validators, and feature catalog for iTrack."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

VERSION: str = "1.0.0"

VALID_STATUSES: tuple[str, ...] = ("open", "in_progress", "blocked", "resolved", "closed")
DEFAULT_STATUS: str = "open"
STATUS_SORT_RANK: dict[str, int] = {
    "open": 1,
    "in_progress": 2,
    "blocked": 3,
    "resolved": 4,
    "closed": 5,
}

VALID_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
DEFAULT_PRIORITY: str = "medium"
PRIORITY_SORT_RANK: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

VALID_ISSUE_TYPES: tuple[str, ...] = ("bug", "feature", "task", "improvement")
DEFAULT_ISSUE_TYPE: str = "task"

VALID_RELATIONSHIP_TYPES: tuple[str, ...] = ("blocks", "blocked_by", "relates_to", "duplicates")
INVERSE_RELATIONSHIP_MAP: dict[str, str] = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "relates_to": "relates_to",
    "duplicates": "duplicated_by",
    "duplicated_by": "duplicates",
}

VALID_SORT_FIELDS: tuple[str, ...] = (
    "number",
    "created_at",
    "updated_at",
    "priority",
    "status",
    "summary",
)
SORT_FIELD_ALIASES: dict[str, str] = {
    "id": "number",
    "number": "number",
    "created": "created_at",
    "created_at": "created_at",
    "updated": "updated_at",
    "updated_at": "updated_at",
    "priority": "priority",
    "status": "status",
    "summary": "summary",
}
VALID_SORT_ORDERS: tuple[str, ...] = ("asc", "desc")
VALID_EXPORT_FORMATS: tuple[str, ...] = ("json", "csv", "markdown")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ITrackError(Exception):
    """Base domain exception with structured code, CLI exit code, and HTTP status."""

    def __init__(
        self,
        message: str,
        code: str = "itrack_error",
        exit_code: int = 1,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.status_code = status_code


class ValidationError(ITrackError):
    """Raised on invalid/missing inputs, empty descriptions/comments, or missing confirmation."""

    def __init__(self, message: str, code: str = "validation_error") -> None:
        super().__init__(message=message, code=code, exit_code=1, status_code=400)


class ConflictError(ITrackError):
    """Raised on duplicate/self relationships or duplicate label collisions."""

    def __init__(self, message: str, code: str = "conflict_error") -> None:
        super().__init__(message=message, code=code, exit_code=1, status_code=409)


class NotFoundError(ITrackError):
    """Raised when an issue, comment, relationship, label, or asset does not exist."""

    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(message=message, code=code, exit_code=2, status_code=404)


class ServerError(ITrackError):
    """Raised when starting, stopping, or locating the background server fails."""

    def __init__(self, message: str, code: str = "server_error", exit_code: int = 1) -> None:
        super().__init__(message=message, code=code, exit_code=exit_code, status_code=500)


def utc_now_iso() -> str:
    """Return current UTC timestamp formatted with microsecond precision and Z suffix."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_issue_number(raw: int | str) -> int:
    """Accept positive int or '#<int>' string; raise ValidationError otherwise."""
    if isinstance(raw, bool):
        raise ValidationError("Issue number must be a positive integer.")
    if isinstance(raw, int):
        if raw <= 0:
            raise ValidationError(f"Invalid issue number '{raw}': must be a positive integer.")
        return raw
    if not isinstance(raw, str):
        raise ValidationError("Issue number must be a positive integer.")
    cleaned = raw.strip()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:].strip()
    if not cleaned or not cleaned.isdigit():
        raise ValidationError(f"Invalid issue number '{raw}': expected positive integer or '#N'.")
    val = int(cleaned)
    if val <= 0:
        raise ValidationError(f"Invalid issue number '{raw}': must be greater than zero.")
    return val


def parse_positive_int(raw: int | str, field_name: str = "id") -> int:
    """Parse a positive integer identifier or raise ValidationError."""
    if isinstance(raw, bool):
        raise ValidationError(f"Invalid {field_name}: must be a positive integer.")
    if isinstance(raw, int):
        if raw <= 0:
            raise ValidationError(f"Invalid {field_name} '{raw}': must be a positive integer.")
        return raw
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned.isdigit():
            val = int(cleaned)
            if val > 0:
                return val
    raise ValidationError(f"Invalid {field_name} '{raw}': must be a positive integer.")


def validate_summary(raw: str | None) -> str:
    """Require non-empty single-line summary without newline or carriage return characters."""
    if raw is None or not isinstance(raw, str):
        raise ValidationError("Summary is required and must be a non-empty single line.")
    if "\n" in raw or "\r" in raw:
        raise ValidationError("Summary must be a single line (newlines are not allowed).")
    cleaned = raw.strip()
    if not cleaned:
        raise ValidationError("Summary cannot be empty or whitespace-only.")
    return cleaned


def validate_iso_date(raw: str | None, field_name: str = "due_date") -> str | None:
    """Return None for None/'none'/'clear'/'', or validated 'YYYY-MM-DD' string."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationError(f"{field_name} must be a YYYY-MM-DD date string.")
    cleaned = raw.strip()
    if cleaned.lower() in ("", "none", "clear"):
        return None
    if not _ISO_DATE_RE.match(cleaned):
        raise ValidationError(f"Invalid {field_name} '{raw}': expected YYYY-MM-DD format.")
    try:
        dt.date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValidationError(f"Invalid {field_name} '{raw}': {exc}") from exc
    return cleaned


def normalize_filter_bound(raw: str | None, *, is_upper: bool, field_name: str) -> str | None:
    """Normalize a date (YYYY-MM-DD) or ISO-8601 timestamp into UTC comparison string."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationError(f"{field_name} must be a date or ISO timestamp string.")
    cleaned = raw.strip()
    if not cleaned:
        return None
    if _ISO_DATE_RE.match(cleaned):
        try:
            dt.date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValidationError(f"Invalid {field_name} '{raw}': {exc}") from exc
        return f"{cleaned}T23:59:59.999999Z" if is_upper else f"{cleaned}T00:00:00.000000Z"
    iso_candidate = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    try:
        parsed = dt.datetime.fromisoformat(iso_candidate)
    except ValueError as exc:
        raise ValidationError(f"Invalid {field_name} '{raw}': expected YYYY-MM-DD or ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    else:
        parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_enum(value: str, allowed: tuple[str, ...], field_name: str) -> str:
    """Validate that value belongs to allowed enum tuple."""
    if not isinstance(value, str):
        raise ValidationError(f"Invalid {field_name}: expected one of {', '.join(allowed)}.")
    cleaned = value.strip().lower()
    if cleaned not in allowed:
        raise ValidationError(
            f"Invalid {field_name} '{value}': must be one of {', '.join(allowed)}."
        )
    return cleaned


def validate_sort_by(raw: str | None) -> str:
    """Resolve and validate sort field name."""
    if raw is None:
        return "number"
    if not isinstance(raw, str):
        raise ValidationError("Invalid sort_by value.")
    key = raw.strip().lower()
    if key not in SORT_FIELD_ALIASES:
        raise ValidationError(
            f"Invalid sort_by '{raw}': must be one of {', '.join(VALID_SORT_FIELDS)}."
        )
    return SORT_FIELD_ALIASES[key]


def validate_sort_order(raw: str | None) -> str:
    """Validate sort order ('asc' or 'desc')."""
    if raw is None:
        return "asc"
    return validate_enum(raw, VALID_SORT_ORDERS, "sort_order")


def validate_optional_text(raw: str | None) -> str | None:
    """Normalize optional metadata text; empty/whitespace becomes None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationError("Expected string value.")
    cleaned = raw.strip()
    return cleaned if cleaned else None


def validate_label_name(raw: str) -> str:
    """Validate and normalize a label name."""
    if not isinstance(raw, str):
        raise ValidationError("Label name must be a string.")
    if "\n" in raw or "\r" in raw:
        raise ValidationError("Label name must be single-line.")
    cleaned = raw.strip()
    if not cleaned:
        raise ValidationError("Label name cannot be empty.")
    if "," in cleaned:
        raise ValidationError("Label name cannot contain commas.")
    return cleaned


def validate_hex_color(raw: str | None, default: str = "#3b82f6") -> str:
    """Validate CSS hex color string."""
    if raw is None or not str(raw).strip():
        return default
    cleaned = str(raw).strip()
    if not _HEX_COLOR_RE.match(cleaned):
        raise ValidationError(f"Invalid color '{raw}': expected #RGB or #RRGGBB hex color.")
    return cleaned.lower()


def normalize_label_list(raw: Any) -> list[str]:
    """Normalize sequence or comma-separated string of labels preserving unique order."""
    if raw is None:
        return []
    items: list[str] = []
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    else:
        candidates = []
        for entry in raw:
            if isinstance(entry, str) and "," in entry:
                candidates.extend(part.strip() for part in entry.split(","))
            elif entry is not None:
                candidates.append(str(entry).strip())
    seen: set[str] = set()
    for cand in candidates:
        if not cand:
            continue
        cleaned = validate_label_name(cand)
        if cleaned not in seen:
            seen.add(cleaned)
            items.append(cleaned)
    return items


FEATURES_CATALOG: list[dict[str, str]] = [
    {
        "slug": "issue-creation",
        "title": "Auto-Numbered Issue Creation & Mandatory Validation",
        "description": "Creates sequential #1, #2, ... issues with mandatory one-line summary, mandatory rich-text description, status, priority, type, labels, assignee, reporter, milestone, due date, and initial links.",
        "ui_location": "Top bar -> New Issue button (create-issue-modal)",
        "cli_command": "itrack.py create --summary <TEXT> --description <HTML_OR_MD> [--priority ...] [--type ...]",
    },
    {
        "slug": "paginated-listing",
        "title": "Paginated Issue Listing",
        "description": "Lists issues with configurable page size, first/prev/next/last page navigation, total count, and total pages, always showing #number and summary plus metadata.",
        "ui_location": "Issues list table & pagination bar",
        "cli_command": "itrack.py list [--page N] [--per-page N] [--json]",
    },
    {
        "slug": "multi-field-filtering",
        "title": "Multi-Field Filtering",
        "description": "Filters issues by status, priority, issue type, labels, assignee, reporter, milestone, created/updated date ranges, and archived state.",
        "ui_location": "Filter bar above issue list",
        "cli_command": "itrack.py list [--status ...] [--priority ...] [--type ...] [--label ...] [--assignee ...]",
    },
    {
        "slug": "full-text-search",
        "title": "Full-Text Search",
        "description": "Searches across issue number, summary, rich-text description text, image alt text, and comments while preserving pagination.",
        "ui_location": "Search bar at top of Issues list",
        "cli_command": "itrack.py search <QUERY> [--page N] [--per-page N]",
    },
    {
        "slug": "multi-field-sorting",
        "title": "Multi-Field Sorting",
        "description": "Sorts ascending or descending by issue number, created_at, updated_at, priority rank, workflow status rank, or summary.",
        "ui_location": "Sort by & Sort order controls",
        "cli_command": "itrack.py list --sort-by <FIELD> --sort-order asc|desc",
    },
    {
        "slug": "issue-detail-and-lookup",
        "title": "Issue Selection, Direct Lookup & Detail View",
        "description": "Opens any issue by #number showing full rich-text description, metadata, linked issues, chronological comments, and activity audit log with explicit not-found errors.",
        "ui_location": "Click any issue row or use Jump to #N in header",
        "cli_command": "itrack.py show <ISSUE_NUMBER> [--json]",
    },
    {
        "slug": "edit-and-workflow",
        "title": "Issue Editing & Workflow Transitions",
        "description": "Edits summary, rich-text description, metadata, and labels with validation; transitions status including dedicated close and reopen actions.",
        "ui_location": "Issue Detail view -> Edit / Status / Close / Reopen controls",
        "cli_command": "itrack.py edit <N> ... | itrack.py status <N> <STATUS> | itrack.py close <N> | itrack.py reopen <N>",
    },
    {
        "slug": "relationships",
        "title": "Issue Relationships",
        "description": "Links and unlinks issues using blocks, blocked_by, relates_to, and duplicates with bidirectional visibility and conflict detection.",
        "ui_location": "Issue Detail view -> Relationships section",
        "cli_command": "itrack.py link add <N> <REL_TYPE> <TARGET> | remove | list",
    },
    {
        "slug": "labels-management",
        "title": "Labels Management",
        "description": "Lists, creates, renames, recolors, and deletes labels globally while keeping attached issues synchronized.",
        "ui_location": "Top bar -> Labels button (labels-manager)",
        "cli_command": "itrack.py label list | create | rename | delete",
    },
    {
        "slug": "clone-archive-delete",
        "title": "Clone, Archive, Restore & Confirmed Delete",
        "description": "Clones an issue into a new sequential #number, archives/restores issues, and permanently deletes issues with confirmation and asset cleanup.",
        "ui_location": "Issue Detail view -> Clone / Archive / Restore / Delete buttons",
        "cli_command": "itrack.py clone <N> | archive <N> | restore <N> | delete <N> --yes",
    },
    {
        "slug": "bulk-operations",
        "title": "Bulk Operations",
        "description": "Selects multiple issues to bulk-update status, priority, assignee, or labels, or bulk-delete with confirmation.",
        "ui_location": "Row checkboxes -> Bulk Actions Bar",
        "cli_command": "itrack.py bulk update <N...> ... | itrack.py bulk delete <N...> --yes",
    },
    {
        "slug": "rich-text-and-images",
        "title": "Rich-Text Formatting, Links, Embedded Images & XSS Sanitization",
        "description": "Supports bold, italic, underline, strikethrough, inline/block code, headings, blockquotes, lists, http(s)/mailto links, uploaded/data-URI/URL images, and strict XSS sanitization.",
        "ui_location": "WYSIWYG + HTML Rich-Text Editor toolbar in descriptions and comments",
        "cli_command": "itrack.py create / edit / comment add / comment edit with HTML/Markdown, --image, --image-url",
    },
    {
        "slug": "rich-text-comments",
        "title": "Rich-Text Comments (Add, Edit & Delete)",
        "description": "Adds, lists, edits by comment ID (with edited indicator and timestamp), and deletes rich-text comments on any issue.",
        "ui_location": "Issue Detail view -> Comments composer and thread",
        "cli_command": "itrack.py comment add|list|show|edit|delete <N> ...",
    },
    {
        "slug": "activity-audit-log",
        "title": "Per-Issue Activity Audit Trail",
        "description": "Automatically records and displays creation, field edits, workflow changes, relationships, and comment lifecycle events.",
        "ui_location": "Issue Detail view -> Activity History section",
        "cli_command": "itrack.py history <ISSUE_NUMBER> [--json]",
    },
    {
        "slug": "export-and-import",
        "title": "Export (JSON/CSV/Markdown) & JSON Import",
        "description": "Exports all, filtered, or single issues (including portable embedded image assets) to JSON, CSV, or Markdown and imports JSON payloads.",
        "ui_location": "Top bar -> Export / Import button (export-import-panel)",
        "cli_command": "itrack.py export --format json|csv|markdown | itrack.py import <PATH>",
    },
    {
        "slug": "features-discovery",
        "title": "Supported Features & Help Discovery",
        "description": "Lists every supported capability with UI location and matching CLI command alongside issue listing.",
        "ui_location": "Top bar -> Features / Help button (features-view)",
        "cli_command": "itrack.py features [--json] | itrack.py help",
    },
    {
        "slug": "background-server-management",
        "title": "Background Server Lifecycle Management",
        "description": "Starts, inspects, restarts, and stops the local iTrack HTTP & Web UI server as a detached background daemon with readiness health checks, PID/metadata tracking, and log redirection.",
        "ui_location": "Served locally at http://<host>:<port>/ (managed via CLI)",
        "cli_command": "itrack.py server start|stop|status|restart | itrack.py start|stop | itrack.py ui --background",
    },
]
