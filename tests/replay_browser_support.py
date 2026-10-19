"""Shared harness of the real-browser replay tests (opt-in: RUN_BROWSER_TESTS=1, Selenium and Chrome).

The replay workspace service and its fixture bundles always run in WSL (tests/replay_web_fixture_server.py, where the
service and the finalizer live); the test process drives Chrome. `just test` runs the browser tests with the Windows
Python that has Selenium: it then starts the fixture service through `wsl.exe` in the distribution and directory of
the repository's \\\\wsl.localhost\\... path, and Windows Chrome reaches the service on 127.0.0.1 through WSL's
localhost forwarding. In WSL with Selenium installed the fixture service is a plain child process.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
RUN_BROWSER_TESTS = os.environ.get("RUN_BROWSER_TESTS") == "1"

try:
	from selenium import webdriver
	from selenium.webdriver.common.action_chains import ActionChains
	from selenium.webdriver.common.by import By
	from selenium.webdriver.common.keys import Keys
	from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
	webdriver = ActionChains = By = Keys = WebDriverWait = None


def find_chrome() -> Optional[str]:
	configured = os.environ.get("CHROME_BINARY")
	if configured and Path(configured).is_file():
		return configured
	for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
		found = shutil.which(command)
		if found:
			return found
	for candidate in (
		r"C:\Program Files\Google\Chrome\Application\chrome.exe",
		r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
	):
		if Path(candidate).is_file():
			return candidate
	return None


CHROME_BINARY = find_chrome()
if RUN_BROWSER_TESTS and webdriver is None:
	raise RuntimeError("RUN_BROWSER_TESTS=1 requires the Selenium Python package")
if RUN_BROWSER_TESTS and CHROME_BINARY is None:
	raise RuntimeError("RUN_BROWSER_TESTS=1 requires Chrome, Chromium, or CHROME_BINARY")


def make_driver(width: int = 1280, height: int = 1000):
	options = webdriver.ChromeOptions()
	options.binary_location = CHROME_BINARY
	for argument in ("--headless=new", "--disable-gpu", "--no-sandbox", "--no-proxy-server", f"--window-size={width},{height}"):
		options.add_argument(argument)
	options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
	return webdriver.Chrome(options=options)


_WSL_UNC = re.compile(r"^\\\\(?:wsl\.localhost|wsl\$)\\(?P<distro>[^\\]+)(?P<tail>\\.*)?$", re.IGNORECASE)


def fixture_command() -> Tuple[List[str], Optional[str]]:
	if os.name == "nt":
		match = _WSL_UNC.match(str(REPO))
		if not match:
			raise RuntimeError(f"on Windows the browser tests must run from the WSL checkout (\\\\wsl.localhost\\...), not {REPO}")
		linux = (match.group("tail") or "\\").replace("\\", "/")
		return ["wsl.exe", "-d", match.group("distro"), "--cd", linux, "--exec", "python3", "-m", "tests.replay_web_fixture_server"], None
	return [os.environ.get("REPLAY_FIXTURE_PYTHON", sys.executable), "-m", "tests.replay_web_fixture_server"], str(REPO)


class FixtureService:
	"""The fixture service process: `base` is the workspace URL, `call` sends one command."""

	def __init__(self, timeout: float = 180.0):
		argv, cwd = fixture_command()
		self.process = subprocess.Popen(
			argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			text=True, encoding="utf-8", errors="replace", bufsize=1,
		)
		self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
		self.errors: List[str] = []
		threading.Thread(target=self._pump, daemon=True).start()
		threading.Thread(target=self._drain, daemon=True).start()
		ready = self._read(timeout)
		self.base = ready["base"]
		self.root = ready["root"]

	def _pump(self) -> None:
		for line in self.process.stdout:
			self.lines.put(line)
		self.lines.put(None)

	def _drain(self) -> None:
		for line in self.process.stderr:
			self.errors.append(line)
			del self.errors[:-200]

	def _read(self, timeout: float) -> Dict[str, Any]:
		try:
			line = self.lines.get(timeout=timeout)
		except queue.Empty as error:
			raise RuntimeError("the fixture service did not answer:\n" + "".join(self.errors[-40:])) from error
		if line is None:
			raise RuntimeError("the fixture service exited:\n" + "".join(self.errors[-40:]))
		return json.loads(line)

	def call(self, cmd: str, timeout: float = 180.0, **arguments: Any) -> Dict[str, Any]:
		self.process.stdin.write(json.dumps({"cmd": cmd, **arguments}) + "\n")
		self.process.stdin.flush()
		reply = self._read(timeout)
		if "error" in reply:
			raise RuntimeError(f"fixture command {cmd} failed: {reply['error']}\n" + "".join(self.errors[-20:]))
		return reply

	def close(self) -> None:
		try:
			self.process.stdin.close()
			self.process.wait(timeout=30)
		except (OSError, subprocess.TimeoutExpired):
			self.process.kill()


def severe_logs(driver) -> List[Dict[str, Any]]:
	return [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
