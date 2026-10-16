#!/usr/bin/env python3
"""Make git-based pip requirements installable behind an HTTP proxy that stalls on partial clones.

pip clones `name @ git+https://...` requirements with `git clone --filter=blob:none`. Through the
proxy used here that request never gets an answer and pip hangs for hours, while a plain
`git clone --depth 1` of the same repository takes seconds. This helper therefore clones every such
repository shallowly into a vendor directory and points the requirement at the local copy.

Usage:
	vendor_git_requirements.py <requirements.txt> <vendor_dir>      rewritten requirements on stdout
	vendor_git_requirements.py --clone <git_url> <vendor_dir>       just clone, print the local path

The commit that was cloned is reported on stderr so it can be recorded.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

GIT_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*@\s*git\+(\S+?)(?:@(\S+))?\s*$")
CLONE_TIMEOUT_S = 120


def shallow_clone(url: str, vendor: Path, ref: str = None) -> Path:
	"""Shallow-clone url into vendor/<repo name> (reusing an existing clone) and return its path."""
	repo = url.rstrip("/").split("/")[-1]
	if repo.endswith(".git"):
		repo = repo[:-4]
	dest = vendor / repo
	if not (dest / ".git").exists():
		vendor.mkdir(parents=True, exist_ok=True)
		# The proxy occasionally drops a response without closing the connection; git then waits
		# forever (http.lowSpeedLimit does not fire). A shallow clone normally takes seconds, so
		# kill attempts that run longer than CLONE_TIMEOUT_S and try again.
		for attempt in range(1, 6):
			shutil.rmtree(dest, ignore_errors=True)
			cmd = ["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [url, str(dest)]
			try:
				if subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=CLONE_TIMEOUT_S).returncode == 0:
					break
				print(f"clone of {url} failed (attempt {attempt}/5)", file=sys.stderr)
			except subprocess.TimeoutExpired:
				print(f"clone of {url} stalled for {CLONE_TIMEOUT_S}s, retrying (attempt {attempt}/5)", file=sys.stderr)
		else:
			raise SystemExit(f"could not clone {url}")
	sha = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
	print(f"vendored {url} @ {sha} -> {dest}", file=sys.stderr)
	return dest


def main() -> None:
	if len(sys.argv) == 4 and sys.argv[1] == "--clone":
		print(shallow_clone(sys.argv[2], Path(sys.argv[3])))
		return
	if len(sys.argv) != 3:
		raise SystemExit(__doc__)

	requirements, vendor = Path(sys.argv[1]), Path(sys.argv[2])
	for line in requirements.read_text().splitlines():
		match = GIT_REQUIREMENT.match(line)
		if not match:
			print(line)
			continue
		name, url, ref = match.groups()
		print(f"{name} @ file://{shallow_clone(url, vendor, ref)}")


if __name__ == "__main__":
	main()
