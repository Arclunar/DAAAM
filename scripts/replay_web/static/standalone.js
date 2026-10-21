// Offline adapter: the shared viewer reads compressed data embedded in this file.
const offline = JSON.parse(document.getElementById('replay-offline-data').textContent);
const pane = document.getElementById('offline-pane');
const imageTypes = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png' };
const offlineApi = {
  chunkUrl: (_run, index) => offline.manifest.chunks[index].path,
  referenceUrl: () => offline.manifest.reference_mesh.path,
  imageUrl: (_run, path) => {
    const type = imageTypes[path.split('.').pop().toLowerCase()];
    return type && offline.assets[path] ? 'data:' + type + ';base64,' + offline.assets[path] : null;
  },
};
class OfflineDataSource extends ReplayDataSource {
  async fetchJson(path) {
    const encoded = offline.assets[path];
    if (!encoded) throw new Error('导出文件缺少 ' + path);
    const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).json();
  }
}
try {
  const source = new OfflineDataSource(offlineApi, 'offline', offline.manifest, 'offline');
  const viewer = new ReplayViewer(pane, source, {});
  viewer.mount();
  if (offline.graph) viewer.applyGraph(offline.graph.document, offline.graph.version);
  else viewer.clearGraph();
  for (const name of ['tasks', 'chat']) {
    const button = viewer.root.querySelector('[data-section-tab="' + name + '"]');
    button.disabled = true;
    button.title = '离线文件仅供浏览；此功能需要本地回看服务';
  }
  viewer.activate();
  window.addEventListener('resize', () => viewer.resize());
  window.replayOffline = { viewer, source }; // same viewer debug surface as the live application
} catch (error) {
  const message = document.createElement('p');
  message.className = 'workspace-empty';
  message.textContent = '离线回放无法打开: ' + error.message;
  pane.appendChild(message);
}
