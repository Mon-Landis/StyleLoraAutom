"""Thread-safe, bounded summary stream; no database reader is retained."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import threading
import time
from uuid import uuid4
from .contracts import Failure, canonical


class EventBridge:
    def __init__(self, *, capacity=10000, byte_limit=8*1024*1024, clock=time.monotonic):
        self.epoch, self.sequence = str(uuid4()), 0
        self.history, self.listeners = deque(), {}
        self.capacity, self.byte_limit, self.clock = capacity, byte_limit, clock
        self.bytes, self.lock, self.progress = 0, threading.RLock(), {}

    def publish(self, kind, *, project_id=None, resource=None, payload=None, version=None):
        with self.lock:
            now = self.clock()
            key = (project_id, str(resource), kind)
            if kind.endswith('.progress') and now - self.progress.get(key, -1e10) < .25:
                return
            self.progress[key] = now
            self.sequence += 1
            event = dict(schema_version=1, epoch=self.epoch, sequence=self.sequence,
                         occurred_at=datetime.now(timezone.utc).isoformat(), project_id=project_id,
                         resource=resource, version=version, payload=payload or {})
            size = len(canonical(event))
            if size > 64*1024:
                event['payload'] = {'invalidate_scope': True}
                size = len(canonical(event))
            self.history.append((now, kind, event, size))
            self.bytes += size
            while self.history and (len(self.history) > self.capacity or self.bytes > self.byte_limit or now-self.history[0][0] > 600):
                self.bytes -= self.history.popleft()[3]
            for loop, queue, matches in tuple(self.listeners.values()):
                if matches(kind, event):
                    def deliver(q=queue, item=(kind, event)):
                        if q.full():
                            while not q.empty():
                                q.get_nowait()
                            q.put_nowait(('stream.resync_required', {'reason': 'slow_consumer'}))
                        else:
                            q.put_nowait(item)
                    loop.call_soon_threadsafe(deliver)

    def connect(self, after=None, *, topics=(), project_id=None, resource_id=None):
        def matches(kind, event):
            return ((not topics or kind.split('.')[0] in topics or kind in topics)
                    and (project_id is None or event['project_id'] == project_id)
                    and (resource_id is None or str((event['resource'] or {}).get('id')) == resource_id))
        with self.lock:
            if len(self.listeners) >= 4:
                raise Failure(429, 'request:rate_limited')
            replay = []
            if after:
                try:
                    epoch, raw = after.rsplit(':', 1)
                    if not raw.isdigit() or not epoch:
                        raise ValueError
                    seq = int(raw)
                except ValueError:
                    raise Failure(400, 'event:invalid_id') from None
                expired = bool(self.history and (seq < self.history[0][2]['sequence']-1 or self.clock()-self.history[0][0] > 600))
                if epoch != self.epoch or expired or seq > self.sequence:
                    replay.append(('stream.resync_required', {'reason': 'epoch_changed' if epoch != self.epoch else 'buffer_expired'}))
                else:
                    replay = [(k, e) for _, k, e, _ in self.history if e['sequence'] > seq and matches(k, e)]
            key, queue = str(uuid4()), asyncio.Queue(256)
            self.listeners[key] = (asyncio.get_running_loop(), queue, matches)
            return key, queue, replay

    def disconnect(self, key):
        with self.lock:
            self.listeners.pop(key, None)

    def attach_jobs(self, bus, project_id):
        def receive(event):
            self.publish(event.kind, project_id=project_id, resource={'kind': 'job', 'id': event.job_id},
                         payload={'job_id': event.job_id, 'image_id': event.image_id,
                                  'counts': dict(event.counts), 'reason_code': event.reason})
        return bus.subscribe(receive)
