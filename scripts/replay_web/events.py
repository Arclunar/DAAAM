"""Event channels of the replay workspace: what the service tells the open pages as it happens.

Producers (the scene graph monitor, later the job manager and the chat) call `EventBroker.publish(run_id, type, payload)`
after they have updated their own state. Each page reads the events of its open results through one server-sent
events stream (server.py), which is a subscription here. The broker keeps a short history per result, so a reconnecting
page can ask for what it missed; when it cannot (the history no longer reaches back, the id comes from another service
instance, or the page was too slow to keep up) it receives one `state` event instead: the current state of the result,
built by the state provider, which replaces everything that was dropped. Pages therefore treat events as notifications
and re-read the state they refer to, so seeing one twice does no harm.

Event ids are one increasing counter for the whole broker, formatted as "<epoch>.<n>" on the wire; the epoch is random
per service start, so a page that remembers an id from before a restart is not mistaken for one that missed nothing.

Standard library only, Python 3.8.
"""

import collections
import json
import queue
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Set, Tuple, Union

StateProvider = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class ReplayEvent:
	event_id: int
	run_id: str
	event_type: str
	payload: Mapping[str, Any]


class SubscriptionClosed(Exception):
	"""The subscription was closed, or the broker shut down."""


def encode_sse(event: ReplayEvent, epoch: str) -> bytes:
	"""One server-sent event frame. The data is the payload plus the run it belongs to (one stream can carry many runs)."""
	data = json.dumps(dict(event.payload, run_id=event.run_id), separators=(",", ":"), ensure_ascii=False)
	return f"id: {epoch}.{event.event_id}\nevent: {event.event_type}\ndata: {data}\n\n".encode("utf-8")


class EventSubscription:
	"""A consumer's bounded queue of the events of one or more runs. `get` is meant for a single consumer thread."""

	def __init__(self, broker: "EventBroker", run_ids: Tuple[str, ...]):
		self._broker = broker
		self.run_ids = run_ids
		self._queue: Deque[ReplayEvent] = collections.deque()
		self._need_state: List[str] = []   # runs whose current state must be sent before anything else
		self._closed = False

	def __enter__(self) -> "EventSubscription":
		return self

	def __exit__(self, *exc: Any) -> None:
		self.close()

	def close(self) -> None:
		self._broker._unsubscribe(self)

	def _offer(self, event: ReplayEvent) -> None:   # called with the broker lock held
		if event.run_id in self._need_state:
			return   # the state that is on its way already covers it
		if len(self._queue) >= self._broker.queue_limit:
			self._queue.clear()   # too slow: one current state replaces the deltas that cannot be kept
			self._need_state = list(self.run_ids)
			return
		self._queue.append(event)

	def get(self, timeout: Optional[float] = None) -> ReplayEvent:
		"""The next event; queue.Empty after `timeout` seconds without one, SubscriptionClosed once closed."""
		deadline = None if timeout is None else time.monotonic() + timeout
		cond = self._broker._cond
		with cond:
			while True:
				if self._closed:
					raise SubscriptionClosed()
				if self._need_state:
					runs, self._need_state = self._need_state, []
					break
				if self._queue:
					return self._queue.popleft()
				remaining = None if deadline is None else deadline - time.monotonic()
				if remaining is not None and remaining <= 0:
					raise queue.Empty()
				cond.wait(remaining)
		states = []
		for run_id in runs:   # outside the lock: the provider takes locks of its own
			with cond:
				watermark = self._broker._next_id - 1
			states.append(ReplayEvent(watermark, run_id, "state", self._broker._state_for(run_id)))
		marks = {state.run_id: state.event_id for state in states}
		with cond:
			if self._closed:
				raise SubscriptionClosed()
			# a state reflects every event up to its watermark: queued events that old are redundant
			kept = [event for event in self._queue if not (event.run_id in marks and event.event_id <= marks[event.run_id])]
			self._queue = collections.deque(states + kept)
			return self._queue.popleft()


class EventBroker:
	def __init__(self, history_limit: int = 256, queue_limit: int = 128, state_provider: Optional[StateProvider] = None,
			epoch: Optional[str] = None):
		self.history_limit = history_limit
		self.queue_limit = queue_limit
		self.state_provider = state_provider
		self.epoch = epoch or secrets.token_hex(6)
		self._lock = threading.Lock()
		self._cond = threading.Condition(self._lock)
		self._next_id = 1
		self._history: Dict[str, Deque[ReplayEvent]] = {}
		self._evicted: Dict[str, int] = {}   # run -> highest id that fell out of its history
		self._subs: Set[EventSubscription] = set()
		self._closed = False

	@property
	def latest_id(self) -> int:
		with self._lock:
			return self._next_id - 1

	def subscriber_count(self) -> int:
		with self._lock:
			return len(self._subs)

	def parse_event_id(self, value: Optional[str]) -> Optional[int]:
		"""The numeric id of a `Last-Event-ID` this instance issued, else None."""
		epoch, _, number = (value or "").strip().partition(".")
		if epoch != self.epoch or not number.isdigit():
			return None
		return int(number)

	def publish(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> ReplayEvent:
		frozen = json.loads(json.dumps(payload, allow_nan=False))   # a private, JSON-safe copy; a bad payload fails here, not in a stream
		with self._cond:
			event = ReplayEvent(self._next_id, run_id, event_type, frozen)
			self._next_id += 1
			if self._closed:
				return event
			history = self._history.setdefault(run_id, collections.deque())
			history.append(event)
			while len(history) > self.history_limit:
				self._evicted[run_id] = history.popleft().event_id
			for subscription in self._subs:
				if run_id in subscription.run_ids:
					subscription._offer(event)
			self._cond.notify_all()
			return event

	def subscribe(self, run_ids: Union[str, Iterable[str]], after_id: Optional[int] = None) -> EventSubscription:
		"""Events of the given runs after `after_id`; without one (or when it cannot be continued from) a state comes first."""
		runs = (run_ids,) if isinstance(run_ids, str) else tuple(dict.fromkeys(run_ids))
		subscription = EventSubscription(self, runs)
		with self._cond:
			if self._closed:
				subscription._closed = True
				return subscription
			missed: List[ReplayEvent] = []
			for run_id in runs:
				if after_id is None or after_id >= self._next_id or after_id < self._evicted.get(run_id, 0):
					subscription._need_state.append(run_id)
				else:
					missed.extend(event for event in self._history.get(run_id, ()) if event.event_id > after_id)
			missed.sort(key=lambda event: event.event_id)
			if len(missed) > self.queue_limit:
				subscription._need_state = list(runs)
			else:
				subscription._queue.extend(event for event in missed if event.run_id not in subscription._need_state)
			self._subs.add(subscription)
		return subscription

	def forget(self, run_id: str) -> None:
		"""Drop the history of a closed run; a page resuming an old id for it gets a state instead of silence."""
		with self._lock:
			self._history.pop(run_id, None)
			self._evicted[run_id] = self._next_id - 1

	def close(self) -> None:
		"""Shut down: every waiting subscriber is released and no new event is kept."""
		with self._cond:
			self._closed = True
			for subscription in self._subs:
				subscription._closed = True
			self._subs.clear()
			self._cond.notify_all()

	def _unsubscribe(self, subscription: EventSubscription) -> None:
		with self._cond:
			subscription._closed = True
			self._subs.discard(subscription)
			self._cond.notify_all()

	def _state_for(self, run_id: str) -> Mapping[str, Any]:
		if self.state_provider is None:
			return {}
		try:
			return json.loads(json.dumps(self.state_provider(run_id), allow_nan=False))
		except Exception:
			sys.stderr.write(f"[replay-web] the state of run {run_id} cannot be built:\n{traceback.format_exc()}")
			return {"unavailable": True}
