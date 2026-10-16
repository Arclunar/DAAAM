import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from daaam.grounding.workers.dam_grounding import DAMGroundingWorkerMultiImage
from daaam.grounding.utils import (
	correction_label_entry,
	create_corrections_from_descriptions,
)
from daaam.pipeline.models import PromptRecord
from daaam.tracking.models import Track


class DescriptionProvenanceTest(unittest.TestCase):
	def test_multi_image_batch_keeps_each_records_own_source_frame(self) -> None:
		worker = object.__new__(DAMGroundingWorkerMultiImage)
		worker.worker_id = "test-worker"
		worker.worker_logger = mock.Mock()
		worker.total_masks = 2
		worker.config = {"enable_selectframe_clip_features": False}
		worker.clip_handler = None
		worker.compute_full_image_description = False
		worker.sentence_embedding_handler = None
		worker.dam_agent = SimpleNamespace(
			query_multi_image_multi_mask=lambda **_: [["first object"], ["second object"]]
		)
		worker._save_grounding_image = mock.Mock(return_value=None)
		worker._save_grounding_annotations = mock.Mock()
		mask = np.ones((8, 8), np.uint8)
		track = Track.from_mask(7, mask, np.asarray([0, 0, 8, 8]))
		worker.aggregated_records = [
			PromptRecord(np.zeros((8, 8, 3), np.uint8), [track], {7: 41}, frame_id=11, timestamp=0.5),
			PromptRecord(np.zeros((8, 8, 3), np.uint8), [track], {7: 42}, frame_id=29, timestamp=1.75),
		]

		corrections = worker._process_aggregated_batch()

		self.assertEqual(
			[(item.semantic_id, item.description_frame_id, item.description_timestamp) for item in corrections],
			[(41, 11, 0.5), (42, 29, 1.75)],
		)

	def test_object_correction_keeps_the_exact_dam_input_frame(self) -> None:
		corrections = create_corrections_from_descriptions(
			["a green chair", "a wooden desk"],
			[41, 42],
			"test-worker",
			source_frame_id=137,
			source_timestamp=9.125,
		)

		self.assertEqual([item.description_frame_id for item in corrections], [137, 137])
		self.assertEqual([item.description_timestamp for item in corrections], [9.125, 9.125])

	def test_correction_yaml_entry_separates_description_source_from_history(self) -> None:
		correction = create_corrections_from_descriptions(
			["a green chair"],
			[41],
			"test-worker",
			source_frame_id=137,
			source_timestamp=9.125,
		)[0]
		correction.frame_ids = [120, 121, 137]
		correction.timestamps = [8.0, 8.1, 9.125]
		correction.observation_count = 3

		entry = correction_label_entry(41, correction)

		self.assertEqual(entry["description_source"], {"frame_id": 137, "timestamp": 9.125})
		self.assertEqual(entry["temporal_history"]["frame_ids"], [120, 121, 137])


if __name__ == "__main__":
	unittest.main()
