// The task panel of one result tab: Postprocess, Summarize and "全部运行", their progress and the output of the latest job.
//
// The service decides what each stage may do (the `actions` it sends with the jobs: available, confirm, reason) and the
// panel only follows it; nothing here guesses from file names. A stage over existing outputs asks the person first
// (`重新运行`). The model (jobs, bounded log lines) is kept up to date from the service's events even while the tab is in
// the background; the DOM is only rebuilt while the tab is shown.

import { ApiError } from './api.js';

export const MAX_LOG_LINES = 500;
const MAX_JOBS = 6;
const STAGES = [
  { stage: 'postprocess', label: 'Postprocess', role: 'task-postprocess', title: '生成句向量并聚类成房间(dsg_updated.json、clustered_dsg.json)' },
  { stage: 'summarize', label: 'Summarize', role: 'task-summarize', title: '用 OpenAI 为每个房间写摘要(需要先 Postprocess 和 OPENAI_API_KEY)' },
  { stage: 'all', label: '全部运行', role: 'task-all', title: '依次运行 Postprocess 和 Summarize,第一步失败就停止' },
];
const STATE_NAMES = { queued: '排队中', running: '运行中', succeeded: '成功', failed: '失败', cancelled: '已取消', interrupted: '已中断' };
const STATE_RANK = { queued: 0, running: 1, succeeded: 2, failed: 2, cancelled: 2, interrupted: 2 };
const stageLabel = (stage) => (STAGES.find((item) => item.stage === stage) || { label: stage }).label;
const unfinished = (job) => job.state === 'queued' || job.state === 'running';

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

export class TaskPanel {
  // api: the ReplayApi; runId(): the result's current run ID; onChange(panel): after the jobs changed (tab markers)
  constructor({ api, runId, onChange = () => {} }) {
    this.api = api;
    this.runId = runId;
    this.onChange = onChange;
    this.jobs = [];   // newest first
    this.logs = new Map();   // job ID -> at most MAX_LOG_LINES lines
    this.actions = {};
    this.pending = null;   // a stage waiting for the person's confirmation
    this.error = null;
    this.requesting = false;
    this.unseen = null;   // 'succeeded' | 'failed': a job ended while the tab was in the background
    this.active = false;
    this.refreshTimer = 0;
    this.build();
  }

  // ------------------------------------------------------------------ model
  applyState(state) {
    if (!state) return;
    this.jobs = (state.jobs || []).slice(0, MAX_JOBS);
    this.actions = state.actions || {};
    for (const [jobId, lines] of Object.entries(state.logs || {})) this.logs.set(jobId, lines.slice(-MAX_LOG_LINES));
    this.afterChange();
  }

  onJobState(job) {
    const known = this.jobs.find((item) => item.job_id === job.job_id);
    if (known && STATE_RANK[job.state] < STATE_RANK[known.state]) return;   // an older event overtaken by a newer one
    const wasUnfinished = known ? unfinished(known) : false;
    this.jobs = [job, ...this.jobs.filter((item) => item.job_id !== job.job_id)].sort((a, b) => b.created - a.created).slice(0, MAX_JOBS);
    if (!unfinished(job) && (wasUnfinished || !known) && !this.active) this.unseen = job.state === 'succeeded' ? 'succeeded' : 'failed';
    if (!known || known.state !== job.state) this.refreshSoon();   // a job started or ended: what each stage may do now changed
    this.afterChange();
  }

  onJobLog({ job_id: jobId, lines }) {
    const kept = (this.logs.get(jobId) || []).concat(lines);
    this.logs.set(jobId, kept.length > MAX_LOG_LINES ? kept.slice(-MAX_LOG_LINES) : kept);
    if (this.jobs[0] && this.jobs[0].job_id === jobId) this.renderLog();
  }

  async refresh() {
    try {
      this.applyState(await this.api.jobs(this.runId()));
    } catch (error) { /* the next event brings the state again */ }
  }

  refreshSoon() {
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh(), 150);
  }

  afterChange() {
    for (const jobId of [...this.logs.keys()]) if (!this.jobs.some((job) => job.job_id === jobId)) this.logs.delete(jobId);
    if (this.pending && !(this.actions[this.pending] && this.actions[this.pending].available)) this.pending = null;
    this.render();
    this.onChange(this);
  }

  get running() { return this.jobs.some(unfinished); }

  setActive(active) {
    this.active = active;
    if (active) {
      this.unseen = null;
      this.render();
      this.onChange(this);
    }
  }

  destroy() {
    clearTimeout(this.refreshTimer);
    this.el.remove();
  }

  // ------------------------------------------------------------------ actions
  choose(stage) {
    const action = this.actions[stage];
    if (!action || !action.available || this.requesting) return;
    this.error = null;
    if (action.confirm) {
      this.pending = stage;
      this.render();
    } else {
      this.start(stage, false);
    }
  }

  async start(stage, replace) {
    this.pending = null;
    this.error = null;
    this.requesting = true;
    this.render();
    try {
      const { job } = await this.api.startJob(this.runId(), stage, replace);
      this.onJobState(job);
    } catch (error) {
      if (error instanceof ApiError && error.code === 'confirm') this.pending = stage;
      else this.error = error.message;
      this.refreshSoon();
    } finally {
      this.requesting = false;
      this.render();
    }
  }

  async cancel() {
    const job = this.jobs.find(unfinished);
    if (!job || job.cancel_requested || this.requesting) return;
    this.requesting = true;
    this.render();
    try {
      const { job: updated } = await this.api.cancelJob(job.job_id);
      this.onJobState(updated);
    } catch (error) {
      this.error = error.message;
      this.refreshSoon();
    } finally {
      this.requesting = false;
      this.render();
    }
  }

  // ------------------------------------------------------------------ view
  build() {
    this.el = element('div', 'task-panel__body');
    const head = element('div', 'task-head');
    head.append(element('b', '', '处理任务'));
    this.statusEl = element('span', 'task-status');
    this.statusEl.dataset.role = 'task-status';
    head.append(this.statusEl);
    this.actionsEl = element('div', 'task-actions');
    this.buttons = {};
    for (const item of STAGES) {
      const button = element('button', 'task-button', item.label);
      button.type = 'button';
      button.dataset.role = item.role;
      button.dataset.stage = item.stage;
      button.title = item.title;
      button.addEventListener('click', () => this.choose(item.stage));
      this.buttons[item.stage] = button;
      this.actionsEl.append(button);
    }
    this.cancelEl = element('button', 'task-button task-cancel', '取消');
    this.cancelEl.type = 'button';
    this.cancelEl.dataset.role = 'task-cancel';
    this.cancelEl.title = '请求停止正在运行的任务:先优雅终止,10 秒后再停止它的容器';
    this.cancelEl.addEventListener('click', () => this.cancel());
    this.actionsEl.append(this.cancelEl);
    this.noteEl = element('div', 'task-note');
    this.noteEl.dataset.role = 'task-note';
    this.confirmEl = element('div', 'task-confirm');
    this.confirmEl.dataset.role = 'task-confirm';
    this.confirmEl.hidden = true;
    this.confirmText = element('div', 'task-confirm__text');
    const yes = element('button', 'task-button', '确认重新运行');
    yes.type = 'button';
    yes.dataset.role = 'task-confirm-yes';
    yes.addEventListener('click', () => this.pending && this.start(this.pending, true));
    const no = element('button', 'task-button task-button--quiet', '取消');
    no.type = 'button';
    no.dataset.role = 'task-confirm-no';
    no.addEventListener('click', () => { this.pending = null; this.render(); });
    const confirmButtons = element('div', 'task-confirm__buttons');
    confirmButtons.append(yes, no);
    this.confirmEl.append(this.confirmText, confirmButtons);
    this.jobsEl = element('ul', 'task-jobs');
    this.jobsEl.dataset.role = 'task-jobs';
    this.logEl = element('pre', 'task-log');
    this.logEl.dataset.role = 'task-log';
    this.logEl.tabIndex = 0;
    this.el.append(head, this.actionsEl, this.noteEl, this.confirmEl, this.jobsEl, this.logEl);
  }

  attach(container) {
    container.replaceChildren(this.el);
    this.render();
  }

  render() {
    if (!this.active || !this.el.isConnected) return;
    const job = this.jobs.find(unfinished);
    for (const item of STAGES) {
      const action = this.actions[item.stage];
      const button = this.buttons[item.stage];
      button.disabled = !action || !action.available || this.requesting || !!job;   // one job per result at a time
      button.classList.toggle('needs-confirm', !!(action && action.confirm));
      button.textContent = action && action.confirm ? `重新运行 ${item.label}` : item.label;
      button.title = action && action.reason ? action.reason : item.title;
    }
    this.cancelEl.disabled = !job || !!job.cancel_requested || this.requesting;
    this.statusEl.textContent = job ? `${stageLabel(job.stage)} · ${job.cancel_requested ? '正在取消…' : STATE_NAMES[job.state]}` : '空闲';
    this.statusEl.dataset.state = job ? job.state : 'idle';
    this.confirmEl.hidden = !this.pending;
    if (this.pending) {
      const action = this.actions[this.pending] || { existing: [] };
      this.confirmText.textContent = `${stageLabel(this.pending)} 已经生成过 ${action.existing.join('、')}。重新运行会在成功后整体替换它们(失败或取消不会改动现有文件)。`;
    }
    const reasons = [];
    if (this.error) reasons.push(this.error);
    if (!job) {
      for (const item of STAGES) {
        const action = this.actions[item.stage];
        if (action && !action.available && action.reason && !reasons.includes(action.reason)) reasons.push(action.reason);
      }
    }
    this.noteEl.textContent = reasons.join('\n');
    this.noteEl.classList.toggle('task-note--error', !!this.error);
    this.renderJobs();
    this.renderLog();
  }

  renderJobs() {
    this.jobsEl.replaceChildren();
    if (!this.jobs.length) this.jobsEl.append(element('li', 'task-empty', '还没有运行过任务'));
    for (const job of this.jobs) {
      const item = element('li', `task-job task-job--${job.state}`);
      item.dataset.jobId = job.job_id;
      item.dataset.state = job.state;
      const title = element('span', 'task-job__title', `${stageLabel(job.stage)} · ${job.cancel_requested && unfinished(job) ? '正在取消…' : STATE_NAMES[job.state]}`);
      const when = element('span', 'task-job__time', new Date(job.created * 1000).toLocaleTimeString());
      item.append(title, when);
      const detail = job.state === 'failed' || job.state === 'interrupted' ? [job.message].filter(Boolean).join('') : '';
      if (detail) item.append(element('div', 'task-job__detail', detail));
      if (job.state === 'failed') {
        const retry = element('button', 'task-button task-button--quiet task-retry', '重试');
        retry.type = 'button';
        retry.dataset.role = 'task-retry';
        retry.addEventListener('click', () => this.choose(job.stage));
        item.append(retry);
      }
      this.jobsEl.append(item);
    }
  }

  renderLog() {
    if (!this.active || !this.el.isConnected) return;
    const job = this.jobs[0];
    const lines = job ? this.logs.get(job.job_id) || [] : [];
    const stick = this.logEl.scrollTop + this.logEl.clientHeight >= this.logEl.scrollHeight - 8;
    this.logEl.textContent = lines.join('\n');
    this.logEl.dataset.jobId = job ? job.job_id : '';
    if (stick) this.logEl.scrollTop = this.logEl.scrollHeight;
  }
}
