import importlib.util
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def load_protocol():
	"""The module by path, the way the Habitat bridge loads it (its environment has no `daaam` package)."""
	spec = importlib.util.spec_from_file_location("live_protocol_under_test", REPO / "src" / "daaam" / "datasets" / "live_protocol.py")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


protocol = load_protocol()


class Producer(threading.Thread):
	"""Stands in for the simulator loop: captures a frame (its colour is the capture counter) whenever a client waits."""

	def __init__(self, server, width=8, height=6):
		super().__init__(daemon=True)
		self.server, self.width, self.height = server, width, height
		self.stop = threading.Event()
		self.captures = 0

	def run(self):
		while not self.stop.is_set():
			generation = self.server.poll()
			if generation:
				self.captures += 1
				rgb = np.full((self.height, self.width, 3), self.captures % 256, np.uint8)
				depth = np.full((self.height, self.width), 1000 + self.captures, np.uint16)
				meta = {"wall_time": time.time(), "intrinsics": [4.0, 4.0, 3.5, 2.5], "depth_scale": 5000.0, "pose": list(np.eye(4).reshape(-1)),
					"pose_convention": "opengl_yup", "captures": self.captures}
				self.server.publish(generation, meta, rgb, depth)
			time.sleep(0.001)


class WireFormatTest(unittest.TestCase):
	def test_url(self) -> None:
		self.assertEqual(protocol.parse_url("tcp://127.0.0.1:8002"), ("127.0.0.1", 8002))
		self.assertEqual(protocol.parse_url("localhost:99"), ("localhost", 99))
		for bad in ("tcp://127.0.0.1", "tcp://:8002", "tcp://host:port"):
			with self.assertRaises(ValueError):
				protocol.parse_url(bad)

	def test_depth_encoding_keeps_measurements_and_zeroes_the_rest(self) -> None:
		depth = np.array([[0.0, 0.5, 4.2], [np.nan, 10.0, 12.0], [-1.0, 9.99, np.inf]], np.float32)
		encoded = protocol.encode_depth(depth, far=10.0)
		self.assertEqual(encoded.dtype, np.uint16)
		self.assertEqual(encoded.tolist(), [[0, 2500, 21000], [0, 0, 0], [0, 49950, 0]])

	def test_a_frame_survives_the_wire(self) -> None:
		left, right = socket.socketpair()
		self.addCleanup(left.close)
		self.addCleanup(right.close)
		rgb = np.random.default_rng(1).integers(0, 255, (6, 8, 3), dtype=np.uint8)
		depth = np.random.default_rng(2).integers(0, 60000, (6, 8), dtype=np.uint16)
		meta = {"status": "frame", "seq": 7, "wall_time": 12.5, "width": 8, "height": 6, "note": "ü"}
		protocol.send_request(left, last_seq=6, mapped=3, rate=2.5)
		self.assertEqual(protocol.read_request(right), {"last_seq": 6, "mapped": 3, "rate": 2.5})
		sender = threading.Thread(target=protocol.send_response, args=(right, meta, rgb, depth))
		sender.start()
		got_meta, got_rgb, got_depth = protocol.read_response(left)
		sender.join()
		self.assertEqual(got_meta, meta)
		np.testing.assert_array_equal(got_rgb, rgb)
		np.testing.assert_array_equal(got_depth, depth)

	def test_answers_without_a_frame_have_no_payload(self) -> None:
		left, right = socket.socketpair()
		self.addCleanup(left.close)
		self.addCleanup(right.close)
		protocol.send_response(right, {"status": "timeout"})
		meta, rgb, depth = protocol.read_response(left)
		self.assertEqual(meta, {"status": "timeout"})
		self.assertIsNone(rgb)
		self.assertIsNone(depth)

	def test_a_closed_peer_is_reported(self) -> None:
		left, right = socket.socketpair()
		right.close()
		self.addCleanup(left.close)
		with self.assertRaises(protocol.SourceClosed):
			protocol.read_response(left)
		self.assertIsNone(protocol.read_request(left))

	def test_a_wrong_payload_size_is_refused(self) -> None:
		left, right = socket.socketpair()
		self.addCleanup(left.close)
		self.addCleanup(right.close)
		meta = b'{"status":"frame","width":8,"height":6}'
		right.sendall(protocol._RESPONSE.pack(protocol.RESPONSE_MAGIC, len(meta), 10) + meta + bytes(10))
		with self.assertRaises(ValueError):
			protocol.read_response(left)


class ServerAndClientTest(unittest.TestCase):
	def setUp(self) -> None:
		self.server = protocol.FrameServer("127.0.0.1", 0, wait_s=0.3)
		self.port = self.server.start()
		self.producer = Producer(self.server)
		self.producer.start()
		self.addCleanup(self.stop)
		self.client = protocol.FrameClient(f"tcp://127.0.0.1:{self.port}", io_timeout=5.0)
		self.client.connect(timeout=5.0)

	def stop(self) -> None:
		self.producer.stop.set()
		self.client.close()
		self.server.close()

	def test_every_request_gets_a_frame_that_was_captured_after_it(self) -> None:
		last = 0
		for expected in range(1, 6):
			asked = time.time()
			frame = self.client.request(last_seq=last, mapped=expected - 1, rate=2.0)
			self.assertIsNotNone(frame)
			self.assertEqual(frame.seq, expected)
			self.assertGreaterEqual(frame.wall_time, asked - 1e-3, "the frame was captured before the request: a stale frame")
			self.assertEqual(int(frame.rgb[0, 0, 0]), expected)
			self.assertEqual(int(frame.depth[0, 0]), 1000 + expected)
			self.assertEqual(frame.rgb.shape, (6, 8, 3))
			last = frame.seq
		self.assertEqual(self.producer.captures, 5, "frames nobody asked for are not captured")

	def test_a_slow_client_gets_no_backlog(self) -> None:
		self.client.request()
		time.sleep(0.2)  # the simulator keeps running, nobody asks
		frame = self.client.request()
		self.assertEqual(self.producer.captures, 2)
		self.assertEqual(frame.seq, 2)

	def test_the_server_remembers_what_the_mapper_reports(self) -> None:
		self.client.request(last_seq=0, mapped=41, rate=2.25)
		self.assertEqual(self.server.mapped, 41)
		self.assertAlmostEqual(self.server.rate, 2.25)
		self.assertEqual(self.server.clients, 1)

	def test_two_clients_can_wait_for_one_capture(self) -> None:
		other = protocol.FrameClient(f"tcp://127.0.0.1:{self.port}", io_timeout=5.0)
		other.connect(timeout=5.0)
		self.addCleanup(other.close)
		results = []
		threads = [threading.Thread(target=lambda c=c: results.append(c.request())) for c in (self.client, other)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(5.0)
		self.assertEqual(len(results), 2)
		self.assertTrue(all(frame is not None for frame in results))

	def test_closing_the_server_ends_the_stream(self) -> None:
		self.client.request()
		self.producer.stop.set()
		self.server.close()
		with self.assertRaises(protocol.SourceClosed):
			for _ in range(3):
				self.client.request()


class NoProducerTest(unittest.TestCase):
	def test_a_client_gets_nothing_instead_of_hanging_when_the_simulator_stalls(self) -> None:
		server = protocol.FrameServer("127.0.0.1", 0, wait_s=0.1)
		port = server.start()
		self.addCleanup(server.close)
		client = protocol.FrameClient(f"tcp://127.0.0.1:{port}", io_timeout=5.0)
		client.connect(timeout=5.0)
		self.addCleanup(client.close)
		started = time.monotonic()
		self.assertIsNone(client.request())
		self.assertLess(time.monotonic() - started, 2.0)

	def test_connect_waits_for_the_server_to_come_up(self) -> None:
		probe = socket.socket()
		probe.bind(("127.0.0.1", 0))
		port = probe.getsockname()[1]
		probe.close()
		server = protocol.FrameServer("127.0.0.1", port, wait_s=0.1)
		timer = threading.Timer(0.6, server.start)
		timer.start()
		self.addCleanup(server.close)
		self.addCleanup(timer.cancel)
		client = protocol.FrameClient(f"tcp://127.0.0.1:{port}")
		client.connect(timeout=10.0, interval=0.1)
		self.addCleanup(client.close)
		self.assertTrue(client.connected)

	def test_connect_gives_up_and_can_be_stopped(self) -> None:
		probe = socket.socket()
		probe.bind(("127.0.0.1", 0))
		port = probe.getsockname()[1]
		probe.close()
		client = protocol.FrameClient(f"tcp://127.0.0.1:{port}")
		with self.assertRaises(TimeoutError):
			client.connect(timeout=0.3, interval=0.05)
		stop = threading.Event()
		stop.set()
		with self.assertRaises(protocol.SourceClosed):
			client.connect(timeout=5.0, stop=stop)


if __name__ == "__main__":
	unittest.main()
