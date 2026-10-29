"""UQ-DAAAM active view refinement (Algorithm 1, Thm 4.4-4.8) over plain data."""
import numpy as np

from .uncertainty import Uncertainty


def threshold(mode, tau, quantile, seen):
    """fixed: tau as given. quantile: the given quantile of the initial uncertainties seen so far (causal online estimate)."""
    if mode == 'fixed':
        return float(tau)
    if mode != 'quantile':
        raise ValueError('tau mode must be fixed or quantile')
    if not seen:
        raise ValueError('quantile threshold needs at least one initial uncertainty')
    return float(np.quantile(np.asarray(seen, dtype=np.float64), quantile))


class ObjectUQ:
    """Views of one object: initial r captions, then refinement in descending quality until u <= tau, budget or no candidates.

    candidates: {frame_id: q}; frames already used for this object are never queried again.
    """

    def __init__(self, candidates, alpha=1.0, lam=1e-8):
        self.uncertainty = Uncertainty(alpha, lam)
        self.candidates = dict(candidates)
        self.views = []
        self.stop_reason = None

    @property
    def value(self):
        return self.uncertainty.value

    def add_view(self, stage, frame_id, quality, probability, text, embedding, **provenance):
        if any(view['frame_id'] == frame_id for view in self.views):
            raise ValueError('frame ' + str(frame_id) + ' was already used for this object')
        step = self.uncertainty.add(embedding, probability)
        self.candidates.pop(frame_id, None)
        view = dict(provenance, stage=stage, frame_id=frame_id, q=float(quality), p=float(probability), text=text,
                    gamma=step['gamma'], eta=step['eta'], s=step['s'], u=step['u_after'], decreased=step['decreases'])
        self.views.append(view)
        return view

    def next_view(self, tau, budget):
        """The next (frame_id, q) to describe, or None with stop_reason set."""
        if self.value <= tau:
            self.stop_reason = 'below_tau'
        elif sum(view['stage'] == 'refine' for view in self.views) >= budget:
            self.stop_reason = 'budget'
        elif not self.candidates:
            self.stop_reason = 'no_candidates'
        else:
            # proxy rule (Prop 4.7 / Thm 4.8): highest quality first; earlier frame on ties
            return max(self.candidates.items(), key=lambda item: (item[1], -item[0]))
        return None


def summarize(rows):
    """uq_summary.json counts; URR = share of initially-above-tau objects whose final u <= tau."""
    participants = [row for row in rows if row['uq_status'] == 'ok']
    above = [row for row in participants if row['initial_above_tau']]
    stages = [view['stage'] for row in rows for view in row['views']]
    fusion = {}
    for row in rows:
        fusion[row['fusion_status']] = fusion.get(row['fusion_status'], 0) + 1

    def distribution(key):
        values = [row[key] for row in participants]
        return {'count': len(values), 'mean': float(np.mean(values)), 'median': float(np.median(values)),
                'min': float(np.min(values)), 'max': float(np.max(values))} if values else None
    return {
        'uq_objects': len(rows),
        'uq_participants': len(participants),
        'uq_status': {status: sum(row['uq_status'] == status for row in rows) for status in sorted({row['uq_status'] for row in rows})},
        'initial_above_tau': len(above),
        'refined_objects': sum(any(view['stage'] == 'refine' for view in row['views']) for row in rows),
        'urr': sum(row['u_final'] <= row['tau'] for row in above) / len(above) if above else None,
        'dam_descriptions': {'initial': stages.count('initial'), 'refine': stages.count('refine')},
        'fusion_status': fusion,
        'tau_values': sorted({row['tau'] for row in participants}),
        'u_initial': distribution('u_initial'),
        'u_final': distribution('u_final'),
    }


VIEW_KEYS = ('stage', 'frame_id', 'quality', 'probability', 'text', 'embedding')


def add(state, view):
    """Add a described view dict (VIEW_KEYS + provenance) to an ObjectUQ."""
    state.add_view(*(view[key] for key in VIEW_KEYS), **{key: value for key, value in view.items() if key not in VIEW_KEYS})


def refine_objects(objects, describe, uq, seen):
    """Algorithm 1 for many objects, one batched describe call per round.

    objects: {key: {'initial': [frame_id], 'candidates': {frame_id: q}}}; describe(stage, [(key, frame_id)]) returns one view
    dict per request. Sets raw/status/state/skipped/u_initial on each object, appends initial u to seen and returns tau.
    """
    initial = [(key, frame) for key, item in objects.items() for frame in item['initial']]
    for (key, _), view in zip(initial, describe('initial', initial)):
        objects[key].setdefault('raw', []).append(view)
    for item in objects.values():
        item['skipped'] = []
        if any(view['probability'] is None for view in item['raw']):
            item['status'] = 'missing_token_probability'
            continue
        item['status'] = 'ok' if len(item['raw']) >= uq['min_views'] else 'insufficient_views'
        item['state'] = ObjectUQ(item['candidates'], uq['alpha'], uq['lam'])
        for view in item['raw']:
            add(item['state'], view)
        if item['status'] == 'ok':
            item['u_initial'] = item['state'].value
            seen.append(item['u_initial'])
    tau = threshold(uq['tau_mode'], uq['tau'], uq['tau_quantile'], seen) if seen or uq['tau_mode'] == 'fixed' else None
    active = [key for key, item in objects.items() if item['status'] == 'ok']
    while active:
        keys = []
        for key in active:
            choice = objects[key]['state'].next_view(tau, uq['refine_budget'])
            if choice is not None:
                keys.append((key, choice[0]))
        if not keys:
            break
        for (key, frame), view in zip(keys, describe('refine', keys)):
            state = objects[key]['state']
            if view['probability'] is None:
                state.candidates.pop(frame, None)  # cannot enter u; never fabricate p = 1
                objects[key]['skipped'].append({'frame_id': frame, 'reason': 'missing_token_probability', 'text': view['text']})
                continue
            add(state, view)
    return tau


def finish_objects(objects, tau, uq, complete):
    """Fuse every object and build its uq_objects.jsonl row (without identity keys); the row's description_frame_id is the
    highest-q view, the one written back as the description source."""
    from concurrent.futures import ThreadPoolExecutor
    from . import fusion

    def fuse(item):
        if item['status'] == 'missing_token_probability':
            return {'fused_caption': max(item['raw'], key=lambda view: view['quality'])['text'], 'fusion_status': 'not_applicable'}
        return fusion.fuse(item['state'].views, complete)
    with ThreadPoolExecutor(max_workers=8) as pool:
        fused = dict(zip(objects, pool.map(fuse, objects.values())))
    rows = {}
    for key, item in objects.items():
        views = item['state'].views if 'state' in item else [
            dict({k: v for k, v in view.items() if k not in VIEW_KEYS}, stage=view['stage'], frame_id=view['frame_id'],
                 q=view['quality'], p=view['probability'], text=view['text']) for view in item['raw']]
        best = max(views, key=lambda view: (view['q'], -view['frame_id']))
        ok = item['status'] == 'ok'
        row = dict(uq_status=item['status'], views=views, skipped=item['skipped'], u_initial=item.get('u_initial'),
                   u_final=item['state'].value if ok else None, tau=tau, tau_mode=uq['tau_mode'],
                   initial_above_tau=bool(ok and item['u_initial'] > tau), stop_reason=item['state'].stop_reason if ok else None,
                   final_caption=fused[key]['fused_caption'] or 'unknown', description_frame_id=best['frame_id'], **fused[key],
                   text_encoder=uq['text_encoder'], caption_temperature=uq['caption_temperature'], fusion_model=uq['fusion_model'],
                   alpha=uq['alpha'], lam=uq['lam'], refine_budget=uq['refine_budget'])
        row.pop('fused_caption')
        rows[key] = row
    return rows
