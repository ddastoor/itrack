"""Self-contained air-gapped offline SPA Web UI for iTrack with full data-testid coverage."""

from __future__ import annotations

_SPA_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>iTrack — Issue Tracker</title>
<style>
  :root {
    --bg: #f4f6f9;
    --surface: #ffffff;
    --surface-muted: #f8fafc;
    --border: #d8dee9;
    --text: #1e293b;
    --muted: #64748b;
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --danger: #dc2626;
    --danger-hover: #b91c1c;
    --success: #16a34a;
    --warning: #d97706;
    --radius: 8px;
    --shadow: 0 2px 10px rgba(15, 23, 42, 0.08);
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  .hidden { display: none !important; }
  .app-header {
    background: #0f172a;
    color: #f8fafc;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    position: sticky;
    top: 0;
    z-index: 40;
  }
  .brand {
    font-weight: 700;
    font-size: 18px;
    letter-spacing: -0.02em;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .nav-group, .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .btn {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .btn:hover:not(:disabled) { background: #f1f5f9; }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-primary {
    background: var(--primary);
    border-color: var(--primary);
    color: #fff;
  }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  .btn-danger {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
  }
  .btn-danger:hover:not(:disabled) { background: var(--danger-hover); }
  .btn-nav {
    background: #1e293b;
    border-color: #334155;
    color: #e2e8f0;
  }
  .btn-nav:hover:not(:disabled) { background: #334155; }
  .btn-sm { padding: 4px 8px; font-size: 12px; }

  .jump-box { display: flex; align-items: center; gap: 4px; }
  .jump-box input {
    width: 90px;
    padding: 5px 8px;
    border-radius: 6px;
    border: 1px solid #334155;
    background: #1e293b;
    color: #f8fafc;
    font-size: 13px;
  }

  .container {
    max-width: 1320px;
    margin: 18px auto;
    padding: 0 18px 40px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 16px;
    margin-bottom: 16px;
  }
  .error-banner {
    background: #fef2f2;
    border: 1px solid #fecaca;
    color: #991b1b;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 14px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    font-size: 13px;
  }
  .success-banner {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #166534;
    padding: 10px 14px;
    border-radius: 6px;
    margin-top: 10px;
    font-size: 13px;
  }

  input[type="text"], input[type="date"], input[type="number"], input[type="color"], select, textarea {
    font-family: var(--font);
    font-size: 13px;
    padding: 6px 9px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: #fff;
    color: var(--text);
  }
  textarea.single-line-textarea {
    resize: none;
    overflow: hidden;
    height: 34px;
    line-height: 20px;
    width: 100%;
  }

  .search-row {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
    flex-wrap: wrap;
    align-items: center;
  }
  .search-row input { flex: 1; min-width: 240px; }

  .filters-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(165px, 1fr));
    gap: 10px;
    align-items: end;
  }
  .field-group { display: flex; flex-direction: column; gap: 4px; }
  .field-group label { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; }
  .filter-actions {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }

  .bulk-bar {
    background: #eff6ff;
    border: 1px solid #bfdbfe;
    padding: 10px 12px;
    border-radius: 6px;
    margin-bottom: 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 8px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
  th { font-size: 12px; font-weight: 600; color: var(--muted); background: var(--surface-muted); }
  tr:hover td { background: #f8fafc; }
  .clickable-link {
    color: var(--primary);
    cursor: pointer;
    font-weight: 600;
    text-decoration: none;
  }
  .clickable-link:hover { text-decoration: underline; }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    background: #e2e8f0;
    color: #334155;
  }
  .status-open { background: #dbeafe; color: #1e40af; }
  .status-in_progress { background: #fef3c7; color: #92400e; }
  .status-blocked { background: #fee2e2; color: #991b1b; }
  .status-resolved { background: #dcfce7; color: #166534; }
  .status-closed { background: #e2e8f0; color: #475569; }
  .priority-low { background: #f1f5f9; color: #475569; }
  .priority-medium { background: #e0f2fe; color: #0369a1; }
  .priority-high { background: #ffedd5; color: #c2410c; }
  .priority-critical { background: #fee2e2; color: #991b1b; }
  .label-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    background: #e0e7ff;
    color: #1e3a8a;
    margin: 1px 3px 1px 0;
  }
  .chip-remove {
    border: none;
    background: transparent;
    cursor: pointer;
    font-weight: 700;
    color: inherit;
    padding: 0 2px;
  }

  .pagination-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    font-size: 13px;
  }
  .pagination-controls { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }

  /* Rich Text Editor */
  .rt-container {
    border: 1px solid var(--border);
    border-radius: 6px;
    background: #fff;
    overflow: hidden;
  }
  .rt-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 6px 8px;
    background: var(--surface-muted);
    border-bottom: 1px solid var(--border);
    align-items: center;
  }
  .rt-toolbar .btn { padding: 3px 7px; font-size: 12px; }
  .rt-toolbar select { padding: 3px 6px; font-size: 12px; }
  .rt-popover {
    padding: 8px 10px;
    background: #f1f5f9;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .rt-editor {
    min-height: 120px;
    padding: 10px 12px;
    outline: none;
    font-size: 14px;
  }
  .rt-editor img, .rich-rendered img {
    max-width: 100%;
    max-height: 360px;
    border-radius: 4px;
    border: 1px solid var(--border);
    margin: 4px 0;
  }
  .rt-editor pre, .rich-rendered pre {
    background: #0f172a;
    color: #e2e8f0;
    padding: 10px;
    border-radius: 6px;
    overflow-x: auto;
    font-family: var(--mono);
  }
  .rt-editor code, .rich-rendered code {
    background: #f1f5f9;
    padding: 1px 4px;
    border-radius: 4px;
    font-family: var(--mono);
    font-size: 12px;
  }
  .rt-editor pre code, .rich-rendered pre code {
    background: transparent;
    padding: 0;
    color: inherit;
  }
  .rt-editor blockquote, .rich-rendered blockquote {
    border-left: 3px solid var(--primary);
    margin: 6px 0;
    padding-left: 10px;
    color: var(--muted);
  }
  .rt-html-input {
    width: 100%;
    min-height: 120px;
    border: none;
    border-top: 1px solid var(--border);
    border-radius: 0;
    font-family: var(--mono);
    font-size: 12px;
    padding: 10px;
  }

  /* Modal */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(15, 23, 42, 0.55);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 28px 16px;
    overflow-y: auto;
    z-index: 100;
  }
  .modal-card {
    background: var(--surface);
    border-radius: 10px;
    width: 100%;
    max-width: 820px;
    padding: 20px;
    box-shadow: 0 20px 50px rgba(15, 23, 42, 0.25);
  }
  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
  }
  .modal-header h2 { margin: 0; font-size: 18px; }
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px;
    margin: 10px 0;
  }
  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 16px;
  }

  /* Detail View */
  .detail-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .detail-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 10px;
    padding: 12px;
    background: var(--surface-muted);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin: 12px 0;
    font-size: 13px;
  }
  .comment-card {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    margin-bottom: 10px;
    background: #fff;
  }
  .comment-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .activity-row {
    padding: 7px 10px;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
  }
  .rel-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 6px;
    font-size: 13px;
  }
  .feature-card {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 8px;
    background: var(--surface-muted);
  }
  .mono { font-family: var(--mono); font-size: 12px; }
</style>
</head>
<body>
  <header class="app-header" data-testid="app-header">
    <div class="brand">
      <span>iTrack</span>
      <div class="nav-group">
        <button class="btn btn-nav" data-testid="nav-issues">Issues</button>
        <button class="btn btn-nav" data-testid="nav-labels">Labels</button>
        <button class="btn btn-nav" data-testid="nav-export-import">Export / Import</button>
        <button class="btn btn-nav" data-testid="nav-features">Features / Help</button>
      </div>
    </div>
    <div class="header-actions">
      <div class="jump-box">
        <input type="text" data-testid="issue-jump-input" placeholder="#1" aria-label="Jump to issue number" />
        <button class="btn btn-nav" data-testid="btn-issue-jump">Go</button>
      </div>
      <button class="btn btn-primary" data-testid="btn-new-issue">+ New Issue</button>
    </div>
  </header>

  <main class="container">
    <div class="error-banner hidden" data-testid="global-error-banner">
      <span id="global-error-text"></span>
      <button class="btn btn-sm" id="btn-dismiss-global-error">Dismiss</button>
    </div>

    <div class="error-banner hidden" data-testid="issue-not-found-error">
      <span id="issue-not-found-text"></span>
      <button class="btn btn-sm" id="btn-dismiss-not-found">Dismiss</button>
    </div>

    <!-- LIST VIEW -->
    <section id="list-view">
      <div class="card">
        <div class="search-row">
          <input type="text" data-testid="search-input" placeholder="Search #number, summary, description, comments..." />
          <button class="btn btn-primary" data-testid="btn-search-submit">Search</button>
          <button class="btn" data-testid="btn-search-clear">Clear</button>
        </div>

        <div class="filters-grid">
          <div class="field-group">
            <label>Status</label>
            <select data-testid="filter-status">
              <option value="">All</option>
              <option value="open">open</option>
              <option value="in_progress">in_progress</option>
              <option value="blocked">blocked</option>
              <option value="resolved">resolved</option>
              <option value="closed">closed</option>
            </select>
          </div>
          <div class="field-group">
            <label>Priority</label>
            <select data-testid="filter-priority">
              <option value="">All</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="critical">critical</option>
            </select>
          </div>
          <div class="field-group">
            <label>Type</label>
            <select data-testid="filter-type">
              <option value="">All</option>
              <option value="bug">bug</option>
              <option value="feature">feature</option>
              <option value="task">task</option>
              <option value="improvement">improvement</option>
            </select>
          </div>
          <div class="field-group">
            <label>Label(s)</label>
            <input type="text" data-testid="filter-label" placeholder="e.g. auth,backend" />
          </div>
          <div class="field-group">
            <label>Assignee</label>
            <input type="text" data-testid="filter-assignee" placeholder="Assignee" />
          </div>
          <div class="field-group">
            <label>Reporter</label>
            <input type="text" data-testid="filter-reporter" placeholder="Reporter" />
          </div>
          <div class="field-group">
            <label>Milestone</label>
            <input type="text" data-testid="filter-milestone" placeholder="Milestone" />
          </div>
          <div class="field-group">
            <label>Created From</label>
            <input type="date" data-testid="filter-created-from" />
          </div>
          <div class="field-group">
            <label>Created To</label>
            <input type="date" data-testid="filter-created-to" />
          </div>
          <div class="field-group">
            <label>Updated From</label>
            <input type="date" data-testid="filter-updated-from" />
          </div>
          <div class="field-group">
            <label>Updated To</label>
            <input type="date" data-testid="filter-updated-to" />
          </div>
          <div class="field-group">
            <label>Archived</label>
            <select data-testid="filter-archived">
              <option value="exclude">Exclude archived</option>
              <option value="include">Include archived</option>
              <option value="only">Archived only</option>
            </select>
          </div>
        </div>

        <div class="filter-actions">
          <button class="btn btn-primary" data-testid="btn-apply-filters">Apply Filters</button>
          <button class="btn" data-testid="btn-reset-filters">Reset</button>
          <span style="margin-left:auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <label style="font-size:12px;color:var(--muted);">Sort by</label>
            <select data-testid="sort-by-select">
              <option value="number">number</option>
              <option value="created_at">created_at</option>
              <option value="updated_at">updated_at</option>
              <option value="priority">priority</option>
              <option value="status">status</option>
              <option value="summary">summary</option>
            </select>
            <select data-testid="sort-order-select">
              <option value="asc">asc</option>
              <option value="desc">desc</option>
            </select>
          </span>
        </div>
      </div>

      <div class="bulk-bar hidden" data-testid="bulk-actions-bar">
        <strong data-testid="bulk-selected-count">0 selected</strong>
        <select data-testid="bulk-status-select">
          <option value="">Status (unchanged)</option>
          <option value="open">open</option>
          <option value="in_progress">in_progress</option>
          <option value="blocked">blocked</option>
          <option value="resolved">resolved</option>
          <option value="closed">closed</option>
        </select>
        <select data-testid="bulk-priority-select">
          <option value="">Priority (unchanged)</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
          <option value="critical">critical</option>
        </select>
        <input type="text" data-testid="bulk-assignee-input" placeholder="Assignee" style="width:120px;" />
        <label style="font-size:12px; display:inline-flex; align-items:center; gap:4px;">
          <input type="checkbox" data-testid="bulk-clear-assignee" /> Unassign
        </label>
        <input type="text" data-testid="bulk-add-labels-input" placeholder="+labels (csv)" style="width:130px;" />
        <input type="text" data-testid="bulk-remove-labels-input" placeholder="-labels (csv)" style="width:130px;" />
        <button class="btn btn-primary btn-sm" data-testid="btn-bulk-apply">Apply Bulk</button>
        <button class="btn btn-danger btn-sm" data-testid="btn-bulk-delete">Delete Selected</button>
      </div>

      <div class="card">
        <div style="overflow-x:auto;">
          <table data-testid="issue-list-table">
            <thead>
              <tr>
                <th><input type="checkbox" data-testid="select-all-issues" /></th>
                <th>#</th>
                <th>Summary</th>
                <th>Status</th>
                <th>Priority</th>
                <th>Type</th>
                <th>Labels</th>
                <th>Assignee</th>
                <th>Comments</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody id="issue-table-body"></tbody>
          </table>
        </div>
        <div class="hidden" data-testid="issue-list-empty" style="padding:24px; text-align:center; color:var(--muted);">
          No issues found matching the current filters.
        </div>

        <div class="pagination-bar" data-testid="pagination-bar">
          <div>
            Total: <strong data-testid="pagination-total-count">0</strong>
            &nbsp;|&nbsp;
            <span data-testid="pagination-info">Page 1 of 1</span>
          </div>
          <div class="pagination-controls">
            <label style="font-size:12px;color:var(--muted);">Per page</label>
            <select data-testid="pagination-per-page">
              <option value="5">5</option>
              <option value="10" selected>10</option>
              <option value="20">20</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
            <button class="btn btn-sm" data-testid="pagination-first">First</button>
            <button class="btn btn-sm" data-testid="pagination-prev">Prev</button>
            <button class="btn btn-sm" data-testid="pagination-next">Next</button>
            <button class="btn btn-sm" data-testid="pagination-last">Last</button>
          </div>
        </div>
      </div>
    </section>

    <!-- DETAIL VIEW -->
    <section id="detail-view" class="hidden" data-testid="issue-detail-view">
      <div class="card">
        <div class="detail-header">
          <div>
            <button class="btn btn-sm" data-testid="btn-back-to-list">&larr; Back to Issues</button>
            <h2 style="margin:10px 0 4px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
              <span data-testid="detail-number">#1</span>
              <span data-testid="detail-summary">Summary</span>
              <span class="badge hidden" data-testid="detail-archived-badge">ARCHIVED</span>
            </h2>
          </div>
          <div class="detail-actions">
            <select data-testid="detail-status-select">
              <option value="open">open</option>
              <option value="in_progress">in_progress</option>
              <option value="blocked">blocked</option>
              <option value="resolved">resolved</option>
              <option value="closed">closed</option>
            </select>
            <button class="btn btn-sm" data-testid="btn-close-issue">Close</button>
            <button class="btn btn-sm" data-testid="btn-reopen-issue">Reopen</button>
            <button class="btn btn-sm" data-testid="btn-edit-issue">Edit</button>
            <button class="btn btn-sm" data-testid="btn-clone-issue">Clone</button>
            <button class="btn btn-sm" data-testid="btn-archive-issue">Archive</button>
            <button class="btn btn-sm" data-testid="btn-restore-issue">Restore</button>
            <button class="btn btn-danger btn-sm" data-testid="btn-delete-issue">Delete</button>
          </div>
        </div>

        <div class="meta-grid">
          <div><strong>Status:</strong> <span class="badge" data-testid="detail-status"></span></div>
          <div><strong>Priority:</strong> <span class="badge" data-testid="detail-priority"></span></div>
          <div><strong>Type:</strong> <span class="badge" data-testid="detail-type"></span></div>
          <div><strong>Assignee:</strong> <span data-testid="detail-assignee"></span></div>
          <div><strong>Reporter:</strong> <span data-testid="detail-reporter"></span></div>
          <div><strong>Milestone:</strong> <span data-testid="detail-milestone"></span></div>
          <div><strong>Due Date:</strong> <span data-testid="detail-due-date"></span></div>
          <div><strong>Created:</strong> <span class="mono" data-testid="detail-created-at"></span></div>
          <div><strong>Updated:</strong> <span class="mono" data-testid="detail-updated-at"></span></div>
          <div><strong>Closed:</strong> <span class="mono" data-testid="detail-closed-at"></span></div>
        </div>

        <div style="margin:10px 0;">
          <strong>Labels:</strong>
          <span data-testid="detail-labels"></span>
          <span style="display:inline-flex; gap:4px; margin-left:8px; align-items:center;">
            <input type="text" data-testid="detail-add-label-input" placeholder="Add label..." style="width:130px;" />
            <button class="btn btn-sm" data-testid="btn-add-issue-label">+ Label</button>
          </span>
        </div>

        <!-- Inline Edit Form -->
        <div class="card hidden" data-testid="edit-issue-form" style="background:var(--surface-muted); margin-top:12px;">
          <h3 style="margin-top:0;">Edit Issue</h3>
          <div class="field-group">
            <label>One-Line Summary *</label>
            <textarea rows="1" class="single-line-textarea" data-testid="edit-summary-input"></textarea>
          </div>
          <div class="form-grid">
            <div class="field-group">
              <label>Priority</label>
              <select data-testid="edit-priority-select">
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="critical">critical</option>
              </select>
            </div>
            <div class="field-group">
              <label>Type</label>
              <select data-testid="edit-type-select">
                <option value="bug">bug</option>
                <option value="feature">feature</option>
                <option value="task">task</option>
                <option value="improvement">improvement</option>
              </select>
            </div>
            <div class="field-group">
              <label>Assignee</label>
              <input type="text" data-testid="edit-assignee-input" />
            </div>
            <div class="field-group">
              <label>Reporter</label>
              <input type="text" data-testid="edit-reporter-input" />
            </div>
            <div class="field-group">
              <label>Milestone</label>
              <input type="text" data-testid="edit-milestone-input" />
            </div>
            <div class="field-group">
              <label>Due Date</label>
              <input type="date" data-testid="edit-due-date-input" />
            </div>
            <div class="field-group">
              <label>Labels (csv)</label>
              <input type="text" data-testid="edit-labels-input" />
            </div>
          </div>
          <div class="field-group">
            <label>Rich-Text Description *</label>
            <div id="edit-desc-host"></div>
          </div>
          <div class="error-banner hidden" data-testid="edit-validation-error" style="margin-top:10px;"></div>
          <div style="display:flex; gap:8px; margin-top:12px;">
            <button class="btn btn-primary" data-testid="btn-edit-save">Save Changes</button>
            <button class="btn" data-testid="btn-edit-cancel">Cancel</button>
          </div>
        </div>

        <h3 style="margin-top:18px;">Description</h3>
        <div class="rich-rendered" data-testid="detail-description" style="padding:10px; border:1px solid var(--border); border-radius:6px;"></div>

        <h3 style="margin-top:20px;">Relationships</h3>
        <div data-testid="relationships-list"></div>
        <div style="display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; align-items:center;">
          <select data-testid="relationship-type-select">
            <option value="blocks">blocks</option>
            <option value="blocked_by">blocked_by</option>
            <option value="relates_to">relates_to</option>
            <option value="duplicates">duplicates</option>
          </select>
          <input type="text" data-testid="relationship-target-input" placeholder="Target # (e.g. 2)" style="width:140px;" />
          <button class="btn btn-sm" data-testid="btn-add-relationship">Add Link</button>
        </div>

        <h3 style="margin-top:22px;">Comments</h3>
        <div data-testid="comments-list"></div>

        <div class="card" style="background:var(--surface-muted); margin-top:12px;">
          <h4 style="margin:0 0 8px;">Add Rich-Text Comment</h4>
          <div class="field-group" style="margin-bottom:8px; max-width:260px;">
            <label>Author (optional)</label>
            <input type="text" data-testid="comment-author-input" placeholder="anonymous" />
          </div>
          <div id="new-comment-host"></div>
          <div class="error-banner hidden" data-testid="comment-validation-error" style="margin-top:8px;"></div>
          <div style="margin-top:10px;">
            <button class="btn btn-primary" data-testid="btn-add-comment">Post Comment</button>
          </div>
        </div>

        <h3 style="margin-top:22px;">Activity History</h3>
        <div data-testid="activity-history-list" style="border:1px solid var(--border); border-radius:6px;"></div>
      </div>
    </section>

    <!-- CREATE ISSUE MODAL -->
    <div class="modal-backdrop hidden" data-testid="create-issue-modal">
      <div class="modal-card">
        <div class="modal-header">
          <h2>Create New Issue</h2>
          <button class="btn btn-sm" data-testid="btn-create-cancel">Close</button>
        </div>
        <div class="field-group">
          <label>One-Line Summary *</label>
          <textarea rows="1" class="single-line-textarea" data-testid="create-summary-input" placeholder="Concise single-line summary"></textarea>
        </div>
        <div class="form-grid">
          <div class="field-group">
            <label>Status</label>
            <select data-testid="create-status-select">
              <option value="open" selected>open</option>
              <option value="in_progress">in_progress</option>
              <option value="blocked">blocked</option>
              <option value="resolved">resolved</option>
              <option value="closed">closed</option>
            </select>
          </div>
          <div class="field-group">
            <label>Priority</label>
            <select data-testid="create-priority-select">
              <option value="low">low</option>
              <option value="medium" selected>medium</option>
              <option value="high">high</option>
              <option value="critical">critical</option>
            </select>
          </div>
          <div class="field-group">
            <label>Type</label>
            <select data-testid="create-type-select">
              <option value="bug">bug</option>
              <option value="feature">feature</option>
              <option value="task" selected>task</option>
              <option value="improvement">improvement</option>
            </select>
          </div>
          <div class="field-group">
            <label>Labels (csv)</label>
            <input type="text" data-testid="create-labels-input" placeholder="auth, backend" />
          </div>
          <div class="field-group">
            <label>Assignee</label>
            <input type="text" data-testid="create-assignee-input" placeholder="alice" />
          </div>
          <div class="field-group">
            <label>Reporter</label>
            <input type="text" data-testid="create-reporter-input" placeholder="bob" />
          </div>
          <div class="field-group">
            <label>Milestone</label>
            <input type="text" data-testid="create-milestone-input" placeholder="v1.0" />
          </div>
          <div class="field-group">
            <label>Due Date</label>
            <input type="date" data-testid="create-due-date-input" />
          </div>
          <div class="field-group">
            <label>Initial Link Type</label>
            <select data-testid="create-rel-type-select">
              <option value="blocks">blocks</option>
              <option value="blocked_by">blocked_by</option>
              <option value="relates_to">relates_to</option>
              <option value="duplicates">duplicates</option>
            </select>
          </div>
          <div class="field-group">
            <label>Initial Link Target #</label>
            <input type="text" data-testid="create-rel-target-input" placeholder="e.g. 1" />
          </div>
        </div>
        <div class="field-group">
          <label>Rich-Text Description *</label>
          <div id="create-desc-host"></div>
        </div>
        <div class="error-banner hidden" data-testid="create-validation-error" style="margin-top:10px;"></div>
        <div class="modal-footer">
          <button class="btn btn-primary" data-testid="btn-create-submit">Create Issue</button>
        </div>
      </div>
    </div>

    <!-- LABELS MANAGER MODAL -->
    <div class="modal-backdrop hidden" data-testid="labels-manager">
      <div class="modal-card">
        <div class="modal-header">
          <h2>Labels Management</h2>
          <button class="btn btn-sm" id="btn-close-labels-manager">Close</button>
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-bottom:14px; flex-wrap:wrap;">
          <input type="text" data-testid="label-create-name" placeholder="New label name" />
          <input type="color" data-testid="label-create-color" value="#3b82f6" />
          <button class="btn btn-primary" data-testid="btn-label-create">Create Label</button>
        </div>
        <div id="labels-manager-list"></div>
      </div>
    </div>

    <!-- EXPORT / IMPORT MODAL -->
    <div class="modal-backdrop hidden" data-testid="export-import-panel">
      <div class="modal-card">
        <div class="modal-header">
          <h2>Export &amp; Import Issues</h2>
          <button class="btn btn-sm" id="btn-close-export-import">Close</button>
        </div>
        <h3 style="margin-top:0;">Export</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px;">
          <select data-testid="export-scope-select">
            <option value="all">All issues</option>
            <option value="filtered">Current filtered list</option>
            <option value="single">Single issue</option>
          </select>
          <input type="text" data-testid="export-issue-number-input" placeholder="Issue #" style="width:100px;" />
          <select data-testid="export-format-select">
            <option value="json">json</option>
            <option value="csv">csv</option>
            <option value="markdown">markdown</option>
          </select>
          <button class="btn btn-primary" data-testid="btn-export-submit">Export</button>
        </div>
        <textarea readonly data-testid="export-preview-output" style="width:100%; height:140px; font-family:var(--mono); font-size:12px;"></textarea>

        <h3 style="margin-top:16px;">Import (JSON)</h3>
        <div style="margin-bottom:8px;">
          <input type="file" data-testid="import-file-input" accept=".json,application/json" />
        </div>
        <textarea data-testid="import-json-textarea" placeholder='Paste JSON export payload here...' style="width:100%; height:120px; font-family:var(--mono); font-size:12px;"></textarea>
        <div style="margin-top:8px;">
          <button class="btn btn-primary" data-testid="btn-import-submit">Import JSON</button>
        </div>
        <div class="hidden" data-testid="import-result-message"></div>
      </div>
    </div>

    <!-- FEATURES / HELP MODAL -->
    <div class="modal-backdrop hidden" data-testid="features-view">
      <div class="modal-card">
        <div class="modal-header">
          <h2>Supported iTrack Features &amp; CLI Parity Guide</h2>
          <button class="btn btn-sm" data-testid="btn-close-features">Close</button>
        </div>
        <div data-testid="features-list"></div>
      </div>
    </div>

    <!-- CONFIRM DELETE MODAL -->
    <div class="modal-backdrop hidden" data-testid="confirm-delete-modal">
      <div class="modal-card" style="max-width:460px;">
        <div class="modal-header">
          <h2>Confirm Deletion</h2>
        </div>
        <p data-testid="confirm-delete-message"></p>
        <div class="modal-footer">
          <button class="btn" data-testid="btn-cancel-delete">Cancel</button>
          <button class="btn btn-danger" data-testid="btn-confirm-delete">Delete</button>
        </div>
      </div>
    </div>
  </main>

<script>
(() => {
  "use strict";

  const state = {
    page: 1,
    per_page: 10,
    sort_by: "number",
    sort_order: "asc",
    search: "",
    status: "",
    priority: "",
    issue_type: "",
    label: "",
    assignee: "",
    reporter: "",
    milestone: "",
    created_from: "",
    created_to: "",
    updated_from: "",
    updated_to: "",
    archived: "exclude",
    selectedNumbers: new Set(),
    currentItems: [],
    currentIssue: null,
    editingCommentId: null,
    pendingDeleteAction: null,
  };

  const editors = {};

  function qs(sel, root = document) { return root.querySelector(sel); }
  function qsa(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }
  function byTestId(id, root = document) { return root.querySelector(`[data-testid="${CSS.escape(id)}"]`); }

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function showGlobalError(msg) {
    const el = byTestId("global-error-banner");
    qs("#global-error-text").textContent = msg;
    el.classList.remove("hidden");
  }
  function clearGlobalError() {
    byTestId("global-error-banner").classList.add("hidden");
  }
  function showNotFoundError(msg) {
    const el = byTestId("issue-not-found-error");
    qs("#issue-not-found-text").textContent = msg;
    el.classList.remove("hidden");
  }
  function clearNotFoundError() {
    byTestId("issue-not-found-error").classList.add("hidden");
  }

  async function apiFetch(url, options = {}) {
    const resp = await fetch(url, options);
    const contentType = resp.headers.get("content-type") || "";
    let body = null;
    if (contentType.includes("application/json")) {
      body = await resp.json();
    } else {
      body = await resp.text();
    }
    if (!resp.ok) {
      const msg = (body && typeof body === "object" && (body.message || body.detail)) || `Request failed (${resp.status})`;
      const err = new Error(msg);
      err.status = resp.status;
      err.payload = body;
      throw err;
    }
    return body;
  }

  // ------------------------------------------------------------------
  // Reusable Rich-Text Editor Component
  // ------------------------------------------------------------------
  const ALLOWED_RICH_TAGS = new Set([
    "p", "br", "hr", "div", "span",
    "strong", "b", "em", "i", "u", "s", "del", "strike",
    "code", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "ul", "ol", "li",
    "a", "img",
  ]);
  const VOID_RICH_TAGS = new Set(["br", "hr", "img"]);
  const DROP_RICH_TAGS = new Set([
    "script", "style", "iframe", "object", "embed",
    "svg", "math", "noscript", "template", "form",
  ]);

  function isSafeClientHref(rawHref) {
    const cleaned = String(rawHref || "").trim();
    if (!cleaned) return null;
    const normalized = cleaned.replace(/[\x00-\x20]+/g, "").toLowerCase();
    if (
      normalized.startsWith("http://") ||
      normalized.startsWith("https://") ||
      normalized.startsWith("mailto:")
    ) {
      return cleaned;
    }
    return null;
  }

  function isSafeClientImgSrc(rawSrc) {
    const cleaned = String(rawSrc || "").trim();
    if (!cleaned) return null;
    const normalized = cleaned.replace(/[\x00-\x20]+/g, "");
    const low = normalized.toLowerCase();
    if (low.startsWith("http://") || low.startsWith("https://")) {
      return cleaned;
    }
    if (/^\/api\/assets\/[A-Za-z0-9._-]+$/.test(normalized)) {
      return normalized;
    }
    if (/^data:image\/(?:png|jpeg|jpg|gif|webp|bmp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$/i.test(normalized)) {
      return normalized;
    }
    return null;
  }

  function sanitizeClientNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      return esc(node.nodeValue || "");
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      return "";
    }
    const tag = node.tagName.toLowerCase();
    if (DROP_RICH_TAGS.has(tag)) {
      return "";
    }
    let childrenHtml = "";
    if (!VOID_RICH_TAGS.has(tag)) {
      for (const child of Array.from(node.childNodes)) {
        childrenHtml += sanitizeClientNode(child);
      }
    }
    if (!ALLOWED_RICH_TAGS.has(tag)) {
      return childrenHtml;
    }
    if (tag === "img") {
      const safeSrc = isSafeClientImgSrc(node.getAttribute("src"));
      if (!safeSrc) return "";
      const altVal = node.getAttribute("alt") || "";
      const assetId = (node.getAttribute("data-asset-id") || "").trim();
      const assetAttr = /^[A-Za-z0-9._-]+$/.test(assetId)
        ? ` data-asset-id="${esc(assetId)}"`
        : "";
      return `<img src="${esc(safeSrc)}" alt="${esc(altVal)}"${assetAttr} />`;
    }
    if (VOID_RICH_TAGS.has(tag)) {
      return `<${tag} />`;
    }
    const attrs = [];
    if (tag === "a") {
      const safeHref = isSafeClientHref(node.getAttribute("href"));
      if (safeHref) {
        attrs.push(`href="${esc(safeHref)}"`);
      }
      const title = (node.getAttribute("title") || "").trim();
      if (title) {
        attrs.push(`title="${esc(title)}"`);
      }
      if ((node.getAttribute("target") || "").trim() === "_blank" && safeHref) {
        attrs.push('target="_blank"');
        attrs.push('rel="noopener noreferrer"');
      }
    } else if (tag === "code" || tag === "pre") {
      const cls = (node.getAttribute("class") || "").trim();
      if (cls && /^[A-Za-z0-9 _-]+$/.test(cls)) {
        attrs.push(`class="${esc(cls)}"`);
      }
    }
    const attrStr = attrs.length > 0 ? " " + attrs.join(" ") : "";
    return `<${tag}${attrStr}>${childrenHtml}</${tag}>`;
  }

  function sanitizeClientHtml(rawHtml) {
    if (!rawHtml) return "";
    const parsed = new DOMParser().parseFromString(String(rawHtml), "text/html");
    let out = "";
    for (const child of Array.from(parsed.body.childNodes)) {
      out += sanitizeClientNode(child);
    }
    return out;
  }

  function createRichEditor(prefix, initialHtml = "") {
    const wrapper = document.createElement("div");
    wrapper.className = "rt-container";
    wrapper.innerHTML = `
      <div class="rt-toolbar" data-testid="${prefix}-toolbar">
        <button type="button" class="btn" data-testid="${prefix}-btn-bold" title="Bold"><strong>B</strong></button>
        <button type="button" class="btn" data-testid="${prefix}-btn-italic" title="Italic"><em>I</em></button>
        <button type="button" class="btn" data-testid="${prefix}-btn-underline" title="Underline"><u>U</u></button>
        <button type="button" class="btn" data-testid="${prefix}-btn-strike" title="Strikethrough"><s>S</s></button>
        <button type="button" class="btn" data-testid="${prefix}-btn-code" title="Inline code">&lt;/&gt;</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-codeblock" title="Code block">Code Block</button>
        <select data-testid="${prefix}-heading-level" title="Heading level">
          <option value="h1">H1</option>
          <option value="h2" selected>H2</option>
          <option value="h3">H3</option>
        </select>
        <button type="button" class="btn" data-testid="${prefix}-btn-heading" title="Heading">Heading</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-quote" title="Blockquote">Quote</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-ul" title="Bulleted list">&bull; List</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-ol" title="Numbered list">1. List</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-link" title="Insert link">Link</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-image" title="Insert image">Image</button>
        <button type="button" class="btn" data-testid="${prefix}-btn-source" title="Toggle HTML source">HTML</button>
      </div>
      <div class="rt-popover hidden" data-testid="${prefix}-link-popover">
        <input type="text" data-testid="${prefix}-link-text" placeholder="Link text" style="width:160px;" />
        <input type="text" data-testid="${prefix}-link-url" placeholder="https://example.com" style="width:220px;" />
        <button type="button" class="btn btn-primary btn-sm" data-testid="${prefix}-link-insert">Insert Link</button>
      </div>
      <div class="rt-popover hidden" data-testid="${prefix}-image-popover">
        <input type="file" accept="image/*" data-testid="${prefix}-image-file" />
        <input type="text" data-testid="${prefix}-image-src" placeholder="Image URL or data:image/... URI" style="width:220px;" />
        <input type="text" data-testid="${prefix}-image-alt" placeholder="Alt text" style="width:140px;" />
        <button type="button" class="btn btn-primary btn-sm" data-testid="${prefix}-image-insert">Insert Image</button>
      </div>
      <div class="rt-editor" contenteditable="true" data-testid="${prefix}-editor"></div>
      <textarea class="rt-html-input hidden" data-testid="${prefix}-html-input"></textarea>
    `;

    const editorEl = byTestId(`${prefix}-editor`, wrapper);
    const htmlInput = byTestId(`${prefix}-html-input`, wrapper);
    const linkPopover = byTestId(`${prefix}-link-popover`, wrapper);
    const imagePopover = byTestId(`${prefix}-image-popover`, wrapper);
    let sourceMode = false;
    let lastSurface = "editor";

    const safeInitial = sanitizeClientHtml(initialHtml || "");
    editorEl.innerHTML = safeInitial;
    htmlInput.value = safeInitial;

    function syncFromEditor() {
      htmlInput.value = editorEl.innerHTML;
      lastSurface = "editor";
    }
    function syncFromHtmlInput() {
      editorEl.innerHTML = sanitizeClientHtml(htmlInput.value);
      lastSurface = "html";
    }

    editorEl.addEventListener("input", syncFromEditor);
    editorEl.addEventListener("paste", (e) => {
      if (e.clipboardData && e.clipboardData.types.includes("text/html")) {
        e.preventDefault();
        const pastedHtml = e.clipboardData.getData("text/html");
        insertHtmlAtCaret(sanitizeClientHtml(pastedHtml));
      }
    });
    htmlInput.addEventListener("input", syncFromHtmlInput);
    htmlInput.addEventListener("change", syncFromHtmlInput);

    function insertHtmlAtCaret(htmlStr) {
      const safeHtml = sanitizeClientHtml(htmlStr);
      editorEl.focus();
      const sel = window.getSelection();
      if (sel && sel.rangeCount > 0 && editorEl.contains(sel.anchorNode)) {
        const range = sel.getRangeAt(0);
        range.deleteContents();
        const frag = range.createContextualFragment(safeHtml);
        const lastNode = frag.lastChild;
        range.insertNode(frag);
        if (lastNode) {
          const newRange = document.createRange();
          newRange.setStartAfter(lastNode);
          newRange.collapse(true);
          sel.removeAllRanges();
          sel.addRange(newRange);
        }
      } else {
        editorEl.insertAdjacentHTML("beforeend", safeHtml);
      }
      syncFromEditor();
    }

    function wrapOrInsertTag(tagName, placeholder) {
      editorEl.focus();
      const sel = window.getSelection();
      const selected = sel ? sel.toString() : "";
      const inner = selected ? esc(selected) : placeholder;
      insertHtmlAtCaret(`<${tagName}>${inner}</${tagName}>`);
    }

    // Keep selection when clicking toolbar buttons
    qsa(".rt-toolbar button", wrapper).forEach((btn) => {
      btn.addEventListener("mousedown", (e) => e.preventDefault());
    });

    byTestId(`${prefix}-btn-bold`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      document.execCommand("bold", false, null);
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-italic`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      document.execCommand("italic", false, null);
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-underline`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      document.execCommand("underline", false, null);
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-strike`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      document.execCommand("strikeThrough", false, null);
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-code`, wrapper).addEventListener("click", () => {
      wrapOrInsertTag("code", "code");
    });
    byTestId(`${prefix}-btn-codeblock`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      const sel = window.getSelection();
      const selected = sel ? sel.toString() : "";
      insertHtmlAtCaret(`<pre><code>${esc(selected || "code block")}</code></pre>`);
    });
    byTestId(`${prefix}-btn-heading`, wrapper).addEventListener("click", () => {
      const lvl = byTestId(`${prefix}-heading-level`, wrapper).value || "h2";
      editorEl.focus();
      const sel = window.getSelection();
      const selected = sel ? sel.toString().trim() : "";
      if (selected) {
        insertHtmlAtCaret(`<${lvl}>${esc(selected)}</${lvl}>`);
      } else {
        document.execCommand("formatBlock", false, lvl);
        if (!editorEl.querySelector(lvl)) {
          insertHtmlAtCaret(`<${lvl}>Heading</${lvl}>`);
        }
      }
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-quote`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      const sel = window.getSelection();
      const selected = sel ? sel.toString().trim() : "";
      if (selected) {
        insertHtmlAtCaret(`<blockquote>${esc(selected)}</blockquote>`);
      } else {
        document.execCommand("formatBlock", false, "blockquote");
        if (!editorEl.querySelector("blockquote")) {
          insertHtmlAtCaret("<blockquote>Quote</blockquote>");
        }
      }
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-ul`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      document.execCommand("insertUnorderedList", false, null);
      syncFromEditor();
    });
    byTestId(`${prefix}-btn-ol`, wrapper).addEventListener("click", () => {
      editorEl.focus();
      document.execCommand("insertOrderedList", false, null);
      syncFromEditor();
    });

    byTestId(`${prefix}-btn-link`, wrapper).addEventListener("click", () => {
      imagePopover.classList.add("hidden");
      linkPopover.classList.toggle("hidden");
    });
    byTestId(`${prefix}-link-insert`, wrapper).addEventListener("click", () => {
      const linkText = byTestId(`${prefix}-link-text`, wrapper).value.trim();
      const linkUrl = byTestId(`${prefix}-link-url`, wrapper).value.trim();
      if (!linkUrl) return;
      insertHtmlAtCaret(`<a href="${esc(linkUrl)}" target="_blank">${esc(linkText || linkUrl)}</a>`);
      byTestId(`${prefix}-link-text`, wrapper).value = "";
      byTestId(`${prefix}-link-url`, wrapper).value = "";
      linkPopover.classList.add("hidden");
    });

    byTestId(`${prefix}-btn-image`, wrapper).addEventListener("click", () => {
      linkPopover.classList.add("hidden");
      imagePopover.classList.toggle("hidden");
    });

    const imageFileInput = byTestId(`${prefix}-image-file`, wrapper);
    imageFileInput.addEventListener("change", async () => {
      const file = imageFileInput.files && imageFileInput.files[0];
      if (!file) return;
      const altInput = byTestId(`${prefix}-image-alt`, wrapper);
      const altVal = altInput.value.trim() || file.name;
      const fd = new FormData();
      fd.append("file", file);
      fd.append("alt", altVal);
      try {
        const asset = await apiFetch("/api/assets", { method: "POST", body: fd });
        insertHtmlAtCaret(
          `<img src="${esc(asset.url)}" alt="${esc(asset.alt || altVal)}" data-asset-id="${esc(asset.asset_id)}" />`
        );
        imageFileInput.value = "";
        altInput.value = "";
        imagePopover.classList.add("hidden");
      } catch (err) {
        showGlobalError(err.message);
      }
    });

    byTestId(`${prefix}-image-insert`, wrapper).addEventListener("click", () => {
      const srcVal = byTestId(`${prefix}-image-src`, wrapper).value.trim();
      const altVal = byTestId(`${prefix}-image-alt`, wrapper).value.trim();
      if (!srcVal) return;
      insertHtmlAtCaret(`<img src="${esc(srcVal)}" alt="${esc(altVal)}" />`);
      byTestId(`${prefix}-image-src`, wrapper).value = "";
      byTestId(`${prefix}-image-alt`, wrapper).value = "";
      imagePopover.classList.add("hidden");
    });

    byTestId(`${prefix}-btn-source`, wrapper).addEventListener("click", () => {
      sourceMode = !sourceMode;
      if (sourceMode) {
        htmlInput.value = editorEl.innerHTML;
        htmlInput.classList.remove("hidden");
        editorEl.classList.add("hidden");
      } else {
        const safeHtml = sanitizeClientHtml(htmlInput.value);
        editorEl.innerHTML = safeHtml;
        htmlInput.value = safeHtml;
        htmlInput.classList.add("hidden");
        editorEl.classList.remove("hidden");
      }
    });

    const api = {
      el: wrapper,
      setValue(val) {
        const safeVal = sanitizeClientHtml(val || "");
        editorEl.innerHTML = safeVal;
        htmlInput.value = safeVal;
        lastSurface = "editor";
        sourceMode = false;
        htmlInput.classList.add("hidden");
        editorEl.classList.remove("hidden");
        linkPopover.classList.add("hidden");
        imagePopover.classList.add("hidden");
      },
      getValue() {
        if (sourceMode || lastSurface === "html") {
          return htmlInput.value;
        }
        return editorEl.innerHTML;
      },
      clear() {
        this.setValue("");
      },
    };
    editors[prefix] = api;
    return api;
  }

  // Prevent normal Enter key from creating newlines in single-line summary textareas
  function bindSingleLineGuard(el) {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") e.preventDefault();
    });
  }

  // ------------------------------------------------------------------
  // Navigation & List Loading
  // ------------------------------------------------------------------
  function buildFilterQueryParams(includePagination = true) {
    const params = new URLSearchParams();
    if (includePagination) {
      params.set("page", String(state.page));
      params.set("per_page", String(state.per_page));
    }
    params.set("sort_by", state.sort_by);
    params.set("sort_order", state.sort_order);
    if (state.search) params.set("search", state.search);
    if (state.status) params.set("status", state.status);
    if (state.priority) params.set("priority", state.priority);
    if (state.issue_type) params.set("issue_type", state.issue_type);
    if (state.label) params.set("label", state.label);
    if (state.assignee) params.set("assignee", state.assignee);
    if (state.reporter) params.set("reporter", state.reporter);
    if (state.milestone) params.set("milestone", state.milestone);
    if (state.created_from) params.set("created_from", state.created_from);
    if (state.created_to) params.set("created_to", state.created_to);
    if (state.updated_from) params.set("updated_from", state.updated_from);
    if (state.updated_to) params.set("updated_to", state.updated_to);
    if (state.archived === "include") params.set("include_archived", "true");
    if (state.archived === "only") params.set("archived_only", "true");
    return params;
  }

  function syncStateFromFilterInputs() {
    state.search = byTestId("search-input").value.trim();
    state.status = byTestId("filter-status").value;
    state.priority = byTestId("filter-priority").value;
    state.issue_type = byTestId("filter-type").value;
    state.label = byTestId("filter-label").value.trim();
    state.assignee = byTestId("filter-assignee").value.trim();
    state.reporter = byTestId("filter-reporter").value.trim();
    state.milestone = byTestId("filter-milestone").value.trim();
    state.created_from = byTestId("filter-created-from").value;
    state.created_to = byTestId("filter-created-to").value;
    state.updated_from = byTestId("filter-updated-from").value;
    state.updated_to = byTestId("filter-updated-to").value;
    state.archived = byTestId("filter-archived").value;
    state.sort_by = byTestId("sort-by-select").value;
    state.sort_order = byTestId("sort-order-select").value;
    state.per_page = parseInt(byTestId("pagination-per-page").value, 10) || 10;
  }

  function updateBulkBar() {
    const count = state.selectedNumbers.size;
    const bar = byTestId("bulk-actions-bar");
    byTestId("bulk-selected-count").textContent = `${count} selected`;
    if (count > 0) bar.classList.remove("hidden");
    else bar.classList.add("hidden");

    const selectAll = byTestId("select-all-issues");
    if (state.currentItems.length > 0 && state.currentItems.every((it) => state.selectedNumbers.has(it.number))) {
      selectAll.checked = true;
    } else {
      selectAll.checked = false;
    }
  }

  async function loadIssues() {
    clearGlobalError();
    const params = buildFilterQueryParams(true);
    try {
      const data = await apiFetch(`/api/issues?${params.toString()}`);
      state.currentItems = data.items || [];
      const tbody = qs("#issue-table-body");
      tbody.innerHTML = "";
      const emptyEl = byTestId("issue-list-empty");
      if (state.currentItems.length === 0) {
        emptyEl.classList.remove("hidden");
      } else {
        emptyEl.classList.add("hidden");
        for (const item of state.currentItems) {
          const tr = document.createElement("tr");
          tr.setAttribute("data-testid", `issue-row-${item.number}`);
          const checked = state.selectedNumbers.has(item.number) ? "checked" : "";
          const labelsHtml = (item.labels || [])
            .map((l) => `<span class="label-chip">${esc(l)}</span>`)
            .join("");
          tr.innerHTML = `
            <td><input type="checkbox" data-testid="issue-checkbox-${item.number}" ${checked} /></td>
            <td><a class="clickable-link" data-testid="issue-number-${item.number}">${esc(item.display_id)}</a></td>
            <td><a class="clickable-link" data-testid="issue-summary-${item.number}">${esc(item.summary)}</a>${item.archived ? ' <span class="badge">archived</span>' : ''}</td>
            <td><span class="badge status-${esc(item.status)}" data-testid="issue-status-${item.number}">${esc(item.status)}</span></td>
            <td><span class="badge priority-${esc(item.priority)}" data-testid="issue-priority-${item.number}">${esc(item.priority)}</span></td>
            <td><span class="badge" data-testid="issue-type-${item.number}">${esc(item.issue_type)}</span></td>
            <td data-testid="issue-labels-${item.number}">${labelsHtml}</td>
            <td data-testid="issue-assignee-${item.number}">${esc(item.assignee || "-")}</td>
            <td data-testid="issue-comments-count-${item.number}">${item.comment_count}</td>
            <td class="mono" data-testid="issue-updated-${item.number}">${esc(item.updated_at)}</td>
          `;
          byTestId(`issue-checkbox-${item.number}`, tr).addEventListener("change", (e) => {
            if (e.target.checked) state.selectedNumbers.add(item.number);
            else state.selectedNumbers.delete(item.number);
            updateBulkBar();
          });
          byTestId(`issue-number-${item.number}`, tr).addEventListener("click", () => openIssueDetail(item.number));
          byTestId(`issue-summary-${item.number}`, tr).addEventListener("click", () => openIssueDetail(item.number));
          tbody.appendChild(tr);
        }
      }

      byTestId("pagination-total-count").textContent = String(data.total);
      byTestId("pagination-info").textContent = `Page ${data.page} of ${data.total_pages}`;
      byTestId("pagination-first").disabled = !data.has_prev;
      byTestId("pagination-prev").disabled = !data.has_prev;
      byTestId("pagination-next").disabled = !data.has_next;
      byTestId("pagination-last").disabled = !data.has_next;
      state.lastTotalPages = data.total_pages;
      updateBulkBar();
    } catch (err) {
      showGlobalError(err.message);
    }
  }

  function showListView() {
    qs("#detail-view").classList.add("hidden");
    qs("#list-view").classList.remove("hidden");
    state.currentIssue = null;
    loadIssues();
  }

  // ------------------------------------------------------------------
  // Issue Detail View
  // ------------------------------------------------------------------
  async function openIssueDetail(issueNumberOrStr) {
    clearGlobalError();
    clearNotFoundError();
    const raw = String(issueNumberOrStr ?? "").trim();
    if (!raw) {
      showNotFoundError("Enter an issue number to look up.");
      return;
    }
    try {
      const issue = await apiFetch(`/api/issues/${encodeURIComponent(raw)}`);
      renderIssueDetail(issue);
      qs("#list-view").classList.add("hidden");
      qs("#detail-view").classList.remove("hidden");
    } catch (err) {
      showNotFoundError(err.message);
    }
  }

  function renderIssueDetail(issue) {
    state.currentIssue = issue;
    byTestId("detail-number").textContent = issue.display_id;
    byTestId("detail-summary").textContent = issue.summary;
    byTestId("detail-status").textContent = issue.status;
    byTestId("detail-status").className = `badge status-${issue.status}`;
    byTestId("detail-priority").textContent = issue.priority;
    byTestId("detail-priority").className = `badge priority-${issue.priority}`;
    byTestId("detail-type").textContent = issue.issue_type;
    byTestId("detail-assignee").textContent = issue.assignee || "unassigned";
    byTestId("detail-reporter").textContent = issue.reporter || "unknown";
    byTestId("detail-milestone").textContent = issue.milestone || "none";
    byTestId("detail-due-date").textContent = issue.due_date || "none";
    byTestId("detail-created-at").textContent = issue.created_at;
    byTestId("detail-updated-at").textContent = issue.updated_at;
    byTestId("detail-closed-at").textContent = issue.closed_at || "-";
    byTestId("detail-status-select").value = issue.status;

    const archBadge = byTestId("detail-archived-badge");
    if (issue.archived) archBadge.classList.remove("hidden");
    else archBadge.classList.add("hidden");

    byTestId("detail-description").innerHTML = sanitizeClientHtml(issue.description || "");

    // Labels
    const labelsHost = byTestId("detail-labels");
    labelsHost.innerHTML = "";
    for (const lbl of issue.labels || []) {
      const chip = document.createElement("span");
      chip.className = "label-chip";
      chip.setAttribute("data-testid", `detail-label-chip-${lbl}`);
      chip.innerHTML = `${esc(lbl)} <button type="button" class="chip-remove" data-testid="btn-remove-issue-label-${esc(lbl)}" title="Remove label">&times;</button>`;
      byTestId(`btn-remove-issue-label-${lbl}`, chip).addEventListener("click", async () => {
        try {
          const updated = await apiFetch(`/api/issues/${issue.number}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ remove_labels: [lbl] }),
          });
          renderIssueDetail(updated);
        } catch (err) {
          showGlobalError(err.message);
        }
      });
      labelsHost.appendChild(chip);
    }

    // Relationships
    const relHost = byTestId("relationships-list");
    relHost.innerHTML = "";
    for (const rel of issue.relationships || []) {
      const row = document.createElement("div");
      row.className = "rel-row";
      row.setAttribute("data-testid", `relationship-item-${rel.id}`);
      row.innerHTML = `
        <span>
          <strong>${esc(rel.relation_type)}</strong>
          <a class="clickable-link" data-testid="relationship-target-${rel.id}">${esc(rel.target_display_id)}</a>
          &mdash; ${esc(rel.target_summary)} <span class="badge status-${esc(rel.target_status)}">${esc(rel.target_status)}</span>
        </span>
        <button type="button" class="btn btn-sm" data-testid="btn-remove-relationship-${rel.id}">Remove</button>
      `;
      byTestId(`relationship-target-${rel.id}`, row).addEventListener("click", () => openIssueDetail(rel.target_number));
      byTestId(`btn-remove-relationship-${rel.id}`, row).addEventListener("click", async () => {
        try {
          await apiFetch(`/api/issues/${issue.number}/relationships/${rel.id}`, { method: "DELETE" });
          await openIssueDetail(issue.number);
        } catch (err) {
          showGlobalError(err.message);
        }
      });
      relHost.appendChild(row);
    }

    // Comments
    const cmtHost = byTestId("comments-list");
    cmtHost.innerHTML = "";
    for (const c of issue.comments || []) {
      const card = document.createElement("div");
      card.className = "comment-card";
      card.setAttribute("data-testid", `comment-item-${c.id}`);
      const isEditing = state.editingCommentId === c.id;
      card.innerHTML = `
        <div class="comment-head">
          <div>
            <strong data-testid="comment-author-${c.id}">${esc(c.author)}</strong>
            &bull; <span class="mono" data-testid="comment-created-at-${c.id}">${esc(c.created_at)}</span>
            ${c.is_edited ? `<span class="badge" data-testid="comment-edited-indicator-${c.id}">edited ${esc(c.updated_at)}</span>` : ""}
          </div>
          <div style="display:flex; gap:6px;">
            <button type="button" class="btn btn-sm" data-testid="btn-edit-comment-${c.id}">Edit</button>
            <button type="button" class="btn btn-danger btn-sm" data-testid="btn-delete-comment-${c.id}">Delete</button>
          </div>
        </div>
        <div class="rich-rendered ${isEditing ? "hidden" : ""}" data-testid="comment-body-${c.id}">${sanitizeClientHtml(c.body || "")}</div>
        <div class="edit-comment-box ${isEditing ? "" : "hidden"}" id="edit-comment-box-${c.id}">
          <div id="edit-comment-host-${c.id}"></div>
          <div class="error-banner hidden" data-testid="edit-comment-error-${c.id}" style="margin-top:8px;"></div>
          <div style="display:flex; gap:8px; margin-top:8px;">
            <button type="button" class="btn btn-primary btn-sm" data-testid="btn-save-comment-${c.id}">Save Comment</button>
            <button type="button" class="btn btn-sm" data-testid="btn-cancel-comment-${c.id}">Cancel</button>
          </div>
        </div>
      `;

      if (isEditing) {
        const host = qs(`#edit-comment-host-${c.id}`, card);
        const ed = createRichEditor(`edit-comment-${c.id}`, c.body || "");
        host.appendChild(ed.el);
      }

      byTestId(`btn-edit-comment-${c.id}`, card).addEventListener("click", () => {
        state.editingCommentId = c.id;
        renderIssueDetail(state.currentIssue);
      });
      byTestId(`btn-cancel-comment-${c.id}`, card).addEventListener("click", () => {
        state.editingCommentId = null;
        renderIssueDetail(state.currentIssue);
      });
      byTestId(`btn-save-comment-${c.id}`, card).addEventListener("click", async () => {
        const errBox = byTestId(`edit-comment-error-${c.id}`, card);
        errBox.classList.add("hidden");
        const ed = editors[`edit-comment-${c.id}`];
        const bodyVal = ed ? ed.getValue() : "";
        try {
          await apiFetch(`/api/issues/${issue.number}/comments/${c.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ body: bodyVal }),
          });
          state.editingCommentId = null;
          await openIssueDetail(issue.number);
        } catch (err) {
          errBox.textContent = err.message;
          errBox.classList.remove("hidden");
        }
      });
      byTestId(`btn-delete-comment-${c.id}`, card).addEventListener("click", async () => {
        try {
          await apiFetch(`/api/issues/${issue.number}/comments/${c.id}`, { method: "DELETE" });
          await openIssueDetail(issue.number);
        } catch (err) {
          showGlobalError(err.message);
        }
      });
      cmtHost.appendChild(card);
    }

    // Activity History
    const actHost = byTestId("activity-history-list");
    actHost.innerHTML = "";
    for (const ev of issue.activity || []) {
      const row = document.createElement("div");
      row.className = "activity-row";
      row.setAttribute("data-testid", `activity-item-${ev.id}`);
      const changesText = (ev.changes || [])
        .map((ch) => `${ch.field}: ${JSON.stringify(ch.old)} -> ${JSON.stringify(ch.new)}`)
        .join("; ");
      row.innerHTML = `
        <span class="mono">${esc(ev.created_at)}</span> &bull;
        <strong>${esc(ev.event_type)}</strong> by <em>${esc(ev.actor)}</em> &mdash; ${esc(ev.summary)}
        ${changesText ? `<span style="color:var(--muted);"> (${esc(changesText)})</span>` : ""}
      `;
      actHost.appendChild(row);
    }
  }

  // ------------------------------------------------------------------
  // Labels Manager, Export/Import, Features
  // ------------------------------------------------------------------
  async function loadLabelsManager() {
    try {
      const data = await apiFetch("/api/labels");
      const list = qs("#labels-manager-list");
      list.innerHTML = "";
      for (const lbl of data.labels || []) {
        const row = document.createElement("div");
        row.className = "rel-row";
        row.setAttribute("data-testid", `label-item-${lbl.name}`);
        row.innerHTML = `
          <span style="display:flex; align-items:center; gap:8px;">
            <span class="label-chip" style="background:${esc(lbl.color)}; color:#fff;">${esc(lbl.name)}</span>
            <span style="color:var(--muted); font-size:12px;">${lbl.issue_count} issues</span>
          </span>
          <span style="display:flex; gap:6px; align-items:center;">
            <input type="text" data-testid="label-rename-input-${esc(lbl.name)}" value="${esc(lbl.name)}" style="width:130px;" />
            <button type="button" class="btn btn-sm" data-testid="btn-label-rename-${esc(lbl.name)}">Rename</button>
            <button type="button" class="btn btn-danger btn-sm" data-testid="btn-label-delete-${esc(lbl.name)}">Delete</button>
          </span>
        `;
        byTestId(`btn-label-rename-${lbl.name}`, row).addEventListener("click", async () => {
          const newName = byTestId(`label-rename-input-${lbl.name}`, row).value.trim();
          try {
            await apiFetch(`/api/labels/${encodeURIComponent(lbl.name)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ new_name: newName }),
            });
            await loadLabelsManager();
            if (!qs("#list-view").classList.contains("hidden")) loadIssues();
            if (state.currentIssue) openIssueDetail(state.currentIssue.number);
          } catch (err) {
            showGlobalError(err.message);
          }
        });
        byTestId(`btn-label-delete-${lbl.name}`, row).addEventListener("click", async () => {
          try {
            await apiFetch(`/api/labels/${encodeURIComponent(lbl.name)}`, { method: "DELETE" });
            await loadLabelsManager();
            if (!qs("#list-view").classList.contains("hidden")) loadIssues();
            if (state.currentIssue) openIssueDetail(state.currentIssue.number);
          } catch (err) {
            showGlobalError(err.message);
          }
        });
        list.appendChild(row);
      }
    } catch (err) {
      showGlobalError(err.message);
    }
  }

  async function loadFeaturesView() {
    try {
      const data = await apiFetch("/api/features");
      const list = byTestId("features-list");
      list.innerHTML = "";
      for (const f of data.features || []) {
        const card = document.createElement("div");
        card.className = "feature-card";
        card.setAttribute("data-testid", `feature-item-${f.slug}`);
        card.innerHTML = `
          <div><strong>${esc(f.title)}</strong></div>
          <div style="font-size:13px; margin:4px 0;">${esc(f.description)}</div>
          <div style="font-size:12px; color:var(--muted);">
            <strong>UI:</strong> ${esc(f.ui_location)}<br/>
            <strong>CLI:</strong> <code class="mono">${esc(f.cli_command)}</code>
          </div>
        `;
        list.appendChild(card);
      }
    } catch (err) {
      showGlobalError(err.message);
    }
  }

  // ------------------------------------------------------------------
  // Event Wiring
  // ------------------------------------------------------------------
  function init() {
    qs("#create-desc-host").appendChild(createRichEditor("create-desc").el);
    qs("#edit-desc-host").appendChild(createRichEditor("edit-desc").el);
    qs("#new-comment-host").appendChild(createRichEditor("new-comment").el);

    bindSingleLineGuard(byTestId("create-summary-input"));
    bindSingleLineGuard(byTestId("edit-summary-input"));

    qs("#btn-dismiss-global-error").addEventListener("click", clearGlobalError);
    qs("#btn-dismiss-not-found").addEventListener("click", clearNotFoundError);

    byTestId("nav-issues").addEventListener("click", showListView);
    byTestId("btn-back-to-list").addEventListener("click", showListView);

    byTestId("nav-labels").addEventListener("click", () => {
      byTestId("labels-manager").classList.remove("hidden");
      loadLabelsManager();
    });
    qs("#btn-close-labels-manager").addEventListener("click", () => {
      byTestId("labels-manager").classList.add("hidden");
    });
    byTestId("btn-label-create").addEventListener("click", async () => {
      const name = byTestId("label-create-name").value.trim();
      const color = byTestId("label-create-color").value;
      try {
        await apiFetch("/api/labels", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, color }),
        });
        byTestId("label-create-name").value = "";
        await loadLabelsManager();
      } catch (err) {
        showGlobalError(err.message);
      }
    });

    byTestId("nav-export-import").addEventListener("click", () => {
      byTestId("export-import-panel").classList.remove("hidden");
    });
    qs("#btn-close-export-import").addEventListener("click", () => {
      byTestId("export-import-panel").classList.add("hidden");
    });

    byTestId("nav-features").addEventListener("click", () => {
      byTestId("features-view").classList.remove("hidden");
      loadFeaturesView();
    });
    byTestId("btn-close-features").addEventListener("click", () => {
      byTestId("features-view").classList.add("hidden");
    });

    // Direct issue jump
    byTestId("btn-issue-jump").addEventListener("click", () => {
      openIssueDetail(byTestId("issue-jump-input").value);
    });
    byTestId("issue-jump-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") openIssueDetail(byTestId("issue-jump-input").value);
    });

    // Search & Filters & Sort
    byTestId("btn-search-submit").addEventListener("click", () => {
      state.page = 1;
      syncStateFromFilterInputs();
      loadIssues();
    });
    byTestId("search-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        state.page = 1;
        syncStateFromFilterInputs();
        loadIssues();
      }
    });
    byTestId("btn-search-clear").addEventListener("click", () => {
      byTestId("search-input").value = "";
      state.page = 1;
      syncStateFromFilterInputs();
      loadIssues();
    });

    byTestId("btn-apply-filters").addEventListener("click", () => {
      state.page = 1;
      syncStateFromFilterInputs();
      loadIssues();
    });
    [
      "filter-status", "filter-priority", "filter-type", "filter-label",
      "filter-assignee", "filter-reporter", "filter-milestone",
      "filter-created-from", "filter-created-to", "filter-updated-from", "filter-updated-to",
      "filter-archived", "sort-by-select", "sort-order-select",
    ].forEach((id) => {
      byTestId(id).addEventListener("change", () => {
        state.page = 1;
        syncStateFromFilterInputs();
        loadIssues();
      });
    });

    byTestId("btn-reset-filters").addEventListener("click", () => {
      byTestId("search-input").value = "";
      byTestId("filter-status").value = "";
      byTestId("filter-priority").value = "";
      byTestId("filter-type").value = "";
      byTestId("filter-label").value = "";
      byTestId("filter-assignee").value = "";
      byTestId("filter-reporter").value = "";
      byTestId("filter-milestone").value = "";
      byTestId("filter-created-from").value = "";
      byTestId("filter-created-to").value = "";
      byTestId("filter-updated-from").value = "";
      byTestId("filter-updated-to").value = "";
      byTestId("filter-archived").value = "exclude";
      byTestId("sort-by-select").value = "number";
      byTestId("sort-order-select").value = "asc";
      byTestId("pagination-per-page").value = "10";
      state.page = 1;
      syncStateFromFilterInputs();
      loadIssues();
    });

    // Pagination
    byTestId("pagination-per-page").addEventListener("change", () => {
      state.page = 1;
      syncStateFromFilterInputs();
      loadIssues();
    });
    byTestId("pagination-first").addEventListener("click", () => { state.page = 1; loadIssues(); });
    byTestId("pagination-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadIssues(); } });
    byTestId("pagination-next").addEventListener("click", () => { if (state.page < (state.lastTotalPages || 1)) { state.page += 1; loadIssues(); } });
    byTestId("pagination-last").addEventListener("click", () => { state.page = state.lastTotalPages || 1; loadIssues(); });

    // Bulk Selection & Operations
    byTestId("select-all-issues").addEventListener("change", (e) => {
      if (e.target.checked) {
        state.currentItems.forEach((it) => state.selectedNumbers.add(it.number));
      } else {
        state.currentItems.forEach((it) => state.selectedNumbers.delete(it.number));
      }
      loadIssues();
    });
    byTestId("btn-bulk-apply").addEventListener("click", async () => {
      const nums = Array.from(state.selectedNumbers);
      if (nums.length === 0) return;
      const payload = { action: "update", issue_numbers: nums };
      const st = byTestId("bulk-status-select").value;
      const pr = byTestId("bulk-priority-select").value;
      const asg = byTestId("bulk-assignee-input").value.trim();
      const unassign = byTestId("bulk-clear-assignee").checked;
      const addLbls = byTestId("bulk-add-labels-input").value.trim();
      const remLbls = byTestId("bulk-remove-labels-input").value.trim();
      if (st) payload.status = st;
      if (pr) payload.priority = pr;
      if (unassign) payload.unassign = true;
      else if (asg) payload.assignee = asg;
      if (addLbls) payload.add_labels = addLbls.split(",").map((s) => s.trim()).filter(Boolean);
      if (remLbls) payload.remove_labels = remLbls.split(",").map((s) => s.trim()).filter(Boolean);
      try {
        await apiFetch("/api/issues/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        byTestId("bulk-status-select").value = "";
        byTestId("bulk-priority-select").value = "";
        byTestId("bulk-assignee-input").value = "";
        byTestId("bulk-clear-assignee").checked = false;
        byTestId("bulk-add-labels-input").value = "";
        byTestId("bulk-remove-labels-input").value = "";
        loadIssues();
      } catch (err) {
        showGlobalError(err.message);
      }
    });

    byTestId("btn-bulk-delete").addEventListener("click", () => {
      const nums = Array.from(state.selectedNumbers);
      if (nums.length === 0) return;
      openDeleteConfirm(`Permanently delete ${nums.length} selected issue(s) (${nums.map((n) => "#" + n).join(", ")})?`, async () => {
        await apiFetch("/api/issues/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", issue_numbers: nums }),
        });
        state.selectedNumbers.clear();
        loadIssues();
      });
    });

    // Create Issue Modal
    byTestId("btn-new-issue").addEventListener("click", () => {
      byTestId("create-summary-input").value = "";
      byTestId("create-status-select").value = "open";
      byTestId("create-priority-select").value = "medium";
      byTestId("create-type-select").value = "task";
      byTestId("create-labels-input").value = "";
      byTestId("create-assignee-input").value = "";
      byTestId("create-reporter-input").value = "";
      byTestId("create-milestone-input").value = "";
      byTestId("create-due-date-input").value = "";
      byTestId("create-rel-target-input").value = "";
      editors["create-desc"].clear();
      byTestId("create-validation-error").classList.add("hidden");
      byTestId("create-issue-modal").classList.remove("hidden");
    });
    byTestId("btn-create-cancel").addEventListener("click", () => {
      byTestId("create-issue-modal").classList.add("hidden");
    });
    byTestId("btn-create-submit").addEventListener("click", async () => {
      const errBox = byTestId("create-validation-error");
      errBox.classList.add("hidden");
      const relTarget = byTestId("create-rel-target-input").value.trim();
      const rels = relTarget
        ? [{ relation_type: byTestId("create-rel-type-select").value, target_number: relTarget }]
        : [];
      const labelsRaw = byTestId("create-labels-input").value.trim();
      const payload = {
        summary: byTestId("create-summary-input").value,
        description: editors["create-desc"].getValue(),
        status: byTestId("create-status-select").value,
        priority: byTestId("create-priority-select").value,
        issue_type: byTestId("create-type-select").value,
        labels: labelsRaw ? labelsRaw.split(",").map((s) => s.trim()).filter(Boolean) : [],
        assignee: byTestId("create-assignee-input").value.trim() || null,
        reporter: byTestId("create-reporter-input").value.trim() || null,
        milestone: byTestId("create-milestone-input").value.trim() || null,
        due_date: byTestId("create-due-date-input").value || null,
        relationships: rels,
      };
      try {
        const created = await apiFetch("/api/issues", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        byTestId("create-issue-modal").classList.add("hidden");
        await loadIssues();
        renderIssueDetail(created);
        qs("#list-view").classList.add("hidden");
        qs("#detail-view").classList.remove("hidden");
      } catch (err) {
        errBox.textContent = err.message;
        errBox.classList.remove("hidden");
      }
    });

    // Detail Workflow & Edit Actions
    byTestId("detail-status-select").addEventListener("change", async () => {
      if (!state.currentIssue) return;
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: byTestId("detail-status-select").value }),
        });
        renderIssueDetail(updated);
      } catch (err) {
        showGlobalError(err.message);
      }
    });
    byTestId("btn-close-issue").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}/close`, { method: "POST" });
        renderIssueDetail(updated);
      } catch (err) {
        showGlobalError(err.message);
      }
    });
    byTestId("btn-reopen-issue").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}/reopen`, { method: "POST" });
        renderIssueDetail(updated);
      } catch (err) {
        showGlobalError(err.message);
      }
    });
    byTestId("btn-clone-issue").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      try {
        const cloned = await apiFetch(`/api/issues/${state.currentIssue.number}/clone`, { method: "POST" });
        renderIssueDetail(cloned);
      } catch (err) {
        showGlobalError(err.message);
      }
    });
    byTestId("btn-archive-issue").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}/archive`, { method: "POST" });
        renderIssueDetail(updated);
      } catch (err) {
        showGlobalError(err.message);
      }
    });
    byTestId("btn-restore-issue").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}/restore`, { method: "POST" });
        renderIssueDetail(updated);
      } catch (err) {
        showGlobalError(err.message);
      }
    });
    byTestId("btn-delete-issue").addEventListener("click", () => {
      if (!state.currentIssue) return;
      const num = state.currentIssue.number;
      openDeleteConfirm(`Permanently delete issue #${num} (${state.currentIssue.summary})?`, async () => {
        await apiFetch(`/api/issues/${num}`, { method: "DELETE" });
        state.selectedNumbers.delete(num);
        showListView();
      });
    });

    // Quick add label on detail view
    byTestId("btn-add-issue-label").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      const lbl = byTestId("detail-add-label-input").value.trim();
      if (!lbl) return;
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ add_labels: [lbl] }),
        });
        byTestId("detail-add-label-input").value = "";
        renderIssueDetail(updated);
      } catch (err) {
        showGlobalError(err.message);
      }
    });

    // Inline Edit Form
    byTestId("btn-edit-issue").addEventListener("click", () => {
      if (!state.currentIssue) return;
      const iss = state.currentIssue;
      byTestId("edit-summary-input").value = iss.summary;
      byTestId("edit-priority-select").value = iss.priority;
      byTestId("edit-type-select").value = iss.issue_type;
      byTestId("edit-assignee-input").value = iss.assignee || "";
      byTestId("edit-reporter-input").value = iss.reporter || "";
      byTestId("edit-milestone-input").value = iss.milestone || "";
      byTestId("edit-due-date-input").value = iss.due_date || "";
      byTestId("edit-labels-input").value = (iss.labels || []).join(", ");
      editors["edit-desc"].setValue(iss.description || "");
      byTestId("edit-validation-error").classList.add("hidden");
      byTestId("edit-issue-form").classList.remove("hidden");
    });
    byTestId("btn-edit-cancel").addEventListener("click", () => {
      byTestId("edit-issue-form").classList.add("hidden");
    });
    byTestId("btn-edit-save").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      const errBox = byTestId("edit-validation-error");
      errBox.classList.add("hidden");
      const labelsRaw = byTestId("edit-labels-input").value.trim();
      const payload = {
        summary: byTestId("edit-summary-input").value,
        description: editors["edit-desc"].getValue(),
        priority: byTestId("edit-priority-select").value,
        issue_type: byTestId("edit-type-select").value,
        assignee: byTestId("edit-assignee-input").value.trim() || null,
        reporter: byTestId("edit-reporter-input").value.trim() || undefined,
        milestone: byTestId("edit-milestone-input").value.trim() || null,
        due_date: byTestId("edit-due-date-input").value || null,
        set_labels: labelsRaw ? labelsRaw.split(",").map((s) => s.trim()).filter(Boolean) : [],
      };
      try {
        const updated = await apiFetch(`/api/issues/${state.currentIssue.number}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        byTestId("edit-issue-form").classList.add("hidden");
        renderIssueDetail(updated);
      } catch (err) {
        errBox.textContent = err.message;
        errBox.classList.remove("hidden");
      }
    });

    // Add Relationship
    byTestId("btn-add-relationship").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      const rt = byTestId("relationship-type-select").value;
      const target = byTestId("relationship-target-input").value.trim();
      try {
        await apiFetch(`/api/issues/${state.currentIssue.number}/relationships`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ relation_type: rt, target_number: target }),
        });
        byTestId("relationship-target-input").value = "";
        await openIssueDetail(state.currentIssue.number);
      } catch (err) {
        showGlobalError(err.message);
      }
    });

    // Add Comment
    byTestId("btn-add-comment").addEventListener("click", async () => {
      if (!state.currentIssue) return;
      const errBox = byTestId("comment-validation-error");
      errBox.classList.add("hidden");
      const author = byTestId("comment-author-input").value.trim() || undefined;
      const body = editors["new-comment"].getValue();
      try {
        await apiFetch(`/api/issues/${state.currentIssue.number}/comments`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body, author }),
        });
        editors["new-comment"].clear();
        await openIssueDetail(state.currentIssue.number);
      } catch (err) {
        errBox.textContent = err.message;
        errBox.classList.remove("hidden");
      }
    });

    // Delete confirmation modal
    byTestId("btn-cancel-delete").addEventListener("click", () => {
      state.pendingDeleteAction = null;
      byTestId("confirm-delete-modal").classList.add("hidden");
    });
    byTestId("btn-confirm-delete").addEventListener("click", async () => {
      const fn = state.pendingDeleteAction;
      state.pendingDeleteAction = null;
      byTestId("confirm-delete-modal").classList.add("hidden");
      if (fn) {
        try {
          await fn();
        } catch (err) {
          showGlobalError(err.message);
        }
      }
    });

    // Export & Import
    byTestId("btn-export-submit").addEventListener("click", async () => {
      const scope = byTestId("export-scope-select").value;
      const fmt = byTestId("export-format-select").value;
      const params = scope === "filtered" ? buildFilterQueryParams(false) : new URLSearchParams();
      params.set("format", fmt);
      if (scope === "single") {
        const issueNum = byTestId("export-issue-number-input").value.trim();
        if (issueNum) params.set("issue", issueNum);
      }
      try {
        const resp = await fetch(`/api/export?${params.toString()}`);
        const text = await resp.text();
        if (!resp.ok) {
          let msg = text;
          try { msg = JSON.parse(text).message || text; } catch (_) {}
          throw new Error(msg);
        }
        byTestId("export-preview-output").value = text;
        const ext = fmt === "markdown" ? "md" : fmt;
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `itrack-export.${ext}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (err) {
        showGlobalError(err.message);
      }
    });

    const importFileInput = byTestId("import-file-input");
    importFileInput.addEventListener("change", () => {
      const f = importFileInput.files && importFileInput.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        byTestId("import-json-textarea").value = String(reader.result || "");
      };
      reader.readAsText(f);
    });

    byTestId("btn-import-submit").addEventListener("click", async () => {
      const msgEl = byTestId("import-result-message");
      msgEl.className = "hidden";
      const rawText = byTestId("import-json-textarea").value;
      try {
        const res = await apiFetch("/api/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ data: rawText }),
        });
        msgEl.textContent = `Imported ${res.imported_count} issue(s): ${res.imported_numbers.map((n) => "#" + n).join(", ")}`;
        msgEl.className = "success-banner";
        loadIssues();
      } catch (err) {
        msgEl.textContent = err.message;
        msgEl.className = "error-banner";
      }
    });

    loadIssues();
  }

  function openDeleteConfirm(message, onConfirm) {
    byTestId("confirm-delete-message").textContent = message;
    state.pendingDeleteAction = onConfirm;
    byTestId("confirm-delete-modal").classList.remove("hidden");
  }

  document.addEventListener("DOMContentLoaded", init);
})();
</script>
</body>
</html>
"""


def render_spa_html() -> str:
    """Return the self-contained single-page Web UI HTML document."""
    return _SPA_HTML
