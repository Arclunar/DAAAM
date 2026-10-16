"""Real-browser tests of the replay viewer inside the workspace (RUN_BROWSER_TESTS=1, Selenium and Chrome).

Every result is a real bundle finalized by the fixture service (tests/replay_web_fixture_server.py) and opened through
the loopback service, so these tests cover the HTTP chunk loading as well as the WebGL viewer. `window.replayDebug` is
the debug handle of the active tab's viewer.
"""

import time
import unittest

from tests.replay_browser_support import RUN_BROWSER_TESTS, ActionChains, By, FixtureService, WebDriverWait, make_driver, severe_logs

PANE = "section.result-pane:not([hidden])"


def frames(*items):
	return [{"frame": frame, "object": obj, "place": place, "offset": offset} for frame, obj, place, offset in items]


@unittest.skipUnless(RUN_BROWSER_TESTS, "set RUN_BROWSER_TESTS=1 to run the real-browser interaction tests")
class ReplayViewerBrowserTestCase(unittest.TestCase):
	fixtures = None

	@classmethod
	def setUpClass(cls) -> None:
		cls.fixtures = FixtureService()

	@classmethod
	def tearDownClass(cls) -> None:
		if cls.fixtures is not None:
			cls.fixtures.close()

	def setUp(self) -> None:
		self.driver = make_driver()
		self.wait = WebDriverWait(self.driver, 30)

	def tearDown(self) -> None:
		self.driver.quit()

	def result(self, **arguments) -> str:
		return self.fixtures.call("make_result", **arguments)["path"]

	def script(self, source: str, *args):
		return self.driver.execute_script(source, *args)

	def open_viewer(self, path: str, snapshot: int) -> None:
		"""Open the workspace (if needed) and the result, and wait until the viewer shows `snapshot`."""
		if not self.script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"):
			self.driver.get(self.fixtures.base + "/replay.html")
			self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.script("window.replayWorkspace.openFromUser(arguments[0])", path)
		self.wait.until(lambda d: d.execute_script("return window.replayWorkspace.activeTab() && window.replayWorkspace.activeTab().canonicalPath") == path)
		self.wait.until(lambda d: d.execute_script("return !!window.replayDebug && window.replayDebug.S.cur") == snapshot)

	def el(self, role: str):
		return self.driver.find_element(By.CSS_SELECTOR, f"{PANE} [data-role={role}]")

	def set_slider(self, index: int) -> None:
		self.script(f"const s = document.querySelector('{PANE} [data-role=slider]'); s.value = arguments[0]; s.dispatchEvent(new Event('input'));", index)
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == index)

	def choose(self, role: str, value: str) -> None:
		self.script(f"const e = document.querySelector('{PANE} [data-role=' + arguments[0] + ']'); e.value = arguments[1]; e.dispatchEvent(new Event('input', {{bubbles: true}}));", role, value)
		self.script("window.replayDebug.draw()")

	def label_keys(self):
		return self.script(f"return Array.from(document.querySelectorAll('{PANE} [data-role=labels] .lab'), (e) => e.dataset.labelKey).sort()")


class ReplayBrowserTest(ReplayViewerBrowserTestCase):
	def test_source_frame_button_opens_image_without_moving_the_timeline(self) -> None:
		path = self.result(name="source", snapshots=frames((1, 7, 3, 0.0)))
		self.open_viewer(path, 0)
		before = self.script("return window.replayDebug.S.cur")
		button = self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:7"] .entity-source')
		self.assertTrue(button.is_enabled())
		self.assertEqual(button.text, "描述源帧")
		button.click()
		self.wait.until(lambda d: "open" in self.el("description-source-modal").get_attribute("class").split())
		self.wait.until(lambda d: self.el("description-source-image").get_property("naturalWidth") > 0)
		self.assertEqual(self.script("return window.replayDebug.S.cur"), before)
		caption = self.el("description-source-caption").text
		self.assertIn("frame 1", caption)
		self.assertIn("相机 rgb", caption)
		self.assertTrue(self.el("description-source-image").get_attribute("src").startswith(self.fixtures.base + "/api/runs/"))
		self.el("description-source-close").click()
		self.assertNotIn("open", self.el("description-source-modal").get_attribute("class").split())
		self.assertEqual(self.script("return window.replayDebug.S.cur"), before)

	def test_an_object_without_recorded_provenance_has_no_source_frame(self) -> None:
		path = self.result(name="nosource", snapshots=frames((1, 7, 3, 0.0)), description=False)
		self.open_viewer(path, 0)
		button = self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:7"] .entity-source')
		self.assertFalse(button.is_enabled())
		self.assertEqual(button.text, "无描述源帧")

	def test_playback_uses_snapshot_timestamps_at_one_times_speed(self) -> None:
		path = self.result(name="playback", snapshots=frames(*[(frame, 7, 3, frame / 15.0) for frame in range(1, 13)]), max_chunk_snapshots=4)
		self.open_viewer(path, 11)
		self.el("first").click()
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
		self.el("play").click()
		time.sleep(0.7)
		current = self.script("return window.replayDebug.S.wanted")
		self.assertGreaterEqual(current, 7, "15 FPS data played substantially slower than its timestamps")

	def test_entity_browser_selects_without_moving_the_timeline_and_hides_it(self) -> None:
		path = self.result(name="entities", snapshots=frames((1, 7, 3, 0.0), (5, 8, 4, 1.0)))
		self.open_viewer(path, 1)
		self.assertEqual(self.el("bbox-mode").get_attribute("value"), "selected")
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:7"]').click()
		self.assertEqual(self.el("slider").get_attribute("value"), "1")
		self.assertEqual(self.script("return window.replayDebug.S.selectedKey"), "object:7")
		self.assertEqual(self.script("return window.replayDebug.S.cur"), 1, "selection never moves the timeline")
		self.assertEqual(self.script("return window.replayDebug.G.highlightNodes"), 0)
		self.assertFalse(self.script("return window.replayDebug.G.overlay.visible"))
		self.set_slider(0)
		# the selection is an amber node plus a screen-space box overlay, no longer thin white WebGL lines
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.G.highlightNodes === 1 && window.replayDebug.G.overlay.visible"))
		self.assertGreater(self.script("return window.replayDebug.G.overlay.segments"), 0)
		polygon = self.script("return window.replayDebug.projectBox(window.replayDebug.S.objItems[0]).polygon")
		self.assertGreaterEqual(len(polygon), 3)
		bounds = self.script(f"const g = document.querySelector('{PANE} [data-role=gl]'); return [g.clientWidth, g.clientHeight]")
		self.assertTrue(all(0 <= point[0] <= bounds[0] and 0 <= point[1] <= bounds[1] for point in polygon))
		camera_cases = self.script("""
			const eye = window.replayDebug.S.eye;
			const inside = window.replayDebug.projectBox({mn: eye.map((value) => value - 0.01), mx: eye.map((value) => value + 0.01)});
			const boundary = window.replayDebug.projectBox({mn: [eye[0], eye[1] - 0.01, eye[2] - 0.01], mx: [eye[0] + 0.01, eye[1] + 0.01, eye[2] + 0.01]});
			return [inside.area, boundary ? boundary.area : null];
		""")
		viewport_area = bounds[0] * bounds[1]
		self.assertEqual(camera_cases[0], viewport_area)
		self.assertTrue(camera_cases[1] is None or camera_cases[1] < viewport_area * 0.99)
		covering_face = self.script("return window.replayDebug.projectBox({mn: [-100, -100, -0.02], mx: [100, 100, 0.02]})")
		self.assertIsNotNone(covering_face)
		self.assertGreater(covering_face["area"], bounds[0] * bounds[1] * 0.2)
		self.assertIn("首次", self.el("entity-detail").text)

		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:7"] .entity-eye').click()
		self.assertTrue(self.script("return window.replayDebug.S.hidden.has('object:7')"))
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.G.highlightNodes === 0 && !window.replayDebug.G.overlay.visible"))
		self.set_slider(1)
		self.set_slider(0)
		self.assertTrue(self.script("return window.replayDebug.S.hidden.has('object:7')"))
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:7"] .entity-eye').click()
		self.assertFalse(self.script("return window.replayDebug.S.hidden.has('object:7')"))

		self.el("entity-tab-places").click()
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="place:3"]').click()
		self.assertEqual(self.script("return window.replayDebug.S.selectedKey"), "place:3")
		self.assertEqual(self.script("return window.replayDebug.S.cur"), 0)

		corner = self.script("return window.replayDebug.project(window.replayDebug.S.objItems[0].mn)")
		canvas = self.el("gl")
		ActionChains(self.driver).move_to_element_with_offset(
			canvas, corner[0] - canvas.size["width"] / 2 + 4, corner[1] - canvas.size["height"] / 2 + 4,
		).click().perform()
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.selectedKey") == "object:7")
		self.assertEqual(self.script("return window.replayDebug.S.cur"), 0, "picking in the map never moves the timeline")
		self.assertEqual(severe_logs(self.driver), [])

	def test_the_first_view_fetches_bounded_chunks_and_images_by_url(self) -> None:
		path = self.result(name="chunks", snapshots=frames(*[(frame, 7, 3, frame / 15.0) for frame in range(1, 10)]), max_chunk_snapshots=2)
		self.open_viewer(path, 8)
		time.sleep(1.0)
		requests = self.script("return performance.getEntriesByType('resource').map((entry) => entry.name)")
		timeline = [url for url in requests if "/timeline/" in url]
		self.assertLessEqual(len(timeline), 2, timeline)
		self.assertTrue(any("/timeline/4?v=" in url for url in timeline), "the last chunk holds the last snapshot")
		self.assertNotIn("data:image", self.driver.page_source)
		sources = self.script(f"return [...document.querySelectorAll('{PANE} .camera-card__image')].map((img) => img.getAttribute('src'))")
		self.assertEqual(len(sources), 2)
		self.assertTrue(all(source.startswith("/api/runs/") for source in sources), sources)
		self.wait.until(lambda d: d.execute_script(f"return [...document.querySelectorAll('{PANE} .camera-card__image')].every((img) => img.naturalWidth > 0)"))
		self.set_slider(0)
		self.assertGreaterEqual(len([url for url in self.script("return performance.getEntriesByType('resource').map((entry) => entry.name)") if "/timeline/" in url]), 3)
		self.assertEqual(severe_logs(self.driver), [])

	def test_a_corrupt_chunk_stops_only_that_result_and_names_the_chunk(self) -> None:
		good = self.result(name="good", snapshots=frames((1, 7, 3, 0.0), (5, 8, 4, 1.0)))
		bad = self.result(name="bad", snapshots=frames((1, 7, 3, 0.0), (5, 8, 4, 1.0), (9, 9, 5, 2.0)), max_chunk_snapshots=1)
		damaged = self.fixtures.call("corrupt_chunk", path=bad, index=0)["chunk"]
		self.open_viewer(good, 1)
		self.open_viewer(bad, 2)
		self.set_slider(1)
		self.script(f"const s = document.querySelector('{PANE} [data-role=slider]'); s.value = 0; s.dispatchEvent(new Event('input'));")
		self.wait.until(lambda d: not self.el("error").get_attribute("hidden") and damaged in self.el("error").text)
		self.assertEqual(self.script("return window.replayDebug.S.cur"), 1, "the last good snapshot stays on screen")
		self.script("window.replayWorkspace.activate([...window.replayWorkspace.tabs.values()][0].tabId)")
		self.set_slider(0)
		self.assertEqual(self.el("error").get_attribute("hidden"), "true")


class ReplayTabsBrowserTest(ReplayViewerBrowserTestCase):
	def test_tabs_own_their_time_and_only_the_active_one_renders_or_loads_images(self) -> None:
		first = self.result(name="first", snapshots=frames((1, 7, 3, 0.0), (5, 8, 4, 1.0)))
		second = self.result(name="second", kind="coda", snapshots=frames((1, 7, 3, 0.0), (2, 8, 4, 1.0), (3, 9, 5, 2.0)))
		self.open_viewer(first, 1)
		self.set_slider(0)
		self.open_viewer(second, 2)
		state = self.script("""
			const tabs = [...window.replayWorkspace.tabs.values()];
			return tabs.map((tab) => [tab.viewer.active, tab.viewer.S.cur, tab.viewer.G.draws,
				[...tab.pane.querySelectorAll('.camera-card__image')].map((img) => img.hasAttribute('src'))]);
		""")
		self.assertEqual([row[:2] for row in state], [[False, 0], [True, 2]])
		self.assertEqual(state[0][3], [False, False], "a background tab decodes no camera images")
		background_draws = state[0][2]
		self.script("window.dispatchEvent(new Event('resize')); window.replayDebug.draw();")
		time.sleep(0.6)
		self.assertEqual(self.script("return [...window.replayWorkspace.tabs.values()][0].viewer.G.draws"), background_draws, "a background tab does not render")
		self.script("window.replayWorkspace.activate([...window.replayWorkspace.tabs.values()][0].tabId)")
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
		self.wait.until(lambda d: d.execute_script(f"return [...document.querySelectorAll('{PANE} .camera-card__image')].every((img) => img.naturalWidth > 0)"))
		self.assertEqual(self.script("return [...window.replayWorkspace.tabs.values()][1].viewer.S.cur"), 2)

	def test_a_background_tab_pauses_playback_and_resumes_on_return(self) -> None:
		path = self.result(name="long", snapshots=frames(*[(frame, 7, 3, frame / 15.0) for frame in range(1, 90)]), max_chunk_snapshots=16)
		other = self.result(name="other", snapshots=frames((1, 7, 3, 0.0)))
		self.open_viewer(path, 88)
		self.el("first").click()
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur") == 0)
		self.el("play").click()
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.wanted") >= 3)
		self.open_viewer(other, 0)
		paused = self.script("const v = [...window.replayWorkspace.tabs.values()][0].viewer; return [v.playing, v.wasPlaying, v.S.wanted]")
		self.assertEqual(paused[:2], [False, True])
		time.sleep(0.6)
		self.assertEqual(self.script("return [...window.replayWorkspace.tabs.values()][0].viewer.S.wanted"), paused[2], "time stands still in the background")
		self.script("window.replayWorkspace.activate([...window.replayWorkspace.tabs.values()][0].tabId)")
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.viewer.playing"))
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.wanted") > paused[2])

	def test_the_view_of_each_tab_is_restored_after_a_reload(self) -> None:
		path = self.result(name="restore", snapshots=frames((1, 7, 3, 0.0), (5, 8, 4, 1.0), (9, 9, 5, 2.0)))
		self.open_viewer(path, 2)
		self.set_slider(1)
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:8"]').click()
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:7"] .entity-eye').click()
		self.choose("label-mode", "all")
		self.script("const d = window.replayDebug; d.S.yaw = 2.25; d.S.pitch = 0.3; d.S.dist *= 1.5; d.viewer.changed();")
		expected = self.script("return [window.replayDebug.S.dist, window.replayDebug.S.target]")
		self.wait.until(lambda d: (d.execute_script("return JSON.parse(localStorage.getItem('daaam.replay.workspace.v1'))")["tabs"][0]["viewerState"] or {}).get("camera", {}).get("yaw") == 2.25)
		self.driver.refresh()
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready && window.replayDebug && window.replayDebug.viewer.started)"))
		restored = self.script("""
			const d = window.replayDebug;
			return [d.S.cur, d.S.selectedKey, [...d.S.hidden], document.querySelector('section.result-pane:not([hidden]) [data-role=label-mode]').value, d.S.yaw, d.S.pitch, d.S.dist, d.S.target];
		""")
		self.assertEqual(restored[:4], [1, "object:8", ["object:7"], "all"])
		self.assertAlmostEqual(restored[4], 2.25)
		self.assertAlmostEqual(restored[5], 0.3)
		self.assertAlmostEqual(restored[6], expected[0], places=4)
		for got, want in zip(restored[7], expected[1]):
			self.assertAlmostEqual(got, want, places=4)


LABEL_VISIBILITY_SCRIPT = """
	const d = window.replayDebug, gl = document.querySelector('section.result-pane:not([hidden]) [data-role=gl]');
	return d.labels().map((item) => {
		const el = document.querySelector('section.result-pane:not([hidden]) [data-role=labels] .lab[data-label-key="' + item.key + '"]');
		const s = d.project(item.pos);
		const inside = !!s && s[0] >= 0 && s[0] <= gl.clientWidth && s[1] >= 0 && s[1] <= gl.clientHeight;
		return [item.key, inside, !!el && el.style.display !== 'none'];
	});
"""


class ReplayViewerFeaturesBrowserTest(ReplayViewerBrowserTestCase):
	"""Mesh source selector, stable label modes and the selection overlay of the viewer."""

	def open_page(self, reference: bool = False, rich: bool = False) -> None:
		snapshots = frames((1, 7, 3, 0.1), (4, 7, 3, 0.4))
		path = self.result(name="features", snapshots=snapshots, rich=rich, reference=reference)
		self.open_viewer(path, 1)

	def test_the_reference_mesh_is_the_default_and_the_sources_switch_without_reuploading(self) -> None:
		self.open_page(reference=True)
		vertices, triangles = 900, 2 * 29 * 29
		self.assertEqual(self.script(f"return Array.from(document.querySelector('{PANE} [data-role=mesh-mode]').options, (o) => o.value)"), ["reference", "dynamic", "none"])
		self.assertEqual(self.el("mesh-mode").get_attribute("value"), "reference")
		self.wait.until(lambda d: d.execute_script("window.replayDebug.draw(); return window.replayDebug.G.meshKey") == "reference")
		self.assertEqual(self.script("return [window.replayDebug.G.meshCount, window.replayDebug.G.meshTriangles]"), [vertices, triangles])
		self.assertIn("不是真值", self.el("mesh-status").text)
		self.assertIn("1 cm", self.el("mesh-status").text)
		uploads = self.script("return window.replayDebug.G.meshUploads")
		for index in (0, 1, 0):   # scrubbing the timeline must not upload the static mesh again
			self.set_slider(index)
			self.script("window.replayDebug.draw()")
		self.assertEqual(self.script("return window.replayDebug.G.meshUploads"), uploads)
		self.assertEqual(self.script("return window.replayDebug.G.meshKey"), "reference")
		self.choose("mesh-mode", "dynamic")
		self.assertTrue(self.script("return window.replayDebug.G.meshKey.startsWith('dynamic:')"))
		self.assertEqual(self.script("return [window.replayDebug.G.meshCount, window.replayDebug.G.meshTriangles]"), [4, 4])
		self.assertNotIn("不是真值", self.el("mesh-status").text)
		self.choose("mesh-mode", "none")
		self.assertEqual(self.script("return [window.replayDebug.G.meshKey, window.replayDebug.G.meshCount]"), ["none", 0])
		self.choose("mesh-mode", "reference")
		self.assertEqual(self.script("return [window.replayDebug.G.meshKey, window.replayDebug.G.meshCount]"), ["reference", vertices])
		self.assertEqual(self.script("return window.replayDebug.D.reference_mesh.vertices"), vertices)
		self.assertEqual(len([url for url in self.script("return performance.getEntriesByType('resource').map((e) => e.name)") if url.split("?")[0].endswith("/reference")]), 1, "the reference is fetched once")
		self.assertEqual(severe_logs(self.driver), [])

	def test_a_bundle_without_a_reference_offers_only_the_dynamic_mesh(self) -> None:
		self.open_page()
		self.assertEqual(self.script(f"return Array.from(document.querySelector('{PANE} [data-role=mesh-mode]').options, (o) => o.value)"), ["dynamic", "none"])
		self.assertEqual(self.el("mesh-mode").get_attribute("value"), "dynamic")
		self.assertTrue(self.script("window.replayDebug.draw(); return window.replayDebug.G.meshKey.startsWith('dynamic:')"))
		self.assertEqual(self.script("return window.replayDebug.G.meshCount"), 4)
		self.assertEqual(severe_logs(self.driver), [])

	def test_all_cards_keep_their_membership_while_the_camera_moves_and_one_control_hides_them(self) -> None:
		self.open_page(rich=True)
		self.assertEqual(self.el("label-mode").get_attribute("value"), "selected")
		self.assertEqual(self.label_keys(), [], "no card before something is selected")
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:12"]').click()
		self.wait.until(lambda d: d.execute_script(f"window.replayDebug.draw(); return document.querySelectorAll('{PANE} [data-role=labels] .lab').length") == 1)
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
		self.assertEqual(self.script(f"return Array.from(document.querySelectorAll('{PANE} [data-role=labels] .lab')).pop().dataset.labelKey"), "object:12")
		self.assertEqual(self.script(f"return getComputedStyle(document.querySelector('{PANE} [data-role=labels] .lab.selected')).zIndex"), "2")

		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:13"] .entity-eye').click()
		self.script("window.replayDebug.draw()")
		self.assertEqual(self.label_keys(), [key for key in everything if key != "object:13"])
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:13"] .entity-eye').click()
		self.script("window.replayDebug.draw()")
		self.assertEqual(self.label_keys(), everything)

		self.choose("label-mode", "none")
		self.assertEqual(self.script(f"return Array.from(document.querySelectorAll('{PANE} [data-role=labels] .lab')).filter((e) => e.style.display !== 'none').length"), 0)
		self.assertGreaterEqual(self.script(f"return document.querySelectorAll('{PANE} [data-role=entity-list] .entity-row').length"), 3, "the entity browser stays")
		self.assertIn("物体", self.el("entity-detail").get_attribute("textContent"))
		self.assertEqual(severe_logs(self.driver), [])

	def test_the_selected_box_overlay_is_vivid_depth_independent_and_transparent_to_clicks(self) -> None:
		self.open_page(reference=True, rich=True)
		self.assertFalse(self.script("return window.replayDebug.G.overlay.visible"))
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:12"]').click()
		self.wait.until(lambda d: d.execute_script("window.replayDebug.draw(); return window.replayDebug.G.overlay.visible"))
		overlay = self.script("return window.replayDebug.G.overlay")
		self.assertEqual(overlay["color"], "#ffb000")
		self.assertGreaterEqual(overlay["haloWidth"], 8)
		self.assertGreaterEqual(overlay["lineWidth"], 4)
		self.assertGreater(overlay["segments"], 0)
		self.assertGreater(overlay["corners"], 0)
		self.assertEqual(self.script(f"return getComputedStyle(document.querySelector('{PANE} [data-role=selection-overlay]')).pointerEvents"), "none")
		self.assertEqual(self.script(f"""
			const g = document.querySelector('{PANE} [data-role=gl]'), r = g.getBoundingClientRect();
			return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2).dataset.role;
		"""), "gl")
		pixel = f"""
			const d = window.replayDebug, item = d.S.objItems.find((i) => i.id === 12), c = document.querySelector('{PANE} [data-role=selection-overlay]');
			const s = d.project(item.mn), k = c.width / document.querySelector('{PANE} [data-role=gl]').clientWidth;
			return Array.from(c.getContext('2d').getImageData(Math.round(s[0] * k), Math.round(s[1] * k), 1, 1).data);
		"""
		self.assertEqual(self.script(pixel), [255, 176, 0, 255], "the corner dot is exactly the amber of the selection")
		for bbox_mode in ("none", "all", "selected"):   # visible whatever the ordinary box mode says
			self.choose("bbox-mode", bbox_mode)
			self.assertTrue(self.script("return window.replayDebug.G.overlay.visible"), bbox_mode)
		for mesh_mode in ("reference", "dynamic", "none"):   # and through either mesh
			self.choose("mesh-mode", mesh_mode)
			self.assertTrue(self.script("return window.replayDebug.G.overlay.visible"), mesh_mode)
		self.driver.find_element(By.CSS_SELECTOR, f'{PANE} [data-entity-key="object:12"] .entity-eye').click()
		self.script("window.replayDebug.draw()")
		self.assertFalse(self.script("return window.replayDebug.G.overlay.visible"))
		self.assertEqual(self.script(pixel)[3], 0, "the overlay is cleared when the selected entity is hidden")
		self.assertEqual(severe_logs(self.driver), [])


if __name__ == "__main__":
	unittest.main()
