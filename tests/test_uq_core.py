"""UQ-DAAAM core (daaam.uq): log-volume uncertainty, refinement stop rules, fusion parsing and fallbacks.

  python -m unittest discover -s tests -p 'test_uq*.py'    (in the daaam-ws container, from this repository)
"""
import json
import math
import unittest

import numpy as np

from daaam.uq.uncertainty import Uncertainty, log_volume
from daaam.uq.refinement import ObjectUQ, threshold
from daaam.uq import fusion


def accumulate(embeddings, probabilities, lam=1e-8):
    state = Uncertainty(1.0, lam)
    steps = [state.add(e, p) for e, p in zip(embeddings, probabilities)]
    return state, steps


class UncertaintyTest(unittest.TestCase):
    def test_incremental_matches_slogdet_and_thm_4_4_4_5(self):
        rng = np.random.default_rng(3)
        for trial in range(50):
            r, d = rng.integers(2, 6), 16
            embeddings = rng.normal(size=(r, d)) + (rng.normal(size=d) * 3 if trial % 2 else 0)
            probabilities = rng.uniform(0.2, 1.0, size=r)
            lam = 10.0 ** rng.integers(-8, -1)
            state, steps = accumulate(embeddings, probabilities, lam)
            self.assertAlmostEqual(state.value, log_volume(embeddings, probabilities, lam=lam), places=6)
            for k in range(1, r):
                before = log_volume(embeddings[:k], probabilities[:k], lam=lam)
                after = log_volume(embeddings[:k + 1], probabilities[:k + 1], lam=lam)
                self.assertAlmostEqual(math.log(steps[k]['s']), after - before, places=6)
                self.assertEqual(steps[k]['decreases'], after < before)

    def test_reference_cases(self):
        same = np.tile([1.0, 2.0, 0.0], (3, 1))
        state, steps = accumulate(same, [1, 1, 1])
        self.assertAlmostEqual(state.value, math.log(3 + 1e-8) + 2 * math.log(1e-8), places=4)
        self.assertTrue(all(step['s'] > 0 for step in steps), 'near-duplicate captions stay numerically positive')
        orthogonal, _ = accumulate(np.eye(3), [1, 1, 1])
        self.assertAlmostEqual(orthogonal.value, 0.0, places=6)
        uncertain, _ = accumulate(np.eye(3), [0.5, 0.5, 0.5])
        self.assertGreater(uncertain.value, orthogonal.value)

    def test_rejects_invalid_probability_and_zero_embedding(self):
        with self.assertRaises(ValueError):
            Uncertainty().add([1, 0], 1.2)
        with self.assertRaises(ValueError):
            Uncertainty().add([0, 0], 0.5)


class RefinementTest(unittest.TestCase):
    def object(self, candidates, texts=('a', 'b')):
        item = ObjectUQ(candidates)
        for frame, vector in zip((1, 2), np.eye(4)[:2]):
            item.add_view('initial', frame, 0.5, 1.0, texts[frame - 1], vector)
        return item

    def test_quality_order_budget_and_no_reuse(self):
        item = self.object({1: 0.99, 7: 0.4, 5: 0.9, 6: 0.9})
        self.assertNotIn(1, item.candidates, 'an initial frame is never a candidate')
        self.assertEqual(item.next_view(-5.0, 2), (5, 0.9))
        item.add_view('refine', 5, 0.9, 1.0, 'c', np.eye(4)[2])
        self.assertEqual(item.next_view(-5.0, 2), (6, 0.9))
        item.add_view('refine', 6, 0.9, 1.0, 'd', np.eye(4)[3])
        self.assertIsNone(item.next_view(-5.0, 2))
        self.assertEqual(item.stop_reason, 'budget')
        with self.assertRaises(ValueError):
            item.add_view('refine', 6, 0.9, 1.0, 'd', np.eye(4)[3])

    def test_stop_below_tau_and_without_candidates(self):
        item = self.object({})
        self.assertIsNone(item.next_view(-5.0, 2))
        self.assertEqual(item.stop_reason, 'no_candidates')
        self.assertIsNone(item.next_view(item.value, 2))
        self.assertEqual(item.stop_reason, 'below_tau')

    def test_threshold_modes(self):
        self.assertEqual(threshold('fixed', -0.25, 0.8, []), -0.25)
        self.assertAlmostEqual(threshold('quantile', 0, 0.8, [0, 1, 2, 3, 4]), 3.2)
        with self.assertRaises(ValueError):
            threshold('quantile', 0, 0.8, [])


class FusionTest(unittest.TestCase):
    views = [dict(stage='initial', frame_id=1, text='a red mug', u=-1.0),
             dict(stage='initial', frame_id=2, text='a red cup', u=-0.5),
             dict(stage='refine', frame_id=9, text='a red ceramic mug', u=-0.9),
             dict(stage='refine', frame_id=8, text='a mug', u=-0.7)]

    def test_valid_reply_and_prompt_order(self):
        seen = []
        reply = json.dumps({'fused_caption': 'A red ceramic mug.', 'facts_used': ['red', 'ceramic'], 'facts_omitted': []})
        result = fusion.fuse(self.views, lambda text: seen.append(text) or 'ok ' + reply)
        self.assertEqual(result['fusion_status'], 'fused')
        self.assertEqual(result['fused_caption'], 'A red ceramic mug.')
        self.assertLess(seen[0].index('a red cup'), seen[0].index('a red ceramic mug'))

    def test_fallbacks_never_invent_text(self):
        self.assertEqual(fusion.fuse(self.views)['fusion_status'], 'fallback_no_llm')
        self.assertEqual(fusion.fuse(self.views)['fused_caption'], 'a red ceramic mug')
        bad = fusion.fuse(self.views, lambda text: '{"fused_caption": "One. Two. Three. Four.", "facts_used": [], "facts_omitted": []}')
        self.assertEqual(bad['fusion_status'], 'fallback_invalid')
        self.assertEqual(fusion.fuse(self.views, lambda text: '{"fused_caption": ""}')['fusion_status'], 'fallback_invalid')

        def offline(text):
            raise ConnectionError('offline')
        self.assertEqual(fusion.fuse(self.views, offline)['fusion_status'], 'fallback_error')
        self.assertEqual(fusion.fuse(self.views[:1])['fusion_status'], 'single_view')


if __name__ == '__main__':
    unittest.main()
