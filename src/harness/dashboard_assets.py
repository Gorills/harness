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
.git-entry { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; color: var(--accent); font-size: 12px; overflow-wrap: anywhere; }
.project-row-links .project-git-button { display: inline-flex; align-items: center; justify-content: center; min-width: 48px; min-height: 34px; padding: 6px 10px; border: 1px solid var(--border-strong); border-radius: 4px; font-size: 13px; font-weight: 600; color: var(--accent); background: var(--panel); }
.git-sync, .git-console { padding: 16px 20px; margin-bottom: 20px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.git-sync h2 { font-size: 15px; margin-bottom: 8px; }
.git-tracking { font-size: 13px; margin-bottom: 12px; overflow-wrap: anywhere; }
.git-sync-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 8px 0 12px; }
.git-sync select { width: auto; max-width: 100%; min-height: 34px; }
.git-publish summary { font-size: 13px; font-weight: 600; color: var(--accent); }
.git-publish form { display: grid; gap: 10px; padding-top: 12px; }
.git-publish-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 12px; }
.git-publish-fields label { min-width: 0; }
.git-publish-fields input, .git-publish-fields select { display: block; width: 100%; margin-top: 6px; }
.git-publish button { justify-self: start; }
.git-console summary { font-weight: 600; font-size: 13px; }
.git-console p { margin: 10px 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.git-console pre { max-height: 220px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; border-radius: 4px; background: var(--sidebar-bg, #202b33); color: #f4f7f8; font-size: 12px; }
.project-git-links { display: flex; flex-wrap: wrap; gap: 12px; margin: -8px 0 20px; }
.task-branch-context { margin: -8px 0 20px; }
.task-branch-prompt, .git-status-bar { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 20px; border: 1px solid var(--border); background: var(--panel); border-radius: var(--radius); margin-bottom: 20px; }
.task-branch-prompt { background: var(--accent-soft); border-color: #b9d9d3; }
.task-branch-prompt p { margin-top: 4px; color: var(--text-secondary); }
.task-branch-prompt .button { color: var(--accent); font-weight: 600; }
.git-status-bar > div { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; min-width: 0; }
.git-status-bar > div > span, .git-status-bar small, .git-status-bar > p { color: var(--text-secondary); font-size: 12px; }
.git-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; align-items: start; }
.git-columns > .panel { margin: 0; min-width: 0; }
.git-workbench form { margin: 0; }
.git-workbench label { display: block; font-weight: 550; font-size: 12px; margin-bottom: 6px; }
.git-workbench button { border: 1px solid var(--border-strong); border-radius: 4px; background: var(--panel); padding: 7px 11px; font-size: 12px; min-height: 34px; }
.git-workbench button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.git-workbench button:disabled { cursor: not-allowed; }
.git-workbench button.primary { background: var(--accent); color: white; border-color: var(--accent); }
.git-search > div { display: flex; gap: 8px; }
.git-search + p { margin: 12px 0; }
.git-branch-list, .git-file-list { list-style: none; margin: 0; padding: 0; }
.git-branch-list { max-height: 380px; overflow-y: auto; }
.git-branch-row, .git-file-row { display: flex; gap: 12px; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); }
.git-branch-name, .git-file-row > div { display: grid; gap: 3px; min-width: 0; }
.git-branch-name span, .git-file-row span { font-size: 11px; color: var(--text-secondary); }
.git-file-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.git-branch-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; flex-shrink: 0; }
.git-merge-choice { position: relative; }
.git-merge-choice > summary { font-size: 12px; color: var(--text-secondary); padding: 8px 4px; }
.git-merge-choice > div { position: absolute; right: 0; width: 290px; max-width: 80vw; padding: 16px; background: var(--panel); border: 1px solid var(--border-strong); border-radius: var(--radius); box-shadow: 0 8px 24px #202b3320; z-index: 5; }
.git-merge-choice p { margin-bottom: 12px; overflow-wrap: anywhere; }
.git-create { margin: 12px 0; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
.git-create summary { padding: 8px 0; color: var(--accent); font-weight: 550; }
.git-create form, .git-commit { display: grid; gap: 10px; margin-top: 12px !important; }
.git-commit { padding-top: 12px; }
.git-commit button { justify-self: start; }
.git-warning { background: var(--warning-soft); border: 1px solid #e4c794; border-radius: var(--radius); padding: 16px 20px; margin-bottom: 18px; overflow-wrap: anywhere; }
.git-warning p { margin: 8px 0 12px; white-space: pre-wrap; }
.git-switch-hint { color: var(--text-secondary); margin: -8px 0 20px; font-size: 12px; }
@media (max-width: 1100px) {
  .git-columns { grid-template-columns: minmax(0, 1fr); }
  .git-workbench.has-changes .git-changes { order: -1; }
}
@media (max-width: 600px) {
  .git-publish-fields { grid-template-columns: minmax(0, 1fr); }
  .git-status-bar, .task-branch-prompt { align-items: flex-start; flex-direction: column; padding: 14px; }
  .git-branch-row { align-items: flex-start; flex-wrap: wrap; }
  .git-branch-actions { width: 100%; justify-content: flex-start; }
  .git-merge-choice > div { right: auto; left: 0; }
  .git-file-row { flex-wrap: wrap; }
}
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
  .header-live-indicator { font-size: 10px; flex-shrink: 1; min-width: 0; }
  .header-live-indicator .live-copy { min-width: 0; overflow-wrap: anywhere; }
  .header-live-indicator .update-link { margin-left: 3px; flex-shrink: 0; white-space: nowrap; }
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
  let queuedExplicitRead = false;
  let queuedMutationRecovery = false;
  let mutationInFlight = false;
  let mutationRefreshQueued = false;
  let mutationEpoch = 0;
  let mutationRecoveryInFlight = false;
  let mutationRecoveryWarning = false;
  let refreshFailed = false;
  let backgroundRefreshPending = false;
  let composing = false;
  let pointerDown = false;
  let interactionCheckScheduled = false;
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
          year: time.hasAttribute('data-time-year') ? 'numeric' : undefined,
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

  // Restoring a value or calling focus() cannot restore an open native picker,
  // composition session, or a selection spanning text nodes. Keep their DOM alive.
  const hasActiveInteraction = () => {
    if (composing || pointerDown) return true;
    const layout = document.querySelector('.app-layout');
    const active = document.activeElement;
    if (layout?.contains(active) && active.closest(
      'input, textarea, select, button, a[href], summary, [contenteditable], [tabindex]'
    )) return true;
    const selection = window.getSelection();
    return selection !== null && !selection.isCollapsed && (
      layout?.contains(selection.anchorNode) || layout?.contains(selection.focusNode)
    );
  };

  const resumeBackgroundRefresh = () => {
    if (!backgroundRefreshPending || interactionCheckScheduled) return;
    interactionCheckScheduled = true;
    // focusout fires before the next control receives focus; check after that transfer.
    setTimeout(() => {
      interactionCheckScheduled = false;
      if (backgroundRefreshPending && !hasUnsavedInput() && !hasActiveInteraction()) {
        void refreshPage({ force: false });
      }
    }, 0);
  };

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

  const showServerView = ({ initial = false } = {}) => {
    const workspace = document.querySelector('[data-server-workspace]');
    if (!(workspace instanceof HTMLElement)) {
      return;
    }
    let target = null;
    try {
      target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    } catch (_error) {
      // An invalid fragment does not prevent reading the overview.
    }
    const views = Array.from(workspace.querySelectorAll('[data-server-view]'));
    const selectedView = target?.closest('[data-server-view]');
    const key = initial && workspace.dataset.serverError === 'true'
      ? workspace.dataset.serverInitialView
      : selectedView?.dataset.serverView || workspace.dataset.serverInitialView || 'overview';
    views.forEach((view) => { view.hidden = view.dataset.serverView !== key; });
    workspace.querySelectorAll('[data-server-tab]').forEach((link) => {
      if (link.dataset.serverTab === key) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    });
    // Exact app/profile links and add/edit links reveal the requested disclosure.
    if (target instanceof HTMLDetailsElement) {
      target.open = true;
      let parent = target.parentElement?.closest('details');
      while (parent instanceof HTMLDetailsElement) {
        parent.open = true;
        parent = parent.parentElement?.closest('details');
      }
    }
  };

  const bindServerControls = () => {
    showServerView({ initial: true });
    document.querySelectorAll('[data-server-navigation]').forEach((link) => {
      if (link.dataset.serverBound === 'true') return;
      link.dataset.serverBound = 'true';
      link.addEventListener('click', (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        navigateServer(link.href);
      });
    });
    document.querySelectorAll('[data-server-cancel]').forEach((button) => {
      if (button.dataset.cancelBound === 'true') return;
      button.dataset.cancelBound = 'true';
      button.addEventListener('click', (event) => {
        event.preventDefault();
        const form = button.closest('form');
        if (!form || mutationInFlight || inFlight) return;
        form.querySelectorAll('textarea, input:not([type="hidden"]), select').forEach((field) => {
          const value = field.dataset.serverDefault ?? field.defaultValue ?? '';
          field.value = value;
          if (field instanceof HTMLSelectElement) {
            Array.from(field.options).forEach((option) => { option.defaultSelected = option.value === value; });
          } else {
            field.defaultValue = value;
          }
          delete field.dataset.recoveredDraft;
        });
        const revision = document.querySelector('[data-server-workspace]')?.dataset.serverRevision;
        const field = form.elements.namedItem('expected_revision');
        if (field && revision) field.value = field.defaultValue = revision;
        const disclosure = form.closest('details');
        if (disclosure) {
          disclosure.open = false;
          disclosure.querySelector('summary')?.focus();
        }
        setState('live', 'Изменения этой формы отменены');
        if (form.elements.namedItem('object_key')?.value === '@new') {
          const target = new URL(window.location.href);
          target.searchParams.delete('object_id');
          navigateServer(target.href);
        }
      });
    });
    document.querySelectorAll('[data-server-tab]:not([data-server-navigation]), [data-server-open]').forEach((link) => {
      if (link.dataset.serverBound === 'true') {
        return;
      }
      link.dataset.serverBound = 'true';
      link.addEventListener('click', (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
          return;
        }
        event.preventDefault();
        const fragment = link.getAttribute('href');
        window.history.pushState(null, '', fragment);
        showServerView();
        if (link.dataset.serverOpen) {
          const disclosure = document.getElementById(link.dataset.serverOpen);
          if (disclosure instanceof HTMLDetailsElement) {
            disclosure.open = true;
            disclosure.scrollIntoView({ block: 'start' });
            disclosure.querySelector('summary')?.focus({ preventScroll: true });
          }
        }
      });
    });
  };

  const applyPage = (nextDocument, { preserveDraftRevisions = false, confirmedForm = null, syncUrl = true } = {}) => {
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
      ...['action', 'task_id', 'workspace_id', 'project_id', 'facet', 'server_id', 'site_key', 'profile_key', 'map_kind', 'object_key', 'link_key', 'from_kind', 'from_id'].map(
        (name) => name === 'server_id' && form.elements.namedItem('action')?.value === 'server_save'
          && form.elements.namedItem('expected_revision')?.value === '0'
          ? '@new-server' : form.elements.namedItem(name)?.value || ''
      ),
    ]);
    const fieldIdentity = (field) => JSON.stringify([
      field.id, field.name, field.type, formIdentity(field.form),
      field.dataset.uiFilter || '',
      ['checkbox', 'radio'].includes(field.type) ? field.value : '',
    ]);
    const currentFields = editableFields(currentLayout);
    const nextFields = editableFields(nextLayout);
    // Server error pages also retain fields for native submissions. With JS, use the
    // current edit history so an untouched field can adopt a concurrent stored update.
    nextFields.forEach((target) => {
      const matches = currentFields.filter((field) => fieldIdentity(field) === fieldIdentity(target));
      if (matches.length === 1 && !fieldHasChanged(matches[0])) {
        delete target.dataset.recoveredDraft;
      }
    });
    const matchingField = (field) => {
      const matches = nextFields.filter((candidate) => fieldIdentity(candidate) === fieldIdentity(field));
      if (field.dataset.uiFilter) {
        const siblings = currentFields.filter((candidate) => fieldIdentity(candidate) === fieldIdentity(field));
        return matches[siblings.indexOf(field)] || null;
      }
      return matches.length === 1 ? matches[0] : null;
    };
    const drafts = currentFields.filter(fieldHasChanged);
    // A neighboring POST response is not permission to rebase an unsent draft.
    // Keep the revision on which each dirty form was edited until an explicit read.
    if (preserveDraftRevisions) {
      const dirtyForms = new Set(drafts.map((field) => field.form).filter(
        (form) => form !== null && form.method.toLowerCase() === 'post' && form !== confirmedForm
      ));
      const nextForms = Array.from(nextLayout.querySelectorAll('form'));
      dirtyForms.forEach((form) => {
        const matches = nextForms.filter((candidate) => formIdentity(candidate) === formIdentity(form));
        const revision = form.elements.namedItem('expected_revision');
        const target = matches.length === 1 ? matches[0].elements.namedItem('expected_revision') : null;
        if (revision !== null && target !== null) {
          target.value = target.defaultValue = revision.value;
        }
      });
    }
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
      // A validation response can echo this value as its default; it is still an unsaved draft.
      target.dataset.recoveredDraft = 'true';
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
    const pageUrl = nextLayout.querySelector('[data-server-page-url]')?.dataset.serverPageUrl;
    if (pageUrl && syncUrl) {
      const canonical = new URL(pageUrl, window.location.href);
      if (canonical.origin === window.location.origin) {
        window.history.replaceState(window.history.state ?? null, '', `${canonical.pathname}${canonical.search}${window.location.hash || ''}`);
      }
    }
    bindRefreshButtons();
    bindMobileNavigation();
    bindWorkControls();
    bindServerControls();
    backgroundRefreshPending = false;
    return true;
  };
  const serverPositions = new Map();

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
      if (!inFlight && !mutationInFlight && !mutationRecoveryInFlight && !mutationRecoveryWarning && !refreshFailed && !backgroundRefreshPending) {
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
      if (!inFlight && !mutationInFlight && !mutationRecoveryInFlight && !mutationRecoveryWarning && !refreshFailed && !backgroundRefreshPending) {
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
    queuedExplicitRead = queuedExplicitRead || Boolean(options.force && !options.mutationRecovery);
    queuedMutationRecovery = queuedMutationRecovery || Boolean(options.mutationRecovery)
      || Boolean(options.force && mutationRecoveryWarning);
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
        const explicitRead = queuedExplicitRead;
        queuedExplicitRead = false;
        mutationRecovery = queuedMutationRecovery;
        queuedMutationRecovery = false;
        mutationRecoveryInFlight = mutationRecovery;
        if (!force && (hasUnsavedInput() || hasActiveInteraction())) {
          backgroundRefreshPending = true;
          if (refreshFailed) {
            setState('update', 'Не удалось обновить · черновик сохранён');
          } else if (!mutationRecoveryWarning) {
            setState('update', 'Есть обновление');
          }
          break;
        }
        backgroundRefreshPending = false;
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
        if (!force && (hasUnsavedInput() || hasActiveInteraction())) {
          backgroundRefreshPending = true;
          setState('update', 'Есть обновление');
          break;
        }
        const nextDocument = new DOMParser().parseFromString(html, 'text/html');
        const scrollX = window.scrollX;
        const scrollY = window.scrollY;
        if (!applyPage(nextDocument, { preserveDraftRevisions: mutationRecovery && !explicitRead })) {
          throw new Error('dashboard refresh parse failed');
        }
        window.scrollTo(scrollX, scrollY);
        refreshFailed = false;
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
          refreshFailed = true;
          setState('update', hasUnsavedInput()
            ? 'Не удалось обновить · черновик сохранён' : 'Не удалось обновить');
        }
      }
    } finally {
      mutationRecoveryInFlight = false;
      inFlight = false;
      if (queued) {
        void refreshPage({
          force: queuedForce,
          mutationRecovery: queuedMutationRecovery && !queuedExplicitRead,
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

  const navigateServer = async (href, { push = true } = {}) => {
    if (inFlight || mutationInFlight || mutationRecoveryInFlight || queuedMutationRecovery) {
      setState('update', 'Дождитесь завершения текущего запроса.');
      return;
    }
    const target = new URL(href, window.location.href);
    if (target.origin !== window.location.origin) return;
    inFlight = true;
    const epoch = mutationEpoch;
    setState('update', 'Загрузка списка…');
    try {
      const response = await fetch(`${target.pathname}${target.search}`, {
        cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'text/html' },
      });
      if (!response.ok) throw new Error('server navigation failed');
      const nextDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (epoch !== mutationEpoch) throw new Error('server changed');
      const currentUrl = window.location.href;
      const currentPosition = [window.scrollX, window.scrollY];
      if (!applyPage(nextDocument, { preserveDraftRevisions: true, syncUrl: false })) throw new Error('server navigation failed');
      if (push) serverPositions.set(currentUrl, currentPosition);
      if (push) window.history.pushState(null, '', `${target.pathname}${target.search}${target.hash}`);
      showServerView();
      const activeView = Array.from(document.querySelectorAll('[data-server-view]')).find((view) => !view.hidden);
      const detail = activeView?.querySelector('.server-map-detail, .server-edit-disclosure[open]');
      const heading = detail || activeView?.querySelector('h2');
      if (heading) {
        heading.setAttribute('tabindex', '-1');
        heading.focus({ preventScroll: true });
        const position = serverPositions.get(target.href);
        if (position) window.scrollTo(...position);
        else heading.scrollIntoView({ block: 'start' });
      }
      refreshFailed = false;
      setState('live', 'Список загружен полностью');
    } catch (error) {
      setState('error', error instanceof Error && error.message === 'dashboard draft target changed'
        ? 'Сохраните черновик перед переходом: его форма исчезнет из раздела.'
        : 'Список не обновлён. Повторите переход; показанные сведения сохранены.');
    } finally {
      inFlight = false;
    }
  };

  const submitServerSearch = async (form) => {
    const notice = form.querySelector('[data-server-filter-status]');
    const report = (message) => { if (notice) notice.textContent = message; };
    if (inFlight || mutationInFlight || mutationRecoveryInFlight || queuedMutationRecovery) {
      report('Дождитесь завершения текущего запроса.');
      return;
    }
    inFlight = true;
    form.setAttribute('aria-busy', 'true');
    const buttons = Array.from(form.querySelectorAll('button'));
    buttons.forEach((button) => { button.disabled = true; });
    report('Поиск…');
    const fields = Array.from(form.querySelectorAll('input, select'));
    const submitted = fields.map((field) => [field, submittedControlValue(field)]);
    const target = new URL(form.getAttribute('action') || window.location.pathname, window.location.href);
    target.search = new URLSearchParams(new FormData(form)).toString();
    const expectedMutationEpoch = mutationEpoch;
    let restoreControls = null;
    try {
      const response = await fetch(`${target.pathname}${target.search}`, {
        cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'text/html' },
      });
      if (!response.ok) throw new Error('server search failed');
      const html = await response.text();
      if (expectedMutationEpoch !== mutationEpoch) {
        report('Повторите поиск после сохранения.');
        return;
      }
      const nextDocument = new DOMParser().parseFromString(html, 'text/html');
      restoreControls = markSubmittedControlsClean(submitted);
      const scrollX = window.scrollX;
      const scrollY = window.scrollY;
      if (!applyPage(nextDocument, { preserveDraftRevisions: true })) {
        throw new Error('server search parse failed');
      }
      // The canonical server response may reconcile an empty last page.
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${target.hash || window.location.hash || ''}`);
      showServerView();
      window.scrollTo(scrollX, scrollY);
      refreshFailed = false;
      setState('live', 'Результаты обновлены');
    } catch (error) {
      if (restoreControls) restoreControls();
      report(error instanceof Error && error.message === 'dashboard draft target changed'
        ? 'Сохраните черновик перед поиском: его форма исчезнет из результатов.'
        : 'Не удалось выполнить поиск. Повторите запрос.');
    } finally {
      form.removeAttribute('aria-busy');
      buttons.forEach((button) => { button.disabled = false; });
      inFlight = false;
      if (queued) void refreshPage({ force: queuedForce });
    }
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
    queuedExplicitRead = false;
    queuedMutationRecovery = false;
    form.setAttribute('aria-busy', 'true');
    setState('saving', form.querySelector('[name="action"]')?.value?.startsWith('git_') ? 'Выполняется Git…' : 'Сохранение');
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
        const action = form.elements.namedItem('action')?.value;
        const keyName = action === 'server_site_delete' ? 'site_key' : action === 'server_profile_delete' ? 'profile_key' : action === 'server_map_delete' ? 'object_key' : action === 'server_map_unlink' ? 'link_key' : null;
        const deletedKey = keyName === null ? null : form.elements.namedItem(keyName)?.value;
        const serverId = form.elements.namedItem('server_id')?.value;
        const removedFields = keyName === null ? [] : Array.from(document.querySelectorAll('form')).filter(
          (candidate) => candidate.elements.namedItem('server_id')?.value === serverId &&
            candidate.elements.namedItem(keyName)?.value === deletedKey &&
            candidate.elements.namedItem('action')?.value === (keyName === 'site_key' ? 'server_site_save' : keyName === 'object_key' ? 'server_map_save' : keyName === 'link_key' ? 'server_map_link' : 'server_profile_save') &&
            (keyName !== 'object_key' || candidate.elements.namedItem('map_kind')?.value === form.elements.namedItem('map_kind')?.value)
        ).flatMap((candidate) => Array.from(candidate.querySelectorAll('textarea, input, select')));
        // A confirmed delete discards only that entity's own editor; failures retain every draft.
        restoreSubmittedControls = markSubmittedControlsClean([
          ...submitted, ...removedFields.map((field) => [field, submittedControlValue(field)]),
        ]);
      }
      const scrollX = window.scrollX;
      const scrollY = window.scrollY;
      // Applying the page adopts its layout and removes the marker from nextDocument.
      const hasCanonicalPageUrl = nextDocument.querySelector('[data-server-page-url]') !== null;
      if (!applyPage(nextDocument, {
        preserveDraftRevisions: true, confirmedForm: response.ok ? form : null,
      })) {
        throw new Error('dashboard mutation parse failed');
      }
      applied = true;
      if (response.url && !hasCanonicalPageUrl) {
        const responseUrl = new URL(response.url, window.location.href);
        const nextLocation = `${responseUrl.pathname}${responseUrl.search}`;
        const currentLocation = `${window.location.pathname}${window.location.search}`;
        if (responseUrl.origin === window.location.origin && nextLocation !== currentLocation) {
          window.history.replaceState(null, '', nextLocation);
        }
      }
      window.scrollTo(scrollX, scrollY);
      refreshFailed = false;
      connectEvents();
      if (!response.ok) {
        const alert = document.querySelector('[data-server-error-summary]');
        if (alert) {
          alert.focus({ preventScroll: true });
          alert.scrollIntoView({ block: 'center' });
        }
      }
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

  bindServerControls();
  document.addEventListener('focusout', resumeBackgroundRefresh);
  document.addEventListener('selectionchange', resumeBackgroundRefresh);
  document.addEventListener('input', resumeBackgroundRefresh);
  document.addEventListener('change', resumeBackgroundRefresh);
  document.addEventListener('compositionstart', () => { composing = true; });
  document.addEventListener('compositionend', () => {
    composing = false;
    resumeBackgroundRefresh();
  });
  document.addEventListener('pointerdown', () => { pointerDown = true; });
  const releasePointer = () => {
    pointerDown = false;
    resumeBackgroundRefresh();
  };
  window.addEventListener('pointerup', releasePointer);
  window.addEventListener('pointercancel', releasePointer);
  window.addEventListener('blur', releasePointer);
  window.addEventListener('hashchange', () => showServerView());
  window.addEventListener('popstate', () => {
    if (window.location.pathname.startsWith('/servers/')) navigateServer(window.location.href, { push: false });
    else showServerView();
  });

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
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    if (form.method.toLowerCase() === 'get' && form.hasAttribute('data-server-search')) {
      event.preventDefault();
      void submitServerSearch(form);
      return;
    }
    if (form.method.toLowerCase() !== 'post') return;
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

DASHBOARD_CSS += """
.server-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:24px; }
.server-heading > div { min-width:0; }
.server-heading h1 { margin:9px 0 7px; overflow-wrap:anywhere; }
.server-heading p { color:var(--text-secondary); font-size:13px; }
.server-heading-meta { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
.server-badge { display:inline-flex; align-items:center; padding:3px 8px; border-radius:4px; background:var(--accent-soft); color:var(--accent-hover); font-size:11px; font-weight:550; overflow-wrap:anywhere; }
.server-badge.is-muted { color:var(--text-secondary); background:#edf1f3; }
.server-tabs, .server-list-tabs { display:flex; gap:24px; border-bottom:1px solid var(--border); margin-bottom:22px; overflow-x:auto; }
.server-tabs a, .server-list-tabs a { display:flex; align-items:center; gap:7px; flex-shrink:0; min-height:44px; border-bottom:2px solid transparent; color:var(--text-secondary); font-size:13px; font-weight:550; }
.server-tabs a[aria-current], .server-list-tabs a[aria-current] { border-bottom-color:var(--accent); color:var(--accent-hover); }
.server-tabs a span { font-size:11px; background:#e9eef0; border-radius:4px; padding:0 6px; color:var(--text-secondary); }
.server-view { margin-bottom:24px; scroll-margin-top:80px; }
.server-section-heading { display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:20px; }
.server-section-heading h2 { font-size:18px; }
.server-section-heading p { color:var(--text-secondary); font-size:12px; margin-top:5px; }
.server-panel { padding:20px; background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); min-width:0; }
.server-panel-title { margin-bottom:16px; }
.server-overview-grid { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(0,1fr); gap:18px; align-items:start; }
.server-facts { margin:0; }
.server-facts > div { display:grid; grid-template-columns:145px minmax(0,1fr); gap:16px; padding:8px 0; }
.server-facts dt { color:var(--text-secondary); font-size:12px; font-weight:400; }
.server-facts dd { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; font-size:13px; }
.server-facts .server-fact-empty { color:var(--text-muted); }
.server-rules { padding-left:18px; margin:0; font-size:13px; }
.server-rules li + li { margin-top:12px; }
.server-event small { display:block; color:var(--text-muted); font-size:11px; margin-top:5px; }
.server-passport-brief, .server-map-summary { margin-bottom:24px; }
.server-map-summary > .server-section-heading { margin-bottom:14px; }
.server-map-summary-grid { display:grid; grid-template-columns:minmax(0,1.65fr) minmax(300px,1fr); gap:18px; align-items:start; }
.server-summary-sidebar { display:grid; gap:18px; min-width:0; }
.server-summary-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
.server-summary-heading h3 { font-size:14px; }
.server-summary-heading a, .server-summary-list-note a { color:var(--accent-hover); font-size:12px; }
.server-summary-count { color:var(--text-secondary); background:var(--bg); border-radius:4px; padding:2px 7px; font-size:12px; font-variant-numeric:tabular-nums; }
.server-summary-attention:has(.server-summary-issues) .server-summary-count { background:var(--warning-soft); color:var(--warning); }
.server-inventory-counts { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:0 20px; border-bottom:1px solid var(--border); padding-bottom:16px; margin-bottom:18px; }
.server-inventory-counts a { display:flex; align-items:center; justify-content:space-between; gap:8px; min-width:0; padding:9px 0; font-size:12px; color:var(--text-secondary); }
.server-inventory-counts a:hover { color:var(--accent-hover); }
.server-inventory-counts strong { font-size:16px; color:var(--text); font-weight:600; font-variant-numeric:tabular-nums; }
.server-summary-apps, .server-summary-issues, .server-summary-unknowns { padding:0; margin:0; list-style:none; }
.server-summary-apps li { padding:12px 0; border-top:1px solid var(--border); min-width:0; }
.server-summary-apps li:first-child { padding-top:0; border-top:0; }
.server-summary-app-heading { display:flex; align-items:baseline; justify-content:space-between; gap:12px; min-width:0; }
.server-summary-app-heading a, .server-summary-issues a { font-size:13px; font-weight:600; color:var(--accent-hover); overflow-wrap:anywhere; }
.server-summary-usage { flex-shrink:0; color:var(--text-secondary); font-size:11px; }
.server-summary-apps p { margin-top:4px; font-size:12px; line-height:1.5; color:var(--text-secondary); overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.server-summary-list-note { border-top:1px solid var(--border); padding-top:12px; font-size:12px; color:var(--text-secondary); }
.server-summary-empty { color:var(--text-secondary); font-size:13px; line-height:1.6; }
.server-summary-issues { max-height:320px; overflow-y:auto; scrollbar-gutter:stable; }
.server-summary-issues li { padding:10px 0; border-top:1px solid var(--border); }
.server-summary-issues li:first-child { padding-top:0; border-top:0; }
.server-summary-issues p { margin-top:4px; font-size:12px; color:var(--text-secondary); overflow-wrap:anywhere; }
.server-summary-unknowns li { display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding:8px 0; border-top:1px solid var(--border); font-size:13px; }
.server-summary-unknowns li:first-child { border-top:0; padding-top:0; }
.server-summary-unknowns a { color:var(--accent-hover); overflow-wrap:anywhere; }
.server-summary-unknowns strong { font-size:12px; font-weight:550; font-variant-numeric:tabular-nums; }
.server-summary-settings { margin-top:18px; padding-top:6px; padding-bottom:6px; }
.server-summary-settings > details { padding:12px 0; font-size:12px; }
.server-summary-settings > details + details { border-top:1px solid var(--border); }
.server-summary-settings > details > summary { color:var(--text-secondary); min-height:24px; }
.server-summary-settings > details p { margin:12px 0; font-size:12px; color:var(--text-secondary); }
.server-summary-settings li { overflow-wrap:anywhere; }
.server-map-detail h4 { font-size:13px; margin:16px 0 10px; }
.server-map-detail { margin:18px 0; }
.server-understanding { min-width:0; overflow-wrap:anywhere; }
.server-map-detail h3 { margin:18px 0; font-size:18px; }
.server-map-description { font-size:14px; margin-bottom:18px; }
.server-map-relation { border-top:1px solid var(--border); padding:14px 0; display:grid; gap:6px; }
.server-map-relation small { color:var(--text-secondary); }
.server-map-relation .server-editor { margin-top:8px; }
.server-map-list .server-table th:last-child { width:30%; padding:10px 20px; }
.server-map-list .server-table td:last-child { width:auto; }
.server-map-list .server-table th:first-child { width:30%; }
.server-map-list .server-table th:nth-child(2) { width:20%; }
.server-map-list .server-table a { color:var(--accent-hover); font-weight:600; }
.server-passport { margin-bottom:18px; }
.server-passport-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:24px; }
.server-passport-grid > div { min-width:0; }
.server-passport-grid > div + div { border-left:1px solid var(--border); padding-left:24px; }
.server-passport-grid h3 { font-size:13px; margin-bottom:10px; }
.server-passport-grid .server-facts > div { grid-template-columns:minmax(100px,35%) minmax(0,1fr); gap:12px; padding:7px 0; }
.server-storage { margin-top:20px; padding-top:18px; border-top:1px solid var(--border); }
.server-storage h3 { font-size:13px; margin-bottom:10px; }
.server-storage-table { width:100%; border-collapse:collapse; table-layout:fixed; font-size:13px; }
.server-storage-table th { color:var(--text-secondary); font-size:12px; font-weight:400; text-align:left; }
.server-storage-table th, .server-storage-table td { padding:10px 12px; border-bottom:1px solid var(--border); vertical-align:top; overflow-wrap:anywhere; }
.server-storage-table th:first-child { width:25%; }
.server-storage-table th:nth-child(2) { width:18%; }
.server-storage-table tbody tr:last-child td { border-bottom:0; }
.server-storage-capacity { font-variant-numeric:tabular-nums; font-weight:600; }
.server-passport > summary { display:flex; align-items:center; gap:12px; min-height:28px; list-style:none; }
.server-passport > summary::-webkit-details-marker { display:none; }
.server-passport > summary::before { content:'›'; font-size:20px; color:var(--text-muted); }
.server-passport[open] > summary::before { transform:rotate(90deg); }
.server-passport > summary strong { font-size:14px; }
.server-passport > summary span { font-size:12px; color:var(--text-secondary); }
.server-passport-content { border-top:1px solid var(--border); margin-top:16px; padding-top:20px; }
.server-passport-footer { display:flex; align-items:baseline; justify-content:space-between; gap:12px 24px; flex-wrap:wrap; padding-top:12px; }
.server-passport-footer .server-help { margin:0; flex:1 1 380px; }
.server-passport-footer .text-link { font-size:12px; }
.server-passport-empty { padding:10px 12px; margin-bottom:18px; background:#f4f6f8; font-size:12px; color:var(--text-secondary); border-radius:var(--radius); }
.server-evidence-meta { display:flex; gap:18px; flex-wrap:wrap; border-top:1px solid var(--border); padding-top:14px; margin-top:18px; font-size:12px; color:var(--text-secondary); }
.server-evidence-meta details p, .server-evidence-source p { overflow-wrap:anywhere; white-space:pre-wrap; margin-top:8px; }
.server-brief-source { min-width:0; overflow-wrap:anywhere; }
#server-passport-source, #server-passport-edit { scroll-margin-top:80px; }
.server-observations { margin-top:24px; }
.server-observation-snapshot + .server-observation-snapshot, .server-unverified { margin-top:14px; }
.server-observation-snapshot > h3 { font-size:13px; color:var(--text-secondary); border-bottom:1px solid var(--border); padding-bottom:14px; margin-bottom:18px; }
.server-observation-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px 28px; }
.server-observation-group h4 { font-size:13px; margin-bottom:10px; }
.server-observation-group ul { padding-left:18px; margin:0; font-size:13px; line-height:1.65; overflow-wrap:anywhere; white-space:pre-wrap; }
.server-observation-group li + li { margin-top:10px; }
.server-observation-group:last-child:nth-child(odd) { grid-column:1 / -1; }
.server-evidence-source { margin-top:18px; font-size:11px; color:var(--text-secondary); }
.server-unverified { border-style:dashed; }
.server-unverified > summary { font-size:13px; color:var(--text-secondary); font-weight:550; }
.server-unverified[open] > summary { margin-bottom:18px; }
.server-edit-disclosure { margin-top:18px; }
.server-edit-disclosure > summary, .server-secondary > summary, .server-create > summary { color:var(--accent-hover); font-weight:550; font-size:13px; }
.server-edit-disclosure[open] > summary, .server-secondary[open] > summary, .server-create[open] > summary { padding-bottom:16px; border-bottom:1px solid var(--border); margin-bottom:16px; }
.server-secondary { margin-top:14px; }
.server-secondary > summary { color:var(--text-secondary); }
.server-editor { display:grid; gap:20px; margin-top:18px; min-width:0; }
.server-fieldset { border:0; padding:0; margin:0; min-width:0; }
.server-fieldset legend { font-size:13px; font-weight:650; padding:0 0 12px; }
.server-fieldset + .server-fieldset { padding-top:18px; border-top:1px solid var(--border); }
.server-form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px 20px; }
.server-form-grid label { min-width:0; }
.server-form-grid small { display:block; margin-top:5px; font-weight:400; font-size:11px; color:var(--text-muted); }
.server-editor input { min-width:0; }
.server-editor textarea { min-height:82px; }
.server-form-footer { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; border-top:1px solid var(--border); padding-top:16px; }
.server-help { color:var(--text-secondary); font-size:12px; }
.server-form-footer .server-help { max-width:620px; }
.server-entry { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); margin-bottom:12px; overflow-wrap:anywhere; }
.server-entry > summary { display:flex; gap:18px; align-items:center; list-style:none; padding:18px 20px; }
.server-entry > summary::-webkit-details-marker { display:none; }
.server-entry > summary::before { content:'›'; color:var(--text-muted); font-size:22px; line-height:1; }
.server-entry[open] > summary::before { transform:rotate(90deg); }
.server-entry > summary:hover { background:#f9fbfb; }
.server-entry-copy { display:grid; gap:4px; flex:1; min-width:0; }
.server-entry-copy strong { font-size:14px; font-weight:600; }
.server-entry-copy > span { font-size:12px; color:var(--text-secondary); }
.server-entry-copy small { color:var(--text-muted); font-size:11px; }
.server-entry-hint { color:var(--text-muted); font-size:11px; }
.server-entry-body { padding:4px 20px 20px 48px; }
.server-entry-body .server-facts > div { border-bottom:1px solid #edf0f2; padding:11px 0; }
.server-add { border-style:dashed; background:transparent; }
.server-add > summary { color:var(--accent-hover); font-size:13px; font-weight:550; }
.server-add > summary::before { content:none; }
.server-add .server-entry-body { padding-left:20px; }
.server-filters { display:flex; align-items:end; gap:12px; flex-wrap:wrap; }
.server-filters label { flex:1; min-width:140px; }
.server-filters .server-search { flex:3; }
.server-filters .btn { min-height:36px; }
.server-filter-status { flex-basis:100%; font-size:12px; color:var(--text-secondary); }
.server-filter-status:empty { display:none; }
.server-app-search { max-width:650px; margin-bottom:14px; }
.server-list-note { color:var(--text-muted); font-size:11px; margin-bottom:14px; }
.server-pagination { display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }
.server-empty { text-align:center; padding:38px 20px; background:var(--panel); border:1px dashed var(--border-strong); border-radius:var(--radius); margin:16px 0; }
.server-empty p { color:var(--text-secondary); margin-top:7px; font-size:13px; }
.server-status, .server-error { padding:12px 16px; margin-bottom:20px; border-radius:var(--radius); font-size:13px; }
.server-status { color:var(--success); background:var(--accent-soft); }
.server-error { color:var(--danger); background:var(--danger-soft); }
.server-create { margin-bottom:22px; }
.server-directory-panel { border:1px solid var(--border); border-radius:var(--radius); background:var(--panel); overflow:hidden; }
.server-list-tabs { padding:0 20px; margin:0; }
.server-directory-panel > .server-filters { padding:18px 20px; background:#fafbfc; border-bottom:1px solid var(--border); }
.server-directory-caption { font-size:11px; color:var(--text-secondary); padding:11px 20px; border-bottom:1px solid var(--border); }
.server-table table { width:100%; border-collapse:collapse; table-layout:fixed; }
.server-table th { text-align:left; color:var(--text-muted); background:#fafbfc; font-size:10px; font-weight:550; padding:10px 20px; }
.server-table th:first-child { width:34%; }
.server-table th:nth-child(2) { width:16%; }
.server-table th:last-child { width:50px; padding:10px; }
.server-table td { vertical-align:middle; padding:18px 20px; border-top:1px solid var(--border); white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; }
.server-table tr:hover td { background:#f9fbfb; }
.server-name { display:inline-block; color:var(--accent-hover); font-weight:600; font-size:14px; }
.server-name:hover { text-decoration:underline; }
.server-table small { display:block; color:var(--text-secondary); font-size:12px; margin-top:5px; }
.server-row-open { display:flex; align-items:center; justify-content:center; width:32px; height:36px; color:var(--accent-hover); font-size:18px; }
.server-directory-panel > .server-pagination { padding:0 20px 18px; }
.server-directory-panel > .server-empty { border:0; margin:0; }
.server-timeline { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); padding:0 22px; margin-top:18px; }
.server-event { padding:20px 0 20px 20px; position:relative; border-bottom:1px solid var(--border); }
.server-event:last-child { border-bottom:0; }
.server-event::before { content:''; position:absolute; left:0; top:27px; width:6px; height:6px; border-radius:50%; background:var(--accent); }
.server-event-heading { display:flex; gap:16px; align-items:center; justify-content:space-between; margin-bottom:8px; }
.server-event-heading strong { font-size:13px; }
.server-event time { color:var(--text-muted); font-size:11px; }
.server-event p, .server-event li { font-size:13px; overflow-wrap:anywhere; }
.server-event ul { padding-left:18px; margin:10px 0; }
@media (max-width:1100px) {
  .server-passport-grid { gap:18px; }
  .server-passport-grid > div + div { padding-left:18px; }
  .server-passport-grid .server-facts > div { grid-template-columns:1fr; gap:2px; }
  .server-map-summary-grid { grid-template-columns:minmax(0,1.3fr) minmax(260px,1fr); }
  .server-facts > div { grid-template-columns:120px minmax(0,1fr); gap:12px; }
  .server-table td, .server-table th { padding-left:14px; padding-right:14px; }
}
@media (max-width:800px) {
  .server-passport-grid, .server-observation-grid { grid-template-columns:1fr; }
  .server-passport-grid > div + div { border-left:0; border-top:1px solid var(--border); padding:16px 0 0; }
  .server-passport-grid .server-facts > div { grid-template-columns:minmax(100px,35%) minmax(0,1fr); gap:12px; }
  .server-overview-grid, .server-map-summary-grid { grid-template-columns:1fr; gap:14px; }
  .server-summary-sidebar { grid-row:1; gap:14px; }
  .server-heading { align-items:start; flex-wrap:wrap; gap:14px; }
  .server-tabs { gap:20px; }
  .server-section-heading { align-items:start; flex-wrap:wrap; gap:12px; }
  .server-table thead { display:none; }
  .server-table table, .server-table tbody { display:block; }
  .server-table tr { display:grid; grid-template-columns:minmax(0,1fr) auto; position:relative; padding:16px 20px; border-top:1px solid var(--border); gap:10px 12px; }
  .server-table td { display:block; border:0; padding:0; }
  .server-table td:first-child { grid-column:1; grid-row:1; padding-right:8px; }
  .server-table td:nth-child(2) { grid-column:1; grid-row:2; }
  .server-table td:nth-child(3), .server-table td:nth-child(4) { grid-column:1/-1; }
  .server-table td[data-label]::before { content:attr(data-label) ': '; font-family:Inter,system-ui,sans-serif; color:var(--text-muted); font-size:11px; }
  .server-table td:last-child { grid-column:2; grid-row:1/3; align-self:center; }
  .server-map-list .server-table td:last-child { grid-column:1/-1; grid-row:auto; }
}
@media (max-width:520px) {
  .server-storage-table, .server-storage-table tbody { display:block; }
  .server-storage-table thead { display:none; }
  .server-storage-table tr { display:block; padding:8px 0; border-bottom:1px solid var(--border); }
  .server-storage-table tr:last-child { border-bottom:0; }
  .server-storage-table td { display:grid; grid-template-columns:110px minmax(0,1fr); gap:10px; padding:5px 0; border:0; }
  .server-storage-table td::before { content:attr(data-label); color:var(--text-secondary); font-size:12px; font-weight:400; }
  .server-storage-table td[colspan] { display:block; }
  .server-storage-table td[colspan]::before { content:none; }
  .server-heading h1 { font-size:24px; }
  .server-panel { padding:16px; }
  .server-form-grid { grid-template-columns:1fr; gap:14px; }
  .server-facts > div { grid-template-columns:1fr; gap:3px; padding:8px 0; }
  .server-passport > summary { flex-wrap:wrap; gap:4px 10px; }
  .server-passport > summary span { flex-basis:100%; margin-left:18px; }
  .server-summary-app-heading { align-items:start; flex-direction:column; gap:3px; }
  .server-inventory-counts { grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 16px; }
  .server-entry > summary { flex-wrap:wrap; gap:10px; padding:16px; }
  .server-entry-copy { flex-basis:calc(100% - 32px); }
  .server-entry > summary > .server-badge { margin-left:22px; }
  .server-entry-hint { margin-left:auto; }
  .server-entry-body { padding:0 16px 16px; }
  .server-event-heading { align-items:start; flex-direction:column; gap:4px; }
  .server-filters label, .server-filters .server-search { flex-basis:100%; }
  .server-tabs { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 16px; overflow:visible; border-bottom:0; }
  .server-tabs a { min-width:0; min-height:46px; font-size:12px; border-bottom-color:var(--border); }
  .server-section-heading .btn, .server-heading > .btn { min-height:40px; }
}
@media (prefers-reduced-motion:reduce) { .server-tabs a, .server-entry summary { transition:none; } }
"""
