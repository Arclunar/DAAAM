"""Turn a path typed or pasted into the replay workspace into a Linux path of this WSL distribution.

Accepted forms (the result is not resolved yet; scripts/replay_web/runs.py resolves symlinks and checks the result):

    /home/me/DAAAM/output/tum_replay/out_...           Linux path
    /mnt/e/daaam-data/output/coda/out_...             Windows drive as WSL mounts it
    E:\\daaam-data\\output\\coda\\out_...  or  E:/...      Windows drive path            -> /mnt/e/...
    \\\\wsl.localhost\\<distro>\\home\\...  or  \\\\wsl$\\<distro>\\...  WSL path seen from Windows  -> /home/...

One pair of surrounding quotes is removed (Windows Explorer's "Copy as path" adds them). A UNC path of another
distribution or of a network share, a relative path and `~` are rejected rather than guessed.

Standard library only, Python 3.8.
"""

import re
from pathlib import Path, PurePosixPath

WINDOWS_DRIVE = re.compile(r"^(?P<drive>[A-Za-z]):(?:[\\/](?P<tail>.*))?$")
WSL_UNC = re.compile(r"^[\\/]{2}(?P<host>wsl\.localhost|wsl\$)(?:[\\/]+(?P<distro>[^\\/]+)(?P<tail>(?:[\\/].*)?))?$", re.IGNORECASE)


class PathError(ValueError):
	"""A path the workspace cannot open; the message says which forms are accepted."""


def _posix_tail(tail: str) -> PurePosixPath:
	parts = [part for part in re.split(r"[\\/]+", tail or "") if part]
	return PurePosixPath(*parts) if parts else PurePosixPath()


def normalize_user_path(raw: str, *, distro: str) -> Path:
	"""The absolute Linux path that `raw` names inside the WSL distribution `distro`."""
	if not isinstance(raw, str):
		raise PathError("the result path must be text")
	value = raw.strip()
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
		value = value[1:-1].strip()
	if not value:
		raise PathError("enter the absolute path of a result directory")
	if "\x00" in value:
		raise PathError("the result path contains a NUL character")

	match = WINDOWS_DRIVE.match(value)
	if match:
		return Path("/mnt") / match.group("drive").lower() / _posix_tail(match.group("tail"))
	if value[:2] in ("\\\\", "//") or value[:2] == "\\/" or value[:2] == "/\\":
		match = WSL_UNC.match(value)
		if not match:
			raise PathError(f"only paths of this WSL distribution are supported (\\\\wsl.localhost\\{distro}\\...), not {value}")
		if not match.group("distro"):
			raise PathError(f"name the distribution: \\\\wsl.localhost\\{distro}\\...")
		if match.group("distro").casefold() != distro.casefold():
			raise PathError(f"{value} belongs to the WSL distribution {match.group('distro')}; this service runs in {distro}")
		return Path("/") / _posix_tail(match.group("tail"))
	if not value.startswith("/"):
		raise PathError(f"the result path must be absolute (/home/..., /mnt/<drive>/..., C:\\... or \\\\wsl.localhost\\{distro}\\...), got {value}")
	return Path(value)
