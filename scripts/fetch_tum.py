#!/usr/bin/env python3
"""Download TUM RGB-D sequences and convert them for ImageSequenceDataset: the test sets of the run scripts.

Inside the daaam-ws container (`just tum-list` and `just tum-fetch <name>...` run these):
	python scripts/fetch_tum.py list
	python scripts/fetch_tum.py get fr1_room fr3_walking_xyz --proxy http://172.19.64.1:7890

A short name frN_<sequence> stands for the TUM sequence rgbd_dataset_freiburgN_<sequence>, fetched from
https://cvg.cit.tum.de/rgbd/dataset/freiburgN/rgbd_dataset_freiburgN_<sequence>.tgz. Any sequence of
https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download that comes with a public ground truth can be named; the
table of `list` only holds the ones that were run through the pipeline. For every name:
	1. the archive is downloaded to <raw-dir>/ (resumable: a dropped connection is continued where it stopped),
	2. it is unpacked to <raw-dir>/rgbd_dataset_freiburgN_<sequence>/ and the archive is deleted (--keep-archive keeps it);
	   the unpacked sequence stays, so that scripts/prepare_tum_rgbd.py can convert it again with another stride,
	3. scripts/prepare_tum_rgbd.py writes every 2nd frame (undistorted if the camera needs it, 15 fps) to <out-dir>/tum_<name>: the folder that
	   SEQ=<name> selects in scripts/run_tum_example.sh, run_replay.sh and run_live_preview.sh.
Names that are already converted are skipped; delete <out-dir>/tum_<name> to convert one again.
"""

import argparse
import http.client
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://cvg.cit.tum.de/rgbd/dataset/freiburg{cam}/rgbd_dataset_freiburg{cam}_{seq}.tgz"
NAME_RE = re.compile(r"^fr([123])_([a-z0-9_]+)$")
STRIDE = 2  # the recordings are 30 Hz; every 2nd frame gives the 15 fps that the run scripts default to
RETRIES = 12
DEFAULT_RAW_DIR = Path("/data/tum")  # E:\daaam-data\tum on this machine (DAAAM_DATA_DIR in .env), see compose.yaml
DEFAULT_OUT_DIR = Path("/datasets")

# the sequences that were run through the pipeline: name -> (archive size in MB, what it shows)
SEQUENCES = {
	"fr1_desk": (344, "办公桌:显示器、键盘、书、杯子,Kinect 绕桌一圈;默认序列,最小"),
	"fr1_desk2": (349, "同一间办公室的第二次桌面录制,相机和 fr1_desk 相同;小而快"),
	"fr1_room": (782, "沿整个办公室绕一圈并闭环,会经过四张桌子和四周的墙;物体最多,看 places 和 rooms"),
	"fr3_long_office_household": (1483, "家居加办公室的大回环,最长;Xtion 相机,图像已校正(不需要去畸变)"),
	"fr3_walking_xyz": (527, "两个人在办公室里走动,相机沿 xyz 平移;动态物体"),
}


def sequence_name(name: str) -> str:
	"""frN_<sequence> -> rgbd_dataset_freiburgN_<sequence>, the folder inside the archive."""
	match = NAME_RE.match(name)
	if match is None:
		raise ValueError(f"'{name}' is not a TUM short name: expected fr1_<sequence>, fr2_<sequence> or fr3_<sequence>, e.g. fr1_room")
	return f"rgbd_dataset_freiburg{match.group(1)}_{match.group(2)}"


def archive_url(name: str) -> str:
	match = NAME_RE.match(name)
	sequence_name(name)  # validates
	return URL.format(cam=match.group(1), seq=match.group(2))


def state(name: str, raw_dir: Path, out_dir: Path) -> str:
	"""What exists on disk for this name, for the table of `list`."""
	out = out_dir / f"tum_{name}"
	if (out / "camera_info.json").is_file():
		return f"ready ({len(list((out / 'rgb').glob('*.png')))} frames)"
	if (raw_dir / sequence_name(name)).is_dir():
		return "unpacked, not converted"
	if (raw_dir / f"{sequence_name(name)}.tgz").exists():
		return "partial download"
	return "not downloaded"


def list_sets(raw_dir: Path, out_dir: Path) -> None:
	print(f"{'SEQ':<28}{'archive':>9}  {'state':<24}  what it shows")
	for name, (size_mb, note) in SEQUENCES.items():
		print(f"{name:<28}{size_mb:>6} MB  {state(name, raw_dir, out_dir):<24}  {note}")
	others = sorted(p.name[len("tum_"):] for p in out_dir.glob("tum_*") if (p / "camera_info.json").is_file())
	others = [n for n in others if n not in SEQUENCES]
	if others:
		print("其他已转换的数据集: " + ", ".join(others))
	print(f"\n选择:just tum-run 298 <SEQ 名称>(帧数写 all 为全部帧);下载:just tum-fetch <SEQ 名称>;原始数据目录 {raw_dir}")
	print("没列出的序列也能下载,名称写成 fr1_xyz、fr2_desk 这样(官网序列列表:https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download)")


def wait(attempt: int, error: Exception) -> None:
	delay = min(30, 2 * attempt + 2)
	print(f"  连接中断({type(error).__name__}: {error}),{delay} 秒后重试 {attempt + 1}/{RETRIES}", flush=True)
	time.sleep(delay)


def remote_size(opener, url: str) -> int:
	for attempt in range(RETRIES):
		try:
			with opener.open(urllib.request.Request(url, method="HEAD"), timeout=60) as response:
				return int(response.headers["Content-Length"])
		except urllib.error.HTTPError as error:
			if error.code < 500:
				raise SystemExit(f"{url}: HTTP {error.code}(这个序列不存在,或者名称写错了)")
			wait(attempt, error)
		except (OSError, http.client.HTTPException) as error:
			wait(attempt, error)
	raise SystemExit(f"连不上 {url}(是否需要 --proxy?)")


def download(opener, url: str, dest: Path, size: int) -> None:
	"""Fetch url into dest, resuming with a Range request after every dropped connection."""
	dest.parent.mkdir(parents=True, exist_ok=True)
	for attempt in range(RETRIES):
		have = dest.stat().st_size if dest.exists() else 0
		if have == size:
			return
		if have > size:
			dest.unlink()
			have = 0
		request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
		last_report = time.monotonic()
		try:
			with opener.open(request, timeout=60) as response:
				if have and response.status != 206:  # the server ignored the Range header: start again
					have = 0
				with open(dest, "ab" if have else "wb") as f:
					while True:
						chunk = response.read(1 << 20)
						if not chunk:
							break
						f.write(chunk)
						have += len(chunk)
						if time.monotonic() - last_report > 20:
							print(f"  {have / 1e6:.0f} / {size / 1e6:.0f} MB", flush=True)
							last_report = time.monotonic()
		except (OSError, http.client.HTTPException) as error:
			wait(attempt, error)
	if not dest.exists() or dest.stat().st_size != size:
		raise SystemExit(f"{dest.name} 没下完整,再运行一次会从断点继续")


def extract(archive: Path, raw_dir: Path, sequence: str) -> None:
	"""Unpack into a fresh temporary folder (the archive is untrusted data), then move the sequence into place."""
	tmp = raw_dir / f".extract-{sequence}"
	shutil.rmtree(tmp, ignore_errors=True)
	tmp.mkdir(parents=True)
	try:
		with tarfile.open(archive) as tar:
			for member in tar.getmembers():  # a TUM archive holds plain files and folders under one top folder, nothing else
				if not (member.isreg() or member.isdir()) or Path(member.name).is_absolute() or ".." in Path(member.name).parts:
					raise SystemExit(f"{archive.name}: 不安全的压缩包成员 {member.name!r},已中止解压")
			tar.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
		source = tmp / sequence
		if not (source / "rgb.txt").is_file():
			raise SystemExit(f"{archive.name} 里没有 {sequence}/rgb.txt,不是预期的 TUM 压缩包")
		shutil.rmtree(raw_dir / sequence, ignore_errors=True)
		source.replace(raw_dir / sequence)
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def convert(sequence_dir: Path, out: Path) -> None:
	part = out.with_name(out.name + ".part")
	shutil.rmtree(part, ignore_errors=True)
	script = Path(__file__).resolve().with_name("prepare_tum_rgbd.py")
	# -I: the converter reads files from the unpacked archive, so it must not import anything from that neighbourhood
	subprocess.run([sys.executable, "-I", str(script), str(sequence_dir), str(part), "--stride", str(STRIDE)], check=True, stdin=subprocess.DEVNULL)
	shutil.rmtree(out, ignore_errors=True)
	part.replace(out)


def get(name: str, raw_dir: Path, out_dir: Path, proxy: str, keep_archive: bool) -> None:
	sequence = sequence_name(name)
	out = out_dir / f"tum_{name}"
	if (out / "camera_info.json").is_file():
		print(f"{name}: 已经转换过了({out}),跳过;要重做就删掉这个目录")
		return
	archive = raw_dir / f"{sequence}.tgz"
	if not (raw_dir / sequence / "rgb.txt").is_file():
		url = archive_url(name)
		opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []))
		size = remote_size(opener, url)
		raw_dir.mkdir(parents=True, exist_ok=True)
		free = shutil.disk_usage(raw_dir).free
		if free < 2.2 * size:  # archive plus the unpacked sequence
			raise SystemExit(f"{raw_dir} 只剩 {free / 1e9:.1f} GB,下载和解压需要约 {2.2 * size / 1e9:.1f} GB")
		print(f"{name}: 下载 {url}({size / 1e6:.0f} MB)", flush=True)
		start = time.monotonic()
		download(opener, url, archive, size)
		print(f"{name}: 下载完成,用时 {time.monotonic() - start:.0f} 秒;解压到 {raw_dir / sequence}", flush=True)
		try:
			extract(archive, raw_dir, sequence)
		except (tarfile.TarError, EOFError, OSError) as error:
			archive.unlink(missing_ok=True)  # a damaged archive would fail the same way again
			raise SystemExit(f"{name}: 解压失败({error}),已删除这个压缩包,请重新运行")
	print(f"{name}: 转换(每 {STRIDE} 帧取 1 帧,去畸变)到 {out}", flush=True)
	convert(raw_dir / sequence, out)
	if archive.exists() and not keep_archive:
		archive.unlink()
	print(f"{name}: 就绪。用法:just tum-run 298 {name}(帧数写 all 为全部帧)", flush=True)


def main() -> None:
	parser = argparse.ArgumentParser(description="Download and convert TUM RGB-D sequences", formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help="archives and unpacked sequences (default: %(default)s)")
	parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="where tum_<name> is written (default: %(default)s)")
	commands = parser.add_subparsers(dest="command", required=True)
	commands.add_parser("list", help="show the known sequences and what is on disk")
	fetch = commands.add_parser("get", help="download, unpack and convert sequences")
	fetch.add_argument("names", nargs="+", help="short names such as fr1_room")
	fetch.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"), help="HTTP proxy (default: $HTTPS_PROXY)")
	fetch.add_argument("--keep-archive", action="store_true", help="keep the .tgz after unpacking")
	args = parser.parse_args()

	if args.command == "list":
		list_sets(args.raw_dir, args.out_dir)
		return
	try:
		for name in args.names:
			sequence_name(name)
	except ValueError as error:
		parser.error(str(error))
	for name in args.names:
		get(name, args.raw_dir, args.out_dir, args.proxy, args.keep_archive)


if __name__ == "__main__":
	main()
