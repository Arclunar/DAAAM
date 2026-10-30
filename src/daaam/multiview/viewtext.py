"""Observation conditions of each described view (K): structured values plus one English sentence for the fusion prompt;
refinement views are described relative to the initial view."""
import math

import numpy as np


def confidence_tier(p):
	if p is None:
		return None
	return 'high' if p >= 0.8 else 'medium' if p >= 0.6 else 'low'


def _angles(direction):
	"""Azimuth around world z and elevation of an object -> camera direction, degrees."""
	x, y, z = direction
	return math.degrees(math.atan2(y, x)), math.degrees(math.asin(max(-1.0, min(1.0, z))))


def condition(view, initial=None):
	"""view and initial: observation rows with optional 'direction', 'distance', 'p'. Returns the structured condition."""
	c = dict(distance_m=view.get('distance'), area_percent=100.0 * view['area_ratio'], exposure_ms=None if view.get('exposure_s') is None
		else 1000.0 * view['exposure_s'], blur_px=view.get('b'), truncated=bool(view['truncated']), confidence=confidence_tier(view.get('p')))
	if initial is not None and view.get('direction') is not None and initial.get('direction') is not None:
		a0, e0 = _angles(initial['direction'])
		a1, e1 = _angles(view['direction'])
		c['azimuth_deg'] = (a1 - a0 + 180.0) % 360.0 - 180.0  # positive: counter-clockwise seen from above, i.e. to the right
		c['elevation_deg'] = e1 - e0
		c['angle_to_initial_deg'] = math.degrees(math.acos(float(np.clip(np.dot(view['direction'], initial['direction']), -1, 1))))
	if initial is not None:
		c['initial_distance_m'] = initial.get('distance')
		c['initial_area_percent'] = 100.0 * initial['area_ratio']
	return c


def sentence(c, reasons=None):
	parts = []
	if 'azimuth_deg' in c:
		side = 'right' if c['azimuth_deg'] > 0 else 'left'
		height = 'higher' if c['elevation_deg'] > 10 else 'lower' if c['elevation_deg'] < -10 else 'at a similar height'
		parts.append('seen from about %.0f° to the %s of the initial view, %s' % (abs(c['azimuth_deg']), side, height))
	if c.get('distance_m') is not None:
		parts.append('distance about %.1f m' % c['distance_m'] + (' (initial %.1f m)' % c['initial_distance_m']
			if c.get('initial_distance_m') is not None else ''))
	parts.append('object fills about %.1f%% of the image' % c['area_percent'] + (' (initial %.1f%%)' % c['initial_area_percent']
		if c.get('initial_area_percent') is not None else ''))
	if c.get('exposure_ms') is not None or c.get('blur_px') is not None:
		parts.append('exposure %s, predicted motion blur %s' % ('%.0f ms' % c['exposure_ms'] if c.get('exposure_ms') is not None else 'unknown',
			'%.1f px' % c['blur_px'] if c.get('blur_px') is not None else 'unknown'))
	parts.append('cut off by the image border' if c['truncated'] else 'fully inside the image')
	if c.get('confidence'):
		parts.append('model confidence %s' % c['confidence'])
	text = '; '.join(parts)
	if reasons:
		text += '; flagged as possibly unreliable: ' + ', '.join(reasons)
	return text
