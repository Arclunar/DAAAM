"""UQ-DAAAM integration: disabled selection unchanged, r-assignment, token probabilities, bundle delivery and grounding loop.

  python -m unittest discover -s tests -p 'test_uq*.py'    (in the daaam-ws container, from this repository)
"""
import os
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
import torch

from daaam.assignment.models import AssignmentResult, SelectedGroup
from daaam.assignment.workers.min_frames_max_size import MinFramesMaxSizeAssignmentWorker
from daaam.config import PipelineConfig, UQConfig
from daaam.grounding.workers.dam_grounding import DAMGroundingWorkerMultiImage
from daaam.pipeline.models import Frame, PromptRecord, UQObjectRecord, UQPromptBundle
from daaam.pipeline.orchestrator import PipelineOrchestrator
from daaam.query_manager.dam.services import DAMAgentPanoptic
from daaam.tracking.models import SimplifiedTrack, Track



def frame(number=1):
    image = np.zeros((8, 8, 3), np.uint8)
    tracks = [Track.from_mask(i, np.ones((8, 8), bool), np.asarray([0, 0, 8, 8])) for i in (1, 2)]
    return Frame(number, number / 15, image, np.ones((8, 8), np.float32), tracks=tracks)


def orchestrator():
    obj = object.__new__(PipelineOrchestrator)
    obj.config = PipelineConfig()
    obj.config.grounding.release_assignment_frames = True
    obj.config.workers.assignment_config.min_obs_per_track = 1
    obj._initialize_state()
    obj.logger = mock.Mock()
    obj._stop_group_processing = False
    obj.assignment_service = SimpleNamespace(workers=[SimpleNamespace(is_alive=lambda: True)])
    obj.scene_graph_service = SimpleNamespace(correction_lock=threading.RLock(), corrections={})
    obj.assignment_task_queue = queue.Queue()
    obj.query_group_queue = queue.Queue()
    return obj


def history():
    """Track 1 in every frame (moving right), track 2 in frames 0-3, track 3 only in frame 5 (one view)."""
    frames = []
    for index in range(8):
        tracks = [SimplifiedTrack(1, np.asarray([10 * index, 20, 10 * index + 40, 60]), region_area=500 + 50 * index)]
        if index < 4:
            tracks.append(SimplifiedTrack(2, np.asarray([50, 50, 90, 90]), region_area=300 * (index + 1)))
        if index == 5:
            tracks.append(SimplifiedTrack(3, np.asarray([0, 0, 30, 30]), region_area=900))
        frames.append(tracks)
    return frames


def worker(uq=None):
    item = object.__new__(MinFramesMaxSizeAssignmentWorker)
    item.config = {'min_frame_margin_slack': 1, **({'uq': uq} if uq is not None else {})}
    item.worker_logger = mock.Mock()
    item.min_obs_per_track = 1
    item.N_masks_per_batch = 64
    return item


def select(item):
    return item.select_prompt_images(history(), {}, (120, 160), {1: 11, 2: 12, 3: 13}, set(), 100)


class AssignmentTest(unittest.TestCase):
    def test_disabled_selection_takes_the_upstream_path(self):
        baseline = select(worker())
        with mock.patch.object(MinFramesMaxSizeAssignmentWorker, 'select_prompt_images_uq', side_effect=AssertionError):
            disabled = select(worker(dict(UQConfig().__dict__)))
        self.assertEqual([(g.global_frame_id, sorted(g.track_ids), g.role, g.quality) for g in disabled],
                         [(g.global_frame_id, sorted(g.track_ids), 'view', None) for g in baseline])

    def test_r_assignment_distinct_frames_and_candidates(self):
        uq = dict(UQConfig(enabled=True).__dict__)
        groups = select(worker(uq))
        views = {}
        for group in groups:
            for track_id in group.track_ids:
                views.setdefault((group.role, track_id), []).append(group.global_frame_id - 100)
                self.assertGreaterEqual(group.quality[track_id], 0.0)
        self.assertEqual(len(views['view', 1]), 3)
        self.assertEqual(len(set(views['view', 1])), 3)
        self.assertEqual(len(set(views['view', 2])), 3)
        self.assertEqual(views['view', 3], [5], 'one visible frame keeps the single-view path')
        self.assertNotIn(('candidate', 3), views)
        for track_id in (1, 2):
            candidates = views['candidate', track_id]
            self.assertTrue(set(candidates).isdisjoint(views['view', track_id]))
        self.assertEqual(len(views['candidate', 1]), 2)
        self.assertEqual(len(views['candidate', 2]), 1, 'track 2 has four frames: three views, one candidate')
        reward = worker(uq).track_size_reward
        scores = {i: reward(t, 120, 160) for i, tracks in enumerate(history()) for t in tracks if t.id == 1}
        unused = sorted((i for i in scores if i not in views['view', 1]), key=lambda i: -scores[i])
        self.assertEqual(sorted(views['candidate', 1]), sorted(unused[:2]), 'candidates are the top-B by quality')


class FakeModel:
    """Generates fixed tokens through the logits processors exactly as transformers does (raw logits first)."""
    def __init__(self, logits, tokens):
        self.logits, self.tokens = logits, tokens

    def generate(self, **kwargs):
        ids = torch.zeros((len(self.tokens), 0), dtype=torch.long)
        for step in range(self.tokens.shape[1]):
            for processor in kwargs['logits_processor']:
                processor(ids, self.logits[:, step].clone())
            ids = torch.cat([ids, self.tokens[:, step:step + 1]], dim=1)
        return ids


class TokenProbabilityTest(unittest.TestCase):
    def test_mean_probability_of_sampled_non_special_tokens(self):
        torch.manual_seed(0)
        logits = torch.randn(2, 3, 5)
        tokens = torch.tensor([[1, 2, 0], [3, 0, 0]])  # 0 = special (eos/pad)
        model = FakeModel(logits, tokens)
        agent = object.__new__(DAMAgentPanoptic)
        agent.dam_model = mock.Mock(model=model, tokenizer=mock.Mock(all_special_ids=[0]))

        def query(pairs, auto_batch, **kwargs):
            self.assertFalse(auto_batch)
            model.generate(logits_processor=None)
            return [['first'], ['second']]
        agent.query_multi_image_multi_mask = query
        texts, probabilities = agent.query_multi_image_multi_mask_scored([(None, [1]), (None, [2])])
        softmax = torch.softmax(logits, dim=-1)
        expected = [float((softmax[0, 0, 1] + softmax[0, 1, 2]) / 2), float(softmax[1, 0, 3])]
        self.assertEqual(texts, [['first'], ['second']])
        for value, reference in zip([p[0] for p in probabilities], expected):
            self.assertAlmostEqual(value, reference, places=6)
            self.assertTrue(0.0 <= value <= 1.0)
        self.assertNotIn('generate', model.__dict__, 'the wrapper is removed after the call')


class BundleDeliveryTest(unittest.TestCase):
    def test_whole_assignment_reaches_grounding_as_one_bundle(self):
        obj = orchestrator()
        obj.config.uq.enabled = True
        obj.object_labels = {1: 11, 2: 12}
        obj._frame_store.retain(10, {1: frame(1), 2: frame(2)}, {1, 2}, float('inf'))
        obj._process_assignment_result(AssignmentResult(10, [
            SelectedGroup(1, [1, 2], quality={1: 0.5, 2: 0.4}),
            SelectedGroup(2, [1], role='candidate', quality={1: 0.7})]))
        bundle = obj.query_group_queue.get_nowait()
        self.assertIsInstance(bundle, UQPromptBundle)
        self.assertEqual([r.frame_id for r in bundle.views], [1])
        self.assertEqual([[t.id for t in r.tracks] for r in bundle.candidates], [[1]])
        self.assertEqual(bundle.quality[1, 2], 0.7)
        self.assertEqual(obj.prompted_track_ids, {1, 2})
        self.assertTrue(obj.query_group_queue.empty())


class ShutdownTest(unittest.TestCase):
    def test_a_worker_still_busy_after_the_join_timeout_is_reported(self):
        from daaam.grounding.services import GroundingService
        service = object.__new__(GroundingService)
        service._running, service.stop_event, service._join_timeout, service.logger = True, None, 0.0, mock.Mock()
        busy, done = mock.Mock(pid=1, is_alive=mock.Mock(return_value=True)), mock.Mock(is_alive=mock.Mock(return_value=False))
        busy.name, done.name = 'GroundingWorker-0', 'GroundingWorker-1'
        service.workers = [busy, done]
        with mock.patch('os.kill'):
            service.stop()
        self.assertEqual(service.interrupted_workers, ['GroundingWorker-0'], 'its queued groups are lost: the run is incomplete')
        busy.terminate.assert_called_once()

    def test_default_shutdown_wait_covers_a_full_jellyhold_run(self):
        self.assertGreaterEqual(UQConfig().shutdown_timeout_s, 3600)


class GroundingLoopTest(unittest.TestCase):
    def test_refines_uncertain_object_and_fuses(self):
        os.environ.pop('OPENAI_API_KEY', None)
        item = object.__new__(DAMGroundingWorkerMultiImage)
        item.worker_id, item.worker_logger = 'test', mock.Mock()
        output_dir = tempfile.TemporaryDirectory(); self.addCleanup(output_dir.cleanup)
        item.config = {'uq': dict(UQConfig(enabled=True, tau=-5.0).__dict__), 'enable_selectframe_clip_features': False,
                       'save_description_masks': True, 'output_dir': output_dir.name}
        item.compute_full_image_description, item.sentence_embedding_handler = False, None
        # orthogonal captions never reduce u (s = gamma^2 + lam > 1), so object 1 uses the whole budget
        vectors = {text: np.eye(6)[i] for i, text in enumerate(['mug', 'cup', 'bowl', 'red mug', 'tall mug', 'chair'])}
        item.clip_handler = mock.Mock(extract_text_features=lambda texts, batch_size: np.asarray([vectors[t] for t in texts], float))
        answers = {(1, 1): 'mug', (1, 2): 'cup', (1, 3): 'bowl', (1, 4): 'red mug', (2, 1): 'chair', (2, 2): 'chair', (2, 3): 'chair'}
        item._uq_describe = lambda requests: [(answers[track.id, record.frame_id], 0.9) for record, track in requests]
        mask = np.ones((8, 8), bool)
        track = lambda i: Track.from_mask(i, mask, np.asarray([0, 0, 8, 8]))
        record = lambda frame, ids: PromptRecord(np.full((8, 8, 3), frame, np.uint8), [track(i) for i in ids], {1: 11, 2: 12}, frame, frame / 10)
        bundle = UQPromptBundle([record(1, [1, 2]), record(2, [1, 2]), record(3, [1, 2])], [record(4, [1]), record(5, [1])],
                                {(1, 1): .5, (1, 2): .5, (1, 3): .5, (1, 4): .9, (1, 5): .1, (2, 1): .5, (2, 2): .5, (2, 3): .5})
        answers[1, 5] = 'tall mug'
        output = item._process_uq_batch([bundle])
        rows = {r.data['track_id']: r.data for r in output if isinstance(r, UQObjectRecord)}
        corrections = {c.semantic_id: c for c in output if not isinstance(c, UQObjectRecord)}
        self.assertEqual(rows[2]['initial_above_tau'], False)
        self.assertEqual(rows[2]['stop_reason'], 'below_tau')
        self.assertTrue(rows[1]['initial_above_tau'])
        self.assertEqual([v['frame_id'] for v in rows[1]['views'] if v['stage'] == 'refine'], [4, 5], 'highest q first, budget 2')
        self.assertEqual(rows[1]['stop_reason'], 'budget')
        self.assertEqual(rows[1]['fusion_status'], 'fallback_no_llm')
        self.assertIn(corrections[11].semantic_label, {'mug', 'cup', 'bowl', 'red mug', 'tall mug'})
        self.assertEqual(corrections[11].description_frame_id, 4)
        self.assertEqual(corrections[11].description_mask, 'description_masks/11_4.png', 'the written-back view keeps its exact DAM mask')
        import cv2
        self.assertTrue((cv2.imread(str(Path(output_dir.name) / corrections[11].description_mask), cv2.IMREAD_GRAYSCALE) == 255).all())
        self.assertNotIn('embedding', rows[1]['views'][0])
        for view in rows[1]['views']:
            self.assertEqual(view['image_path'], 'uq_views/frames/%d.jpg' % view['frame_id'], 'every described view keeps its frame')
            self.assertEqual(view['mask_path'], 'uq_views/masks/11_%d.png' % view['frame_id'], 'and its mask')
            pixels = cv2.imread(str(Path(output_dir.name) / view['image_path']))
            self.assertLessEqual(np.abs(pixels.astype(int) - view['frame_id']).max(), 2, 'the exact frame that was described')
            self.assertTrue((cv2.imread(str(Path(output_dir.name) / view['mask_path']), cv2.IMREAD_GRAYSCALE) == 255).all())


if __name__ == '__main__':
    unittest.main()
