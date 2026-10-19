// Snapshot data of one open result, fetched from its replay bundle in bounded chunks.
//
// A timeline chunk (timeline/NNNNNN.json.gz, served with Content-Encoding: gzip, so the browser inflates it) holds
// {"snapshots": [{n, t, p}]}, where p is the base64 of a deflated packed-array payload written by scripts/replay.py
// pack(): [uint32 header length][JSON header][arrays, 8-byte aligned]. Decoded snapshots and raw chunks are kept in
// small LRU caches; only neighbouring chunks are prefetched, and only while the tab is active.

const TYPES = { f4: Float32Array, i4: Int32Array, u4: Uint32Array, i2: Int16Array, u2: Uint16Array, u1: Uint8Array };

export async function inflateBase64(b64) {
  const bin = atob(b64), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
  return new Response(stream).arrayBuffer();
}

export function unpack(buf) {   // layout written by pack() in replay.py
  const headLen = new DataView(buf).getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headLen)));
  const base = 4 + headLen, out = {};
  for (const h of header) out[h.n] = new TYPES[h.t](buf, base + h.o, h.s.reduce((a, b) => a * b, 1));
  return out;
}

export function vertexNormals(pos, idx) {
  const n = new Float32Array(pos.length);
  for (let t = 0; t < idx.length; t += 3) {
    const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
    const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2];
    const vx = pos[c] - pos[a], vy = pos[c + 1] - pos[a + 1], vz = pos[c + 2] - pos[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    n[a] += nx; n[a + 1] += ny; n[a + 2] += nz;
    n[b] += nx; n[b + 1] += ny; n[b + 2] += nz;
    n[c] += nx; n[c + 1] += ny; n[c + 2] += nz;
  }
  for (let i = 0; i < n.length; i += 3) {
    const l = Math.hypot(n[i], n[i + 1], n[i + 2]) || 1;
    n[i] /= l; n[i + 1] /= l; n[i + 2] /= l;
  }
  return n;
}

function dequantize(quantized, quant) {
  const nv = quantized.length / 3, pos = new Float32Array(nv * 3);
  for (let k = 0; k < nv; k++) {
    pos[3 * k] = quant.min[0] + (quantized[3 * k] + 32768) * quant.scale[0];
    pos[3 * k + 1] = quant.min[1] + (quantized[3 * k + 1] + 32768) * quant.scale[1];
    pos[3 * k + 2] = quant.min[2] + (quantized[3 * k + 2] + 32768) * quant.scale[2];
  }
  return pos;
}

class Lru {
  constructor(limit) { this.limit = limit; this.map = new Map(); }
  get(key) {
    if (!this.map.has(key)) return undefined;
    const value = this.map.get(key);
    this.map.delete(key);
    this.map.set(key, value);
    return value;
  }
  set(key, value) {
    this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.limit) this.map.delete(this.map.keys().next().value);
  }
  clear() { this.map.clear(); }
  get size() { return this.map.size; }
}

export class ChunkError extends Error {
  constructor(chunk, message) {
    super(`时间轴数据块 ${chunk.path} 无法读取:${message}`);
    this.chunk = chunk;
  }
}

export class ReplayDataSource {
  constructor(api, runId, manifest, version, { maxChunks = 3, maxSnapshots = 12 } = {}) {
    this.api = api;
    this.runId = runId;
    this.manifest = manifest;
    this.version = version;
    this.maxChunks = maxChunks;
    this.chunks = new Lru(maxChunks);   // chunk index -> Promise of its records
    this.decoded = new Lru(maxSnapshots);   // snapshot index -> Promise of the decoded snapshot
    this.paused = false;
    this.snapshots = manifest.snapshots.numbers.map((n, i) => ({ n, t: manifest.snapshots.times[i] }));
  }

  get count() { return this.snapshots.length; }

  chunkIndexOf(index) {
    const chunks = this.manifest.chunks;
    let lo = 0, hi = chunks.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, chunk = chunks[mid];
      if (index < chunk.first_snapshot) hi = mid - 1;
      else if (index >= chunk.first_snapshot + chunk.snapshot_count) lo = mid + 1;
      else return mid;
    }
    return -1;
  }

  loadChunk(chunkIndex) {
    let pending = this.chunks.get(chunkIndex);
    if (!pending) {
      const chunk = this.manifest.chunks[chunkIndex];
      pending = fetch(this.api.chunkUrl(this.runId, chunkIndex, this.version), { credentials: 'same-origin' })
        .then(async (response) => {
          if (!response.ok) throw new ChunkError(chunk, `HTTP ${response.status}`);
          const data = await response.json();
          if (!data || !Array.isArray(data.snapshots) || data.snapshots.length !== chunk.snapshot_count) throw new ChunkError(chunk, '内容与清单不符');
          return data.snapshots;
        })
        .catch((error) => {
          if (this.chunks.map.get(chunkIndex) === pending) this.chunks.map.delete(chunkIndex);   // a later attempt may succeed
          throw error instanceof ChunkError ? error : new ChunkError(chunk, error.message);
        });
      this.chunks.set(chunkIndex, pending);
    }
    return pending;
  }

  async decode(index) {
    const chunkIndex = this.chunkIndexOf(index);
    if (chunkIndex < 0) throw new RangeError(`snapshot ${index} is outside the replay`);
    const records = await this.loadChunk(chunkIndex);
    const record = records[index - this.manifest.chunks[chunkIndex].first_snapshot];
    let a;
    try {
      a = unpack(await inflateBase64(record.p));
    } catch (error) {
      throw new ChunkError(this.manifest.chunks[chunkIndex], `快照 ${record.n} 解码失败(${error.message})`);
    }
    a.pos = dequantize(a.mesh_pos, this.manifest.quant);
    a.nrm = vertexNormals(a.pos, a.mesh_faces);
    return a;
  }

  // The decoded snapshot at a timeline index.
  snapshot(index) {
    let pending = this.decoded.get(index);
    if (!pending) {
      pending = this.decode(index);
      pending.catch(() => { if (this.decoded.map.get(index) === pending) this.decoded.map.delete(index); });
      this.decoded.set(index, pending);
    }
    return pending;
  }

  // Fetch (not decode) the chunks just before and after the one holding `index`.
  prefetchAround(index) {
    if (this.paused) return;
    const at = this.chunkIndexOf(index);
    for (const next of [at + 1, at - 1]) {
      if (next >= 0 && next < this.manifest.chunks.length && !this.chunks.map.has(next)) this.loadChunk(next).catch(() => {});
    }
  }

  image(kind, index) {
    const entry = this.manifest.images.timeline[index];
    const asset = entry && entry[kind];
    return asset ? this.api.imageUrl(this.runId, asset, this.version) : null;
  }

  frameOf(index) {
    const entry = this.manifest.images.timeline[index];
    return entry ? entry.index : null;
  }

  exactFrameImage(index) {   // source frame is a dataset index; never substitute a nearby snapshot
    const entry = this.manifest.images.timeline.find((item) => item.index === index && item.rgb);
    if (entry) return { camera: 'rgb', index, url: this.api.imageUrl(this.runId, entry.rgb, this.version) };
    const description = Object.values(this.manifest.images.descriptions).find((item) => item.index === index);
    return description ? { ...description, url: this.api.imageUrl(this.runId, description.path, this.version) } : null;
  }

  descriptionImage(frameId) {
    const entry = this.manifest.images.descriptions[String(frameId)];
    return entry ? { ...entry, url: this.api.imageUrl(this.runId, entry.path, this.version) } : null;
  }

  // The static 1 cm reference mesh: one payload with its own quantization grid and int16 normals, decoded once.
  referenceMesh() {
    if (!this.manifest.reference_mesh) return Promise.resolve(null);
    if (!this.reference) {
      this.reference = fetch(this.api.referenceUrl(this.runId, this.version), { credentials: 'same-origin' })
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .then(async (ref) => {
          const a = unpack(await inflateBase64(ref.chunk));
          const pos = dequantize(a.mesh_pos, ref.quant), nv = pos.length / 3;
          let nrm;
          if (a.mesh_nrm) { nrm = new Float32Array(a.mesh_nrm.length); for (let k = 0; k < nrm.length; k++) nrm[k] = a.mesh_nrm[k] / 32767; }
          else nrm = vertexNormals(pos, a.mesh_faces);   // a payload without normals: compute them as for the dynamic mesh
          return { pos, nrm, col: a.mesh_col, idx: a.mesh_faces, count: nv, tris: a.mesh_faces.length / 3, quant: ref.quant, label: ref.label };
        });
      this.reference.catch(() => { this.reference = null; });
    }
    return this.reference;
  }

  // Drop decoded data (an inactive or closed tab); the manifest and the static reference stay.
  clearDecoded() {
    this.decoded.clear();
    this.chunks.clear();
  }
}
