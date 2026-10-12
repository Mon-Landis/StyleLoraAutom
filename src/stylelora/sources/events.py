import asyncio
from dataclasses import dataclass
from collections.abc import Mapping
from stylelora.backends.types import Immutable, AccountStatus


@dataclass(frozen=True)
class ProgressEvent(Immutable):
    source_id: int
    cycle_id: str | None
    sequence: int
    status: str
    counts: Mapping
    current_item_id: int | None
    phase: str
    reason_code: str | None = None
    estimated_anlas: int | None = None
    account: AccountStatus | None = None


class EventBus:
    """Bounded summary delivery. Reconnect callers must fetch a DB snapshot."""
    def __init__(self):
        self.latest = {}
        self.important = {}
        self.subscribers = set()

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    async def publish(self, event):
        self.latest[event.source_id] = event
        if event.reason_code is not None or event.status in ('paused', 'canceled', 'done'):
            self.important[event.source_id] = event
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
