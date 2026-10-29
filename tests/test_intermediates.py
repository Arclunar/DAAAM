"""save_intermediates: background export of per-frame tracks/label images and query statuses that match the eligibility loop.

  python -m unittest discover -s tests -p 'test_intermediates.py'    (in the daaam-ws container, from this repository)
"""
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import cv2
import numpy as np

from daaam.assignment.models import AssignmentResult, SelectedGroup
from daaam.config import PipelineConfig
from daaam.grounding.models import ObjectAnnotation
from daaam.pipeline.intermediates import IntermediateRecorder
from daaam.pipeline.models import Frame
from daaam.pipeline.orchestrator import PipelineOrchestrator
from daaam.tracking.models import Track


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def orchestrator(output):
    obj = object.__new__(PipelineOrchestrator)
    obj.config = PipelineConfig()
    obj.config.grounding.release_assignment_frames = True
    obj.config.workers.assignment_config.min_obs_per_track = 2
    obj._initialize_state()
    obj.logger = mock.Mock()
    obj._stop_group_processing = False
    obj.scene_graph_service = SimpleNamespace(correction_lock=threading.RLock(), corrections={},
                                              store_correction=lambda c: None)
    obj.assignment_task_queue = queue.Queue()
    obj.query_group_queue = queue.Queue()
    obj._intermediates = IntermediateRecorder(output, obj.logger)
    return obj


class RecorderTest(unittest.TestCase):
    def test_frame_row_and_exact_uint16_label_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = IntermediateRecorder(tmp, mock.Mock())
            labels = np.zeros((4, 5), np.uint16)
            labels[1:3, 2:4] = 700
            recorder.frame(dict(frame_id=3, timestamp=0.2, detections=1, tracks=[]), labels)
            recorder.event('description', 3, semantic_id=700, label='a cup')
            recorder.close()
            root = Path(tmp) / 'intermediates'
            row, = lines(root / 'frames.jsonl')
            self.assertEqual(row['label_path'], 'labels/000003.png')
            np.testing.assert_array_equal(cv2.imread(str(root / row['label_path']), cv2.IMREAD_UNCHANGED), labels)
            event, = lines(root / 'events.jsonl')
            self.assertEqual((event['kind'], event['frame_id'], event['semantic_id']), ('description', 3, 700))


class QueryStatusTest(unittest.TestCase):
    def test_statuses_follow_the_eligibility_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = orchestrator(tmp)
            mask = np.ones((8, 8), bool)
            # 1 eligible, 2 already prompted, 3 never depth-valid, 4 seen once, 5 already described, 6 'unknown' again
            valid = {1: True, 2: True, 3: False, 4: True, 5: True, 6: True}
            for number in (1, 2):
                ids = [1, 2, 3, 5, 6] + ([4] if number == 2 else [])
                tracks = [Track.from_mask(i, mask, np.asarray([0, 0, 8, 8]), depth_valid=valid[i]) for i in ids]
                obj.frame_count = number
                f = Frame(number, number / 15, np.zeros((8, 8, 3), np.uint8), tracks=tracks)
                obj._add_frame_to_history(f)
            obj.object_labels = {i: 10 + i for i in valid}
            obj.prompted_track_ids = {2}
            obj.scene_graph_service.corrections = {15: SimpleNamespace(semantic_label='a cup', confidence=1.0),
                                                   16: SimpleNamespace(semantic_label='unknown', confidence=10.0)}
            obj._trigger_query_assignment()
            obj._intermediates.close()
            event, = lines(Path(tmp) / 'intermediates' / 'events.jsonl')
            status = {t['track_id']: t['status'] for t in event['tracks']}
            self.assertEqual(status, {1: 'eligible', 2: 'prompted', 3: 'depth_invalid', 4: 'few_observations',
                                      5: 'described', 6: 'eligible'})
            self.assertEqual(obj.pending_track_ids, {t for t, s in status.items() if s == 'eligible'})
            self.assertEqual(event['window'], [1, 2])


class EventTest(unittest.TestCase):
    def test_selection_prompt_description_and_unknown_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = orchestrator(tmp)
            obj.frame_count = 9
            obj.object_labels = {1: 11}
            track = Track.from_mask(1, np.ones((8, 8), bool), np.asarray([0, 0, 8, 8]))
            f = Frame(4, 0.3, np.zeros((8, 8, 3), np.uint8), tracks=[track])
            obj._frame_store.retain(9, {4: f}, {1}, float('inf'))
            obj._process_assignment_result(AssignmentResult(9, [SelectedGroup(4, [1])]))
            obj._record_description(ObjectAnnotation(semantic_id=11, semantic_label='a cup', confidence=1.0))
            obj._create_automatic_unknown_correction(12, 'depth filtering')
            obj._intermediates.close()
            events = lines(Path(tmp) / 'intermediates' / 'events.jsonl')
            self.assertEqual([e['kind'] for e in events], ['selection', 'prompt', 'description', 'unknown'])
            self.assertEqual(events[0]['groups'], [dict(frame_id=4, track_ids=[1], role='view')])
            self.assertEqual(events[1]['semantic_ids'], [11])
            self.assertTrue(all(e['frame_id'] == 9 for e in events))


if __name__ == '__main__':
    unittest.main()
