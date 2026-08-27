from __future__ import annotations

DASHBOARD_CSS = r"""
:root {
  color-scheme: dark;
  --canvas: #090b10;
  --canvas-elevated: #0e1118;
  --surface: rgba(18, 22, 31, 0.82);
  --surface-solid: #12161f;
  --surface-raised: #171c27;
  --surface-hover: #1c2230;
  --ink: #f5f7fb;
  --muted: #949dad;
  --faint: #626b7a;
  --line: rgba(149, 161, 181, 0.16);
  --line-strong: rgba(166, 179, 200, 0.3);
  --accent: #8b7cff;
  --accent-strong: #b7adff;
  --accent-soft: rgba(139, 124, 255, 0.14);
  --cyan: #4bd7e8;
  --cyan-soft: rgba(75, 215, 232, 0.12);
  --good: #69dda9;
  --good-soft: rgba(105, 221, 169, 0.12);
  --warn: #f0bd68;
  --warn-soft: rgba(240, 189, 104, 0.12);
  --danger: #ff827c;
  --danger-soft: rgba(255, 130, 124, 0.12);
  --info: #74b8ff;
  --info-soft: rgba(116, 184, 255, 0.12);
  --shadow: 0 24px 70px rgba(0, 0, 0, 0.34);
  --shadow-small: 0 12px 34px rgba(0, 0, 0, 0.22);
  --radius-sm: 9px;
  --radius-md: 14px;
  --radius-lg: 20px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  --space-7: 48px;
  --space-8: 64px;
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  background: var(--canvas);
  color: var(--ink);
}

* { box-sizing: border-box; }
html { min-width: 320px; background: var(--canvas); }
body {
  margin: 0;
  min-height: 100vh;
  background:
    radial-gradient(circle at 12% -8%, rgba(139, 124, 255, 0.2), transparent 30rem),
    radial-gradient(circle at 88% 4%, rgba(75, 215, 232, 0.09), transparent 28rem),
    linear-gradient(rgba(255, 255, 255, 0.018) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.018) 1px, transparent 1px),
    var(--canvas);
  background-size: auto, auto, 48px 48px, 48px 48px, auto;
  color: var(--ink);
}
a { color: inherit; }
button, input, textarea { font: inherit; }
button, a, summary { -webkit-tap-highlight-color: transparent; }
::selection { background: var(--accent-soft); color: var(--ink); }

.shell { width: min(1500px, calc(100% - 48px)); margin: 0 auto; padding-bottom: var(--space-8); }
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-height: 64px;
  background: rgba(9, 11, 16, 0.76);
  border-bottom: 1px solid var(--line);
  backdrop-filter: blur(20px) saturate(135%);
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 11px;
  text-decoration: none;
  font-weight: 760;
  letter-spacing: -0.025em;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 31px;
  height: 31px;
  border: 1px solid rgba(183, 173, 255, 0.44);
  border-radius: 9px;
  background: linear-gradient(145deg, rgba(139, 124, 255, 0.3), rgba(75, 215, 232, 0.08));
  color: var(--ink);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.08), 0 0 24px rgba(139, 124, 255, 0.16);
}
.topbar-meta { display: flex; align-items: center; gap: var(--space-3); color: var(--muted); font-size: 12px; }
.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: rgba(18, 22, 31, 0.68);
  white-space: nowrap;
}
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 4px var(--good-soft); }
.live-indicator[data-state="reconnecting"] .live-dot { background: var(--warn); box-shadow: 0 0 0 4px var(--warn-soft); }
.live-indicator[data-state="update"] .live-dot { background: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); animation: live-pulse 1.5s ease-in-out infinite; }
.live-copy { color: var(--muted); }
.update-link { display: none; border: 0; background: transparent; color: var(--accent-strong); padding: 0; font-weight: 720; cursor: pointer; }
.live-indicator[data-state="update"] .update-link { display: inline; }

.breadcrumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; padding: 18px 0 0; color: var(--faint); font-size: 12px; }
.breadcrumbs a { text-decoration: none; color: var(--muted); }
.breadcrumbs a:hover { color: var(--ink); }
.breadcrumbs .sep { color: var(--faint); }

.hero {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(260px, .8fr);
  gap: var(--space-6);
  align-items: end;
  padding: 44px 0 28px;
}
.hero.compact { padding-top: 34px; }
.eyebrow { margin: 0 0 10px; color: var(--accent-strong); font-size: 11px; font-weight: 780; letter-spacing: .13em; text-transform: uppercase; }
.hero h1, .section-title, .task-title {
  font-family: inherit;
  font-weight: 700;
  letter-spacing: -0.035em;
}
.hero h1 {
  margin: 0;
  max-width: min(38ch, 100%);
  font-size: clamp(30px, 3vw, 42px);
  line-height: 1.06;
  text-wrap: balance;
  overflow-wrap: anywhere;
}
.hero.compact h1 { font-size: clamp(28px, 2.7vw, 38px); }
.hero h1.task-title { max-width: none; font-size: clamp(25px, 2.35vw, 34px); line-height: 1.12; text-wrap: pretty; }
.hero-copy { margin: 13px 0 0; max-width: 820px; color: var(--muted); font-size: 15px; line-height: 1.65; }
.hero-aside { display: grid; gap: 10px; align-content: end; justify-items: start; }
.identity-line { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; line-height: 1.7; color: var(--muted); }

.control-brief {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 0 0 18px;
}
.brief-card {
  position: relative;
  min-height: 142px;
  overflow: hidden;
  padding: 18px 19px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: linear-gradient(145deg, rgba(23, 28, 39, 0.9), rgba(14, 17, 24, 0.82));
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.04);
}
.brief-card::after {
  content: "";
  position: absolute;
  inset: auto -45px -70px auto;
  width: 150px;
  height: 150px;
  border-radius: 50%;
  background: var(--accent-soft);
  filter: blur(2px);
  pointer-events: none;
}
.brief-card[data-tone="knowledge"]::after { background: var(--cyan-soft); }
.brief-card[data-tone="workflow"]::after { background: var(--info-soft); }
.brief-kicker { display: block; margin-bottom: 22px; color: var(--faint); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; letter-spacing: .11em; text-transform: uppercase; }
.brief-title { position: relative; z-index: 1; margin: 0; font-size: 14px; font-weight: 760; letter-spacing: -0.01em; }
.brief-copy { position: relative; z-index: 1; margin: 7px 0 0; max-width: 46ch; color: var(--muted); font-size: 12px; line-height: 1.55; }

.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: var(--space-7); }
.metric {
  min-height: 108px;
  padding: 17px 18px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: rgba(14, 17, 24, 0.7);
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.03);
}
.metric-label { color: var(--faint); font-size: 10px; font-weight: 760; letter-spacing: .1em; text-transform: uppercase; }
.metric-value { display: block; margin-top: 14px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 29px; font-weight: 650; letter-spacing: -0.045em; }
.metric:nth-child(2) .metric-value { color: var(--good); }
.metric:nth-child(3) .metric-value { color: var(--warn); }
.metric:nth-child(4) .metric-value { color: var(--cyan); }

.section { margin-top: var(--space-7); }
.section-head { display: flex; align-items: end; justify-content: space-between; gap: var(--space-4); margin-bottom: var(--space-4); }
.section-title { margin: 0; font-size: 19px; line-height: 1.25; }
.section-note { margin: 0; color: var(--muted); font-size: 12px; }

.workspace-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.workspace-card {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(150px, 0.34fr);
  gap: var(--space-5);
  min-height: 278px;
  padding: 22px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: linear-gradient(145deg, rgba(18, 22, 31, 0.94), rgba(13, 16, 23, 0.9));
  box-shadow: var(--shadow-small), inset 0 1px rgba(255, 255, 255, 0.035);
  transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease, background .18s ease;
}
.workspace-card::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 2px;
  border-radius: var(--radius-lg) 0 0 var(--radius-lg);
  background: linear-gradient(var(--accent), transparent 55%);
  opacity: .7;
}
.workspace-card:hover { transform: translateY(-2px); border-color: var(--line-strong); background: linear-gradient(145deg, rgba(23, 28, 40, 0.97), rgba(15, 18, 26, 0.94)); box-shadow: var(--shadow); }
.card-main { min-width: 0; }
.card-kicker { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 20px; }
.project-link { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; text-decoration: none; }
.project-link:hover { color: var(--accent-strong); }
.project-chip {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 4px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.025);
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 10px;
  text-decoration: none;
}
.project-chip:hover { border-color: rgba(139, 124, 255, 0.46); color: var(--accent-strong); }
.workspace-name { margin: 0; font-size: 19px; font-weight: 730; letter-spacing: -0.025em; line-height: 1.28; }
.workspace-name a { text-decoration: none; }
.workspace-name a:hover { color: var(--accent-strong); }
.workspace-path { margin: 7px 0 0; color: var(--faint); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.task-focus { margin-top: 24px; padding-top: 17px; border-top: 1px solid var(--line); }
.task-focus-label { margin-bottom: 7px; color: var(--faint); font-size: 10px; font-weight: 780; letter-spacing: .1em; text-transform: uppercase; }
.task-focus-title { margin: 0; font-size: 16px; font-weight: 720; line-height: 1.4; }
.task-focus-title a { text-decoration: none; }
.task-focus-title a:hover { color: var(--accent-strong); }
.task-git-branch { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin: 8px 0 0; color: var(--muted); font-size: 11px; }
.task-git-branch strong { color: var(--ink); font-weight: 650; overflow-wrap: anywhere; }
.next-step { margin: 10px 0 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
.context-rail { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; color: var(--faint); font-size: 11px; line-height: 1.45; }
.context-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--cyan); box-shadow: 0 0 0 3px var(--cyan-soft); }
.card-side { display: flex; flex-direction: column; align-items: stretch; gap: var(--space-3); min-width: 150px; padding-left: 18px; border-left: 1px solid var(--line); }
.mini-stats { display: grid; gap: 0; width: 100%; }
.mini-stat { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); color: var(--faint); font-size: 10px; }
.mini-stat:last-child { border-bottom: 0; }
.mini-stat strong { max-width: 150px; color: var(--ink); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-weight: 650; overflow-wrap: anywhere; text-align: right; }

.pill { display: inline-flex; align-items: center; gap: 7px; width: fit-content; min-height: 26px; padding: 4px 9px; border: 1px solid transparent; border-radius: 999px; font-size: 10px; font-weight: 780; letter-spacing: .055em; text-transform: uppercase; white-space: nowrap; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 3px currentColor; opacity: .72; }
.pill-working { color: var(--good); background: var(--good-soft); border-color: rgba(105, 221, 169, 0.14); }
.pill-waiting { color: var(--warn); background: var(--warn-soft); border-color: rgba(240, 189, 104, 0.14); }
.pill-completed { color: var(--info); background: var(--info-soft); border-color: rgba(116, 184, 255, 0.14); }
.pill-cancelled { color: var(--danger); background: var(--danger-soft); border-color: rgba(255, 130, 124, 0.14); }
.pill-idle { color: var(--muted); background: rgba(255, 255, 255, 0.04); border-color: var(--line); }
.pill-review { color: var(--accent-strong); background: var(--accent-soft); border-color: rgba(139, 124, 255, 0.2); }

.action-panel { display: grid; gap: 8px; width: 100%; padding-top: 6px; }
.visibility-box { display: grid; gap: 8px; width: 100%; }
.visibility-hint { margin: 0; color: var(--muted); font-size: 11px; line-height: 1.5; text-align: left; }
.visibility-form { margin: 0; width: 100%; }
.visibility-form .btn { width: 100%; }
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
  font-weight: 720;
  font-size: 11px;
  cursor: pointer;
  transition: transform .15s ease, border-color .15s ease, background .15s ease, box-shadow .15s ease;
}
.btn:hover { transform: translateY(-1px); border-color: rgba(183, 173, 255, 0.55); background: var(--surface-hover); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18); }
.btn-primary { border-color: rgba(139, 124, 255, 0.62); background: linear-gradient(135deg, #7667ee, #6255cf); color: #fff; }
.btn-primary:hover { background: linear-gradient(135deg, #8b7cff, #7062e7); }
.btn-danger { color: var(--danger); }
.feedback-disclosure { width: 100%; border-top: 1px dashed var(--line-strong); padding-top: 8px; }
.feedback-disclosure summary { list-style: none; cursor: pointer; color: var(--accent-strong); font-size: 11px; font-weight: 740; text-align: right; }
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
.feedback-form textarea:focus { outline: 3px solid rgba(139, 124, 255, 0.22); border-color: var(--accent); }

.detail-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(300px, .65fr); gap: var(--space-5); align-items: start; }
.panel { border: 1px solid var(--line); border-radius: var(--radius-lg); background: rgba(18, 22, 31, 0.9); box-shadow: var(--shadow-small); overflow: clip; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); padding: 18px 21px; border-bottom: 1px solid var(--line); background: rgba(255, 255, 255, 0.012); }
.panel-head h2 { margin: 0; font-size: 14px; letter-spacing: -0.01em; }
.panel-body { padding: 21px; }
.fact-list { display: grid; gap: 0; margin: 0; }
.fact { display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.fact:last-child { border-bottom: 0; }
.fact dt { color: var(--faint); font-size: 11px; }
.fact dd { margin: 0; overflow-wrap: anywhere; font-size: 13px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; }

.task-list { display: grid; gap: 0; }
.task-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 20px; align-items: center; padding: 15px 0; border-bottom: 1px solid var(--line); }
.task-row:last-child { border-bottom: 0; }
.task-row-title { margin: 0; font-size: 14px; font-weight: 720; }
.task-row-title a { text-decoration: none; }
.task-row-title a:hover { color: var(--accent-strong); }
.task-row-meta { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 6px; color: var(--faint); font-size: 11px; }

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
.search-input::placeholder { color: var(--faint); }
.search-input:focus { outline: 3px solid rgba(139, 124, 255, 0.2); border-color: var(--accent); }
.search-results { margin-top: 18px; border-top: 1px solid var(--line); }
.search-hit { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; padding: 14px 0; border-bottom: 1px solid var(--line); }
.search-hit-path { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; }
.search-hit-meta { color: var(--faint); font-size: 10px; text-align: right; white-space: nowrap; }

.timeline { position: relative; display: grid; gap: 0; padding-left: 18px; }
.timeline::before { content: ""; position: absolute; top: 9px; bottom: 12px; left: 4px; width: 1px; background: var(--line-strong); }
.timeline-item { position: relative; padding: 0 0 28px 24px; }
.timeline-item::before { content: ""; position: absolute; top: 6px; left: -17px; width: 9px; height: 9px; border-radius: 50%; border: 2px solid var(--surface-solid); background: var(--muted); box-shadow: 0 0 0 1px var(--line-strong); }
.timeline-item[data-kind="operator_feedback"]::before { background: var(--accent); }
.timeline-item[data-kind="accepted"]::before { background: var(--good); }
.timeline-item[data-kind="cancelled"]::before { background: var(--danger); }
.timeline-item[data-kind="checkpoint"]::before { background: var(--info); }
.timeline-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 12px; }
.timeline-title { margin: 0; font-size: 13px; font-weight: 760; }
.timeline-time { color: var(--faint); font-size: 10px; }
.timeline-content { margin-top: 9px; color: var(--muted); font-size: 13px; line-height: 1.6; }
.timeline-summary { color: var(--ink); }
.timeline-branch { margin-bottom: 6px; }
.timeline-branch strong { margin-right: 8px; }
.feedback-quote { margin: 10px 0 0; padding: 12px 14px; border-left: 3px solid var(--accent); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; background: var(--accent-soft); color: var(--accent-strong); white-space: pre-wrap; }
.path-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.path-chip { max-width: 100%; padding: 4px 7px; border: 1px solid var(--line); border-radius: 6px; background: rgba(255, 255, 255, 0.025); color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; overflow-wrap: anywhere; }

.empty-state { padding: 42px 24px; border: 1px dashed var(--line-strong); border-radius: var(--radius-lg); background: rgba(18, 22, 31, 0.55); text-align: center; }
.empty-state strong { display: block; font-size: 17px; font-weight: 700; line-height: 1.3; }
.empty-state span { display: block; margin-top: 7px; color: var(--muted); font-size: 13px; }
.error-note { color: var(--danger); font-weight: 700; }

.skip-link { position: fixed; top: 8px; left: 8px; z-index: 50; transform: translateY(-160%); padding: 8px 10px; border-radius: 8px; background: var(--ink); color: var(--canvas); text-decoration: none; }
.skip-link:focus { transform: translateY(0); }
:focus-visible { outline: 3px solid rgba(139, 124, 255, 0.45); outline-offset: 3px; }

@keyframes live-pulse { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(.78); opacity: .65; } }

@media (max-width: 1050px) {
  .hero, .detail-grid { grid-template-columns: 1fr; }
  .hero { gap: var(--space-5); padding-top: 28px; }
  .hero-aside { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .workspace-grid { grid-template-columns: 1fr; }
}

@media (max-width: 820px) {
  .control-brief { grid-template-columns: 1fr; }
  .brief-card { min-height: 120px; }
  .brief-kicker { margin-bottom: 14px; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 640px) {
  .shell { width: min(100% - 24px, 1500px); }
  .section-head { align-items: flex-start; flex-direction: column; gap: 8px; }
  .topbar { min-height: 58px; }
  .topbar-meta > .mono { display: none; }
  .live-indicator { padding-inline: 8px; }
  .hero { padding: 24px 0 18px; }
  .hero h1 { font-size: clamp(27px, 8vw, 34px); }
  .hero h1.task-title { font-size: clamp(23px, 6.5vw, 30px); }
  .metrics { grid-template-columns: 1fr 1fr; gap: 8px; }
  .metric { min-height: 92px; padding: 14px; }
  .metric-value { margin-top: 10px; font-size: 24px; }
  .workspace-card { grid-template-columns: 1fr; padding: 18px; }
  .card-side { min-width: 0; padding: 16px 0 0; border-left: 0; border-top: 1px solid var(--line); }
  .mini-stat strong { max-width: 55vw; }
  .action-row { justify-content: flex-start; }
  .feedback-disclosure summary { text-align: left; }
  .fact { grid-template-columns: 1fr; gap: 5px; }
  .search-box { grid-template-columns: 1fr; }
  .task-row { grid-template-columns: 1fr; gap: 10px; }
  .search-hit { grid-template-columns: 1fr; gap: 5px; }
  .search-hit-meta { text-align: left; }
}

@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --canvas: #f5f7fb;
    --canvas-elevated: #eef1f7;
    --surface: rgba(255, 255, 255, 0.84);
    --surface-solid: #ffffff;
    --surface-raised: #ffffff;
    --surface-hover: #f6f7fb;
    --ink: #151925;
    --muted: #60697a;
    --faint: #8a93a3;
    --line: rgba(46, 55, 74, 0.12);
    --line-strong: rgba(46, 55, 74, 0.22);
    --accent: #6758db;
    --accent-strong: #5143c1;
    --accent-soft: rgba(103, 88, 219, 0.1);
    --cyan: #138b9d;
    --cyan-soft: rgba(19, 139, 157, 0.1);
    --good: #16875d;
    --good-soft: rgba(22, 135, 93, 0.1);
    --warn: #a6680e;
    --warn-soft: rgba(166, 104, 14, 0.1);
    --danger: #bf4b45;
    --danger-soft: rgba(191, 75, 69, 0.1);
    --info: #3277bd;
    --info-soft: rgba(50, 119, 189, 0.1);
    --shadow: 0 24px 70px rgba(36, 43, 59, 0.12);
    --shadow-small: 0 10px 28px rgba(36, 43, 59, 0.08);
  }
  body {
    background:
      radial-gradient(circle at 12% -8%, rgba(103, 88, 219, 0.12), transparent 30rem),
      radial-gradient(circle at 88% 4%, rgba(19, 139, 157, 0.07), transparent 28rem),
      linear-gradient(rgba(46, 55, 74, 0.025) 1px, transparent 1px),
      linear-gradient(90deg, rgba(46, 55, 74, 0.025) 1px, transparent 1px),
      var(--canvas);
    background-size: auto, auto, 48px 48px, 48px 48px, auto;
  }
  .topbar { background: rgba(245, 247, 251, 0.8); }
  .brief-card, .workspace-card { background: rgba(255, 255, 255, 0.82); }
  .workspace-card:hover { background: #fff; }
  .metric { background: rgba(255, 255, 255, 0.68); }
  .panel { background: rgba(255, 255, 255, 0.9); }
  .btn-primary { background: linear-gradient(135deg, #6758db, #5547c7); }
  .btn-primary:hover { background: linear-gradient(135deg, #7465e8, #6253d5); }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
""".strip()


DASHBOARD_JS = r"""
(() => {
  const body = document.body;
  const eventsUrl = body.dataset.eventsUrl;

  const enhanceProjectsHome = () => {
    const main = document.querySelector('main');
    const hero = main?.querySelector(':scope > .hero');
    const heading = hero?.querySelector('h1');
    if (!main || !hero || !heading || heading.textContent.trim() !== 'Проекты') {
      return;
    }

    body.dataset.homeEnhanced = 'true';

    const brief = document.createElement('section');
    brief.className = 'control-brief';
    brief.setAttribute('aria-label', 'Как устроен Harness');
    const cards = [
      ['runtime', 'LOCAL CONTROL PLANE', 'Локальный контур', 'Daemon и SQLite держат авторитетное состояние. Dashboard и MCP работают с одной моделью проекта.'],
      ['knowledge', 'PROJECT KNOWLEDGE', 'База знаний', 'Смысловые карточки сохраняют provenance, anchors и freshness; изменившиеся якоря требуют revalidation.'],
      ['workflow', 'AGENT WORKFLOW', 'Контур агента', 'Project-scoped MCP связывает контекст, задачи, checkpoint и операторское ревью без отдельной теневой модели.'],
    ];
    cards.forEach(([tone, kicker, title, copy]) => {
      const card = document.createElement('article');
      card.className = 'brief-card';
      card.dataset.tone = tone;
      const kickerNode = document.createElement('span');
      kickerNode.className = 'brief-kicker';
      kickerNode.textContent = kicker;
      const titleNode = document.createElement('h2');
      titleNode.className = 'brief-title';
      titleNode.textContent = title;
      const copyNode = document.createElement('p');
      copyNode.className = 'brief-copy';
      copyNode.textContent = copy;
      card.append(kickerNode, titleNode, copyNode);
      brief.append(card);
    });
    hero.insertAdjacentElement('afterend', brief);

    document.querySelectorAll('.workspace-card').forEach((card) => {
      const projectInput = card.querySelector('input[name="project_id"]');
      const kicker = card.querySelector('.card-kicker');
      if (projectInput instanceof HTMLInputElement && kicker) {
        const projectId = projectInput.value;
        const projectLink = document.createElement('a');
        projectLink.className = 'project-chip';
        projectLink.href = `${location.pathname}projects/${encodeURIComponent(projectId)}/`;
        projectLink.textContent = `project ${projectId.slice(0, 8)}`;
        projectLink.title = projectId;
        kicker.append(projectLink);
      }

      const taskFocus = card.querySelector('.task-focus');
      const stats = Array.from(card.querySelectorAll('.mini-stat'));
      const indexStat = stats.find((item) => item.querySelector('span')?.textContent.trim() === 'Индекс');
      const indexValue = indexStat?.querySelector('strong')?.textContent.trim();
      if (taskFocus && indexValue) {
        const rail = document.createElement('div');
        rail.className = 'context-rail';
        const dot = document.createElement('span');
        dot.className = 'context-dot';
        dot.setAttribute('aria-hidden', 'true');
        const copy = document.createElement('span');
        copy.textContent = `Контекст проекта: ${indexValue} файлов в индексе · Knowledge хранится на уровне проекта`;
        rail.append(dot, copy);
        taskFocus.insertAdjacentElement('afterend', rail);
      }
    });
  };

  enhanceProjectsHome();

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
  source.addEventListener('ready', () => setState('live', 'Онлайн'));
  source.addEventListener('refresh', () => {
    if (hasUnsavedInput()) {
      setState('update', 'Есть обновление');
      return;
    }
    setState('update', 'Обновление');
    window.setTimeout(() => window.location.reload(), 160);
  });
  source.onerror = () => setState('reconnecting', 'Переподключение');
  window.addEventListener('pagehide', () => source.close(), { once: true });
})();
""".strip()
