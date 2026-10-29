"""UQ-DAAAM (arXiv:2606.08277): object-level caption uncertainty, active view refinement and caption fusion.

uncertainty: log-volume u of probability-weighted caption embeddings (numpy only).
refinement:  Algorithm 1 over plain data (initial views, refinement by view quality, stop rules) and uq_summary.json counts.
scoring:     mean token probability of DAM captions through a logits processor (does not modify the dam package).
fusion:      LLM caption fusion with strict JSON validation and a deterministic fallback.
Used by the grounding worker only when uq.enabled; nothing here runs in the upstream path.
"""
