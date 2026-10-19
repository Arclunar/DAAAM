"""Minimal reader for ROS1 bag files (format 2.0) and the message types gs_world converts; no ROS installation needed.

A bag is a sequence of records, each a header of 'name=value' fields plus data. Messages sit in chunk records
(uncompressed or bz2; lz4 when the optional `lz4` module is importable) next to the connection records that give
their topic and type. The reader walks the file from the start, so it also reads bags without an index (still being
recorded, or cut off by an interrupted copy: everything before the cut is returned and `truncated` is set). Messages
are decoded from the ROS1 wire format (little endian; strings and arrays carry a uint32 length) for
sensor_msgs/Image, sensor_msgs/CompressedImage (including compressedDepth), sensor_msgs/CameraInfo, nav_msgs/Odometry,
geometry_msgs/PoseStamped and geometry_msgs/PoseWithCovarianceStamped.
"""

import bz2
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Optional, Tuple

import cv2
import numpy as np

from .geometry import pose_matrix

MAGIC = b"#ROSBAG V2.0\n"
OP_MESSAGE, OP_BAG_HEADER, OP_INDEX, OP_CHUNK, OP_CHUNK_INFO, OP_CONNECTION = 0x02, 0x03, 0x04, 0x05, 0x06, 0x07


class BagError(ValueError):
	pass


@dataclass(frozen=True)
class Connection:
	id: int
	topic: str
	type: str
	md5sum: str = ""
	definition: str = ""


@dataclass(frozen=True)
class RawMessage:
	topic: str
	type: str
	time_ns: int  # receive time stored by rosbag (the header stamp is inside data)
	data: bytes


@dataclass
class TopicInfo:
	type: str
	count: int = 0
	first_ns: Optional[int] = None
	last_ns: Optional[int] = None


def parse_fields(buf) -> Dict[str, bytes]:
	"""Record header: a sequence of (uint32 length, b'name=value')."""
	view = memoryview(buf)
	fields: Dict[str, bytes] = {}
	pos = 0
	while pos < len(view):
		if pos + 4 > len(view):
			raise BagError("truncated record header")
		(length,) = struct.unpack_from("<I", view, pos)
		pos += 4
		if pos + length > len(view):
			raise BagError("record header field runs past the header")
		name, sep, value = bytes(view[pos:pos + length]).partition(b"=")
		pos += length
		if not sep:
			raise BagError("record header field without '='")
		fields[name.decode("ascii", "replace")] = value
	return fields


def _u32(value: bytes) -> int:
	return struct.unpack("<I", value)[0]


def _u64(value: bytes) -> int:
	return struct.unpack("<Q", value)[0]


def _time_ns(value: bytes) -> int:
	sec, nsec = struct.unpack("<II", value)
	return sec * 1_000_000_000 + nsec


def _op(fields: Dict[str, bytes]) -> int:
	op = fields.get("op")
	if not op:
		raise BagError("record without op field")
	return op[0]


class Bag:
	"""Sequential access to one ROS1 bag."""

	def __init__(self, path):
		self.path = Path(path)
		self.size = self.path.stat().st_size
		with open(self.path, "rb") as f:
			magic = f.read(len(MAGIC))
		if magic != MAGIC:
			raise BagError(f"{self.path} is not a ROS1 bag (format 2.0); it starts with {magic[:16]!r}")
		self.connections: Dict[int, Connection] = {}
		self.truncated = False

	@staticmethod
	def _read_record(stream) -> Optional[Tuple[Dict[str, bytes], bytes]]:
		"""Next top-level record, None at the end of the file; EOFError when the file ends inside a record."""
		head = stream.read(4)
		if not head:
			return None
		if len(head) < 4:
			raise EOFError
		header_len = struct.unpack("<I", head)[0]
		header = stream.read(header_len)
		if len(header) < header_len:
			raise EOFError
		size = stream.read(4)
		if len(size) < 4:
			raise EOFError
		length = struct.unpack("<I", size)[0]
		data = stream.read(length)
		if len(data) < length:
			raise EOFError
		return parse_fields(header), data

	@staticmethod
	def _records_in(buf) -> Iterator[Tuple[Dict[str, bytes], memoryview]]:
		view = memoryview(buf)
		pos = 0
		while pos < len(view):
			if pos + 4 > len(view):
				raise BagError("truncated record inside a chunk")
			(header_len,) = struct.unpack_from("<I", view, pos)
			header = view[pos + 4:pos + 4 + header_len]
			pos += 4 + header_len
			if pos + 4 > len(view):
				raise BagError("truncated record inside a chunk")
			(data_len,) = struct.unpack_from("<I", view, pos)
			data = view[pos + 4:pos + 4 + data_len]
			pos += 4 + data_len
			if pos > len(view):
				raise BagError("truncated record inside a chunk")
			yield parse_fields(header), data

	@staticmethod
	def _decompress(fields: Dict[str, bytes], data: bytes) -> bytes:
		compression = fields.get("compression", b"none").decode("ascii", "replace")
		if compression == "none":
			return data
		if compression == "bz2":
			return bz2.decompress(data)
		if compression == "lz4":
			try:
				import lz4.frame
			except ImportError as exc:
				raise BagError("the bag uses lz4 chunk compression and the lz4 Python module is not installed; "
					"recompress it with `rosbag compress --bz2` or decompress it with `rosbag decompress`") from exc
			return lz4.frame.decompress(data)
		raise BagError(f"unknown chunk compression {compression!r}")

	def _add_connection(self, fields: Dict[str, bytes], data) -> None:
		conn_id = _u32(fields["conn"])
		if conn_id in self.connections:
			return
		info = parse_fields(data)
		topic = (fields.get("topic") or info.get("topic") or b"").decode("utf-8", "replace")
		self.connections[conn_id] = Connection(conn_id, topic, info.get("type", b"").decode("utf-8", "replace"),
			info.get("md5sum", b"").decode("ascii", "replace"),
			info.get("message_definition", b"").decode("utf-8", "replace"))

	def _walk(self, wanted: Optional[set], with_data: bool) -> Iterator[Tuple[Connection, int, Optional[bytes]]]:
		self.truncated = False
		with open(self.path, "rb") as stream:
			stream.seek(len(MAGIC))
			while True:
				try:
					record = self._read_record(stream)
				except EOFError:
					self.truncated = True
					return
				if record is None:
					return
				fields, data = record
				op = _op(fields)
				if op == OP_CHUNK:
					inner = self._records_in(self._decompress(fields, data))
				elif op in (OP_CONNECTION, OP_MESSAGE):
					inner = iter([(fields, data)])
				else:
					continue  # bag header, index data and chunk info records hold no messages
				for item_fields, item_data in inner:
					item_op = _op(item_fields)
					if item_op == OP_CONNECTION:
						self._add_connection(item_fields, item_data)
					elif item_op == OP_MESSAGE:
						conn = self.connections.get(_u32(item_fields["conn"]))
						if conn is None or (wanted is not None and conn.topic not in wanted):
							continue
						yield conn, _time_ns(item_fields["time"]), (bytes(item_data) if with_data else None)

	def messages(self, topics: Optional[Iterable[str]] = None) -> Iterator[RawMessage]:
		"""Messages of the given topics (all when None) in file order."""
		wanted = set(topics) if topics is not None else None
		for conn, time_ns, data in self._walk(wanted, with_data=True):
			yield RawMessage(conn.topic, conn.type, time_ns, data)

	def topics(self) -> Dict[str, TopicInfo]:
		"""Topic -> type, message count and first/last receive time.

		Uses the index at the end of the bag when there is one (times are then per chunk), else reads every record.
		"""
		summary = self._summary_from_index()
		if summary is not None:
			return summary
		result: Dict[str, TopicInfo] = {}
		for conn, time_ns, _ in self._walk(None, with_data=False):
			info = result.setdefault(conn.topic, TopicInfo(conn.type))
			info.count += 1
			info.first_ns = time_ns if info.first_ns is None else min(info.first_ns, time_ns)
			info.last_ns = time_ns if info.last_ns is None else max(info.last_ns, time_ns)
		return result

	def _summary_from_index(self) -> Optional[Dict[str, TopicInfo]]:
		try:
			with open(self.path, "rb") as stream:
				stream.seek(len(MAGIC))
				record = self._read_record(stream)
				if record is None or _op(record[0]) != OP_BAG_HEADER:
					return None
				fields = record[0]
				index_pos, conn_count = _u64(fields["index_pos"]), _u32(fields["conn_count"])
				chunk_count = _u32(fields["chunk_count"])
				if index_pos == 0 or index_pos >= self.size:
					return None
				stream.seek(index_pos)
				for _ in range(conn_count):
					record = self._read_record(stream)
					if record is None or _op(record[0]) != OP_CONNECTION:
						return None
					self._add_connection(*record)
				counts: Dict[int, int] = {}
				first: Dict[int, int] = {}
				last: Dict[int, int] = {}
				for _ in range(chunk_count):
					record = self._read_record(stream)
					if record is None or _op(record[0]) != OP_CHUNK_INFO:
						return None
					fields, data = record
					start, end = _time_ns(fields["start_time"]), _time_ns(fields["end_time"])
					for j in range(_u32(fields["count"])):
						conn_id, count = struct.unpack_from("<II", data, 8 * j)
						counts[conn_id] = counts.get(conn_id, 0) + count
						first[conn_id] = min(first.get(conn_id, start), start)
						last[conn_id] = max(last.get(conn_id, end), end)
		except (EOFError, KeyError, struct.error, BagError):
			return None
		result: Dict[str, TopicInfo] = {}
		for conn_id, count in counts.items():
			conn = self.connections.get(conn_id)
			if conn is None:
				return None
			info = result.setdefault(conn.topic, TopicInfo(conn.type))
			info.count += count
			info.first_ns = first[conn_id] if info.first_ns is None else min(info.first_ns, first[conn_id])
			info.last_ns = last[conn_id] if info.last_ns is None else max(info.last_ns, last[conn_id])
		return result


class _Cursor:
	def __init__(self, data):
		self.buf = memoryview(data)
		self.pos = 0

	def take(self, n: int) -> memoryview:
		if self.pos + n > len(self.buf):
			raise BagError("message is shorter than its type requires")
		value = self.buf[self.pos:self.pos + n]
		self.pos += n
		return value

	def u8(self) -> int:
		return self.take(1)[0]

	def u32(self) -> int:
		return struct.unpack("<I", self.take(4))[0]

	def f64s(self, n: int) -> np.ndarray:
		return np.frombuffer(self.take(8 * n), dtype="<f8").astype(np.float64)

	def string(self) -> str:
		return bytes(self.take(self.u32())).decode("utf-8", "replace")

	def blob(self) -> memoryview:
		return self.take(self.u32())

	def stamp_ns(self) -> int:
		return _time_ns(bytes(self.take(8)))

	def header(self) -> Tuple[int, str]:
		self.u32()  # seq
		stamp = self.stamp_ns()
		return stamp, self.string()


def header_stamp(data: bytes) -> int:
	"""Header stamp (ns) of any message that starts with std_msgs/Header, without decoding the rest."""
	if len(data) < 12:
		raise BagError("message too short for a std_msgs/Header")
	sec, nsec = struct.unpack_from("<II", data, 4)
	return sec * 1_000_000_000 + nsec


@dataclass
class Image:
	stamp_ns: int
	frame_id: str
	height: int
	width: int
	encoding: str
	is_bigendian: int
	step: int
	data: memoryview


@dataclass
class CompressedImage:
	stamp_ns: int
	frame_id: str
	format: str
	data: memoryview


@dataclass
class CameraInfo:
	stamp_ns: int
	frame_id: str
	height: int
	width: int
	distortion_model: str
	D: np.ndarray
	K: np.ndarray
	R: np.ndarray
	P: np.ndarray


@dataclass
class PoseSample:
	stamp_ns: int
	frame_id: str
	child_frame_id: str
	T: np.ndarray


def parse_image(data: bytes) -> Image:
	c = _Cursor(data)
	stamp, frame = c.header()
	height, width = c.u32(), c.u32()
	encoding = c.string()
	big, step = c.u8(), c.u32()
	return Image(stamp, frame, height, width, encoding, big, step, c.blob())


_ENCODINGS = {
	"rgb8": (3, np.uint8), "bgr8": (3, np.uint8), "8uc3": (3, np.uint8), "rgba8": (4, np.uint8), "bgra8": (4, np.uint8),
	"mono8": (1, np.uint8), "8uc1": (1, np.uint8), "mono16": (1, np.uint16), "16uc1": (1, np.uint16),
	"32fc1": (1, np.float32),
}


def image_to_array(msg: Image) -> np.ndarray:
	"""Pixels of an Image: RGB uint8 for colour encodings, the stored values (HxW) for mono and depth encodings."""
	encoding = msg.encoding.lower()
	if encoding not in _ENCODINGS:
		raise BagError(f"unsupported image encoding {msg.encoding!r}")
	channels, dtype = _ENCODINGS[encoding]
	itemsize = np.dtype(dtype).itemsize
	row_bytes = msg.width * channels * itemsize
	if msg.step < row_bytes or len(msg.data) < msg.step * msg.height:
		raise BagError(f"image data ({len(msg.data)} bytes, step {msg.step}) too short for {msg.width}x{msg.height} {msg.encoding}")
	rows = np.frombuffer(msg.data, dtype=np.uint8, count=msg.step * msg.height).reshape(msg.height, msg.step)
	wire = np.dtype(dtype).newbyteorder(">" if msg.is_bigendian else "<")
	pixels = np.ascontiguousarray(rows[:, :row_bytes]).view(wire).reshape(msg.height, msg.width, channels)
	pixels = pixels.astype(dtype)
	if encoding in ("bgr8", "8uc3"):
		pixels = pixels[..., ::-1]
	elif encoding == "bgra8":
		pixels = pixels[..., 2::-1]
	elif encoding == "rgba8":
		pixels = pixels[..., :3]
	return np.ascontiguousarray(pixels[..., 0] if channels == 1 else pixels)


def parse_compressed_image(data: bytes) -> CompressedImage:
	c = _Cursor(data)
	stamp, frame = c.header()
	return CompressedImage(stamp, frame, c.string(), c.blob())


def decode_compressed_color(msg: CompressedImage, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
	"""RGB uint8 of a JPEG/PNG CompressedImage (grayscale when flags asks for it)."""
	image = cv2.imdecode(np.frombuffer(msg.data, np.uint8), flags)
	if image is None:
		raise BagError(f"cannot decode compressed image (format {msg.format!r})")
	return cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if image.ndim == 3 else image


def decode_compressed_depth(msg: CompressedImage) -> np.ndarray:
	"""Depth of a compressed depth image: uint16 sensor units for 16UC1, float32 metres for 32FC1.

	compressed_depth_image_transport prefixes the PNG with a 12-byte header (int32 format, float32 depthQuantA,
	float32 depthQuantB); 32FC1 depth is stored as quantised inverse depth q = depthQuantA / depth + depthQuantB.
	"""
	fmt = msg.format.lower()
	raw = bytes(msg.data)
	if "compresseddepth" in fmt:
		if "rvl" in fmt:
			raise BagError("compressedDepth images in RVL format are not supported; record them as png")
		if len(raw) < 12:
			raise BagError("compressedDepth message without its 12-byte header")
		_, quant_a, quant_b = struct.unpack_from("<iff", raw, 0)
		image = cv2.imdecode(np.frombuffer(raw[12:], np.uint8), cv2.IMREAD_UNCHANGED)
		if image is None:
			raise BagError("cannot decode the PNG of a compressedDepth image")
		if fmt.startswith("32fc1"):
			q = image.astype(np.float32)
			with np.errstate(divide="ignore", invalid="ignore"):
				depth = np.where(q > 0, quant_a / (q - quant_b), 0.0)
			return np.where(np.isfinite(depth) & (depth > 0), depth, 0.0).astype(np.float32)
		return image
	image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
	if image is None:
		raise BagError(f"cannot decode compressed depth image (format {msg.format!r})")
	return image


def parse_camera_info(data: bytes) -> CameraInfo:
	c = _Cursor(data)
	stamp, frame = c.header()
	height, width = c.u32(), c.u32()
	model = c.string()
	D = c.f64s(c.u32())
	K = c.f64s(9).reshape(3, 3)
	R = c.f64s(9).reshape(3, 3)
	P = c.f64s(12).reshape(3, 4)
	return CameraInfo(stamp, frame, height, width, model, D, K, R, P)


def _pose(c: _Cursor) -> np.ndarray:
	position = c.f64s(3)
	orientation = c.f64s(4)  # x, y, z, w
	if not np.all(np.isfinite(position)) or not np.all(np.isfinite(orientation)) or np.linalg.norm(orientation) < 1e-9:
		raise BagError("pose with non-finite values or a zero quaternion")
	return pose_matrix(position, orientation)


def parse_odometry(data: bytes) -> PoseSample:
	c = _Cursor(data)
	stamp, frame = c.header()
	child = c.string()
	return PoseSample(stamp, frame, child, _pose(c))


def parse_pose_stamped(data: bytes) -> PoseSample:
	c = _Cursor(data)
	stamp, frame = c.header()
	return PoseSample(stamp, frame, "", _pose(c))


POSE_PARSERS: Dict[str, Callable[[bytes], PoseSample]] = {
	"nav_msgs/Odometry": parse_odometry,
	"geometry_msgs/PoseStamped": parse_pose_stamped,
	# PoseWithCovarianceStamped: header, then pose (followed by the covariance, which is not needed)
	"geometry_msgs/PoseWithCovarianceStamped": parse_pose_stamped,
}
