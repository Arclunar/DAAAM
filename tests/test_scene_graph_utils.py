from __future__ import annotations

import types
import unittest

import numpy as np

from daaam.utils.scene_graph_utils import objects_by_region


class _Layer:
	def __init__(self, nodes):
		self.nodes = nodes


class _Graph:
	def __init__(self):
		place_id = types.SimpleNamespace(value=1)
		object_id = types.SimpleNamespace(category_id=10)
		self.place = types.SimpleNamespace(
			id=place_id,
			layer=types.SimpleNamespace(layer=3),
			attributes=types.SimpleNamespace(position=np.array([0.0, 0.0, 0.0])),
		)
		self.room = types.SimpleNamespace(layer=types.SimpleNamespace(layer=4))
		self.obj = types.SimpleNamespace(
			id=object_id,
			attributes=types.SimpleNamespace(position=np.array([0.1, 0.0, 0.0])),
		)
		self.layers = {"OBJECTS": _Layer([self.obj]), "PLACES": _Layer([self.place])}
		self.interlayer_edges = [types.SimpleNamespace(source=1, target=100)]

	def get_layer(self, name):
		if name == 1:  # spark_dsg.DsgLayers.OBJECTS
			return self.layers["OBJECTS"]
		return self.layers[name]

	def has_layer(self, name):
		return name in self.layers

	def has_node(self, node_id):
		return node_id in (1, 100)

	def get_node(self, node_id):
		return self.place if node_id == 1 else self.room


class SceneGraphRegionTest(unittest.TestCase):
	def test_objects_can_be_assigned_through_the_tum_places_layer(self) -> None:
		by_region, region_by_object, backgrounds = objects_by_region(_Graph(), places_layer="PLACES")
		self.assertEqual(by_region, {100: [10]})
		self.assertEqual(region_by_object, {10: 100})
		self.assertEqual(backgrounds, {})


if __name__ == "__main__":
	unittest.main()
