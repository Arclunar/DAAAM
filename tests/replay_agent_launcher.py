"""Runs the real replay Agent worker (scripts.replay_web.agent_worker.main) with a fake Agent, for the client tests.

The question picks the behaviour, so a test drives it through the real protocol:

    echo: TEXT      answer "echo: TEXT" with progress
    pid             answer the process id
    boom            the Agent raises
    crash           the process dies before answering, the first time only (marker file $FAKE_CRASH_MARKER)
    crash-always    the process dies before answering, every time
    crash-late      the process dies after streaming the first piece of the answer
    hang            the Agent never returns
    slow N          answer after N seconds
    noise           write to stdout (Python and C level) and a key to stderr, then answer
    private         tool progress with arguments and a private reasoning in the structured answer
    invalidated     answer with the results that were invalidated
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.replay_web import agent_worker  # noqa: E402

PRIVATE = "PRIVATE-REASONING-DO-NOT-LEAK"
ARGUMENTS = "TOOL-ARGUMENTS-DO-NOT-LEAK"
LEAK = "sk-test-LEAK-KEY-1234567890"
STATE = {"crash_after_first_chunk": False}


class FakeCache:
	def __init__(self):
		self.invalidated = []
		self.agent = FakeAgent(self)

	def get(self, result_key, graph_path, graph_version, dataset, model_name, background_objects=None):
		return self.agent

	def invalidate(self, result_key):
		self.invalidated.append(result_key)


class FakeAgent:
	def __init__(self, cache):
		self.cache = cache

	def answer_query(self, response_format, query, image=None, *, conversation=None, context=None, progress=None, **kwargs):
		if query == "crash-always":
			os._exit(3)
		if query == "crash":
			marker = Path(os.environ["FAKE_CRASH_MARKER"])
			if not marker.exists():
				marker.write_text("crashed once", encoding="utf-8")
				os._exit(3)
			query = "echo: recovered"
		if query == "hang":
			time.sleep(3600)
		if query.startswith("slow "):
			time.sleep(float(query.split()[1]))
			query = "echo: slow"
		if query == "boom":
			raise RuntimeError("the Agent exploded: " + LEAK)
		if query == "noise":
			print("noise on stdout from a library")
			os.write(1, b"C-LEVEL NOISE on descriptor 1\n")
			sys.stderr.write("provider said: %s\n" % LEAK)
			query = "echo: noisy"
		if query == "crash-late":
			STATE["crash_after_first_chunk"] = True
			query = "echo: " + "late " * 60
		progress({"type": "iteration", "number": 1, "maximum": 3})
		if query == "private":
			progress({"type": "tool", "name": "get_matching_subjects", "arguments": ARGUMENTS, "reasoning": PRIVATE})
		else:
			progress({"type": "tool", "name": "get_matching_subjects"})
		progress({"type": "final"})
		if query == "pid":
			answer = str(os.getpid())
		elif query == "invalidated":
			answer = ",".join(self.cache.invalidated) or "none"
		elif query == "private":
			answer = "A plain answer."
		else:
			answer = query if query.startswith("echo: ") else "echo: " + query
		return SimpleNamespace(answer=answer, reasoning=PRIVATE), {"iterations": []}, []


def crashing_chunks(text, size=80):
	for index, start in enumerate(range(0, len(text), size)):
		if index == 1 and STATE["crash_after_first_chunk"]:
			os._exit(4)
		yield text[start:start + size]


if __name__ == "__main__":
	os.environ.setdefault("OPENAI_API_KEY", "sk-test-launcher-key-0123456789")
	agent_worker.text_chunks = crashing_chunks
	agent_worker.run(sys.argv[1:], cache=FakeCache(), load_environment=lambda: None, response_format="TEXT-RESPONSE")
