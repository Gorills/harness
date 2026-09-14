from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from harness.dashboard_assets import DASHBOARD_JS

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required to execute dashboard JS")

# Execute the production script through its event listeners. This deliberately small DOM double
# models form state and subtree replacement; browser rendering and HTML parsing are separate proof.
_BROWSER_PROBE = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { source, scenario } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

class HTMLElement {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = tag.toUpperCase();
    this.attrs = attrs;
    this.children = children;
    this.parentElement = null;
    this.ownerDocument = null;
    this.dataset = {};
    this.listeners = new Map();
    this.textContent = '';
    this.scrollTop = 0;
    for (const [name, value] of Object.entries(attrs)) {
      if (name.startsWith('data-')) {
        this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
      }
    }
    for (const child of children) child.parentElement = this;
  }
  get id() { return this.attrs.id || ''; }
  get name() { return this.attrs.name || ''; }
  get className() { return this.attrs.class || ''; }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(value) { this._scrollTop = this.ownerDocument?.active ? value : 0; }
  get form() { return this.closest('form'); }
  get elements() {
    const fields = this.querySelectorAll('input, textarea, select');
    fields.namedItem = (name) => fields.find((field) => field.name === name) || null;
    return fields;
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  fire(name) { this.listeners.get(name)?.({ target: this }); }
  matches(selector) {
    const excluded = selector.match(/:not\(([^)]+)\)/);
    if (excluded) {
      if (this.matches(excluded[1])) return false;
      selector = selector.replace(excluded[0], '');
    }
    const tag = selector.match(/^[a-z]+/i)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const className = selector.match(/\.([\w-]+)/)?.[1];
    if (className && !this.className.split(' ').includes(className)) return false;
    for (const [, name, value] of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
      if (!(name in this.attrs)) return false;
      if (value !== undefined && this.attrs[name] !== value) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const all = this.children.flatMap((child) => [child, ...child.descendants()]);
    if (selector === '.mobile-navigation a') return [];
    return all.filter((item) => selector.split(',').some((part) => item.matches(part.trim())));
  }
  descendants() { return this.children.flatMap((child) => [child, ...child.descendants()]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) {
      if (node.matches(selector)) return node;
    }
    return null;
  }
  focus() { this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end, direction) {
    this.selectionStart = start;
    this.selectionEnd = end;
    this.selectionDirection = direction;
  }
  replaceWith(next) {
    const parent = this.parentElement;
    const document = this.ownerDocument;
    if (this === document.activeElement || this.descendants().includes(document.activeElement)) {
      document.activeElement = document.body;
    }
    parent.children[parent.children.indexOf(this)] = next;
    next.parentElement = parent;
    document.adopt(next);
    document.replacements += 1;
  }
}
class HTMLInputElement extends HTMLElement {
  constructor(name, type = 'text', value = '') {
    super('input', { name, type });
    this.type = type;
    this.value = this.defaultValue = value;
    this.checked = this.defaultChecked = false;
    this.selectionStart = this.selectionEnd = 0;
    this.selectionDirection = 'none';
  }
}
class HTMLTextAreaElement extends HTMLElement {
  constructor(name, value = '') {
    super('textarea', { name });
    this.type = 'textarea';
    this.value = this.defaultValue = value;
    this.selectionStart = this.selectionEnd = 0;
    this.selectionDirection = 'none';
  }
}
class HTMLSelectElement extends HTMLElement {
  constructor(name) {
    super('select', { name });
    this.type = 'select-one';
    this.options = ['first', 'second'].map((value, index) => ({
      value, selected: index === 0, defaultSelected: index === 0,
    }));
  }
  get value() { return this.options.find((option) => option.selected)?.value || ''; }
  set value(value) { for (const option of this.options) option.selected = option.value === value; }
}
class HTMLDetailsElement extends HTMLElement {
  constructor(children) {
    super('details', { id: 'feedback-disclosure' }, children);
    this.open = false;
  }
}
class Document {
  constructor({ revision = '7', action = 'feedback', task = 'task-a', method = 'post', active = false } = {}) {
    this.active = active;
    this.feedback = new HTMLTextAreaElement('feedback');
    this.checkbox = new HTMLInputElement('confirmed', 'checkbox', 'yes');
    this.radio = new HTMLInputElement('mode', 'radio', 'manual');
    this.select = new HTMLSelectElement('status');
    this.search = new HTMLInputElement('q', 'search', 'original query');
    this.revision = new HTMLInputElement('expected_revision', 'hidden', revision);
    const fields = method === 'get' ? [this.search] : [
      new HTMLInputElement('action', 'hidden', action),
      new HTMLInputElement('task_id', 'hidden', task),
      new HTMLInputElement('workspace_id', 'hidden', 'workspace-a'),
      this.revision, this.feedback, this.checkbox, this.radio, this.select,
    ];
    this.form = new HTMLElement('form', { method, action: '/cap/workspaces/workspace-a/' }, fields);
    const summary = new HTMLElement('summary');
    summary.textContent = 'Feedback';
    this.details = new HTMLDetailsElement([summary, this.form]);
    this.button = new HTMLElement('button', { 'data-refresh-now': '' });
    this.copy = new HTMLElement('span', { class: 'live-copy' });
    this.indicator = new HTMLElement('span', { class: 'live-indicator' }, [this.copy]);
    this.sidebar = new HTMLElement('aside', { class: 'app-sidebar' });
    this.layout = new HTMLElement('main', { class: 'app-layout' }, [
      this.sidebar, this.details, this.button, this.indicator,
    ]);
    this.body = new HTMLElement('body', { 'data-events-url': '/cap/events?snapshot=old' }, [
      this.layout,
    ]);
    this.title = 'Dashboard';
    this.activeElement = this.body;
    this.replacements = 0;
    this.adopt(this.body);
  }
  adopt(element) {
    element.ownerDocument = this;
    for (const child of element.children) this.adopt(child);
  }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
  querySelector(selector) { return this.body.querySelector(selector); }
  addEventListener() {}
}

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const tick = () => new Promise(setImmediate);

async function probe() {
  const getForm = scenario === 'force-get';
  const document = new Document({ method: getForm ? 'get' : 'post', active: true });
  const fresh = new Document({ revision: '9', method: getForm ? 'get' : 'post' });
  document.sidebar.scrollTop = 37;
  const requests = [];
  const sources = [];
  const scrolls = [];
  class EventSource extends HTMLElement {
    constructor() { super('event-source'); sources.push(this); }
    close() {}
  }
  class DOMParser { parseFromString() { return fresh; } }
  const window = {
    EventSource, location: { pathname: '/cap/workspaces/workspace-a/', search: '?q=original&page=2' },
    scrollX: 17, scrollY: 99, scrollTo: (...args) => scrolls.push(args), addEventListener() {},
  };
  vm.runInNewContext(source, {
    document, window, EventSource, DOMParser, HTMLElement, HTMLInputElement,
    HTMLTextAreaElement, HTMLSelectElement, HTMLDetailsElement,
    fetch: (url, options) => {
      const response = deferred();
      const body = deferred();
      requests.push({ response, body, url, options });
      return response.promise;
    },
  }, { timeout: 1000 });
  const beginResponse = async () => {
    assert.equal(requests.length, 1, 'refresh must issue one request');
    requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
    await tick();
  };
  const finishResponse = async () => {
    requests[0].body.resolve('<fresh page>');
    await tick();
  };

  if (scenario === 'clean-auto-refresh') {
    sources[0].fire('refresh');
    await beginResponse();
    await finishResponse();
    assert.equal(document.replacements, 1, 'clean page must still accept automatic updates');
    assert.equal(fresh.revision.value, '9');
    assert.equal(fresh.indicator.dataset.state, 'live');
    assert.equal(fresh.sidebar.scrollTop, 37);
    return;
  }

  if (scenario.startsWith('dirty-before-')) {
    const field = scenario.slice('dirty-before-'.length);
    if (field === 'feedback') document.feedback.value = 'Unsaved feedback';
    if (field === 'checkbox') document.checkbox.checked = true;
    if (field === 'radio') document.radio.checked = true;
    if (field === 'select') document.select.value = 'second';
    if (field === 'recovered') {
      document.feedback.value = document.feedback.defaultValue = 'Recovered failed submission';
      document.feedback.dataset.recoveredDraft = 'true';
    }
    sources[0].fire('refresh');
    await tick();
    assert.equal(requests.length, 0, `${field} edit must prevent automatic fetch`);
    assert.equal(document.replacements, 0);
    assert.equal(document.indicator.dataset.state, 'update');
    return;
  }

  if (scenario === 'dirty-during-text') {
    sources[0].fire('refresh');
    await beginResponse();
    document.feedback.value = 'Typed while response text was pending';
    await finishResponse();
    assert.equal(document.replacements, 0, 'late edit must prevent automatic replacement');
    assert.equal(document.feedback.value, 'Typed while response text was pending');
    assert.equal(document.indicator.dataset.state, 'update');
    return;
  }

  const draft = getForm ? document.search : document.feedback;
  draft.value = 'Initial draft';
  draft.focus();
  document.checkbox.checked = true;
  document.select.value = 'second';
  document.button.fire('click');
  await beginResponse();
  document.details.open = true;
  draft.value = 'Последний черновик';
  draft.setSelectionRange(2, 8, 'backward');
  draft.scrollTop = 23;
  if (scenario === 'missing-form') fresh.details.children = [];
  if (scenario === 'different-action') fresh.form.elements.namedItem('action').value = 'comment';
  if (scenario === 'different-task') fresh.form.elements.namedItem('task_id').value = 'task-b';
  if (scenario === 'missing-option') fresh.select.options = fresh.select.options.slice(0, 1);
  if (scenario === 'ambiguous-control') {
    const duplicate = new HTMLTextAreaElement('feedback');
    duplicate.parentElement = fresh.form;
    fresh.form.children.push(duplicate);
    fresh.adopt(duplicate);
  }
  await finishResponse();

  if (['missing-form', 'different-action', 'different-task', 'missing-option', 'ambiguous-control'].includes(scenario)) {
    assert.equal(document.replacements, 0, 'unmatched draft must keep its original layout');
    assert.equal(document.activeElement, draft);
    assert.equal(draft.value, 'Последний черновик');
    assert.equal(document.indicator.dataset.state, 'update');
    assert.equal(document.copy.textContent, 'Форма изменилась · черновик сохранён');
    return;
  }

  assert.equal(document.replacements, 1);
  const restored = getForm ? fresh.search : fresh.feedback;
  assert.equal(restored.value, 'Последний черновик', 'explicit refresh must keep the latest edit');
  assert.equal(document.activeElement, restored, 'focus must follow the restored draft');
  assert.equal(restored.selectionStart, 2);
  assert.equal(restored.selectionEnd, 8);
  assert.equal(restored.selectionDirection, 'backward');
  assert.equal(restored.scrollTop, 23);
  assert.equal(fresh.details.open, true);
  assert.equal(fresh.sidebar.scrollTop, 37);
  assert.deepEqual(scrolls, [[17, 99]]);
  assert.equal(requests[0].url, '/cap/workspaces/workspace-a/?q=original&page=2');
  if (!getForm) {
    assert.equal(fresh.checkbox.checked, true);
    assert.equal(fresh.select.value, 'second');
    assert.equal(fresh.revision.value, '9', 'hidden CAS revision must remain authoritative');
  }
  sources.at(-1).fire('refresh');
  await tick();
  assert.equal(requests.length, 1, 'restored draft must still block automatic replacement');
}
probe().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    (
        "clean-auto-refresh",
        "dirty-before-feedback",
        "dirty-before-checkbox",
        "dirty-before-radio",
        "dirty-before-select",
        "dirty-before-recovered",
        "dirty-during-text",
        "force-post",
        "force-get",
        "missing-form",
        "different-action",
        "different-task",
        "missing-option",
        "ambiguous-control",
    ),
)
def test_dashboard_javascript_retains_drafts_across_refresh(scenario: str) -> None:
    assert _NODE is not None
    result = subprocess.run(
        [_NODE, "-e", _BROWSER_PROBE],
        input=json.dumps({"source": DASHBOARD_JS, "scenario": scenario}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"Node probe {scenario} failed:\n{result.stdout}{result.stderr}")
