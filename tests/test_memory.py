import importlib.util
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]


def load_memory_module():
	"""daaam/utils/memory.py without importing the daaam package (and with it torch)."""
	spec = importlib.util.spec_from_file_location("daaam_memory_under_test", REPO / "src" / "daaam" / "utils" / "memory.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class ReleaseFreedMemoryTest(unittest.TestCase):
	def test_resident_size_is_positive_on_linux(self) -> None:
		memory = load_memory_module()
		self.assertGreater(memory.resident_mb(), 0.0)

	def test_release_never_reports_a_negative_drop(self) -> None:
		memory = load_memory_module()
		self.assertGreaterEqual(memory.release_freed_memory(), 0.0)

	def test_returns_zero_where_malloc_trim_does_not_exist(self) -> None:
		memory = load_memory_module()
		with mock.patch.object(memory, "_malloc_trim", return_value=None):
			self.assertEqual(memory.release_freed_memory(), 0.0)

	def test_missing_proc_file_gives_zero(self) -> None:
		memory = load_memory_module()
		with mock.patch("builtins.open", side_effect=OSError):
			self.assertEqual(memory.resident_mb(), 0.0)

	def test_calls_malloc_trim_once_with_zero_padding(self) -> None:
		memory = load_memory_module()
		trim = mock.Mock(return_value=1)
		with mock.patch.object(memory, "_malloc_trim", return_value=trim):
			memory.release_freed_memory()
		trim.assert_called_once_with(0)


if __name__ == "__main__":
	unittest.main()
