from __future__ import annotations

DASHBOARD_CSS = r"""
:root {
  color-scheme: light;
  --canvas: #f4f1ea;
  --canvas-strong: #ebe6db;
  --surface: #fffdf8;
  --surface-raised: #ffffff;
  --ink: #1c211e;
  --muted: #69706b;
  --faint: #929892;
  --line: #ddd7ca;
  --line-strong: #c8c0b2;
  --accent: #d8683d;
  --accent-strong: #a74727;
  --accent-soft: #f7e1d7;
  --good: #2e7657;
  --good-soft: #dceee5;
  --warn: #855615;
  --warn-soft: #f4ead4;
  --danger: #a43e36;
  --danger-soft: #f5dfdd;
  --info: #426d82;
  --info-soft: #dfeaf0;
  --shadow: 0 18px 50px rgba(47, 42, 32, 0.08);
  --shadow-small: 0 8px 24px rgba(47, 42, 32, 0.07);
  --radius-sm: 8px;
  --radius-md: 14px;
  --radius-lg: 22px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  --space-7: 48px;
  --space-8: 64px;
  font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  background: var(--canvas);
  color: var(--ink);
}

* { box-sizing: border-box; }
html { min-width: 320px; background: var(--canvas); }
body { margin: 0; min-height: 100vh; background: var(--canvas); color: var(--ink); }
a { color: inherit; }
button, input, textarea { font: inherit; }
button, a, summary { -webkit-tap-highlight-color: transparent; }

::selection { background: var(--accent-soft); color: var(--ink); }

.shell { width: min(1440px, calc(100% - 40px)); margin: 0 auto; padding-bottom: var(--space-8); }
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-height: 68px;
  background: var(--canvas);
  background: color-mix(in srgb, var(--canvas) 90%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--line) 84%, transparent);
  backdrop-filter: blur(16px);
}
.brand { display: inline-flex; align-items: center; gap: 10px; text-decoration: none; font-weight: 760; letter-spacing: -0.02em; }
.brand-mark {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--ink);
  border-radius: 9px 9px 9px 3px;
  background: var(--ink);
  color: var(--surface);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  box-shadow: 4px 4px 0 var(--accent);
}
.topbar-meta { display: flex; align-items: center; gap: var(--space-3); color: var(--muted); font-size: 13px; }
.live-indicator { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 4px var(--good-soft); }
.live-indicator[data-state="reconnecting"] .live-dot { background: var(--warn); box-shadow: 0 0 0 4px var(--warn-soft); }
.live-indicator[data-state="update"] .live-dot { background: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); animation: live-pulse 1.5s ease-in-out infinite; }
.live-copy { color: var(--muted); }
.update-link { display: none; border: 0; background: transparent; color: var(--accent-strong); padding: 0; font-weight: 700; cursor: pointer; }
.live-indicator[data-state="update"] .update-link { display: inline; }

.breadcrumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; padding: 22px 0 0; color: var(--muted); font-size: 13px; }
.breadcrumbs a { text-decoration: none; }
.breadcrumbs a:hover { color: var(--ink); }
.breadcrumbs .sep { color: var(--faint); }

.hero { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(260px, .8fr); gap: var(--space-7); align-items: end; padding: 56px 0 38px; }
.hero.compact { padding-top: 38px; }
.eyebrow { margin: 0 0 10px; color: var(--accent-strong); font-size: 12px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
.hero h1, .section-title, .task-title {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-weight: 600;
  letter-spacing: -0.035em;
}
.hero h1 { margin: 0; max-width: 16ch; font-size: clamp(42px, 6vw, 78px); line-height: .98; }
.hero.compact h1 { font-size: clamp(36px, 5vw, 62px); }
.hero-copy { margin: 18px 0 0; max-width: 760px; color: var(--muted); font-size: 17px; line-height: 1.65; }
.hero-aside { display: grid; gap: 10px; align-content: end; }
.identity-line { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.65; color: var(--muted); }

.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-3); margin-bottom: var(--space-7); }
.metric { min-height: 118px; padding: 18px 20px; border: 1px solid var(--line); border-radius: var(--radius-md); background: color-mix(in srgb, var(--surface) 86%, transparent); }
.metric-label { color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.metric-value { display: block; margin-top: 16px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 30px; font-weight: 650; letter-spacing: -0.04em; }

.section { margin-top: var(--space-7); }
.section-head { display: flex; align-items: end; justify-content: space-between; gap: var(--space-4); margin-bottom: var(--space-4); }
.section-title { margin: 0; font-size: clamp(26px, 3vw, 38px); line-height: 1.05; }
.section-note { margin: 0; color: var(--muted); font-size: 13px; }

.workspace-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.workspace-card {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: var(--space-5);
  min-height: 260px;
  padding: 24px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-small);
  transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
}
.workspace-card:hover { transform: translateY(-2px); border-color: var(--line-strong); box-shadow: var(--shadow); }
.card-main { min-width: 0; }
.card-kicker { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin-bottom: 20px; }
.project-link { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; text-decoration: none; }
.project-link:hover { color: var(--accent-strong); }
.workspace-name { margin: 0; font-size: 24px; font-weight: 760; letter-spacing: -0.035em; }
.workspace-name a { text-decoration: none; }
.workspace-path { margin: 7px 0 0; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.task-focus { margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--line); }
.task-focus-label { margin-bottom: 7px; color: var(--faint); font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.task-focus-title { margin: 0; font-size: 17px; font-weight: 720; line-height: 1.35; }
.task-focus-title a { text-decoration: none; }
.task-focus-title a:hover { color: var(--accent-strong); }
.next-step { margin: 10px 0 0; color: var(--muted); line-height: 1.55; }
.card-side { display: flex; flex-direction: column; align-items: flex-end; gap: var(--space-3); min-width: 132px; }
.mini-stats { display: grid; gap: 8px; width: 100%; }
.mini-stat { display: flex; justify-content: space-between; gap: 14px; color: var(--muted); font-size: 12px; }
.mini-stat strong { color: var(--ink); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-weight: 650; }

.pill { display: inline-flex; align-items: center; gap: 7px; width: fit-content; min-height: 26px; padding: 4px 9px; border-radius: 999px; font-size: 11px; font-weight: 800; letter-spacing: .045em; text-transform: uppercase; white-space: nowrap; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: .8; }
.pill-working { color: var(--good); background: var(--good-soft); }
.pill-waiting { color: var(--warn); background: var(--warn-soft); }
.pill-completed { color: var(--info); background: var(--info-soft); }
.pill-cancelled { color: var(--danger); background: var(--danger-soft); }
.pill-idle { color: var(--muted); background: var(--canvas-strong); }
.pill-review { color: var(--accent-strong); background: var(--accent-soft); }

.action-panel { display: grid; gap: 8px; width: 100%; padding-top: 6px; }
.action-row { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.action-row form { margin: 0; }
.btn {
  appearance: none;
  min-height: 36px;
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-raised);
  color: var(--ink);
  font-weight: 740;
  font-size: 12px;
  cursor: pointer;
  transition: transform .15s ease, border-color .15s ease, background .15s ease;
}
.btn:hover { transform: translateY(-1px); border-color: var(--ink); }
.btn-primary { border-color: var(--ink); background: var(--ink); color: var(--surface); }
.btn-primary:hover { background: #2b302c; }
.btn-danger { color: var(--danger); }
.feedback-disclosure { width: 100%; border-top: 1px dashed var(--line); padding-top: 8px; }
.feedback-disclosure summary { list-style: none; cursor: pointer; color: var(--accent-strong); font-size: 12px; font-weight: 760; text-align: right; }
.feedback-disclosure summary::-webkit-details-marker { display: none; }
.feedback-form { display: grid; gap: 8px; margin-top: 10px; }
.feedback-form textarea {
  width: min(360px, 100%);
  min-height: 86px;
  resize: vertical;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-raised);
  color: var(--ink);
  padding: 10px 11px;
  line-height: 1.45;
}
.feedback-form textarea:focus { outline: 3px solid var(--accent); outline: 3px solid color-mix(in srgb, var(--accent) 22%, transparent); border-color: var(--accent); }

.detail-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(300px, .65fr); gap: var(--space-5); align-items: start; }
.panel { border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--surface); box-shadow: var(--shadow-small); overflow: clip; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); padding: 19px 22px; border-bottom: 1px solid var(--line); }
.panel-head h2 { margin: 0; font-size: 15px; letter-spacing: -0.01em; }
.panel-body { padding: 22px; }
.fact-list { display: grid; gap: 0; margin: 0; }
.fact { display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.fact:last-child { border-bottom: 0; }
.fact dt { color: var(--muted); font-size: 12px; }
.fact dd { margin: 0; overflow-wrap: anywhere; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }

.task-list { display: grid; gap: 10px; }
.task-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 20px; align-items: center; padding: 16px 0; border-bottom: 1px solid var(--line); }
.task-row:last-child { border-bottom: 0; }
.task-row-title { margin: 0; font-size: 15px; font-weight: 720; }
.task-row-title a { text-decoration: none; }
.task-row-title a:hover { color: var(--accent-strong); }
.task-row-meta { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 6px; color: var(--muted); font-size: 12px; }

.search-box { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
.search-input {
  width: 100%;
  min-height: 44px;
  border: 1px solid var(--line-strong);
  border-radius: 11px;
  background: var(--surface-raised);
  color: var(--ink);
  padding: 10px 13px;
}
.search-input:focus { outline: 3px solid var(--accent); outline: 3px solid color-mix(in srgb, var(--accent) 20%, transparent); border-color: var(--accent); }
.search-results { margin-top: 18px; border-top: 1px solid var(--line); }
.search-hit { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; padding: 14px 0; border-bottom: 1px solid var(--line); }
.search-hit-path { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.search-hit-meta { color: var(--muted); font-size: 11px; text-align: right; white-space: nowrap; }

.timeline { position: relative; display: grid; gap: 0; padding-left: 18px; }
.timeline::before { content: ""; position: absolute; top: 9px; bottom: 12px; left: 4px; width: 1px; background: var(--line-strong); }
.timeline-item { position: relative; padding: 0 0 28px 24px; }
.timeline-item::before { content: ""; position: absolute; top: 6px; left: -17px; width: 9px; height: 9px; border-radius: 50%; border: 2px solid var(--surface); background: var(--muted); box-shadow: 0 0 0 1px var(--line-strong); }
.timeline-item[data-kind="operator_feedback"]::before { background: var(--accent); }
.timeline-item[data-kind="accepted"]::before { background: var(--good); }
.timeline-item[data-kind="cancelled"]::before { background: var(--danger); }
.timeline-item[data-kind="checkpoint"]::before { background: var(--info); }
.timeline-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 12px; }
.timeline-title { margin: 0; font-size: 14px; font-weight: 780; }
.timeline-time { color: var(--muted); font-size: 11px; }
.timeline-content { margin-top: 9px; color: var(--muted); line-height: 1.6; }
.timeline-summary { color: var(--ink); }
.feedback-quote { margin: 10px 0 0; padding: 12px 14px; border-left: 3px solid var(--accent); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; background: var(--accent-soft); color: #6e3420; white-space: pre-wrap; }
.path-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.path-chip { max-width: 100%; padding: 4px 7px; border: 1px solid var(--line); border-radius: 6px; background: var(--canvas); color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; overflow-wrap: anywhere; }

.empty-state { padding: 42px 24px; border: 1px dashed var(--line-strong); border-radius: var(--radius-lg); background: color-mix(in srgb, var(--surface) 62%, transparent); text-align: center; }
.empty-state strong { display: block; font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif; font-size: 24px; font-weight: 600; }
.empty-state span { display: block; margin-top: 7px; color: var(--muted); }
.error-note { color: var(--danger); font-weight: 700; }

.skip-link { position: fixed; top: 8px; left: 8px; z-index: 50; transform: translateY(-160%); padding: 8px 10px; border-radius: 8px; background: var(--ink); color: var(--surface); text-decoration: none; }
.skip-link:focus { transform: translateY(0); }
:focus-visible { outline: 3px solid var(--accent); outline: 3px solid color-mix(in srgb, var(--accent) 45%, transparent); outline-offset: 3px; }

@keyframes live-pulse { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(.78); opacity: .65; } }

@media (max-width: 960px) {
  .hero, .detail-grid { grid-template-columns: 1fr; }
  .hero { gap: var(--space-5); padding-top: 40px; }
  .hero-aside { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .workspace-grid { grid-template-columns: 1fr; }
}

@media (max-width: 640px) {
  .shell { width: min(100% - 24px, 1440px); }
  .section-head { align-items: flex-start; flex-direction: column; gap: 8px; }
  .topbar { min-height: 60px; }
  .topbar-meta > .mono { display: none; }
  .hero { padding: 34px 0 28px; }
  .hero h1 { font-size: clamp(38px, 13vw, 58px); }
  .metrics { grid-template-columns: 1fr 1fr; gap: 8px; }
  .metric { min-height: 96px; padding: 14px; }
  .metric-value { margin-top: 10px; font-size: 24px; }
  .workspace-card { grid-template-columns: 1fr; padding: 18px; }
  .card-side { align-items: stretch; min-width: 0; }
  .action-row { justify-content: flex-start; }
  .feedback-disclosure summary { text-align: left; }
  .fact { grid-template-columns: 1fr; gap: 5px; }
  .search-box { grid-template-columns: 1fr; }
  .task-row { grid-template-columns: 1fr; gap: 10px; }
  .search-hit { grid-template-columns: 1fr; gap: 5px; }
  .search-hit-meta { text-align: left; }
}

@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --canvas: #161916;
    --canvas-strong: #20241f;
    --surface: #1c201c;
    --surface-raised: #232823;
    --ink: #f2eee4;
    --muted: #a6ada5;
    --faint: #747c75;
    --line: #343a34;
    --line-strong: #4a514a;
    --accent: #ed875f;
    --accent-strong: #f3a080;
    --accent-soft: #3b241d;
    --good: #7fc29f;
    --good-soft: #1d3428;
    --warn: #d7aa62;
    --warn-soft: #392f1e;
    --danger: #e18c83;
    --danger-soft: #3a2422;
    --info: #8ab5c8;
    --info-soft: #21323a;
    --shadow: 0 18px 50px rgba(0, 0, 0, 0.28);
    --shadow-small: 0 8px 24px rgba(0, 0, 0, 0.2);
  }
  .feedback-quote { color: #f0c5b5; }
  .btn-primary { color: #191c19; background: #f0ebe0; border-color: #f0ebe0; }
  .btn-primary:hover { background: #ffffff; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
""".strip()


DASHBOARD_JS = r"""
(() => {
  const body = document.body;
  const eventsUrl = body.dataset.eventsUrl;
  const indicator = document.querySelector('[data-live-indicator]');
  const copy = document.querySelector('[data-live-copy]');
  const refreshButton = document.querySelector('[data-refresh-now]');
  if (!eventsUrl || !indicator || !copy || !refreshButton || !('EventSource' in window)) {
    return;
  }

  const hasUnsavedInput = () => Array.from(
    document.querySelectorAll('textarea, input[type="search"]')
  ).some((field) => field.value !== field.defaultValue);

  const setState = (state, text) => {
    indicator.dataset.state = state;
    copy.textContent = text;
  };

  refreshButton.addEventListener('click', () => window.location.reload());

  const source = new EventSource(eventsUrl);
  source.addEventListener('ready', () => setState('live', 'Live'));
  source.addEventListener('refresh', () => {
    if (hasUnsavedInput()) {
      setState('update', 'Update available');
      return;
    }
    setState('update', 'Refreshing');
    window.setTimeout(() => window.location.reload(), 160);
  });
  source.onerror = () => setState('reconnecting', 'Reconnecting');
  window.addEventListener('pagehide', () => source.close(), { once: true });
})();
""".strip()
