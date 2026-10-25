"""Assignment-owned RGB frames; callers hold the orchestrator state lock."""


def image_bytes(frames):
    """Count shared NumPy image storage once, including views."""
    owners = {}
    for frame in frames:
        image = frame.rgb_image
        if image is None:
            continue
        while getattr(image, "base", None) is not None:
            image = image.base
        owners[id(image)] = memoryview(image).nbytes
    return sum(owners.values())


class AssignmentFrameStore:
    def __init__(self, max_bytes):
        if max_bytes <= 0:
            raise ValueError("frame cache budget must be positive")
        self.max_bytes = max_bytes
        self.frames = {}
        self.assignments = {}

    def check_budget(self, recent_frames):
        from itertools import chain

        size = image_bytes(chain(self.frames.values(), recent_frames))
        if size > self.max_bytes:
            raise MemoryError(f"RGB frame cache needs {size} bytes; budget is {self.max_bytes}")

    def retain(self, assignment_id, frames, track_ids, deadline):
        if assignment_id in self.assignments:
            raise ValueError(f"duplicate assignment {assignment_id}")
        self.check_budget(frames.values())
        self.frames.update(frames)
        self.assignments[assignment_id] = (set(frames), set(track_ids), deadline)

    def release(self, assignment_id):
        item = self.assignments.pop(assignment_id, None)
        if item is None:
            return set()
        retained_frames = set()
        retained_tracks = set()
        for frame_ids, track_ids, _ in self.assignments.values():
            retained_frames.update(frame_ids)
            retained_tracks.update(track_ids)
        for frame_id in item[0] - retained_frames:
            self.frames.pop(frame_id, None)
        return item[1] - retained_tracks

    def clear(self):
        self.assignments.clear()
        self.frames.clear()
