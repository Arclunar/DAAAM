// Client of the replay workspace service (scripts/replay_web/server.py). The browser names a result by its path only
// when opening it; every later request uses the opaque run ID the service returned.

export class ApiError extends Error {
  constructor(status, message, code = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const run = (runId) => `/api/runs/${encodeURIComponent(runId)}`;
const withVersion = (url, version) => (version ? `${url}?v=${encodeURIComponent(version)}` : url);

// The event types the service sends (scripts/replay_web/events.py); a handler object names the ones it wants.
const EVENT_TYPES = ['state', 'graph', 'artifact-error', 'closed', 'job-state', 'job-log'];

// One server-sent events connection for all the results this page has open. A browser allows only a handful of
// connections per origin, so a stream per tab would starve the chunk and image requests; instead the single stream is
// re-opened, with the service sending the state of every result first, whenever the set of watched results changes.
// The browser reconnects by itself after a dropped connection and sends the last event id it saw.
export class EventHub {
  constructor(makeSource = (url) => new window.EventSource(url)) {
    this.makeSource = makeSource;
    this.watchers = new Map();   // run ID -> Set of handler objects
    this.source = null;
    this.key = '';
    this.scheduled = false;
    this.connected = null;
  }

  // handlers: { state(data), graph(data), 'artifact-error'(data), closed(data), 'job-state'(data), 'job-log'(data), connection({connected}) };
  // returns the unsubscribe function
  subscribe(runId, handlers) {
    let handlerSet = this.watchers.get(runId);
    if (!handlerSet) { handlerSet = new Set(); this.watchers.set(runId, handlerSet); }
    handlerSet.add(handlers);
    this.schedule();
    return () => {
      const current = this.watchers.get(runId);
      if (!current || !current.delete(handlers)) return;
      if (!current.size) this.watchers.delete(runId);
      this.schedule();
    };
  }

  schedule() {   // several tabs subscribing together (a restore) cost one connection
    if (this.scheduled) return;
    this.scheduled = true;
    Promise.resolve().then(() => { this.scheduled = false; this.sync(); });
  }

  sync() {
    const runs = [...this.watchers.keys()].sort();
    const key = runs.join(',');
    if (this.source && key === this.key) return;
    this.disconnect();
    if (!runs.length) return;
    this.key = key;
    const source = this.makeSource(`/api/events?${runs.map((id) => `run=${encodeURIComponent(id)}`).join('&')}`);
    this.source = source;
    for (const type of EVENT_TYPES) source.addEventListener(type, (event) => this.dispatch(type, event));
    source.onopen = () => this.setConnected(true);
    source.onerror = () => this.setConnected(false);
  }

  disconnect() {
    if (this.source) this.source.close();
    this.source = null;
    this.key = '';
    this.connected = null;
  }

  setConnected(connected) {
    if (this.connected === connected) return;
    this.connected = connected;
    for (const handlerSet of [...this.watchers.values()]) {
      for (const handlers of [...handlerSet]) this.call(handlers, 'connection', { connected });
    }
  }

  dispatch(type, event) {
    let data;
    try { data = JSON.parse(event.data); } catch (error) { return; }
    const handlerSet = data && this.watchers.get(data.run_id);
    if (!handlerSet) return;
    for (const handlers of [...handlerSet]) this.call(handlers, type, data);
  }

  call(handlers, type, data) {
    const handler = handlers[type];
    if (!handler) return;
    try { handler(data); } catch (error) { console.error(`replay event handler "${type}" failed`, error); }
  }
}

export class ReplayApi {
  constructor(fetchImpl = (...args) => window.fetch(...args)) {
    this.fetch = fetchImpl;
    this.settings = null;
    this.hub = null;
  }

  // JSON request; mutations carry the startup CSRF token, failures become ApiError(status, message). A service that
  // restarted has a new token: a mutation it refuses for that is sent once more with the token read again.
  async request(path, { method = 'GET', body } = {}, tokenRefreshed = false) {
    const mutating = method !== 'GET' && method !== 'HEAD';
    if (mutating && !this.settings) await this.config();
    const headers = { Accept: 'application/json' };
    if (mutating) headers['X-Replay-CSRF'] = this.settings.csrf_token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await this.fetch(path, {
        method, headers, credentials: 'same-origin', cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      throw new ApiError(0, '回看服务没有响应:它还在运行吗?(just replay-web)', 'network');
    }
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      let code = null;
      try {
        const data = await response.json();
        if (data && data.error) message = data.error;
        if (data && data.code) code = data.code;
      } catch (error) { /* not JSON */ }
      if (mutating && code === 'csrf' && !tokenRefreshed) {
        await this.config();
        return this.request(path, { method, body }, true);
      }
      throw new ApiError(response.status, message, code);
    }
    return response.status === 204 ? null : response.json();
  }

  async config() {
    this.settings = await this.request('/api/config');
    return this.settings;
  }

  async discover() { return this.request('/api/results'); }
  async open(path) { return this.request('/api/runs/open', { method: 'POST', body: { path } }); }
  async close(runId) { return this.request(run(runId), { method: 'DELETE' }); }
  async manifest(runId) { return this.request(`${run(runId)}/manifest`); }
  // The active scene graph: { scanned, version, document, artifacts, priority }; version and document are null without a graph.
  async graph(runId) { return this.request(`${run(runId)}/graph`); }
  // The processing jobs of a result and what each stage may do now: { jobs, logs, actions }.
  async jobs(runId) { return this.request(`${run(runId)}/jobs`); }
  // stage: 'postprocess' | 'summarize' | 'all'; replace: the person agreed to replace the outputs that exist already.
  async startJob(runId, stage, replace = false) { return this.request(`${run(runId)}/jobs`, { method: 'POST', body: { stage, replace } }); }
  async cancelJob(jobId) { return this.request(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: {} }); }
  // Follow what changes in an open result (see EventHub.subscribe); returns the function that stops following it.
  events(runId, handlers) {
    if (!this.hub) this.hub = new EventHub();
    return this.hub.subscribe(runId, handlers);
  }

  chunkUrl(runId, index, version) { return withVersion(`${run(runId)}/timeline/${index}`, version); }
  imageUrl(runId, asset, version) { return withVersion(`${run(runId)}/images/${encodeURIComponent(asset)}`, version); }
  referenceUrl(runId, version) { return withVersion(`${run(runId)}/reference`, version); }
}
