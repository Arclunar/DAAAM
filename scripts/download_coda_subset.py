#!/usr/bin/env python3
"""Download the first N frames of a CODa sequence without fetching the whole zip.

coda-devkit's scripts/download_split.py downloads sequences/<seq>.zip in one go (44.7 GB for sequence 0) and cannot
resume. This script reads the zip index with HTTP Range requests, then fetches and unpacks only the wanted files into
the same layout (<dest>/2d_rect/cam0/<seq>/...). Every file is CRC-checked, a rerun resumes where it stopped, and
connection errors are retried (a proxy drops connections now and then).

  python3 scripts/download_coda_subset.py -d /path/to/CODa -se 0 --frames 1000 --proxy http://172.19.64.1:7890

Fetched: the cam0/cam1 images of the first --frames frames (all frames if 0) and the small per-sequence files
(calibrations, poses, timestamps, metadata). The LiDAR scans (3d_comp), the cam3 raw depth (3d_raw) and the 3D
annotations (3d_bbox, 3d_semantic) are not used by DAAAM and are skipped unless named in --include.
"""
import argparse
import collections
import http.client
import io
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.parse
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = "https://web.corral.tacc.utexas.edu/texasrobotics/web_CODa/sequences/{seq}.zip"
SKIPPED_DIRS = ("3d_comp", "3d_raw", "3d_bbox", "3d_semantic")
IMAGE_RE = re.compile(r"^2d_rect/(cam\d+)/[^/]+/.*_(\d+)\.png$")
RETRIES = 8
TIMEOUT = 120

Entry = collections.namedtuple("Entry", "name offset csize size crc method")


class Remote:
    """One HTTPS connection to the archive (through an HTTP proxy if given), re-opened after errors."""

    def __init__(self, url, proxy):
        parts = urllib.parse.urlsplit(url)
        self.host, self.path = parts.hostname, parts.path
        self.proxy = urllib.parse.urlsplit(proxy) if proxy else None
        self.conn = None

    def _request(self, method, headers, expect):
        error = None
        for attempt in range(RETRIES):
            try:
                if self.conn is None:
                    if self.proxy:
                        self.conn = http.client.HTTPSConnection(self.proxy.hostname, self.proxy.port, timeout=TIMEOUT)
                        self.conn.set_tunnel(self.host, 443)
                    else:
                        self.conn = http.client.HTTPSConnection(self.host, 443, timeout=TIMEOUT)
                self.conn.request(method, self.path, headers=headers)
                response = self.conn.getresponse()
                if response.status != expect:  # e.g. 200 = the server ignored the Range header: never read that body
                    raise IOError("HTTP %d (expected %d)" % (response.status, expect))
                return response.getheader("Content-Length"), response.read()
            except (OSError, http.client.HTTPException) as e:
                error = e
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                time.sleep(min(2 ** attempt, 20))
        raise error

    def archive_size(self):
        return int(self._request("HEAD", {}, 200)[0])

    def read_range(self, start, length):
        data = self._request("GET", {"Range": "bytes=%d-%d" % (start, start + length - 1)}, 206)[1]
        if len(data) != length:
            raise IOError("short read: %d of %d bytes" % (len(data), length))
        return data


class RangeFile(io.RawIOBase):
    """Read-only file object over HTTP Range requests, so that zipfile can read the index without the archive."""

    def __init__(self, remote, size):
        self.remote, self.size, self.pos = remote, size, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        data = self.remote.read_range(self.pos, n)
        self.pos += n
        return data


def load_index(remote, size, cache):
    if os.path.exists(cache):
        with open(cache) as f:
            cached = json.load(f)
        if cached["archive_size"] == size:
            return [Entry(*e) for e in cached["entries"]]
    infos = zipfile.ZipFile(RangeFile(remote, size)).infolist()
    entries = [Entry(i.filename, i.header_offset, i.compress_size, i.file_size, i.CRC, i.compress_type)
               for i in infos if not i.is_dir()]
    with open(cache, "w") as f:
        json.dump({"archive_size": size, "entries": entries}, f)
    return entries


def select(entries, frames, include):
    """The entries to fetch, metadata first, then the images by frame number."""
    skipped = tuple(d for d in SKIPPED_DIRS if d not in include)
    ids = collections.defaultdict(set)
    for e in entries:
        m = IMAGE_RE.match(e.name)
        if m:
            ids[m.group(1)].add(int(m.group(2)))
    keep = set(sorted(ids["cam0"])[:frames]) if frames else None
    picked = []
    for e in entries:
        m = IMAGE_RE.match(e.name)
        if m:
            if keep is None or int(m.group(2)) in keep:
                picked.append((int(m.group(2)), e))
        elif e.name.split("/")[0] not in skipped and not e.name.startswith("2d_rect/"):
            picked.append((-1, e))
    return [e for _, e in sorted(picked, key=lambda p: (p[0], p[1].name))]


def fetch(remote, size, e, root):
    """Fetch, unpack and verify one file; returns the bytes transferred (0 if it was already there)."""
    out = os.path.abspath(os.path.join(root, *e.name.split("/")))
    if os.path.commonpath([root, out]) != root:
        raise ValueError("unsafe path in the archive: %s" % e.name)
    if os.path.exists(out) and os.path.getsize(out) == e.size:
        return 0
    # local header (30 bytes + name + a short extra field), then the data
    buf = remote.read_range(e.offset, min(30 + len(e.name.encode()) + 256 + e.csize, size - e.offset))
    if buf[:4] != b"PK\x03\x04":
        raise IOError("no local file header at offset %d" % e.offset)
    start = 30 + sum(struct.unpack("<HH", buf[26:30]))
    if len(buf) < start + e.csize:
        buf = remote.read_range(e.offset, start + e.csize)
    packed = buf[start:start + e.csize]
    if e.method == zipfile.ZIP_DEFLATED:
        inflater = zlib.decompressobj(-15)
        data = inflater.decompress(packed) + inflater.flush()
    elif e.method == zipfile.ZIP_STORED:
        data = packed
    else:
        raise IOError("unsupported compression method %d" % e.method)
    if len(data) != e.size or zlib.crc32(data) & 0xFFFFFFFF != e.crc:
        raise IOError("size or CRC mismatch")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out + ".part", "wb") as f:
        f.write(data)
    os.replace(out + ".part", out)
    return len(packed)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-d", "--dest", required=True, help="CODa root directory (the future CODA_ROOT_DIR)")
    ap.add_argument("-se", "--sequence", default="0", help="CODa sequence number (default 0)")
    ap.add_argument("--frames", type=int, default=0, help="fetch the first N frames only (0 = all frames)")
    ap.add_argument("--include", nargs="*", default=[], choices=SKIPPED_DIRS, help="also fetch these groups")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
                    help="HTTP proxy, e.g. http://172.19.64.1:7890 (default: $HTTPS_PROXY)")
    ap.add_argument("--workers", type=int, default=6, help="parallel connections (default 6)")
    ap.add_argument("--list", action="store_true", help="only print what would be fetched")
    args = ap.parse_args()

    root = os.path.abspath(args.dest)
    os.makedirs(root, exist_ok=True)
    url = URL.format(seq=args.sequence)
    remote = Remote(url, args.proxy)
    size = remote.archive_size()
    entries = load_index(remote, size, os.path.join(root, ".zip_index_seq%s.json" % args.sequence))
    todo = select(entries, args.frames, set(args.include))
    missing = [e for e in todo if not (os.path.exists(os.path.join(root, *e.name.split("/")))
                                       and os.path.getsize(os.path.join(root, *e.name.split("/"))) == e.size)]
    print("%s: %.2f GB, %d files; selected %d files (%.2f GB), %d still missing (%.2f GB)" % (
        url, size / 1e9, len(entries), len(todo), sum(e.csize for e in todo) / 1e9,
        len(missing), sum(e.csize for e in missing) / 1e9), flush=True)
    if args.list or not missing:
        return 0

    lock, local, stop = threading.Lock(), threading.local(), threading.Event()
    stats = {"done": 0, "bytes": 0, "failed": []}
    started = time.time()

    def work(e):
        if stop.is_set():
            return
        if not hasattr(local, "remote"):
            local.remote = Remote(url, args.proxy)
        for attempt in range(3):  # connection errors are retried inside Remote; this covers CRC mismatches
            try:
                n = fetch(local.remote, size, e, root)
                break
            except Exception as err:
                if attempt == 2:
                    with lock:
                        stats["failed"].append((e.name, repr(err)))
                    return
        with lock:
            stats["done"] += 1
            stats["bytes"] += n

    with ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(work, e) for e in missing]
        try:
            for i, f in enumerate(as_completed(futures), 1):
                if i % 100 == 0 or i == len(futures):
                    elapsed = time.time() - started
                    print("%d/%d files, %.2f GB, %.1f MB/s, %d failed" % (
                        i, len(futures), stats["bytes"] / 1e9, stats["bytes"] / 1e6 / elapsed, len(stats["failed"])),
                        flush=True)
        except KeyboardInterrupt:
            stop.set()
            print("interrupted: run the same command again to resume", flush=True)
            return 130

    for name, err in stats["failed"]:
        print("FAILED %s: %s" % (name, err), file=sys.stderr)
    print("done: %d files fetched in %.0f s; CODA_ROOT_DIR=%s" % (stats["done"], time.time() - started, root))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
