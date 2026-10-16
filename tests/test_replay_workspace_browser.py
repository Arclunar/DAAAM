"""Real-browser tests of the replay workspace: tabs, restoration, the result list (RUN_BROWSER_TESTS=1)."""

import unittest

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


if __name__ == "__main__":
	unittest.main()
