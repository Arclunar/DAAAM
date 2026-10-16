import numpy as np
import spark_dsg
from spark_dsg import DynamicSceneGraph, DsgLayers
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from scipy.spatial import cKDTree


def get_object_distances(graph: DynamicSceneGraph) -> Dict[Tuple[str, str], float]:
    """Calculate pairwise distances between objects in the scene graph."""
    distances = {}
    for node1 in graph.nodes:
        if node1.layer != DsgLayers.OBJECTS:
            continue
        for node2 in graph.nodes:
            if node2.layer != DsgLayers.OBJECTS or node1.id >= node2.id:
                continue
                
            pos1 = node1.attributes.position
            pos2 = node2.attributes.position
            dist = np.linalg.norm(pos1 - pos2)
            key = (node1.attributes.name, node2.attributes.name)
            distances[key] = dist
    return distances

def calculate_object_proximity_score(pos1: np.ndarray, pos2: np.ndarray, threshold: float = 2.0) -> float:
    """Calculate proximity score between two positions using soft threshold."""
    distance = np.linalg.norm(pos1 - pos2)
    return 1.0 / (1.0 + np.exp(distance - threshold))  # Sigmoid function


def _build_trav_room_mapping(scene_graph: DynamicSceneGraph, traversability_places) -> Dict[int, int]:
	"""Build mapping from traversability place IDs to room IDs from interlayer edges."""
	trav_to_room = {}
	trav_node_ids = {n.id.value for n in traversability_places.nodes}

	for edge in scene_graph.interlayer_edges:
		if not (scene_graph.has_node(edge.source) and scene_graph.has_node(edge.target)):
			continue

		source_node = scene_graph.get_node(edge.source)
		target_node = scene_graph.get_node(edge.target)

		if (hasattr(source_node, 'layer') and source_node.layer.layer == 3 and
			hasattr(target_node, 'layer') and target_node.layer.layer == 4):
			if edge.source in trav_node_ids:
				trav_to_room[edge.source] = edge.target
		elif (hasattr(target_node, 'layer') and target_node.layer.layer == 3 and
			  hasattr(source_node, 'layer') and source_node.layer.layer == 4):
			if edge.target in trav_node_ids:
				trav_to_room[edge.target] = edge.source

	return trav_to_room


def _build_trav_kdtree(traversability_places) -> Tuple[cKDTree, List[int]]:
	"""Extract positions from traversability layer and build KD-tree."""
	trav_positions = []
	trav_node_ids = []

	for node in traversability_places.nodes:
		if hasattr(node.attributes, 'position'):
			trav_positions.append(node.attributes.position)
			trav_node_ids.append(node.id.value)

	assert trav_positions, "No traversability places with positions found"

	trav_positions_array = np.array(trav_positions)
	kdtree = cKDTree(trav_positions_array)

	return kdtree, trav_node_ids


def _find_room_for_object(obj_position: np.ndarray, kdtree: cKDTree, trav_node_ids: List[int], trav_to_room: Dict[int, int]) -> Optional[int]:
	"""Find room ID for object by querying nearest traversability places."""
	_, idx = kdtree.query(obj_position, k=1)
	closest_trav_id = trav_node_ids[idx]

	if closest_trav_id in trav_to_room:
		return trav_to_room[closest_trav_id]

	_, idxs = kdtree.query(obj_position, k=5)
	for i in idxs:
		candidate_trav_id = trav_node_ids[i]
		if candidate_trav_id in trav_to_room:
			return trav_to_room[candidate_trav_id]

	return None


def objects_by_region(
	scene_graph: DynamicSceneGraph,
	places_layer: str = "TRAVERSABILITY",
) -> Tuple[Dict[int, List[int]], Dict[int, int], Dict[int, List[int]]]:
	"""Organize objects by their associated regions (rooms).

	Returns:
		Tuple[Dict[int, List[int]], Dict[int, int], Dict[int, List[int]]]:
			- Dictionary Region ID -> List of foreground Object IDs (category_ids with prefix 'O')
			- Dictionary foreground Object ID -> Region ID
			- Dictionary Region ID -> List of Background Object IDs (category_ids with prefix 'o')
	"""
	objs_by_region = defaultdict(list)
	region_by_obj = {}
	background_objs_by_region = defaultdict(list)

	objects_layer = scene_graph.get_layer(spark_dsg.DsgLayers.OBJECTS)
	traversability_places = scene_graph.get_layer(places_layer)
	background_layer = scene_graph.get_layer("BACKGROUND_OBJECTS") if scene_graph.has_layer("BACKGROUND_OBJECTS") else None

	trav_to_room = _build_trav_room_mapping(scene_graph, traversability_places)
	kdtree, trav_node_ids = _build_trav_kdtree(traversability_places)

	for obj_node in objects_layer.nodes:
		if hasattr(obj_node.attributes, 'position'):
			obj_id = obj_node.id.category_id
			room_id = _find_room_for_object(obj_node.attributes.position, kdtree, trav_node_ids, trav_to_room)
			if room_id is not None:
				objs_by_region[room_id].append(obj_id)
				region_by_obj[obj_id] = room_id

	if background_layer:
		for obj_node in background_layer.nodes:
			if hasattr(obj_node.attributes, 'position'):
				obj_id = obj_node.id.category_id
				room_id = _find_room_for_object(obj_node.attributes.position, kdtree, trav_node_ids, trav_to_room)
				if room_id is not None:
					background_objs_by_region[room_id].append(obj_id)

	return dict(objs_by_region), region_by_obj, dict(background_objs_by_region)
