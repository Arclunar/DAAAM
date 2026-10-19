"""Wire protocol between a simulator bridge and `LiveDataset`: the newest RGB-D frame over a plain TCP socket.

Standard library and numpy only. The bridge runs in an environment without the `daaam` package (the habitat-sim conda
environment) and loads this file by path, so nothing here may import from `daaam`.

The mapper pulls. It sends a request when it is ready for another frame and gets a frame that was captured after the
request arrived, so a mapper that is slower than the simulator never sees a backlog and never works on stale data;
the frames it skips are simply never captured (no read-back, no copy, no encoding).

    request  (client -> server, 32 bytes)  b"DLQ1", flags u32 (0), last_seq u64, mapped u64, rate f64
    response (server -> client)            b"DLP1", meta_len u32, payload_len u32, meta (UTF-8 JSON), payload
    payload  (status "frame" only)         RGB uint8 H*W*3, then depth uint16 H*W (little endian, 0 = no measurement)

`mapped` and `rate` (frames the mapper has finished, and its recent speed in frames/s) are only for the simulator's
on-screen display. The meta of a frame holds `status`, `seq`, `wall_time` (time.time() of the capture), `width`,
`height`, `depth_scale` (depth units per metre), `intrinsics` ([fx, fy, cx, cy] in pixels), `pose` (16 numbers, the
row-major 4x4 camera-to-world matrix) and `pose_convention` (`opengl_yup` or `optical_zup`, see LiveDataset). A
response with status `timeout` (nothing captured within `wait_s`) or `closed` (the simulator is going away) has no
payload.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import numpy as np

REQUEST_MAGIC = b"DLQ1"
RESPONSE_MAGIC = b"DLP1"
_REQUEST = struct.Struct("<4sIQQd")
_RESPONSE = struct.Struct("<4sII")
DEPTH_SCALE = 5000.0  # depth units per metre on the wire and in the recorded PNGs (as the other RGB-D datasets here)
DEFAULT_URL = "tcp://127.0.0.1:8002"
_MAX_META_BYTES = 1 << 20


class SourceClosed(ConnectionError):
	"""The simulator ended the stream or went away."""


class RemoteFrame(NamedTuple):
	seq: int
	wall_time: float
	rgb: np.ndarray  # H x W x 3 uint8, RGB order
	depth: np.ndarray  # H x W uint16, `meta["depth_scale"]` units per metre, 0 = no measurement
	meta: Dict[str, Any]


def parse_url(url: str) -> Tuple[str, int]:
	"""`tcp://host:port` (or plain `host:port`) -> (host, port)."""
	text = url.split("://", 1)[1] if "://" in url else url
	host, _, port = text.rpartition(":")
	if not host or not port.isdigit():
		raise ValueError(f"expected tcp://host:port, got {url!r}")
	return host, int(port)


def encode_depth(depth: np.ndarray, far: float, scale: float = DEPTH_SCALE) -> np.ndarray:
	"""Metric float depth -> uint16 units; no measurement (non-finite, <= 0, at or beyond the far plane) becomes 0."""
	depth = np.asarray(depth)
	valid = np.isfinite(depth) & (depth > 0) & (depth < far) & (depth < 65535.0 / scale)
	out = np.zeros(depth.shape, np.uint16)
	out[valid] = np.rint(depth[valid] * scale).astype(np.uint16)
	return out


def recv_exact(sock: socket.socket, count: int) -> bytes:
	"""Exactly `count` bytes; SourceClosed when the peer closes first."""
	chunks: List[bytes] = []
	remaining = count
	while remaining:
		chunk = sock.recv(min(remaining, 1 << 20))
		if not chunk:
			raise SourceClosed("connection closed by the peer")
		chunks.append(chunk)
		remaining -= len(chunk)
	return b"".join(chunks)


def send_request(sock: socket.socket, last_seq: int = 0, mapped: int = 0, rate: float = 0.0) -> None:
	sock.sendall(_REQUEST.pack(REQUEST_MAGIC, 0, int(last_seq), int(mapped), float(rate)))


def read_request(sock: socket.socket) -> Optional[Dict[str, Any]]:
	"""One request, or None when the client closed the connection between two requests."""
	try:
		first = sock.recv(_REQUEST.size)
	except OSError:
		return None
	if not first:
		return None
	data = first
	if len(data) < _REQUEST.size:
		data += recv_exact(sock, _REQUEST.size - len(data))
	magic, _flags, last_seq, mapped, rate = _REQUEST.unpack(data)
	if magic != REQUEST_MAGIC:
		raise ValueError(f"bad request magic {magic!r}")
	return {"last_seq": last_seq, "mapped": mapped, "rate": rate}


def send_response(sock: socket.socket, meta: Dict[str, Any], rgb: Optional[np.ndarray] = None, depth: Optional[np.ndarray] = None) -> None:
	body = json.dumps(meta, separators=(",", ":")).encode("utf-8")
	parts: List[Any] = []
	if rgb is not None and depth is not None:
		# flat byte views of the (contiguous) arrays: no copy
		parts = [np.ascontiguousarray(rgb, dtype=np.uint8).reshape(-1), np.ascontiguousarray(depth, dtype="<u2").reshape(-1).view(np.uint8)]
	payload = sum(len(part) for part in parts)
	sock.sendall(_RESPONSE.pack(RESPONSE_MAGIC, len(body), payload) + body)
	for part in parts:
		sock.sendall(part)


def read_response(sock: socket.socket) -> Tuple[Dict[str, Any], Optional[np.ndarray], Optional[np.ndarray]]:
	magic, meta_len, payload_len = _RESPONSE.unpack(recv_exact(sock, _RESPONSE.size))
	if magic != RESPONSE_MAGIC or meta_len > _MAX_META_BYTES:
		raise ValueError("not a live frame response (bad magic or meta size)")
	meta = json.loads(recv_exact(sock, meta_len).decode("utf-8"))
	if not payload_len:
		return meta, None, None
	payload = recv_exact(sock, payload_len)
	width, height = int(meta["width"]), int(meta["height"])
	rgb_bytes = width * height * 3
	if payload_len != rgb_bytes + width * height * 2:
		raise ValueError(f"payload of {payload_len} bytes does not fit {width}x{height}")
	rgb = np.frombuffer(payload, np.uint8, rgb_bytes).reshape(height, width, 3)
	depth = np.frombuffer(payload, "<u2", width * height, rgb_bytes).reshape(height, width)
	return meta, rgb, depth


class FrameServer:
	"""Serves frames that the simulator captures on demand (the simulator side of the protocol).

	The simulator's own loop stays in charge of the simulator and of the GL context: once per tick it calls `poll()`,
	and when that returns a generation it captures one frame and hands it over with `publish(generation, ...)`. A
	request that arrived before the capture started is answered with that frame, one that arrived during or after it
	waits for the next capture.
	"""

	def __init__(self, host: str = "127.0.0.1", port: int = 8002, wait_s: float = 0.5):
		self.host = host
		self.port = port
		self.wait_s = float(wait_s)
		self._cond = threading.Condition()
		self._requests = 0  # demands received
		self._done = 0  # newest demand that a published frame answers
		self._seq = 0
		self._frame: Optional[Tuple[Dict[str, Any], np.ndarray, np.ndarray]] = None
		self._closed = False
		self._listener: Optional[socket.socket] = None
		self._accept_thread: Optional[threading.Thread] = None
		self.clients = 0
		self.served = 0
		self.mapped = 0  # as last reported by a client
		self.rate = 0.0
		self.last_request = 0.0  # time.monotonic() of the last request

	def start(self) -> int:
		"""Bind and start accepting; returns the port (useful with port 0)."""
		self._listener = socket.create_server((self.host, self.port), backlog=4)
		self._listener.settimeout(0.5)
		self.port = self._listener.getsockname()[1]
		self._accept_thread = threading.Thread(target=self._accept_loop, name="live-frame-accept", daemon=True)
		self._accept_thread.start()
		return self.port

	def poll(self) -> int:
		"""The generation to capture for when a client is waiting for a frame, otherwise 0."""
		with self._cond:
			return self._requests if self._requests > self._done else 0

	def publish(self, generation: int, meta: Dict[str, Any], rgb: np.ndarray, depth: np.ndarray) -> int:
		"""Answer every demand up to `generation` with this frame (the arrays must not be changed afterwards)."""
		with self._cond:
			self._seq += 1
			full = dict(meta, status="frame", seq=self._seq, width=int(rgb.shape[1]), height=int(rgb.shape[0]))
			full.setdefault("wall_time", time.time())
			self._frame = (full, rgb, depth)
			self._done = max(self._done, generation)
			self._cond.notify_all()
			return self._seq

	def close(self) -> None:
		"""Tell the clients that are waiting that the stream ended and stop listening."""
		with self._cond:
			self._closed = True
			self._cond.notify_all()
		if self._listener is not None:
			try:
				self._listener.close()
			except OSError:
				pass
		if self._accept_thread is not None:
			self._accept_thread.join(timeout=2.0)
		time.sleep(0.05)  # let the waiting handlers send their `closed` answer

	def _accept_loop(self) -> None:
		while not self._closed:
			try:
				conn, _addr = self._listener.accept()  # type: ignore[union-attr]
			except socket.timeout:
				continue
			except OSError:
				return
			conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
			threading.Thread(target=self._serve, args=(conn,), name="live-frame-client", daemon=True).start()

	def _serve(self, conn: socket.socket) -> None:
		with self._cond:
			self.clients += 1
		try:
			while True:
				request = read_request(conn)
				if request is None:
					return
				self.mapped, self.rate, self.last_request = request["mapped"], request["rate"], time.monotonic()
				frame = None
				with self._cond:
					self._requests += 1
					mine = self._requests
					deadline = time.monotonic() + self.wait_s
					while self._done < mine and not self._closed:
						left = deadline - time.monotonic()
						if left <= 0:
							break
						self._cond.wait(left)
					if self._closed:
						status = "closed"
					elif self._done >= mine and self._frame is not None:
						status, frame = "frame", self._frame
					else:
						status = "timeout"
				if frame is not None:
					send_response(conn, frame[0], frame[1], frame[2])
					self.served += 1
				else:
					send_response(conn, {"status": status})
				if status == "closed":
					return
		except (OSError, ValueError, SourceClosed):
			return
		finally:
			with self._cond:
				self.clients -= 1
			try:
				conn.close()
			except OSError:
				pass


class FrameClient:
	"""The mapper side of the protocol: pulls the newest frame from a `FrameServer`."""

	def __init__(self, url: str = DEFAULT_URL, io_timeout: float = 30.0):
		self.url = url
		self.host, self.port = parse_url(url)
		self.io_timeout = io_timeout
		self._sock: Optional[socket.socket] = None

	@property
	def connected(self) -> bool:
		return self._sock is not None

	def connect(self, timeout: float = 60.0, stop: Optional[threading.Event] = None, interval: float = 0.5) -> None:
		"""Connect, retrying until the simulator is up; TimeoutError after `timeout` seconds, SourceClosed if `stop` is set."""
		deadline = time.monotonic() + timeout
		while True:
			if stop is not None and stop.is_set():
				raise SourceClosed("stopped while waiting for the simulator")
			try:
				sock = socket.create_connection((self.host, self.port), timeout=3.0)
				break
			except OSError:
				if time.monotonic() >= deadline:
					raise TimeoutError(f"no simulator is serving frames at {self.url}") from None
				time.sleep(interval)
		sock.settimeout(self.io_timeout)
		sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		self._sock = sock

	def request(self, last_seq: int = 0, mapped: int = 0, rate: float = 0.0) -> Optional[RemoteFrame]:
		"""The next frame captured after this call, or None when the simulator had none within its wait time."""
		if self._sock is None:
			raise SourceClosed("not connected")
		try:
			send_request(self._sock, last_seq, mapped, rate)
			meta, rgb, depth = read_response(self._sock)
		except (OSError, SourceClosed) as error:
			self.close()
			raise SourceClosed(f"lost the simulator at {self.url}: {error}") from error
		status = meta.get("status")
		if status == "closed":
			self.close()
			raise SourceClosed("the simulator ended the stream")
		if status != "frame" or rgb is None or depth is None:
			return None
		return RemoteFrame(int(meta["seq"]), float(meta["wall_time"]), rgb, depth, meta)

	def close(self) -> None:
		if self._sock is not None:
			try:
				self._sock.close()
			except OSError:
				pass
			self._sock = None
