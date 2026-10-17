import contextlib
import io
import json
import queue
import threading
import time
import unittest

from scripts.replay_web.events import EventBroker, ReplayEvent, SubscriptionClosed, encode_sse


class EventBrokerTest(unittest.TestCase):
	def test_bounded_history_replays_events_after_an_id_then_continues_live(self) -> None:
		broker = EventBroker(history_limit=3)
		events = [broker.publish("run", "graph", {"n": n}) for n in range(5)]
		with broker.subscribe("run", after_id=events[2].event_id) as subscription:
			self.assertEqual([subscription.get(timeout=.1).payload["n"] for _ in range(2)], [3, 4])
			with self.assertRaises(queue.Empty):
				subscription.get(timeout=.02)
			live = broker.publish("run", "graph", {"n": 5})
			self.assertEqual(subscription.get(timeout=.1), live)

	def test_ids_increase_across_runs_and_each_run_has_its_own_channel(self) -> None:
		broker = EventBroker()
		a1, b1, a2 = broker.publish("a", "graph", {}), broker.publish("b", "graph", {}), broker.publish("a", "job-log", {})
		self.assertEqual([a1.event_id, b1.event_id, a2.event_id], [1, 2, 3])
		self.assertEqual(broker.latest_id, 3)
		with broker.subscribe("a", after_id=0) as only_a, broker.subscribe(["a", "b"], after_id=0) as both:
			self.assertEqual([only_a.get(timeout=.1).event_id for _ in range(2)], [1, 3])
			self.assertEqual([both.get(timeout=.1).event_id for _ in range(3)], [1, 2, 3])
			broker.publish("b", "graph", {})
			with self.assertRaises(queue.Empty):
				only_a.get(timeout=.02)
			self.assertEqual(both.get(timeout=.1).run_id, "b")

	def test_a_fresh_subscription_starts_with_the_current_state(self) -> None:
		asked = []

		def provider(run_id: str):
			asked.append(run_id)
			return {"graph": {"scanned": True, "of": run_id}}

		broker = EventBroker(state_provider=provider)
		broker.publish("run", "graph", {"n": 1})
		broker.publish("run", "graph", {"n": 2})
		with broker.subscribe("run") as subscription:
			state = subscription.get(timeout=.1)
			self.assertEqual((state.event_type, state.run_id, state.event_id), ("state", "run", 2))
			self.assertEqual(state.payload, {"graph": {"scanned": True, "of": "run"}})
			with self.assertRaises(queue.Empty):
				subscription.get(timeout=.02)
			live = broker.publish("run", "graph", {"n": 3})
			self.assertEqual(subscription.get(timeout=.1), live)
		self.assertEqual(asked, ["run"])

	def test_a_gap_in_the_history_starts_with_a_state_and_skips_the_stale_deltas(self) -> None:
		broker = EventBroker(history_limit=3, state_provider=lambda run_id: {"now": True})
		events = [broker.publish("run", "graph", {"n": n}) for n in range(5)]
		with broker.subscribe("run", after_id=events[0].event_id) as subscription:   # events 1 and 2 are gone from the history
			state = subscription.get(timeout=.1)
			self.assertEqual((state.event_type, state.payload), ("state", {"now": True}))
			self.assertEqual(state.event_id, events[-1].event_id)
			with self.assertRaises(queue.Empty):
				subscription.get(timeout=.02)

	def test_an_id_from_the_future_or_without_a_state_provider_is_a_gap(self) -> None:
		broker = EventBroker()
		broker.publish("run", "graph", {})
		with broker.subscribe("run", after_id=99) as subscription:
			state = subscription.get(timeout=.1)
			self.assertEqual((state.event_type, state.payload), ("state", {}))

	def test_last_event_ids_of_another_service_instance_are_not_trusted(self) -> None:
		broker, other = EventBroker(), EventBroker()
		event = broker.publish("run", "graph", {})
		self.assertEqual(broker.parse_event_id("%s.%d" % (broker.epoch, event.event_id)), event.event_id)
		self.assertNotEqual(broker.epoch, other.epoch)
		for value in (None, "", "12", "%s.%d" % (other.epoch, event.event_id), "%s.x" % broker.epoch, "%s.-1" % broker.epoch, "%s." % broker.epoch):
			with self.subTest(value=value):
				self.assertIsNone(broker.parse_event_id(value))

	def test_a_slow_subscriber_gets_one_current_state_instead_of_the_dropped_deltas(self) -> None:
		broker = EventBroker(history_limit=50, queue_limit=3, state_provider=lambda run_id: {"graph": "current"})
		slow = broker.subscribe("run", after_id=0)
		fast = broker.subscribe("run", after_id=0)
		seen = []
		for n in range(10):
			broker.publish("run", "job-log", {"n": n})
			seen.append(fast.get(timeout=.1).payload["n"])
		self.assertEqual(seen, list(range(10)), "a consumer that keeps up never loses an event")
		state = slow.get(timeout=.1)
		self.assertEqual((state.event_type, state.payload, state.event_id), ("state", {"graph": "current"}, 10))
		with self.assertRaises(queue.Empty):
			slow.get(timeout=.02)
		broker.publish("run", "graph", {"n": 99})
		self.assertEqual(slow.get(timeout=.1).payload, {"n": 99})
		slow.close()
		fast.close()

	def test_an_event_is_not_delivered_twice_when_it_is_older_than_the_state_that_replaced_it(self) -> None:
		broker = EventBroker(queue_limit=2, state_provider=lambda run_id: {"ok": 1})
		subscription = broker.subscribe("run", after_id=0)
		for n in range(3):
			broker.publish("run", "graph", {"n": n})   # overflow: the queue is replaced by a pending state
		broker.publish("run", "graph", {"n": 3})   # published while the state is pending
		kinds = []
		while True:
			try:
				kinds.append(subscription.get(timeout=.02).event_type)
			except queue.Empty:
				break
		self.assertEqual(kinds, ["state"])

	def test_closing_the_broker_unblocks_waiting_and_future_subscribers(self) -> None:
		broker = EventBroker()
		subscription = broker.subscribe("run", after_id=0)
		outcome = []

		def wait() -> None:
			try:
				subscription.get(timeout=30)
			except SubscriptionClosed:
				outcome.append("closed")

		thread = threading.Thread(target=wait)
		thread.start()
		time.sleep(.05)
		started = time.monotonic()
		broker.close()
		thread.join(5)
		self.assertFalse(thread.is_alive())
		self.assertEqual(outcome, ["closed"])
		self.assertLess(time.monotonic() - started, 2)
		with self.assertRaises(SubscriptionClosed):
			broker.subscribe("run").get(timeout=.1)
		self.assertEqual(broker.subscriber_count(), 0)
		broker.publish("run", "graph", {})   # publishing after the close is harmless

	def test_leaving_a_subscription_releases_it(self) -> None:
		broker = EventBroker()
		with broker.subscribe("run", after_id=0) as subscription:
			self.assertEqual(broker.subscriber_count(), 1)
		self.assertEqual(broker.subscriber_count(), 0)
		with self.assertRaises(SubscriptionClosed):
			subscription.get(timeout=.1)
		subscription.close()

	def test_forgetting_a_run_drops_its_history(self) -> None:
		broker = EventBroker(state_provider=lambda run_id: {"gone": True})
		events = [broker.publish("run", "graph", {"n": n}) for n in range(3)]
		broker.forget("run")
		with broker.subscribe("run", after_id=events[0].event_id) as subscription:
			self.assertEqual(subscription.get(timeout=.1).event_type, "state")

	def test_a_failing_state_provider_is_reported_without_breaking_the_stream(self) -> None:
		def provider(run_id: str):
			raise RuntimeError("provider failed")

		broker = EventBroker(state_provider=provider)
		with contextlib.redirect_stderr(io.StringIO()) as log, broker.subscribe("run") as subscription:
			state = subscription.get(timeout=.1)
		self.assertEqual((state.event_type, state.payload), ("state", {"unavailable": True}))
		self.assertIn("provider failed", log.getvalue())

	def test_payloads_are_private_json_copies(self) -> None:
		broker = EventBroker()
		payload = {"list": [1, 2], "nested": {"a": "b"}}
		event = broker.publish("run", "graph", payload)
		payload["list"].append(3)
		payload["nested"]["a"] = "changed"
		self.assertEqual(event.payload, {"list": [1, 2], "nested": {"a": "b"}})
		for bad in ({"set": {1}}, {"nan": float("nan")}, {"object": object()}):
			with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
				broker.publish("run", "graph", bad)
		self.assertEqual(broker.latest_id, 1, "a rejected payload takes no id")

	def test_concurrent_publishers_get_unique_increasing_ids(self) -> None:
		broker = EventBroker(history_limit=1000)
		ids, lock = [], threading.Lock()

		def publisher() -> None:
			for n in range(100):
				event = broker.publish("run", "job-log", {"n": n})
				with lock:
					ids.append(event.event_id)

		threads = [threading.Thread(target=publisher) for _ in range(8)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()
		self.assertEqual(sorted(ids), list(range(1, 801)))


class SseFrameTest(unittest.TestCase):
	def test_one_event_is_one_frame_with_its_run_id_in_the_data(self) -> None:
		frame = encode_sse(ReplayEvent(7, "run", "graph", {"x": "中文"}), "ep")
		self.assertEqual(frame, b'id: ep.7\nevent: graph\ndata: {"x":"\xe4\xb8\xad\xe6\x96\x87","run_id":"run"}\n\n')

	def test_line_breaks_in_text_cannot_split_a_frame(self) -> None:
		frame = encode_sse(ReplayEvent(1, "run", "job-log", {"line": "a\nb\r\nc d"}), "ep").decode("utf-8")
		self.assertEqual(frame.count("\n\n"), 1)
		self.assertTrue(frame.endswith("\n\n"))
		data = [line for line in frame.split("\n") if line.startswith("data: ")]
		self.assertEqual(len(data), 1)
		self.assertEqual(json.loads(data[0][6:])["line"], "a\nb\r\nc d")


if __name__ == "__main__":
	unittest.main()
