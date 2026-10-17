// The replay workspace shell: open bar, result list, one tab per open result, restoration of the open tabs.
//
// Every result tab owns its pane (<section class="result-pane">) and everything inside it; only the active pane is
// visible. Before a pane is shown or hidden it receives `replay-tab-activated` / `replay-tab-deactivated`, which the
// viewer uses to start and stop rendering. Open paths, their order, the active tab and each tab's viewer state are kept
// in localStorage (never the service's run IDs, which change when the service restarts).
//
// Every tab, active or not, follows its result's scene graph through the service's event stream. A new graph version is
// fetched at once, but only the active tab applies it to its viewer (state, time and camera kept); a background tab
// shows a marker and applies the newest version when it is activated.

import { ApiError, ReplayApi } from './api.js';
import { ReplayDataSource } from './replay_data.js';
import { ReplayViewer } from './viewer.js';

export const STORAGE_KEY = 'daaam.replay.workspace.v1';
const DATASET_NAMES = { tum: 'TUM', coda: 'CODa' };
const NOTICE_MS = 9000;
const MAX_PENDING_NOTICES = 5;

const randomId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
export const datasetName = (kind) => DATASET_NAMES[kind] || String(kind || '?').toUpperCase();
const basename = (path) => String(path).split('/').filter(Boolean).pop() || path;

// The viewer of one tab: its own bounded data source and WebGL view inside the tab's pane.
export function mountReplayViewer(tab, app) {
  const data = new ReplayDataSource(app.api, tab.runId, tab.manifest, tab.bundleVersion);
  const viewer = new ReplayViewer(tab.pane, data, tab.viewerState, { onStateChange: () => app.persist() });
  viewer.mount();
  return viewer;
}

export class WorkspaceApp {
  constructor(api = new ReplayApi(), storage = window.localStorage, options = {}) {
    this.api = api;
    this.storage = storage;
    this.tabs = new Map();   // tabId -> tab
    this.order = [];   // tabIds in strip order
    this.activeTabId = null;
    // (tab, app) -> viewer with activate/deactivate/serializeState/destroy; null shows a manifest summary only
    this.mountViewer = options.mountViewer === undefined ? mountReplayViewer : options.mountViewer;
    this.persistTimer = 0;
    this.el = {
      form: document.getElementById('open-form'),
      path: document.getElementById('result-path'),
      open: document.getElementById('open-result'),
      browser: document.getElementById('result-browser'),
      list: document.getElementById('result-list'),
      root: document.getElementById('result-root'),
      refresh: document.getElementById('refresh-results'),
      tabs: document.getElementById('tabs'),
      workspace: document.getElementById('workspace'),
      status: document.getElementById('global-status'),
    };
  }

  // ------------------------------------------------------------------ start
  async start() {
    document.getElementById('boot-message')?.remove();
    this.empty = element('p', 'workspace-empty', '在上方输入结果目录,或从 “output/ 里的结果” 中选择一个结果打开。');
    this.el.workspace.appendChild(this.empty);
    this.bindShell();
    try {
      await this.api.config();
    } catch (error) {
      this.notify(`无法连接回看服务:${error.message}`, 'error', 0);
      this.ready = true;
      return;
    }
    this.refreshResults();
    await this.restore();
    window.addEventListener('beforeunload', () => this.persistNow());
    this.ready = true;
  }

  bindShell() {
    this.el.form.addEventListener('submit', (event) => {
      event.preventDefault();
      const path = this.el.path.value.trim();
      if (path) this.openFromUser(path);
    });
    this.el.refresh.addEventListener('click', () => this.refreshResults());
    this.el.browser.addEventListener('toggle', () => { if (this.el.browser.open) this.refreshResults(); });
    this.el.tabs.addEventListener('keydown', (event) => this.onTabKey(event));
    window.addEventListener('resize', () => this.activeTab()?.viewer?.resize?.());
  }

  async openFromUser(path) {
    this.el.open.disabled = true;
    try {
      const tab = await this.openPath(path);
      if (tab) this.el.path.value = '';
    } catch (error) {
      this.notify(`打开失败:${path}\n${error.message}`, 'error', 0);
    } finally {
      this.el.open.disabled = false;
      this.openAttempts = (this.openAttempts || 0) + 1;   // finished attempts, successful or not
    }
  }

  // ------------------------------------------------------------------ result list
  async refreshResults() {
    const list = this.el.list;
    try {
      const data = await this.api.discover();
      this.el.root.textContent = data.output_root;
      list.replaceChildren();
      if (!data.results.length) list.appendChild(element('li', 'empty', '这里还没有带 replay/ 数据包的结果'));
      for (const item of data.results) {
        const li = element('li');
        const button = element('button', 'result-item');
        button.type = 'button';
        button.dataset.path = item.path;
        button.title = item.path;
        const badge = element('span', `dataset-badge dataset-badge--${item.dataset_kind}`, datasetName(item.dataset_kind));
        const name = element('span', 'result-item__name', `${item.title}  ·  ${item.relative_path}`);
        const meta = element('span', 'result-item__meta', `${item.sequence} · ${item.snapshot_count} 个快照`);
        button.append(badge, name, meta);
        button.addEventListener('click', () => {
          this.el.browser.open = false;
          this.openFromUser(item.path);
        });
        li.appendChild(button);
        list.appendChild(li);
      }
    } catch (error) {
      list.replaceChildren(element('li', 'empty', `无法列出结果:${error.message}`));
    }
  }

  // ------------------------------------------------------------------ tabs
  findByPath(canonicalPath) {
    return [...this.tabs.values()].find((tab) => tab.canonicalPath === canonicalPath) || null;
  }

  activeTab() { return this.activeTabId ? this.tabs.get(this.activeTabId) || null : null; }

  // Open a result (or focus its tab when it is open already); `saved` restores order position and viewer state.
  async openPath(path, saved = {}, { activate = true } = {}) {
    const opened = await this.api.open(path);
    const existing = this.findByPath(opened.canonical_path);
    if (existing) {
      if (existing.bundleVersion !== opened.bundle_version) await this.reload(existing, opened);
      if (activate) this.activate(existing.tabId);
      return existing;
    }
    const manifest = await this.api.manifest(opened.run_id);
    const tab = this.addTab({ opened, manifest, saved });
    if (activate) this.activate(tab.tabId);
    this.persist();
    return tab;
  }

  addTab({ opened, manifest, saved = {} }) {
    const tabId = randomId();
    const tab = {
      tabId,
      canonicalPath: opened.canonical_path,
      runId: opened.run_id,
      bundleVersion: opened.bundle_version,
      title: `${datasetName(manifest.dataset.kind)} / ${manifest.dataset.sequence}`,
      manifest,
      viewerState: saved.viewerState || {},
      viewer: null,
      active: false,
    };
    tab.item = element('div', 'tab-item');
    tab.item.setAttribute('role', 'presentation');
    tab.item.dataset.tabId = tabId;
    tab.button = element('button', 'tab');
    tab.button.type = 'button';
    tab.button.id = `tab-${tabId}`;
    tab.button.setAttribute('role', 'tab');
    tab.button.setAttribute('aria-selected', 'false');
    tab.button.setAttribute('aria-controls', `pane-${tabId}`);
    tab.button.tabIndex = -1;
    tab.button.dataset.path = tab.canonicalPath;
    tab.label = element('span', 'tab__label', tab.title);
    tab.button.appendChild(tab.label);
    tab.marker = element('span', 'tab__marker');
    tab.marker.setAttribute('aria-hidden', 'true');
    tab.close = element('button', 'tab__close', '×');
    tab.close.type = 'button';
    tab.close.tabIndex = -1;
    tab.item.append(tab.marker, tab.button, tab.close);
    tab.button.addEventListener('click', () => { if (!this.dragJustEnded) this.activate(tabId); });
    tab.close.addEventListener('click', (event) => { event.stopPropagation(); this.close(tabId); });
    this.bindTabDrag(tab);

    tab.pane = element('section', 'result-pane');
    tab.pane.id = `pane-${tabId}`;
    tab.pane.setAttribute('role', 'tabpanel');
    tab.pane.setAttribute('aria-labelledby', tab.button.id);
    tab.pane.dataset.path = tab.canonicalPath;
    tab.pane.hidden = true;
    this.el.workspace.appendChild(tab.pane);
    this.el.tabs.appendChild(tab.item);
    this.tabs.set(tabId, tab);
    this.order.push(tabId);
    this.mount(tab);
    this.watch(tab);
    this.renderLabels();
    this.empty.hidden = true;
    return tab;
  }

  mount(tab) {
    if (this.mountViewer) {
      tab.viewer = this.mountViewer(tab, this);
      return;
    }
    const summary = element('div', 'pane-summary');
    const title = element('h2');
    title.append(element('span', `dataset-badge dataset-badge--${tab.manifest.dataset.kind}`, datasetName(tab.manifest.dataset.kind)), element('span', '', tab.manifest.title || basename(tab.canonicalPath)));
    summary.append(title, element('div', 'path', tab.canonicalPath),
      element('div', 'pane-loading', `${tab.manifest.snapshots.count} 个快照 · ${tab.manifest.timebase.first.toFixed(1)}–${tab.manifest.timebase.last.toFixed(1)} s · 回放加载中…`));
    tab.pane.appendChild(summary);
  }

  // The bundle of an open tab was published again: replace the viewer, keep the tab and its saved view.
  async reload(tab, opened) {
    const state = this.viewerState(tab);
    tab.viewer?.destroy?.();
    tab.viewer = null;
    tab.pane.replaceChildren();
    tab.runId = opened.run_id;
    tab.bundleVersion = opened.bundle_version;
    tab.manifest = await this.api.manifest(opened.run_id);
    tab.viewerState = state;
    this.mount(tab);
    this.watch(tab);
    if (tab.active) {
      tab.viewer?.activate?.();
      this.applyGraph(tab);
    }
  }

  // ------------------------------------------------------------------ scene graph
  watch(tab) {
    tab.unwatch?.();
    Object.assign(tab, { graph: null, graphToken: undefined, appliedToken: undefined, appliedName: null, appliedRank: null, graphSeq: 0, notices: [] });
    this.markGraph(tab, false);
    tab.unwatch = this.api.events(tab.runId, {
      state: (data) => this.onRunState(tab, data),
      graph: () => this.refreshGraph(tab),
      'artifact-error': (data) => this.onArtifactError(tab, data),
      closed: (data) => this.onRunClosed(tab, data),
      connection: (info) => this.onConnection(info),
    });
  }

  onRunState(tab, data) {
    const graph = data && data.graph;
    if (!graph) return;
    if (!graph.scanned) { tab.viewer?.setGraphPending?.(); return; }
    const token = graph.graph ? graph.graph.token : null;
    if (tab.graphToken === undefined || token !== tab.graphToken) this.refreshGraph(tab);
  }

  async refreshGraph(tab) {
    const seq = tab.graphSeq = tab.graphSeq + 1;
    let data;
    try {
      data = await this.api.graph(tab.runId);
    } catch (error) {
      if (seq === tab.graphSeq && this.tabs.has(tab.tabId) && !(error instanceof ApiError && error.status === 404)) this.queueNotice(tab, `读取场景图失败:${error.message}`, 'warn');
      return;
    }
    if (seq !== tab.graphSeq || !this.tabs.has(tab.tabId) || !data.scanned) return;   // a newer answer overtook this one, or the tab closed
    tab.graph = data;
    tab.graphToken = data.version ? data.version.token : null;
    this.applyGraph(tab);
  }

  // Bring the viewer to the newest graph the tab has fetched; a background tab only gets its marker.
  applyGraph(tab) {
    const data = tab.graph, viewer = tab.viewer;
    if (!data) return;
    const token = data.version ? data.version.token : null;
    if (tab.appliedToken === token) { this.markGraph(tab, false); return; }
    if (!tab.active || !viewer?.applyGraph) { this.markGraph(tab, true); return; }
    this.markGraph(tab, false);
    const first = tab.appliedToken === undefined, previousName = tab.appliedName, previousRank = tab.appliedRank;
    if (!data.version) {
      viewer.clearGraph();
      if (!first && previousName) viewer.notify('场景图文件已不存在,当前没有可用的场景图', 'warn');
    } else {
      const before = viewer.started ? viewer.serializeState() : null;
      const outcome = viewer.applyGraph(data.document, data.version);
      if (before) viewer.restoreState(before);   // the graph changes entity details only: time, camera and layout stay as they were
      const name = data.version.name;
      let text = `场景图:${name}`;
      if (!first && !previousName) text = `场景图已加载:${name}`;
      else if (!first && previousName === name) text = `场景图已更新:${name}`;
      else if (!first) text = `场景图已${data.version.rank < previousRank ? '升级' : '回退'}:${previousName} → ${name}`;
      viewer.notify(text, 'info');
      if (outcome.selectedEntityMissing) viewer.clearSelection('更新后的场景图中已不存在该实体');
    }
    tab.appliedToken = token;
    tab.appliedName = data.version ? data.version.name : null;
    tab.appliedRank = data.version ? data.version.rank : null;
  }

  markGraph(tab, pending) {
    tab.marker.classList.toggle('graph', pending);
    tab.marker.title = pending ? '场景图有更新:切换到这个标签页时应用' : '';
  }

  onArtifactError(tab, data) {
    const active = data.active ? `,继续使用 ${data.active.name}` : '';
    this.queueNotice(tab, `${data.name} 无效${active}:${data.error}`, 'warn');
  }

  // A message for a tab's pane; a background tab keeps the latest few until it is shown.
  queueNotice(tab, text, kind) {
    if (tab.active && tab.viewer?.notify) { tab.viewer.notify(text, kind); return; }
    tab.notices.push({ text, kind });
    tab.notices.splice(0, Math.max(0, tab.notices.length - MAX_PENDING_NOTICES));
  }

  flushNotices(tab) {
    for (const { text, kind } of tab.notices.splice(0)) tab.viewer?.notify?.(text, kind);
  }

  // The service no longer knows this run (it restarted, or another page closed the result): open it again by its path.
  async onRunClosed(tab, data) {
    if (tab.recovering || !this.tabs.has(tab.tabId)) return;
    tab.recovering = true;
    try {
      const opened = await this.api.open(tab.canonicalPath);
      if (this.tabs.has(tab.tabId)) await this.reload(tab, opened);
    } catch (error) {
      this.notify(`${tab.title}:回看服务不再保留这个结果,重新打开失败\n${error.message}`, 'error', 0);
    } finally {
      tab.recovering = false;
    }
  }

  onConnection(info) {
    if (info.connected === false) {
      if (!this.connectionNotice) this.connectionNotice = this.notify('与回看服务的事件连接中断,正在重连…', 'warn', 0);
    } else if (this.connectionNotice) {
      this.connectionNotice.remove();
      this.connectionNotice = null;
    }
  }

  renderLabels() {
    const counts = new Map();
    for (const tab of this.tabs.values()) counts.set(tab.title, (counts.get(tab.title) || 0) + 1);
    for (const tab of this.tabs.values()) {
      const label = counts.get(tab.title) > 1 ? `${tab.title} · ${basename(tab.canonicalPath)}` : tab.title;
      tab.label.textContent = label;
      tab.button.title = tab.canonicalPath;
      tab.close.setAttribute('aria-label', `关闭 ${label}`);
      tab.close.title = `关闭 ${label}(不删除任何文件)`;
    }
  }

  activate(tabId) {
    const next = this.tabs.get(tabId);
    if (!next) return null;
    const current = this.activeTab();
    if (current && current !== next) {
      current.pane.dispatchEvent(new CustomEvent('replay-tab-deactivated', { detail: { tabId: current.tabId } }));
      current.viewer?.deactivate?.();
      current.active = false;
      current.pane.hidden = true;
      current.button.setAttribute('aria-selected', 'false');
      current.button.tabIndex = -1;
      current.item.classList.remove('active');
    }
    this.activeTabId = tabId;
    if (!next.active) {
      next.pane.dispatchEvent(new CustomEvent('replay-tab-activated', { detail: { tabId } }));
      next.active = true;
      next.pane.hidden = false;
      next.viewer?.activate?.();
      this.applyGraph(next);
      this.flushNotices(next);
    }
    next.button.setAttribute('aria-selected', 'true');
    next.button.tabIndex = 0;
    next.item.classList.add('active');
    next.item.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    this.persist();
    return next;
  }

  async close(tabId) {
    const tab = this.tabs.get(tabId);
    if (!tab) return;
    const index = this.order.indexOf(tabId);
    if (tab.active) tab.pane.dispatchEvent(new CustomEvent('replay-tab-deactivated', { detail: { tabId } }));
    tab.unwatch?.();
    tab.viewer?.destroy?.();
    tab.pane.remove();
    tab.item.remove();
    this.tabs.delete(tabId);
    this.order.splice(index, 1);
    if (this.activeTabId === tabId) {
      this.activeTabId = null;
      const neighbour = this.order[Math.min(index, this.order.length - 1)];
      if (neighbour) this.activate(neighbour);
    }
    this.renderLabels();
    this.empty.hidden = this.tabs.size > 0;
    this.persist();
    try {
      await this.api.close(tab.runId);   // releases the service's run ID only; no file and no history is deleted
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) this.notify(`关闭时服务出错:${error.message}`, 'warn');
    }
  }

  // Put tabId before beforeTabId (null: at the end).
  move(tabId, beforeTabId = null) {
    if (!this.tabs.has(tabId) || tabId === beforeTabId) return;
    this.order.splice(this.order.indexOf(tabId), 1);
    const at = beforeTabId && this.tabs.has(beforeTabId) ? this.order.indexOf(beforeTabId) : this.order.length;
    this.order.splice(at, 0, tabId);
    for (const id of this.order) this.el.tabs.appendChild(this.tabs.get(id).item);
    this.persist();
  }

  onTabKey(event) {
    const tab = [...this.tabs.values()].find((candidate) => candidate.button === document.activeElement);
    if (!tab) return;
    const index = this.order.indexOf(tab.tabId);
    let target = null;
    if (event.key === 'ArrowRight') target = this.order[(index + 1) % this.order.length];
    else if (event.key === 'ArrowLeft') target = this.order[(index - 1 + this.order.length) % this.order.length];
    else if (event.key === 'Home') target = this.order[0];
    else if (event.key === 'End') target = this.order[this.order.length - 1];
    else if (event.key === 'Delete') {
      event.preventDefault();
      this.close(tab.tabId).then(() => this.activeTab()?.button.focus());
      return;
    }
    if (!target) return;
    event.preventDefault();
    this.activate(target);
    this.tabs.get(target).button.focus();
  }

  // Reordering by dragging a tab sideways (pointer events, so mouse, pen and touch behave alike).
  bindTabDrag(tab) {
    tab.item.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.target === tab.close) return;
      const startX = event.clientX;
      let dragging = false;
      let before = null;
      const onMove = (move) => {
        if (!dragging && Math.abs(move.clientX - startX) < 6) return;
        dragging = true;
        tab.item.classList.add('dragging');
        before = null;
        for (const id of this.order) {
          const other = this.tabs.get(id);
          if (other === tab) continue;
          const box = other.item.getBoundingClientRect();
          if (move.clientX < box.left + box.width / 2) { before = id; break; }
        }
        for (const other of this.tabs.values()) other.item.classList.toggle('drop-before', other.tabId === before);
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointercancel', onUp);
        for (const other of this.tabs.values()) other.item.classList.remove('drop-before');
        tab.item.classList.remove('dragging');
        if (dragging) {
          this.move(tab.tabId, before);
          this.dragJustEnded = true;
          setTimeout(() => { this.dragJustEnded = false; }, 0);
        }
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
    });
  }

  // ------------------------------------------------------------------ persistence
  viewerState(tab) {
    try {
      return tab.viewer?.serializeState ? tab.viewer.serializeState() : tab.viewerState;
    } catch (error) {
      return tab.viewerState;
    }
  }

  snapshot() {
    return {
      version: 1,
      tabs: this.order.map((id) => {
        const tab = this.tabs.get(id);
        return { path: tab.canonicalPath, viewerState: this.viewerState(tab) };
      }),
      active: this.activeTab()?.canonicalPath || null,
    };
  }

  persist() {
    clearTimeout(this.persistTimer);
    this.persistTimer = setTimeout(() => this.persistNow(), 250);
  }

  persistNow() {
    clearTimeout(this.persistTimer);
    if (this.restoring) return;
    try {
      this.storage.setItem(STORAGE_KEY, JSON.stringify(this.snapshot()));
    } catch (error) { /* storage full or disabled: the workspace still works for this session */ }
  }

  saved() {
    try {
      const value = JSON.parse(this.storage.getItem(STORAGE_KEY) || 'null');
      return value && value.version === 1 && Array.isArray(value.tabs) ? value : null;
    } catch (error) {
      return null;
    }
  }

  // Reopen the saved paths in their saved order; a path that no longer opens is skipped with a notice.
  async restore() {
    const saved = this.saved();
    if (!saved || !saved.tabs.length) return;
    this.restoring = true;
    const attempts = await Promise.allSettled(saved.tabs.map((entry) => this.api.open(entry.path).then(async (opened) => ({
      opened, manifest: await this.api.manifest(opened.run_id), entry,
    }))));
    for (let i = 0; i < attempts.length; i++) {
      const attempt = attempts[i];
      if (attempt.status === 'fulfilled') {
        if (!this.findByPath(attempt.value.opened.canonical_path)) this.addTab({ opened: attempt.value.opened, manifest: attempt.value.manifest, saved: attempt.value.entry });
      } else {
        this.notify(`已跳过无法重新打开的结果:${saved.tabs[i].path}\n${attempt.reason.message}`, 'warn', 0);
      }
    }
    this.restoring = false;
    const active = saved.active && this.findByPath(saved.active);
    const first = this.order[0];
    if (active) this.activate(active.tabId);
    else if (first) this.activate(first);
    this.persistNow();
  }

  // ------------------------------------------------------------------ notices
  notify(text, kind = 'info', timeout = NOTICE_MS) {
    const notice = element('div', `notice notice--${kind}`);
    notice.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    const body = element('div', 'notice__text', text);
    const close = element('button', 'notice__close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭提示');
    close.addEventListener('click', () => notice.remove());
    notice.append(body, close);
    this.el.status.appendChild(notice);
    while (this.el.status.children.length > 6) this.el.status.firstElementChild.remove();
    if (timeout) setTimeout(() => notice.remove(), timeout);
    return notice;
  }
}

export async function startWorkspace(options = {}) {
  const app = new WorkspaceApp(new ReplayApi(), window.localStorage, options);
  window.replayWorkspace = app;   // handles for debugging in the browser console and for the browser tests
  Object.defineProperty(window, 'replayDebug', { configurable: true, get: () => app.activeTab()?.viewer?.debug });
  await app.start();
  return app;
}

if (document.getElementById('workspace') && !window.replayWorkspace) startWorkspace();
