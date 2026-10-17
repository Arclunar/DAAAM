// The question panel of one result tab: the conversation of the result, the model to ask, whether the current view goes along
// with the question, and the answer as it streams in.
//
// The service owns the conversation (one per result, kept on disk): the panel only shows what it sends and asks for more. Every
// message is rendered with textContent, never as HTML. Each answer shows the model and the scene graph file and version it was
// computed against; when the active graph is not the one the conversation last used, a boundary row says that the next question
// will use the new version. The panel never sees prompts, tool arguments or results, or a key: only the answer text, the number
// of the Agent's round and the name of the scene tool it is using.
//
// Questions can be asked against the last valid graph while a processing job runs, so jobs do not disable the panel. If the page
// stops watching a question ("停止显示") or goes away, the service still finishes it and stores the answer; the panel then picks it
// up from the conversation.

import { ApiError } from './api.js';

const POLL_MS = 2000;
const POLL_LIMIT = 150;   // five minutes of waiting for a question whose stream was left
const ERROR_TEXT = {
  'worker-unavailable': '问答服务没有启动:Docker 可用吗?(它在 daaam-ws 容器里运行)',
  'worker-crashed': '问答进程中途退出了,请重试',
  timeout: '回答超时了,请重试',
  'no-credentials': '没有配置这个模型的 API key:在仓库的 .env 里设置后再试',
  'provider-auth': '模型服务拒绝了 API key,请检查 .env 里的 key',
  'provider-rate-limit': '模型服务限流了,稍后再试',
  'provider-unreachable': '连不上模型服务,请检查网络或代理',
  'model-unavailable': '这个账号用不了所选的模型',
  'query-not-ready': '场景图还不能回答问题:请先运行 postprocess',
  'not-ready': '场景图还不能回答问题:请先运行 postprocess',
  'no-graph': '这个结果还没有场景图',
  'graph-missing': '场景图快照已经不存在,请重试',
  'graph-changing': '场景图正在被替换,稍后再问',
  busy: '这个结果正在回答另一个问题',
  'query-failed': '问答失败了,请重试',
  'invalid-request': '问题被拒绝了',
  disabled: '问答已关闭',
};
const KEY_NAMES = { openai: 'OPENAI_API_KEY', anthropic: 'ANTHROPIC_API_KEY' };

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const shortVersion = (token) => String(token || '').split('-')[1]?.slice(0, 6) || '';
const graphLabel = (name, token) => [name, shortVersion(token)].filter(Boolean).join(' · ');
const errorText = (code, message) => ERROR_TEXT[code] || message || '问答失败了';

export function progressText(progress) {
  switch (progress && progress.kind) {
    case 'queued': return `排队中,前面还有 ${progress.ahead} 个问题`;
    case 'loading': return '正在加载场景图和模型…';
    case 'iteration': return `第 ${progress.number} 轮`;
    case 'tool': return `查询场景: ${progress.name}`;
    case 'final': return '正在整理答案…';
    default: return '';
  }
}

export class ChatPanel {
  // api: the ReplayApi; runId(): the result's current run ID; viewContext(): the view a question may carry (or null);
  // onChange(panel): after the state the tab marker or the tests care about changed
  constructor({ api, runId, viewContext = () => null, onChange = () => {} }) {
    this.api = api;
    this.runId = runId;
    this.viewContext = viewContext;
    this.onChange = onChange;
    this.messages = [];   // the stored conversation rows, oldest first
    this.query = null;   // { ready, reason, graph: { name, token } | null }
    this.agent = null;   // { enabled, models: [{ name, provider, available }], default_model }
    this.model = null;
    this.includeView = true;
    this.busy = false;   // a question of this result is being answered (here or in another window)
    this.turn = null;   // the question this panel is streaming: { id, answer, progress, stopped }
    this.note = '';
    this.noteIsError = false;
    this.confirming = false;
    this.active = false;
    this.alive = true;
    this.pinned = true;   // the newest rows are in view (until the person scrolls up): they stay in view as rows arrive
    this.abort = null;
    this.refreshTimer = 0;
    this.build();
    this.refresh();
  }

  // ------------------------------------------------------------------ model
  async refresh() {   // true when the conversation was read
    let data;
    try {
      data = await this.api.chat(this.runId());
    } catch (error) {
      if (this.alive && !(error instanceof ApiError && error.status === 404)) this.setNote(`读取对话失败:${error.message}`, true);
      return false;
    }
    if (!this.alive) return false;
    this.applyHistory(data);
    return true;
  }

  refreshSoon() {
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh(), 150);
  }

  applyHistory(data) {
    if (!this.turn) this.messages = data.messages || [];   // a question that is streaming appends its own rows
    this.agent = data.agent || this.agent;
    this.query = data.query || this.query;
    this.busy = !!data.busy || !!this.turn;
    this.chooseModel();
    this.afterChange();
  }

  chooseModel() {
    const models = this.agent ? this.agent.models : [];
    if (this.model && models.some((item) => item.name === this.model)) return;
    const preferred = models.find((item) => item.name === this.agent.default_model && item.available) || models.find((item) => item.available) || models[0];
    this.model = preferred ? preferred.name : null;
  }

  // The `chat` part of a state event: whether a question runs and whether the graph can answer one.
  applyState(chat) {
    if (!chat) return;
    this.busy = !!chat.busy || !!this.turn;
    if (chat.query) this.query = chat.query;
    this.afterChange();
  }

  // A question started or ended in some window showing this result.
  onChatEvent(data) {
    if (this.turn && this.turn.id === data.turn) return;   // this panel is streaming it itself
    this.busy = !!data.busy || !!this.turn;
    if (!data.busy) this.refreshSoon();   // its answer is stored now
    this.afterChange();
  }

  setNote(text, isError = false) {
    this.note = text;
    this.noteIsError = isError;
    this.render();
  }

  afterChange() {
    this.render();
    this.onChange(this);
  }

  get unseenBoundary() {
    // The scene graph is not the one the conversation last used: the next question will use the new version.
    if (!this.query || !this.query.graph || !this.messages.length) return null;
    const last = [...this.messages].reverse().find((message) => message.graph_name && message.graph_version);
    if (!last || last.graph_version === this.query.graph.token) return null;
    return { from: graphLabel(last.graph_name, last.graph_version), to: graphLabel(this.query.graph.name, this.query.graph.token) };
  }

  availability() {
    if (!this.agent) return { ok: false, note: '正在读取问答状态…' };
    if (!this.agent.enabled) return { ok: false, note: '问答已关闭(回看服务用了 --no-agent)' };
    const model = this.agent.models.find((item) => item.name === this.model);
    if (!model) return { ok: false, note: '没有可用的模型' };
    if (!model.available) return { ok: false, note: `没有 ${KEY_NAMES[model.provider] || 'API key'}:在仓库的 .env 里设置后再问`, title: '' };
    if (!this.query || !this.query.graph) return { ok: false, note: '这个结果还没有场景图' };
    if (!this.query.ready) return { ok: false, note: '请先运行 postprocess(场景图要有句子嵌入才能提问)', title: this.query.reason || '' };
    return { ok: true, note: '' };
  }

  // ------------------------------------------------------------------ asking
  canSend() {
    return this.availability().ok && !this.busy && !this.turn && this.input.value.trim().length > 0;
  }

  async ask(question = this.input.value) {
    question = question.trim();
    if (!question || !this.availability().ok || this.busy || this.turn) return;
    this.setNote('');
    this.confirming = false;
    const body = { question, model: this.model, include_view_context: this.includeView };
    if (this.includeView) {
      const view = this.viewContext();
      if (view) body.view_context = view;
    }
    const turn = this.turn = { id: null, answer: '', progress: '提问中…', stopped: false };
    this.pinned = true;   // the person just asked: show the question and the answer as it arrives
    const controller = this.abort = new AbortController();
    this.busy = true;
    this.input.value = '';
    this.afterChange();
    let stored = false;
    try {
      await this.api.askChat(this.runId(), body, (event) => { if (this.onTurnEvent(turn, event)) stored = true; }, { signal: controller.signal });
    } catch (error) {
      if (error && error.name === 'AbortError') {
        // the person stopped watching: the service still finishes and stores the answer
        this.turn = null;
        this.abort = null;
        this.setNote('已停止显示:答案生成后会出现在对话里');
        this.awaitStored();
        return;
      }
      // A refusal. Either nothing was stored (no key, not ready, busy, …): the text goes back into the box. Or the worker could not be
      // reached after the question was stored: it is in the conversation now, with an error row to retry from, and not offered twice.
      const known = new Set(this.messages.map((message) => message.message_id));
      this.turn = null;
      this.abort = null;
      if (!(await this.refresh())) this.busy = false;
      const stored = this.messages.some((message) => message.role === 'user' && message.content === question && !known.has(message.message_id));
      if (!stored && !this.input.value) this.input.value = question;
      const unavailable = this.availability();   // what the conversation says now is the better explanation (the missing key's name, run postprocess)
      this.setNote(!unavailable.ok && unavailable.note ? unavailable.note : error instanceof ApiError ? errorText(error.code, error.message) : `提问失败:${error.message}`, true);
      return;
    }
    this.turn = null;
    this.abort = null;
    if (!stored) {   // the stream ended without an answer or an error: look at what the service stored
      this.busy = true;
      this.setNote('与回看服务的连接中断:答案生成后会出现在对话里');
      this.awaitStored();
      return;
    }
    this.busy = false;
    this.afterChange();
    this.refreshSoon();
  }

  // One event of the streamed answer; true once the question has its stored result (an answer or an error row).
  onTurnEvent(turn, { event, data }) {
    if (this.turn !== turn) return false;
    if (event === 'turn') {
      turn.id = data.turn;
      for (const row of [data.boundary, data.user]) if (row && !this.messages.some((message) => message.message_id === row.message_id)) this.messages.push(row);
      turn.progress = '已记录问题,等待回答…';
    } else if (event === 'progress') {
      turn.progress = progressText(data) || turn.progress;
    } else if (event === 'delta') {
      turn.answer += data.text;
    } else if (event === 'complete') {
      this.messages.push(data.message);
      turn.progress = '';
      this.turn = null;
      this.render();
      return true;
    } else if (event === 'error') {
      if (data.row) this.messages.push(data.row);
      this.setNote(errorText(data.code, data.message), true);
      this.turn = null;
      this.render();
      return true;
    }
    this.render();
    return false;
  }

  stop() {
    if (this.abort) this.abort.abort();
  }

  async awaitStored() {
    for (let attempt = 0; attempt < POLL_LIMIT && this.alive; attempt++) {
      try {
        const data = await this.api.chat(this.runId());
        if (!this.alive) return;
        this.applyHistory(data);
        if (!data.busy) {
          this.setNote('');
          return;
        }
      } catch (error) { /* try again */ }
      await sleep(POLL_MS);
    }
    if (this.alive) {
      this.busy = false;
      this.setNote('等不到答案了:稍后重新打开这个标签页再看');
      this.afterChange();
    }
  }

  async clear() {
    this.confirming = false;
    try {
      await this.api.clearChat(this.runId());
    } catch (error) {
      this.setNote(error instanceof ApiError ? errorText(error.code, error.message) : `清空失败:${error.message}`, true);
      return;
    }
    this.messages = [];
    this.setNote('');
    this.refresh();
  }

  setActive(active) {
    this.active = active;
    if (active) {
      this.render();
      if (!this.turn) this.refresh();   // what another window asked meanwhile
    }
  }

  destroy() {
    this.alive = false;
    clearTimeout(this.refreshTimer);
    if (this.abort) this.abort.abort();
    this.el.remove();
  }

  // ------------------------------------------------------------------ view
  build() {
    this.el = element('div', 'chat-panel__body');
    const head = element('div', 'chat-head');
    head.append(element('b', '', '问答'));
    this.statusEl = element('span', 'chat-status');
    this.statusEl.dataset.role = 'chat-status';
    head.append(this.statusEl);
    this.noteEl = element('div', 'chat-note');
    this.noteEl.dataset.role = 'chat-note';
    this.list = element('div', 'chat-messages');
    this.list.dataset.role = 'chat-messages';
    this.list.setAttribute('role', 'log');
    this.list.setAttribute('aria-live', 'polite');
    this.list.addEventListener('scroll', () => {
      if (this.list.clientHeight > 0) this.pinned = this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 24;
    });
    if (typeof ResizeObserver !== 'undefined') {   // the list has a size once its tab and sidebar page are shown: look at the newest rows then
      let shown = false;
      new ResizeObserver(() => {
        const visible = this.list.clientHeight > 0;
        if (visible && !shown && this.pinned) this.list.scrollTop = this.list.scrollHeight;
        shown = visible;
      }).observe(this.list);
    }
    this.progressEl = element('div', 'chat-progress');
    this.progressEl.dataset.role = 'chat-progress';
    const controls = element('div', 'chat-controls');
    const modelLabel = element('label', 'chat-model', '模型 ');
    this.modelSelect = element('select');
    this.modelSelect.dataset.role = 'chat-model';
    this.modelSelect.addEventListener('change', () => { this.model = this.modelSelect.value; this.render(); });
    modelLabel.append(this.modelSelect);
    const contextLabel = element('label', 'chat-context');
    this.contextBox = element('input');
    this.contextBox.type = 'checkbox';
    this.contextBox.checked = true;
    this.contextBox.dataset.role = 'chat-context';
    this.contextBox.addEventListener('change', () => { this.includeView = this.contextBox.checked; });
    contextLabel.append(this.contextBox, document.createTextNode(' 包含当前视图上下文'));
    contextLabel.title = '把时间轴上的时间、选中的实体和 3D 相机位置一起告诉助手';
    controls.append(modelLabel, contextLabel);
    this.input = element('textarea', 'chat-input');
    this.input.dataset.role = 'chat-input';
    this.input.rows = 3;
    this.input.maxLength = 4000;
    this.input.placeholder = '问这个场景…(Ctrl+Enter 发送)';
    this.input.addEventListener('input', () => this.updateButtons());
    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); this.ask(); }
    });
    const actions = element('div', 'chat-actions');
    this.sendButton = element('button', 'task-button', '发送');
    this.sendButton.type = 'button';
    this.sendButton.dataset.role = 'chat-send';
    this.sendButton.addEventListener('click', () => this.ask());
    this.stopButton = element('button', 'task-button task-button--quiet', '停止显示');
    this.stopButton.type = 'button';
    this.stopButton.dataset.role = 'chat-stop';
    this.stopButton.title = '不再等这个答案:助手仍会答完,答案之后出现在对话里';
    this.stopButton.addEventListener('click', () => this.stop());
    this.clearButton = element('button', 'task-button task-button--quiet', '清空对话');
    this.clearButton.type = 'button';
    this.clearButton.dataset.role = 'chat-clear';
    this.clearButton.title = '删除这个结果的全部对话记录(不影响其他结果)';
    this.clearButton.addEventListener('click', () => { this.confirming = true; this.render(); });
    actions.append(this.sendButton, this.stopButton, this.clearButton);
    this.confirmEl = element('div', 'task-confirm');
    this.confirmEl.dataset.role = 'chat-confirm';
    this.confirmEl.hidden = true;
    const confirmText = element('div', 'task-confirm__text', '清空这个结果的全部对话记录?其他结果的对话不受影响,文件不会被删除。');
    const yes = element('button', 'task-button', '确认清空');
    yes.type = 'button';
    yes.dataset.role = 'chat-clear-yes';
    yes.addEventListener('click', () => this.clear());
    const no = element('button', 'task-button task-button--quiet', '取消');
    no.type = 'button';
    no.dataset.role = 'chat-clear-no';
    no.addEventListener('click', () => { this.confirming = false; this.render(); });
    const buttons = element('div', 'task-confirm__buttons');
    buttons.append(yes, no);
    this.confirmEl.append(confirmText, buttons);
    this.el.append(head, this.noteEl, this.list, this.progressEl, controls, this.input, actions, this.confirmEl);
  }

  attach(container) {
    container.replaceChildren(this.el);
    this.render();
  }

  render() {
    if (!this.active || !this.el.isConnected) return;
    this.renderModels();
    this.renderMessages();
    this.renderStatus();
    this.confirmEl.hidden = !this.confirming;
    this.updateButtons();
  }

  renderModels() {
    const models = this.agent ? this.agent.models : [];
    const signature = models.map((item) => `${item.name}:${item.available}`).join('|');
    if (this.modelSelect.dataset.signature !== signature) {
      this.modelSelect.dataset.signature = signature;
      this.modelSelect.replaceChildren(...models.map((item) => {
        const option = element('option', '', item.available ? item.name : `${item.name}(没有 key)`);
        option.value = item.name;
        option.disabled = !item.available;
        return option;
      }));
    }
    if (this.model) this.modelSelect.value = this.model;
    this.modelSelect.disabled = !!this.turn || this.busy;
  }

  renderStatus() {
    const availability = this.availability();
    const waiting = this.turn ? this.turn.progress : this.busy ? '正在回答上一个问题…' : '';
    this.statusEl.textContent = waiting || (availability.ok ? '可以提问' : '');
    this.statusEl.dataset.state = this.turn || this.busy ? 'busy' : availability.ok ? 'ready' : 'unavailable';
    this.progressEl.textContent = this.turn ? this.turn.progress : '';
    this.progressEl.hidden = !this.turn;
    const notes = [];
    if (this.note) notes.push(this.note);
    else if (!availability.ok && availability.note) notes.push(availability.note);
    this.noteEl.textContent = notes.join('\n');
    this.noteEl.classList.toggle('task-note--error', this.noteIsError && !!this.note);
    this.noteEl.title = availability.title || '';
  }

  updateButtons() {
    const availability = this.availability();
    this.sendButton.disabled = !this.canSend();
    this.sendButton.title = availability.ok ? 'Ctrl+Enter 发送' : availability.note;
    this.sendButton.hidden = !!this.turn;
    this.stopButton.hidden = !this.turn;
    this.input.disabled = !this.agent || !this.agent.enabled;
    this.clearButton.disabled = !this.messages.length || !!this.turn || this.busy;
  }

  renderMessages() {
    const stick = this.pinned;
    this.list.replaceChildren();
    if (!this.messages.length && !this.turn) this.list.append(element('div', 'chat-empty', '还没有对话。问一个关于这个场景的问题,比如“桌子旁边有什么?”'));
    this.messages.forEach((message, index) => this.list.append(this.messageElement(message, index)));
    if (this.turn && !this.turn.stopped) {
      const pending = element('div', 'chat-message chat-message--assistant chat-message--pending');
      pending.dataset.role = 'chat-message';
      pending.dataset.messageRole = 'assistant';
      pending.append(element('div', 'chat-message__head', '助手'), element('div', 'chat-message__body', this.turn.answer || '…'));
      this.list.append(pending);
    }
    const boundary = this.turn ? null : this.unseenBoundary;
    if (boundary) {
      const row = element('div', 'chat-boundary chat-boundary--pending', `场景图已更新:${boundary.from} → ${boundary.to}。下一个问题会用新的版本。`);
      row.dataset.role = 'chat-boundary-pending';
      this.list.append(row);
    }
    if (stick && this.list.clientHeight > 0) this.list.scrollTop = this.list.scrollHeight;
  }

  messageElement(message, index) {
    const role = message.role;
    const row = element('div', `chat-message chat-message--${role}`);
    row.dataset.role = role === 'boundary' ? 'chat-boundary' : 'chat-message';
    row.dataset.messageRole = role;
    row.dataset.messageId = message.message_id;
    row.title = new Date(message.created_at * 1000).toLocaleString();
    if (role === 'boundary') {
      const from = message.context && message.context.from;
      row.className = 'chat-boundary';
      row.textContent = `场景图已更新:${from ? graphLabel(from.name, from.version) + ' → ' : ''}${graphLabel(message.graph_name, message.graph_version)}`;
      return row;
    }
    const head = element('div', 'chat-message__head');
    const names = { user: '你', assistant: '助手', error: '出错' };
    head.append(element('span', 'chat-message__who', names[role] || role));
    if (role === 'assistant' && message.model) head.append(element('span', 'chat-message__meta', ` · ${message.model}`));
    if (message.graph_name && role !== 'error') {
      const meta = element('span', 'chat-message__meta', ` · ${graphLabel(message.graph_name, message.graph_version)}`);
      meta.dataset.role = 'chat-graph';
      head.append(meta);
    }
    if (role === 'user' && message.context && Object.keys(message.context).length) {
      const chip = element('span', 'chat-message__chip', '含当前视图');
      chip.title = JSON.stringify(message.context);
      head.append(chip);
    }
    const body = element('div', 'chat-message__body', role === 'error' ? errorText(message.context && message.context.code, message.content) : message.content);
    row.append(head, body);
    if (role === 'error') {
      const asked = this.messages.slice(0, index).reverse().find((candidate) => candidate.role === 'user');
      if (asked) {
        const retry = element('button', 'task-button task-button--quiet chat-retry', '重试');
        retry.type = 'button';
        retry.dataset.role = 'chat-retry';
        retry.addEventListener('click', () => this.ask(asked.content));
        retry.disabled = this.busy || !!this.turn;
        row.append(retry);
      }
    }
    return row;
  }
}
