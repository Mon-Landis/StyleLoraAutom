from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4


@dataclass(frozen=True)
class JobEvent:
    epoch: str
    sequence: int
    job_id: int
    image_id: int | None
    kind: str
    counts: object
    reason: str | None = None


class EventBus:
    def __init__(self, *, epoch=None, capacity=256):
        self.epoch, self.sequence, self.listeners = epoch or uuid4().hex, 0, []
        self.history = deque(maxlen=capacity)
        self.callback_failures = 0

    def subscribe(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def emit(self, job_id, kind, *, image_id=None, counts=None, reason=None):
        self.sequence += 1
        event = JobEvent(self.epoch, self.sequence, job_id, image_id, kind,
                         MappingProxyType(dict(counts or {})), reason)
        self.history.append(event)
        for listener in tuple(self.listeners):
            try:
                listener(event)
            except Exception:
                self.callback_failures += 1
        return event

    def replay(self, epoch, sequence):
        if (epoch != self.epoch or sequence > self.sequence or
                (self.history and sequence < self.history[0].sequence-1)):
            return 'resync_required', ()
        return 'ok', tuple(event for event in self.history if event.sequence > sequence)
