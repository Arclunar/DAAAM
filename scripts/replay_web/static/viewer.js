// The WebGL replay viewer of one result tab. One instance per tab: it renders, animates and decodes images only
// while its tab is active (activate / deactivate), and serializes its view so that the workspace can restore it.
//
// Rendering, picking, labels and the selection overlay are those of the former self-contained replay page, unchanged
// in behaviour: plain WebGL 1, no libraries. Selecting an Object or Place (map or list) never moves the timeline, and the
// description-source modal shows the exact frame DAM described an object from, without moving it either.

import { ChunkError } from './replay_data.js';

const AMBER = '#ffb000', AMBER_RGB = [1, 176 / 255, 0], HALO = 'rgba(8, 10, 14, 0.94)';   // the selection: vivid amber on a near-black halo
const BOX_EDGES = [[0, 1], [1, 3], [3, 2], [2, 0], [4, 5], [5, 7], [7, 6], [6, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
const CLIP_PLANES = [
  (p) => p[3] + p[0], (p) => p[3] - p[0], (p) => p[3] + p[1],
  (p) => p[3] - p[1], (p) => p[3] + p[2], (p) => p[3] - p[2],
];
const CONTROLS = ['mesh-mode', 'c-bright', 'c-points', 'c-obj', 'bbox-mode', 'label-mode', 'c-places', 'c-pedges', 'c-rooms', 'c-links', 'c-traj', 'c-cam', 'r-size', 'r-space'];
const fmt = (n) => n.toLocaleString('en-US');
const entityKey = (kind, id) => kind + ':' + id;
const rgb = (color) => 'rgb(' + color.map((v) => Math.round(Math.min(1, v) * 255)).join(',') + ')';
const row = (a, i) => [a[3 * i], a[3 * i + 1], a[3 * i + 2]];

// ------------------------------------------------------------------ math (column-major, as WebGL wants it)
function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
}
function lookAt(eye, c, up) {
  let zx = eye[0] - c[0], zy = eye[1] - c[1], zz = eye[2] - c[2], l = Math.hypot(zx, zy, zz);
  zx /= l; zy /= l; zz /= l;
  let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
  l = Math.hypot(xx, xy, xz); xx /= l; xy /= l; xz /= l;
  const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
  return { m: new Float32Array([xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
    -(xx * eye[0] + xy * eye[1] + xz * eye[2]), -(yx * eye[0] + yy * eye[1] + yz * eye[2]), -(zx * eye[0] + zy * eye[1] + zz * eye[2]), 1]),
    right: [xx, xy, xz], up: [yx, yy, yz] };
}
function mul(a, b) {
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
    let s = 0;
    for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
    o[c * 4 + r] = s;
  }
  return o;
}

const hue = (label) => {   // distinct colour per semantic label
  const h = ((label * 0.61803398875) % 1) * 6, c = 0.75, x = c * (1 - Math.abs(h % 2 - 1)), m = 0.22;
  const rgbv = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(h) % 6];
  return [rgbv[0] + m, rgbv[1] + m, rgbv[2] + m];
};
function quatAxes(qv) {
  const [w, x, y, z] = qv;
  return [[1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)],
          [2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w)],
          [2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]];
}

function clipPolygon(vertices) {
  let polygon = vertices.map((point) => point.slice());
  for (const plane of CLIP_PLANES) {
    if (!polygon.length) break;
    const clipped = [];
    let previous = polygon[polygon.length - 1], previousDistance = plane(previous);
    for (const current of polygon) {
      const currentDistance = plane(current);
      if (currentDistance >= 0) {
        if (previousDistance < 0) {
          const t = previousDistance / (previousDistance - currentDistance);
          clipped.push(previous.map((value, i) => value + (current[i] - value) * t));
        }
        clipped.push(current);
      } else if (previousDistance >= 0) {
        const t = previousDistance / (previousDistance - currentDistance);
        clipped.push(previous.map((value, i) => value + (current[i] - value) * t));
      }
      previous = current; previousDistance = currentDistance;
    }
    polygon = clipped;
  }
  return polygon;
}

function convexHull(points) {
  const unique = [];
  for (const point of points.sort((a, b) => a[0] - b[0] || a[1] - b[1]))
    if (!unique.length || Math.hypot(point[0] - unique[unique.length - 1][0], point[1] - unique[unique.length - 1][1]) > 0.01) unique.push(point);
  if (unique.length < 3) return unique;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const point of unique) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) lower.pop(); lower.push(point); }
  for (let i = unique.length - 1; i >= 0; i--) { const point = unique[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) upper.pop(); upper.push(point); }
  lower.pop(); upper.pop(); return lower.concat(upper);
}

function polygonArea(polygon) {
  let twice = 0;
  for (let i = 0; i < polygon.length; i++) { const a = polygon[i], b = polygon[(i + 1) % polygon.length]; twice += a[0] * b[1] - a[1] * b[0]; }
  return Math.abs(twice) / 2;
}

function pointInPolygon(polygon, x, y) {
  let sign = 0;
  for (let i = 0; i < polygon.length; i++) {
    const a = polygon[i], b = polygon[(i + 1) % polygon.length], cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]);
    if (Math.abs(cross) < 0.01) continue;
    if (sign && Math.sign(cross) !== sign) return false;
    sign = Math.sign(cross);
  }
  return true;
}

function clipSegment(a, b) {   // Liang-Barsky in clip space: the part of the segment inside the six frustum planes, or null
  let t0 = 0, t1 = 1;
  for (const plane of CLIP_PLANES) {
    const da = plane(a), db = plane(b);
    if (da < 0 && db < 0) return null;
    if (da < 0) t0 = Math.max(t0, da / (da - db));
    else if (db < 0) t1 = Math.min(t1, da / (da - db));
    if (t0 > t1) return null;
  }
  const at = (t) => a.map((value, i) => value + (b[i] - value) * t);
  return [at(t0), at(t1)];
}

// ------------------------------------------------------------------ shaders
const MESH_SHADERS = [`
  attribute vec3 aPos; attribute vec3 aCol; attribute vec3 aNrm;
  uniform mat4 uMVP; uniform mat4 uView; uniform float uPoint;
  varying vec3 vCol; varying vec3 vNrm;
  void main() { vCol = aCol; vNrm = (uView * vec4(aNrm, 0.0)).xyz; gl_Position = uMVP * vec4(aPos, 1.0); gl_PointSize = uPoint; }`, `
  precision mediump float;
  varying vec3 vCol; varying vec3 vNrm; uniform float uLit; uniform float uGain;
  void main() { float l = mix(1.0, 0.55 + 0.45 * abs(normalize(vNrm).z), uLit); gl_FragColor = vec4(min(vCol * l * uGain, vec3(1.0)), 1.0); }`,
  ['aPos', 'aCol', 'aNrm'], ['uMVP', 'uView', 'uPoint', 'uLit', 'uGain']];

const NODE_SHADERS = [`
  attribute vec3 aPos; attribute vec3 aCol; attribute float aSize;
  uniform mat4 uMVP; uniform float uScale;
  varying vec3 vCol;
  void main() { vCol = aCol; gl_Position = uMVP * vec4(aPos, 1.0);
    gl_PointSize = clamp(2.0 * aSize * uScale / max(gl_Position.w, 0.001), 3.0, 96.0); }`, `
  precision mediump float;
  varying vec3 vCol;
  void main() { vec2 p = gl_PointCoord * 2.0 - 1.0; float r2 = dot(p, p); if (r2 > 1.0) discard;
    gl_FragColor = vec4(vCol * (0.45 + 0.55 * sqrt(1.0 - r2)), 1.0); }`,
  ['aPos', 'aCol', 'aSize'], ['uMVP', 'uScale']];

const LINE_SHADERS = [`
  attribute vec3 aPos; attribute vec3 aCol; uniform mat4 uMVP; varying vec3 vCol;
  void main() { vCol = aCol; gl_Position = uMVP * vec4(aPos, 1.0); }`, `
  precision mediump float; varying vec3 vCol;
  void main() { gl_FragColor = vec4(vCol, 1.0); }`,
  ['aPos', 'aCol'], ['uMVP']];

const TEMPLATE = `
<canvas class="viewer__gl" data-role="gl" tabindex="0"></canvas>
<canvas class="viewer__overlay" data-role="selection-overlay"></canvas>
<div class="viewer__labels" data-role="labels"></div>
<div class="viewer-panel" data-role="panel">
  <div class="viewer-panel__head"><span class="dataset-badge" data-role="dataset-badge"></span><span class="viewer-panel__title" data-role="title"></span></div>
  <div class="viewer-panel__graph" data-role="graph-badge"></div>
  <div class="row"><span>网格</span><select data-role="mesh-mode">
    <option value="reference">1 cm 参考网格</option>
    <option value="dynamic">动态 Hydra 网格</option>
    <option value="none">隐藏网格</option>
  </select></div>
  <div data-role="mesh-status" class="hint"></div>
  <label><input type="checkbox" data-role="c-bright" checked> 提亮网格颜色(场景偏暗)</label>
  <label><input type="checkbox" data-role="c-points"> 只画顶点</label>
  <label><input type="checkbox" data-role="c-obj" checked> 物体节点</label>
  <div class="row"><span>物体框</span><select data-role="bbox-mode"><option value="selected" selected>仅选中</option><option value="all">全部</option><option value="none">隐藏</option></select></div>
  <div class="row"><span>标签</span><select data-role="label-mode" title="漂浮的描述卡片:全部模式下卡片可以互相重叠,但不会因为相机移动而时隐时现"><option value="selected" selected>仅选中</option><option value="all">全部</option><option value="none">隐藏</option></select></div>
  <label><input type="checkbox" data-role="c-places" checked> 地点</label>
  <label><input type="checkbox" data-role="c-pedges" checked> 地点连线</label>
  <label><input type="checkbox" data-role="c-rooms" checked> 房间 / 楼宇</label>
  <label><input type="checkbox" data-role="c-links"> 层间连线</label>
  <label><input type="checkbox" data-role="c-traj" checked> 相机轨迹</label>
  <label data-role="c-cam-row"><input type="checkbox" data-role="c-cam" checked> 相机画面(RGB / 深度)</label>
  <button type="button" class="panel-button" data-role="reset-cameras" title="把相机画面放回默认位置和大小">重置相机布局</button>
  <div class="row"><span>节点大小</span><input type="range" data-role="r-size" min="0.4" max="3" step="0.1" value="1"></div>
  <div class="row"><span>层间距</span><input type="range" data-role="r-space" min="0" max="3" step="0.1" value="1"><span data-role="v-space">1.0 m</span></div>
  <div class="help">左键旋转 · 右键或 Shift+左键平移<br>滚轮缩放 · 双击复位<br>点击物体框内或右侧列表:选择实体<br>空格 播放/暂停 · ←/→ 单步</div>
</div>
<aside class="viewer-sidebar" data-role="sidebar">
  <nav class="sidebar-tabs" data-role="sidebar-tabs"><button type="button" class="active" data-section-tab="entities">实体</button></nav>
  <section class="sidebar-section entity-browser" data-section="entities" data-role="entity-browser">
    <div class="entity-head">
      <div class="entity-title"><b>场景实体</b><span data-role="entity-count"></span></div>
      <div class="entity-tabs">
        <button data-role="entity-tab-objects" class="active" type="button">物体</button>
        <button data-role="entity-tab-places" type="button">地点</button>
      </div>
      <input data-role="entity-filter" class="entity-filter" type="search" placeholder="筛选编号或描述" autocomplete="off">
    </div>
    <div class="entity-list" data-role="entity-list"></div>
    <div class="entity-detail" data-role="entity-detail"><span class="entity-empty">选择一个物体或地点查看详情</span></div>
  </section>
</aside>
<div class="camera-cards" data-role="cameras"></div>
<div class="source-modal" data-role="description-source-modal" role="dialog" aria-modal="true" aria-label="DAM 描述来源图像">
  <div class="source-modal__card">
    <button class="source-modal__close" data-role="description-source-close" type="button" title="关闭">×</button>
    <div class="source-modal__caption" data-role="description-source-caption"></div>
    <img class="source-modal__image" data-role="description-source-image" alt="DAM 描述来源帧">
  </div>
</div>
<div class="viewer-bar" data-role="bar">
  <button data-role="first" type="button" title="回到开头 (Home)">⏮</button>
  <button data-role="prev" type="button" title="上一帧 (←)">◀</button>
  <button data-role="play" type="button" class="viewer-bar__play" title="播放 / 暂停 (空格)">▶</button>
  <button data-role="next" type="button" title="下一帧 (→)">▶▏</button>
  <input type="range" data-role="slider" class="viewer-bar__slider" min="0" value="0" step="1" aria-label="时间轴">
  <select data-role="speed" title="播放速度"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option><option value="4">4×</option></select>
  <span data-role="info" class="viewer-bar__info">加载中…</span>
</div>
<div class="viewer__error" data-role="error" hidden></div>
<div class="viewer__msg" data-role="msg"></div>`;

export class ReplayViewer {
  constructor(host, dataSource, initialState = {}, options = {}) {
    this.host = host;
    this.data = dataSource;
    this.manifest = dataSource.manifest;
    this.initialState = initialState || {};
    this.onStateChange = options.onStateChange || (() => {});
    this.active = false;
    this.mounted = false;
    this.playing = false;
    this.wasPlaying = false;
    this.timer = 0;
    this.raf = 0;
    this.W = 1; this.H = 1;
    this.listeners = [];
    this.labelEls = new Map();   // label key -> its element
    this.labelItems = [];   // { key, pos, text, color, selected } of the cards of this snapshot, the selected one last
    this.cards = new Map();   // camera key -> { card, img, placeholder, caption, handle, camera, position }
    const m = this.manifest;
    // the replay data the former page embedded, now assembled from the manifest (tests and the console read it as debug.D)
    this.D = {
      title: m.title,
      quant: m.quant,
      snapshots: this.data.snapshots,
      labels: m.labels || {},
      description_sources: m.description_sources || {},
      catalog: m.catalog || { objects: [], places: [] },
      reference_mesh: m.reference_mesh || null,
      cameras: m.cameras || [],
    };
    this.N = this.data.count;
    this.cameraLayout = this.sanitizeCameraLayout(this.initialState.cameraLayout);   // camera key -> { x, y, z, big }
    this.S = { cur: -1, snap: null, reference: null, yaw: 0.8, pitch: 0.5, dist: 6, target: [0, 0, 0], dirty: true, wanted: 0, request: 0,
               selectedKey: null, hidden: new Set(), entityTab: 'objects', objItems: [], placeItems: [] };
    this.G = { meshCount: 0, meshTriangles: 0, meshIndexed: 0, meshKey: 'none', meshUploads: 0, indexType: 0, lines: 0, nodes: 0, highlightNodes: 0,
               overlay: { visible: false, segments: 0, corners: 0 } };
    const q = this.D.quant;
    this.center = q.min.map((v, i) => v + 32767.5 * q.scale[i]);   // of the scene, refined by fitView()
    this.radius = Math.max(0.5, Math.hypot(...q.scale.map((s) => 65535 * s)) / 2);
    this.debug = {
      S: this.S, G: this.G, D: this.D, viewer: this,
      draw: () => this.draw(), show: (i) => this.show(i), resetView: () => this.resetView(), fitView: () => this.fitView(),
      project: (p) => this.project(p), projectBox: (item) => this.projectBox(item), pick: (x, y) => this.pick(x, y),
      selectEntity: (kind, id, options) => this.selectEntity(kind, id, options), activeMesh: () => this.activeMesh(),
      positionLabels: () => this.positionLabels(), drawSelectedBoxOverlay: () => this.drawSelectedBoxOverlay(),
      labels: () => this.labelItems,
    };
  }

  // ------------------------------------------------------------------ lifecycle
  mount() {
    this.root = document.createElement('div');
    this.root.className = 'viewer';
    this.root.innerHTML = TEMPLATE;
    this.host.appendChild(this.root);
    this.el = {};
    for (const node of this.root.querySelectorAll('[data-role]')) this.el[node.dataset.role] = node;
    this.el['dataset-badge'].textContent = this.manifest.dataset.kind === 'coda' ? 'CODa' : 'TUM';
    this.el['dataset-badge'].classList.add(`dataset-badge--${this.manifest.dataset.kind}`);
    this.el.title.textContent = this.D.title || '';
    this.el.title.title = this.D.title || '';
    if (!this.D.cameras.length) { this.el['c-cam-row'].hidden = true; this.el['reset-cameras'].hidden = true; }   // bundle without dataset images
    this.el.slider.max = this.N - 1;
    this.canvas = this.el.gl;
    this.overlay = this.el['selection-overlay'];
    this.octx = this.overlay.getContext('2d');
    if (typeof DecompressionStream === 'undefined') { this.fail('浏览器太旧:需要支持 DecompressionStream(Chrome / Edge 80+、Firefox 113+、Safari 16.4+)。'); return; }
    if (!this.initGl()) return;
    this.buildCameraCards();
    this.bindControls();
    this.mounted = true;
    this.ready = this.start().catch((e) => this.fail('出错了:' + e.message));
  }

  initGl() {
    const gl = this.canvas.getContext('webgl', { antialias: true });
    if (!gl) { this.fail('浏览器不支持 WebGL。'); return false; }
    this.gl = gl;
    this.UINT_INDEX_OK = !!gl.getExtension('OES_element_index_uint');   // 32-bit indices: meshes with more than 65,535 vertices
    this.MESH = this.program(...MESH_SHADERS);
    this.NODES = this.program(...NODE_SHADERS);
    this.LINES = this.program(...LINE_SHADERS);
    this.B = {};   // GL buffers
    for (const k of ['mPos', 'mCol', 'mNrm', 'mIdx', 'lPos', 'lCol', 'nPos', 'nCol', 'nSize', 'hNPos', 'hNCol', 'hNSize']) this.B[k] = gl.createBuffer();
    this.G.indexType = gl.UNSIGNED_SHORT;
    this.G.meshKey = 'none';
    this.listen(this.canvas, 'webglcontextlost', (event) => { event.preventDefault(); this.glLost = true; });
    this.listen(this.canvas, 'webglcontextrestored', () => { this.glLost = false; this.initGl(); this.S.dirty = true; this.requestDraw(); });
    return true;
  }

  async start() {
    this.resetView();
    if (this.D.reference_mesh) {
      try { this.S.reference = await this.data.referenceMesh(); }
      catch (e) { this.S.reference = null; this.S.referenceError = e.message; }
    }
    if (!this.S.reference) {   // no reference payload (or it cannot be decoded): the dynamic Hydra mesh is the only mesh there is
      const option = this.el['mesh-mode'].querySelector('option[value="reference"]');
      if (option) option.remove();
      this.el['mesh-mode'].value = 'dynamic';
    }
    const state = this.initialState;
    this.restoreControls(state);
    const start = Number.isInteger(state.snapshot) ? Math.max(0, Math.min(this.N - 1, state.snapshot)) : this.N - 1;
    await this.show(start);
    this.el.slider.value = start;
    this.fitView();
    this.resetView();
    if (state.camera) this.restoreCamera(state.camera);
    this.started = true;
  }

  activate() {
    if (!this.mounted) return;
    this.active = true;
    this.data.paused = false;
    if (this.glLost && this.gl && this.gl.isContextLost()) this.fail('这个标签页的 WebGL 上下文被浏览器回收了:关闭再打开这个结果即可恢复。');
    this.showImages(this.S.wanted);
    this.layoutCameras();
    this.S.dirty = true;
    this.requestDraw();
    if (this.wasPlaying) this.setPlaying(true);
    this.wasPlaying = false;
  }

  deactivate() {
    if (!this.mounted) return;
    this.wasPlaying = this.playing;
    this.setPlaying(false);
    this.active = false;
    this.data.paused = true;
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
    for (const { img } of this.cards.values()) img.removeAttribute('src');   // stop decoding images in the background
    this.closeDescriptionSource();
  }

  destroy() {
    this.setPlaying(false);
    if (this.raf) cancelAnimationFrame(this.raf);
    for (const [target, type, handler, options] of this.listeners) target.removeEventListener(type, handler, options);
    this.listeners = [];
    const gl = this.gl;
    if (gl && !gl.isContextLost()) {
      for (const buffer of Object.values(this.B || {})) gl.deleteBuffer(buffer);
      for (const prog of [this.MESH, this.NODES, this.LINES]) if (prog) gl.deleteProgram(prog.p);
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
    }
    this.data.clearDecoded();
    this.root?.remove();
    this.mounted = false;
  }

  listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    this.listeners.push([target, type, handler, options]);
  }

  fail(text) {
    const m = this.el.msg;
    m.textContent = text;
    m.classList.add('open');
  }

  showError(text) {
    this.el.error.textContent = text;
    this.el.error.hidden = !text;
  }

  changed() {
    this.onStateChange();
  }

  // ------------------------------------------------------------------ state
  serializeState() {
    const S = this.S;
    const controls = {};
    for (const id of CONTROLS) {
      const node = this.el[id];
      controls[id] = node.type === 'checkbox' ? node.checked : node.value;
    }
    return {
      snapshot: S.wanted,
      time: this.D.snapshots[S.wanted] ? this.D.snapshots[S.wanted].t : null,
      speed: this.el.speed.value,
      camera: { yaw: S.yaw, pitch: S.pitch, dist: S.dist, target: S.target.slice() },
      controls,
      hidden: [...S.hidden],
      selected: S.selectedKey,
      entityTab: S.entityTab,
      cameraLayout: this.cameraLayout ? JSON.parse(JSON.stringify(this.cameraLayout)) : undefined,
    };
  }

  restoreControls(state) {
    const controls = state.controls || {};
    for (const id of CONTROLS) {
      if (!(id in controls)) continue;
      const node = this.el[id];
      if (node.type === 'checkbox') node.checked = !!controls[id];
      else if (node.tagName === 'SELECT') { if ([...node.options].some((o) => o.value === String(controls[id]))) node.value = controls[id]; }
      else node.value = controls[id];
    }
    if (state.speed && [...this.el.speed.options].some((o) => o.value === String(state.speed))) this.el.speed.value = state.speed;
    this.el['v-space'].textContent = parseFloat(this.el['r-space'].value).toFixed(1) + ' m';
    if (Array.isArray(state.hidden)) this.S.hidden = new Set(state.hidden.filter((key) => typeof key === 'string'));
    if (typeof state.selected === 'string') this.S.selectedKey = state.selected;
    if (state.entityTab === 'objects' || state.entityTab === 'places') this.S.entityTab = state.entityTab;
    this.debug.S = this.S;
    this.updateCameraVisibility();
  }

  restoreCamera(camera) {
    const S = this.S;
    if (!camera || !Array.isArray(camera.target) || camera.target.length !== 3) return;
    S.yaw = Number(camera.yaw) || S.yaw;
    S.pitch = Math.max(-1.5, Math.min(1.5, Number(camera.pitch) || 0));
    S.dist = Math.max(this.radius * 0.05, Math.min(this.radius * 40, Number(camera.dist) || S.dist));
    S.target = camera.target.map(Number);
    this.requestDraw();
  }

  // ------------------------------------------------------------------ GL helpers
  program(vs, fs, attribs, uniforms) {
    const gl = this.gl;
    const make = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, make(gl.VERTEX_SHADER, vs)); gl.attachShader(p, make(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const out = { p, a: {}, u: {} };
    for (const a of attribs) out.a[a] = gl.getAttribLocation(p, a);
    for (const u of uniforms) out.u[u] = gl.getUniformLocation(p, u);
    return out;
  }

  upload(buffer, target, data) { const gl = this.gl; gl.bindBuffer(target, buffer); gl.bufferData(target, data, gl.DYNAMIC_DRAW); }

  attrib(loc, buffer, size, type, normalized) {
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, size, type, !!normalized, 0, 0);
  }

  // ------------------------------------------------------------------ scene assembly
  fitView() {   // frame everything the current snapshot shows, the lifted layers included
    const a = this.S.snap, q = this.D.quant, sp = parseFloat(this.el['r-space'].value), lo = q.min.slice(), hi = q.min.map((v, i) => v + 65535 * q.scale[i]);
    if (!a) return;
    const grow = (arr, lift) => { for (let i = 0; i < arr.length; i += 3) for (let k = 0; k < 3; k++) {
      const v = arr[i + k] + (k === 2 ? lift * sp : 0); if (v < lo[k]) lo[k] = v; if (v > hi[k]) hi[k] = v; } };
    if (this.S.reference && this.el['mesh-mode'].value === 'reference') {   // the reference mesh is on screen: its bounds count too
      const rq = this.S.reference.quant;
      for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], rq.min[k]); hi[k] = Math.max(hi[k], rq.min[k] + 65535 * rq.scale[k]); }
    }
    grow(a.place_pos, 1); grow(a.room_pos, 2); grow(a.bld_pos, 3); grow(a.agent_pos, 0); grow(a.obj_min, 0); grow(a.obj_max, 0);
    this.center = lo.map((v, i) => (v + hi[i]) / 2);
    this.radius = Math.max(0.5, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2);
  }

  resetView() { const S = this.S; S.yaw = 0.8; S.pitch = 0.5; S.dist = this.radius * 2.0; S.target = this.center.slice(); this.requestDraw(); }

  // Floating cards. Which cards exist is decided only when the scene state changes (a new snapshot, the selection, an
  // entity's eye, the label mode or a layer switch: assemble()); orbiting, panning and zooming only move the existing
  // cards, so a card never appears or disappears because another one happens to cross it.
  setLabelItems(items) {
    const host = this.el.labels, keep = new Set(items.map((item) => item.key));
    for (const [key, el] of this.labelEls) if (!keep.has(key)) { el.remove(); this.labelEls.delete(key); }
    items.sort((a, b) => Number(a.selected) - Number(b.selected));   // the selected card is drawn last, above all others
    for (const item of items) {
      let el = this.labelEls.get(item.key);
      if (!el) { el = document.createElement('div'); el.className = 'lab'; el.dataset.labelKey = item.key; this.labelEls.set(item.key, el); }
      if (el.textContent !== item.text) el.textContent = item.text;
      el.style.borderLeft = '3px solid rgb(' + item.color.map((v) => Math.round(Math.min(1, v) * 255)).join(',') + ')';
      el.classList.toggle('selected', item.selected);
      host.appendChild(el);   // re-appending keeps the DOM order equal to the paint order
    }
    this.labelItems = items;
  }

  shorten(item) {   // label text of an object: its description without the article, cut short
    if (!item.full) return '物体 O' + item.cid;
    const s = item.full.replace(/^(a|an|the)\s+/i, '');
    return s.length > 30 ? s.slice(0, 29) + '…' : s;
  }

  objectAt(id) {
    const S = this.S;
    if (!S.snap) return null;
    const i = Array.from(S.snap.obj_cid).findIndex((value) => value === id);
    if (i < 0) return null;
    const mn = row(S.snap.obj_min, i), mx = row(S.snap.obj_max, i), label = S.snap.obj_label[i];
    return { kind: 'object', id, cid: id, index: i, label, mn, mx, color: hue(label), full: this.D.labels[String(label)] || '',
             center: [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2] };
  }

  placeAt(id) {
    const S = this.S;
    if (!S.snap) return null;
    const ids = S.snap.place_cid || new Int32Array(S.snap.place_rad.length).map((_, i) => i);
    const i = Array.from(ids).findIndex((value) => value === id);
    return i < 0 ? null : { kind: 'place', id, cid: id, index: i, center: row(S.snap.place_pos, i), radius: S.snap.place_rad[i], color: [0.3, 0.9, 0.5] };
  }

  refreshEntityItems() {
    const S = this.S;
    if (!S.snap) { S.objItems = []; S.placeItems = []; return; }
    S.objItems = Array.from(S.snap.obj_cid, (id) => this.objectAt(id));
    const placeIds = S.snap.place_cid || new Int32Array(S.snap.place_rad.length).map((_, i) => i);
    S.placeItems = Array.from(placeIds, (id) => this.placeAt(id));
    for (const item of [...S.objItems, ...S.placeItems]) item.hidden = S.hidden.has(entityKey(item.kind, item.id));
  }

  catalogItem(kind, id) {
    const items = kind === 'object' ? this.D.catalog.objects : this.D.catalog.places;
    return items.find((item) => item.id === id) || null;
  }

  descriptionSource(label) {   // exact DAM source frame of an object label, or null: never guessed from observations
    const source = this.D.description_sources[String(label)];
    if (!source) return null;
    const image = this.data.descriptionImage(source.frame_id);
    return { ...source, image };
  }

  showEntityDetail() {
    const S = this.S, host = this.el['entity-detail'];
    host.replaceChildren();
    if (!S.selectedKey) { const empty = document.createElement('span'); empty.className = 'entity-empty'; empty.textContent = '选择一个物体或地点查看详情'; host.appendChild(empty); return; }
    const [kind, rawId] = S.selectedKey.split(':'), id = Number(rawId), history = this.catalogItem(kind, id);
    const current = kind === 'object' ? this.objectAt(id) : this.placeAt(id);
    const title = document.createElement('div'); title.className = 'detail-title';
    title.textContent = (kind === 'object' ? '物体 O' : '地点 p') + id;
    const description = document.createElement('div'); description.className = 'detail-description';
    if (kind === 'object') description.textContent = (current && current.full) || (history && this.D.labels[String(history.label)]) || '(没有 DAM 文本描述)';
    else description.textContent = current ? '当前地点节点' : '这个地点在当前时刻尚未进入场景图';
    const meta = document.createElement('div'); meta.className = 'detail-meta';
    const lines = [];
    if (current && kind === 'object') {
      const dim = [0, 1, 2].map((k) => (current.mx[k] - current.mn[k]).toFixed(2)).join(' × ');
      lines.push('语义标签 ' + current.label, '尺寸 ' + dim + ' m', '中心 (' + current.center.map((v) => v.toFixed(2)).join(', ') + ')');
    } else if (current) {
      lines.push('半径 ' + current.radius.toFixed(2) + ' m', '位置 (' + current.center.map((v) => v.toFixed(2)).join(', ') + ')');
    }
    if (history) {
      lines.push('首次 ' + history.first.toFixed(1) + ' s · 末次 ' + history.last.toFixed(1) + ' s · 出现于 ' + history.count + ' 个快照');
      if (kind === 'object') lines.push('中心首末位移 ' + history.motion.toFixed(2) + ' m');
    }
    const source = kind === 'object' && history ? this.descriptionSource(history.label) : null;
    if (source) lines.push('描述来源 frame ' + source.frame_id + (source.timestamp == null ? '' : ' · ' + this.sourceTime(source.timestamp)) + (source.image ? '' : ' · 没有这一帧的图像'));
    lines.push(current ? '当前快照中可见' : '当前快照中未出现', S.hidden.has(S.selectedKey) ? '已手动隐藏' : '正在显示');
    meta.textContent = lines.join('\n');
    host.append(title, description, meta);
  }

  sourceTime(timestamp) {   // the recorded stamp, and its time on this replay's clock when the dataset has an origin
    const origin = this.manifest.dataset.time_origin || 0;
    return origin ? `${timestamp.toFixed(3)} (t = ${(timestamp - origin).toFixed(3)} s)` : `${timestamp.toFixed(3)} s`;
  }

  openDescriptionSource(item, source) {   // opens the modal only: never calls show()
    if (!source || !source.image) return;
    const image = source.image;
    this.el['description-source-caption'].textContent = '物体 O' + item.id + ' · 描述来源 frame ' + source.frame_id + ' · 相机 ' + image.camera
      + (source.timestamp == null ? '' : ' · ' + this.sourceTime(source.timestamp)) + ' · 数据集帧 ' + image.index;
    this.el['description-source-image'].src = image.url;
    this.el['description-source-modal'].classList.add('open');
  }

  closeDescriptionSource() {
    if (!this.el) return;
    this.el['description-source-modal'].classList.remove('open');
    this.el['description-source-image'].removeAttribute('src');
  }

  renderEntityBrowser() {
    const S = this.S, D = this.D, isObject = S.entityTab === 'objects', items = isObject ? D.catalog.objects : D.catalog.places;
    this.el['entity-tab-objects'].classList.toggle('active', isObject);
    this.el['entity-tab-places'].classList.toggle('active', !isObject);
    const query = this.el['entity-filter'].value.trim().toLowerCase();
    const filtered = items.filter((item) => {
      const name = isObject ? (D.labels[String(item.label)] || '') : '';
      return !query || String(item.id).includes(query) || name.toLowerCase().includes(query);
    });
    this.el['entity-count'].textContent = filtered.length + ' / ' + items.length;
    const list = this.el['entity-list'], scroll = list.scrollTop;
    list.replaceChildren();
    if (!filtered.length) { const empty = document.createElement('div'); empty.className = 'entity-empty'; empty.textContent = '没有匹配的实体'; list.appendChild(empty); }
    for (const item of filtered) {
      const kind = isObject ? 'object' : 'place', key = entityKey(kind, item.id), current = isObject ? this.objectAt(item.id) : this.placeAt(item.id);
      const rowEl = document.createElement('div'); rowEl.className = 'entity-row'; rowEl.dataset.entityKey = key;
      rowEl.classList.toggle('active', S.selectedKey === key); rowEl.classList.toggle('absent', !current); rowEl.classList.toggle('hidden', S.hidden.has(key));
      const dot = document.createElement('span'); dot.className = 'entity-dot'; dot.style.background = rgb(isObject ? hue(item.label) : [0.3, 0.9, 0.5]);
      const copy = document.createElement('div'); copy.className = 'entity-copy';
      const name = document.createElement('div'); name.className = 'entity-name';
      name.textContent = (isObject ? 'O' : 'p') + item.id + (isObject && D.labels[String(item.label)] ? ' · ' + D.labels[String(item.label)] : '');
      const meta = document.createElement('div'); meta.className = 'entity-meta';
      meta.textContent = (current ? '当前可见' : '当前未出现') + ' · ' + item.count + ' 个快照';
      copy.append(name, meta);
      const eye = document.createElement('button'); eye.type = 'button'; eye.className = 'entity-eye';
      eye.textContent = S.hidden.has(key) ? '◌' : '◉'; eye.title = S.hidden.has(key) ? '显示这个实体' : '隐藏这个实体';
      eye.onclick = (event) => { event.stopPropagation(); if (S.hidden.has(key)) S.hidden.delete(key); else S.hidden.add(key); S.dirty = true; this.renderEntityBrowser(); this.requestDraw(); this.changed(); };
      rowEl.onclick = () => this.selectEntity(kind, item.id);
      rowEl.append(dot, copy);
      if (isObject) {
        const source = this.descriptionSource(item.label);
        const sourceButton = document.createElement('button'); sourceButton.type = 'button'; sourceButton.className = 'entity-source';
        sourceButton.disabled = !(source && source.image);
        sourceButton.textContent = sourceButton.disabled ? '无描述源帧' : '描述源帧';
        sourceButton.title = !source ? '没有记录 DAM 描述来源(不从观测推测)' : source.image ? '打开 DAM 描述所用的 frame ' + source.frame_id : '数据包里没有 frame ' + source.frame_id + ' 的图像';
        sourceButton.onclick = (event) => { event.stopPropagation(); this.openDescriptionSource(item, source); };
        rowEl.append(sourceButton);
      }
      rowEl.append(eye); list.appendChild(rowEl);
    }
    list.scrollTop = scroll;
    this.showEntityDetail();
  }

  revealEntityRow(key) {
    const list = this.el['entity-list'];
    let rowEl = list.querySelector('[data-entity-key="' + key + '"]');
    if (!rowEl && this.el['entity-filter'].value) {
      this.el['entity-filter'].value = '';
      this.renderEntityBrowser();
      rowEl = list.querySelector('[data-entity-key="' + key + '"]');
    }
    if (rowEl) rowEl.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' });
  }

  selectEntity(kind, id, { revealInList = false } = {}) {   // highlights only: never calls show() or changes the snapshot
    const S = this.S, key = entityKey(kind, id);
    S.selectedKey = key;
    if (revealInList) S.entityTab = kind === 'object' ? 'objects' : 'places';
    S.dirty = true; this.renderEntityBrowser(); this.requestDraw();
    if (revealInList) this.revealEntityRow(key);
    this.changed();
  }

  clearSelection() {
    this.S.selectedKey = null;
    this.S.dirty = true;
    this.renderEntityBrowser();
    this.requestDraw();
    this.changed();
  }

  assemble() {   // everything except the mesh: boxes, graph nodes and edges, trajectory
    const S = this.S, D = this.D, gl = this.gl, B = this.B, G = this.G, el = this.el;
    const a = S.snap, sp = parseFloat(el['r-space'].value), nodeScale = parseFloat(el['r-size'].value);
    const L = [], LC = [], P = [], PC = [], PS = [], HP = [], HPC = [], HPS = [];
    const line = (p, r, c) => { L.push(p[0], p[1], p[2], r[0], r[1], r[2]); LC.push(c[0], c[1], c[2], c[0], c[1], c[2]); };
    const node = (p, c, s) => { P.push(p[0], p[1], p[2]); PC.push(c[0], c[1], c[2]); PS.push(s * nodeScale); };
    const highlightNode = (p, s) => { HP.push(p[0], p[1], p[2]); HPC.push(AMBER_RGB[0], AMBER_RGB[1], AMBER_RGB[2]); HPS.push(s * nodeScale); };   // the selection: amber, an x-ray overlay
    const lift = (p, k) => [p[0], p[1], p[2] + k * sp];
    const labels = [], labelMode = el['label-mode'].value;   // selected | all | none
    const wantLabel = (picked) => labelMode === 'all' || (labelMode === 'selected' && picked);
    const nObj = a.obj_cid.length, nPl = a.place_rad.length, nRoom = a.room_pos.length / 3, nBld = a.bld_pos.length / 3, nAg = a.agent_pos.length / 3;

    S.objItems = [];   // every object of this snapshot, for labels and picking
    for (let i = 0; i < nObj; i++) {
      const mn = row(a.obj_min, i), mx = row(a.obj_max, i), cid = a.obj_cid[i], color = hue(a.obj_label[i]);
      const item = { kind: 'object', id: cid, cid, index: i, label: a.obj_label[i], mn, mx, color, full: D.labels[String(a.obj_label[i])] || '',
                     center: [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2] };
      S.objItems.push(item);
      const key = entityKey('object', cid), hidden = S.hidden.has(key), picked = key === S.selectedKey;
      item.hidden = hidden;
      if (hidden) continue;
      if (el['c-obj'].checked) {
        if (picked) highlightNode(item.center, 0.085); else node(item.center, color, 0.035);
      }
      const boxMode = el['bbox-mode'].value, showBox = boxMode === 'all' || (boxMode === 'selected' && picked);
      const big = Math.hypot(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]) > 1.5;
      const c = big && !picked ? color.map((v) => v * 0.5) : color;   // floor, desk and walls are dimmed
      if (showBox) {   // the ordinary 3D box; the selected box is also drawn by the screen-space overlay (drawSelectedBoxOverlay)
        const corners = [];
        for (let k = 0; k < 8; k++) corners.push([(k & 1) ? mx[0] : mn[0], (k & 2) ? mx[1] : mn[1], (k & 4) ? mx[2] : mn[2]]);
        for (const [u, v] of BOX_EDGES) line(corners[u], corners[v], c);
      }
      if (wantLabel(picked)) labels.push({ key: entityKey('object', cid), pos: [item.center[0], item.center[1], mx[2] + 0.02], text: this.shorten(item), color, selected: picked });
    }
    const placeIds = a.place_cid || new Int32Array(nPl).map((_, i) => i);
    S.placeItems = [];
    for (let i = 0; i < nPl; i++) {
      const cid = placeIds[i], key = entityKey('place', cid), picked = key === S.selectedKey;
      const item = { kind: 'place', id: cid, cid, index: i, center: row(a.place_pos, i), radius: a.place_rad[i], color: [0.3, 0.9, 0.5], hidden: S.hidden.has(key) };
      S.placeItems.push(item);
      if (item.hidden || !el['c-places'].checked) continue;
      if (picked) highlightNode(lift(item.center, 1), 0.075); else node(lift(item.center, 1), item.color, 0.035);
      if (wantLabel(picked)) labels.push({ key: entityKey('place', cid), pos: lift(item.center, 1), text: '地点 p' + cid, color: item.color, selected: picked });
    }
    if (el['c-pedges'].checked) for (let e = 0; e < a.place_edges.length; e += 2) {
      const u = a.place_edges[e], v = a.place_edges[e + 1];
      if (!S.hidden.has(entityKey('place', placeIds[u])) && !S.hidden.has(entityKey('place', placeIds[v])))
        line(lift(row(a.place_pos, u), 1), lift(row(a.place_pos, v), 1), [0.2, 0.62, 0.38]);
    }
    if (el['c-rooms'].checked) {
      for (let i = 0; i < nRoom; i++) {
        node(lift(row(a.room_pos, i), 2), [1.0, 0.62, 0.2], 0.09);
        if (labelMode === 'all') labels.push({ key: 'room:' + i, pos: lift(row(a.room_pos, i), 2), text: '房间 R' + i, color: [1.0, 0.62, 0.2], selected: false });
      }
      for (let i = 0; i < nBld; i++) {
        node(lift(row(a.bld_pos, i), 3), [1.0, 0.9, 0.3], 0.11);
        if (labelMode === 'all') labels.push({ key: 'building:' + i, pos: lift(row(a.bld_pos, i), 3), text: '楼宇', color: [1.0, 0.9, 0.3], selected: false });
      }
    }
    if (el['c-links'].checked) {
      const grey = [0.46, 0.5, 0.58];
      for (let e = 0; e < a.room_place_edges.length; e += 2) {
        const p = a.room_place_edges[e + 1];
        if (!S.hidden.has(entityKey('place', placeIds[p]))) line(lift(row(a.room_pos, a.room_place_edges[e]), 2), lift(row(a.place_pos, p), 1), grey);
      }
      for (let e = 0; e < a.bld_room_edges.length; e += 2)
        line(lift(row(a.bld_pos, a.bld_room_edges[e]), 3), lift(row(a.room_pos, a.bld_room_edges[e + 1]), 2), grey);
      for (let e = 0; e < a.obj_place_edges.length; e += 2) {
        const p = a.obj_place_edges[e], o = a.obj_place_edges[e + 1], mn = row(a.obj_min, o), mx = row(a.obj_max, o);
        if (!S.hidden.has(entityKey('place', placeIds[p])) && !S.hidden.has(entityKey('object', a.obj_cid[o])))
          line(lift(row(a.place_pos, p), 1), [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2], grey);
      }
    }
    if (el['c-traj'].checked && nAg) {
      const blue = [0.35, 0.8, 1.0];
      for (let i = 0; i < nAg; i++) {
        node(row(a.agent_pos, i), blue, 0.02);
        if (i) line(row(a.agent_pos, i - 1), row(a.agent_pos, i), blue);
      }
      const p = row(a.agent_pos, nAg - 1), axes = quatAxes(Array.from(a.agent_quat.slice(4 * (nAg - 1), 4 * nAg))), len = this.radius * 0.12;
      const colors = [[1, 0.3, 0.3], [0.4, 1, 0.4], [0.4, 0.6, 1]];
      for (let k = 0; k < 3; k++) line(p, [p[0] + axes[k][0] * len, p[1] + axes[k][1] * len, p[2] + axes[k][2] * len], colors[k]);
    }
    this.upload(B.lPos, gl.ARRAY_BUFFER, new Float32Array(L)); this.upload(B.lCol, gl.ARRAY_BUFFER, new Float32Array(LC));
    this.upload(B.nPos, gl.ARRAY_BUFFER, new Float32Array(P)); this.upload(B.nCol, gl.ARRAY_BUFFER, new Float32Array(PC));
    this.upload(B.nSize, gl.ARRAY_BUFFER, new Float32Array(PS));
    this.upload(B.hNPos, gl.ARRAY_BUFFER, new Float32Array(HP)); this.upload(B.hNCol, gl.ARRAY_BUFFER, new Float32Array(HPC));
    this.upload(B.hNSize, gl.ARRAY_BUFFER, new Float32Array(HPS));
    G.lines = L.length / 3; G.nodes = PS.length; G.highlightNodes = HPS.length;
    this.setLabelItems(labels);
    this.showEntityDetail();
  }

  activeMesh() {   // the arrays of the selected mesh source, and the key that says which arrays they are
    const S = this.S, mode = this.el['mesh-mode'].value;
    if (mode === 'reference' && S.reference) return { key: 'reference', mesh: S.reference };
    if (mode === 'dynamic' && S.snap) {
      const a = S.snap;
      return { key: 'dynamic:' + S.cur, mesh: { pos: a.pos, nrm: a.nrm, col: a.mesh_col, idx: a.mesh_faces, count: a.pos.length / 3, tris: a.mesh_faces.length / 3 } };
    }
    return { key: 'none', mesh: null };
  }

  uploadMesh() {   // skipped while the buffers already hold the arrays of the active source: the static mesh is uploaded once
    const G = this.G, B = this.B, gl = this.gl;
    const { key, mesh } = this.activeMesh();
    if (key === G.meshKey) return;
    G.meshKey = key;
    if (!mesh) { G.meshCount = 0; G.meshTriangles = 0; G.meshIndexed = 0; return; }
    G.meshUploads++;
    this.upload(B.mPos, gl.ARRAY_BUFFER, mesh.pos); this.upload(B.mCol, gl.ARRAY_BUFFER, mesh.col); this.upload(B.mNrm, gl.ARRAY_BUFFER, mesh.nrm);
    this.upload(B.mIdx, gl.ELEMENT_ARRAY_BUFFER, mesh.idx);
    G.meshCount = mesh.count; G.meshTriangles = mesh.tris; G.meshIndexed = mesh.idx.length;
    G.indexType = mesh.idx instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;
    if (mesh.idx instanceof Uint32Array && !this.UINT_INDEX_OK) G.meshIndexed = 0;   // no 32-bit indices here: draw the vertices as points
  }

  updateMeshStatus() {
    const S = this.S, mode = this.el['mesh-mode'].value, active = this.activeMesh().mesh;
    let text = '网格已隐藏。';
    if (mode === 'reference' && S.reference)
      text = (this.D.reference_mesh.label || '1 cm 参考网格') + '(' + fmt(S.reference.count) + ' 顶点 · ' + fmt(S.reference.tris) + ' 三角形):用整段序列的 RGB-D 帧和位姿重建的静态网格,是参考,不是真值,也不是 Hydra 在这一时刻已有的地图。';
    else if (mode === 'dynamic') text = '动态 Hydra 网格:Hydra 在当前快照时已经建好的网格,随时间变化。';
    if (active && active.idx instanceof Uint32Array && !this.UINT_INDEX_OK) text += ' 这个浏览器不支持 32 位索引,只能画顶点。';
    if (S.referenceError) text += ' 参考网格无法使用(' + S.referenceError + ')。';
    this.el['mesh-status'].textContent = text;
  }

  // ------------------------------------------------------------------ drawing
  requestDraw() {
    if (!this.active || this.raf || !this.gl) return;
    this.raf = requestAnimationFrame(() => { this.raf = 0; this.draw(); });
  }

  draw() {
    const S = this.S, G = this.G, gl = this.gl, canvas = this.canvas, B = this.B, el = this.el;
    if (!gl || gl.isContextLost()) return;
    G.draws = (G.draws || 0) + 1;   // rendered frames: an inactive tab must not add any
    const cssW = Math.max(1, canvas.clientWidth), cssH = Math.max(1, canvas.clientHeight);
    const dpr = Math.min(window.devicePixelRatio || 1, Math.sqrt(2.2e6 / (cssW * cssH)));   // render at most ~2.2 megapixels
    const w = Math.max(1, Math.round(cssW * dpr)), h = Math.max(1, Math.round(cssH * dpr));
    if (w !== canvas.width || h !== canvas.height) { canvas.width = w; canvas.height = h; }
    this.W = w; this.H = h; S.dpr = dpr;
    const W = w, H = h;
    gl.viewport(0, 0, W, H);
    gl.clearColor(0.082, 0.09, 0.106, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    if (!S.snap) return;
    if (S.dirty) { this.uploadMesh(); this.assemble(); S.dirty = false; }

    const cp = Math.cos(S.pitch), eye = [S.target[0] + S.dist * cp * Math.cos(S.yaw), S.target[1] + S.dist * cp * Math.sin(S.yaw), S.target[2] + S.dist * Math.sin(S.pitch)];
    const view = lookAt(eye, S.target, [0, 0, 1]);
    const fovy = 0.8, proj = perspective(fovy, W / H, S.dist * 0.01, S.dist * 40 + this.radius * 10), mvp = mul(proj, view.m);
    S.eye = eye;
    S.view = { m: view.m, mvp, right: view.right, up: view.up };

    const MESH = this.MESH, NODES = this.NODES, LINES = this.LINES;
    if (G.meshCount) {
      const asPoints = el['c-points'].checked || !G.meshIndexed;
      gl.useProgram(MESH.p);
      this.attrib(MESH.a.aPos, B.mPos, 3, gl.FLOAT); this.attrib(MESH.a.aCol, B.mCol, 3, gl.UNSIGNED_BYTE, true); this.attrib(MESH.a.aNrm, B.mNrm, 3, gl.FLOAT);
      gl.uniformMatrix4fv(MESH.u.uMVP, false, mvp); gl.uniformMatrix4fv(MESH.u.uView, false, view.m);
      gl.uniform1f(MESH.u.uPoint, Math.max(2, 2.5 * S.dpr)); gl.uniform1f(MESH.u.uLit, asPoints ? 0 : 1);
      gl.uniform1f(MESH.u.uGain, el['c-bright'].checked ? 1.8 : 1.0);
      if (asPoints) gl.drawArrays(gl.POINTS, 0, G.meshCount);
      else { gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, B.mIdx); gl.drawElements(gl.TRIANGLES, G.meshIndexed, G.indexType, 0); }
      for (const k of ['aPos', 'aCol', 'aNrm']) gl.disableVertexAttribArray(MESH.a[k]);
    }
    if (G.lines) {
      gl.useProgram(LINES.p);
      this.attrib(LINES.a.aPos, B.lPos, 3, gl.FLOAT); this.attrib(LINES.a.aCol, B.lCol, 3, gl.FLOAT);
      gl.uniformMatrix4fv(LINES.u.uMVP, false, mvp);
      gl.drawArrays(gl.LINES, 0, G.lines);
      gl.disableVertexAttribArray(LINES.a.aPos); gl.disableVertexAttribArray(LINES.a.aCol);
    }
    if (G.nodes) {
      gl.useProgram(NODES.p);
      this.attrib(NODES.a.aPos, B.nPos, 3, gl.FLOAT); this.attrib(NODES.a.aCol, B.nCol, 3, gl.FLOAT); this.attrib(NODES.a.aSize, B.nSize, 1, gl.FLOAT);
      gl.uniformMatrix4fv(NODES.u.uMVP, false, mvp); gl.uniform1f(NODES.u.uScale, H / (2 * Math.tan(fovy / 2)));
      gl.drawArrays(gl.POINTS, 0, G.nodes);
      for (const k of ['aPos', 'aCol', 'aSize']) gl.disableVertexAttribArray(NODES.a[k]);
    }
    if (G.highlightNodes) {
      gl.disable(gl.DEPTH_TEST);   // the selected node is an x-ray overlay and stays visible through the mesh
      gl.useProgram(NODES.p);
      this.attrib(NODES.a.aPos, B.hNPos, 3, gl.FLOAT); this.attrib(NODES.a.aCol, B.hNCol, 3, gl.FLOAT); this.attrib(NODES.a.aSize, B.hNSize, 1, gl.FLOAT);
      gl.uniformMatrix4fv(NODES.u.uMVP, false, mvp); gl.uniform1f(NODES.u.uScale, H / (2 * Math.tan(fovy / 2)));
      gl.drawArrays(gl.POINTS, 0, G.highlightNodes);
      for (const k of ['aPos', 'aCol', 'aSize']) gl.disableVertexAttribArray(NODES.a[k]);
      gl.enable(gl.DEPTH_TEST);
    }
    this.positionLabels();
    this.drawSelectedBoxOverlay();
  }

  project(p) {   // world point -> [x, y, depth] in CSS pixels, or null behind the camera
    const m = this.S.view.mvp, cw = m[3] * p[0] + m[7] * p[1] + m[11] * p[2] + m[15];
    if (cw <= 0.001) return null;
    const x = (m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12]) / cw, y = (m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13]) / cw;
    return [(x * 0.5 + 0.5) * this.canvas.clientWidth, (0.5 - y * 0.5) * this.canvas.clientHeight, cw];
  }

  clipPoint(p) {
    const m = this.S.view.mvp;
    return [m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
            m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
            m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14],
            m[3] * p[0] + m[7] * p[1] + m[11] * p[2] + m[15]];
  }

  projectBox(item) {
    const S = this.S;
    if (!S.view) return null;
    const cw = this.canvas.clientWidth, ch = this.canvas.clientHeight;
    const insideEpsilon = 1e-7;
    if (S.eye && S.eye.every((value, axis) => value > item.mn[axis] + insideEpsilon && value < item.mx[axis] - insideEpsilon))
      return { polygon: [[0, 0], [cw, 0], [cw, ch], [0, ch]], area: cw * ch, depth: 0 };
    const world = [];
    for (let k = 0; k < 8; k++) world.push([(k & 1) ? item.mx[0] : item.mn[0], (k & 2) ? item.mx[1] : item.mn[1], (k & 4) ? item.mx[2] : item.mn[2]]);
    const clip = world.map((p) => this.clipPoint(p)), visible = [], depths = [];
    const faces = [[0, 1, 3, 2], [4, 5, 7, 6], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]];
    for (const face of faces) {
      for (const p of clipPolygon(face.map((index) => clip[index]))) {
        const x = Math.max(0, Math.min(cw, (p[0] / p[3] * 0.5 + 0.5) * cw));
        const y = Math.max(0, Math.min(ch, (0.5 - p[1] / p[3] * 0.5) * ch));
        visible.push([x, y]); depths.push(p[3]);
      }
    }
    const polygon = convexHull(visible);
    return polygon.length >= 3 ? { polygon, area: polygonArea(polygon), depth: Math.min(...depths.filter((value) => value > 0)) } : null;
  }

  positionLabels() {   // every draw: only the screen position of each card; a card is hidden only when its own anchor is off screen
    const cw = this.canvas.clientWidth, chh = this.canvas.clientHeight;
    for (const item of this.labelItems) {
      const el = this.labelEls.get(item.key), s = this.project(item.pos);
      if (!el) continue;
      const onScreen = !!s && s[0] >= 0 && s[0] <= cw && s[1] >= 0 && s[1] <= chh;   // project() is null behind the camera
      el.style.display = onScreen ? 'block' : 'none';
      if (onScreen) el.style.transform = 'translate(' + Math.round(s[0] - 8) + 'px,' + Math.round(s[1] - 22) + 'px)';
    }
  }

  // The selected box is drawn on a 2D canvas above the WebGL one: depth-independent, vivid amber on a dark halo, with
  // corner dots, so neither the mesh shading nor a browser's WebGL line-width limit can make it look like an ordinary edge.
  selectedBoxItem() {   // the selected object, when this snapshot has it and its eye is not closed
    const S = this.S;
    if (!S.selectedKey || !S.selectedKey.startsWith('object:')) return null;
    const item = (S.objItems || []).find((it) => entityKey('object', it.id) === S.selectedKey);
    return item && !item.hidden ? item : null;
  }

  drawSelectedBoxOverlay() {
    const S = this.S, G = this.G, overlay = this.overlay, octx = this.octx;
    const cw = this.canvas.clientWidth, chh = this.canvas.clientHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(cw * dpr)), h = Math.max(1, Math.round(chh * dpr));
    if (overlay.width !== w || overlay.height !== h) { overlay.width = w; overlay.height = h; }
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    octx.clearRect(0, 0, cw, chh);
    G.overlay = { visible: false, segments: 0, corners: 0, haloWidth: 10, lineWidth: 4, color: AMBER };
    const item = this.selectedBoxItem();
    if (!item || !S.view) return;
    const clip = [];
    for (let k = 0; k < 8; k++) clip.push(this.clipPoint([(k & 1) ? item.mx[0] : item.mn[0], (k & 2) ? item.mx[1] : item.mn[1], (k & 4) ? item.mx[2] : item.mn[2]]));
    const px = (p) => [(p[0] / p[3] * 0.5 + 0.5) * cw, (0.5 - p[1] / p[3] * 0.5) * chh];
    const segments = [];
    for (const [u, v] of BOX_EDGES) { const s = clipSegment(clip[u], clip[v]); if (s) segments.push([px(s[0]), px(s[1])]); }
    const corners = clip.filter((p) => CLIP_PLANES.every((plane) => plane(p) >= 0)).map(px);
    octx.lineCap = 'round'; octx.lineJoin = 'round';
    for (const [width, color] of [[G.overlay.haloWidth, HALO], [G.overlay.lineWidth, AMBER]]) {
      octx.lineWidth = width; octx.strokeStyle = color; octx.beginPath();
      for (const [p, q] of segments) { octx.moveTo(p[0], p[1]); octx.lineTo(q[0], q[1]); }
      octx.stroke();
    }
    octx.lineWidth = 2; octx.strokeStyle = HALO; octx.fillStyle = AMBER;
    for (const [x, y] of corners) { octx.beginPath(); octx.arc(x, y, 5, 0, 2 * Math.PI); octx.fill(); octx.stroke(); }
    G.overlay.segments = segments.length; G.overlay.corners = corners.length; G.overlay.visible = segments.length > 0;
  }

  pick(clientX, clientY) {   // select inside the clipped visible bbox polygon; smaller and then nearer overlaps win
    const S = this.S, r = this.canvas.getBoundingClientRect(), x = clientX - r.left, y = clientY - r.top;
    const hits = [];
    for (const it of S.objItems || []) {
      if (it.hidden) continue;
      const projected = this.projectBox(it);
      if (projected && pointInPolygon(projected.polygon, x, y)) hits.push({ item: it, area: projected.area, depth: projected.depth });
    }
    hits.sort((a, b) => a.area - b.area || a.depth - b.depth);
    if (hits.length) { this.selectEntity('object', hits[0].item.cid, { revealInList: true }); return; }
    let bestPlace = null, bestDistance = 22;
    const sp = parseFloat(this.el['r-space'].value);
    for (const it of S.placeItems || []) {
      if (it.hidden) continue;
      const s = this.project([it.center[0], it.center[1], it.center[2] + sp]);
      const distance = s ? Math.hypot(s[0] - x, s[1] - y) : Infinity;
      if (distance < bestDistance) { bestDistance = distance; bestPlace = it; }
    }
    if (bestPlace) this.selectEntity('place', bestPlace.cid, { revealInList: true });
    else this.clearSelection();
  }

  // ------------------------------------------------------------------ time slider, playback and camera images
  updateInfo() {   // the status line: time, snapshot, entity counts and the vertex count of the mesh on screen
    const S = this.S, a = S.snap;
    if (!a) return;
    const mesh = this.activeMesh().mesh;
    this.el.info.textContent = 't = ' + this.D.snapshots[S.cur].t.toFixed(1) + ' s  ·  ' + (S.cur + 1) + ' / ' + this.N + '  ·  物体 ' + a.obj_cid.length + '  ·  地点 ' + a.place_rad.length
      + '  ·  房间 ' + a.room_pos.length / 3 + '  ·  顶点 ' + fmt(mesh ? mesh.count : 0);
  }

  // ------------------------------------------------------------------ camera cards
  // The current-time RGB and depth images. Each card can be dragged by its handle (or moved with the arrow keys while
  // the handle has focus, Shift for 1 px), comes to the front when clicked and stays inside the viewer. A card that was
  // never moved follows the default layout (left of the sidebar, above the time bar); moved positions, the stacking
  // order and the enlarged state belong to this tab's view state. Nothing here touches the timeline or the 3D view.
  sanitizeCameraLayout(value) {
    const layout = {};
    if (!value || typeof value !== 'object') return layout;
    for (const camera of this.D.cameras) {
      const entry = value[camera.key];
      if (!entry || typeof entry !== 'object') continue;
      const finite = (v) => typeof v === 'number' && Number.isFinite(v);
      layout[camera.key] = {
        x: finite(entry.x) ? entry.x : null,
        y: finite(entry.y) && finite(entry.x) ? entry.y : null,
        z: finite(entry.z) ? Math.max(0, Math.round(entry.z)) : 0,
        big: !!entry.big,
      };
    }
    return layout;
  }

  buildCameraCards() {
    const host = this.el.cameras;
    for (const camera of this.D.cameras) {
      const key = camera.key;
      const card = document.createElement('div');
      card.className = 'camera-card';
      card.dataset.camera = key;
      const handle = document.createElement('div');
      handle.className = 'camera-card__handle';
      handle.tabIndex = 0;
      handle.setAttribute('role', 'button');
      handle.setAttribute('aria-label', '移动' + camera.label + '画面(拖动,或用方向键)');
      handle.title = '拖动移动这张画面;获得焦点后方向键移动 10 px,Shift+方向键 1 px';
      const caption = document.createElement('span');
      caption.className = 'camera-card__caption';
      handle.appendChild(caption);
      const img = document.createElement('img');
      img.className = 'camera-card__image';
      img.alt = camera.label;
      img.title = '点击放大 / 缩小';
      const placeholder = document.createElement('div');
      placeholder.className = 'camera-card__placeholder';
      placeholder.hidden = true;
      card.append(handle, img, placeholder);
      host.appendChild(card);
      const entry = { card, img, placeholder, caption, handle, camera, position: { x: 0, y: 0 } };
      this.cards.set(key, entry);
      if (this.cameraLayout[key] && this.cameraLayout[key].big) card.classList.add('big');
      card.addEventListener('pointerdown', () => this.raiseCamera(key), true);
      img.addEventListener('load', () => this.layoutCameras());
      img.addEventListener('click', () => {
        card.classList.toggle('big');
        this.cameraEntry(key).big = card.classList.contains('big');
        this.layoutCameras();
        this.changed();
      });
      handle.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        this.cardDrag = { key, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, origin: { ...entry.position } };
        card.classList.add('dragging');
      });
      handle.addEventListener('pointermove', (event) => {
        const drag = this.cardDrag;
        if (!drag || drag.key !== key || drag.pointerId !== event.pointerId) return;
        this.placeCamera(key, drag.origin.x + event.clientX - drag.startX, drag.origin.y + event.clientY - drag.startY);
      });
      const finish = (event) => {
        const drag = this.cardDrag;
        if (!drag || drag.key !== key || drag.pointerId !== event.pointerId) return;
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
        this.cardDrag = null;
        card.classList.remove('dragging');
        this.changed();
      };
      handle.addEventListener('pointerup', finish);
      handle.addEventListener('pointercancel', finish);
      handle.addEventListener('keydown', (event) => {
        const step = event.shiftKey ? 1 : 10;
        const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key];
        if (!delta) return;
        event.preventDefault();
        event.stopPropagation();   // the arrows move the card, not the timeline
        this.raiseCamera(key);
        this.placeCamera(key, entry.position.x + delta[0], entry.position.y + delta[1]);
        this.changed();
      });
    }
    this.el['reset-cameras'].addEventListener('click', () => this.resetCameraLayout());
    this.listen(window, 'resize', () => { if (this.active) this.layoutCameras(); });
  }

  cameraEntry(key) {
    if (!this.cameraLayout[key]) this.cameraLayout[key] = { x: null, y: null, z: 0, big: false };
    return this.cameraLayout[key];
  }

  raiseCamera(key) {
    const z = (name) => (this.cameraLayout[name] ? this.cameraLayout[name].z || 0 : 0);
    const levels = [...this.cards.keys()].map(z), top = Math.max(0, ...levels);
    if (z(key) === top && levels.filter((level) => level === top).length === 1) return;   // already alone on top
    this.cameraEntry(key).z = top + 1;
    this.layoutCameras();
  }

  // Move a card to (x, y) in viewer pixels, kept inside the viewer; the position is remembered for this tab.
  placeCamera(key, x, y) {
    const entry = this.cards.get(key), stored = this.cameraEntry(key);
    const position = this.clampCamera(entry.card, x, y);
    stored.x = position.x;
    stored.y = position.y;
    this.applyCameraPosition(entry, position, stored.z);
  }

  clampCamera(card, x, y) {
    const W = this.root.clientWidth, H = this.root.clientHeight;
    return {
      x: Math.round(Math.max(0, Math.min(W - card.offsetWidth, x))),
      y: Math.round(Math.max(0, Math.min(H - card.offsetHeight, y))),
    };
  }

  applyCameraPosition(entry, position, z) {
    entry.position = position;
    entry.card.style.transform = 'translate(' + position.x + 'px, ' + position.y + 'px)';
    entry.card.style.zIndex = String(10 + (z || 0));
  }

  defaultCameraPositions() {   // side by side, left of the sidebar and above the time bar, as on the former page
    const W = this.root.clientWidth, H = this.root.clientHeight;
    const sidebar = this.el.sidebar.getBoundingClientRect(), root = this.root.getBoundingClientRect(), bar = this.el.bar.getBoundingClientRect();
    let right = (sidebar.width ? sidebar.left - root.left : W - 12) - 14;
    const bottom = (bar.height ? bar.top - root.top : H - 12) - 14;
    const positions = {};
    for (const key of [...this.cards.keys()].reverse()) {
      const card = this.cards.get(key).card;
      positions[key] = { x: right - card.offsetWidth, y: bottom - card.offsetHeight };
      right -= card.offsetWidth + 8;
    }
    return positions;
  }

  layoutCameras() {
    if (!this.cards.size || !this.root || !this.root.clientWidth || this.el.cameras.hidden) return;   // a hidden pane has no size yet
    const defaults = this.defaultCameraPositions();
    for (const [key, entry] of this.cards) {
      const stored = this.cameraLayout[key];
      const wanted = stored && stored.x != null ? stored : defaults[key];
      this.applyCameraPosition(entry, this.clampCamera(entry.card, wanted.x, wanted.y), stored ? stored.z : 0);
    }
  }

  resetCameraLayout() {
    this.cameraLayout = {};
    for (const { card } of this.cards.values()) card.classList.remove('big');
    this.layoutCameras();
    this.changed();
  }

  showImages(i) {   // the RGB and depth image of the frame the pipeline had processed at this moment (active tab only)
    if (!this.cards.size) return;
    const frame = this.data.frameOf(i), total = this.manifest.images.frame_total;
    for (const [key, { img, placeholder, caption, camera }] of this.cards) {
      caption.textContent = key === 'depth'
        ? '深度 · 蓝近红远(0–' + camera.max_depth + ' m),黑色没测到'
        : 'RGB · 帧 ' + (frame == null ? '?' : frame + 1) + ' / ' + total;
      const url = this.data.image(key, i);
      placeholder.hidden = !!url;
      img.hidden = !url;
      if (!url) {
        placeholder.textContent = '没有图像 · ' + camera.label + ' · 帧 ' + (frame == null ? '?' : frame + 1);
        img.removeAttribute('src');
      } else if (this.active && img.getAttribute('src') !== url) img.src = url;
    }
  }

  updateCameraVisibility() {
    this.el.cameras.hidden = !(this.D.cameras.length && this.el['c-cam'].checked);
    this.layoutCameras();
  }

  async show(i) {
    const S = this.S;
    i = Math.max(0, Math.min(this.N - 1, i));
    S.wanted = i; this.el.slider.value = i;
    const mine = ++S.request;
    let a;
    try {
      a = await this.data.snapshot(i);
    } catch (error) {
      if (mine !== S.request) return;
      this.setPlaying(false);   // a broken chunk stops this result's playback only
      this.showError(error instanceof ChunkError ? error.message : '快照无法读取:' + error.message);
      return;
    }
    if (mine !== S.request || !this.mounted) return;   // a newer request overtook this one, or the tab closed
    this.showError('');
    S.cur = i; S.snap = a; S.dirty = true;
    this.refreshEntityItems();   // picking must immediately use this snapshot, before the next animation frame assembles GL buffers
    this.showImages(i);
    this.updateInfo(); this.updateMeshStatus();
    this.renderEntityBrowser();
    this.requestDraw();
    if (this.active) this.data.prefetchAround(i);
    this.changed();
  }

  playbackTick() {
    if (!this.playing) return;
    const S = this.S, D = this.D, N = this.N;
    const speed = parseFloat(this.el.speed.value);
    const targetTime = D.snapshots[this.playbackStartIndex].t + (performance.now() - this.playbackStartedAt) / 1000 * speed;
    let next = S.wanted;
    while (next + 1 < N && D.snapshots[next + 1].t <= targetTime + 1e-6) next++;
    if (next !== S.wanted) this.show(next);
    if (next >= N - 1) { this.setPlaying(false); return; }
    const delay = Math.max(0, (D.snapshots[next + 1].t - targetTime) * 1000 / speed);
    this.timer = setTimeout(() => this.playbackTick(), delay);
  }

  setPlaying(on) {
    on = !!on && this.mounted;
    this.playing = on;
    if (this.el) this.el.play.textContent = on ? '⏸' : '▶';
    clearTimeout(this.timer);
    if (on) {
      if (this.S.wanted >= this.N - 1) this.show(0);
      this.playbackStartIndex = this.S.wanted;
      this.playbackStartedAt = performance.now();
      this.playbackTick();
    }
  }

  // ------------------------------------------------------------------ controls
  bindControls() {
    const el = this.el, S = this.S;
    el.play.onclick = () => this.setPlaying(!this.playing);
    el.first.onclick = () => { this.setPlaying(false); this.show(0); };
    el.prev.onclick = () => { this.setPlaying(false); this.show(S.wanted - 1); };
    el.next.onclick = () => { this.setPlaying(false); this.show(S.wanted + 1); };
    el.speed.onchange = () => { if (this.playing) this.setPlaying(true); this.changed(); };
    el.slider.oninput = () => this.show(parseInt(el.slider.value, 10));
    this.listen(window, 'keydown', (e) => this.onKey(e));
    for (const id of CONTROLS)
      el[id].addEventListener('input', () => { el['v-space'].textContent = parseFloat(el['r-space'].value).toFixed(1) + ' m'; S.dirty = true; this.requestDraw(); this.changed(); });
    el['mesh-mode'].addEventListener('input', () => { this.updateMeshStatus(); this.updateInfo(); });
    el['c-cam'].addEventListener('input', () => this.updateCameraVisibility());
    el['entity-tab-objects'].onclick = () => { S.entityTab = 'objects'; this.renderEntityBrowser(); this.changed(); };
    el['entity-tab-places'].onclick = () => { S.entityTab = 'places'; this.renderEntityBrowser(); this.changed(); };
    el['entity-filter'].addEventListener('input', () => this.renderEntityBrowser());
    el['description-source-close'].onclick = () => this.closeDescriptionSource();
    el['description-source-modal'].onclick = (event) => { if (event.target === el['description-source-modal']) this.closeDescriptionSource(); };
    const canvas = this.canvas;
    let drag = null;
    this.listen(canvas, 'pointerdown', (e) => { canvas.setPointerCapture(e.pointerId); canvas.focus(); drag = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, moved: 0, pan: e.button === 2 || e.button === 1 || e.shiftKey }; });
    this.listen(canvas, 'pointermove', (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      drag.moved = Math.max(drag.moved, Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0));
      if (drag.pan && S.view) {
        const k = S.dist * 0.0016;
        for (let i = 0; i < 3; i++) S.target[i] += (-S.view.right[i] * dx + S.view.up[i] * dy) * k;
      } else {
        S.yaw -= dx * 0.006; S.pitch = Math.max(-1.5, Math.min(1.5, S.pitch + dy * 0.006));
      }
      this.requestDraw();
    });
    this.listen(canvas, 'pointerup', (e) => {
      if (drag && drag.moved < 4 && e.button === 0 && !e.shiftKey) this.pick(e.clientX, e.clientY);
      else if (drag) this.changed();
      drag = null;
    });
    this.listen(canvas, 'pointercancel', () => { drag = null; });
    this.listen(canvas, 'wheel', (e) => { e.preventDefault(); S.dist = Math.max(this.radius * 0.05, Math.min(this.radius * 40, S.dist * Math.exp(e.deltaY * 0.0012))); this.requestDraw(); this.changed(); }, { passive: false });
    this.listen(canvas, 'dblclick', () => { if (S.snap) this.fitView(); this.resetView(); this.changed(); });
    this.listen(canvas, 'contextmenu', (e) => e.preventDefault());
    this.listen(window, 'resize', () => this.requestDraw());
  }

  onKey(e) {
    if (!this.active) return;
    const modalOpen = this.el['description-source-modal'].classList.contains('open');
    if (e.key === 'Escape' && modalOpen) { this.closeDescriptionSource(); return; }
    if (modalOpen) return;
    const target = e.target;
    if (target !== document.body && !(target instanceof Node && this.root.contains(target))) return;   // the open bar, the tabs, other panels
    if (target && (target.tagName === 'SELECT' || target.tagName === 'TEXTAREA' || target.isContentEditable
        || (target.tagName === 'INPUT' && !['checkbox', 'range', 'button'].includes(target.type)))) return;
    const S = this.S;
    if (e.key === ' ') { e.preventDefault(); this.setPlaying(!this.playing); }
    else if (e.key === 'ArrowLeft') { this.setPlaying(false); this.show(S.wanted - 1); }
    else if (e.key === 'ArrowRight') { this.setPlaying(false); this.show(S.wanted + 1); }
    else if (e.key === 'Home') { this.setPlaying(false); this.show(0); }
    else if (e.key === 'End') { this.setPlaying(false); this.show(this.N - 1); }
  }
}
