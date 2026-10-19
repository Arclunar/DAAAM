import bz2
import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
import yaml

from scripts.gs_world import rosbag1, rosbag_to_rgbd
from scripts.gs_world.data import RGBDSequence
from scripts.gs_world.geometry import invert, matrix_to_quat, pose_matrix
from scripts.gs_world.rosbag1 import Bag

NS = 1_000_000_000
WIDTH, HEIGHT = 64, 48
K = np.array([[60.0, 0.0, 31.5], [0.0, 60.0, 23.5], [0.0, 0.0, 1.0]])
CAMERA_FRAME = "camera_color_optical_frame"


# ---- a small ROS1 bag writer (format 2.0), enough to produce the bags rosbag would record
def _field(name: str, value: bytes) -> bytes:
	item = name.encode() + b"=" + value
	return struct.pack("<I", len(item)) + item


def _record(fields: dict, data: bytes) -> bytes:
	header = b"".join(_field(k, v) for k, v in fields.items())
	return struct.pack("<I", len(header)) + header + struct.pack("<I", len(data)) + data


def _time(t: int) -> bytes:
	return struct.pack("<II", t // NS, t % NS)


def _string(text: str) -> bytes:
	data = text.encode()
	return struct.pack("<I", len(data)) + data


def _header(stamp: int, frame: str) -> bytes:
	return struct.pack("<I", 0) + _time(stamp) + _string(frame)


def image_msg(stamp: int, frame: str, array: np.ndarray, encoding: str) -> bytes:
	data = np.ascontiguousarray(array).tobytes()
	step = len(data) // array.shape[0]
	return (_header(stamp, frame) + struct.pack("<II", array.shape[0], array.shape[1]) + _string(encoding)
		+ struct.pack("<BI", 0, step) + struct.pack("<I", len(data)) + data)


def compressed_msg(stamp: int, frame: str, fmt: str, payload: bytes) -> bytes:
	return _header(stamp, frame) + _string(fmt) + struct.pack("<I", len(payload)) + payload


def camera_info_msg(stamp: int, frame: str, D, model: str = "plumb_bob") -> bytes:
	P = np.zeros((3, 4))
	P[:, :3] = K
	return (_header(stamp, frame) + struct.pack("<II", HEIGHT, WIDTH) + _string(model)
		+ struct.pack("<I", len(D)) + np.asarray(D, "<f8").tobytes() + K.astype("<f8").tobytes()
		+ np.eye(3).astype("<f8").tobytes() + P.astype("<f8").tobytes() + struct.pack("<II", 0, 0)
		+ struct.pack("<IIIIB", 0, 0, 0, 0, 0))


def odometry_msg(stamp: int, frame: str, child: str, T: np.ndarray) -> bytes:
	return (_header(stamp, frame) + _string(child) + T[:3, 3].astype("<f8").tobytes()
		+ matrix_to_quat(T[:3, :3]).astype("<f8").tobytes() + np.zeros(36, "<f8").tobytes()
		+ np.zeros(6, "<f8").tobytes() + np.zeros(36, "<f8").tobytes())


def write_bag(path: Path, messages, compression: str = "none", index: bool = True, chunk_size: int = 8) -> None:
	"""messages: (topic, type, receive time ns, serialized data) in recording order."""
	topics = {}
	for topic, kind, _, _ in messages:
		topics.setdefault(topic, (len(topics), kind))

	def connection(topic: str) -> bytes:
		conn_id, kind = topics[topic]
		data = _field("topic", topic.encode()) + _field("type", kind.encode()) + _field("md5sum", b"0" * 32)
		return _record({"op": b"\x07", "conn": struct.pack("<I", conn_id), "topic": topic.encode()}, data + _field("message_definition", b""))

	header_size = 4096
	pos = len(rosbag1.MAGIC) + header_size
	body, infos, seen = b"", [], set()
	for start in range(0, len(messages), chunk_size):
		group = messages[start:start + chunk_size]
		inner, counts = b"", {}
		for topic, _, t, data in group:
			if topic not in seen:
				inner += connection(topic)
				seen.add(topic)
			conn_id = topics[topic][0]
			inner += _record({"op": b"\x02", "conn": struct.pack("<I", conn_id), "time": _time(t)}, data)
			counts[conn_id] = counts.get(conn_id, 0) + 1
		payload = bz2.compress(inner) if compression == "bz2" else inner
		chunk = _record({"op": b"\x05", "compression": compression.encode(), "size": struct.pack("<I", len(inner))}, payload)
		times = [t for _, _, t, _ in group]
		infos.append((pos, min(times), max(times), counts))
		body += chunk
		pos += len(chunk)
	tail = b""
	if index:
		for topic in topics:
			tail += connection(topic)
		for chunk_pos, t0, t1, counts in infos:
			data = b"".join(struct.pack("<II", c, n) for c, n in counts.items())
			tail += _record({"op": b"\x06", "ver": struct.pack("<I", 1), "chunk_pos": struct.pack("<Q", chunk_pos),
				"start_time": _time(t0), "end_time": _time(t1), "count": struct.pack("<I", len(counts))}, data)
	fields = {"op": b"\x03", "index_pos": struct.pack("<Q", pos if index else 0),
		"conn_count": struct.pack("<I", len(topics)), "chunk_count": struct.pack("<I", len(infos))}
	header_bytes = b"".join(_field(k, v) for k, v in fields.items())
	padding = b" " * (header_size - 8 - len(header_bytes))
	bag_header = _record(fields, padding)
	assert len(bag_header) == header_size
	path.write_bytes(rosbag1.MAGIC + bag_header + body + tail)


def textured(seed: int) -> np.ndarray:
	rng = np.random.default_rng(seed)
	blocks = rng.integers(0, 256, (HEIGHT // 4, WIDTH // 4, 3), dtype=np.uint8)
	return cv2.resize(blocks, (WIDTH, HEIGHT), interpolation=cv2.INTER_NEAREST)


def body_pose(t: float) -> np.ndarray:
	"""T_world_body of a platform driving along x at 0.3 m/s while turning slowly."""
	a = 0.05 * t
	T = np.eye(4)
	T[:3, :3] = [[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]]
	T[:3, 3] = [0.3 * t, 0.0, 1.0]
	return T


# camera optical frame (z forward, x right, y down) mounted on a body (x forward, y left, z up), 10 cm above it
T_BODY_CAM = np.eye(4)
T_BODY_CAM[:3, :3] = np.column_stack([[0, -1, 0], [0, 0, -1], [1, 0, 0]])
T_BODY_CAM[:3, 3] = [0.05, -0.02, 0.10]
T_CAM_BODY = invert(T_BODY_CAM)


def recording(blur_every: int = 3, seconds: float = 2.0, compressed_depth: bool = False):
	"""Messages of a D455 + odometry recording: 30 Hz colour (JPEG) and aligned depth, camera info, 10 Hz odometry."""
	messages, blurred = [], set()
	t0 = 1_700_000_000 * NS
	frames = int(seconds * 30)
	depth = np.full((HEIGHT, WIDTH), 2000, np.uint16)  # 2.0 m in millimetres
	for i in range(frames):
		t = t0 + int(i * NS / 30)
		image = textured(i % 5)
		if i % blur_every == 1:
			image = cv2.GaussianBlur(image, (9, 9), 4)
			blurred.add(i)
		ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(image, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
		messages.append(("/camera/color/image_raw/compressed", "sensor_msgs/CompressedImage", t + 2_000_000,
			compressed_msg(t, CAMERA_FRAME, "rgb8; jpeg compressed bgr8", jpeg.tobytes())))
		if compressed_depth:
			ok, png = cv2.imencode(".png", depth)
			payload = struct.pack("<iff", 0, 0.0, 0.0) + png.tobytes()
			messages.append(("/camera/aligned_depth_to_color/image_raw/compressedDepth", "sensor_msgs/CompressedImage",
				t + 3_000_000, compressed_msg(t + 4_000_000, CAMERA_FRAME, "16UC1; compressedDepth png", payload)))
		else:
			messages.append(("/camera/aligned_depth_to_color/image_raw", "sensor_msgs/Image", t + 3_000_000,
				image_msg(t + 4_000_000, CAMERA_FRAME, depth, "16UC1")))
		if i % 10 == 0:
			messages.append(("/camera/color/camera_info", "sensor_msgs/CameraInfo", t + 2_000_000,
				camera_info_msg(t, CAMERA_FRAME, [-0.05, 0.01, 0.0, 0.0, 0.0])))
	for j in range(int(seconds * 10) + 2):
		t = t0 - NS // 10 + j * NS // 10
		messages.append(("/Odometry", "nav_msgs/Odometry", t, odometry_msg(t, "camera_init", "body", body_pose((t - t0) / NS))))
	messages.sort(key=lambda m: m[2])
	return messages, blurred, t0


class ReaderTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name)

	def tearDown(self) -> None:
		self.tmp.cleanup()

	def test_topics_from_the_index_and_by_scanning_agree(self) -> None:
		messages, _, _ = recording(seconds=0.5)
		write_bag(self.root / "indexed.bag", messages, index=True)
		write_bag(self.root / "plain.bag", messages, index=False)
		indexed = Bag(self.root / "indexed.bag").topics()
		scanned = Bag(self.root / "plain.bag").topics()
		self.assertEqual({t: i.count for t, i in indexed.items()}, {t: i.count for t, i in scanned.items()})
		self.assertEqual(indexed["/Odometry"].type, "nav_msgs/Odometry")
		self.assertEqual(scanned["/camera/color/image_raw/compressed"].count, 15)

	def test_messages_are_filtered_and_bz2_chunks_are_read(self) -> None:
		messages, _, _ = recording(seconds=0.5)
		write_bag(self.root / "x.bag", messages, compression="bz2")
		bag = Bag(self.root / "x.bag")
		infos = list(bag.messages(["/camera/color/camera_info"]))
		self.assertEqual(len(infos), 2)
		info = rosbag1.parse_camera_info(infos[0].data)
		self.assertEqual((info.width, info.height, info.distortion_model, info.frame_id), (WIDTH, HEIGHT, "plumb_bob", CAMERA_FRAME))
		np.testing.assert_allclose(info.K, K)
		np.testing.assert_allclose(info.D, [-0.05, 0.01, 0, 0, 0])
		self.assertFalse(bag.truncated)

	def test_cut_off_bag_returns_the_messages_before_the_cut(self) -> None:
		messages, _, _ = recording(seconds=1.0)
		path = self.root / "cut.bag"
		write_bag(path, messages, index=False, chunk_size=10)
		data = path.read_bytes()
		path.write_bytes(data[: int(len(data) * 0.6)])
		bag = Bag(path)
		got = list(bag.messages())
		self.assertTrue(bag.truncated)
		self.assertGreater(len(got), 10)
		self.assertLess(len(got), len(messages))
		self.assertEqual([m.time_ns for m in got], [m[2] for m in messages[: len(got)]])

	def test_not_a_bag(self) -> None:
		(self.root / "x.bag").write_bytes(b"#ROSBAG V1.2\n")
		with self.assertRaisesRegex(rosbag1.BagError, "format 2.0"):
			Bag(self.root / "x.bag")


class MessageTest(unittest.TestCase):
	def test_images(self) -> None:
		rgb = textured(1)
		decoded = rosbag1.image_to_array(rosbag1.parse_image(image_msg(5 * NS, "cam", rgb, "rgb8")))
		np.testing.assert_array_equal(decoded, rgb)
		decoded = rosbag1.image_to_array(rosbag1.parse_image(image_msg(5 * NS, "cam", rgb[..., ::-1], "bgr8")))
		np.testing.assert_array_equal(decoded, rgb)
		depth = np.arange(WIDTH * HEIGHT, dtype=np.uint16).reshape(HEIGHT, WIDTH)
		msg = rosbag1.parse_image(image_msg(5 * NS + 7, "depth", depth, "16UC1"))
		self.assertEqual(msg.stamp_ns, 5 * NS + 7)
		np.testing.assert_array_equal(rosbag1.image_to_array(msg), depth)
		self.assertEqual(rosbag1.header_stamp(image_msg(5 * NS + 7, "depth", depth, "16UC1")), 5 * NS + 7)
		with self.assertRaisesRegex(rosbag1.BagError, "encoding"):
			rosbag1.image_to_array(rosbag1.parse_image(image_msg(0, "x", rgb, "bayer_rggb8")))

	def test_compressed_depth_png_and_quantised_inverse_depth(self) -> None:
		depth = np.full((4, 5), 2000, np.uint16)
		depth[0, 0] = 0
		ok, png = cv2.imencode(".png", depth)
		msg = rosbag1.parse_compressed_image(compressed_msg(0, "d", "16UC1; compressedDepth png",
			struct.pack("<iff", 0, 0.0, 0.0) + png.tobytes()))
		np.testing.assert_array_equal(rosbag1.decode_compressed_depth(msg), depth)
		quant_a, quant_b = 1000.0, 10.0
		q = np.full((4, 5), quant_a / 2.0 + quant_b, np.float64).astype(np.uint16)  # 2 m
		q[1, 1] = 0  # no measurement
		ok, png = cv2.imencode(".png", q)
		msg = rosbag1.parse_compressed_image(compressed_msg(0, "d", "32FC1; compressedDepth png",
			struct.pack("<iff", 0, quant_a, quant_b) + png.tobytes()))
		metres = rosbag1.decode_compressed_depth(msg)
		self.assertAlmostEqual(float(metres[0, 0]), 2.0, places=5)
		self.assertEqual(float(metres[1, 1]), 0.0)

	def test_odometry_pose(self) -> None:
		T = body_pose(1.5)
		sample = rosbag1.parse_odometry(odometry_msg(3 * NS, "camera_init", "body", T))
		self.assertEqual((sample.frame_id, sample.child_frame_id, sample.stamp_ns), ("camera_init", "body", 3 * NS))
		np.testing.assert_allclose(sample.T, T, atol=1e-12)


class ExtrinsicTest(unittest.TestCase):
	def test_key_conventions(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			chain = Path(tmp) / "chain.yaml"
			chain.write_text(yaml.safe_dump({"T_cam_livox": T_CAM_BODY.tolist(), "T_body_cam": T_BODY_CAM.tolist(),
				"_convention": "p_a = T_a_b * p_b"}))
			T, note = rosbag_to_rgbd.load_extrinsic(f"{chain}:T_cam_livox")
			np.testing.assert_allclose(T, T_BODY_CAM, atol=1e-12)
			self.assertIn("inverted", note)
			T, note = rosbag_to_rgbd.load_extrinsic(f"{chain}:T_body_cam")
			np.testing.assert_allclose(T, T_BODY_CAM)
			self.assertNotIn("inverted", note)
			with self.assertRaisesRegex(SystemExit, "T_cam_livox"):
				rosbag_to_rgbd.load_extrinsic(str(chain))  # two matrices: the key is required
			with self.assertRaisesRegex(SystemExit, "no 4x4 matrix 'T_x'"):
				rosbag_to_rgbd.load_extrinsic(f"{chain}:T_x")
			js = Path(tmp) / "extrinsic.json"
			js.write_text(json.dumps({"status": "calibrated", "T_tracking_camera": T_BODY_CAM.tolist()}))
			np.testing.assert_allclose(rosbag_to_rgbd.load_extrinsic(str(js))[0], T_BODY_CAM)
			txt = Path(tmp) / "T.txt"
			txt.write_text(" ".join(f"{v:.12g}" for v in T_BODY_CAM.reshape(-1)))
			np.testing.assert_allclose(rosbag_to_rgbd.load_extrinsic(str(txt))[0], T_BODY_CAM)

	def test_kalibr_camchain(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "camchain.yaml"
			path.write_text(yaml.safe_dump({"cam0": {"camera_model": "pinhole", "distortion_model": "radtan",
				"distortion_coeffs": [-0.04, 0.03, -0.0005, -0.0008], "intrinsics": [648.1, 648.0, 652.4, 371.5],
				"resolution": [1280, 720]}}))
			Kc, D, model, size = rosbag_to_rgbd.load_camchain(str(path))
		self.assertEqual((model, size), ("plumb_bob", (1280, 720)))
		self.assertAlmostEqual(Kc[0, 2], 652.4)
		np.testing.assert_allclose(D, [-0.04, 0.03, -0.0005, -0.0008])


class ConversionTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name)
		self.chain = self.root / "chain.yaml"
		self.chain.write_text(yaml.safe_dump({"T_cam_livox": T_CAM_BODY.tolist()}))

	def tearDown(self) -> None:
		self.tmp.cleanup()

	def run_converter(self, *argv) -> str:
		buffer = io.StringIO()
		with contextlib.redirect_stdout(buffer):
			code = rosbag_to_rgbd.main([str(a) for a in argv])
		self.assertEqual(code, 0, buffer.getvalue())
		return buffer.getvalue()

	def test_d455_bag_with_odometry_becomes_a_posed_rgbd_folder(self) -> None:
		messages, blurred, t0 = recording()
		write_bag(self.root / "d455.bag", messages)
		out = self.root / "d455"
		text = self.run_converter(self.root / "d455.bag", out, "--extrinsic", f"{self.chain}:T_cam_livox",
			"--min-translation", "0.05", "--min-rotation", "30")
		self.assertIn("wrote", text)
		seq = RGBDSequence(out)
		self.assertGreaterEqual(len(seq), 8)
		self.assertLessEqual(len(seq), 14)
		for i in range(len(seq)):
			t = seq.timestamps[i]
			expected = body_pose(t - t0 / NS) @ T_BODY_CAM
			np.testing.assert_allclose(seq.poses[i], expected, atol=1e-5)
			frame = int(round((t - t0 / NS) * 30))
			self.assertNotIn(frame, blurred, "the sharpest image of each stretch is kept")
		centre = seq.depth(0)[HEIGHT // 2, WIDTH // 2]
		self.assertAlmostEqual(float(centre), 2.0, places=3)
		np.testing.assert_allclose(seq.camera.K, K)
		self.assertTrue((out / "valid_mask.png").is_file(), "distorted images are undistorted, their border is masked")
		report = json.loads((out / "conversion.json").read_text())
		self.assertIn("inverted", report["extrinsic"])
		self.assertTrue(report["undistorted"])
		self.assertEqual(report["topics"]["pose"], "/Odometry")
		self.assertEqual(report["counts"]["keyframes"], len(seq))
		self.assertLess(report["rgb_depth_skew_ms"]["max"], 5.0)

	def test_compressed_depth_and_camera_poses(self) -> None:
		messages, _, t0 = recording(seconds=1.0, compressed_depth=True)
		write_bag(self.root / "d455.bag", messages, compression="bz2")
		out = self.root / "cam"
		self.run_converter(self.root / "d455.bag", out, "--pose-is-camera")
		seq = RGBDSequence(out)
		np.testing.assert_allclose(seq.poses[0], body_pose(seq.timestamps[0] - t0 / NS), atol=1e-5)
		self.assertAlmostEqual(float(seq.depth(0)[HEIGHT // 2, WIDTH // 2]), 2.0, places=3)

	def test_missing_extrinsic_and_listing(self) -> None:
		messages, _, _ = recording(seconds=0.5)
		write_bag(self.root / "d455.bag", messages)
		with contextlib.redirect_stdout(io.StringIO()):
			with self.assertRaisesRegex(SystemExit, "--extrinsic"):
				rosbag_to_rgbd.main([str(self.root / "d455.bag"), str(self.root / "out")])
		text = self.run_converter(self.root / "d455.bag", "--list")
		self.assertIn("/camera/aligned_depth_to_color/image_raw", text)
		self.assertIn("nav_msgs/Odometry", text)
		self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
	unittest.main()
