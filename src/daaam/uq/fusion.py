"""UQ-DAAAM caption fusion: LLM prompt, strict JSON validation and a deterministic fallback that never invents text."""
import json
import re

INSTRUCTIONS = """You fuse several captions of the same physical object into one description.
Captions are listed in acquisition order: initial views first, then refinement views taken because the initial ones disagreed.
Rules:
- Use only facts stated in at least one caption; never add new facts.
- On conflicts prefer refinement captions over initial captions.
- Keep unresolved disagreements among initial captions as "possibly A or B".
- fused_caption has 1 to 3 sentences.
Answer with JSON only: {"fused_caption": str, "facts_used": [str], "facts_omitted": [str]}"""


def ordered(views):
    return [view for view in views if view['stage'] == 'initial'] + [view for view in views if view['stage'] != 'initial']


def prompt(views):
    lines = ['%d. [%s view, frame %s] %s' % (number, view['stage'], view['frame_id'], view['text'])
             for number, view in enumerate(ordered(views), 1)]
    return INSTRUCTIONS + '\n\nCaptions:\n' + '\n'.join(lines)


def parse(text):
    match = re.search(r'\{.*\}', text, re.S)
    if match is None:
        raise ValueError('no JSON object in fusion reply')
    data = json.loads(match.group(0))
    caption = data.get('fused_caption')
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError('fused_caption must be a non-empty string')
    sentences = [part for part in re.split(r'(?<=[.!?])\s+', caption.strip()) if part]
    if len(sentences) > 3:
        raise ValueError('fused_caption must have 1 to 3 sentences')
    for key in ('facts_used', 'facts_omitted'):
        if not isinstance(data.get(key), list) or not all(isinstance(item, str) for item in data[key]):
            raise ValueError(key + ' must be a list of strings')
    return {'fused_caption': caption.strip(), 'facts_used': data['facts_used'], 'facts_omitted': data['facts_omitted']}


def fallback(views):
    """Last caption at the minimum u reached once the initial set is complete (u is only comparable for >= r views)."""
    initial = sum(view['stage'] == 'initial' for view in views)
    tail = views[max(initial, 1) - 1:]
    best = min(range(len(tail)), key=lambda index: (tail[index]['u'], -index))
    return tail[best]['text']


def fuse(views, complete=None):
    """complete(prompt) -> reply text, or None when no LLM is configured. Returns the caption plus fusion metadata."""
    if len(views) == 1:
        return {'fused_caption': views[0]['text'], 'fusion_status': 'single_view'}
    if complete is None:
        return {'fused_caption': fallback(views), 'fusion_status': 'fallback_no_llm'}
    try:
        reply = complete(prompt(views))
    except Exception as error:  # network/API failures must not lose the object's description
        return {'fused_caption': fallback(views), 'fusion_status': 'fallback_error', 'fusion_error': str(error)[:300]}
    try:
        return dict(parse(reply), fusion_status='fused')
    except (ValueError, TypeError) as error:
        return {'fused_caption': fallback(views), 'fusion_status': 'fallback_invalid', 'fusion_error': str(error)[:300],
                'fusion_reply': str(reply)[:2000]}


def openai_complete(model):
    """complete(prompt) through the OpenAI chat API, or None without OPENAI_API_KEY (callers then use the fallback)."""
    import os
    if not os.environ.get('OPENAI_API_KEY'):
        return None
    from openai import OpenAI
    client = OpenAI(timeout=60.0, max_retries=1)

    def complete(text):
        return client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': text}]).choices[0].message.content
    return complete
