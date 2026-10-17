"""Real-browser tests of the replay workspace: tabs, restoration, the result list, scene graph hot reload (RUN_BROWSER_TESTS=1)."""

import unittest
from typing import Optional

from tests.replay_browser_support import RUN_BROWSER_TESTS, ActionChains, By, FixtureService, Keys, WebDriverWait, make_driver, severe_logs


@unittest.skipUnless(RUN_BROWSER_TESTS, "set RUN_BROWSER_TESTS=1 to run the real-browser workspace tests")
class WorkspaceBrowserTestCase(unittest.TestCase):
	fixtures = None

	@classmethod
	def setUpClass(cls) -> None:
		cls.fixtures = FixtureService()
		cls.tum_result = cls.fixtures.call("make_result", name="tum_replay", kind="tum", sequence="fr1_desk")["path"]
		cls.coda_result = cls.fixtures.call("make_result", name="coda", kind="coda", sequence="0", outside=True)["path"]

	@classmethod
	def tearDownClass(cls) -> None:
		if cls.fixtures is not None:
			cls.fixtures.close()

	def setUp(self) -> None:
		self.driver = make_driver()
		self.wait = WebDriverWait(self.driver, 30)

	def tearDown(self) -> None:
		self.driver.quit()

	def script(self, source: str, *args):
		return self.driver.execute_script(source, *args)

	def open_workspace(self) -> None:
		self.driver.get(self.fixtures.base + "/replay.html")
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))

	def wait_for_tabs(self, count: int) -> None:
		self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "[role=tab]")) == count)

	def tab_titles(self):
		return [tab.text for tab in self.driver.find_elements(By.CSS_SELECTOR, "[role=tab]")]

	def selected_title(self) -> str:
		return self.driver.find_element(By.CSS_SELECTOR, "[role=tab][aria-selected=true]").text

	def open_result(self, path: str) -> None:
		attempts = self.script("return window.replayWorkspace.openAttempts || 0")
		field = self.driver.find_element(By.ID, "result-path")
		field.clear()
		field.send_keys(path)
		self.driver.find_element(By.ID, "open-result").click()
		self.wait.until(lambda d: d.execute_script("return window.replayWorkspace.openAttempts || 0") > attempts)

	def tab_for(self, path: str):
		return self.driver.find_element(By.CSS_SELECTOR, f'[role=tab][data-path="{path}"]')

	def notices(self) -> str:
		return self.driver.find_element(By.ID, "global-status").text


class WorkspaceTabsBrowserTest(WorkspaceBrowserTestCase):
	def test_two_results_keep_independent_tabs_and_restore_after_reload(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(self.coda_result)
		self.assertEqual(self.tab_titles(), ["TUM / fr1_desk", "CODa / 0"])
		self.assertEqual(self.selected_title(), "CODa / 0")
		panes = self.driver.find_elements(By.CSS_SELECTOR, "section.result-pane")
		self.assertEqual(len(panes), 2)
		self.assertEqual([pane.get_attribute("hidden") is not None for pane in panes], [True, False])
		self.driver.find_elements(By.CSS_SELECTOR, "[role=tab]")[0].click()
		self.assertEqual(self.selected_title(), "TUM / fr1_desk")
		self.driver.refresh()
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.wait_for_tabs(2)
		self.assertEqual(self.tab_titles(), ["TUM / fr1_desk", "CODa / 0"])
		self.assertEqual(self.selected_title(), "TUM / fr1_desk")
		stored = self.script("return JSON.parse(localStorage.getItem('daaam.replay.workspace.v1'))")
		self.assertEqual([tab["path"] for tab in stored["tabs"]], [self.tum_result, self.coda_result])
		self.assertNotIn("run", " ".join(stored["tabs"][0].keys()), "service run IDs are never stored")
		self.assertEqual(severe_logs(self.driver), [])

	def test_opening_an_open_result_again_focuses_its_tab(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(self.coda_result)
		unc = "\\\\wsl.localhost\\" + self.script("return window.replayWorkspace.api.settings.distro") + self.tum_result.replace("/", "\\")
		self.open_result(unc)
		self.wait.until(lambda d: self.selected_title() == "TUM / fr1_desk")
		self.assertEqual(self.tab_titles(), ["TUM / fr1_desk", "CODa / 0"])

	def test_closing_a_tab_releases_it_without_deleting_files(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(self.coda_result)
		self.tab_for(self.tum_result).find_element(By.XPATH, "..").find_element(By.CSS_SELECTOR, ".tab__close").click()
		self.wait_for_tabs(1)
		self.assertEqual(self.tab_titles(), ["CODa / 0"])
		self.assertEqual(self.fixtures.call("exists", path=self.tum_result), {"ok": True, "manifest": True, "result": True})
		self.open_result(self.tum_result)
		self.wait_for_tabs(2)
		self.assertEqual(self.tab_titles(), ["CODa / 0", "TUM / fr1_desk"])

	def test_tabs_reorder_by_dragging_and_the_order_is_restored(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(self.coda_result)
		tum, coda = self.tab_for(self.tum_result), self.tab_for(self.coda_result)
		left = tum.rect["x"] + 6 - (coda.rect["x"] + coda.rect["width"] / 2)   # onto the left edge of the first tab
		ActionChains(self.driver).click_and_hold(coda).move_by_offset(-10, 0).move_by_offset(int(left) + 10, 0).release().perform()
		self.wait.until(lambda d: self.tab_titles() == ["CODa / 0", "TUM / fr1_desk"])
		self.wait.until(lambda d: [t["path"] for t in d.execute_script("return JSON.parse(localStorage.getItem('daaam.replay.workspace.v1'))")["tabs"]] == [self.coda_result, self.tum_result])
		self.driver.refresh()
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.wait_for_tabs(2)
		self.assertEqual(self.tab_titles(), ["CODa / 0", "TUM / fr1_desk"])

	def test_keyboard_moves_between_tabs_and_delete_closes_one(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(self.coda_result)
		self.tab_for(self.tum_result).click()
		self.tab_for(self.tum_result).send_keys(Keys.ARROW_RIGHT)
		self.wait.until(lambda d: self.selected_title() == "CODa / 0")
		self.assertEqual(self.script("return document.activeElement.textContent"), "CODa / 0")
		self.driver.switch_to.active_element.send_keys(Keys.ARROW_RIGHT)
		self.wait.until(lambda d: self.selected_title() == "TUM / fr1_desk")
		self.driver.switch_to.active_element.send_keys(Keys.DELETE)
		self.wait_for_tabs(1)
		self.assertEqual(self.tab_titles(), ["CODa / 0"])

	def test_panes_receive_lifecycle_events_before_they_are_shown_or_hidden(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(self.coda_result)
		self.script("""
			window.__paneEvents = [];
			for (const pane of document.querySelectorAll('section.result-pane'))
				for (const type of ['replay-tab-activated', 'replay-tab-deactivated'])
					pane.addEventListener(type, () => window.__paneEvents.push([type, pane.dataset.path, pane.hidden]));
		""")
		self.tab_for(self.tum_result).click()
		events = self.script("return window.__paneEvents")
		self.assertEqual(events, [
			["replay-tab-deactivated", self.coda_result, False],
			["replay-tab-activated", self.tum_result, True],
		])


class WorkspaceRestoreBrowserTest(WorkspaceBrowserTestCase):
	def test_a_restored_path_that_no_longer_opens_is_skipped_with_a_notice(self) -> None:
		doomed = self.fixtures.call("make_result", name="doomed", kind="tum", sequence="fr1_room")["path"]
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result(doomed)
		self.wait_for_tabs(2)
		self.fixtures.call("remove_bundle", path=doomed)
		self.driver.refresh()
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.wait_for_tabs(1)
		self.assertEqual(self.tab_titles(), ["TUM / fr1_desk"])
		self.assertEqual(self.selected_title(), "TUM / fr1_desk")
		self.assertIn(doomed, self.notices())
		self.assertIn("replay/manifest.json", self.notices())

	def test_an_unopenable_path_is_reported_and_open_tabs_stay(self) -> None:
		self.open_workspace()
		self.open_result(self.tum_result)
		self.open_result("relative/path")
		self.wait.until(lambda d: "absolute" in self.notices())
		self.assertEqual(self.tab_titles(), ["TUM / fr1_desk"])

	def test_results_below_the_output_root_open_from_the_list(self) -> None:
		self.open_workspace()
		self.driver.find_element(By.CSS_SELECTOR, "#result-browser > summary").click()
		self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, f'#result-list .result-item[data-path="{self.tum_result}"]'))
		paths = [item.get_attribute("data-path") for item in self.driver.find_elements(By.CSS_SELECTOR, "#result-list .result-item")]
		self.assertIn(self.tum_result, paths)
		self.assertNotIn(self.coda_result, paths, "only results below the output root are listed")
		self.driver.find_element(By.CSS_SELECTOR, f'#result-list .result-item[data-path="{self.tum_result}"]').click()
		self.wait_for_tabs(1)
		self.assertEqual(self.selected_title(), "TUM / fr1_desk")


class WorkspaceGraphBrowserTest(WorkspaceBrowserTestCase):
	"""The scene graph files of an open result: the best valid one is shown, and new ones appear without disturbing the view."""

	STATE_KEYS = ("snapshot", "time", "camera", "hidden", "controls", "speed", "cameraLayout")

	def setUp(self) -> None:
		super().setUp()
		self.result = self.fixtures.call("make_result", name="graph_tum", kind="tum", sequence="fr1_desk")["path"]

	def pane_script(self, path: str, expression: str):
		return self.script("const pane = document.querySelector('section.result-pane[data-path=\"' + arguments[0] + '\"]'); return " + expression, path)

	def badge(self, path: str) -> str:
		return self.pane_script(path, "pane.querySelector('[data-role=graph-badge]').dataset.graph")

	def badge_state(self, path: str) -> str:
		return self.pane_script(path, "pane.querySelector('[data-role=graph-badge]').dataset.state")

	def wait_for_badge(self, path: str, name: str) -> None:
		self.wait.until(lambda d: self.badge(path) == name)

	def view_state(self):
		return self.script("return window.replayWorkspace.activeTab().viewer.serializeState()")

	def notices(self):   # the shown pane's messages
		return self.script("return [...document.querySelectorAll('section.result-pane:not([hidden]) .viewer-notice')].map((n) => n.textContent)")

	def wait_for_notice(self, text: str) -> None:
		self.wait.until(lambda d: any(text in note for note in self.notices()))

	def wait_for_viewer(self, path: str) -> None:
		self.wait.until(lambda d: self.pane_script(path, "!!pane.querySelector('[data-role=graph-badge]')"))
		self.wait.until(lambda d: d.execute_script("return !!(window.replayDebug && window.replayDebug.viewer.started)"))

	def settle_view(self) -> None:
		"""Time at the first snapshot, a distinctive camera, object O8 selected: a state any reset would show."""
		self.script("window.replayDebug.show(0)")
		self.wait.until(lambda d: d.execute_script("return window.replayDebug.S.cur === 0"))
		self.script("window.replayDebug.viewer.restoreCamera({yaw: 1.1, pitch: 0.4, dist: 3.3, target: [0.1, 0.2, 0.3]}); window.replayDebug.selectEntity('object', 8)")

	def test_a_result_without_a_graph_says_so(self) -> None:
		self.open_workspace()
		self.open_result(self.result)
		self.wait.until(lambda d: self.badge_state(self.result) == "none")
		self.assertIn("没有可用文件", self.pane_script(self.result, "pane.querySelector('[data-role=graph-badge]').textContent"))
		self.assertEqual(severe_logs(self.driver), [])

	def test_new_graph_files_replace_the_shown_graph_without_moving_time_camera_or_selection(self) -> None:
		self.open_workspace()
		self.open_result(self.result)
		self.wait_for_viewer(self.result)
		self.wait.until(lambda d: self.badge_state(self.result) == "none")
		self.settle_view()
		before = self.view_state()
		self.assertEqual((before["snapshot"], before["selected"]), (0, "object:8"))

		self.fixtures.call("write_graph", path=self.result, name="dsg.json", objects=[7, 8])
		self.wait_for_badge(self.result, "dsg.json")
		self.wait_for_notice("dsg.json")
		self.fixtures.call("write_graph", path=self.result, name="dsg_updated.json", objects=[7, 8])
		self.wait_for_badge(self.result, "dsg_updated.json")
		self.wait_for_notice("dsg.json → dsg_updated.json")
		after = self.view_state()
		for key in self.STATE_KEYS + ("selected", "entityTab"):
			self.assertEqual(after[key], before[key], key)
		self.assertIn("2 个物体", self.pane_script(self.result, "pane.querySelector('[data-role=graph-badge]').textContent"))
		self.assertIn("场景图 dsg_updated.json:包含这个实体", self.pane_script(self.result, "pane.querySelector('[data-role=entity-detail]').textContent"))

		# a graph without the selected entity: only the selection goes, with a visible reason
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7], rooms=[["office", "a desk area"]])
		self.wait_for_badge(self.result, "clustered_dsg.json")
		self.wait_for_notice("更新后的场景图中已不存在该实体")
		cleared = self.view_state()
		self.assertIsNone(cleared["selected"])
		for key in self.STATE_KEYS:
			self.assertEqual(cleared[key], before[key], key)
		self.assertIn("1 个房间(1 个有摘要)", self.pane_script(self.result, "pane.querySelector('[data-role=graph-badge]').textContent"))

		# the room and its summary are browsable; looking at them never moves the timeline
		self.driver.find_element(By.CSS_SELECTOR, "section.result-pane:not([hidden]) [data-role=entity-tab-rooms]").click()
		row = self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, 'section.result-pane:not([hidden]) .entity-row[data-entity-key="room:R0"]'))
		self.assertIn("office", row.text)
		row.click()
		detail = self.pane_script(self.result, "pane.querySelector('[data-role=entity-detail]').textContent")
		self.assertIn("a desk area", detail)
		self.assertNotIn("PRIVATE CHAIN OF THOUGHT", self.script("return document.body.innerText"))
		self.assertEqual(self.view_state()["snapshot"], 0)
		self.assertEqual(self.view_state()["selected"], "room:R0")
		self.assertEqual(severe_logs(self.driver), [])

	def test_a_bad_higher_candidate_is_reported_and_the_valid_graph_stays(self) -> None:
		self.open_workspace()
		self.open_result(self.result)
		self.fixtures.call("write_graph", path=self.result, name="dsg.json", objects=[7, 8])
		self.wait_for_badge(self.result, "dsg.json")
		self.fixtures.call("write_text", path=self.result, name="clustered_dsg_with_summaries.json", text="{")
		self.wait_for_notice("clustered_dsg_with_summaries.json 无效,继续使用 dsg.json")
		self.assertEqual(self.badge(self.result), "dsg.json")
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg_with_summaries.json", objects=[7, 8], rooms=True)
		self.wait_for_badge(self.result, "clustered_dsg_with_summaries.json")
		self.fixtures.call("remove_file", path=self.result, name="clustered_dsg_with_summaries.json")
		self.wait_for_badge(self.result, "dsg.json")
		self.wait_for_notice("场景图已回退")

	def test_each_tab_shows_only_its_own_graph_and_a_background_tab_applies_it_when_shown(self) -> None:
		other = self.fixtures.call("make_result", name="graph_coda", kind="coda", sequence="0", outside=True)["path"]
		self.open_workspace()
		self.open_result(self.result)
		self.open_result(other)
		self.wait.until(lambda d: self.badge_state(other) == "none" and self.badge_state(self.result) == "none")
		# the first tab is in the background: it learns of the graph, marks itself, and leaves its view alone
		self.fixtures.call("write_graph", path=self.result, name="dsg.json", objects=[7, 8])
		marker = lambda: self.script("return document.querySelector('.tab[data-path=\"' + arguments[0] + '\"]').parentElement.querySelector('.tab__marker').className", self.result)
		self.wait.until(lambda d: "graph" in marker().split())
		self.assertEqual(self.badge(self.result), "", "a background tab does not touch its view")
		self.assertEqual(self.badge_state(other), "none", "the other result has no graph")
		self.tab_for(self.result).click()
		self.wait_for_badge(self.result, "dsg.json")
		self.wait_for_notice("场景图已加载:dsg.json")
		self.assertNotIn("graph", marker().split())
		self.tab_for(other).click()
		self.assertEqual(self.badge_state(other), "none")
		self.assertEqual(severe_logs(self.driver), [])

	def test_a_result_the_service_forgot_is_opened_again_with_its_view(self) -> None:
		self.open_workspace()
		self.fixtures.call("write_graph", path=self.result, name="dsg.json", objects=[7, 8])
		self.open_result(self.result)
		self.wait_for_viewer(self.result)
		self.wait_for_badge(self.result, "dsg.json")
		self.settle_view()
		before = self.view_state()
		old_run = self.script("return window.replayWorkspace.activeTab().runId")
		self.script("window.replayWorkspace.api.close(arguments[0])", old_run)   # as a restart or another page's close would
		self.wait.until(lambda d: d.execute_script("return window.replayWorkspace.activeTab().runId") != old_run)
		self.wait.until(lambda d: d.execute_script("return !!(window.replayDebug && window.replayDebug.viewer.started)"))
		self.wait_for_badge(self.result, "dsg.json")
		after = self.view_state()
		for key in self.STATE_KEYS + ("selected",):
			self.assertEqual(after[key], before[key], key)
		self.assertEqual(self.tab_titles(), ["TUM / fr1_desk"])
		self.assertEqual(severe_logs(self.driver), [])

	def test_a_reloaded_page_shows_the_graph_again(self) -> None:
		self.open_workspace()
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.open_result(self.result)
		self.wait_for_badge(self.result, "clustered_dsg.json")
		self.driver.refresh()
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.wait_for_tabs(1)
		self.wait_for_badge(self.result, "clustered_dsg.json")
		self.assertEqual(severe_logs(self.driver), [])


class WorkspaceTasksBrowserTest(WorkspaceBrowserTestCase):
	"""Postprocess, Summarize and 全部运行 from the page. The container is a fake: the test prints, writes and exits for it."""

	PANE = "section.result-pane:not([hidden])"

	def setUp(self) -> None:
		super().setUp()
		self.fixtures.call("job_reset")
		self.addCleanup(lambda: self.fixtures.call("job_api_key", present=True))
		self.addCleanup(lambda: self.fixtures.call("job_reset"))
		self.result = self.fixtures.call("make_result", name="task_tum", kind="tum", sequence="fr1_desk")["path"]
		self.fixtures.call("write_graph", path=self.result, name="dsg.json", objects=[7, 8])

	def open_tasks(self, path: Optional[str] = None) -> None:
		self.open_workspace()
		self.open_result(path or self.result)
		self.show_tasks()

	def show_tasks(self) -> None:
		self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=tasks]"))
		self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=tasks]").click()

	def control(self, role: str):
		return self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-role={role}]")

	def text(self, role: str) -> str:
		return self.control(role).text

	def wait_for_text(self, role: str, text: str) -> None:
		self.wait.until(lambda d: text in self.text(role))

	def wait_enabled(self, role: str, enabled: bool = True) -> None:
		self.wait.until(lambda d: self.control(role).is_enabled() == enabled)

	def badge(self) -> str:
		return self.script("return document.querySelector('section.result-pane:not([hidden]) [data-role=graph-badge]').dataset.graph")

	def wait_for_badge(self, name: str) -> None:
		self.wait.until(lambda d: self.badge() == name)

	def calls(self):
		return self.fixtures.call("job_calls")["calls"]

	def test_postprocess_runs_streams_its_output_and_promotes_the_new_graph(self) -> None:
		self.open_tasks()
		self.wait_for_badge("dsg.json")
		self.wait_enabled("task-postprocess")
		self.assertTrue(self.control("task-all").is_enabled())
		self.assertFalse(self.control("task-summarize").is_enabled(), "the summaries need the clustering first")
		self.assertIn("Postprocess", self.text("task-note"))
		self.assertFalse(self.control("task-cancel").is_enabled())
		self.control("task-postprocess").click()
		self.wait_for_text("task-status", "运行中")
		self.wait_enabled("task-postprocess", False)
		self.fixtures.call("job_emit", line="=== sentence embeddings")
		self.wait_for_text("task-log", "sentence embeddings")
		command = self.calls()[0]
		self.assertEqual(command[-4:], ["--atomic", "--stage", "postprocess", "/selected-result"])
		self.assertIn("PLACES_LAYER=PLACES", command, "TUM settings come from the manifest")
		self.fixtures.call("job_finish", code=0, files=["dsg_updated.json", "clustered_dsg.json"], path=self.result)
		self.wait_for_badge("clustered_dsg.json")
		self.wait_for_text("task-status", "空闲")
		self.wait_for_text("task-jobs", "成功")
		self.wait_enabled("task-summarize")
		self.assertIn("sentence embeddings", self.text("task-log"), "the output stays after the job")
		self.assertEqual(severe_logs(self.driver), [])

	def test_summarize_follows_postprocess_and_brings_the_room_summaries(self) -> None:
		self.fixtures.call("write_graph", path=self.result, name="dsg_updated.json", objects=[7, 8])
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.open_tasks()
		self.wait_for_badge("clustered_dsg.json")
		self.wait_enabled("task-summarize")
		self.control("task-summarize").click()
		self.wait_for_text("task-status", "Summarize")
		self.assertEqual(self.calls()[0][-3:], ["--stage", "summarize", "/selected-result"])
		self.fixtures.call("job_finish", code=0, files=["clustered_dsg_with_summaries.json", "region_summaries.yaml"], path=self.result, rooms=[["office", "a desk area"]])
		self.wait_for_badge("clustered_dsg_with_summaries.json")
		self.assertIn("1 个房间(1 个有摘要)", self.script("return document.querySelector('section.result-pane:not([hidden]) [data-role=graph-badge]').textContent"))

	def test_running_a_stage_again_needs_an_explicit_confirmation(self) -> None:
		self.fixtures.call("write_graph", path=self.result, name="dsg_updated.json", objects=[7, 8])
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.open_tasks()
		self.wait_for_text("task-postprocess", "重新运行 Postprocess")
		self.control("task-postprocess").click()
		self.wait.until(lambda d: self.control("task-confirm").is_displayed())
		self.assertIn("dsg_updated.json", self.text("task-confirm"))
		self.control("task-confirm-no").click()
		self.wait.until(lambda d: not self.control("task-confirm").is_displayed())
		self.assertEqual(self.calls(), [], "declining starts nothing")
		self.control("task-postprocess").click()
		self.wait.until(lambda d: self.control("task-confirm").is_displayed())
		self.control("task-confirm-yes").click()
		self.wait_for_text("task-status", "运行中")
		self.assertEqual(len(self.calls()), 1)
		self.assertTrue(self.script("return window.replayWorkspace.activeTab().tasks.jobs[0].replace"))

	def test_a_running_job_can_be_cancelled_and_the_graph_stays(self) -> None:
		self.open_tasks()
		self.wait_for_badge("dsg.json")
		self.wait_enabled("task-postprocess")
		self.control("task-postprocess").click()
		self.wait_for_text("task-status", "运行中")
		self.wait_enabled("task-cancel")
		self.control("task-cancel").click()
		self.wait_for_text("task-jobs", "已取消")
		self.wait_for_text("task-status", "空闲")
		self.wait_enabled("task-postprocess")
		self.assertEqual(self.badge(), "dsg.json")
		self.assertFalse(self.control("task-cancel").is_enabled())

	def test_a_failed_job_shows_its_exit_status_and_can_be_retried_without_touching_the_graph(self) -> None:
		self.open_tasks()
		self.wait_for_badge("dsg.json")
		self.wait_enabled("task-postprocess")
		self.control("task-postprocess").click()
		self.wait_for_text("task-status", "运行中")
		self.fixtures.call("job_emit", line="the embedding step failed")
		self.fixtures.call("job_finish", code=3)
		self.wait_for_text("task-jobs", "失败")
		self.assertIn("exit status 3", self.text("task-jobs"))
		self.assertIn("the embedding step failed", self.text("task-log"))
		self.assertEqual(self.badge(), "dsg.json", "a failed job never changes the shown graph")
		self.wait_enabled("task-postprocess")
		self.control("task-retry").click()
		self.wait_for_text("task-status", "运行中")
		self.assertEqual(len(self.calls()), 2)

	def test_without_an_api_key_the_summary_stages_say_why(self) -> None:
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.fixtures.call("job_api_key", present=False)
		self.open_tasks()
		self.wait_for_badge("clustered_dsg.json")
		self.wait_enabled("task-summarize", False)
		self.assertIn("OPENAI_API_KEY", self.text("task-note"))
		self.assertTrue(self.control("task-postprocess").is_enabled() or "重新运行" in self.text("task-postprocess"))

	def test_a_background_tab_keeps_its_own_jobs_marks_their_end_and_shows_them_when_opened(self) -> None:
		other = self.fixtures.call("make_result", name="task_coda", kind="coda", sequence="0", outside=True)["path"]
		self.fixtures.call("write_graph", path=other, name="dsg.json", objects=[7, 8])
		self.open_tasks()
		self.wait_for_badge("dsg.json")
		self.wait_enabled("task-postprocess")
		self.control("task-postprocess").click()
		self.wait_for_text("task-status", "运行中")
		self.fixtures.call("job_emit", line="first result output")
		self.open_result(other)
		self.show_tasks()
		marker = lambda: self.script("return document.querySelector('.tab[data-path=\"' + arguments[0] + '\"]').parentElement.querySelector('.tab__marker').className", self.result)
		self.wait.until(lambda d: "busy" in marker().split())
		self.assertEqual(self.text("task-log"), "", "another result's output never shows here")
		self.wait_for_text("task-jobs", "还没有运行过任务")
		self.fixtures.call("job_emit", line="more output")
		self.fixtures.call("job_finish", code=0, files=["dsg_updated.json", "clustered_dsg.json"], path=self.result)
		self.wait.until(lambda d: "done" in marker().split())
		self.assertNotIn("busy", marker().split())
		self.tab_for(self.result).click()
		self.wait_for_text("task-log", "more output")
		self.assertIn("first result output", self.text("task-log"))
		self.wait_for_badge("clustered_dsg.json")
		self.assertNotIn("done", marker().split(), "looking at the tab clears the marker")
		self.assertEqual(self.calls()[0][-4:][-1], "/selected-result")
		self.assertEqual(severe_logs(self.driver), [])


@unittest.skipUnless(RUN_BROWSER_TESTS, "set RUN_BROWSER_TESTS=1 to run the real-browser workspace tests")
class WorkspaceChatBrowserTest(WorkspaceBrowserTestCase):
	"""The question panel of a result tab. The Agent worker is a scripted fake: the test decides what it answers and when."""

	PANE = "section.result-pane:not([hidden])"

	def setUp(self) -> None:
		super().setUp()
		self.fixtures.call("agent_reset")
		self.fixtures.call("job_reset")
		self.addCleanup(lambda: self.fixtures.call("agent_reset"))
		self.addCleanup(lambda: self.fixtures.call("job_reset"))
		self.result = self.fixtures.call("make_result", name="chat_tum", kind="tum", sequence="fr1_desk")["path"]
		self.fixtures.call("write_graph", path=self.result, name="dsg_updated.json", objects=[7, 8])

	def open_chat(self, path: Optional[str] = None) -> None:
		self.open_workspace()
		self.open_result(path or self.result)
		self.show_chat()

	def show_chat(self) -> None:
		self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=chat]"))
		self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=chat]").click()

	def control(self, role: str):
		return self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-role={role}]")

	def text(self, role: str) -> str:
		return self.control(role).text

	def wait_ready(self) -> None:
		self.wait.until(lambda d: self.control("chat-status").text == "可以提问")

	def rows(self):
		return self.script("""
			return [...document.querySelectorAll('section.result-pane:not([hidden]) [data-role=chat-messages] > *')].map((row) => ({
				role: row.dataset.messageRole || (row.dataset.role === 'chat-boundary-pending' ? 'pending' : 'empty'),
				head: (row.querySelector('.chat-message__head') || {}).textContent || '',
				text: (row.querySelector('.chat-message__body') || row).textContent,
			})).filter((row) => row.role !== 'empty');
		""")

	def roles(self):
		return [row["role"] for row in self.rows()]

	def wait_for_roles(self, expected) -> None:
		self.wait.until(lambda d: self.roles() == expected)

	def ask(self, question: str) -> None:
		field = self.control("chat-input")
		field.clear()
		field.send_keys(question)
		self.wait.until(lambda d: self.control("chat-send").is_enabled())
		self.control("chat-send").click()

	def asked(self):
		return self.fixtures.call("agent_requests")["requests"]

	def test_a_question_is_answered_and_a_follow_up_carries_the_conversation(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.assertFalse(self.control("chat-send").is_enabled(), "nothing to send yet")
		self.assertEqual(self.control("chat-model").get_attribute("value"), "gpt-5-mini")
		self.assertTrue(self.control("chat-context").is_selected(), "the current view goes along by default")
		self.ask("桌子旁边有什么?")
		self.wait_for_roles(["user", "assistant"])
		rows = self.rows()
		self.assertEqual(rows[0]["text"], "桌子旁边有什么?")
		self.assertEqual(rows[1]["text"], "答复 1:桌子旁边有什么?(收到 0 条历史)")
		self.assertIn("gpt-5-mini", rows[1]["head"])
		self.assertIn("dsg_updated.json", rows[1]["head"], "an answer names the scene graph it was computed against")
		self.wait.until(lambda d: self.control("chat-status").text == "可以提问")
		self.ask("它是什么颜色?")
		self.wait_for_roles(["user", "assistant", "user", "assistant"])
		self.assertIn("收到 2 条历史", self.rows()[3]["text"])
		requests = self.asked()
		self.assertEqual(requests[1]["conversation"], [
			{"role": "user", "content": "桌子旁边有什么?"}, {"role": "assistant", "content": "答复 1:桌子旁边有什么?(收到 0 条历史)"},
		])
		self.assertEqual((requests[0]["model"], requests[0]["dataset"]["kind"]), ("gpt-5-mini", "tum"))
		self.driver.refresh()   # the conversation belongs to the result: it is still there after a reload
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.wait_for_tabs(1)
		self.show_chat()
		self.wait_for_roles(["user", "assistant", "user", "assistant"])
		self.assertEqual(self.rows()[2]["text"], "它是什么颜色?")
		self.assertEqual(severe_logs(self.driver), [])

	def test_two_results_keep_separate_conversations_and_clearing_one_leaves_the_other(self) -> None:
		other = self.fixtures.call("make_result", name="chat_coda", kind="coda", sequence="0", outside=True)["path"]
		self.fixtures.call("write_graph", path=other, name="dsg_updated.json", objects=[7, 8])
		self.open_chat()
		self.wait_ready()
		self.ask("first result question")
		self.wait_for_roles(["user", "assistant"])
		self.open_result(other)
		self.show_chat()
		self.wait_ready()
		self.assertEqual(self.roles(), [], "another result starts with an empty conversation")
		self.ask("second result question")
		self.wait_for_roles(["user", "assistant"])
		self.assertEqual(self.asked()[1]["conversation"], [], "the first result's questions are not sent for the second")
		self.assertEqual(self.asked()[1]["dataset"]["kind"], "coda")
		self.tab_for(self.result).click()
		self.show_chat()
		self.wait.until(lambda d: self.rows() and self.rows()[0]["text"] == "first result question")
		self.control("chat-clear").click()
		self.assertTrue(self.control("chat-confirm").is_displayed(), "clearing asks first")
		self.control("chat-clear-no").click()
		self.assertEqual(self.roles(), ["user", "assistant"])
		self.control("chat-clear").click()
		self.control("chat-clear-yes").click()
		self.wait_for_roles([])
		self.tab_for(other).click()
		self.show_chat()
		self.wait.until(lambda d: self.rows() and self.rows()[0]["text"] == "second result question")
		self.assertEqual(self.roles(), ["user", "assistant"])
		self.assertEqual(severe_logs(self.driver), [])

	def test_a_new_graph_version_shows_a_boundary_and_the_next_answer_names_it(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.ask("before the change")
		self.wait_for_roles(["user", "assistant"])
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.wait.until(lambda d: "pending" in self.roles())
		pending = [row for row in self.rows() if row["role"] == "pending"][0]
		self.assertIn("dsg_updated.json", pending["text"])
		self.assertIn("clustered_dsg.json", pending["text"])
		self.wait.until(lambda d: self.control("chat-status").text == "可以提问")
		self.ask("after the change")
		self.wait_for_roles(["user", "assistant", "boundary", "user", "assistant"])
		rows = self.rows()
		self.assertIn("clustered_dsg.json", rows[2]["text"])
		self.assertIn("dsg_updated.json", rows[1]["head"], "the old answer keeps its own provenance")
		self.assertIn("clustered_dsg.json", rows[4]["head"])
		self.assertNotEqual(self.asked()[0]["graph_version"], self.asked()[1]["graph_version"])
		self.assertEqual(severe_logs(self.driver), [])

	def test_the_current_view_goes_along_only_while_the_box_is_checked(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.script("window.replayDebug.selectEntity('object', 7)")
		self.ask("what is this?")
		self.wait_for_roles(["user", "assistant"])
		context = self.asked()[0]["context"]
		self.assertIsInstance(context["replay_time"], (int, float))
		self.assertEqual(sorted(context["camera"]), ["distance", "pitch", "target", "yaw"])
		self.assertTrue(context["selected_entity"].startswith("object 7: "), context)
		self.assertIn("含当前视图", self.rows()[0]["head"])
		self.wait.until(lambda d: self.control("chat-status").text == "可以提问")
		self.control("chat-context").click()
		self.assertFalse(self.control("chat-context").is_selected())
		self.ask("and now?")
		self.wait_for_roles(["user", "assistant", "user", "assistant"])
		self.assertEqual(self.asked()[1]["context"], {})
		self.assertNotIn("含当前视图", self.rows()[2]["head"])

	def test_the_panel_says_why_it_cannot_ask_until_postprocess_has_run(self) -> None:
		import json
		from tests.replay_fixtures import graph_document
		raw = graph_document("raw")
		raw["metadata"] = {}   # a mapping result: no sentence embeddings yet
		for node in raw["nodes"]:
			node["attributes"].get("metadata", {}).pop("sentence_embedding_feature", None)
		raw_result = self.fixtures.call("make_result", name="chat_raw", kind="tum", sequence="fr1_desk")["path"]
		self.fixtures.call("write_text", path=raw_result, name="dsg.json", text=json.dumps(raw))
		self.open_chat(raw_result)
		self.wait.until(lambda d: "postprocess" in self.text("chat-note"))
		self.assertFalse(self.control("chat-send").is_enabled())
		self.control("chat-input").send_keys("can I ask yet?")
		self.assertFalse(self.control("chat-send").is_enabled(), "typing alone does not make an unready graph askable")
		self.fixtures.call("write_graph", path=raw_result, name="dsg_updated.json", objects=[7, 8])
		self.wait.until(lambda d: self.control("chat-send").is_enabled())
		self.assertEqual(self.text("chat-note"), "")
		self.control("chat-send").click()
		self.wait_for_roles(["user", "assistant"])
		self.assertEqual(self.asked()[0]["question"], "can I ask yet?")

	def test_a_question_can_be_left_and_its_answer_is_there_later(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.fixtures.call("agent_hold")
		self.ask("a slow question")
		self.wait.until(lambda d: "查询场景: get_matching_subjects" in self.text("chat-progress"))
		self.assertFalse(self.control("chat-send").is_displayed())
		self.control("chat-stop").click()
		self.wait.until(lambda d: "已停止显示" in self.text("chat-note"))
		self.assertEqual(self.roles(), ["user"], "the stored question stays, the answer is not here yet")
		self.fixtures.call("agent_release")
		self.wait_for_roles(["user", "assistant"])
		self.assertEqual(self.rows()[1]["text"], "答复 1:a slow question(收到 0 条历史)")
		self.wait.until(lambda d: self.control("chat-status").text == "可以提问")

	def test_a_failed_question_shows_the_reason_and_a_retry_does_not_replay_it(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.fixtures.call("agent_fail", mode="after")
		self.ask("will fail")
		self.wait_for_roles(["user", "error"])
		self.assertIn("API key", self.rows()[1]["text"])
		self.assertEqual(self.control("chat-input").get_attribute("value"), "", "the question left the box: it is in the conversation")
		self.fixtures.call("agent_fail", mode=None)
		self.wait.until(lambda d: self.control("chat-retry").is_enabled())
		self.control("chat-retry").click()
		self.wait_for_roles(["user", "error", "user", "assistant"])
		self.assertEqual(self.asked()[1]["question"], "will fail")
		self.assertEqual(self.asked()[1]["conversation"], [], "the attempt that failed is not part of the conversation")

	def test_a_worker_that_cannot_be_reached_leaves_the_question_and_an_error_to_retry_from(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.fixtures.call("agent_fail", mode="unavailable")
		self.ask("is anybody there?")
		self.wait_for_roles(["user", "error"])
		self.assertIn("问答服务没有启动", self.rows()[1]["text"])
		self.assertIn("问答服务没有启动", self.text("chat-note"))
		self.assertEqual(self.control("chat-input").get_attribute("value"), "", "the question is in the conversation: it is not offered twice")
		self.fixtures.call("agent_fail", mode=None)
		self.wait.until(lambda d: self.control("chat-retry").is_enabled())
		self.control("chat-retry").click()
		self.wait_for_roles(["user", "error", "user", "assistant"])
		self.assertEqual(self.rows()[3]["text"], "答复 2:is anybody there?(收到 0 条历史)", "the worker had been asked once before it failed")
		self.assertEqual(self.asked()[1]["conversation"], [], "the attempt that never reached the worker is not part of the conversation")

	def test_a_question_that_is_refused_stays_in_the_box_and_is_not_stored(self) -> None:
		self.open_chat()
		self.wait_ready()
		self.fixtures.call("agent_key", present=False)   # the key went away after the page had looked
		self.ask("will it be refused?")
		self.wait.until(lambda d: "OPENAI_API_KEY" in self.text("chat-note"))
		self.assertEqual(self.control("chat-input").get_attribute("value"), "will it be refused?", "nothing was asked: the text is still there")
		self.assertEqual(self.roles(), [], "a refused question is not part of the conversation")
		self.assertEqual(self.asked(), [])
		self.assertIn("没有 key", self.text("chat-model"), "the models follow the keys that exist")
		self.assertFalse(self.control("chat-send").is_enabled())

	AT_END = ("const l = document.querySelector('section.result-pane:not([hidden]) [data-role=chat-messages]');"
		"return l.scrollHeight > l.clientHeight + 100 && l.scrollTop + l.clientHeight >= l.scrollHeight - 24")

	def test_the_newest_rows_stay_in_view_unless_the_reader_scrolled_up(self) -> None:
		self.fixtures.call("agent_answer", text="\n".join("line %d of a long answer" % number for number in range(60)))
		self.open_chat()
		self.wait_ready()
		self.fixtures.call("agent_hold")
		self.ask("a long one")
		self.wait.until(lambda d: "查询场景: get_matching_subjects" in self.text("chat-progress"))
		self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=entities]").click()   # look at something else meanwhile
		self.fixtures.call("agent_release")
		self.wait_for_roles(["user", "assistant"])   # the answer arrives while the page is not shown
		self.show_chat()
		self.wait.until(lambda d: self.script(self.AT_END))   # the answer's end is in view, not its first line
		self.driver.refresh()   # a restored conversation shows its newest rows, not its first
		self.wait.until(lambda d: d.execute_script("return !!(window.replayWorkspace && window.replayWorkspace.ready)"))
		self.wait_for_tabs(1)
		self.show_chat()
		self.wait_for_roles(["user", "assistant"])
		self.wait.until(lambda d: self.script(self.AT_END))
		self.driver.execute_async_script(   # a reader who scrolls up to the start stays there when a new row arrives
			"const l = document.querySelector('section.result-pane:not([hidden]) [data-role=chat-messages]');"
			"const done = arguments[arguments.length - 1]; l.addEventListener('scroll', () => done(true), {once: true}); l.scrollTop = 0;")
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.wait.until(lambda d: "pending" in self.roles())
		self.assertLess(self.script("return document.querySelector('section.result-pane:not([hidden]) [data-role=chat-messages]').scrollTop"), 50)

	def test_a_question_can_be_asked_while_a_job_runs_and_the_new_graph_follows(self) -> None:
		self.fixtures.call("write_graph", path=self.result, name="dsg.json", objects=[7, 8])   # a finished mapping result: jobs may run
		self.fixtures.call("write_graph", path=self.result, name="clustered_dsg.json", objects=[7, 8])
		self.open_chat()
		self.wait_ready()
		self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=tasks]").click()
		self.wait.until(lambda d: self.control("task-summarize").is_enabled())
		self.control("task-summarize").click()
		self.wait.until(lambda d: "运行中" in self.text("task-status"))
		self.driver.find_element(By.CSS_SELECTOR, f"{self.PANE} [data-section-tab=chat]").click()
		self.ask("asked while the job runs")
		self.wait_for_roles(["user", "assistant"])
		self.assertIn("clustered_dsg.json", self.rows()[1]["head"], "the last valid graph answers, and says which")
		self.fixtures.call("job_finish", code=0, files=["clustered_dsg_with_summaries.json", "region_summaries.yaml"], path=self.result, rooms=[["office", "a desk area"]])
		self.wait.until(lambda d: "pending" in self.roles())
		self.assertIn("clustered_dsg_with_summaries.json", [row for row in self.rows() if row["role"] == "pending"][0]["text"])
		self.assertEqual(severe_logs(self.driver), [])


if __name__ == "__main__":
	unittest.main()
