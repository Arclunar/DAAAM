import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[1]
RUN_BROWSER_TESTS = os.environ.get("RUN_BROWSER_TESTS") == "1"

try:
	from selenium import webdriver
	from selenium.webdriver.common.by import By
	from selenium.webdriver.common.action_chains import ActionChains
	from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
	webdriver = None


def find_chrome():
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


def write_reference(path, side=30):
	"""A flat side x side grid (two triangles per cell), the stand-in for the cached 1 cm reference mesh."""
	ys, xs = np.mgrid[0:side, 0:side]
	positions = np.stack([xs.ravel() * 0.05 - 0.5, ys.ravel() * 0.05 - 0.5, np.full(side * side, -0.1)], axis=1).astype(np.float32)
	index = np.arange(side * side).reshape(side, side)
	a, b, c, d = index[:-1, :-1].ravel(), index[:-1, 1:].ravel(), index[1:, :-1].ravel(), index[1:, 1:].ravel()
	np.savez(
		path,
		positions=positions,
		colors=np.tile(np.array([90, 140, 200], np.uint8), (side * side, 1)),
		normals=np.tile(np.array([0.0, 0.0, 1.0], np.float32), (side * side, 1)),
		triangles=np.concatenate([np.stack([a, b, c], axis=1), np.stack([b, d, c], axis=1)]).astype(np.uint32),
	)
	return side * side, 2 * (side - 1) ** 2


def write_rich_snapshot(path, frame):
	"""Anchors that overlap on screen: three objects a few centimetres apart, two places, a room and a building."""
	lower = np.array([[0.0, 0.0, 0.0], [0.02, 0.01, 0.0], [0.04, 0.02, 0.0]], np.float32)
	arrays = {
		"mesh_pos": np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 2.0]], np.float32),
		"mesh_col": np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]], np.uint8),
		"mesh_faces": np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], np.uint32),
		"obj_cid": np.array([11, 12, 13], np.int32),
		"obj_label": np.array([42, 43, 44], np.uint32),
		"obj_min": lower,
		"obj_max": lower + np.array([0.4, 0.4, 0.6], np.float32),
		"place_cid": np.array([3, 4], np.int32),
		"place_pos": np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.0]], np.float32),
		"place_rad": np.array([0.2, 0.2], np.float32),
		"place_edges": np.zeros((0, 2), np.int32),
		"room_pos": np.array([[0.5, 0.5, 0.0]], np.float32),
		"room_place_edges": np.zeros((0, 2), np.int32),
		"bld_pos": np.array([[0.5, 0.5, 0.0]], np.float32),
		"bld_room_edges": np.zeros((0, 2), np.int32),
		"obj_place_edges": np.zeros((0, 2), np.int32),
		"agent_pos": np.array([[0.0, 0.0, 0.0]], np.float32),
		"agent_quat": np.array([[1.0, 0.0, 0.0, 0.0]], np.float32),
		"agent_t": np.array([frame / 15.0], np.float64),
		"place_t": np.array([frame / 15.0], np.float64),
	}
	np.savez(path / ("snap_%06d.npz" % frame), **arrays)


def build_page(recording, page, reference=None):
	command = [sys.executable, str(REPO / "scripts" / "replay.py"), "build", str(recording), "--out", str(page)]
	if reference is not None:
		command += ["--reference-mesh", str(reference)]
	return subprocess.run(command, cwd=REPO, capture_output=True, text=True)


def make_driver():
	options = webdriver.ChromeOptions()
	options.binary_location = CHROME_BINARY
	options.add_argument("--headless=new")
	options.add_argument("--disable-gpu")
	options.add_argument("--no-sandbox")
	options.add_argument("--window-size=1280,800")
	options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
	return webdriver.Chrome(options=options)


def write_snapshot(path, frame, object_id, place_id, offset):
	arrays = {
		"mesh_pos": np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 2.0]], np.float32),
		"mesh_col": np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]], np.uint8),
		"mesh_faces": np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], np.uint32),
		"obj_cid": np.array([object_id], np.int32),
		"obj_label": np.array([42], np.uint32),
		"obj_min": np.array([[offset, 0.0, 0.0]], np.float32),
		"obj_max": np.array([[offset + 0.4, 0.4, 0.6]], np.float32),
		"place_cid": np.array([place_id], np.int32),
		"place_pos": np.array([[offset, 0.0, 0.0]], np.float32),
		"place_rad": np.array([0.2], np.float32),
		"place_edges": np.zeros((0, 2), np.int32),
		"room_pos": np.zeros((0, 3), np.float32),
		"room_place_edges": np.zeros((0, 2), np.int32),
		"bld_pos": np.zeros((0, 3), np.float32),
		"bld_room_edges": np.zeros((0, 2), np.int32),
		"obj_place_edges": np.array([[0, 0]], np.int32),
		"agent_pos": np.array([[offset, 0.0, 0.0]], np.float32),
		"agent_quat": np.array([[1.0, 0.0, 0.0, 0.0]], np.float32),
		"agent_t": np.array([frame / 15.0], np.float64),
		"place_t": np.array([frame / 15.0], np.float64),
	}
	np.savez(path / ("snap_%06d.npz" % frame), **arrays)


@unittest.skipUnless(RUN_BROWSER_TESTS, "set RUN_BROWSER_TESTS=1 to run the real-browser interaction test")
class ReplayBrowserTest(unittest.TestCase):
	def test_source_frame_button_opens_image_without_moving_the_timeline(self):
		import cv2

		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			recording, run, dataset = root / "recording", root / "run", root / "dataset"
			recording.mkdir(); run.mkdir(); (dataset / "rgb").mkdir(parents=True)
			write_snapshot(recording, 1, 7, 3, 0.0)
			cv2.imwrite(str(dataset / "rgb" / "000000.png"), np.full((8, 8, 3), (0, 255, 0), np.uint8))
			(run / "corrections.yaml").write_text(
				"label_names:\n  - label: 42\n    name: a green object\n"
				"    description_source:\n      frame_id: 1\n      timestamp: 0.067\n",
				encoding="utf-8",
			)
			page = root / "replay.html"
			result = subprocess.run(
				[sys.executable, str(REPO / "scripts" / "replay.py"), "build", str(recording),
				 "--run", str(run), "--dataset", str(dataset), "--thumb-width", "8", "--out", str(page)],
				cwd=REPO, capture_output=True, text=True,
			)
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

			driver = make_driver()
			try:
				driver.get(page.as_uri())
				wait = WebDriverWait(driver, 10)
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
				before = driver.execute_script("return window.replayDebug.S.cur")
				button = driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:7"] .entity-source')
				self.assertTrue(button.is_enabled())
				button.click()
				wait.until(lambda d: "open" in d.find_element(By.ID, "description-source-modal").get_attribute("class").split())
				wait.until(lambda d: d.find_element(By.ID, "description-source-image").get_property("naturalWidth") > 0)
				self.assertEqual(driver.execute_script("return window.replayDebug.S.cur"), before)
				self.assertIn("frame 1", driver.find_element(By.ID, "description-source-caption").text)
				driver.find_element(By.ID, "description-source-close").click()
				self.assertNotIn("open", driver.find_element(By.ID, "description-source-modal").get_attribute("class").split())
			finally:
				driver.quit()

	def test_playback_uses_snapshot_timestamps_at_one_times_speed(self):
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			recording = root / "recording"
			recording.mkdir()
			for frame in range(1, 13):
				write_snapshot(recording, frame, 7, 3, frame / 15.0)
			page = root / "replay.html"
			result = subprocess.run(
				[
					sys.executable,
					str(REPO / "scripts" / "replay.py"),
					"build",
					str(recording),
					"--out",
					str(page),
				],
				cwd=REPO,
				capture_output=True,
				text=True,
			)
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

			options = webdriver.ChromeOptions()
			options.binary_location = CHROME_BINARY
			options.add_argument("--headless=new")
			options.add_argument("--disable-gpu")
			options.add_argument("--no-sandbox")
			driver = webdriver.Chrome(options=options)
			try:
				driver.get(page.as_uri())
				wait = WebDriverWait(driver, 10)
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 11)
				driver.find_element(By.ID, "first").click()
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
				driver.find_element(By.ID, "play").click()
				time.sleep(0.7)
				current = driver.execute_script("return window.replayDebug.S.wanted")
				self.assertGreaterEqual(current, 7, "15 FPS data played substantially slower than its timestamps")
			finally:
				driver.quit()

	def test_entity_browser_selects_without_moving_the_timeline_and_hides_it(self):
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			recording = root / "recording"
			recording.mkdir()
			write_snapshot(recording, 1, 7, 3, 0.0)
			write_snapshot(recording, 5, 8, 4, 1.0)
			page = root / "replay.html"
			result = subprocess.run(
				[
					sys.executable,
					str(REPO / "scripts" / "replay.py"),
					"build",
					str(recording),
					"--out",
					str(page),
				],
				cwd=REPO,
				capture_output=True,
				text=True,
			)
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

			options = webdriver.ChromeOptions()
			options.binary_location = CHROME_BINARY
			options.add_argument("--headless=new")
			options.add_argument("--disable-gpu")
			options.add_argument("--no-sandbox")
			options.add_argument("--window-size=1280,800")
			options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
			driver = webdriver.Chrome(options=options)
			try:
				driver.get(page.as_uri())
				wait = WebDriverWait(driver, 10)
				wait.until(lambda d: d.find_element(By.ID, "entity-browser"))
				self.assertEqual(driver.find_element(By.ID, "bbox-mode").get_attribute("value"), "selected")
				row = driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:7"]')
				row.click()
				self.assertEqual(driver.find_element(By.ID, "slider").get_attribute("value"), "1")
				self.assertEqual(driver.execute_script("return window.replayDebug.S.selectedKey"), "object:7")
				self.assertEqual(driver.execute_script("return window.replayDebug.G.highlightNodes"), 0)
				self.assertFalse(driver.execute_script("return window.replayDebug.G.overlay.visible"))
				driver.execute_script("document.getElementById('slider').value = 0; document.getElementById('slider').dispatchEvent(new Event('input'))")
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
				# the selection is an amber node plus a screen-space box overlay, no longer thin white WebGL lines
				wait.until(lambda d: d.execute_script("return window.replayDebug.G.highlightNodes === 1 && window.replayDebug.G.overlay.visible"))
				self.assertGreater(driver.execute_script("return window.replayDebug.G.overlay.segments"), 0)
				polygon = driver.execute_script("return window.replayDebug.projectBox(window.replayDebug.S.objItems[0]).polygon")
				self.assertGreaterEqual(len(polygon), 3)
				bounds = driver.execute_script("return [document.getElementById('gl').clientWidth, document.getElementById('gl').clientHeight]")
				self.assertTrue(all(0 <= point[0] <= bounds[0] and 0 <= point[1] <= bounds[1] for point in polygon))
				camera_cases = driver.execute_script("""
					const eye = window.replayDebug.S.eye;
					const inside = window.replayDebug.projectBox({
						mn: eye.map((value) => value - 0.01),
						mx: eye.map((value) => value + 0.01),
					});
					const boundary = window.replayDebug.projectBox({
						mn: [eye[0], eye[1] - 0.01, eye[2] - 0.01],
						mx: [eye[0] + 0.01, eye[1] + 0.01, eye[2] + 0.01],
					});
					return [inside.area, boundary ? boundary.area : null];
				""")
				viewport_area = bounds[0] * bounds[1]
				self.assertEqual(camera_cases[0], viewport_area)
				self.assertTrue(camera_cases[1] is None or camera_cases[1] < viewport_area * 0.99)
				covering_face = driver.execute_script("return window.replayDebug.projectBox({mn: [-100, -100, -0.02], mx: [100, 100, 0.02]})")
				self.assertIsNotNone(covering_face)
				self.assertGreater(covering_face["area"], bounds[0] * bounds[1] * 0.2)
				self.assertIn("首次", driver.find_element(By.ID, "entity-detail").text)

				eye = driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:7"] .entity-eye')
				eye.click()
				self.assertTrue(driver.execute_script("return window.replayDebug.S.hidden.has('object:7')"))
				wait.until(lambda d: d.execute_script("return window.replayDebug.G.highlightNodes === 0 && !window.replayDebug.G.overlay.visible"))
				driver.execute_script("document.getElementById('slider').value = 1; document.getElementById('slider').dispatchEvent(new Event('input'))")
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 1)
				driver.execute_script("document.getElementById('slider').value = 0; document.getElementById('slider').dispatchEvent(new Event('input'))")
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
				self.assertTrue(driver.execute_script("return window.replayDebug.S.hidden.has('object:7')"))
				eye = driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:7"] .entity-eye')
				eye.click()
				self.assertFalse(driver.execute_script("return window.replayDebug.S.hidden.has('object:7')"))

				driver.find_element(By.ID, "entity-tab-places").click()
				place = driver.find_element(By.CSS_SELECTOR, '[data-entity-key="place:3"]')
				place.click()
				self.assertEqual(driver.execute_script("return window.replayDebug.S.selectedKey"), "place:3")

				corner = driver.execute_script(
					"return window.replayDebug.project(window.replayDebug.S.objItems[0].mn)"
				)
				canvas = driver.find_element(By.ID, "gl")
				ActionChains(driver).move_to_element_with_offset(
					canvas,
					corner[0] - canvas.size["width"] / 2 + 4,
					corner[1] - canvas.size["height"] / 2 + 4,
				).click().perform()
				wait.until(lambda d: d.execute_script("return window.replayDebug.S.selectedKey") == "object:7")
				severe = [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
				self.assertEqual(severe, [])
			finally:
				driver.quit()


LABEL_VISIBILITY_SCRIPT = """
	const d = window.replayDebug, gl = document.getElementById('gl');
	return d.labels().map((item) => {
		const el = document.querySelector('#labels .lab[data-label-key="' + item.key + '"]');
		const s = d.project(item.pos);
		const inside = !!s && s[0] >= 0 && s[0] <= gl.clientWidth && s[1] >= 0 && s[1] <= gl.clientHeight;
		return [item.key, inside, !!el && el.style.display !== 'none'];
	});
"""


@unittest.skipUnless(RUN_BROWSER_TESTS, "set RUN_BROWSER_TESTS=1 to run the real-browser interaction test")
class ReplayViewerFeaturesBrowserTest(unittest.TestCase):
	"""Mesh source selector, stable label modes and the selection overlay of scripts/replay_viewer.html."""

	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		self.recording = self.root / "recording"
		self.recording.mkdir()
		self.page = self.root / "replay.html"
		self.driver = None

	def tearDown(self):
		if self.driver is not None:
			self.driver.quit()
		self.temp.cleanup()

	def open_page(self, reference=None, rich=False):
		for frame in (1, 4):
			if rich:
				write_rich_snapshot(self.recording, frame)
			else:
				write_snapshot(self.recording, frame, 7, 3, frame / 10.0)
		result = build_page(self.recording, self.page, reference)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.driver = make_driver()
		self.driver.get(self.page.as_uri())
		self.wait = WebDriverWait(self.driver, 20)
		self.wait.until(lambda d: d.execute_script("return !!window.replayDebug && window.replayDebug.S.cur") == 1)
		return self.driver

	def script(self, source, *args):
		return self.driver.execute_script(source, *args)

	def choose(self, element_id, value):
		self.script("const e = document.getElementById(arguments[0]); e.value = arguments[1]; e.dispatchEvent(new Event('input', {bubbles: true}));", element_id, value)
		self.script("window.replayDebug.draw()")

	def label_keys(self):
		return self.script("return Array.from(document.querySelectorAll('#labels .lab'), (e) => e.dataset.labelKey).sort()")

	def severe_logs(self):
		return [entry for entry in self.driver.get_log("browser") if entry["level"] == "SEVERE"]

	def test_the_reference_mesh_is_the_default_and_the_sources_switch_offline(self):
		reference = self.root / "reference.npz"
		vertices, triangles = write_reference(reference)
		driver = self.open_page(reference)
		self.assertEqual(self.script("return Array.from(document.getElementById('mesh-mode').options, (o) => o.value)"), ["reference", "dynamic", "none"])
		self.assertEqual(self.script("return document.getElementById('mesh-mode').value"), "reference")
		self.wait.until(lambda d: d.execute_script("window.replayDebug.draw(); return window.replayDebug.G.meshKey") == "reference")
		self.assertEqual(self.script("return [window.replayDebug.G.meshCount, window.replayDebug.G.meshTriangles]"), [vertices, triangles])
		self.assertIn("不是真值", driver.find_element(By.ID, "mesh-status").text)
		self.assertIn("1 cm", driver.find_element(By.ID, "mesh-status").text)
		uploads = self.script("return window.replayDebug.G.meshUploads")
		for index in (0, 1, 0):   # scrubbing the timeline must not upload the static mesh again
			self.script("const s = document.getElementById('slider'); s.value = arguments[0]; s.dispatchEvent(new Event('input'));", index)
			self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == index)
			self.script("window.replayDebug.draw()")
		self.assertEqual(self.script("return window.replayDebug.G.meshUploads"), uploads)
		self.assertEqual(self.script("return window.replayDebug.G.meshKey"), "reference")

		self.choose("mesh-mode", "dynamic")
		self.assertTrue(self.script("return window.replayDebug.G.meshKey.startsWith('dynamic:')"))
		self.assertEqual(self.script("return [window.replayDebug.G.meshCount, window.replayDebug.G.meshTriangles]"), [4, 4])
		self.assertNotIn("不是真值", driver.find_element(By.ID, "mesh-status").text)
		self.choose("mesh-mode", "none")
		self.assertEqual(self.script("return [window.replayDebug.G.meshKey, window.replayDebug.G.meshCount]"), ["none", 0])
		self.choose("mesh-mode", "reference")
		self.assertEqual(self.script("return [window.replayDebug.G.meshKey, window.replayDebug.G.meshCount]"), ["reference", vertices])
		self.assertEqual(self.script("return window.replayDebug.D.reference_mesh.vertices"), vertices)
		self.assertEqual(self.severe_logs(), [])

	def test_a_page_without_a_reference_offers_only_the_dynamic_mesh(self):
		self.open_page()
		self.assertEqual(self.script("return Array.from(document.getElementById('mesh-mode').options, (o) => o.value)"), ["dynamic", "none"])
		self.assertEqual(self.script("return document.getElementById('mesh-mode').value"), "dynamic")
		self.assertTrue(self.script("window.replayDebug.draw(); return window.replayDebug.G.meshKey.startsWith('dynamic:')"))
		self.assertEqual(self.script("return window.replayDebug.G.meshCount"), 4)
		self.assertEqual(self.severe_logs(), [])

	def test_all_cards_keep_their_membership_while_the_camera_moves_and_one_control_hides_them(self):
		driver = self.open_page(rich=True)
		self.assertEqual(self.script("return document.getElementById('label-mode').value"), "selected")
		self.assertEqual(self.label_keys(), [], "no card before something is selected")
		driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:12"]').click()
		self.wait.until(lambda d: d.execute_script("window.replayDebug.draw(); return document.querySelectorAll('#labels .lab').length") == 1)
		self.assertEqual(self.label_keys(), ["object:12"])

		self.choose("label-mode", "all")
		everything = ["building:0", "object:11", "object:12", "object:13", "place:3", "place:4", "room:0"]
		self.assertEqual(self.label_keys(), everything)
		for yaw, pitch, zoom in ((1.3, 0.2, 0.5), (-2.0, 1.1, 3.0), (0.4, -0.3, 0.2), (2.5, 0.6, 12.0)):
			self.script("const d = window.replayDebug; d.S.yaw += arguments[0]; d.S.pitch = arguments[1]; d.S.dist *= arguments[2]; d.draw();", yaw, pitch, zoom)
			self.assertEqual(self.label_keys(), everything, "orbit and zoom must not change which cards exist")
			for key, inside, shown in self.script(LABEL_VISIBILITY_SCRIPT):
				self.assertEqual(inside, shown, f"{key} is hidden only when its own anchor is off screen")
		self.script("window.replayDebug.resetView(); window.replayDebug.draw();")
		self.assertEqual(self.script("return Array.from(document.querySelectorAll('#labels .lab')).pop().dataset.labelKey"), "object:12")
		self.assertEqual(self.script("return getComputedStyle(document.querySelector('#labels .lab.selected')).zIndex"), "2")

		driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:13"] .entity-eye').click()
		self.script("window.replayDebug.draw()")
		self.assertEqual(self.label_keys(), [key for key in everything if key != "object:13"])
		driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:13"] .entity-eye').click()
		self.script("window.replayDebug.draw()")
		self.assertEqual(self.label_keys(), everything)

		self.choose("label-mode", "none")
		self.assertEqual(self.script("return Array.from(document.querySelectorAll('#labels .lab')).filter((e) => e.style.display !== 'none').length"), 0)
		self.assertGreaterEqual(self.script("return document.querySelectorAll('#entity-list .entity-row').length"), 3, "the entity browser stays")
		self.assertIn("物体", self.script("return document.getElementById('entity-detail').textContent"))
		self.assertEqual(self.severe_logs(), [])

	def test_the_selected_box_overlay_is_vivid_depth_independent_and_transparent_to_clicks(self):
		reference = self.root / "reference.npz"
		write_reference(reference)
		driver = self.open_page(reference, rich=True)
		self.assertFalse(self.script("return window.replayDebug.G.overlay.visible"))
		driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:12"]').click()
		self.wait.until(lambda d: d.execute_script("window.replayDebug.draw(); return window.replayDebug.G.overlay.visible"))
		overlay = self.script("return window.replayDebug.G.overlay")
		self.assertEqual(overlay["color"], "#ffb000")
		self.assertGreaterEqual(overlay["haloWidth"], 8)
		self.assertGreaterEqual(overlay["lineWidth"], 4)
		self.assertGreater(overlay["segments"], 0)
		self.assertGreater(overlay["corners"], 0)
		self.assertEqual(self.script("return getComputedStyle(document.getElementById('selection-overlay')).pointerEvents"), "none")
		self.assertEqual(self.script("const g = document.getElementById('gl'); return document.elementFromPoint(g.clientWidth / 2, g.clientHeight / 2).id"), "gl")
		pixel = """
			const d = window.replayDebug, item = d.S.objItems.find((i) => i.id === 12), c = document.getElementById('selection-overlay');
			const s = d.project(item.mn), k = c.width / document.getElementById('gl').clientWidth;
			return Array.from(c.getContext('2d').getImageData(Math.round(s[0] * k), Math.round(s[1] * k), 1, 1).data);
		"""
		self.assertEqual(self.script(pixel), [255, 176, 0, 255], "the corner dot is exactly the amber of the selection")

		for bbox_mode in ("none", "all", "selected"):   # visible whatever the ordinary box mode says
			self.choose("bbox-mode", bbox_mode)
			self.assertTrue(self.script("return window.replayDebug.G.overlay.visible"), bbox_mode)
		for mesh_mode in ("reference", "dynamic", "none"):   # and through either mesh
			self.choose("mesh-mode", mesh_mode)
			self.assertTrue(self.script("return window.replayDebug.G.overlay.visible"), mesh_mode)

		driver.find_element(By.CSS_SELECTOR, '[data-entity-key="object:12"] .entity-eye').click()
		self.script("window.replayDebug.draw()")
		self.assertFalse(self.script("return window.replayDebug.G.overlay.visible"))
		self.assertEqual(self.script(pixel)[3], 0, "the overlay is cleared when the selected entity is hidden")
		self.assertEqual(self.severe_logs(), [])


if __name__ == "__main__":
	unittest.main()
