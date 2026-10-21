"""Export a finalized result as one offline HTML. Standard library only, Python 3.8.

Run in replay_ws: python3 -m scripts.replay_web.standalone RESULT OUTPUT.html
Only manifest-declared replay assets and the normalized active graph are shared.
"""
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile

from scripts.replay_web.artifacts import normalize_graph, select_active_graph
from scripts.replay_web.manifest import ManifestError, resolve_bundle_member, validate_manifest

STATIC = Path(__file__).resolve().parent / "static"


def safe_json(value):
    # JSON inside an HTML script element must never contain an HTML closing tag.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def viewer_script():
    data = (STATIC / "replay_data.js").read_text(encoding="utf-8")
    viewer = (STATIC / "viewer.js").read_text(encoding="utf-8")
    viewer = viewer.replace("import { ChunkError } from './replay_data.js';", "")
    parts = []
    for source, names in ((data, "ReplayDataSource, ChunkError"), (viewer, "ReplayViewer")):
        source = re.sub(r"^export (?=(?:async )?(?:class|function|const) )", "", source, flags=re.M)
        if re.search(r"^(?:import|export) ", source, re.M):
            raise ValueError("offline viewer has an unsupported module dependency")
        parts.append("(() => {\n" + source + "\nreturn {" + names + "};\n})()")
    script = ("const {ReplayDataSource, ChunkError} = " + parts[0] + ";\n"
              + "const {ReplayViewer} = " + parts[1] + ";\n"
              + (STATIC / "standalone.js").read_text(encoding="utf-8"))
    return re.sub(r"</script", r"<\\/script", script, flags=re.I)


def write_standalone(result, output, *, expected_manifest=None):
    """Publish a complete HTML atomically; never overwrite an existing destination."""
    result, output = Path(result).expanduser().resolve(), Path(output).expanduser().absolute()
    bundle = result / "replay"
    manifest = validate_manifest(bundle, verify_digests=True)
    if expected_manifest is not None and manifest != expected_manifest:
        raise ManifestError("bundle changed; reopen the result before exporting")
    if output.exists() or output.is_symlink():
        raise FileExistsError("output already exists: " + str(output))
    if bundle.resolve() in output.resolve().parents:
        raise ValueError("export output must be outside the replay bundle")
    graph = select_active_graph(result)
    graph = {"document": normalize_graph(graph[0]), "version": graph[1].to_json()} if graph else None
    shared = dict(manifest)
    shared["dataset"] = dict(manifest["dataset"], source=None)
    shared["build"] = {}  # local preparation paths and environment are not required to view the result
    paths = {item["path"]: item for item in manifest["chunks"]}
    for entry in manifest["images"]["timeline"]:
        for camera in manifest["cameras"]:
            name = entry.get(camera["key"])
            if name:
                paths.setdefault(name, {})
    for entry in manifest["images"]["descriptions"].values():
        paths.setdefault(entry["path"], {})
    reference = manifest.get("reference_mesh")
    if reference:
        paths[reference["path"]] = reference
    title = html.escape(manifest.get("title") or result.name)
    script = viewer_script()
    css = (STATIC / "replay.css").read_text(encoding="utf-8")
    css += "\nbody { grid-template-rows: var(--bar-h) minmax(0, 1fr); }\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(output.parent),
                prefix=".replay-export-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write('<!doctype html>\n<html lang="zh"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                'script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data: blob:; '
                'connect-src \'none\'; base-uri \'none\'; form-action \'none\'">'
                '<title>' + title + ' · 离线回放</title><style>' + css + '</style></head><body>'
                '<header id="workspace-bar"><h1 class="brand">DAAAM 离线回放</h1>'
                '<span>独立 HTML · 可离线浏览 · 任务与问答需要本地服务</span></header>'
                '<main id="workspace" class="workspace"><section id="offline-pane" class="result-pane">'
                '</section></main><noscript>请启用 JavaScript 以浏览地图。</noscript>'
                '<script id="replay-offline-data" type="application/json">')
            stream.write('{"manifest":' + safe_json(shared) + ',"graph":' + safe_json(graph) + ',"assets":{')
            for index, (name, metadata) in enumerate(paths.items()):
                raw = resolve_bundle_member(bundle, name).read_bytes()
                if metadata.get("sha256") and hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
                    raise ManifestError("sha256 checksum changed during export: " + name)
                if index:
                    stream.write(",")
                stream.write(safe_json(name) + ':"')
                stream.write(base64.b64encode(raw).decode("ascii"))
                stream.write('"')
            if validate_manifest(bundle, verify_digests=True) != manifest:
                raise ManifestError("bundle changed during export; retry after processing finishes")
            stream.write('}}</script><script>\n' + script + '\n</script></body></html>\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic exclusive publication: a concurrent export can never replace another file.
        os.link(str(temporary), str(output))
    finally:
        if temporary is not None:
            temporary.unlink()
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="将建图结果导出为可离线浏览的独立 HTML")
    parser.add_argument("result", type=Path, help="含 replay/manifest.json 的结果目录")
    parser.add_argument("output", type=Path, help="新的 .html 文件路径（不覆盖已有文件）")
    args = parser.parse_args(argv)
    try:
        output = write_standalone(args.result, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, "导出失败: " + str(error) + "\n")
    print("已导出: {} ({:.1f} MiB)".format(output, output.stat().st_size / 1048576))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
