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
const { source, scenario, renderedOptions } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

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
  get href() { return this.attrs.href || ''; }
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
  hasAttribute(name) { return name in this.attrs; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  fire(name, event = {}) { this.listeners.get(name)?.({ target: this, ...event }); }
  matches(selector) {
    if (selector.includes(',')) return selector.split(',').some((part) => this.matches(part.trim()));
    const excluded = selector.match(/:not\(([^)]+)\)/);
    if (excluded) {
      if (this.matches(excluded[1])) return false;
      selector = selector.replace(excluded[0], '');
    }
    const tag = selector.match(/^[a-z][a-z0-9]*/i)?.[0];
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
  contains(node) { return node === this || this.descendants().includes(node); }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
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
    // Native adoption detaches the layout from the parsed response document.
    if (next.parentElement !== null) {
      const oldParent = next.parentElement;
      oldParent.children.splice(oldParent.children.indexOf(next), 1);
    }
    parent.children[parent.children.indexOf(this)] = next;
    this.parentElement = null;
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
  constructor(children, id = 'feedback-disclosure') {
    super('details', { id }, children);
    this.open = false;
  }
}
class HTMLFormElement extends HTMLElement {
  constructor(attrs, children) { super('form', attrs, children); }
  get method() { return this.attrs.method || 'get'; }
}
class Document {
  constructor({ revision = '7', action = 'feedback', task = 'task-a', method = 'post', active = false } = {}) {
    this.active = active;
    this.feedback = new HTMLTextAreaElement('feedback');
    this.nameField = new HTMLInputElement('name', 'text', 'Stored old name');
    this.checkbox = new HTMLInputElement('confirmed', 'checkbox', 'yes');
    this.radio = new HTMLInputElement('mode', 'radio', 'manual');
    this.select = new HTMLSelectElement('status');
    this.search = new HTMLInputElement('q', 'search', 'original query');
    this.revision = new HTMLInputElement('expected_revision', 'hidden', revision);
    const fields = method === 'get' ? [this.search] : [
      new HTMLInputElement('action', 'hidden', action),
      new HTMLInputElement('task_id', 'hidden', task),
      new HTMLInputElement('workspace_id', 'hidden', 'workspace-a'),
      this.revision, this.nameField, this.feedback, this.checkbox, this.radio, this.select,
    ];
    this.form = new HTMLFormElement({ method, action: '/cap/workspaces/workspace-a/' }, fields);
    const summary = new HTMLElement('summary');
    summary.textContent = 'Feedback';
    this.details = new HTMLDetailsElement([summary, this.form]);
    this.otherDraft = new HTMLTextAreaElement('comment');
    const otherFields = [
      new HTMLInputElement('action', 'hidden', 'comment'),
      new HTMLInputElement('task_id', 'hidden', task),
      new HTMLInputElement('workspace_id', 'hidden', 'workspace-a'),
      new HTMLInputElement('expected_revision', 'hidden', revision), this.otherDraft,
    ];
    this.otherForm = new HTMLFormElement(
      { method: 'post', action: '/cap/workspaces/workspace-a/' }, otherFields
    );
    this.otherDetails = new HTMLDetailsElement(
      [new HTMLElement('summary'), this.otherForm], 'comment-disclosure'
    );
    this.button = new HTMLElement('button', { 'data-refresh-now': '' });
    this.copy = new HTMLElement('span', { class: 'live-copy' });
    this.indicator = new HTMLElement('span', { class: 'live-indicator' }, [this.copy]);
    this.sidebar = new HTMLElement('aside', { class: 'app-sidebar' });
    this.layout = new HTMLElement('main', { class: 'app-layout' }, [
      this.sidebar, this.details, this.otherDetails, this.button, this.indicator,
    ]);
    this.body = new HTMLElement('body', { 'data-events-url': '/cap/events?snapshot=old' }, [
      this.layout,
    ]);
    this.title = 'Dashboard';
    this.activeElement = this.body;
    this.replacements = 0;
    this.listeners = new Map();
    this.adopt(this.body);
  }
  adopt(element) {
    element.ownerDocument = this;
    for (const child of element.children) this.adopt(child);
  }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
  querySelector(selector) { return this.body.querySelector(selector); }
  getElementById(id) { return this.body.descendants().find((element) => element.id === id) || null; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  fire(name, event) { this.listeners.get(name)?.(event); }
}

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
const tick = () => new Promise(setImmediate);

async function probe() {
  const getForm = scenario === 'force-get' || scenario === 'navigation-search' || scenario.startsWith('server-search-');
  const serverConflict = scenario === 'mutation-server-conflict-refresh';
  const serverArchive = scenario === 'mutation-server-archive-navigation';
  const action = serverConflict ? 'server_save' : 'feedback';
  const document = new Document({ method: getForm ? 'get' : 'post', active: true, action });
  const fresh = new Document({ revision: '9', method: getForm ? 'get' : 'post', action });
  let parsedDocument = fresh;
  if (renderedOptions) {
    for (const page of [document, fresh]) {
      page.select.attrs.name = 'kind';
      page.select.options = renderedOptions.map((option) => ({ ...option }));
    }
  }
  if (serverConflict) {
    for (const page of [document, fresh]) {
      page.feedback.attrs.name = 'role';
      page.form.attrs.action = '/servers/shared/';
    }
  }
  document.sidebar.scrollTop = 37;
  if (scenario.startsWith('server-search-')) {
    for (const page of [document, fresh]) {
      page.form.attrs['data-server-search'] = '';
      page.form.attrs.action = '/servers/';
      page.searchNotice = new HTMLElement('p', { 'data-server-filter-status': '' });
      page.form.children.push(page.searchNotice);
      page.searchNotice.parentElement = page.form;
      const heading = new HTMLElement('header', { 'data-server-page-url': '/servers/?q=requested' });
      page.layout.children.push(heading);
      heading.parentElement = page.layout;
      if (scenario === 'server-search-create-draft') {
        page.otherForm.elements.namedItem('action').value = 'server_save';
        page.otherForm.elements.namedItem('expected_revision').value = '0';
        page.serverIdentity = new HTMLInputElement('server_id', 'text', page === document ? 'allocated-one' : 'allocated-two');
        page.otherForm.children.push(page.serverIdentity);
        page.serverIdentity.parentElement = page.otherForm;
      }
      page.adopt(page.layout);
    }
    fresh.search.value = fresh.search.defaultValue = 'requested';
    if (scenario === 'server-search-draft-disappears') fresh.otherDetails.children = [];
  }
  if (scenario.startsWith('server-tabs-')) {
    for (const page of [document, fresh]) {
      page.serverViews = ['overview', 'sites', 'profiles', 'history'].map((key) => new HTMLElement(
        'section', { id: `server-${key}`, 'data-server-view': key },
        key === 'overview' ? [page.details] : key === 'sites' ? [page.otherDetails] : []
      ));
      page.serverLinks = page.serverViews.map((view) => new HTMLElement('a', {
        href: `#${view.id}`, 'data-server-tab': view.dataset.serverView,
      }));
      page.openEditor = new HTMLElement('a', {
        href: '#comment-disclosure', 'data-server-open': 'comment-disclosure',
      });
      page.serverWorkspace = new HTMLElement('div', {
        'data-server-workspace': '', 'data-server-initial-view': 'overview',
        'data-server-page-url': '/servers/shared/?sites_q=app',
      }, [...page.serverLinks, page.openEditor, ...page.serverViews]);
      page.layout.children = [page.sidebar, page.serverWorkspace, page.button, page.indicator];
      page.serverWorkspace.parentElement = page.layout;
      page.adopt(page.layout);
    }
  }
  if (scenario.startsWith('server-map-')) {
    for (const page of [document, fresh]) {
      page.navLink = new HTMLElement('a', {
        href: 'http://127.0.0.1:7777/servers/shared/?view=domains&collection=domain&app_id=app#server-domains',
        'data-server-navigation': '',
      });
      page.detail = new HTMLElement('article', { class: 'server-map-detail', id: 'map-application-app' });
      page.serverViews = ['sites', 'domains'].map((key) => new HTMLElement('section', {
        id: `server-${key}`, 'data-server-view': key,
      }, key === 'sites' ? [page.detail] : [new HTMLElement('h2')]));
      page.serverWorkspace = new HTMLElement('div', {
        'data-server-workspace': '', 'data-server-initial-view': page === document ? 'sites' : 'domains',
        'data-server-revision': '9',
        'data-server-page-url': '/servers/shared/?view=domains&collection=domain&app_id=app',
      }, [page.navLink, ...page.serverViews, page.details, page.otherDetails]);
      page.layout.children = [page.sidebar, page.serverWorkspace, page.button, page.indicator];
      page.serverWorkspace.parentElement = page.layout;
      if (scenario === 'server-map-cancel') {
        page.cancel = new HTMLElement('button', { type: 'reset', 'data-server-cancel': '' });
        page.form.children.push(page.cancel);
        page.cancel.parentElement = page.form;
        page.feedback.dataset.serverDefault = 'Stored text';
      }
      if (scenario === 'server-map-error') {
        page.alert = new HTMLElement('p', { 'data-server-error-summary': '', role: 'alert' });
        page.layout.children.push(page.alert);
        page.alert.parentElement = page.layout;
      }
      page.adopt(page.layout);
    }
  }
  if (scenario === 'project-filters') {
    for (const page of [document, fresh]) {
      const filter = (group) => {
        const field = new HTMLInputElement('', 'search');
        field.attrs['data-ui-filter'] = group;
        field.dataset.uiFilter = group;
        return field;
      };
      page.projectSearch = filter('projects');
      page.sidebarSearch = filter('sidebar');
      page.mobileSearch = filter('sidebar');
      page.reviewFilter = new HTMLElement('button', { 'data-project-filter': 'review' });
      page.reviewProject = new HTMLElement('article', {
        'data-filter-item': 'projects', 'data-filter-text': 'Atlas', 'data-category': 'review',
      });
      page.idleProject = new HTMLElement('article', {
        'data-filter-item': 'projects', 'data-filter-text': 'Atlas docs', 'data-category': 'idle',
      });
      page.projectNavigation = new HTMLElement('nav', { class: 'project-navigation' }, [
        page.sidebarSearch, page.mobileSearch, page.projectSearch, page.reviewFilter,
        page.reviewProject, page.idleProject,
      ]);
      page.sidebar.children.push(page.projectNavigation);
      page.projectNavigation.parentElement = page.sidebar;
      page.adopt(page.layout);
    }
    document.projectNavigation.scrollTop = 95;
  }
  const requests = [];
  const sources = [];
  const scrolls = [];
  const historyUpdates = [];
  const windowListeners = new Map();
  let pageSelection = null;
  const timers = [];
  const flushInteraction = async () => {
    while (timers.length) timers.shift()();
    await tick();
  };
  class EventSource extends HTMLElement {
    constructor() { super('event-source'); sources.push(this); }
    close() {}
  }
  let parseCount = 0;
  class DOMParser {
    parseFromString() {
      parseCount += 1;
      return ['mutation-invalid-html', 'refresh-failure-parse'].includes(scenario) && parseCount === 1
        ? { querySelector: () => null }
        : parsedDocument;
    }
  }
  const window = {
    getSelection: () => pageSelection,
    EventSource, location: {
      pathname: '/cap/workspaces/workspace-a/', search: '?q=original&page=2', hash: '',
      origin: 'http://127.0.0.1:7777',
      href: 'http://127.0.0.1:7777/cap/workspaces/workspace-a/?q=original&page=2',
    },
    history: { replaceState: (...args) => {
      historyUpdates.push(args);
      const url = new URL(args[2], window.location.href);
      Object.assign(window.location, { pathname: url.pathname, search: url.search, hash: url.hash, href: url.href });
    } },
    scrollX: 17, scrollY: 99, scrollTo: (...args) => scrolls.push(args),
    addEventListener(name, handler) { windowListeners.set(name, handler); },
  };
  window.history.pushState = window.history.replaceState;
  class FormData { constructor(form) { this.form = form; } }
  class URLSearchParams {
    constructor(data) { this.data = data; }
    toString() {
      return this.data.form.elements.filter((field) => field.name).map(
        (field) => `${encodeURIComponent(field.name)}=${encodeURIComponent(field.value)}`
      ).join('&');
    }
  }
  vm.runInNewContext(source, {
    document, window, EventSource, DOMParser, HTMLElement, HTMLInputElement, HTMLFormElement,
    HTMLTextAreaElement, HTMLSelectElement, HTMLDetailsElement, FormData, URLSearchParams, URL,
    setTimeout: (callback) => { timers.push(callback); },
    fetch: (url, options) => {
      const response = deferred();
      const body = deferred();
      requests.push({ response, body, url, options });
      return response.promise;
    },
  }, { timeout: 1000 });
  if (scenario.startsWith('interaction-')) {
    const late = scenario.includes('-late-');
    const kind = scenario.split('-').at(-1);
    const begin = () => {
      if (kind === 'selection') {
        pageSelection = { isCollapsed: false, anchorNode: document.details, focusNode: document.details };
      } else if (kind === 'composition') {
        document.fire('compositionstart', { target: document.feedback });
      } else if (kind === 'pointer') {
        document.fire('pointerdown', { target: document.details });
      } else {
        const target = kind === 'select' ? document.select : kind === 'summary'
          ? document.details.querySelector('summary') : kind === 'button' ? document.button : document.feedback;
        target.focus();
      }
    };
    if (!late) begin();
    sources[0].fire('refresh');
    await tick();
    if (late) {
      assert.equal(requests.length, 1);
      begin();
      requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
      await tick();
      requests[0].body.resolve('<fresh>');
      await tick();
    } else {
      assert.equal(requests.length, 0, 'active interaction must defer fetching');
    }
    assert.equal(document.replacements, 0, 'keep the original DOM throughout interaction');
    assert.equal(document.copy.textContent, 'Есть обновление');
    sources[0].fire('ready');
    sources[0].onerror();
    assert.equal(document.copy.textContent, 'Есть обновление', 'reconnect must retain the pending hint');
    if (kind === 'text') {
      document.select.focus();
      document.fire('focusout', {});
      await flushInteraction();
      assert.equal(requests.length, late ? 1 : 0, 'moving focus between controls must not apply a refresh');
    }
    if (kind === 'dirty') document.feedback.value = 'Unsaved text';
    if (kind === 'selection') {
      pageSelection = null;
      document.fire('selectionchange', {});
    } else if (kind === 'composition') {
      document.fire('compositionend', { target: document.feedback });
    } else if (kind === 'pointer') {
      windowListeners.get('pointerup')();
    } else {
      document.activeElement = document.body;
      document.fire('focusout', {});
    }
    await flushInteraction();
    if (kind === 'dirty') {
      assert.equal(requests.length, 0, 'finishing interaction must not discard an unsaved draft');
      document.feedback.value = document.feedback.defaultValue;
      document.fire('input', { target: document.feedback });
      await flushInteraction();
    }
    assert.equal(requests.length, late ? 2 : 1, 'resume the pending hint without needing another SSE event');
    const request = requests.at(-1);
    request.response.resolve({ ok: true, text: () => request.body.promise });
    await tick();
    request.body.resolve('<fresh>');
    await tick();
    assert.equal(document.replacements, 1);
    assert.equal(document.querySelector('.live-copy').textContent, 'Онлайн');
    return;
  }
  if (scenario.startsWith('server-map-')) {
    const setUrl = (href) => {
      const url = new URL(href);
      Object.assign(window.location, { pathname: url.pathname, search: url.search, hash: url.hash, href });
    };
    const original = 'http://127.0.0.1:7777/servers/shared/?view=sites&collection=application&object_id=app&map_order=desc#server-sites';
    setUrl(original);
    if (scenario === 'server-map-cancel') {
      document.feedback.value = 'Cancelled draft';
      document.feedback.dataset.recoveredDraft = 'true';
      document.otherDraft.value = 'Keep neighboring draft';
      document.cancel.fire('click', { preventDefault() {} });
      assert.equal(document.feedback.value, 'Stored text');
      assert.equal(document.feedback.defaultValue, 'Stored text');
      assert.equal(document.feedback.dataset.recoveredDraft, undefined);
      assert.equal(document.otherDraft.value, 'Keep neighboring draft');
      assert.equal(document.revision.value, '9');
      assert.equal(requests.length, 0, 'cancel does not submit or reload');
      assert.equal(document.activeElement, document.details.querySelector('summary'));
      return;
    }
    if (scenario === 'server-map-error') {
      document.feedback.value = 'Retain invalid draft';
      document.fire('submit', { target: document.form, preventDefault() {} });
      await tick();
      requests[0].response.resolve({ ok: false, headers: { get: () => 'text/html' }, text: () => requests[0].body.promise });
      await tick(); requests[0].body.resolve('<error>'); await tick();
      assert.equal(document.activeElement, fresh.alert);
      assert.equal(fresh.feedback.value, 'Retain invalid draft');
      return;
    }
    document.navLink.fire('click', { button: 0, preventDefault() {} });
    await tick();
    requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
    await tick(); requests[0].body.resolve('<domains>'); await tick();
    assert.equal(historyUpdates.length, 1, 'one logical transition produces one history entry');
    assert.equal(window.location.search, '?view=domains&collection=domain&app_id=app');
    assert.equal(window.location.hash, '#server-domains');
    assert.equal(document.activeElement, fresh.serverViews[1].querySelector('h2'));
    const back = new Document({ revision: '9' });
    back.serverWorkspace = new HTMLElement('div', { 'data-server-workspace': '', 'data-server-initial-view': 'sites' }, [
      new HTMLElement('section', { 'data-server-view': 'sites', id: 'server-sites' }, [
        new HTMLElement('article', { class: 'server-map-detail' }),
      ]),
    ]);
    back.layout.children.push(back.serverWorkspace);
    back.serverWorkspace.parentElement = back.layout;
    back.adopt(back.layout);
    parsedDocument = back;
    setUrl(original);
    windowListeners.get('popstate')();
    await tick();
    assert.equal(requests.length, 2, 'Back restores query state with a read');
    requests[1].response.resolve({ ok: true, text: () => requests[1].body.promise });
    await tick(); requests[1].body.resolve('<app>'); await tick();
    assert.equal(historyUpdates.length, 1, 'Back cannot create another history entry');
    assert.equal(document.activeElement, back.serverWorkspace.querySelector('.server-map-detail'));
    assert.equal(window.location.href, original);
    assert.deepEqual(scrolls.at(-1), [17, 99], 'Back restores prior viewport');
    return;
  }
  if (scenario.startsWith('server-search-')) {
    document.search.value = 'requested';
    document.otherDraft.value = 'Keep unsaved editor';
    if (scenario === 'server-search-create-draft') document.serverIdentity.value = 'chosen-id';
    let prevented = false;
    document.fire('submit', {target: document.form, preventDefault() { prevented = true; }});
    await tick();
    assert.equal(prevented, true);
    assert.equal(requests[0].url, '/servers/?q=requested');
    requests[0].response.resolve({ ok: scenario !== 'server-search-error', text: () => requests[0].body.promise });
    await tick(); requests[0].body.resolve('<results>'); await tick();
    if (['server-search-success', 'server-search-create-draft'].includes(scenario)) {
      assert.equal(document.replacements, 1);
      assert.equal(fresh.search.value, 'requested');
      assert.equal(fresh.otherDraft.value, 'Keep unsaved editor');
      assert.equal(fresh.otherForm.elements.namedItem('expected_revision').value, scenario === 'server-search-create-draft' ? '0' : '7',
        'search cannot rebase an unsaved editor');
      if (scenario === 'server-search-create-draft') assert.equal(fresh.serverIdentity.value, 'chosen-id');
      assert.equal(window.location.pathname, '/servers/');
      assert.equal(window.location.search, '?q=requested');
    } else {
      assert.equal(document.replacements, 0, 'failed search keeps the current screen');
      assert.equal(document.otherDraft.value, 'Keep unsaved editor');
      assert.equal(document.search.value, 'requested');
      assert.match(document.searchNotice.textContent, scenario === 'server-search-error'
        ? /Не удалось выполнить поиск/ : /Сохраните черновик/);
    }
    return;
  }
  if (scenario.startsWith('server-tabs-')) {
    const click = (link) => {
      let prevented = false;
      link.fire('click', { button: 0, preventDefault() { prevented = true; } });
      assert.equal(prevented, true);
    };
    assert.equal(document.serverViews[0].hidden, false);
    assert.equal(document.serverViews[1].hidden, true);
    document.feedback.value = 'Unsubmitted server draft';
    click(document.serverLinks[1]);
    assert.equal(window.location.hash, '#server-sites');
    assert.equal(document.serverViews[0].hidden, true);
    assert.equal(document.serverViews[1].hidden, false);
    assert.equal(document.serverLinks[1].getAttribute('aria-current'), 'page');
    assert.equal(requests.length, 0, 'switching sections does not reload the document');
    click(document.openEditor);
    assert.equal(document.otherDetails.open, true, 'add/edit links reveal their disclosure');
    assert.equal(document.serverViews[1].hidden, false);
    if (scenario === 'server-tabs-navigation') {
      window.location.hash = '#server-overview';
      windowListeners.get('popstate')();
      assert.equal(document.serverViews[0].hidden, false, 'Back restores the requested section');
      assert.equal(document.feedback.value, 'Unsubmitted server draft');
      assert.equal(requests.length, 0);
    } else {
      click(document.serverLinks[1]);
      document.button.fire('click');
      await tick();
      requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
      await tick(); requests[0].body.resolve('<fresh page>'); await tick();
      assert.equal(document.replacements, 1);
      assert.equal(window.location.hash, '#server-sites', 'refresh preserves the selected section');
      assert.equal(fresh.serverViews[1].hidden, false);
      assert.equal(fresh.feedback.value, 'Unsubmitted server draft');
      assert.equal(fresh.revision.value, '9');
    }
    return;
  }
  if (scenario.startsWith('refresh-failure-')) {
    const dirty = scenario !== 'refresh-failure-clean';
    if (dirty) {
      document.feedback.value = 'Server edit before failed refresh';
      document.otherDraft.value = 'Unsubmitted operation';
      document.feedback.focus();
      document.feedback.setSelectionRange(2, 8, 'backward');
      document.feedback.scrollTop = 23;
      document.details.open = true;
    }
    const originalForm = document.form;
    const originalFocus = document.activeElement;
    document.button.fire('click');
    await tick();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.method, undefined, 'refresh must remain a safe GET');
    if (['refresh-failure-network', 'refresh-failure-clean'].includes(scenario)) {
      requests[0].response.reject(new Error('offline'));
    } else if (scenario === 'refresh-failure-http') {
      requests[0].response.resolve({ ok: false });
    } else {
      requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
      await tick();
      if (scenario === 'refresh-failure-body') requests[0].body.reject(new Error('offline body'));
      else requests[0].body.resolve('<invalid layout>');
    }
    await tick();
    const expected = dirty ? 'Не удалось обновить · черновик сохранён' : 'Не удалось обновить';
    assert.equal(document.copy.textContent, expected, 'failed refresh must report failure');
    assert.equal(document.replacements, 0);
    assert.equal(document.form, originalForm);
    assert.equal(document.activeElement, originalFocus);
    assert.equal(window.location.search, '?q=original&page=2');
    sources[0].fire('ready');
    sources[0].onerror();
    assert.equal(document.copy.textContent, expected, 'SSE must not erase failed GET feedback');
    if (dirty) {
      sources[0].fire('refresh');
      await tick();
      assert.equal(requests.length, 1, 'automatic hint must preserve an unsaved draft');
      assert.equal(document.copy.textContent, expected);
      document.feedback.value = 'Late server edit before retry';
    }
    document.button.fire('click');
    await tick();
    assert.equal(requests.length, 2, 'same refresh button must support retry');
    assert.equal(requests[1].url, requests[0].url, 'retry must retain URL filters and pagination');
    requests[1].response.resolve({ ok: true, text: () => requests[1].body.promise });
    await tick();
    requests[1].body.resolve('<fresh retry page>');
    await tick();
    assert.equal(document.replacements, 1);
    assert.equal(fresh.copy.textContent, 'Онлайн');
    assert.equal(fresh.revision.value, '9');
    assert.equal(fresh.sidebar.scrollTop, 37);
    assert.deepEqual(scrolls, [[17, 99]]);
    if (dirty) {
      assert.equal(fresh.feedback.value, 'Late server edit before retry');
      assert.equal(fresh.otherDraft.value, 'Unsubmitted operation');
      assert.equal(document.activeElement, fresh.feedback);
      assert.equal(fresh.feedback.selectionStart, 2);
      assert.equal(fresh.feedback.selectionEnd, 8);
      assert.equal(fresh.feedback.scrollTop, 23);
      assert.equal(fresh.details.open, true);
    }
    return;
  }
  if (scenario.startsWith('navigation-')) {
    let prevented = false;
    const event = { preventDefault() { prevented = true; } };
    windowListeners.get('beforeunload')(event);
    assert.equal(prevented, false, 'clean navigation stays immediate');
    if (getForm) document.search.value = 'another query';
    else document.feedback.value = 'unfinished operator feedback';
    windowListeners.get('beforeunload')(event);
    assert.equal(prevented, !getForm, 'only unsaved mutation drafts prompt before leaving');
    return;
  }
  const beginResponse = async () => {
    assert.equal(requests.length, 1, 'refresh must issue one request');
    requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
    await tick();
  };
  const finishResponse = async () => {
    requests[0].body.resolve('<fresh page>');
    await tick();
  };

  if (scenario.startsWith('neighbor-cas-')) {
    const kind = scenario.split('-')[2];
    const success = scenario.endsWith('success');
    const setup = (page) => {
      const configure = (form, action, keyName, key) => {
        form.attrs.action = '/servers/shared/';
        form.elements.namedItem('action').value = action;
        for (const field of [new HTMLInputElement('server_id', 'hidden', 'shared'),
          ...(keyName ? [new HTMLInputElement(keyName, 'hidden', key)] : [])]) {
          form.children.push(field); field.parentElement = form; page.adopt(field);
        }
      };
      configure(page.form, kind === 'site' ? 'server_site_save' : kind === 'profile'
        ? 'server_profile_save' : 'server_save', kind === 'site' ? 'site_key'
          : kind === 'profile' ? 'profile_key' : null, 'owned');
      configure(page.otherForm, 'server_site_save', 'site_key', 'neighbor');
      page.extraForms = ['server_profile_save', 'server_save'].map((action) => {
        const fields = [new HTMLInputElement('action', 'hidden', action),
          new HTMLInputElement('server_id', 'hidden', 'shared'),
          new HTMLInputElement('expected_revision', 'hidden', page.revision.value),
          new HTMLTextAreaElement(action === 'server_save' ? 'role' : 'steps')];
        if (action === 'server_profile_save') fields.push(new HTMLInputElement('profile_key', 'hidden', 'neighbor'));
        const form = new HTMLFormElement({method: 'post', action: '/servers/shared/'}, fields);
        page.layout.children.push(form); form.parentElement = page.layout; page.adopt(form);
        return form;
      });
    };
    for (const page of [document, fresh]) setup(page);
    document.feedback.value = 'Own draft';
    document.otherDraft.value = 'Neighbor site draft';
    document.extraForms.forEach((form) => { form.querySelector('textarea').value = 'Neighbor draft'; });
    const respond = async (index, next, ok) => {
      parsedDocument = next;
      requests[index].response.resolve({ok, url: '', headers: {get: () => 'text/html'},
        text: () => requests[index].body.promise});
      await tick(); requests[index].body.resolve('<authoritative response>'); await tick();
    };
    document.fire('submit', {target: document.form, preventDefault() {}}); await tick();
    // A concurrent editor has written revision8; own result at9 must not rebase neighbors.
    await respond(0, fresh, success);
    const neighborForms = [fresh.otherForm, ...fresh.extraForms];
    const revisions = neighborForms.map((form) => form.elements.namedItem('expected_revision').value);
    assert.deepEqual(revisions, ['7', '7', '7']);
    assert.equal(fresh.revision.value, success ? '9' : '7');
    assert.equal(fresh.otherDraft.value, 'Neighbor site draft');
    assert.equal(fresh.otherDraft.dataset.recoveredDraft, 'true');
    assert.equal(fresh.extraForms[0].querySelector('textarea').value, 'Neighbor draft');
    document.fire('submit', {target: fresh.otherForm, preventDefault() {}}); await tick();
    assert.equal(requests[1].options.body.data.form.elements.namedItem('expected_revision').value, '7');
    const repeated = new Document({revision: '10', active: true}); setup(repeated);
    await respond(1, repeated, false);
    assert.equal(repeated.otherForm.elements.namedItem('expected_revision').value, '7',
      'a failed neighboring save must not rebase any unsent editor');
    assert.equal(repeated.extraForms[1].elements.namedItem('expected_revision').value, '7');
    const reconciled = new Document({revision: '10'}); setup(reconciled);
    reconciled.otherDraft.value = reconciled.otherDraft.defaultValue = 'Concurrent authoritative name';
    repeated.button.fire('click'); await tick();
    await respond(2, reconciled, true);
    assert.equal(reconciled.otherDraft.value, 'Neighbor site draft');
    for (const form of [reconciled.form, reconciled.otherForm, ...reconciled.extraForms]) {
      assert.equal(form.elements.namedItem('expected_revision').value, '10',
        'only an explicit read-latest rebases neighboring drafts');
    }
    console.log(JSON.stringify({neighbor_revisions: revisions}));
    return;
  }

  if (scenario === 'project-filters') {
    document.sidebarSearch.value = 'Atlas';
    document.sidebarSearch.fire('input');
    assert.equal(document.mobileSearch.value, 'Atlas');
    document.projectSearch.value = 'Atlas';
    document.projectSearch.fire('input');
    document.reviewFilter.fire('click');
    assert.equal(document.reviewProject.hidden, false);
    assert.equal(document.idleProject.hidden, true);
    document.sidebarSearch.focus();
    let prompted = false;
    windowListeners.get('beforeunload')({ preventDefault() { prompted = true; } });
    assert.equal(prompted, false, 'Project filtering is not an unsaved mutation');
    sources[0].fire('refresh');
    await tick();
    assert.equal(requests.length, 0, 'filter focus must keep the original input, including IME state');
    document.activeElement = document.body;
    document.fire('focusout', {});
    await flushInteraction();
    await beginResponse();
    await finishResponse();
    assert.equal(document.replacements, 1, 'filters resume SSE updates after focus leaves');
    assert.equal(fresh.sidebarSearch.value, 'Atlas');
    assert.equal(fresh.mobileSearch.value, 'Atlas');
    assert.equal(fresh.projectSearch.value, 'Atlas');
    assert.equal(document.activeElement, document.body, 'do not steal focus after filtering');
    assert.equal(fresh.reviewFilter.getAttribute('aria-pressed'), 'true');
    assert.equal(fresh.idleProject.hidden, true);
    assert.equal(fresh.projectNavigation.scrollTop, 95);
    return;
  }

  if (scenario === 'mutation-during-refresh') {
    sources[0].fire('refresh');
    await tick();
    assert.equal(requests.length, 1, 'background refresh must start first');
    document.feedback.value = 'Mutation while refresh is pending';
    document.fire('submit', { target: document.form, preventDefault() {} });
    await tick();
    assert.equal(requests.length, 2, 'mutation must not wait for an older refresh');
    requests[1].response.resolve({
      ok: true, url: '', headers: { get: () => 'text/html; charset=utf-8' },
      text: () => requests[1].body.promise,
    });
    await tick();
    requests[1].body.resolve('<authoritative mutation page>');
    await tick();
    assert.equal(document.replacements, 1);
    requests[0].response.resolve({ ok: true, text: () => requests[0].body.promise });
    await tick();
    requests[0].body.resolve('<stale refresh page>');
    await tick();
    assert.equal(document.replacements, 1, 'pre-mutation refresh must not replace newer state');
    return;
  }

  if (scenario.startsWith('delete-child-')) {
    const isProfile = scenario.includes('profile');
    const selected = scenario.includes('selected');
    const pageClamp = scenario.includes('page-clamp');
    const uncertain = scenario.endsWith('uncertain');
    const conflict = scenario.endsWith('conflict');
    const keyName = isProfile ? 'profile_key' : 'site_key';
    const saveAction = isProfile ? 'server_profile_save' : 'server_site_save';
    const deleteAction = isProfile ? 'server_profile_delete' : 'server_site_delete';
    const selection = isProfile ? 'profile_id' : 'site_id';
    const neighborSelection = isProfile ? 'site_id' : 'profile_id';
    const filters = `sites_q=app&sites_offset=${pageClamp && !isProfile ? 95 : 90}` +
      `&profiles_offset=${pageClamp && isProfile ? 12 : 8}&before=20&environment=test&q=fixture` +
      (selected ? `&${neighborSelection}=neighbor` : '');
    const originalUrl = `/servers/shared/?${selected ? `${selection}=owned&` : ''}${filters}`;
    const canonicalUrl = conflict ? originalUrl : `/servers/shared/?${filters
      .replace('sites_offset=95', 'sites_offset=90').replace('profiles_offset=12', 'profiles_offset=8')}`;
    if (selected || pageClamp) {
      const url = new URL(originalUrl, window.location.href);
      Object.assign(window.location, { pathname: url.pathname, search: url.search, href: url.href });
      const marker = new HTMLElement('header', { 'data-server-page-url': canonicalUrl });
      fresh.layout.children.push(marker); marker.parentElement = fresh.layout; fresh.adopt(marker);
    }
    for (const page of [document, fresh]) {
      page.form.elements.namedItem('action').value = saveAction;
      if (selected || pageClamp) page.form.attrs.action = page.otherForm.attrs.action = originalUrl;
      for (const field of [new HTMLInputElement('server_id', 'hidden', 'shared'), new HTMLInputElement(keyName, 'hidden', 'owned')]) {
        page.form.children.push(field); field.parentElement = page.form; page.adopt(field);
      }
    }
    const deleteForm = new HTMLFormElement({method: 'post'}, [
      new HTMLInputElement('action', 'hidden', deleteAction),
      new HTMLInputElement('server_id', 'hidden', 'shared'),
      new HTMLInputElement(keyName, 'hidden', 'owned'),
      new HTMLInputElement('expected_revision', 'hidden', '7'),
    ]);
    document.details.children.push(deleteForm); deleteForm.parentElement = document.details;
    document.adopt(deleteForm);
    document.feedback.value = 'Own unsaved deleted draft';
    document.otherDraft.value = 'Neighbor must remain dirty';
    if (!conflict) fresh.details.children = [];
    document.fire('submit', {target: deleteForm, preventDefault() {}});
    await tick();
    if (uncertain) {
      requests[0].response.reject(new Error('uncertain delete'));
      await tick();
      assert.equal(requests.length, 2, 'an uncertain delete performs only a safe recovery GET');
      assert.equal(requests[1].url, originalUrl, 'unconfirmed deletion retains the exact selection');
      requests[1].response.resolve({ok: false});
      await tick();
      assert.equal(document.replacements, 0);
      assert.equal(document.feedback.value, 'Own unsaved deleted draft');
      assert.equal(document.feedback.defaultValue, '');
      assert.equal(document.otherDraft.value, 'Neighbor must remain dirty');
      assert.equal(document.revision.value, '7');
      assert.equal(window.location.pathname + window.location.search, originalUrl);
      assert.deepEqual(historyUpdates, []);
      assert.equal(document.copy.textContent, 'Не удалось подтвердить сохранение');
      return;
    }
    requests[0].response.resolve({ok: !conflict, url: selected || pageClamp
      ? `http://127.0.0.1:7777${originalUrl}` : '', headers: {get: () => 'text/html'},
      text: () => requests[0].body.promise});
    await tick(); requests[0].body.resolve('<child delete result>'); await tick();
    assert.equal(document.replacements, 1, 'confirmed deletion must not strand a ghost editor');
    assert.equal(fresh.otherDraft.value, 'Neighbor must remain dirty');
    assert.equal(fresh.otherDraft.dataset.recoveredDraft, 'true');
    assert.equal(fresh.otherForm.elements.namedItem('expected_revision').value, '7',
      'successful deletion must not rebase an unsent neighboring draft');
    if (selected || pageClamp) {
      assert.equal(window.location.pathname + window.location.search, canonicalUrl,
        'canonical selection/page reconciliation must survive native DOM adoption');
      assert.deepEqual(historyUpdates, [[null, '', canonicalUrl]]);
      assert.equal(fresh.querySelector('[data-server-page-url]'), null);
      assert.ok(document.querySelector('[data-server-page-url]'));
      assert.equal(requests.length, 1, 'confirmed deletion needs no recovery Refresh');
    }
    if (conflict) {
      assert.equal(fresh.feedback.value, 'Own unsaved deleted draft');
      assert.equal(fresh.feedback.dataset.recoveredDraft, 'true');
      assert.equal(fresh.copy.textContent, 'Проверьте форму');
      assert.equal(fresh.revision.value, '7');
    } else {
      assert.equal(document.feedback.defaultValue, 'Own unsaved deleted draft', 'only deleted entity tracking is cleared');
      assert.equal(document.querySelectorAll('form').includes(fresh.form), false);
      assert.equal(fresh.copy.textContent, 'Сохранено');
    }
    return;
  }
  if (scenario.startsWith('mutation-')) {
    if (!serverArchive) {
      document.feedback.value = 'Отправленный комментарий';
      document.otherDraft.value = 'Черновик другой формы';
    }
    let prevented = 0;
    document.fire('submit', { target: document.form, preventDefault: () => { prevented += 1; } });
    await tick();
    assert.equal(prevented, 1, 'enhanced mutation must prevent native navigation');
    assert.equal(requests.length, 1, 'mutation must issue one request');
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(document.copy.textContent, 'Сохранение', 'saving feedback must be immediate');
    assert.equal(document.form.getAttribute('aria-busy'), 'true');

    document.fire('submit', { target: document.form, preventDefault() {} });
    sources[0].fire('refresh');
    await tick();
    assert.equal(requests.length, 1, 'double submit and own SSE hint must not add requests');

    if (['mutation-network-error', 'mutation-network-recovery-retry'].includes(scenario)) {
      requests[0].response.reject(new Error('offline'));
      await tick();
      assert.equal(document.replacements, 0);
      assert.equal(document.feedback.value, 'Отправленный комментарий');
      assert.equal(document.form.getAttribute('aria-busy'), null);
      assert.equal(requests.length, 2, 'uncertain POST must be followed by one safe GET');
      fresh.body.dataset.eventsUrl = '/cap/events?snapshot=recovered';
      sources[0].fire('refresh');
      document.button.fire('click');
      document.fire('submit', { target: document.form, preventDefault() {} });
      await tick();
      assert.equal(
        requests.length, 2,
        'recovery GET must coalesce refresh hints and prevent an uncertain POST replay'
      );
      let recoveryRequest = requests[1];
      if (scenario === 'mutation-network-recovery-retry') {
        requests[1].response.reject(new Error('still offline'));
        await tick();
        assert.equal(document.copy.textContent, 'Не удалось подтвердить сохранение');
        sources[0].fire('ready');
        sources[0].onerror();
        assert.equal(document.copy.textContent, 'Не удалось подтвердить сохранение');
        document.button.fire('click');
        await tick();
        assert.equal(requests.length, 3);
        assert.equal(requests[2].options.method, undefined, 'uncertain write retry must only GET');
        recoveryRequest = requests[2];
      }
      recoveryRequest.response.resolve({ ok: true, text: () => recoveryRequest.body.promise });
      await tick();
      recoveryRequest.body.resolve('<authoritative recovery page>');
      await tick();
      assert.equal(document.replacements, 1);
      assert.equal(fresh.feedback.value, 'Отправленный комментарий');
      const recoveredRevision = scenario === 'mutation-network-recovery-retry' ? '9' : '7';
      assert.equal(fresh.revision.value, recoveredRevision,
        'automatic uncertain-write recovery is a read, not an implicit draft rebase');
      assert.equal(fresh.otherForm.elements.namedItem('expected_revision').value, recoveredRevision);
      assert.equal(fresh.copy.textContent, 'Проверьте сохранение');
      assert.equal(sources.length, 2, 'recovery snapshot must reconnect EventSource');
      sources.at(-1).fire('ready');
      sources.at(-1).onerror();
      assert.equal(
        fresh.copy.textContent, 'Проверьте сохранение',
        'late SSE ready/error must not erase an uncertain-mutation warning'
      );
      return;
    }

    const conflict = scenario === 'mutation-conflict' || serverConflict;
    if (scenario === 'mutation-late-edit') {
      document.feedback.value = 'Изменение во время сохранения';
    }
    if (conflict) {
      fresh.feedback.value = fresh.feedback.defaultValue = 'Отправленный комментарий';
      fresh.feedback.dataset.recoveredDraft = 'true';
      if (serverConflict) fresh.nameField.dataset.recoveredDraft = 'true';
    }
    if (scenario === 'mutation-page-clamp') {
      const marker = new HTMLElement('header', {
        'data-server-page-url': '/servers/shared/?sites_q=app&sites_offset=90&profiles_offset=8',
      });
      fresh.layout.children.push(marker); marker.parentElement = fresh.layout; fresh.adopt(marker);
    }
    requests[0].response.resolve({
      ok: !conflict,
      url: scenario === 'mutation-url-sync'
        ? 'http://127.0.0.1:7777/cap/tasks/task-a/'
        : scenario === 'mutation-page-clamp' ? 'http://127.0.0.1:7777/servers/shared/?sites_offset=95' : '',
      headers: { get: () => 'text/html; charset=utf-8' },
      text: () => requests[0].body.promise,
    });
    await tick();
    requests[0].body.resolve('<authoritative page>');
    await tick();
    if (scenario === 'mutation-invalid-html') {
      assert.equal(requests.length, 2, 'invalid POST response must trigger one safe GET');
      assert.equal(document.feedback.defaultValue, '', 'failed apply must restore dirty tracking');
      requests[1].response.resolve({ ok: true, text: () => requests[1].body.promise });
      await tick();
      requests[1].body.resolve('<authoritative recovery page>');
      await tick();
      assert.equal(document.replacements, 1);
      assert.equal(fresh.feedback.value, 'Отправленный комментарий');
      assert.equal(fresh.copy.textContent, 'Проверьте сохранение');
      return;
    }
    assert.equal(document.replacements, 1, 'mutation response must replace the layout in place');
    assert.equal(requests.length, 1, 'SSE hint from the same commit must stay coalesced');
    assert.equal(fresh.revision.value, conflict ? '7' : '9',
      'failed mutations keep draft baseline; confirmed own save advances its CAS');
    if (!serverArchive) assert.equal(fresh.otherForm.elements.namedItem('expected_revision').value, '7',
      'mutation response must preserve the neighboring draft revision');
    assert.equal(fresh.otherDraft.value, serverArchive ? '' : 'Черновик другой формы');
    if (serverArchive) {
      let prevented = false;
      windowListeners.get('beforeunload')({ preventDefault() { prevented = true; } });
      assert.equal(prevented, false, 'clean navigation remains immediate after archive');
    }
    if (scenario === 'mutation-url-sync') {
      assert.deepEqual(historyUpdates, [[null, '', '/cap/tasks/task-a/']]);
    }
    if (scenario === 'mutation-page-clamp') {
      assert.deepEqual(historyUpdates, [[null, '', '/servers/shared/?sites_q=app&sites_offset=90&profiles_offset=8']],
        'HTML canonical page must win over the POST request URL, retaining filters');
      assert.equal(window.location.pathname + window.location.search,
        '/servers/shared/?sites_q=app&sites_offset=90&profiles_offset=8');
      assert.equal(fresh.querySelector('[data-server-page-url]'), null,
        'the response document no longer owns the adopted canonical marker');
      assert.ok(document.querySelector('[data-server-page-url]'));
    }
    if (conflict) {
      assert.equal(fresh.feedback.value, 'Отправленный комментарий');
      assert.equal(fresh.feedback.dataset.recoveredDraft, 'true');
      assert.equal(fresh.copy.textContent, 'Проверьте форму');
      if (serverConflict) {
        assert.notEqual(fresh.nameField.dataset.recoveredDraft, 'true',
          'untouched echoed field must remain eligible for stored updates');
        const refreshed = new Document({ revision: '10', action: 'server_save' });
        refreshed.feedback.attrs.name = 'role';
        refreshed.form.attrs.action = '/servers/shared/';
        refreshed.nameField.value = refreshed.nameField.defaultValue = 'Concurrent stored name';
        parsedDocument = refreshed;
        fresh.button.fire('click');
        await tick();
        assert.equal(requests.length, 2, 'conflict recovery refresh issues a GET');
        requests[1].response.resolve({ ok: true, text: () => requests[1].body.promise });
        await tick();
        requests[1].body.resolve('<latest server card>');
        await tick();
        assert.equal(refreshed.feedback.value, 'Отправленный комментарий',
          'draft survives the refresh after an echoed conflict response');
        assert.equal(refreshed.revision.value, '10', 'refresh adopts authoritative CAS revision');
        assert.equal(refreshed.nameField.value, 'Concurrent stored name',
          'untouched metadata adopts the concurrent stored update');
        assert.equal(refreshed.feedback.dataset.recoveredDraft, 'true');
        document.fire('submit', { target: refreshed.form, preventDefault() {} });
        await tick();
        assert.equal(requests.length, 3, 'refreshed form can retry its save');
        assert.equal(requests[2].options.body.data.form.elements.namedItem('expected_revision').value, '10');
        const saved = new Document({ revision: '11', action: 'server_save' });
        saved.feedback.attrs.name = 'role';
        saved.form.attrs.action = '/servers/shared/';
        saved.feedback.value = saved.feedback.defaultValue = 'Отправленный комментарий';
        saved.nameField.value = saved.nameField.defaultValue = 'Concurrent stored name';
        parsedDocument = saved;
        requests[2].response.resolve({ ok: true, url: '',
          headers: { get: () => 'text/html' }, text: () => requests[2].body.promise });
        await tick();
        requests[2].body.resolve('<saved server card>');
        await tick();
        assert.equal(saved.feedback.dataset.recoveredDraft, undefined);
        assert.equal(saved.copy.textContent, 'Сохранено');
      }
    } else if (scenario === 'mutation-late-edit') {
      assert.equal(fresh.feedback.value, 'Изменение во время сохранения');
      assert.equal(fresh.copy.textContent, 'Сохранено');
    } else {
      assert.equal(fresh.copy.textContent, 'Сохранено');
    }
    return;
  }

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
        "interaction-text",
        "interaction-dirty",
        "interaction-select",
        "interaction-summary",
        "interaction-button",
        "interaction-selection",
        "interaction-composition",
        "interaction-pointer",
        "interaction-late-text",
        "interaction-late-select",
        "interaction-late-summary",
        "interaction-late-selection",
        "interaction-late-composition",
        "interaction-late-pointer",
        "refresh-failure-clean",
        "refresh-failure-network",
        "refresh-failure-http",
        "refresh-failure-body",
        "refresh-failure-parse",
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
        "delete-child-site-success",
        "delete-child-profile-success",
        "delete-child-site-conflict",
        "delete-child-profile-conflict",
        "delete-child-site-selected-success",
        "delete-child-profile-selected-success",
        "delete-child-site-selected-conflict",
        "delete-child-profile-selected-conflict",
        "delete-child-site-selected-uncertain",
        "delete-child-profile-selected-uncertain",
        "delete-child-site-page-clamp-success",
        "delete-child-profile-page-clamp-success",
        "neighbor-cas-site-success",
        "neighbor-cas-site-conflict",
        "neighbor-cas-profile-success",
        "neighbor-cas-profile-conflict",
        "neighbor-cas-server-success",
        "neighbor-cas-server-conflict",
        "mutation-success",
        "mutation-conflict",
        "mutation-server-conflict-refresh",
        "mutation-late-edit",
        "mutation-network-error",
        "mutation-network-recovery-retry",
        "mutation-invalid-html",
        "mutation-during-refresh",
        "mutation-url-sync",
        "mutation-page-clamp",
        "navigation-feedback",
        "navigation-search",
        "project-filters",
        "server-tabs-navigation",
        "server-tabs-refresh",
        "server-search-success",
        "server-search-error",
        "server-search-draft-disappears",
        "server-search-create-draft",
        "server-map-navigation",
        "server-map-cancel",
        "server-map-error",
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
