"""Real RGB-D geometry/projection regression; test labels are not DAM output."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

DATASET = Path('/datasets/tum_fr1_desk2')
REPO = Path(__file__).resolve().parents[1]


def probe():
	import cv2
	import hydra_python
	import numpy as np
	import spark_dsg
	import yaml
	from scipy.spatial.transform import Rotation
	from daaam.hydra.config_loader import load_hydra_config

	info = json.loads((DATASET / 'camera_info.json').read_text())
	K = np.array(info['intrinsics']).reshape(3, 3)
	camera = hydra_python.make_camera(fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2],
		width=info['width'], height=info['height'], min_range=.3, max_range=4.)
	config = load_hydra_config('config/hydra_config/tum_rgbd_khronos.yaml')
	config.update(total_semantic_labels=2, object_labels=[], dynamic_labels=[], invalid_labels=[0],
		label_names=[{'label': 1, 'name': 'synthetic background'}])
	hydra_python.set_glog_level(2, 0)
	with hydra_python.external_plugins('khronos'):
		pipeline = hydra_python.HydraPipeline.from_config(yaml.safe_dump(config), camera,
			robot_id=0, config_verbosity=0, use_step_mode=True)
	poses = np.loadtxt(DATASET / 'pose/poses.txt').reshape(-1, 4, 4)
	stamps = np.loadtxt(DATASET / 'timestamps.txt')
	for i in range(100):
		rgb = cv2.cvtColor(cv2.imread(str(DATASET / 'rgb' / ('%06d.png' % i))), cv2.COLOR_BGR2RGB)
		depth = cv2.imread(str(DATASET / 'depth' / ('%06d.png' % i)), -1).astype(float) / 5000
		depth[(depth < .3) | (depth > 4)] = 0
		mm = (depth * 1000).astype(np.uint16)
		mm[mm == 0] = 1
		T = poses[i]
		q = Rotation.from_matrix(T[:3, :3]).as_quat()
		pipeline.step(int((stamps[i] - stamps[0]) * 1e9), T[:3, 3], q[[3, 0, 1, 2]],
			mm, np.ones(mm.shape, dtype=np.int32), rgb)
	with tempfile.TemporaryDirectory() as out:
		pipeline.save(Path(out))
		graph = spark_dsg.DynamicSceneGraph.load(str(Path(out) / 'backend/dsg.json'))
		nodes = list(graph.get_layer('TRAVERSABILITY').nodes)
		print('NATIVE_PLACES ' + json.dumps({'count': len(nodes),
			'labelled': sum(1 in node.attributes.label_weights for node in nodes)}), flush=True)
	# Hydra's extension owns background threads; mirror run_pipeline's shutdown.
	sys.stdout.flush()
	os._exit(0)


@unittest.skipUnless(DATASET.exists(), 'requires prepared fr1_desk2 and the DAAAM container')
class TumNativePlacesTest(unittest.TestCase):
	def test_native_traversability_and_projected_labels_survive_100_real_frames(self):
		result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe'],
			cwd=str(REPO), capture_output=True, text=True, timeout=120)
		self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
		line = next(line for line in result.stdout.splitlines() if line.startswith('NATIVE_PLACES '))
		counts = json.loads(line[len('NATIVE_PLACES '):])
		self.assertGreater(counts['count'], 0, 'native places vanished under the handheld-camera height profile')
		self.assertGreater(counts['labelled'], 0, 'image labels never reached the native places')


if __name__ == '__main__':
	if '--probe' in sys.argv:
		probe()
	else:
		unittest.main()
