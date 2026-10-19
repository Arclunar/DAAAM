"""A stand-in for AgentClient: the service and the page can be tested without a worker, Docker or a model."""
import threading
from typing import Callable, List, Optional

from scripts.replay_web.agent_client import AgentRequestFailed, AgentUnavailable
from scripts.replay_web.agent_protocol import Request


class FakeAgent:
	"""Stands in for AgentClient: it records the validated requests and answers from a script."""

	def __init__(self):
		self.requests: List[Request] = []
		self.answer = lambda request: "The chair is beside the desk."
		self.fail_before_output: Optional[Exception] = None
		self.fail_after_output: Optional[Exception] = None
		self.during: Optional[Callable[[Request], None]] = None
		self.invalidated: List[str] = []
		self.released = 0
		self.stopped = 0
		self.busy = False
		self.queued = 0
		self.alive = False

	def ask(self, request, on_event=None):
		assert isinstance(request, Request), "the manager validates a request before it reaches the client"
		self.requests.append(request)
		if self.fail_before_output is not None:
			raise self.fail_before_output
		emit = on_event or (lambda event: None)
		emit({"id": request.id, "type": "progress", "kind": "loading"})
		emit({"id": request.id, "type": "progress", "kind": "iteration", "number": 1, "maximum": 3})
		emit({"id": request.id, "type": "progress", "kind": "tool", "name": "get_matching_subjects"})
		if self.during is not None:
			self.during(request)
		if self.fail_after_output is not None:
			raise self.fail_after_output
		emit({"id": request.id, "type": "progress", "kind": "final"})
		answer = self.answer(request)
		for start in range(0, len(answer), 80):
			emit({"id": request.id, "type": "delta", "text": answer[start:start + 80]})
		return {"id": request.id, "type": "complete", "answer": answer, "model": request.model, "graph_version": request.graph_version}

	def invalidate(self, result_key):
		self.invalidated.append(result_key)
		return True

	def release_if_idle(self):
		self.released += 1
		return True

	def stop(self):
		self.stopped += 1


class ScriptedAgent(FakeAgent):
	"""The fake worker of the browser tests: a test steers it with commands and reads back what it was asked."""

	def __init__(self):
		super().__init__()
		self.gate: Optional[threading.Event] = None
		self.reset()

	def reset(self) -> None:
		if self.gate is not None:
			self.gate.set()   # a request that was held goes on before it is forgotten
		self.gate = None
		self.requests.clear()
		self.invalidated.clear()
		self.released = 0
		self.fail_before_output = self.fail_after_output = self.during = None
		self.answer = lambda request: "答复 %d:%s(收到 %d 条历史)" % (len(self.requests), request.question, len(request.conversation))

	def hold(self) -> None:
		"""The next request waits, with its progress already streamed, until `release`."""
		gate = self.gate = threading.Event()
		self.during = lambda request: gate.wait(60)

	def release(self) -> None:
		if self.gate is not None:
			self.gate.set()

	def fail(self, mode: Optional[str]) -> None:
		self.fail_before_output = AgentUnavailable("the agent worker command was not found") if mode == "unavailable" else None
		self.fail_after_output = AgentRequestFailed("The model provider rejected the API credentials.", "provider-auth") if mode == "after" else None

	def asked(self) -> List[dict]:
		return [{
			"question": request.question, "model": request.model, "conversation": [dict(turn) for turn in request.conversation],
			"context": dict(request.context), "graph_version": request.graph_version,
			"dataset": {"kind": request.dataset.kind, "sequence": request.dataset.sequence, "time_origin": request.dataset.time_origin},
		} for request in self.requests]
