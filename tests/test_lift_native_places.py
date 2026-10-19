import unittest
import json
from pathlib import Path
import tempfile
import numpy as np
from scripts.lift_native_places import surface_votes, lift


class SurfaceProjectionTest(unittest.TestCase):
	def test_votes_require_a_visible_depth_surface_and_a_described_fragment(self):
		# Optical z points downward in world z; camera is 1 m above a flat surface.
		T = np.diag([1., -1., -1., 1.])
		T[2, 3] = 1.
		K = np.array([[10., 0., 2.], [0., 10., 2.], [0., 0., 1.]])
		depth = np.ones((5, 5))
		labels = np.full((5, 5), 7)
		positions = np.array([[0., 0., 1.], [2., 2., 1.]])
		votes = surface_votes(positions, T, K, depth, labels, {7}, max_drop=2., step=.025, tolerance=.01)
		self.assertEqual(votes[0][0], 7)
		self.assertAlmostEqual(votes[0][1], 1.)
		self.assertIsNone(votes[1], 'outside the image must not acquire a label')
		self.assertEqual(surface_votes(positions, T, K, depth * 0, labels, {7}), [None, None])
		self.assertEqual(surface_votes(positions, T, K, depth, labels, {8}), [None, None])

	def test_unknown_foreground_surface_does_not_reveal_an_occluded_label(self):
		T = np.diag([1., -1., -1., 1.]); T[2, 3] = 1.
		K = np.eye(3)
		self.assertEqual(surface_votes(np.array([[0., 0., 1.]]), T, K,
			np.ones((2, 2)), np.zeros((2, 2), dtype=int), {7}), [None])

	def test_saved_fragment_frames_publish_native_votes_with_provenance_idempotently(self):
		import cv2
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp); run = root / 'run'; dataset = root / 'dataset'
			(run / 'images').mkdir(parents=True)
			(dataset / 'depth').mkdir(parents=True); (dataset / 'pose').mkdir()
			T = np.diag([1., -1., -1., 1.]); T[2, 3] = 1.
			np.savetxt(dataset / 'pose/poses.txt', T.reshape(1, 16))
			(dataset / 'camera_info.json').write_text(json.dumps({'intrinsics': np.eye(3).tolist(), 'depth_scale': 5000}))
			cv2.imwrite(str(dataset / 'depth/000000.png'), np.full((2, 2), 5000, dtype=np.uint16))
			cv2.imwrite(str(run / 'images/labels_000000.png'), np.full((2, 2), 7, dtype=np.uint16))
			(run / 'corrections.yaml').write_text('label_names:\n- label: 7\n  name: wooden floor\n')
			graph_path = run / 'dsg.json'
			graph_path.write_text(json.dumps({'layer_names': {'TRAVERSABILITY': {'layer': 3, 'partition': 2}},
				'nodes': [{'id': 99, 'layer': 3, 'partition': 2, 'attributes':
					{'position': [0., 0., 1.], 'label_weights': [[0, 3.]]}}]}))
			self.assertEqual(lift(run, dataset), 1)
			first = json.loads(graph_path.read_text())
			attrs = first['nodes'][0]['attributes']
			self.assertEqual(attrs['semantic_label'], 7)
			self.assertEqual(attrs['metadata']['description'], 'wooden floor')
			self.assertEqual(attrs['metadata']['semantic_lifting']['sources'], [{'frame_id': 0, 'label': 7}])
			self.assertEqual(lift(run, dataset), 1)
			self.assertEqual(json.loads(graph_path.read_text()), first)
