"""Hand freed heap memory back to the operating system.

glibc keeps memory that Python, PyTorch and numpy have freed inside its arenas, so a process that loaded large models
(the grounding worker) or churns through many frame arrays (the pipeline node) stays resident at its peak size, and on
a machine with little memory the kernel swaps that dead memory out instead of dropping it. malloc_trim() gives the free
pages back. Measured on CODa sequence 0 it returns about 0.5 GB in the grounding worker after the models are loaded and
70-80 MB per call in the pipeline node: helpful, but not enough to fit a whole 8213-frame sequence in 15.9 GB RAM + 4 GB swap.
"""
import ctypes
import ctypes.util
import gc
import os
from typing import Callable, Optional

_trim: Optional[Callable[[int], int]] = None
_trim_loaded = False


def _malloc_trim() -> Optional[Callable[[int], int]]:
	"""malloc_trim from glibc, or None where it does not exist (musl, macOS, Windows)."""
	global _trim, _trim_loaded
	if not _trim_loaded:
		_trim_loaded = True
		try:
			libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
			libc.malloc_trim.argtypes = [ctypes.c_size_t]
			libc.malloc_trim.restype = ctypes.c_int
			_trim = libc.malloc_trim
		except (OSError, AttributeError):
			_trim = None
	return _trim


def resident_mb() -> float:
	"""Resident set size of this process in MB (Linux only, 0.0 elsewhere)."""
	try:
		with open("/proc/self/statm") as statm:
			return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
	except (OSError, ValueError, IndexError):
		return 0.0


def release_freed_memory() -> float:
	"""Collect garbage, then return the free heap pages to the OS; the drop of the resident size in MB (0.0 where unsupported)."""
	before = resident_mb()
	gc.collect()
	trim = _malloc_trim()
	if trim is None:
		return 0.0
	trim(0)
	return max(before - resident_mb(), 0.0)


class _Mallinfo2(ctypes.Structure):
	_fields_ = [(name, ctypes.c_size_t) for name in (
		'arena', 'ordblks', 'smblks', 'hblks', 'hblkhd', 'usmblks',
		'fsmblks', 'uordblks', 'fordblks', 'keepcost')]


def allocation_stats() -> dict:
	"""Diagnostic allocator counters, not a whole-process memory limit."""
	import tracemalloc

	stats = {'rss_mib': resident_mb(), 'glibc': None, 'python_traced_bytes': None}
	try:
		libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6')
		libc.mallinfo2.restype = _Mallinfo2
		libc.mallinfo2.argtypes = []
		info = libc.mallinfo2()
		stats['glibc'] = {name: getattr(info, name) for name in ('arena', 'uordblks', 'fordblks', 'hblkhd')}
	except (OSError, AttributeError):
		pass
	if tracemalloc.is_tracing():
		stats['python_traced_bytes'], stats['python_traced_peak_bytes'] = tracemalloc.get_traced_memory()
	return stats
