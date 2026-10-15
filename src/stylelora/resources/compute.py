"""Shared local inference slots and explicit memory reservations."""
import asyncio
from collections import Counter
from dataclasses import dataclass
import math
import time
from weakref import WeakKeyDictionary
from stylelora.llm.types import LlmFailure


@dataclass(frozen=True)
class ComputePolicy:
    gpu_slots: int = 1
    cpu_slots: int = 1
    gpu_budget_bytes: int | None = None
    reserve_bytes: int = 1024**3
    reserve_ratio: float = .2
    wd_max_batch: int = 4

    def __post_init__(self):
        if (any(type(v) is not int or v < 1 for v in (self.gpu_slots, self.cpu_slots, self.wd_max_batch)) or
                self.gpu_budget_bytes is not None and (type(self.gpu_budget_bytes) is not int or self.gpu_budget_bytes < 1) or
                type(self.reserve_bytes) is not int or self.reserve_bytes < 0 or
                type(self.reserve_ratio) not in (int, float) or not math.isfinite(self.reserve_ratio) or not 0 <= self.reserve_ratio < 1):
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')


@dataclass(frozen=True)
class ComputeDemand:
    owner: str
    device_id: str
    backend: str
    model_key: str
    resident_bytes: int
    workspace_bytes: int
    requested_batch: int


class ComputeLease:
    def __init__(self, controller, demand, batch, reserved):
        self.controller, self.demand = controller, demand
        self.provider = 'cpu' if demand.device_id == 'cpu' else 'cuda'
        self.actual_batch, self.reserved_bytes = batch, reserved
        self.released = False

    async def release(self):
        async with self.controller.lock:
            if not self.released:
                self.released = True
                self.controller.slots[self.demand.device_id] -= 1
                self.controller.reserved[self.demand.device_id] -= self.reserved_bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.release()


class ComputeController:
    def __init__(self, policy=None, *, probe=None, clock=time.monotonic, sleep=asyncio.sleep):
        self.policy = policy or ComputePolicy()
        if probe is None:
            from stylelora.vision.device import probe_device
            probe = probe_device
        self.probe, self.clock, self.sleep = probe, clock, sleep
        self.lock = asyncio.Lock()
        self.slots, self.reserved = Counter(), Counter()
        self.residents = {}
        self.revision = 0

    def configure(self, policy):
        if not isinstance(policy, ComputePolicy):
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')
        self.policy = policy
        self.revision += 1
        return self.revision

    def register_resident(self, model_key, device_id, bytes):
        if type(bytes) is not int or bytes < 0:
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')
        old, refs = self.residents.get((device_id, model_key), (bytes, 0))
        self.residents[device_id, model_key] = (max(old, bytes), refs+1)

    def release_resident(self, model_key, device_id):
        key = (device_id, model_key)
        if key in self.residents:
            size, refs = self.residents[key]
            if refs == 1:
                del self.residents[key]
            else:
                self.residents[key] = (size, refs-1)

    async def acquire(self, demand, *, cancel, deadline):
        if (any(type(v) is not int or v < 0 for v in (demand.resident_bytes, demand.workspace_bytes)) or
                type(demand.requested_batch) is not int or demand.requested_batch < 1):
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')
        while True:
            if cancel is not None and cancel.canceled:
                raise LlmFailure('llm:canceled', outcome='not_sent')
            if self.clock() >= deadline:
                raise LlmFailure('resource:timeout', outcome='not_sent')
            async with self.lock:
                policy = self.policy
                cpu = demand.device_id == 'cpu'
                batch, allocation = 1, 0
                if not cpu:
                    device = self.probe()
                    if not device.total_bytes or device.free_bytes is None or not demand.workspace_bytes:
                        raise LlmFailure('resource:insufficient', outcome='not_sent')
                    margin = max(policy.reserve_bytes, int(device.total_bytes * policy.reserve_ratio))
                    limit = policy.gpu_budget_bytes if policy.gpu_budget_bytes is not None else max(0, device.total_bytes-margin)
                    existing = sum(size for (dev, _), (size, _) in self.residents.items() if dev == demand.device_id)
                    new_resident = 0 if (demand.device_id, demand.model_key) in self.residents else demand.resident_bytes
                    if demand.resident_bytes + demand.workspace_bytes > limit:
                        raise LlmFailure('resource:insufficient', outcome='not_sent')
                    available = min(limit-existing-self.reserved[demand.device_id],
                                    max(0, device.free_bytes-margin)-self.reserved[demand.device_id])
                    batch = min(demand.requested_batch, policy.wd_max_batch,
                                max(0, (available-new_resident)//demand.workspace_bytes))
                    allocation = new_resident + batch*demand.workspace_bytes
                if batch > 0 and self.slots[demand.device_id] < (policy.cpu_slots if cpu else policy.gpu_slots):
                    self.slots[demand.device_id] += 1
                    self.reserved[demand.device_id] += allocation
                    return ComputeLease(self, demand, batch, allocation)
            await self.sleep(min(.01, max(0, deadline-self.clock())))


_shared = WeakKeyDictionary()


def shared_compute():
    loop = asyncio.get_running_loop()
    if loop not in _shared:
        _shared[loop] = ComputeController()
    return _shared[loop]


async def run_worker(work):
    """Cancellation never detaches a worker holding an inference reservation."""
    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        finally:
            raise
