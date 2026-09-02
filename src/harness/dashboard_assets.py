from __future__ import annotations

DASHBOARD_CSS = r"""
:root {
  color-scheme: dark;
  --bg: #0b0d12;
  --sidebar: #0d0f14;
  --panel: #13161d;
  --panel-raised: #171a22;
  --panel-hover: #1b1f29;
  --panel-subtle: #10131a;
  --text: #f3f5f8;
  --text-secondary: #aab2c0;
  --text-muted: #737d8e;
  --border: #252a35;
  --border-strong: #343b49;
  --accent: #748cff;
  --accent-hover: #91a2ff;
  --accent-soft: rgba(116, 140, 255, 0.12);
  --accent-border: rgba(116, 140, 255, 0.32);
  --success: #55c993;
  --success-soft: rgba(85, 201, 147, 0.12);
  --warning: #efb45f;
  --warning-soft: rgba(239, 180, 95, 0.12);
  --danger: #f2767f;
  --danger-soft: rgba(242, 118, 127, 0.11);
  --info: #67b5e8;
  --info-soft: rgba(103, 181, 232, 0.11);
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.2), 0 8px 24px rgba(0, 0, 0, 0.12);
  --shadow-lg: 0 24px 72px rgba(0, 0, 0, 0.34);
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  --space-7: 40px;
  --space-8: 56px;
  --font-sans: Inter, "SF Pro Text", "SF Pro Display", "Segoe UI Variable", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-mono: "SFMono-Regular", "Cascadia Code", "Roboto Mono", Consolas, monospace;
  font-family: var(--font-sans);
  font-size: 15px;
  font-synthesis: none;
  background: var(--bg);
  color: var(--text);
}

* { box-sizing: border-box; }
html { min-width: 320px; min-height: 100%; background: var(--bg); }
body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
a { color: inherit; }
button, input, textarea, select { font: inherit; }
button, a, summary { -webkit-tap-highlight-color: transparent; }
::selection { background: rgba(116, 140, 255, 0.28); color: #fff; }

.app-layout {
  display: grid;
  grid-template-columns: 292px minmax(0, 1fr);
  min-height: 100vh;
}
.app-sidebar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  border-right: 1px solid var(--border);
  background: var(--sidebar);
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 72px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--border);
  text-decoration: none;
}
.brand-mark {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: linear-gradient(145deg, #8195ff, #6279ef);
  color: #fff;
  font-size: 15px;
  font-weight: 800;
  letter-spacing: -0.03em;
  box-shadow: 0 8px 22px rgba(74, 99, 220, 0.26), inset 0 1px rgba(255, 255, 255, 0.22);
}
.brand-copy { display: grid; gap: 2px; min-width: 0; }
.brand-copy strong { font-size: 14px; font-weight: 720; letter-spacing: -0.015em; }
.brand-copy small { color: var(--text-muted); font-size: 11px; }

.project-navigation {
  min-height: 0;
  overflow: auto;
  padding: 14px 10px 28px;
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
}
.overview-link,
.nav-project-link,
.nav-workspace,
.nav-task {
  text-decoration: none;
  transition: background .14s ease, color .14s ease, border-color .14s ease;
}
.overview-link {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 9px 11px;
  border-radius: 9px;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 650;
}
.overview-link:hover { background: var(--panel-raised); color: var(--text); }
.overview-link.is-current { background: var(--accent-soft); color: #dce2ff; }
.nav-overview-icon { display: grid; width: 18px; place-items: center; color: var(--accent-hover); font-size: 16px; }
.nav-label {
  margin: 25px 11px 10px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: .085em;
  text-transform: uppercase;
}
.nav-project { margin-bottom: 8px; border-radius: 11px; }
.nav-project.is-context { background: rgba(255, 255, 255, 0.025); }
.nav-project-link {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  min-height: 40px;
  padding: 8px 11px;
  border-radius: 9px;
}
.nav-project-link:hover { background: var(--panel-raised); }
.nav-project-name { overflow: hidden; color: var(--text); font-size: 13px; font-weight: 680; text-overflow: ellipsis; white-space: nowrap; }
.nav-project-id { color: var(--text-muted); font-size: 10px; }
.nav-workspaces { padding: 1px 5px 8px 11px; }
.nav-workspace {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  min-height: 42px;
  padding: 7px 9px;
  border-radius: 8px;
}
.nav-workspace:hover,
.nav-workspace.is-current { background: var(--panel-raised); }
.nav-state { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); box-shadow: 0 0 0 3px rgba(115, 125, 142, 0.08); }
.nav-state[data-state="working"] { background: var(--success); box-shadow: 0 0 0 3px var(--success-soft); }
.nav-state[data-state="waiting"] { background: var(--warning); box-shadow: 0 0 0 3px var(--warning-soft); }
.nav-state[data-state="completed"] { background: var(--info); box-shadow: 0 0 0 3px var(--info-soft); }
.nav-state[data-state="cancelled"] { background: var(--danger); box-shadow: 0 0 0 3px var(--danger-soft); }
.nav-workspace-copy { display: grid; min-width: 0; gap: 2px; }
.nav-workspace-name,
.nav-workspace-meta,
.nav-task span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-workspace-name { color: var(--text-secondary); font-size: 12px; font-weight: 620; }
.nav-workspace.is-current .nav-workspace-name { color: var(--text); }
.nav-workspace-meta { color: var(--text-muted); font-family: var(--font-mono); font-size: 10px; }
.nav-task {
  display: block;
  margin: 2px 4px 3px 27px;
  padding: 7px 10px;
  border-left: 1px solid var(--border-strong);
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}
.nav-task:hover { border-left-color: var(--accent); color: var(--text-secondary); }
.nav-task.is-current { border-left-color: var(--accent); color: #dce2ff; }
.nav-empty { margin: 12px; color: var(--text-muted); font-size: 12px; line-height: 1.5; }
.sidebar-footer { margin-top: auto; padding: 14px 20px; border-top: 1px solid var(--border); }

.app-stage { min-width: 0; }
.context-header {
  position: sticky;
  top: 0;
  z-index: 20;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  min-height: 64px;
  padding: 0 clamp(24px, 3vw, 48px);
  border-bottom: 1px solid var(--border);
  background: rgba(11, 13, 18, 0.88);
  backdrop-filter: blur(18px) saturate(130%);
}
.breadcrumbs { min-width: 0; }
.breadcrumbs ol { display: flex; align-items: center; min-width: 0; margin: 0; padding: 0; list-style: none; }
.breadcrumbs li { display: flex; min-width: 0; align-items: center; color: var(--text-muted); font-size: 12px; }
.breadcrumbs li + li::before { content: "/"; flex: 0 0 auto; margin: 0 10px; color: #454d5c; }
.breadcrumbs a,
.breadcrumbs span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.breadcrumbs a { text-decoration: none; }
.breadcrumbs a:hover { color: var(--text-secondary); }
.breadcrumbs [aria-current="page"] { color: var(--text-secondary); font-weight: 600; }
.mobile-navigation { display: none; }

.live-indicator { display: inline-flex; align-items: center; gap: 8px; min-height: 28px; color: var(--text-muted); font-size: 11px; white-space: nowrap; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); box-shadow: 0 0 0 3px var(--success-soft); }
.live-indicator[data-state="reconnecting"] .live-dot { background: var(--warning); box-shadow: 0 0 0 3px var(--warning-soft); }
.live-indicator[data-state="update"] .live-dot { background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); animation: live-pulse 1.4s ease-in-out infinite; }
.update-link { display: none; border: 0; background: transparent; color: var(--accent-hover); padding: 0; font-size: inherit; font-weight: 700; cursor: pointer; }
.live-indicator[data-state="update"] .update-link { display: inline; }

.content-frame {
  width: 100%;
  max-width: 1680px;
  margin: 0 auto;
  padding: clamp(32px, 4vw, 56px) clamp(28px, 3.6vw, 56px) 80px;
}
.page-intro {
  display: flex;
  justify-content: space-between;
  gap: 32px;
  align-items: flex-end;
  margin-bottom: 32px;
}
.page-intro.compact { margin-bottom: 28px; }
.eyebrow,
.panel-kicker,
.project-kicker,
.workspace-card-label {
  margin: 0 0 8px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 720;
  letter-spacing: .075em;
  text-transform: uppercase;
}
.page-intro h1,
.project-title,
.section-title,
.task-title {
  margin: 0;
  font-family: var(--font-sans);
  font-weight: 720;
  letter-spacing: -0.035em;
}
.page-intro h1 { max-width: 34ch; font-size: clamp(34px, 3.3vw, 48px); line-height: 1.05; text-wrap: balance; overflow-wrap: anywhere; }
.page-intro.compact h1 { font-size: clamp(32px, 3vw, 44px); }
.page-intro .task-title { max-width: 34ch; font-size: clamp(32px, 3vw, 44px); line-height: 1.08; text-wrap: pretty; }
.hero-copy { max-width: 760px; margin: 13px 0 0; color: var(--text-secondary); font-size: 14px; line-height: 1.6; }
.identity-line { color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; overflow-wrap: anywhere; }
.page-intro-actions { width: min(280px, 100%); }
.task-intro-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; margin-top: 18px; color: var(--text-muted); font-size: 12px; }

.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 0 0 32px;
}
.metric {
  min-height: 102px;
  padding: 18px 20px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  box-shadow: var(--shadow-sm);
}
.metric-label { color: var(--text-muted); font-size: 12px; }
.metric-value { display: block; margin-top: 12px; font-size: 28px; font-weight: 720; line-height: 1; letter-spacing: -0.035em; }
.metric:nth-child(2) .metric-value { color: var(--success); }
.metric:nth-child(3) .metric-value { color: var(--warning); }

.project-stack { display: grid; gap: 24px; }
.project-section {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--panel);
  box-shadow: var(--shadow-sm);
}
.project-section-head {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: center;
  padding: 22px 24px;
  border-bottom: 1px solid var(--border);
}
.project-kicker { margin-bottom: 5px; }
.project-title { font-size: 22px; line-height: 1.2; }
.project-title a { text-decoration: none; }
.project-title a:hover { color: var(--accent-hover); }
.project-meta { margin: 6px 0 0; color: var(--text-muted); font-size: 12px; }
.project-controls { display: flex; align-items: center; justify-content: flex-end; gap: 16px; }
.project-controls .visibility-box { width: auto; }
.project-controls .visibility-form .btn { width: auto; }
.workspace-list { display: grid; }
.workspace-card {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  min-width: 0;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
  transition: background .14s ease;
}
.workspace-card:last-child { border-bottom: 0; }
.workspace-card:hover { background: var(--panel-raised); }
.workspace-card::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: transparent; }
.workspace-card[data-state="working"]::before { background: var(--success); }
.workspace-card[data-state="waiting"]::before { background: var(--warning); }
.workspace-card[data-state="completed"]::before { background: var(--info); }
.workspace-card-main { min-width: 0; padding: 22px 24px; }
.workspace-card-head { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }
.workspace-card-label { margin-bottom: 5px; }
.workspace-name { margin: 0; font-size: 17px; font-weight: 680; letter-spacing: -0.018em; line-height: 1.3; }
.workspace-name a { text-decoration: none; }
.workspace-name a:hover { color: var(--accent-hover); }
.workspace-path { margin: 6px 0 0; color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.task-focus {
  margin: 18px 0 15px;
  padding: 17px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel-subtle);
}
.task-focus-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.task-focus-links { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 9px; }
.task-focus-label { color: var(--text-muted); font-size: 11px; font-weight: 700; }
.task-focus-title { margin: 8px 0 0; font-size: 16px; font-weight: 680; line-height: 1.4; text-wrap: pretty; }
.task-focus-title a { text-decoration: none; }
.task-focus-title a:hover { color: var(--accent-hover); }
.task-git-branch { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin: 9px 0 0; color: var(--text-muted); font-size: 11px; }
.task-git-branch strong { color: var(--text-secondary); overflow-wrap: anywhere; }
.task-jira { color: var(--accent-hover); font-size: 11px; font-weight: 700; text-decoration: none; }
.task-jira:hover { text-decoration: underline; }
.task-marker { color: var(--warning); font-size: 11px; font-weight: 680; }
.next-step { margin-top: 14px; padding-top: 13px; border-top: 1px solid var(--border); }
.next-step > span { color: var(--text-muted); font-size: 11px; font-weight: 700; }
.next-step p { margin: 6px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.55; }
.text-link { color: var(--accent-hover); font-size: 12px; font-weight: 680; text-decoration: none; }
.text-link span { display: inline-block; margin-left: 3px; transition: transform .14s ease; }
.text-link:hover span { transform: translateX(3px); }
.workspace-card-side {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  padding: 22px;
  border-left: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.012);
}
.mini-stats { display: grid; }
.mini-stat { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; padding: 9px 0; border-bottom: 1px solid var(--border); color: var(--text-muted); font-size: 11px; }
.mini-stat:last-child { border-bottom: 0; }
.mini-stat strong { max-width: 150px; color: var(--text-secondary); font-family: var(--font-mono); font-size: 11px; font-weight: 560; overflow-wrap: anywhere; text-align: right; }

.pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  width: fit-content;
  min-height: 27px;
  padding: 5px 10px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 650;
  white-space: nowrap;
}
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill-working { color: var(--success); background: var(--success-soft); border-color: rgba(85, 201, 147, 0.18); }
.pill-waiting { color: var(--warning); background: var(--warning-soft); border-color: rgba(239, 180, 95, 0.18); }
.pill-completed { color: var(--info); background: var(--info-soft); border-color: rgba(103, 181, 232, 0.18); }
.pill-cancelled { color: var(--danger); background: var(--danger-soft); border-color: rgba(242, 118, 127, 0.18); }
.pill-idle { color: var(--text-muted); background: var(--panel-raised); border-color: var(--border); }
.pill-review { color: var(--accent-hover); background: var(--accent-soft); border-color: var(--accent-border); }

.action-panel { display: grid; gap: 10px; width: 100%; }
.visibility-box { display: grid; gap: 9px; width: 100%; }
.visibility-hint { margin: 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }
.visibility-form { margin: 0; width: 100%; }
.visibility-form .btn { width: 100%; }
.management-disclosure { width: 100%; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
.management-disclosure summary { list-style: none; color: var(--text-secondary); font-size: 12px; font-weight: 650; cursor: pointer; }
.management-disclosure summary::-webkit-details-marker { display: none; }
.management-disclosure summary::before { content: "+ "; color: var(--accent-hover); }
.management-disclosure[open] summary::before { content: "− "; }
.management-disclosure.danger-zone summary,
.management-disclosure.danger-zone summary::before { color: var(--danger); }
.management-hint { margin: 10px 0 0; color: var(--text-muted); font-size: 11px; line-height: 1.55; }
.action-row { display: flex; flex-wrap: wrap; gap: 9px; }
.action-row form { margin: 0; }
.btn {
  appearance: none;
  min-height: 38px;
  padding: 9px 13px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--panel-raised);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 650;
  cursor: pointer;
  transition: transform .14s ease, border-color .14s ease, background .14s ease, color .14s ease;
}
.btn:hover { transform: translateY(-1px); border-color: #4a5365; background: var(--panel-hover); color: var(--text); }
.btn-primary { border-color: #7187ef; background: #667cee; color: #fff; }
.btn-primary:hover { border-color: #8fa0ff; background: #768cff; color: #fff; }
.btn-danger { color: var(--danger); }
.btn-danger:hover { border-color: rgba(242, 118, 127, 0.42); background: var(--danger-soft); color: #ff9aa1; }
.feedback-disclosure { width: 100%; padding-top: 9px; border-top: 1px solid var(--border); }
.feedback-disclosure summary { list-style: none; color: var(--text-secondary); font-size: 12px; font-weight: 620; cursor: pointer; }
.feedback-disclosure summary::-webkit-details-marker { display: none; }
.feedback-disclosure summary::before { content: "+ "; color: var(--accent-hover); }
.feedback-disclosure[open] summary::before { content: "− "; }
.feedback-form { display: grid; gap: 9px; margin-top: 11px; }
.feedback-form label { color: var(--text-muted); font-size: 11px; }
.feedback-form textarea,
.feedback-form input,
.feedback-form select,
.search-input {
  width: 100%;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--panel-subtle);
  color: var(--text);
  padding: 10px 12px;
  font: inherit;
  font-size: 13px;
}
.feedback-form textarea { min-height: 96px; resize: vertical; line-height: 1.5; }
.feedback-form textarea:focus,
.feedback-form input:focus,
.feedback-form select:focus,
.search-input:focus { outline: 3px solid rgba(116, 140, 255, 0.18); border-color: var(--accent); }

.panel {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--panel);
  box-shadow: var(--shadow-sm);
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  min-height: 72px;
  padding: 17px 20px;
  border-bottom: 1px solid var(--border);
}
.panel-kicker { margin-bottom: 4px; }
.panel-head h2 { margin: 0; font-size: 17px; font-weight: 680; letter-spacing: -0.018em; }
.panel-head h2 a { text-decoration: none; }
.panel-head h2 a:hover { color: var(--accent-hover); }
.panel-body { padding: 20px; }
.section-note { margin: 0; color: var(--text-muted); font-size: 11px; }
.workspace-layout,
.task-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 360px);
  gap: 20px;
  align-items: start;
}
.workspace-main,
.task-main,
.workspace-aside,
.task-aside { display: grid; gap: 20px; min-width: 0; }
.sticky-panel,
.task-aside { position: sticky; top: 84px; }
.search-panel { margin-bottom: 20px; }
.search-shortcut {
  display: grid;
  place-items: center;
  width: 27px;
  height: 27px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--panel-subtle);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  box-shadow: 0 1px 0 #05060a;
}
.search-box { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
.search-input { min-height: 42px; }
.search-input::placeholder { color: var(--text-muted); }
.search-results { margin-top: 18px; border-top: 1px solid var(--border); }
.search-hit { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; padding: 15px 0; border-bottom: 1px solid var(--border); }
.search-hit:last-child { border-bottom: 0; }
.search-hit-path { overflow-wrap: anywhere; font-family: var(--font-mono); font-size: 11px; }
.search-hit-path a { color: var(--accent-hover); font-family: var(--font-sans); font-size: 13px; font-weight: 660; text-decoration: none; }
.search-hit-meta { color: var(--text-muted); font-size: 11px; text-align: right; white-space: nowrap; }

.focus-panel .panel-body { display: grid; gap: 17px; }
.task-primary-meta { display: flex; flex-wrap: wrap; gap: 9px 16px; color: var(--text-muted); font-size: 12px; }
.settings-divider { margin: 18px 0; border-top: 1px solid var(--border); }
.fact-list { display: grid; margin: 0; }
.fact { display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.fact:last-child { border-bottom: 0; }
.fact dt { color: var(--text-muted); font-size: 11px; }
.fact dd { margin: 0; overflow-wrap: anywhere; color: var(--text-secondary); font-size: 12px; line-height: 1.5; }
.fact a { color: var(--accent-hover); text-decoration: none; }
.fact a:hover { text-decoration: underline; }
.mono { font-family: var(--font-mono); font-size: .92em; }

.task-list { display: grid; }
.task-row {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 18px;
  align-items: center;
  padding: 16px 0 16px 14px;
  border-bottom: 1px solid var(--border);
}
.task-row:last-child { border-bottom: 0; }
.task-row::before { content: ""; position: absolute; inset: 19px auto 19px 0; width: 3px; border-radius: 2px; background: var(--border-strong); }
.task-row[data-state="working"]::before { background: var(--success); }
.task-row[data-state="waiting"]::before { background: var(--warning); }
.task-row[data-state="completed"]::before { background: var(--info); }
.task-row-title { margin: 0; font-size: 13px; font-weight: 660; line-height: 1.45; }
.task-row-title a { text-decoration: none; }
.task-row-title a:hover { color: var(--accent-hover); }
.task-row-meta { display: flex; flex-wrap: wrap; gap: 7px 13px; margin-top: 7px; color: var(--text-muted); font-size: 11px; }
.task-row-meta .task-git-branch { display: inline-flex; margin: 0; }
.task-row-aside { display: flex; align-items: center; gap: 13px; }
.row-arrow { color: var(--text-muted); transition: transform .14s ease, color .14s ease; }
.task-row:hover .row-arrow { color: var(--accent-hover); transform: translateX(3px); }

.task-update {
  position: relative;
  overflow: hidden;
  padding: 22px 24px;
  border: 1px solid var(--accent-border);
  border-radius: var(--radius-lg);
  background: linear-gradient(135deg, rgba(116, 140, 255, 0.12), rgba(116, 140, 255, 0.045));
  box-shadow: var(--shadow-sm);
}
.task-update::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); }
.task-update .panel-kicker { color: var(--accent-hover); }
.task-update-summary { margin: 0; color: var(--text); font-size: 16px; font-weight: 650; line-height: 1.5; }
.task-update .next-step { border-color: rgba(116, 140, 255, 0.2); }
.action-card .panel-body,
.facts-card .panel-body { padding: 18px; }

.timeline { position: relative; display: grid; padding-left: 18px; }
.timeline::before { content: ""; position: absolute; top: 9px; bottom: 15px; left: 5px; width: 1px; background: var(--border-strong); }
.timeline-item { position: relative; padding: 0 0 28px 24px; }
.timeline-item:last-child { padding-bottom: 0; }
.timeline-item::before {
  content: "";
  position: absolute;
  top: 6px;
  left: -17px;
  width: 9px;
  height: 9px;
  border: 2px solid var(--panel);
  border-radius: 50%;
  background: var(--text-muted);
  box-shadow: 0 0 0 1px var(--border-strong);
}
.timeline-item[data-kind="operator_feedback"]::before,
.timeline-item[data-kind="operator_comment"]::before { background: var(--accent); }
.timeline-item[data-kind="accepted"]::before { background: var(--success); }
.timeline-item[data-kind="cancelled"]::before { background: var(--danger); }
.timeline-item[data-kind="checkpoint"]::before { background: var(--info); }
.timeline-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 13px; }
.timeline-title { margin: 0; font-size: 13px; font-weight: 660; }
.timeline-time { color: var(--text-muted); font-family: var(--font-mono); font-size: 10px; }
.timeline-content { margin-top: 9px; color: var(--text-secondary); font-size: 12px; line-height: 1.65; }
.timeline-summary { color: var(--text); font-size: 13px; }
.timeline-branch { margin-bottom: 7px; color: var(--text-muted); font-size: 11px; }
.timeline-branch strong { margin-right: 8px; font-weight: 600; }
.feedback-quote { margin: 11px 0 0; padding: 12px 14px; border-left: 3px solid var(--accent); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; background: var(--accent-soft); color: #cbd3ff; white-space: pre-wrap; }
.path-chips { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 11px; }
.path-chip { max-width: 100%; padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-subtle); color: var(--text-secondary); font-family: var(--font-mono); font-size: 10px; overflow-wrap: anywhere; }

.empty-state { padding: 40px 24px; border: 1px dashed var(--border-strong); border-radius: var(--radius-lg); background: var(--panel-subtle); text-align: center; }
.empty-state strong { display: block; font-size: 17px; font-weight: 660; }
.empty-state span { display: block; margin-top: 8px; color: var(--text-muted); font-size: 12px; }
.error-note { color: var(--danger); font-weight: 700; }

.skip-link { position: fixed; top: 8px; left: 8px; z-index: 80; transform: translateY(-160%); padding: 9px 12px; border-radius: 8px; background: var(--text); color: var(--bg); text-decoration: none; }
.skip-link:focus { transform: translateY(0); }
:focus-visible { outline: 3px solid rgba(116, 140, 255, 0.5); outline-offset: 3px; }

@keyframes live-pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(.76); opacity: .62; }
}

@media (max-width: 1180px) {
  .app-layout { grid-template-columns: 264px minmax(0, 1fr); }
  .workspace-layout,
  .task-layout { grid-template-columns: minmax(0, 1fr) 320px; }
  .workspace-card { grid-template-columns: minmax(0, 1fr) 250px; }
}

@media (max-width: 940px) {
  .app-layout { display: block; }
  .app-sidebar { display: none; }
  .context-header { grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; padding-inline: 18px; }
  .mobile-navigation { position: relative; display: block; }
  .mobile-navigation summary {
    display: flex;
    align-items: center;
    gap: 9px;
    min-height: 44px;
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 650;
    list-style: none;
    cursor: pointer;
  }
  .mobile-navigation summary::-webkit-details-marker { display: none; }
  .mobile-navigation .brand-mark { width: 30px; height: 30px; font-size: 12px; }
  .mobile-chevron { color: var(--text-muted); transition: transform .14s ease; }
  .mobile-navigation[open] .mobile-chevron { transform: rotate(180deg); }
  .mobile-navigation-panel {
    position: absolute;
    top: 50px;
    left: 0;
    width: min(360px, calc(100vw - 36px));
    max-height: min(72vh, 680px);
    overflow: auto;
    border: 1px solid var(--border-strong);
    border-radius: 13px;
    background: var(--sidebar);
    box-shadow: var(--shadow-lg);
  }
  .breadcrumbs li:first-child { display: none; }
  .breadcrumbs li:nth-child(2)::before { display: none; }
  .workspace-layout,
  .task-layout { grid-template-columns: 1fr; }
  .sticky-panel,
  .task-aside { position: static; }
  .task-aside { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 720px) {
  .content-frame { padding: 28px 16px 56px; }
  .context-header { min-height: 58px; }
  .mobile-navigation summary > span:nth-child(2) { display: none; }
  .header-live-indicator .live-copy { display: none; }
  .page-intro { align-items: stretch; flex-direction: column; gap: 22px; margin-bottom: 26px; }
  .page-intro h1,
  .page-intro.compact h1,
  .page-intro .task-title { font-size: clamp(29px, 9vw, 38px); }
  .page-intro-actions { width: 100%; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .metric { min-height: 90px; }
  .project-section-head,
  .workspace-card-head { align-items: flex-start; }
  .project-section-head { flex-direction: column; padding: 19px 18px; }
  .project-controls { width: 100%; justify-content: space-between; }
  .workspace-card { grid-template-columns: 1fr; }
  .workspace-card-main { padding: 20px 18px; }
  .workspace-card-side { padding: 17px 18px 20px; border-top: 1px solid var(--border); border-left: 0; }
  .search-box { grid-template-columns: 1fr; }
  .task-aside { grid-template-columns: 1fr; }
  .task-row { grid-template-columns: 1fr; gap: 11px; }
  .task-row-aside { justify-content: space-between; }
  .fact { grid-template-columns: 1fr; gap: 5px; }
  .search-hit { grid-template-columns: 1fr; gap: 6px; }
  .search-hit-meta { text-align: left; }
}

@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #f5f6f8;
    --sidebar: #ffffff;
    --panel: #ffffff;
    --panel-raised: #f4f6f9;
    --panel-hover: #eef1f5;
    --panel-subtle: #f8f9fb;
    --text: #171a21;
    --text-secondary: #505866;
    --text-muted: #858e9d;
    --border: #e2e5ea;
    --border-strong: #cfd4dc;
    --accent: #5d74ea;
    --accent-hover: #435cd7;
    --accent-soft: rgba(93, 116, 234, 0.09);
    --accent-border: rgba(93, 116, 234, 0.25);
    --success: #258661;
    --success-soft: rgba(37, 134, 97, 0.1);
    --warning: #a76a1e;
    --warning-soft: rgba(167, 106, 30, 0.1);
    --danger: #c64f59;
    --danger-soft: rgba(198, 79, 89, 0.09);
    --info: #397da8;
    --info-soft: rgba(57, 125, 168, 0.09);
    --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.04), 0 8px 24px rgba(15, 23, 42, 0.04);
    --shadow-lg: 0 24px 72px rgba(15, 23, 42, 0.18);
  }
  .context-header { background: rgba(245, 246, 248, 0.9); }
  .workspace-card-side { background: rgba(15, 23, 42, 0.012); }
  .search-shortcut { box-shadow: 0 1px 0 #bcc2cc; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
  }
}
""".strip()


DASHBOARD_JS = r"""
(() => {
  const body = document.body;
  let eventsUrl = body.dataset.eventsUrl;
  let source = null;
  let inFlight = false;
  let queued = false;
  let queuedForce = false;

  const fieldHasChanged = (field) => {
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
    currentLayout.replaceWith(nextLayout);
    const nextTitle = nextDocument.querySelector('title');
    if (nextTitle && nextTitle.textContent) {
      document.title = nextTitle.textContent;
    }
    if (nextDocument.body && nextDocument.body.dataset.eventsUrl) {
      body.dataset.eventsUrl = nextDocument.body.dataset.eventsUrl;
    }
    bindRefreshButtons();
    bindMobileNavigation();
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
    source.addEventListener('ready', () => setState('live', 'Онлайн'));
    source.addEventListener('refresh', () => {
      void refreshPage({ force: false });
    });
    source.onerror = () => setState('reconnecting', 'Переподключение');
  };

  const refreshPage = async (options) => {
    queued = true;
    queuedForce = queuedForce || Boolean(options.force);
    if (inFlight) {
      return;
    }
    inFlight = true;
    try {
      while (queued) {
        queued = false;
        const force = queuedForce;
        queuedForce = false;
        if (!force && hasUnsavedInput()) {
          setState('update', 'Есть обновление');
          break;
        }
        setState('update', 'Обновление');
        const response = await fetch(`${window.location.pathname}${window.location.search}`, {
          cache: 'no-store',
          credentials: 'same-origin',
          headers: { Accept: 'text/html' },
        });
        if (!response.ok) {
          throw new Error('dashboard refresh failed');
        }
        const html = await response.text();
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
        connectEvents();
        setState('live', 'Онлайн');
      }
    } catch {
      setState('update', 'Есть обновление');
    } finally {
      inFlight = false;
      if (queued) {
        void refreshPage({ force: queuedForce });
      }
    }
  };

  bindRefreshButtons();
  bindMobileNavigation();

  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
      return;
    }
    const search = document.querySelector('input[type="search"]');
    if (search instanceof HTMLInputElement) {
      event.preventDefault();
      search.focus();
    }
  });

  connectEvents();
  window.addEventListener('pagehide', () => disconnectEvents(), { once: true });
})();
""".strip()
