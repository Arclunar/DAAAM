#!/usr/bin/env python3
"""Lift TUM native places from observed RGB-D surfaces and saved fragment maps.

No object-neighbour inference or new model calls: vote only from depth-verified
image fragments whose DAM descriptions are already present in this run.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def surface_votes(positions, world_T_camera, intrinsics, depth, labels, described,
		max_drop=2., step=.025, tolerance=.04):
	"""First observed surface below each native place, or None when unobserved.

	The depth comparison is axial camera Z (not Euclidean range). An unknown
	front surface blocks labels behind it. Holes and out-of-view points never vote.
	"""
	if depth.shape != labels.shape:
		raise ValueError('depth and fragment labels must have the same shape')
	if step <= 0 or max_drop <= 0 or tolerance < 0:
		raise ValueError('invalid surface sampling parameters')
	T = np.linalg.inv(world_T_camera)
	result = []
	for x, y, z in positions:
		world = np.array([[x, y, z - drop, 1.] for drop in np.arange(0., max_drop, step)])
		camera = world @ T.T
		pixels = camera[:, :3] @ intrinsics.T
		valid = camera[:, 2] > 1e-6
		uv = np.zeros((len(world), 2), dtype=int)
		uv[valid] = np.rint(pixels[valid, :2] / pixels[valid, 2:3]).astype(int)
		vote = None
		for point, (u, v), in_front in zip(camera, uv, valid):
			if not in_front or not (0 <= u < depth.shape[1] and 0 <= v < depth.shape[0]):
				continue
			observed = depth[v, u]
			if not np.isfinite(observed) or observed <= 0:
				continue
			if abs(point[2] - observed) <= tolerance:
				label = int(labels[v, u])
				if label in described:
					vote = (label, 1. / float(point[:3] @ point[:3]))
				break
		result.append(vote)
	return result


def lift(run, dataset):
	import cv2
	import yaml
	from scripts.replay_web.manifest import write_json_atomic

	run, dataset = Path(run), Path(dataset)
	graph_path = run / 'dsg.json'
	graph = json.loads(graph_path.read_text())
	key = graph['layer_names']['TRAVERSABILITY']
	places = [n for n in graph['nodes'] if (n['layer'], n.get('partition', 0)) == (key['layer'], key['partition'])]
	if not places:
		print('Native place semantic lifting: no native places yet; longer/observed geometry is required')
		return 0
	frames = sorted((run / 'images').glob('labels_*.png'))
	if not frames:
		raise ValueError('Native semantic lifting requires saved per-pixel fragment label maps')
	info = json.loads((dataset / 'camera_info.json').read_text())
	K = np.asarray(info['intrinsics']).reshape(3, 3)
	poses = np.loadtxt(dataset / 'pose/poses.txt').reshape(-1, 4, 4)
	corrections = yaml.safe_load((run / 'corrections.yaml').read_text())
	descriptions = {item['label']: item['name'] for item in corrections['label_names']
		if item.get('name') and item['name'].lower() != 'unknown'}
	positions = np.array([node['attributes']['position'] for node in places])
	weights = [Counter() for _ in places]
	sources = [[] for _ in places]
	for file in frames:
		frame = int(file.stem.split('_')[-1])
		if frame >= len(poses):
			raise ValueError('fragment frame index exceeds dataset poses')
		depth = cv2.imread(str(dataset / 'depth' / ('%06d.png' % frame)), cv2.IMREAD_UNCHANGED)
		labels = cv2.imread(str(file), cv2.IMREAD_UNCHANGED)
		if depth is None or labels is None:
			raise ValueError('missing depth or fragment map for frame %d' % frame)
		depth = depth.astype(float) / float(info['depth_scale'])
		# Match the TUM pipeline's accepted depth interval.
		depth[(depth < .3) | (depth > 4.)] = 0
		votes = surface_votes(positions, poses[frame], K, depth, labels, descriptions)
		for i, vote in enumerate(votes):
			if vote is not None:
				label, weight = vote
				weights[i][label] += weight
				sources[i].append({'frame_id': frame, 'label': label})
	count = 0
	for node, votes, source in zip(places, weights, sources):
		if not votes:
			continue
		attrs = node['attributes']
		metadata = attrs.setdefault('metadata', {})
		metadata.setdefault('hydra_label_weights', attrs.get('label_weights', []))
		attrs['label_weights'] = sorted(votes.items())
		label = max(votes, key=votes.get)
		attrs['semantic_label'] = label
		metadata['description'] = descriptions[label]
		metadata['semantic_lifting'] = {'method': 'rgbd_vertical_surface_projection', 'sources': source}
		count += 1
	graph.setdefault('metadata', {})['place_semantics'] = {
		'layer': 'TRAVERSABILITY', 'method': 'rgbd_vertical_surface_projection',
		'frames': len(frames), 'described_places': count, 'total_places': len(places),
		'step_m': .025, 'depth_tolerance_m': .04, 'max_drop_m': 2.,
	}
	write_json_atomic(graph_path, graph)
	print(f'Native place semantic lifting: {count}/{len(places)} places from {len(frames)} real fragment frames')
	return count


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--run', type=Path, required=True)
	parser.add_argument('--dataset', type=Path, required=True)
	args = parser.parse_args()
	lift(args.run, args.dataset)
