"""FastAPI application factory exposing iTrack REST API, assets, export/import, and SPA UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from itrack_core.exporter import export_issues, import_issues_json
from itrack_core.models import (
    FEATURES_CATALOG,
    ITrackError,
    VERSION,
    ValidationError,
    validate_enum,
    VALID_EXPORT_FORMATS,
)
from itrack_core.storage import IssueStore
from itrack_core.ui import render_spa_html

_UNSET = object()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message, "detail": message},
    )


async def _read_json_dict(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if not raw or not raw.strip():
        if allow_empty:
            return {}
        raise ValidationError("Request body must be a valid JSON object.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Malformed JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Request JSON body must be an object.")
    return parsed


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    """Create and configure FastAPI app backed by IssueStore(data_dir)."""
    store = IssueStore(data_dir)
    app = FastAPI(title="iTrack Issue Tracker", version="1.0.0")
    app.state.store = store

    @app.exception_handler(ITrackError)
    async def _handle_itrack_error(_: Request, exc: ITrackError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(400, "validation_error", f"Invalid request parameters: {exc.errors()}")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui", response_class=HTMLResponse)
    async def serve_ui() -> HTMLResponse:
        return HTMLResponse(content=render_spa_html(), status_code=200)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "data_dir": str(store.data_dir),
            "pid": os.getpid(),
            "version": VERSION,
        }

    @app.get("/api/features")
    async def list_features() -> dict[str, Any]:
        return {"features": FEATURES_CATALOG, "total": len(FEATURES_CATALOG)}

    @app.get("/api/meta")
    async def get_meta() -> dict[str, Any]:
        return store.get_metadata_options()

    @app.post("/api/assets", status_code=201)
    async def upload_asset(
        file: UploadFile = File(...),
        alt: str = Form(""),
    ) -> dict[str, Any]:
        content = await file.read()
        return store.save_asset(
            content,
            filename=file.filename or "upload.png",
            mime_type=file.content_type,
            alt=alt,
        )

    @app.get("/api/assets/{asset_id}")
    async def download_asset(asset_id: str) -> FileResponse:
        file_path, meta = store.get_asset(asset_id)
        return FileResponse(
            path=str(file_path),
            media_type=meta["mime_type"],
            filename=meta["original_filename"],
        )

    @app.get("/api/issues")
    async def list_issues_endpoint(
        page: int = Query(1),
        per_page: int = Query(10),
        page_size: int | None = Query(None),
        status: list[str] | None = Query(None),
        priority: list[str] | None = Query(None),
        issue_type: list[str] | None = Query(None),
        type: list[str] | None = Query(None),
        label: list[str] | None = Query(None),
        assignee: str | None = Query(None),
        reporter: str | None = Query(None),
        milestone: str | None = Query(None),
        created_from: str | None = Query(None),
        created_to: str | None = Query(None),
        updated_from: str | None = Query(None),
        updated_to: str | None = Query(None),
        search: str | None = Query(None),
        q: str | None = Query(None),
        sort_by: str = Query("number"),
        sort_order: str = Query("asc"),
        include_archived: bool = Query(False),
        archived_only: bool = Query(False),
    ) -> dict[str, Any]:
        eff_per_page = page_size if page_size is not None else per_page
        eff_type = issue_type if issue_type is not None else type
        eff_search = search if search is not None else q
        return store.list_issues(
            page=page,
            per_page=eff_per_page,
            status=status,
            priority=priority,
            issue_type=eff_type,
            labels=label,
            assignee=assignee,
            reporter=reporter,
            milestone=milestone,
            created_from=created_from,
            created_to=created_to,
            updated_from=updated_from,
            updated_to=updated_to,
            search=eff_search,
            sort_by=sort_by,
            sort_order=sort_order,
            include_archived=include_archived,
            archived_only=archived_only,
        )

    @app.post("/api/issues", status_code=201)
    async def create_issue_endpoint(request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        return store.create_issue(
            summary=payload.get("summary"),
            description=payload.get("description"),
            status=payload.get("status") or "open",
            priority=payload.get("priority") or "medium",
            issue_type=payload.get("issue_type") or payload.get("type") or "task",
            labels=payload.get("labels"),
            assignee=payload.get("assignee"),
            reporter=payload.get("reporter"),
            milestone=payload.get("milestone"),
            due_date=payload.get("due_date"),
            relationships=payload.get("relationships"),
            actor=payload.get("actor"),
        )

    @app.post("/api/issues/bulk")
    async def bulk_issues_endpoint(request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        action = str(payload.get("action") or "").strip().lower()
        issue_numbers = payload.get("issue_numbers")
        if not isinstance(issue_numbers, list):
            raise ValidationError("'issue_numbers' must be a list of issue numbers.")
        actor = payload.get("actor")

        if action == "delete":
            return store.bulk_delete(issue_numbers, actor=actor)
        if action == "update":
            assignee_arg: Any = ...
            if payload.get("unassign"):
                assignee_arg = None
            elif "assignee" in payload:
                assignee_arg = payload.get("assignee")
            return store.bulk_update(
                issue_numbers,
                status=payload.get("status"),
                priority=payload.get("priority"),
                assignee=assignee_arg,
                add_labels=payload.get("add_labels"),
                remove_labels=payload.get("remove_labels"),
                set_labels=payload.get("set_labels"),
                actor=actor,
            )
        raise ValidationError("Bulk action must be 'update' or 'delete'.")

    @app.get("/api/issues/{number}")
    async def get_issue_endpoint(number: str) -> dict[str, Any]:
        return store.get_issue(number)

    @app.patch("/api/issues/{number}")
    async def update_issue_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        assignee_arg: Any = ...
        if payload.get("unassign"):
            assignee_arg = None
        elif "assignee" in payload:
            assignee_arg = payload.get("assignee")

        milestone_arg: Any = ...
        if payload.get("clear_milestone"):
            milestone_arg = None
        elif "milestone" in payload:
            milestone_arg = payload.get("milestone")

        due_date_arg: Any = ...
        if payload.get("clear_due_date"):
            due_date_arg = None
        elif "due_date" in payload:
            due_date_arg = payload.get("due_date")

        issue_type_val = payload.get("issue_type")
        if issue_type_val is None and "type" in payload:
            issue_type_val = payload.get("type")

        return store.update_issue(
            number,
            summary=payload.get("summary"),
            description=payload.get("description"),
            status=payload.get("status"),
            priority=payload.get("priority"),
            issue_type=issue_type_val,
            assignee=assignee_arg,
            reporter=payload.get("reporter"),
            milestone=milestone_arg,
            due_date=due_date_arg,
            set_labels=payload.get("set_labels"),
            add_labels=payload.get("add_labels"),
            remove_labels=payload.get("remove_labels"),
            actor=payload.get("actor"),
        )

    @app.post("/api/issues/{number}/close")
    async def close_issue_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request, allow_empty=True)
        return store.close_issue(number, actor=payload.get("actor"))

    @app.post("/api/issues/{number}/reopen")
    async def reopen_issue_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request, allow_empty=True)
        return store.reopen_issue(number, actor=payload.get("actor"))

    @app.post("/api/issues/{number}/clone", status_code=201)
    async def clone_issue_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request, allow_empty=True)
        return store.clone_issue(number, summary=payload.get("summary"), actor=payload.get("actor"))

    @app.post("/api/issues/{number}/archive")
    async def archive_issue_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request, allow_empty=True)
        return store.archive_issue(number, actor=payload.get("actor"))

    @app.post("/api/issues/{number}/restore")
    async def restore_issue_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request, allow_empty=True)
        return store.restore_issue(number, actor=payload.get("actor"))

    @app.delete("/api/issues/{number}")
    async def delete_issue_endpoint(number: str) -> dict[str, Any]:
        return store.delete_issue(number)

    @app.get("/api/issues/{number}/relationships")
    async def list_relationships_endpoint(number: str) -> dict[str, Any]:
        issue = store.get_issue(number)
        return {"issue_number": issue["number"], "relationships": issue["relationships"]}

    @app.post("/api/issues/{number}/relationships", status_code=201)
    async def add_relationship_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        return store.add_relationship(
            number,
            relation_type=str(payload.get("relation_type") or ""),
            target_number=payload.get("target_number", ""),
            actor=payload.get("actor"),
        )

    @app.delete("/api/issues/{number}/relationships/{relationship_id}")
    async def delete_relationship_endpoint(number: str, relationship_id: int) -> dict[str, Any]:
        return store.remove_relationship(number, relationship_id=relationship_id)

    @app.get("/api/issues/{number}/comments")
    async def list_comments_endpoint(number: str) -> dict[str, Any]:
        issue = store.get_issue(number)
        return {"issue_number": issue["number"], "comments": issue["comments"]}

    @app.post("/api/issues/{number}/comments", status_code=201)
    async def add_comment_endpoint(number: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        return store.add_comment(number, payload.get("body"), author=payload.get("author"))

    @app.get("/api/issues/{number}/comments/{comment_id}")
    async def get_comment_endpoint(number: str, comment_id: str) -> dict[str, Any]:
        return store.get_comment(number, comment_id)

    @app.patch("/api/issues/{number}/comments/{comment_id}")
    async def update_comment_endpoint(number: str, comment_id: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        return store.update_comment(
            number,
            comment_id,
            payload.get("body"),
            actor=payload.get("actor"),
        )

    @app.delete("/api/issues/{number}/comments/{comment_id}")
    async def delete_comment_endpoint(number: str, comment_id: str) -> dict[str, Any]:
        return store.delete_comment(number, comment_id)

    @app.get("/api/issues/{number}/history")
    async def get_history_endpoint(number: str) -> dict[str, Any]:
        issue = store.get_issue(number)
        return {"issue_number": issue["number"], "activity": issue["activity"]}

    @app.get("/api/labels")
    async def list_labels_endpoint() -> dict[str, Any]:
        return {"labels": store.list_labels()}

    @app.post("/api/labels", status_code=201)
    async def create_label_endpoint(request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        return store.create_label(
            name=str(payload.get("name") or ""),
            color=str(payload.get("color") or "#3b82f6"),
        )

    @app.patch("/api/labels/{label_name}")
    async def rename_label_endpoint(label_name: str, request: Request) -> dict[str, Any]:
        payload = await _read_json_dict(request)
        new_name = str(payload.get("new_name") or label_name)
        color = payload.get("color")
        return store.rename_label(label_name, new_name, color=color, actor=payload.get("actor"))

    @app.delete("/api/labels/{label_name}")
    async def delete_label_endpoint(label_name: str) -> dict[str, Any]:
        return store.delete_label(label_name)

    @app.get("/api/export")
    async def export_endpoint(
        format: str = Query("json"),
        issue: str | None = Query(None),
        status: list[str] | None = Query(None),
        priority: list[str] | None = Query(None),
        issue_type: list[str] | None = Query(None),
        type: list[str] | None = Query(None),
        label: list[str] | None = Query(None),
        assignee: str | None = Query(None),
        reporter: str | None = Query(None),
        milestone: str | None = Query(None),
        created_from: str | None = Query(None),
        created_to: str | None = Query(None),
        updated_from: str | None = Query(None),
        updated_to: str | None = Query(None),
        search: str | None = Query(None),
        q: str | None = Query(None),
        sort_by: str = Query("number"),
        sort_order: str = Query("asc"),
        include_archived: bool | None = Query(None),
        archived_only: bool = Query(False),
    ) -> Response:
        fmt = validate_enum(format, VALID_EXPORT_FORMATS, "format")
        filter_kwargs: dict[str, Any] = {
            "status": status,
            "priority": priority,
            "issue_type": issue_type if issue_type is not None else type,
            "labels": label,
            "assignee": assignee,
            "reporter": reporter,
            "milestone": milestone,
            "created_from": created_from,
            "created_to": created_to,
            "updated_from": updated_from,
            "updated_to": updated_to,
            "search": search if search is not None else q,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "archived_only": archived_only,
        }
        if include_archived is not None:
            filter_kwargs["include_archived"] = include_archived

        content = export_issues(
            store,
            fmt=fmt,  # type: ignore[arg-type]
            issue_number=issue,
            filter_kwargs=filter_kwargs,
        )
        media_map = {
            "json": ("application/json", "itrack-export.json"),
            "csv": ("text/csv", "itrack-export.csv"),
            "markdown": ("text/markdown", "itrack-export.md"),
        }
        media_type, filename = media_map[fmt]
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/import", status_code=201)
    async def import_endpoint(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw or not raw.strip():
            raise ValidationError("Import JSON payload cannot be empty.")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid import JSON: {exc}") from exc
        actor = parsed.get("actor") if isinstance(parsed, dict) else None
        return import_issues_json(store, parsed, actor=actor)

    return app
