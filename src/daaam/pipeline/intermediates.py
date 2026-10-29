"""Opt-in export of the front-end products a viewer needs to replay what happened in each frame (save_intermediates).

The frame loop only enqueues references; a background thread encodes the label PNGs and JSON lines, so the pipeline
does not wait for the disk. Layout under <output>/intermediates/:
  frames.jsonl   one row per frame: detections and every track (Track ID -> semantic ID, box, depth check)
  labels/        the uint16 label image of each frame exactly as sent to Hydra (pixel = semantic ID, 0 = none)
  events.jsonl   queries, selections, prompts to DAM, descriptions and automatic 'unknown' changes, with the frame
                 count at which they happened
"""
import json
import queue
import threading
import time
from pathlib import Path

import cv2


class IntermediateRecorder:
	def __init__(self, output_dir, logger):
		self.root = Path(output_dir) / "intermediates"
		(self.root / "labels").mkdir(parents=True, exist_ok=True)
		self.logger = logger
		self.errors = 0
		self._queue = queue.Queue()
		self._frames = (self.root / "frames.jsonl").open("a", encoding="utf-8")
		self._events = (self.root / "events.jsonl").open("a", encoding="utf-8")
		self._thread = threading.Thread(target=self._run, name="intermediates", daemon=True)
		self._thread.start()

	def frame(self, row, label_image):
		"""row: JSON-ready frame record (needs frame_id); label_image: uint16 HxW, not modified afterwards."""
		self._queue.put(("frame", row, label_image))

	def event(self, kind, frame_id, **data):
		self._queue.put(("event", dict(kind=kind, frame_id=int(frame_id), wall_time=time.time(), **data), None))

	def _run(self):
		while True:
			item = self._queue.get()
			if item is None:
				return
			kind, row, image = item
			try:
				if kind == "frame":
					name = "labels/%06d.png" % row["frame_id"]
					if not cv2.imwrite(str(self.root / name), image):
						raise OSError("label image was not written: " + name)
					self._frames.write(json.dumps(dict(row, label_path=name)) + "\n")
					self._frames.flush()
				else:
					self._events.write(json.dumps(row) + "\n")
					self._events.flush()
			except Exception as error:  # an export failure must not stop mapping
				self.errors += 1
				self.logger.error(f"Intermediate export failed: {error}")

	def close(self):
		"""Write everything still queued, then close the files."""
		if self._thread.is_alive():
			self._queue.put(None)
			self._thread.join()
		self._frames.close()
		self._events.close()
		if self.errors:
			self.logger.error(f"{self.errors} intermediate records were not written")
