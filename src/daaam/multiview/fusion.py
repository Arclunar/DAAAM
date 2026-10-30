"""Fusion of an object's descriptions (L). A view is a dict with stage ('initial' | 'refine'), frame_id, text, p, gamma, q_prime and,
for the viewtext mode, condition_text. Every mode ends in a valid result: LLM failures fall back to none_best."""
import json
import re

RULES = """Rules:
1. Use only information stated in the descriptions; never add new facts.
2. Views that are closer, show the object larger, sharper, uncut and with higher model confidence are more trustworthy.
3. The initial description was flagged as possibly unreliable: when it conflicts with a refinement view in better conditions, follow the refinement view and record the dropped content and why.
4. When refinement views contradict each other under similar conditions, keep the possibilities (e.g. "a remote control or a hairbrush").
5. If no category can be determined, set category to "uncertain"."""

SCHEMA = """Answer with JSON only: {"category": str, "description": str (1 to 3 sentences), "confidence": "high" | "medium" | "low", "facts_used": [str], "facts_dropped": [{"fact": str, "source": str, "reason": str}], "rationale": str (one sentence)}"""

PROMPTS = {
	'naive_llm': 'Merge these descriptions of the same physical object into one description.',
	'constrained_llm': 'You fuse several descriptions of the same physical object into one description.\n' + RULES,
	'constrained_llm_viewtext': 'You fuse several descriptions of the same physical object into one description. Each description '
		'comes with the conditions under which its view was observed.\n' + RULES,
}


def prompt(mode, views):
	lines = []
	for number, view in enumerate(views, 1):
		head = '%d. [%s view, frame %s]' % (number, view['stage'], view['frame_id'])
		lines.append(head + ' ' + view['text'])
		if mode == 'constrained_llm_viewtext' and view.get('condition_text'):
			lines.append('   Conditions: ' + view['condition_text'])
	return PROMPTS[mode] + '\n\nDescriptions:\n' + '\n'.join(lines) + '\n\n' + SCHEMA


def parse(text):
	match = re.search(r'\{.*\}', text or '', re.S)
	if match is None:
		raise ValueError('no JSON object in fusion reply')
	data = json.loads(match.group(0))
	for key in ('category', 'description', 'rationale'):
		if not isinstance(data.get(key), str) or not data[key].strip():
			raise ValueError(key + ' must be a non-empty string')
	if len([s for s in re.split(r'(?<=[.!?])\s+', data['description'].strip()) if s]) > 3:
		raise ValueError('description must have 1 to 3 sentences')
	if data.get('confidence') not in ('high', 'medium', 'low'):
		raise ValueError('confidence must be high, medium or low')
	if not isinstance(data.get('facts_used'), list) or not all(isinstance(f, str) for f in data['facts_used']):
		raise ValueError('facts_used must be a list of strings')
	dropped = data.get('facts_dropped')
	if not isinstance(dropped, list) or not all(isinstance(d, dict) and all(isinstance(d.get(k), str) for k in ('fact', 'source', 'reason'))
			for d in dropped):
		raise ValueError('facts_dropped must be a list of {fact, source, reason}')
	return dict(category=data['category'].strip(), description=data['description'].strip(), confidence=data['confidence'],
		facts_used=data['facts_used'], facts_dropped=dropped, rationale=data['rationale'].strip())


def best(views):
	"""none_best: the view with the largest q' / gamma (gamma = 1 without token probabilities)."""
	return max(views, key=lambda v: (v.get('q_prime') or 0.0) / (v.get('gamma') or 1.0))


def fuse(mode, views, complete):
	"""complete(prompt) -> reply, or None without an LLM. Returns description, chosen frame and fusion metadata."""
	chosen = best(views)
	fallback = dict(description=chosen['text'], description_frame_id=chosen['frame_id'], category=None, confidence=None,
		facts_used=[], facts_dropped=[], rationale=None)
	if mode == 'none_best' or len(views) == 1:
		return dict(fallback, fusion_status='none_best' if mode == 'none_best' else 'single_view')
	if complete is None:
		return dict(fallback, fusion_status='fallback_no_llm')
	errors = []
	for _ in range(2):  # one retry on an invalid reply
		try:
			reply = complete(prompt(mode, views))
		except Exception as error:  # network/API failures must not lose the description
			errors.append(str(error)[:300])
			continue
		try:
			return dict(parse(reply), description_frame_id=chosen['frame_id'], fusion_status='fused')
		except (ValueError, TypeError) as error:
			errors.append('%s: %s' % (error, str(reply)[:500]))
	return dict(fallback, fusion_status='fallback_none_best', fusion_errors=errors)
