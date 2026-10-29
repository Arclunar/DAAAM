"""Mean token probability p of DAM captions (UQ-DAAAM gamma), without modifying the installed DAM package."""
import torch


class TokenProbabilityRecorder:
    """Logits processor that stores softmax(raw logits)[sampled token] per step; only one (batch, vocab) step is kept."""

    def __init__(self):
        self.previous = None
        self.steps = []

    def __call__(self, input_ids, scores):
        if self.previous is not None:
            self.steps.append(self.previous.gather(1, input_ids[:, -1:].to(self.previous.device)).squeeze(1))
        self.previous = torch.softmax(scores.float(), dim=-1)
        return scores

    def mean_probabilities(self, sequences, special_ids):
        if self.previous is None:
            return [None] * len(sequences)
        steps = self.steps + [self.previous.gather(1, sequences[:, -1:].to(self.previous.device)).squeeze(1)]
        probabilities = torch.stack(steps, dim=1).cpu()
        tokens = sequences[:, -probabilities.shape[1]:].cpu()  # generated tokens are the last columns
        keep = ~torch.isin(tokens, special_ids)
        counts = keep.sum(dim=1)
        means = (probabilities * keep).sum(dim=1) / counts.clamp(min=1)
        return [float(mean) if count else None for mean, count in zip(means, counts)]


def scored(dam_model, call):
    """(call(), probabilities of every caption generated inside it, in generation order).

    dam_model is a dam.DescribeAnythingModel; its model.generate is wrapped for this call only and gets a logits processor
    that runs before the temperature/top-p warpers, i.e. it reads the raw next-token distribution.
    """
    from transformers import LogitsProcessorList

    model = dam_model.model
    special = torch.tensor(sorted(dam_model.tokenizer.all_special_ids))
    flat = []
    original = model.generate

    def generate(*args, **kwargs):
        recorder = TokenProbabilityRecorder()
        kwargs['logits_processor'] = LogitsProcessorList(list(kwargs.get('logits_processor') or []) + [recorder])
        sequences = original(*args, **kwargs)
        flat.extend(recorder.mean_probabilities(sequences, special))
        return sequences

    model.generate = generate
    try:
        result = call()
    finally:
        del model.generate
    return result, flat
