import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from scipy.spatial.transform import Rotation

from daaam.datasets import DatasetExhausted, DatasetFrame, ImageSequenceDataset, LiveDataset
from daaam.datasets.live_protocol import FrameServer, RemoteFrame, SourceClosed
from daaam.datasets.loaders.live import camera_pose_to_optical_zup, matrix_to_pose7


class FakeClient:
	"""Stands in for FrameClient: the frames come from a list; None is a timeout, an exception is raised, and the end of the list closes the stream."""

	def __init__(self, frames):
		self.frames = list(frames)
		self.requests = []
		self.closed = False
		self.connect_error = None

	def connect(self, timeout=60.0, stop=None, interval=0.5):
		if self.connect_error is not None:
			raise self.connect_error

	def request(self, last_seq=0, mapped=0, rate=0.0):
		self.requests.append((last_seq, mapped, rate))
		if not self.frames:
			raise SourceClosed("the simulator ended the stream")
		item = self.frames.pop(0)
		if isinstance(item, Exception):
			raise item
		return item

	def close(self):
		self.closed = True


def habitat_pose(position=(0.0, 0.0, 0.0), yaw_deg=0.0):
	"""Camera-to-world of a Habitat camera: rotation about +y, y up, looking along its own -z."""
	pose = np.eye(4)
	pose[:3, :3] = Rotation.from_euler("y", yaw_deg, degrees=True).as_matrix()
	pose[:3, 3] = position
	return pose


def make_frame(seq, pose=None, convention="opengl_yup", width=8, height=6, depth_units=5000, wall_time=None):
	pose = habitat_pose() if pose is None else pose
	rgb = np.full((height, width, 3), seq % 250, np.uint8)
	rgb[0, 0] = (10, 20, 30)
	depth = np.full((height, width), depth_units, np.uint16)
	depth[1, 1] = 0  # no measurement
	wall_time = time.time() if wall_time is None else wall_time
	meta = {"wall_time": wall_time, "intrinsics": [4.0, 4.0, 3.5, 2.5], "depth_scale": 5000.0, "pose": list(pose.reshape(-1)),
		"pose_convention": convention, "info": {"scene": "fake"}}
	return RemoteFrame(seq, wall_time, rgb, depth, meta)


def dataset(frames, tmp=None, **kwargs):
	kwargs.setdefault("fps", 4.0)
	kwargs.setdefault("record", False)
	kwargs.setdefault("status_every", 0)
	client = FakeClient(frames)
	return LiveDataset(Path(tmp or "unused"), client=client, **kwargs), client


class PoseConventionTest(unittest.TestCase):
	def test_a_habitat_camera_becomes_an_optical_camera_in_a_z_up_world(self) -> None:
		matrix = camera_pose_to_optical_zup(habitat_pose((1.0, 2.0, 3.0)), "opengl_yup")
		np.testing.assert_allclose(matrix[:3, 3], [1.0, -3.0, 2.0], atol=1e-12)  # (x, y, z) -> (x, -z, y)
		rotation = matrix[:3, :3]
		np.testing.assert_allclose(rotation @ [1, 0, 0], [1, 0, 0], atol=1e-12)  # image right stays right
		np.testing.assert_allclose(rotation @ [0, 1, 0], [0, 0, -1], atol=1e-12)  # image down is down (-z)
		np.testing.assert_allclose(rotation @ [0, 0, 1], [0, 1, 0], atol=1e-12)  # forward: Habitat -z is world +y
		self.assertAlmostEqual(np.linalg.det(rotation), 1.0)

	def test_the_floor_stays_level_when_the_camera_turns_and_tilts(self) -> None:
		# a camera 1.2 m above the floor, yawed 90 degrees to the left: forward is Habitat -x, i.e. -x in the z-up world
		pose = habitat_pose((0.0, 1.2, 0.0), 90.0)
		matrix = camera_pose_to_optical_zup(pose, "opengl_yup")
		np.testing.assert_allclose(matrix[:3, :3] @ [0, 0, 1], [-1, 0, 0], atol=1e-12)
		np.testing.assert_allclose(matrix[:3, 3], [0.0, 0.0, 1.2], atol=1e-12, err_msg="Habitat's height (+y) is the height (+z) of the z-up world")
		# a level camera keeps its image rows horizontal: the image-right axis has no height component
		self.assertAlmostEqual((matrix[:3, :3] @ [1, 0, 0])[2], 0.0)
		# looking up by 20 degrees tilts the forward axis up by 20 degrees
		tilted = pose.copy()
		tilted[:3, :3] = tilted[:3, :3] @ Rotation.from_euler("x", 20.0, degrees=True).as_matrix()
		forward = camera_pose_to_optical_zup(tilted, "opengl_yup")[:3, :3] @ [0, 0, 1]
		self.assertAlmostEqual(np.degrees(np.arcsin(forward[2])), 20.0, places=6)

	def test_poses_that_are_already_optical_and_z_up_pass_through(self) -> None:
		pose = np.eye(4)
		pose[:3, 3] = (1, 2, 3)
		np.testing.assert_array_equal(camera_pose_to_optical_zup(pose, "optical_zup"), pose)
		with self.assertRaises(ValueError):
			camera_pose_to_optical_zup(pose, "something else")

	def test_the_7d_pose_holds_the_same_rotation(self) -> None:
		matrix = camera_pose_to_optical_zup(habitat_pose((0.5, 1.0, -2.0), 33.0), "opengl_yup")
		pose = matrix_to_pose7(matrix)
		np.testing.assert_allclose(pose[:3], matrix[:3, 3])
		np.testing.assert_allclose(Rotation.from_quat(pose[3:]).as_matrix(), matrix[:3, :3], atol=1e-12)


class FramesTest(unittest.TestCase):
	def test_frame_k_is_stamped_k_over_fps(self) -> None:
		live, _ = dataset([make_frame(1), make_frame(2), make_frame(3)])
		frames = [live[i] for i in range(3)]
		self.assertEqual([f.timestamp for f in frames], [0.0, 0.25, 0.5])
		self.assertEqual([f.frame_id for f in frames], [0, 1, 2])

	def test_the_frame_has_what_the_pipeline_needs(self) -> None:
		live, _ = dataset([make_frame(1, habitat_pose((1.0, 1.2, 3.0)), depth_units=7500)])
		frame = live[0]
		self.assertIsInstance(frame, DatasetFrame)
		self.assertEqual(frame.rgb_image.shape, (6, 8, 3))
		self.assertEqual(frame.rgb_image.dtype, np.uint8)
		self.assertTrue(frame.rgb_image.flags.writeable)
		self.assertEqual(tuple(frame.rgb_image[0, 0]), (10, 20, 30), "RGB stays RGB")
		self.assertEqual(frame.depth_image.dtype, np.float32)
		self.assertAlmostEqual(float(frame.depth_image[2, 2]), 1.5)
		self.assertEqual(float(frame.depth_image[1, 1]), 0.0, "no measurement stays 0")
		self.assertEqual(frame.transform.shape, (7,))
		np.testing.assert_allclose(frame.transform[:3], [1.0, -3.0, 1.2])
		np.testing.assert_allclose(frame.camera_info["intrinsics"], [[4, 0, 3.5], [0, 4, 2.5], [0, 0, 1]])
		self.assertEqual((frame.camera_info["width"], frame.camera_info["height"]), (8, 6))
		pipeline_frame = frame.to_pipeline_frame()
		self.assertEqual(pipeline_frame.camera_intrinsics, {"fx": 4.0, "fy": 4.0, "cx": 3.5, "cy": 2.5})

	def test_camera_info_comes_from_the_first_frame(self) -> None:
		live, client = dataset([make_frame(1)])
		info = live.get_camera_info()
		self.assertEqual(info["width"], 8)
		self.assertEqual(len(client.requests), 1)

	def test_length_is_unbounded_unless_a_frame_limit_is_given(self) -> None:
		self.assertEqual(len(dataset([])[0]), 10 ** 9)
		bounded, _ = dataset([make_frame(1)], max_frames=5)
		self.assertEqual(len(bounded), 5)
		with self.assertRaises(IndexError):
			bounded[5]

	def test_what_the_mapper_has_done_is_reported_to_the_simulator(self) -> None:
		live, client = dataset([make_frame(i) for i in range(1, 5)])
		for i in range(4):
			live[i]
		self.assertEqual([r[0] for r in client.requests], [0, 1, 2, 3], "the newest sequence number it has")
		self.assertEqual([r[1] for r in client.requests], [0, 1, 2, 3], "the frames it has mapped")

	def test_velocity_follows_the_poses(self) -> None:
		frames = [make_frame(i + 1, habitat_pose((0.0, 1.2, -0.5 * i))) for i in range(5)]  # 0.5 m per frame along Habitat -z
		live, _ = dataset(frames, fps=2.0)
		velocities = [live[i].lin_vel for i in range(5)]
		np.testing.assert_allclose(velocities[0], 0.0)
		np.testing.assert_allclose(velocities[-1], [0.0, 1.0, 0.0], atol=1e-9)  # 1 m/s along the world +y

	def test_statistics_of_the_motion_between_mapped_frames(self) -> None:
		frames = [make_frame(1, habitat_pose((0, 1.2, 0), 0)), make_frame(2, habitat_pose((0, 1.2, -0.4), 30)), make_frame(3, habitat_pose((0, 1.2, -0.8), 60))]
		live, _ = dataset(frames)
		for i in range(3):
			live[i]
		stats = live.stats()
		self.assertEqual(stats["frames"], 3)
		self.assertAlmostEqual(stats["step_m_mean"], 0.4, places=6)
		self.assertAlmostEqual(stats["turn_deg_mean"], 30.0, places=6)
		self.assertAlmostEqual(stats["turn_deg_max"], 30.0, places=6)
		self.assertGreaterEqual(stats["frame_age_ms_mean"], 0.0)


class FirstFrameTest(unittest.TestCase):
	def test_reading_the_same_index_again_returns_the_same_frame(self) -> None:
		live, client = dataset([make_frame(1), make_frame(2)], max_cached_age=5.0)
		first = live[0]
		self.assertIs(live[0], first)
		self.assertEqual(len(client.requests), 1)

	def test_an_old_first_frame_is_replaced_by_a_fresh_one(self) -> None:
		# the runner reads index 0 to set up the camera, loads its models for a minute, then reads index 0 as the first frame
		live, client = dataset([make_frame(1), make_frame(2), make_frame(3)], max_cached_age=0.05)
		probe = live[0]
		time.sleep(0.12)
		first = live[0]
		self.assertIsNot(first, probe)
		self.assertEqual(int(first.rgb_image[2, 2, 0]), 2, "the second frame of the stream")
		self.assertEqual(first.frame_id, 0)
		self.assertEqual(first.timestamp, 0.0)
		self.assertEqual(int(live[1].rgb_image[2, 2, 0]), 3)
		self.assertEqual(live.stats()["frames"], 2, "the probe is not a frame of the run")

	def test_the_clock_of_the_run_starts_with_the_first_frame_it_maps(self) -> None:
		live, _ = dataset([make_frame(1), make_frame(2), make_frame(3)], max_cached_age=0.0)
		live[0]
		time.sleep(0.3)  # models loading
		live[0]
		live[1]
		self.assertLess(live._returned - live._started, 0.25)


class EndOfStreamTest(unittest.TestCase):
	def test_a_closed_simulator_ends_the_dataset(self) -> None:
		live, client = dataset([make_frame(1)])
		live[0]
		with self.assertRaises(DatasetExhausted) as caught:
			live[1]
		self.assertIsInstance(caught.exception, IndexError)
		live.close()
		self.assertTrue(client.closed)

	def test_stopping_ends_the_dataset_even_while_it_waits_for_a_frame(self) -> None:
		class Stalled(FakeClient):
			def request(self, last_seq=0, mapped=0, rate=0.0):
				time.sleep(0.01)
				return None  # the simulator has nothing yet

		live = LiveDataset(Path("unused"), client=Stalled([]), record=False, status_every=0, fps=4.0)
		threading.Timer(0.1, live.request_stop).start()
		started = time.monotonic()
		with self.assertRaises(DatasetExhausted):
			live[0]
		self.assertLess(time.monotonic() - started, 2.0)

	def test_no_simulator_means_the_end_not_a_crash(self) -> None:
		live, client = dataset([])
		client.connect_error = TimeoutError("no simulator is serving frames")
		self.assertFalse(live.validate())
		with self.assertRaises(DatasetExhausted):
			live[0]

	def test_validate_connects(self) -> None:
		live, _ = dataset([])
		self.assertTrue(live.validate())


class RecordingTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.session = Path(self.temp.name) / "session"

	def record(self, frames, reads, **kwargs):
		live, _ = dataset(frames, tmp=self.session, record=True, **kwargs)
		read = [live[i] for i in reads]
		live.close()
		return live, read

	def test_a_session_is_a_dataset_that_maps_and_replays_again(self) -> None:
		poses = [habitat_pose((0.1 * i, 1.2, -0.3 * i), 7.0 * i) for i in range(4)]
		frames = [make_frame(i + 1, poses[i], depth_units=3000 + 100 * i) for i in range(4)]
		live, read = self.record(frames, range(4))
		self.assertEqual(sorted(p.name for p in (self.session / "rgb").iterdir()), [f"{i:06d}.png" for i in range(4)])
		self.assertEqual(len(list((self.session / "depth").iterdir())), 4)
		self.assertEqual([p.name for p in (self.session / "pose").iterdir()], ["poses.txt"], "the layout of scripts/prepare_tum_rgbd.py")
		self.assertEqual(np.loadtxt(self.session / "pose" / "poses.txt").shape, (4, 16))
		self.assertEqual(len((self.session / "timestamps.txt").read_text().split()), 4)
		self.assertTrue((self.session / "live_stats.json").is_file())
		offline = ImageSequenceDataset(self.session, config={"fps": 4.0}, depth_scale=5000.0, compute_velocities=False)
		self.assertEqual(len(offline), 4)
		for i, live_frame in enumerate(read):
			again = offline[i]
			np.testing.assert_array_equal(again.rgb_image, live_frame.rgb_image)
			np.testing.assert_array_equal(again.depth_image, live_frame.depth_image)
			self.assertEqual(again.timestamp, live_frame.timestamp)
			np.testing.assert_allclose(again.transform[:3], live_frame.transform[:3], atol=1e-9)
			np.testing.assert_allclose(Rotation.from_quat(again.transform[3:]).as_matrix(), Rotation.from_quat(live_frame.transform[3:]).as_matrix(), atol=1e-9)
		info = offline.get_camera_info()
		self.assertEqual(info["depth_scale"], 5000.0)
		self.assertEqual(info["fps"], 4.0)

	def test_a_frame_that_was_read_twice_is_recorded_once_with_its_second_content(self) -> None:
		frames = [make_frame(1), make_frame(2), make_frame(3)]
		live, _ = dataset(frames, tmp=self.session, record=True, max_cached_age=0.0)
		live[0]
		live[0]  # the runner's second read of index 0 (the cache is too old)
		live[1]
		live.close()
		self.assertEqual(len(list((self.session / "rgb").iterdir())), 2)
		self.assertEqual(len((self.session / "timestamps.txt").read_text().split()), 2)
		offline = ImageSequenceDataset(self.session, config={"fps": 4.0}, depth_scale=5000.0, compute_velocities=False)
		self.assertEqual(int(offline[0].rgb_image[2, 2, 0]), 2)
		self.assertEqual(int(offline[1].rgb_image[2, 2, 0]), 3)

	def test_a_session_that_was_never_finished_can_still_be_read(self) -> None:
		live, _ = dataset([make_frame(i + 1, habitat_pose((0.0, 1.2, -0.2 * i))) for i in range(3)], tmp=self.session, record=True)
		for i in range(3):
			live[i]
		live._recorder._queue.put(None)  # let the writer finish what was handed to it, as a crash after the last frame would
		live._recorder._thread.join()
		self.assertEqual(len(list((self.session / "pose").glob("0*.txt"))), 3, "one file per pose until the session is finished")
		offline = ImageSequenceDataset(self.session, config={"fps": 4.0}, depth_scale=5000.0, compute_velocities=False)
		self.assertEqual(len(offline), 3)
		self.assertAlmostEqual(float(offline[2].transform[1]), 0.4)  # two steps of 0.2 m along Habitat -z (forward) are 0.4 m along the world's +y

	def test_an_earlier_session_is_never_overwritten(self) -> None:
		self.record([make_frame(1)], [0])
		live, _ = dataset([make_frame(1)], tmp=self.session, record=True)
		with self.assertRaises(FileExistsError):
			live[0]

	def test_without_recording_nothing_is_written(self) -> None:
		live, _ = dataset([make_frame(1)], tmp=self.session, record=False)
		live[0]
		live.close()
		self.assertFalse(self.session.exists())


class RecorderFailureTest(unittest.TestCase):
	def setUp(self):
		from daaam.datasets.loaders.live import SessionRecorder
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name) / "session"
		self.recorder = SessionRecorder(self.root)
		self.rgb = np.zeros((2, 2, 3), dtype=np.uint8)
		self.depth = np.ones((2, 2), dtype=np.uint16)

	def test_failed_rgb_or_depth_write_is_reported_at_finish(self):
		for results in ([False], [True, False]):
			with self.subTest(results=results), tempfile.TemporaryDirectory() as tmp:
				from daaam.datasets.loaders.live import SessionRecorder
				recorder = SessionRecorder(Path(tmp) / "session")
				recorder.start({})
				with mock.patch("daaam.datasets.loaders.live.cv2.imwrite", side_effect=results):
					recorder.add(0, self.rgb, self.depth, np.eye(4))
					with self.assertRaisesRegex(RuntimeError, "record"):
						recorder.finish([0.0])
				self.assertEqual(recorder.written, 0)
				self.assertFalse((recorder.root / "timestamps.txt").exists())

	def test_error_writing_the_final_frame_is_not_published_as_a_complete_session(self):
		self.recorder.start({})
		with mock.patch("daaam.datasets.loaders.live.cv2.imwrite", side_effect=OSError("disk full")):
			self.recorder.add(0, self.rgb, self.depth, np.eye(4))
			with self.assertRaisesRegex(RuntimeError, "disk full"):
				self.recorder.finish([0.0])
		self.assertFalse((self.root / "timestamps.txt").exists())
		self.assertFalse((self.root / "pose" / "poses.txt").exists())

	def test_a_failed_writer_releases_a_producer_or_finish_waiting_on_a_full_queue(self):
		from daaam.datasets.loaders.live import SessionRecorder
		import queue
		for operation in ("add", "finish"):
			with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
				recorder = SessionRecorder(Path(tmp) / "session")
				started, release = threading.Event(), threading.Event()
				errors = []
				def fail_write(*args):
					started.set()
					release.wait(3.0)
					raise OSError("disk full")
				def produce():
					try:
						if operation == "add":
							recorder.add(65, self.rgb, self.depth, np.eye(4))
						else:
							recorder.finish([0.0])
					except RuntimeError as error:
						errors.append(error)
				with mock.patch("daaam.datasets.loaders.live.cv2.imwrite", side_effect=fail_write):
					recorder.start({})
					recorder.add(0, self.rgb, self.depth, np.eye(4))
					self.assertTrue(started.wait(2.0))
					for index in range(1, 65):
						recorder.add(index, self.rgb, self.depth, np.eye(4))
					producer = threading.Thread(target=produce, daemon=True)
					producer.start()
					release.set()
					producer.join(2.0)
					try:
						self.assertFalse(producer.is_alive(), "failed recording must not hang mapping or shutdown")
						self.assertEqual(len(errors), 1)
					finally:
						while not recorder._queue.empty():
							try:
								recorder._queue.get_nowait()
							except queue.Empty:
								break
						producer.join(2.0)
						if recorder._thread is not None:
							recorder._thread.join(2.0)


class OverTheSocketTest(unittest.TestCase):
	def test_frames_flow_from_a_server_through_the_dataset_and_the_end_is_noticed(self) -> None:
		server = FrameServer("127.0.0.1", 0, wait_s=0.2)
		port = server.start()
		stop = threading.Event()
		captures = []

		def simulator():
			while not stop.is_set():
				generation = server.poll()
				if generation:
					captures.append(generation)
					frame = make_frame(len(captures), habitat_pose((0.0, 1.2, -0.1 * len(captures))))
					server.publish(generation, frame.meta, frame.rgb, frame.depth)
				time.sleep(0.001)

		thread = threading.Thread(target=simulator, daemon=True)
		thread.start()
		self.addCleanup(thread.join, 2.0)
		self.addCleanup(stop.set)
		live = LiveDataset(Path("unused"), url=f"tcp://127.0.0.1:{port}", record=False, status_every=0, fps=4.0, connect_timeout=5.0)
		self.assertTrue(live.validate())
		frames = [live[i] for i in range(4)]
		self.assertEqual([int(f.rgb_image[2, 2, 0]) for f in frames], [1, 2, 3, 4])
		self.assertEqual(len(captures), 4, "one capture per frame the mapper asked for")
		server.close()
		with self.assertRaises(DatasetExhausted):
			live[4]
		live.close()


class RunnerEndTest(unittest.TestCase):
	def test_the_runner_finishes_normally_when_the_stream_ends(self) -> None:
		try:
			from daaam.hydra.runner import HydraPipelineRunner
		except Exception as error:  # noqa: BLE001 - the pipeline stack is only in the container
			self.skipTest(f"pipeline stack not importable: {error}")

		class Stream:
			def __len__(self):
				return 10 ** 9

			def __getitem__(self, i):
				if i >= 3:
					raise DatasetExhausted("the simulator ended the stream")
				return make_dataset_frame(i)

		def make_dataset_frame(i):
			return DatasetFrame(frame_id=i, timestamp=i / 4.0, rgb_image=np.zeros((6, 8, 3), np.uint8), depth_image=np.ones((6, 8), np.float32),
				transform=np.array([0, 0, 0, 0, 0, 0, 1.0]))

		runner = object.__new__(HydraPipelineRunner)
		runner.logger = logging.getLogger("test_live_runner")
		runner.dataset = Stream()
		runner.show_progress = False
		runner.target_fps = None
		runner.wait_for_workers = False
		runner.logging_manager = None
		runner.output_dir = Path("unused")
		runner.stats = {"frames_processed": 0, "total_frames": len(runner.dataset), "hydra_processing_times": [], "cv_processing_times": [],
			"tracks_created": 0, "corrections_applied": 0, "dsg_nodes": 0, "dsg_edges": 0}
		runner._initialize_hydra_camera = lambda: True
		runner.hydra = mock.MagicMock()
		runner.hydra.initialize_pipeline.return_value = True
		runner.hydra.process_frame.return_value = True
		runner.hydra.get_stats.return_value = {"dsg_nodes": 1, "dsg_edges": 2, "frames_processed": 3}
		runner.orchestrator = mock.MagicMock()
		runner.orchestrator.process_frame.return_value = (np.zeros((6, 8), np.int32), np.zeros((6, 8, 3), np.uint8))
		runner.orchestrator.get_health_status.return_value = {}
		runner._save_results = mock.MagicMock()
		stats = runner.run()
		self.assertEqual(stats["frames_processed"], 3)
		runner._save_results.assert_called_once()
		runner.orchestrator.stop.assert_called_once()
		runner.hydra.shutdown.assert_called_once()


if __name__ == "__main__":
	unittest.main()
