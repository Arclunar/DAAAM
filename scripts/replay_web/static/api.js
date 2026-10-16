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

export class ReplayApi {
  constructor(fetchImpl = (...args) => window.fetch(...args)) {
    this.fetch = fetchImpl;
    this.settings = null;
  }

  // JSON request; mutations carry the startup CSRF token, failures become ApiError(status, message).
  async request(path, { method = 'GET', body } = {}) {
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

  chunkUrl(runId, index, version) { return withVersion(`${run(runId)}/timeline/${index}`, version); }
  imageUrl(runId, asset, version) { return withVersion(`${run(runId)}/images/${encodeURIComponent(asset)}`, version); }
  referenceUrl(runId, version) { return withVersion(`${run(runId)}/reference`, version); }
}
