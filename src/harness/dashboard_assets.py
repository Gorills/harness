from __future__ import annotations

DASHBOARD_CSS = r"""
:root {
  color-scheme: light;
  --bg: #f4f6f7;
  --panel: #ffffff;
  --text: #202b33;
  --text-secondary: #56636e;
  --text-muted: #65737e;
  --border: #e1e6e9;
  --border-strong: #c2cdd3;
  --accent: #137d75;
  --accent-hover: #0d625c;
  --accent-soft: #e6f3f1;
  --danger: #b5373f;
  --danger-soft: #fff0f0;
  --warning: #946017;
  --warning-soft: #fff4dc;
  --success: #287750;
  --sidebar: #202a32;
  --sidebar-text: #d7dfe4;
  --radius: 6px;
  font-family: Inter, "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; color: var(--text); background: var(--bg); }
a { color: inherit; text-decoration: none; }
a:hover { color: var(--accent); }
button, input, select, textarea { font: inherit; }
button, summary, a.btn { cursor: pointer; }
button { color: inherit; }
button:disabled { opacity: .55; cursor: wait; }
button, a, summary, input, select, textarea { -webkit-tap-highlight-color: transparent; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: 28px; font-weight: 650; line-height: 1.25; letter-spacing: -.65px; }
h2 { font-size: 16px; font-weight: 650; }
h3 { font-size: 14px; font-weight: 650; }
svg { flex-shrink: 0; }
.ui-icon { width: 18px; height: 18px; }
.sr-only, .skip-link:not(:focus) { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
.skip-link:focus { position: fixed; top: 10px; left: 10px; padding: 10px 16px; z-index: 100; background: white; }
.mono { font-family: "SFMono-Regular", Consolas, monospace; font-size: .9em; overflow-wrap: anywhere; }
.muted, .section-note, .hero-copy { color: var(--text-secondary); }
.section-note { font-size: 12px; }
.app-layout { display: grid; grid-template-columns: 220px minmax(0,1fr); min-height: 100dvh; }
.app-sidebar { position: sticky; top: 0; height: 100dvh; display: flex; flex-direction: column; padding: 26px 12px 12px; background: var(--sidebar); color: var(--sidebar-text); min-width: 0; }
.brand { display: flex; align-items: center; gap: 11px; padding: 0 12px 26px; color: #fff; }
.brand:hover { color: #fff; }
.brand-mark { width: 32px; height: 32px; display: grid; place-items: center; color: #fff; background: #407d78; border-radius: 6px; font-size: 21px; font-weight: 750; }
.brand-copy { display: grid; }
.brand-copy strong { font-size: 17px; letter-spacing: -.3px; }
.brand-copy small { color: #a6b5be; font-size: 11px; }
.project-navigation { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.app-sidebar .primary-navigation, .app-sidebar .nav-projects-heading, .app-sidebar .nav-filter { flex-shrink: 0; }
.app-sidebar .nav-projects-list { flex: 1; min-height: 0; overflow-y: auto; scrollbar-width: none; }
.app-sidebar .nav-projects-list::-webkit-scrollbar { display: none; }
.primary-navigation { display: grid; gap: 4px; padding-bottom: 24px; }
.overview-link { display: flex; align-items: center; gap: 10px; min-height: 40px; border-radius: 5px; padding: 9px 12px; font-size: 13px; }
.overview-link:hover, .nav-project-link:hover { background: #2b3740; color: #fff; }
.overview-link.is-current { background: #35474d; color: #fff; }
.nav-count { margin-left: auto; font-size: 11px; font-variant-numeric: tabular-nums; color: #bfced5; }
.nav-projects-heading { padding: 4px 12px 10px; }
.nav-label { color: #a4b3bd; font-size: 11px; font-weight: 650; letter-spacing: .8px; text-transform: uppercase; }
.nav-filter { padding: 0 7px 12px; }
.nav-filter input { background: #28353e; color: #fff; border-color: #41505b; min-height: 33px; font-size: 12px; }
.nav-filter input::placeholder { color: #acbcc6; }
.nav-project-link { display: flex; align-items: center; gap: 10px; min-height: 35px; padding: 8px 12px; border-radius: 5px; font-size: 12px; }
.nav-project-link[aria-current] { color: #fff; background: #35474d; }
.nav-project-name { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.project-dot { width: 6px; height: 6px; border-radius: 50%; background: #73818c; flex-shrink: 0; }
.project-dot.is-active { background: #6db9a9; }
.project-dot.has-review { background: #e9bb68; }
.review-count { background: #4e4536; color: #ffdba0; border-radius: 3px; padding: 0 5px; }
.nav-empty { color: #adbbc3; font-size: 12px; padding: 12px; }
.sidebar-footer { border-top: 1px solid #3b474f; padding: 14px 10px 2px; margin-top: 14px; }
.live-indicator { display: flex; align-items: center; gap: 7px; color: var(--text-secondary); font-size: 11px; }
.sidebar-footer .live-indicator { color: #b9c7cf; flex-wrap: wrap; }
.live-dot { width: 6px; height: 6px; border-radius: 50%; background: #9ba7af; flex-shrink: 0; }
[data-state="live"] .live-dot { background: #4eac83; }
[data-state="reconnecting"] .live-dot { background: #d6a14d; }
[data-state="error"] .live-dot { background: #cb5454; }
.update-link { border: 0; padding: 0; background: none; color: inherit; font-size: inherit; text-decoration: underline; text-underline-offset: 3px; }
.sidebar-footer .update-link { display: none; }
.app-stage { min-width: 0; }
.context-header { height: 54px; border-bottom: 1px solid var(--border); background: #fff; padding: 0 30px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.breadcrumbs ol { display: flex; align-items: center; gap: 10px; list-style: none; margin: 0; padding: 0; font-size: 12px; color: var(--text-secondary); flex-wrap: wrap; }
.breadcrumbs li { display: flex; align-items: center; gap: 10px; }
.breadcrumbs li + li::before { content: "/"; color: #b4c0c6; }
.breadcrumbs [aria-current] { color: var(--text); font-weight: 550; }
.header-live-indicator { flex-shrink: 0; }
.header-live-indicator .update-link { margin-left: 8px; }
.mobile-navigation { display: none; }
main { padding: 28px 30px 40px; }
.content-frame { margin: 0 auto; max-width: 1800px; min-width: 0; }
.page-intro { display: flex; gap: 24px; align-items: flex-end; justify-content: space-between; margin-bottom: 24px; }
.page-intro > div { min-width: 0; }
.eyebrow { color: var(--text-muted); text-transform: uppercase; letter-spacing: .9px; font-size: 10px; font-weight: 650; margin-bottom: 5px; }
.hero-copy { font-size: 13px; margin-top: 7px; }
.workspace-path { overflow-wrap: anywhere; }
.task-search-form { display: flex; align-items: center; gap: 6px; width: min(380px, 45%); }
.task-search-form label { flex: 1; min-width: 0; }
.task-search-form input { min-height: 36px; font-size: 12px; }
input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), select, textarea { width: 100%; color: var(--text); background: #fff; border: 1px solid var(--border-strong); border-radius: 5px; padding: 8px 10px; min-height: 36px; outline-offset: 1px; }
input::placeholder, textarea::placeholder { color: #70808b; }
textarea { resize: vertical; min-height: 88px; }
label { font-size: 12px; font-weight: 550; }
label select, label textarea, label input { display: block; margin-top: 5px; font-weight: 400; }
.task-search-form input, .directory-search input { margin-top: 0; }
.btn, a.btn, summary { transition: background-color .15s, border-color .15s; }
.btn, a.btn { display: inline-flex; align-items: center; justify-content: center; min-height: 33px; gap: 6px; padding: 6px 12px; background: #fff; border: 1px solid var(--border-strong); border-radius: 5px; color: var(--text); font-size: 12px; font-weight: 550; white-space: nowrap; }
.btn:hover, a.btn:hover { background: #f0f4f5; border-color: #9dafb9; color: var(--text); }
.btn-primary, a.btn-primary { color: #fff; background: var(--accent); border-color: var(--accent); }
.btn-primary:hover, a.btn-primary:hover { color: #fff; background: var(--accent-hover); border-color: var(--accent-hover); }
.btn-danger, a.btn-danger { color: var(--danger); border-color: #e7b7ba; }
.btn-danger:hover { color: var(--danger); background: var(--danger-soft); border-color: var(--danger); }
.text-link { color: var(--accent); font-size: 12px; font-weight: 550; }
.text-link:hover { text-decoration: underline; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); min-width: 0; }
.panel + .panel { margin-top: 16px; }
.panel-head { padding: 18px 20px; display: flex; align-items: center; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--border); }
.panel-head .section-note { margin-top: 4px; }
.panel-body { padding: 20px; }
.panel-body > p + p { margin-top: 12px; }
.panel-kicker { color: var(--text-muted); font-size: 10px; font-weight: 650; letter-spacing: .8px; text-transform: uppercase; margin-bottom: 4px; }
.portfolio-layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 20px; align-items: start; }
.project-directory { display: flex; flex-direction: column; }
.directory-toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 16px 18px; border-bottom: 1px solid var(--border); }
.directory-search { flex: 1; min-width: 155px; }
.directory-search input { min-height: 33px; font-size: 12px; border-color: var(--border); background: #f9fafb; }
.filter-buttons { display: flex; gap: 3px; }
.filter-buttons button { background: transparent; border: 1px solid transparent; border-radius: 4px; color: var(--text-secondary); font-size: 11px; padding: 7px 9px; min-height: 33px; }
.filter-buttons button:hover { background: #f2f5f6; }
.filter-buttons button[aria-pressed="true"] { background: var(--accent-soft); color: #146b64; font-weight: 650; }
.directory-scroll { max-height: calc(100dvh - 270px); overflow-y: auto; scrollbar-width: thin; }
.project-directory-head, .project-row { display: grid; grid-template-columns: minmax(135px, .9fr) minmax(130px, 1.1fr) 125px 70px; align-items: start; gap: 18px; padding: 17px 18px; }
.project-directory-head { position: sticky; top: 0; z-index: 1; background: #f9fafb; border-bottom: 1px solid var(--border); padding-top: 9px; padding-bottom: 9px; color: var(--text-muted); font-size: 10px; font-weight: 550; }
.project-row { border-bottom: 1px solid #edf0f2; font-size: 12px; }
.project-row:last-of-type { border-bottom: 0; }
.project-row:hover { background: #fbfcfc; }
.project-row > div { min-width: 0; }
.project-row-name, .project-row-focus, .project-row-state { display: grid; gap: 6px; }
.project-name { display: flex; align-items: center; gap: 9px; font-weight: 650; font-size: 13px; }
.project-name > span:last-child { overflow-wrap: anywhere; }
.project-avatar { width: 24px; height: 24px; border: 1px solid #d9e4e6; border-radius: 5px; background: #edf3f3; color: #496b6e; display: grid; place-items: center; font-size: 11px; font-weight: 650; flex-shrink: 0; }
.project-row-path { color: var(--text-muted); font-size: 10px; padding-left: 33px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-row-focus > a { font-size: 12px; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.project-row-next { font-size: 10px; color: var(--text-muted); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.project-row-state { font-size: 11px; }
.project-row-state > span:last-child { color: var(--text-muted); font-size: 10px; }
.project-review-link { color: var(--warning); font-weight: 600; }
.project-active-count { color: #386d66; }
.project-row-links { display: flex; align-items: center; justify-content: flex-end; gap: 10px; font-size: 10px; padding-top: 4px; }
.project-row-links > a { color: var(--text-secondary); }
.project-menu { position: relative; }
.project-menu summary { color: var(--text-secondary); list-style: none; padding: 1px 3px; }
.project-menu summary::-webkit-details-marker { display: none; }
.project-row-links:has(.project-menu[open]) { grid-column: 1 / -1; justify-content: flex-start; }
.project-menu[open] { display: flex; align-items: center; gap: 10px; }
.project-menu > div { display: flex; flex-wrap: wrap; gap: 5px; background: #f5f8f8; padding: 3px; border: 1px solid var(--border); border-radius: 5px; }
.project-menu a { padding: 8px 10px; }
.project-menu a:hover { background: var(--bg); }
.filter-empty { padding: 32px 24px; color: var(--text-secondary); text-align: center; }
.portfolio-layout .inbox { margin-top: 0; }
.inbox { display: flex; flex-direction: column; }
.inbox .panel-head { padding: 16px 18px; }
.queue-count { font-size: 11px; border: 1px solid #ebd8b1; color: var(--warning); background: var(--warning-soft); border-radius: 4px; padding: 1px 6px; margin-left: 6px; vertical-align: middle; }
.inbox-scroll { max-height: calc(100dvh - 283px); overflow-y: auto; scrollbar-width: thin; }
.task-row { display: grid; grid-template-columns: minmax(0, 1fr) 200px; gap: 26px; padding: 20px 24px; border-bottom: 1px solid var(--border); align-items: start; }
.task-row:last-child { border-bottom: 0; }
.task-row[data-state="completed"], .task-row[data-state="cancelled"] { background: #fcfdfd; }
.task-row-content { min-width: 0; }
.task-row-title { font-size: 15px; font-weight: 600; line-height: 1.55; margin: 6px 0; overflow-wrap: anywhere; }
.task-row-meta { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; color: var(--text-muted); font-size: 11px; }
.task-row-meta a { color: var(--text-secondary); }
.task-row-meta > span:empty { display: none; }
.task-git-branch { min-width: 0; overflow-wrap: anywhere; }
.task-row-meta .mono { font-weight: 450; }
.row-summary, .row-next { font-size: 12px; color: var(--text-secondary); margin: 8px 0 12px; line-height: 1.6; overflow-wrap: anywhere; white-space: pre-line; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.row-next { color: #476b69; }
.task-row-meta .row-result-link { color: var(--accent); }
.task-row-aside { min-width: 0; }
.row-actions { display: flex; align-items: flex-start; gap: 7px; flex-wrap: wrap; }
.row-actions > .state-dropdown { flex-basis: 100%; margin-bottom: 5px; }
.row-actions > form { margin: 0; }
.row-disclosure summary { list-style: none; min-height: 33px; padding: 6px 10px; border: 1px solid var(--border-strong); border-radius: 5px; font-size: 12px; font-weight: 550; }
.row-disclosure summary::-webkit-details-marker { display: none; }
.row-disclosure summary:hover { background: #f1f5f6; }
.state-dropdown summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; border-color: var(--border); background: #f8fafb; }
.row-disclosure[open] { flex-basis: 100%; }
.row-disclosure[open] summary { background: #eef3f4; }
.row-disclosure .feedback-form { margin-top: 10px; padding: 12px; border: 1px solid var(--border); background: #f9fbfb; border-radius: 5px; }
.pill { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 550; line-height: 1.4; padding: 3px 7px; border-radius: 4px; background: #f0f3f5; color: var(--text-secondary); }
.pill::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.pill-working { color: #267267; background: #e8f4f1; }
.pill-review { color: #895913; background: var(--warning-soft); }
.pill-waiting { color: #725e34; background: #f6f1e5; }
.pill-completed { color: #427051; background: #edf5ee; }
.pill-cancelled { color: #7b6467; background: #f4eeef; }
.state-dropdown .pill { background: transparent; padding: 0; }
.inbox .task-row { display: flex; flex-direction: column; gap: 12px; padding: 20px 18px; }
.inbox .task-row-title { font-size: 14px; }
.inbox .task-row-meta { font-size: 10px; gap: 8px; }
.inbox .task-row-meta > .task-git-branch { display: none; }
.inbox .task-row-aside { width: 100%; }
.inbox .row-summary { font-size: 12px; margin-bottom: 9px; }
.inbox .row-next { display: none; }
.inbox .row-actions > .state-dropdown { flex-basis: auto; margin-bottom: 0; }
.inbox .row-actions > .state-dropdown[open] { flex-basis: 100%; }
.inbox .state-dropdown summary { padding: 7px; }
.inbox .state-dropdown .pill { font-size: 10px; }
.inbox .row-actions .btn, .inbox .row-disclosure > summary { font-size: 11px; padding-left: 9px; padding-right: 9px; }
.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: 48px 30px; text-align: center; color: var(--text-secondary); }
.empty-state strong { color: var(--text); font-size: 14px; }
.empty-state p { font-size: 12px; max-width: 340px; }
.empty-check { width: 36px; height: 36px; display: grid; place-items: center; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-size: 20px; margin-bottom: 8px; }
.task-scopes { display: flex; gap: 24px; padding: 0 24px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.task-scopes a { display: flex; align-items: center; gap: 8px; border-bottom: 2px solid transparent; padding: 15px 0 13px; color: var(--text-secondary); font-size: 12px; }
.task-scopes a[aria-current] { border-color: var(--accent); color: var(--accent); font-weight: 650; }
.task-scopes a > span { font-size: 10px; background: #f0f3f5; border-radius: 3px; padding: 1px 5px; font-weight: 450; color: var(--text-secondary); }
.action-row { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
nav.action-row { border-top: 1px solid var(--border); padding: 16px 24px; }
.inbox nav.action-row { padding: 12px 18px; }
.inbox nav.action-row .section-note { flex-basis: 100%; order: -1; }
.search-limit { padding: 16px 24px; color: var(--text-secondary); font-size: 12px; }
.project-tabs { display: flex; align-items: center; gap: 24px; border-bottom: 1px solid var(--border); margin: -12px 0 24px; }
.project-tabs a { padding: 12px 0; color: var(--text-secondary); font-size: 12px; border-bottom: 2px solid transparent; }
.project-tabs a[aria-current] { color: var(--accent); border-color: var(--accent); font-weight: 650; }
.project-tabs .return-task { margin-left: auto; }
.workspace-switcher { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 18px; }
.workspace-switcher a { font-size: 11px; border-radius: 4px; padding: 5px 10px; border: 1px solid var(--border); background: #fff; }
.workspace-switcher a[aria-current] { border-color: #a8ccc7; color: var(--accent); background: var(--accent-soft); }
.folder-facts { margin-top: 18px; }
.folder-facts > summary, .facts-card > summary { padding: 14px 20px; font-size: 12px; color: var(--text-secondary); }
.folder-fact { display: grid; gap: 4px; padding: 10px 0; font-size: 12px; overflow-wrap: anywhere; }
.folder-fact span { font-size: 11px; color: var(--text-secondary); }
.feedback-form { display: grid; gap: 12px; }
.feedback-form .btn { justify-self: start; }
.feedback-disclosure, .state-control { border-bottom: 1px solid var(--border); padding: 14px 0; }
.feedback-disclosure > summary { font-size: 12px; font-weight: 550; color: var(--text-secondary); }
.feedback-disclosure .feedback-form { margin-top: 14px; }
.state-control h3 { margin-bottom: 12px; }
.action-card .panel-body > .action-row + .feedback-disclosure { margin-top: 8px; }
.action-card .feedback-disclosure:last-child { border-bottom: 0; padding-bottom: 0; }
.task-intro { align-items: start; }
.task-title { max-width: 1000px; font-size: 27px; overflow-wrap: anywhere; }
.task-intro-meta { display: flex; align-items: center; gap: 14px; margin-top: 14px; font-size: 11px; color: var(--text-secondary); flex-wrap: wrap; }
.task-sections { display: flex; gap: 24px; margin-bottom: 20px; color: var(--text-secondary); font-size: 12px; }
.task-layout { display: grid; grid-template-columns: minmax(0,1fr) 320px; align-items: start; gap: 20px; }
.task-summary, .task-main-column, .task-side-column { min-width: 0; }
.task-update { padding: 24px; }
.task-update-summary { margin-top: 14px; font-size: 14px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; }
.next-step { border-top: 1px solid var(--border); padding-top: 16px; margin-top: 20px; font-size: 13px; }
.next-step > span { color: var(--accent); font-size: 11px; font-weight: 650; }
.next-step > p { margin-top: 5px; white-space: pre-wrap; overflow-wrap: anywhere; }
.task-layout > .action-card { margin-top: 0; }
.task-main-column > .timeline-panel { margin-top: 16px; }
.task-side-column > .facts-card { margin-top: 16px; }
.timeline { display: grid; gap: 0; }
.timeline-item { border-left: 2px solid var(--border); padding: 0 0 24px 20px; position: relative; }
.timeline-item::before { content: ""; position: absolute; left: -5px; top: 6px; width: 8px; height: 8px; background: #a8b8c0; border: 2px solid #fff; border-radius: 50%; }
.timeline-item:last-child { padding-bottom: 0; }
.timeline-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.timeline-title { font-size: 12px; }
.timeline-time { font-size: 10px; color: var(--text-muted); }
.timeline-content { font-size: 12px; line-height: 1.7; margin-top: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.timeline-content .next-step { font-size: 12px; padding-top: 8px; margin-top: 10px; }
.timeline-branch { font-size: 11px; margin: 8px 0; color: var(--text-secondary); }
.feedback-quote { border-left: 2px solid #9fcac5; margin: 10px 0; padding: 8px 12px; background: #f5faf9; white-space: pre-wrap; }
.verification-list { display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; }
.verification-item { display: grid; gap: 5px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.verification-item:last-child { border-bottom: 0; }
.verification-head { display: flex; align-items: center; gap: 12px; justify-content: space-between; }
.verification-name { font-size: 13px; font-weight: 550; }
.verification-evidence { font-size: 12px; color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.verification-status { font-size: 10px; padding: 2px 6px; border-radius: 3px; background: #edf3ef; color: var(--success); }
.verification-status[data-status="failed"] { background: var(--danger-soft); color: var(--danger); }
.verification-status[data-status="not_run"] { background: var(--warning-soft); color: var(--warning); }
.fact-list { display: grid; gap: 0; margin: 0; }
.fact { display: grid; grid-template-columns: 110px minmax(0,1fr); gap: 12px; padding: 9px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.fact:last-child { border-bottom: 0; }
.fact dt { color: var(--text-secondary); }
.fact dd { margin: 0; overflow-wrap: anywhere; }
.facts-card .fact { grid-template-columns: 90px minmax(0,1fr); font-size: 11px; }
.workspace-setting + .workspace-setting { border-top: 1px solid var(--border); padding-top: 20px; margin-top: 20px; }
.workspace-setting h3 { margin-bottom: 12px; overflow-wrap: anywhere; }
.visibility-control { display: grid; gap: 12px; }
.visibility-control label { display: flex; gap: 10px; align-items: center; }
.visibility-control input[type="radio"] { width: auto; }
.visibility-control .btn { justify-self: start; }
.skill-scope-panel .panel-body { padding-top: 18px; }
.skill-scope-hint, .skill-empty, .skill-scope-detection, .skill-delivery-count { color: var(--text-secondary); font-size: 12px; }
.skill-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 10px; }
.skill-section-heading h3 { font-size: 13px; font-weight: 650; }
.skill-delivery { margin: 24px 0; }
.skill-delivery-row { border-bottom: 1px solid var(--border); }
.skill-delivery-row > summary { display: grid; grid-template-columns: minmax(100px,1fr) 90px minmax(150px,1fr) 16px; align-items: center; gap: 16px; min-height: 52px; padding: 10px 0; list-style: none; }
.skill-delivery-name { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.skill-delivery-status { justify-self: end; color: var(--text-secondary); font-size: 12px; text-align: right; }
[data-skill-status="current"] .skill-delivery-status { color: var(--success); }
[data-skill-status="pending"] .skill-delivery-status, [data-skill-status="no_host"] .skill-delivery-status { color: var(--warning); }
[data-skill-status="error"] .skill-delivery-status, [data-skill-status="conflict"] .skill-delivery-status { color: var(--danger); }
.skill-scope-row { border-top: 1px solid var(--border); }
.skill-scope-row:last-child { border-bottom: 1px solid var(--border); }
.skill-scope-row > summary { display: grid; grid-template-columns: minmax(120px,1fr) minmax(150px,1fr) 100px 16px; align-items: center; gap: 16px; min-height: 52px; padding: 10px 0; list-style: none; }
.skill-scope-row > summary:hover, .skill-delivery-row > summary:hover { background: var(--bg); }
.skill-scope-name { font-size: 13px; font-weight: 600; }
.skill-scope-current { justify-self: end; font-size: 12px; background: var(--bg); color: var(--text-secondary); padding: 3px 10px; border-radius: 4px; }
[data-mode="included"] > summary .skill-scope-current { color: var(--accent); background: var(--accent-soft); }
.skill-chevron { color: var(--text-muted); transition: transform 150ms; }
.skill-scope-row[open] > summary .skill-chevron, .skill-delivery-row[open] > summary .skill-chevron { transform: rotate(180deg); }
.skill-scope-row > summary::-webkit-details-marker, .skill-delivery-row > summary::-webkit-details-marker { display: none; }
.skill-scope-body { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1.4fr); align-items: start; gap: 24px; padding: 16px; background: var(--bg); border-top: 1px solid var(--border); }
.skill-scope-copy, .skill-scope-actions, .skill-delivery-body, .skill-help-body, .skill-baseline-body { display: grid; gap: 12px; min-width: 0; font-size: 12px; }
.skill-delivery-body { padding: 12px 0 18px; }
.skill-workspace-path, .skill-diagnostic { overflow-wrap: anywhere; }
.skill-mode-preview { border: 1px solid var(--border-strong); border-radius: 5px; background: var(--panel); }
.skill-mode-preview > summary { padding: 10px 12px; min-height: 40px; font-weight: 600; }
.skill-mode-preview[open] > summary { color: var(--accent); border-bottom: 1px solid var(--border); }
.skill-preview-body { display: grid; gap: 16px; padding: 14px; }
.skill-preview-workspace { display: grid; gap: 10px; min-width: 0; }
.skill-preview-workspace + .skill-preview-workspace { border-top: 1px solid var(--border); padding-top: 14px; }
.skill-preview-workspace h5 { margin: 0; font-size: 12px; overflow-wrap: anywhere; }
.skill-catalog { list-style: none; display: grid; gap: 12px; margin: 0; padding: 0; }
.skill-catalog li { display: grid; gap: 4px; min-width: 0; }
.skill-catalog code, .skill-reasons code { font-size: 12px; overflow-wrap: anywhere; color: var(--text); }
.skill-catalog li > span, .skill-description { color: var(--text-secondary); }
.skill-description p { margin-top: 6px; overflow-wrap: anywhere; }
.skill-reasons { margin: 0; padding-left: 18px; display: grid; gap: 6px; color: var(--text-secondary); }
.skill-warning { color: var(--danger); font-size: 12px; overflow-wrap: anywhere; }
.skill-baseline, .skill-help { border-bottom: 1px solid var(--border); font-size: 12px; }
.skill-baseline > summary, .skill-help > summary { min-height: 44px; padding: 13px 0; }
.skill-baseline-body, .skill-help-body { padding: 0 0 18px; color: var(--text-secondary); }
.notice, .recovery-notice { border: 1px solid #e4c794; background: var(--warning-soft); color: #775724; padding: 16px 20px; border-radius: 5px; margin-bottom: 18px; font-size: 13px; }
.form-error { border: 1px solid #e1afb3; background: var(--danger-soft); padding: 20px; border-radius: 6px; margin-bottom: 20px; }
.form-error h2 { color: var(--danger); }
.form-error p { margin: 8px 0 16px; }
.draft-recovery { margin: 18px 0; }
.vault-content { display: flex; flex-direction: column; gap: 18px; }
.vault-intro { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.app-stage:has(.vault-frame) main { height: calc(100dvh - 54px); padding: 0; }
.content-frame:has(> .vault-frame) { height: 100%; display: flex; flex-direction: column; }
.content-frame:has(> .vault-frame) > .project-tabs { margin: 0; padding: 0 24px; flex-shrink: 0; }
.vault-frame { display: block; width: 100%; height: 100%; flex: 1; min-height: 0; border: 0; background: #fff; }
.app-sidebar .nav-filter input[data-ui-filter="sidebar"], .mobile-navigation-panel .nav-filter input[data-ui-filter="sidebar"] { background: #28353e; color: #fff; border-color: #41505b; }
.project-row-focus > a { font-size: 13px; }
.project-row-path, .project-row-next, .project-row-state > span:last-child { font-size: 11px; }
.project-row-links { font-size: 11px; }
.mobile-work-tabs { display: none; }
@media (min-width: 1700px) {
  .portfolio-layout { grid-template-columns: minmax(0,1fr) 360px; }
  .project-directory-head, .project-row { grid-template-columns: minmax(160px,.8fr) minmax(200px,1.3fr) 145px 95px; padding-left: 24px; padding-right: 24px; }
}
@media (max-width: 1250px) {
  .app-layout { grid-template-columns: 190px minmax(0,1fr); }
  main { padding: 24px 20px 36px; }
  .context-header { padding: 0 20px; }
  .portfolio-layout { grid-template-columns: minmax(0,1fr) 290px; gap: 14px; }
  .project-directory-head, .project-row { grid-template-columns: minmax(110px,1fr) minmax(120px,1.1fr) 105px; gap: 12px; padding: 15px 16px; }
  .project-row-links { grid-column: 1 / -1; padding: 0; justify-content: flex-start; }
  .project-directory-head > span:last-child { display: none; }
  .project-directory-head { padding-top: 9px; padding-bottom: 9px; }
  .task-search-form { width: min(340px, 48%); }
}
@media (max-width: 1000px) {
  .portfolio-layout { grid-template-columns: minmax(0,1fr); }
  .inbox { order: -1; }
  .inbox-scroll { max-height: 340px; }
  .inbox .task-row { display: grid; grid-template-columns: minmax(0,1fr) 205px; gap: 16px; }
  .inbox .row-actions > .state-dropdown { flex-basis: 100%; }
  .directory-scroll { max-height: none; }
  .project-directory-head, .project-row { grid-template-columns: minmax(130px,.9fr) minmax(160px,1.2fr) 120px 70px; }
  .project-row-links { grid-column: auto; justify-content: flex-end; padding-top: 4px; }
  .project-directory-head > span:last-child { display: block; }
  .task-layout { grid-template-columns: minmax(0,1fr) 280px; gap: 16px; }
}
@media (max-width: 760px) {
  .skill-section-heading { align-items: flex-start; }
  .skill-section-heading .skill-empty { display: none; }
  .skill-delivery-row > summary { grid-template-columns: minmax(0,1fr) auto 16px; gap: 6px 12px; padding: 12px 0; }
  .skill-delivery-name { grid-column: 1; grid-row: 1; }
  .skill-delivery-count { grid-column: 2; grid-row: 1; }
  .skill-delivery-status { grid-column: 1 / 3; grid-row: 2; justify-self: start; text-align: left; }
  .skill-delivery-row > summary .skill-chevron { grid-column: 3; grid-row: 1 / 3; }
  .skill-scope-row > summary { grid-template-columns: minmax(0,1fr) auto 16px; gap: 6px 12px; padding: 12px 0; }
  .skill-scope-name { grid-column: 1; grid-row: 1; }
  .skill-scope-detection { grid-column: 1; grid-row: 2; }
  .skill-scope-current { grid-column: 2; grid-row: 1 / 3; }
  .skill-scope-row > summary .skill-chevron { grid-column: 3; grid-row: 1 / 3; }
  .skill-scope-body { grid-template-columns: minmax(0,1fr); gap: 18px; padding: 12px; }
  .app-layout { display: block; }
  .app-sidebar { display: none; }
  .context-header { height: auto; min-height: 52px; padding: 10px 16px; position: sticky; top: 0; z-index: 10; }
  .context-header .breadcrumbs { display: none; }
  .mobile-navigation { display: block; }
  .mobile-navigation > summary { display: flex; align-items: center; gap: 8px; list-style: none; font-size: 12px; padding: 3px 0; }
  .mobile-navigation > summary::-webkit-details-marker { display: none; }
  .mobile-navigation .brand-mark { width: 27px; height: 27px; font-size: 17px; }
  .mobile-navigation-panel { position: absolute; top: 100%; left: 0; right: 0; background: var(--sidebar); color: var(--sidebar-text); padding: 16px; border-top: 1px solid #47545d; box-shadow: 0 6px 14px #202b3324; max-height: 75dvh; overflow: auto; }
  .mobile-navigation-panel .primary-navigation { grid-template-columns: 1fr 1fr; padding-bottom: 16px; }
  .mobile-navigation-panel .overview-link { min-height: 44px; }
  .mobile-navigation-panel .nav-project-link { min-height: 44px; }
  .header-live-indicator { font-size: 10px; }
  .header-live-indicator .update-link { margin-left: 3px; }
  main { padding: 22px 16px 32px; }
  .mobile-work-tabs { display: flex; gap: 6px; margin: -8px 0 22px; }
  .mobile-work-tabs a { flex: 1; display: grid; place-items: center; min-height: 42px; font-size: 12px; border: 1px solid var(--border); background: #fff; border-radius: 5px; }
  .mobile-work-tabs a[aria-current] { color: var(--accent); border-color: #a8ccc7; background: var(--accent-soft); font-weight: 650; }
  h1 { font-size: 25px; }
  .page-intro { flex-direction: column; align-items: stretch; gap: 16px; margin-bottom: 20px; }
  .task-search-form { width: 100%; }
  .task-search-form input { min-height: 42px; font-size: 13px; }
  .btn, a.btn, .row-disclosure summary { min-height: 44px; padding: 10px 13px; font-size: 12px; }
  .directory-toolbar { padding: 14px; gap: 10px; }
  .directory-search { flex-basis: 100%; }
  .directory-search input { min-height: 42px; font-size: 13px; }
  .filter-buttons { width: 100%; gap: 6px; }
  .filter-buttons button { flex: 1; min-height: 44px; font-size: 12px; }
  .project-directory-head { display: none; }
  .project-row { display: grid; grid-template-columns: minmax(0,1fr) auto; padding: 18px 16px; gap: 12px 16px; }
  .project-row-name { grid-column: 1; }
  .project-name { font-size: 14px; }
  .project-avatar { width: 28px; height: 28px; }
  .project-row-path { font-size: 11px; padding-left: 37px; }
  .project-row-state { grid-column: 2; grid-row: 1; text-align: right; font-size: 11px; }
  .project-row-state > span:last-child { display: none; }
  .project-row-focus { grid-column: 1 / -1; padding-left: 37px; }
  .project-row-focus > a { font-size: 13px; }
  .project-row-next { display: none; }
  .project-row-links { grid-column: 1 / -1; padding: 0 0 0 37px; justify-content: flex-start; font-size: 12px; }
  .project-row-links > a { display: inline-flex; align-items: center; min-height: 44px; padding: 8px 0; }
  .project-menu > summary { min-height: 44px; padding: 12px 14px; }
  .project-menu > div { left: 0; right: auto; }
  .inbox-scroll { max-height: 390px; }
  .inbox .task-row, .task-row { display: flex; flex-direction: column; padding: 18px 16px; gap: 14px; }
  .task-row-aside { width: 100%; }
  .row-actions > .state-dropdown { flex-basis: 100%; margin-bottom: 4px; }
  .inbox .row-actions > .state-dropdown { flex-basis: auto; margin-bottom: 0; }
  .row-actions > .state-dropdown[open], .inbox .row-actions > .state-dropdown[open] { flex-basis: 100%; }
  .inbox .row-disclosure summary, .inbox .row-actions .btn { font-size: 11px; min-height: 44px; padding: 10px; }
  .task-row-title { font-size: 14px; }
  .task-row-meta { font-size: 10px; }
  .task-scopes { display: grid; grid-template-columns: 1fr 1fr; padding: 0 16px; gap: 0 12px; }
  .task-scopes a { font-size: 11px; padding: 14px 0 12px; gap: 5px; }
  .task-scopes a > span { font-size: 9px; }
  .project-tabs { margin: -8px 0 20px; gap: 18px; flex-wrap: wrap; }
  .project-tabs a { padding: 12px 0; font-size: 11px; }
  .project-tabs .return-task { margin-left: 0; }
  .workspace-switcher a { min-height: 38px; padding: 9px 10px; }
  .task-layout { display: flex; flex-direction: column; gap: 16px; }
  .task-main-column, .task-side-column { display: contents; }
  .task-summary { order: 0; }
  .action-card { order: 1; }
  .timeline-panel { order: 2; }
  .facts-card { order: 3; }
  .task-main-column > .timeline-panel, .task-side-column > .facts-card { margin-top: 0; }
  .task-layout > * { width: 100%; }
  .task-title { font-size: 23px; }
  .task-intro-meta { gap: 9px; font-size: 10px; }
  .task-update, .panel-body { padding: 18px; }
  .task-update-summary { font-size: 13px; }
  .timeline-head { display: grid; gap: 4px; }
  .timeline-time { font-size: 10px; }
  .timeline-panel .panel-body { padding: 18px 16px; }
  .task-sections { gap: 18px; font-size: 11px; }
  .panel-head { padding: 16px 18px; }
  .empty-state { padding: 36px 24px; }
  .fact { grid-template-columns: 100px minmax(0,1fr); }
  .vault-frame { min-height: 520px; height: calc(100dvh - 230px); }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition: none !important; scroll-behavior: auto !important; } }

"""

DASHBOARD_JS = r"""
(() => {
  const body = document.body;
  let eventsUrl = body.dataset.eventsUrl;
  let source = null;
  let inFlight = false;
  let queued = false;
  let queuedForce = false;
  let queuedMutationRecovery = false;
  let mutationInFlight = false;
  let mutationRefreshQueued = false;
  let mutationEpoch = 0;
  let mutationRecoveryInFlight = false;
  let mutationRecoveryWarning = false;
  const filters = new Map();
  let projectFilter = 'all';

  const applyFilters = () => {
    ['sidebar', 'projects'].forEach((group) => {
      const query = (filters.get(group) || '').trim().toLocaleLowerCase('ru');
      let visible = 0;
      document.querySelectorAll(`[data-filter-item="${group}"]`).forEach((item) => {
        const matchesText = (item.dataset.filterText || '').toLocaleLowerCase('ru').includes(query);
        const matchesState = group !== 'projects' || projectFilter === 'all'
          || item.dataset.category === projectFilter
          || (projectFilter === 'active' && item.dataset.category === 'review');
        item.hidden = !matchesText || !matchesState;
        if (!item.hidden) visible += 1;
      });
      document.querySelectorAll(`[data-filter-empty="${group}"]`).forEach((empty) => {
        empty.hidden = visible !== 0;
      });
    });
  };

  const bindWorkControls = () => {
    document.querySelectorAll('[data-work-controls]').forEach((control) => { control.hidden = false; });
    document.querySelectorAll('[data-ui-filter]').forEach((field) => {
      const group = field.dataset.uiFilter;
      field.value = filters.get(group) || '';
      field.addEventListener('input', () => {
        filters.set(group, field.value);
        document.querySelectorAll(`[data-ui-filter="${group}"]`).forEach((other) => {
          if (other !== field) other.value = field.value;
        });
        applyFilters();
      });
    });
    document.querySelectorAll('[data-project-filter]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.projectFilter === projectFilter));
      button.addEventListener('click', () => {
        projectFilter = button.dataset.projectFilter;
        document.querySelectorAll('[data-project-filter]').forEach((other) => {
          other.setAttribute('aria-pressed', String(other === button));
        });
        applyFilters();
      });
    });
    document.querySelectorAll('.state-form').forEach((form) => {
      const state = form.querySelector('select[name="state"]');
      const reason = form.querySelector('[data-wait-reason]');
      if (!state || !reason) return;
      const updateReason = () => { reason.hidden = state.value !== 'waiting'; };
      state.addEventListener('change', updateReason);
      updateReason();
    });
    document.querySelectorAll('[data-local-time]').forEach((time) => {
      const date = new Date(time.getAttribute('datetime'));
      if (!Number.isNaN(date.getTime())) {
        time.textContent = date.toLocaleString('ru', {
          day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
        });
        time.title = date.toLocaleString('ru');
      }
    });
    applyFilters();
  };

  const fieldHasChanged = (field) => {
    if (field.dataset.uiFilter) {
      return false;
    }
    if (field.dataset.recoveredDraft === 'true') {
      return true;
    }
    if (field instanceof HTMLInputElement && ['checkbox', 'radio'].includes(field.type)) {
      return field.checked !== field.defaultChecked;
    }
    if (field instanceof HTMLSelectElement) {
      return Array.from(field.options).some((option) => option.selected !== option.defaultSelected);
    }
    return field.value !== field.defaultValue;
  };

  const hasUnsavedInput = () => Array.from(
    document.querySelectorAll('textarea, input:not([type="hidden"]), select')
  ).some(fieldHasChanged);

  const setState = (state, text) => {
    document.querySelectorAll('.live-indicator').forEach((indicator) => {
      indicator.dataset.state = state;
      const copy = indicator.querySelector('.live-copy');
      if (copy) {
        copy.textContent = text;
      }
    });
  };

  const bindRefreshButtons = () => {
    document.querySelectorAll('[data-refresh-now]').forEach((button) => {
      if (button.dataset.refreshBound === 'true') {
        return;
      }
      button.dataset.refreshBound = 'true';
      button.addEventListener('click', () => {
        void refreshPage({ force: true });
      });
    });
  };

  const bindMobileNavigation = () => {
    document.querySelectorAll('.mobile-navigation a').forEach((link) => {
      if (link.dataset.navBound === 'true') {
        return;
      }
      link.dataset.navBound = 'true';
      link.addEventListener('click', () => {
        const disclosure = link.closest('details');
        if (disclosure instanceof HTMLDetailsElement) {
          disclosure.open = false;
        }
      });
    });
  };

  const applyPage = (nextDocument) => {
    const currentLayout = document.querySelector('.app-layout');
    const nextLayout = nextDocument.querySelector('.app-layout');
    if (!(currentLayout instanceof HTMLElement) || !(nextLayout instanceof HTMLElement)) {
      return false;
    }
    const editableFields = (root) => Array.from(
      root.querySelectorAll('textarea, input:not([type="hidden"]), select')
    );
    const formIdentity = (form) => form === null ? '' : JSON.stringify([
      form.getAttribute('method'), form.getAttribute('action') || '',
      ...['action', 'task_id', 'workspace_id', 'project_id', 'facet'].map(
        (name) => form.elements.namedItem(name)?.value || ''
      ),
    ]);
    const fieldIdentity = (field) => JSON.stringify([
      field.id, field.name, field.type, formIdentity(field.form),
      field.dataset.uiFilter || '',
      ['checkbox', 'radio'].includes(field.type) ? field.value : '',
    ]);
    const currentFields = editableFields(currentLayout);
    const nextFields = editableFields(nextLayout);
    const matchingField = (field) => {
      const matches = nextFields.filter((candidate) => fieldIdentity(candidate) === fieldIdentity(field));
      if (field.dataset.uiFilter) {
        const siblings = currentFields.filter((candidate) => fieldIdentity(candidate) === fieldIdentity(field));
        return matches[siblings.indexOf(field)] || null;
      }
      return matches.length === 1 ? matches[0] : null;
    };
    const drafts = currentFields.filter(fieldHasChanged);
    // Capture after the fetch: edits made while the response arrived are still operator-owned.
    const replacements = drafts.map((field) => [field, matchingField(field)]);
    if (replacements.some(([field, target]) => target === null || (
      field instanceof HTMLSelectElement && Array.from(field.options).some(
        (option) => option.selected && !Array.from(target.options).some(
          (candidate) => candidate.value === option.value
        )
      )
    ))) {
      throw new Error('dashboard draft target changed');
    }
    const focused = currentFields.includes(document.activeElement) ? document.activeElement : null;
    const focusTarget = focused === null ? null : matchingField(focused);
    const selection = focused !== null && typeof focused.selectionStart === 'number' ? [
      focused.selectionStart, focused.selectionEnd, focused.selectionDirection,
    ] : null;
    for (const [field, target] of replacements) {
      if (field instanceof HTMLSelectElement) {
        const selected = new Set(Array.from(field.options).filter((option) => option.selected).map(
          (option) => option.value
        ));
        Array.from(target.options).forEach((option) => { option.selected = selected.has(option.value); });
      } else if (field instanceof HTMLInputElement && ['checkbox', 'radio'].includes(field.type)) {
        target.checked = field.checked;
      } else {
        target.value = field.value;
      }
      if (field.dataset.recoveredDraft === 'true') {
        target.dataset.recoveredDraft = 'true';
      }
    }
    const disclosureIdentity = (disclosure) => disclosure.id || JSON.stringify([
      disclosure.className,
      Array.from(disclosure.querySelectorAll('form')).map(formIdentity),
      disclosure.querySelector('summary')?.textContent || '',
    ]);
    const currentDisclosures = Array.from(currentLayout.querySelectorAll('details'));
    nextLayout.querySelectorAll('details').forEach((disclosure) => {
      const matches = currentDisclosures.filter(
        (candidate) => disclosureIdentity(candidate) === disclosureIdentity(disclosure)
      );
      if (matches.length === 1) {
        disclosure.open = matches[0].open;
      }
    });
    const fieldScroll = replacements.map(([field, target]) => [target, field.scrollTop]);
    const sidebar = currentLayout.querySelector('.app-sidebar');
    const nextSidebar = nextLayout.querySelector('.app-sidebar');
    const sidebarScroll = sidebar instanceof HTMLElement ? sidebar.scrollTop : 0;
    const projectNavigation = currentLayout.querySelector('.project-navigation');
    const nextProjectNavigation = nextLayout.querySelector('.project-navigation');
    const projectScroll = projectNavigation instanceof HTMLElement ? projectNavigation.scrollTop : 0;
    const paneScroll = ['.directory-scroll', '.inbox-scroll', '.nav-projects-list'].map((selector) => {
      const current = currentLayout.querySelector(selector);
      const next = nextLayout.querySelector(selector);
      return [next, current instanceof HTMLElement ? current.scrollTop : 0];
    });
    currentLayout.replaceWith(nextLayout);
    if (focusTarget !== null) {
      focusTarget.focus({ preventScroll: true });
      if (selection !== null) {
        focusTarget.setSelectionRange(...selection);
      }
    }
    fieldScroll.forEach(([field, scrollTop]) => { field.scrollTop = scrollTop; });
    if (nextSidebar instanceof HTMLElement) {
      nextSidebar.scrollTop = sidebarScroll;
    }
    if (nextProjectNavigation instanceof HTMLElement) {
      nextProjectNavigation.scrollTop = projectScroll;
    }
    paneScroll.forEach(([pane, scrollTop]) => {
      if (pane instanceof HTMLElement) {
        pane.scrollTop = scrollTop;
      }
    });
    const nextTitle = nextDocument.querySelector('title');
    if (nextTitle && nextTitle.textContent) {
      document.title = nextTitle.textContent;
    }
    if (nextDocument.body && nextDocument.body.dataset.eventsUrl) {
      body.dataset.eventsUrl = nextDocument.body.dataset.eventsUrl;
    }
    bindRefreshButtons();
    bindMobileNavigation();
    bindWorkControls();
    return true;
  };

  const disconnectEvents = () => {
    if (source === null) {
      return;
    }
    source.onerror = null;
    source.close();
    source = null;
  };

  const connectEvents = () => {
    const nextUrl = body.dataset.eventsUrl;
    if (!nextUrl || !('EventSource' in window)) {
      return;
    }
    if (source !== null && eventsUrl === nextUrl) {
      return;
    }
    disconnectEvents();
    eventsUrl = nextUrl;
    source = new EventSource(nextUrl);
    source.addEventListener('ready', () => {
      if (!mutationInFlight && !mutationRecoveryInFlight && !mutationRecoveryWarning) {
        setState('live', 'Онлайн');
      }
    });
    source.addEventListener('refresh', () => {
      if (mutationInFlight) {
        mutationRefreshQueued = true;
        return;
      }
      if (mutationRecoveryInFlight || queuedMutationRecovery) {
        return;
      }
      void refreshPage({ force: false });
    });
    source.onerror = () => {
      if (!mutationInFlight && !mutationRecoveryInFlight && !mutationRecoveryWarning) {
        setState('reconnecting', 'Переподключение');
      }
    };
  };

  const refreshPage = async (options) => {
    if (mutationInFlight) {
      mutationRefreshQueued = true;
      return;
    }
    if (mutationRecoveryInFlight) {
      return;
    }
    queued = true;
    queuedForce = queuedForce || Boolean(options.force);
    queuedMutationRecovery = queuedMutationRecovery || Boolean(options.mutationRecovery);
    if (inFlight) {
      return;
    }
    inFlight = true;
    let mutationRecovery = false;
    try {
      while (queued) {
        queued = false;
        const force = queuedForce;
        queuedForce = false;
        mutationRecovery = queuedMutationRecovery;
        queuedMutationRecovery = false;
        mutationRecoveryInFlight = mutationRecovery;
        if (!force && hasUnsavedInput()) {
          setState('update', 'Есть обновление');
          break;
        }
        setState('update', 'Обновление');
        const expectedMutationEpoch = mutationEpoch;
        const response = await fetch(`${window.location.pathname}${window.location.search}`, {
          cache: 'no-store',
          credentials: 'same-origin',
          headers: { Accept: 'text/html' },
        });
        if (!response.ok) {
          throw new Error('dashboard refresh failed');
        }
        const html = await response.text();
        if (expectedMutationEpoch !== mutationEpoch) {
          break;
        }
        if (!force && hasUnsavedInput()) {
          setState('update', 'Есть обновление');
          break;
        }
        const nextDocument = new DOMParser().parseFromString(html, 'text/html');
        const scrollX = window.scrollX;
        const scrollY = window.scrollY;
        if (!applyPage(nextDocument)) {
          throw new Error('dashboard refresh parse failed');
        }
        window.scrollTo(scrollX, scrollY);
        mutationRecoveryWarning = mutationRecovery;
        connectEvents();
        setState(
          mutationRecovery ? 'update' : 'live',
          mutationRecovery ? 'Проверьте сохранение' : 'Онлайн'
        );
        mutationRecoveryInFlight = false;
      }
    } catch (error) {
      if (!mutationInFlight) {
        if (error instanceof Error && error.message === 'dashboard draft target changed') {
          mutationRecoveryWarning = mutationRecovery;
          setState('update', 'Форма изменилась · черновик сохранён');
        } else if (mutationRecovery) {
          mutationRecoveryWarning = true;
          setState('update', 'Не удалось подтвердить сохранение');
        } else {
          setState('update', 'Есть обновление');
        }
      }
    } finally {
      mutationRecoveryInFlight = false;
      inFlight = false;
      if (queued) {
        void refreshPage({
          force: queuedForce,
          mutationRecovery: queuedMutationRecovery,
        });
      }
    }
  };

  const submittedControlValue = (field) => {
    if (field instanceof HTMLInputElement && ['checkbox', 'radio'].includes(field.type)) {
      return JSON.stringify([field.checked, field.value]);
    }
    if (field instanceof HTMLSelectElement) {
      return JSON.stringify(Array.from(field.options).filter((option) => option.selected).map(
        (option) => option.value
      ));
    }
    return field.value;
  };

  const markSubmittedControlsClean = (submitted) => {
    const previous = [];
    submitted.forEach(([field, value]) => {
      if (submittedControlValue(field) !== value) {
        return;
      }
      if (field instanceof HTMLInputElement && ['checkbox', 'radio'].includes(field.type)) {
        previous.push([field, field.defaultChecked, null, field.dataset.recoveredDraft]);
        field.defaultChecked = field.checked;
      } else if (field instanceof HTMLSelectElement) {
        previous.push([
          field,
          null,
          Array.from(field.options).map((option) => option.defaultSelected),
          field.dataset.recoveredDraft,
        ]);
        Array.from(field.options).forEach((option) => { option.defaultSelected = option.selected; });
      } else {
        previous.push([field, field.defaultValue, null, field.dataset.recoveredDraft]);
        field.defaultValue = field.value;
      }
      delete field.dataset.recoveredDraft;
    });
    return () => {
      previous.forEach(([field, defaultValue, defaultOptions, recoveredDraft]) => {
        if (field instanceof HTMLInputElement && ['checkbox', 'radio'].includes(field.type)) {
          field.defaultChecked = defaultValue;
        } else if (field instanceof HTMLSelectElement) {
          Array.from(field.options).forEach((option, index) => {
            option.defaultSelected = defaultOptions[index];
          });
        } else {
          field.defaultValue = defaultValue;
        }
        if (recoveredDraft === undefined) {
          delete field.dataset.recoveredDraft;
        } else {
          field.dataset.recoveredDraft = recoveredDraft;
        }
      });
    };
  };

  const submitMutation = async (form) => {
    if (mutationInFlight || mutationRecoveryInFlight || queuedMutationRecovery) {
      return;
    }
    mutationInFlight = true;
    mutationRecoveryWarning = false;
    mutationRefreshQueued = false;
    mutationEpoch += 1;
    queued = false;
    queuedForce = false;
    queuedMutationRecovery = false;
    form.setAttribute('aria-busy', 'true');
    setState('saving', 'Сохранение');
    const fields = Array.from(form.querySelectorAll('textarea, input, select'));
    const submitted = fields.map((field) => [field, submittedControlValue(field)]);
    const target = form.getAttribute('action') || `${window.location.pathname}${window.location.search}`;
    let restoreSubmittedControls = null;
    let applied = false;
    let mutationFailed = false;
    try {
      const response = await fetch(target, {
        method: 'POST',
        body: new URLSearchParams(new FormData(form)),
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Accept: 'text/html' },
      });
      const contentType = response.headers.get('Content-Type') || '';
      if (!contentType.toLowerCase().startsWith('text/html')) {
        throw new Error('dashboard mutation response is not HTML');
      }
      const html = await response.text();
      const nextDocument = new DOMParser().parseFromString(html, 'text/html');
      if (!(nextDocument.querySelector('.app-layout') instanceof HTMLElement)) {
        throw new Error('dashboard mutation parse failed');
      }
      if (response.ok) {
        restoreSubmittedControls = markSubmittedControlsClean(submitted);
      }
      const scrollX = window.scrollX;
      const scrollY = window.scrollY;
      if (!applyPage(nextDocument)) {
        throw new Error('dashboard mutation parse failed');
      }
      applied = true;
      if (response.url) {
        const responseUrl = new URL(response.url, window.location.href);
        const nextLocation = `${responseUrl.pathname}${responseUrl.search}`;
        const currentLocation = `${window.location.pathname}${window.location.search}`;
        if (responseUrl.origin === window.location.origin && nextLocation !== currentLocation) {
          window.history.replaceState(null, '', nextLocation);
        }
      }
      window.scrollTo(scrollX, scrollY);
      connectEvents();
      setState(response.ok ? 'live' : 'update', response.ok ? 'Сохранено' : 'Проверьте форму');
      mutationRefreshQueued = false;
    } catch (_error) {
      mutationFailed = true;
      if (!applied && restoreSubmittedControls !== null) {
        restoreSubmittedControls();
      }
      form.removeAttribute('aria-busy');
      mutationRefreshQueued = false;
      setState('update', 'Не удалось сохранить');
    } finally {
      mutationInFlight = false;
      if (mutationFailed) {
        void refreshPage({ force: true, mutationRecovery: true });
      } else if (mutationRefreshQueued) {
        mutationRefreshQueued = false;
        void refreshPage({ force: false });
      }
    }
  };

  bindRefreshButtons();
  bindMobileNavigation();
  bindWorkControls();

  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
      return;
    }
    const search = document.querySelector('main input[type="search"]');
    if (search instanceof HTMLInputElement) {
      event.preventDefault();
      search.focus();
    }
  });

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') {
      return;
    }
    event.preventDefault();
    void submitMutation(form);
  });

  connectEvents();
  window.addEventListener('beforeunload', (event) => {
    const dirtyForm = Array.from(
      document.querySelectorAll('textarea, input:not([type="hidden"]), select')
    ).some((field) => field.form?.method.toLowerCase() === 'post' && fieldHasChanged(field));
    if (mutationInFlight || dirtyForm) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  window.addEventListener('pagehide', () => disconnectEvents(), { once: true });
})();
""".strip()
