"""Atomic admission for each network send, including shared secret references."""
import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import time
from types import MappingProxyType
from stylelora.llm.types import LlmFailure


@dataclass(frozen=True)
class NetworkPolicy:
    global_limit: int = 2
    endpoint_default: int = 2
    token_default: int = 1
    endpoint_limits: Mapping[str, int] = field(default_factory=dict)
    token_limits: Mapping[str, int] = field(default_factory=dict)
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0

    def __post_init__(self):
        for key in ('endpoint_limits', 'token_limits'):
            value = getattr(self, key)
            if not isinstance(value, Mapping) or any(not isinstance(k, str) or not k for k in value):
                raise LlmFailure('resource:invalid_policy', outcome='not_sent')
            object.__setattr__(self, key, MappingProxyType(dict(value)))
        limits = (self.global_limit, self.endpoint_default, self.token_default,
                  *self.endpoint_limits.values(), *self.token_limits.values())
        if any(type(v) is not int or v < 1 for v in limits):
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
               for v in (self.backoff_base_s, self.backoff_cap_s)):
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')


class NetworkLease:
    def __init__(self, controller, endpoint_id, token_ref):
        self.controller, self.endpoint_id, self.token_ref = controller, endpoint_id, token_ref
        self.released = False

    async def release(self):
        async with self.controller.lock:
            if self.released:
                return
            self.released = True
            self.controller.active -= 1
            self.controller.endpoints[self.endpoint_id] -= 1
            self.controller.tokens[self.token_ref] -= 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.release()


class NetworkController:
    def __init__(self, policy=None, *, clock=time.monotonic, sleep=asyncio.sleep, jitter=lambda: 0.0):
        self.policy = policy or NetworkPolicy()
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.revision, self.active = 0, 0
        self.endpoints, self.tokens = Counter(), Counter()
        self.cooldowns, self.endpoint_cooldowns, self.cursors = {}, {}, {}
        self.lock = asyncio.Lock()
        self.waiters = []

    def configure(self, policy):
        if not isinstance(policy, NetworkPolicy):
            raise LlmFailure('resource:invalid_policy', outcome='not_sent')
        self.policy = policy
        self.revision += 1
        return self.revision

    def _select(self, endpoint, refs):
        policy, now = self.policy, self.clock()
        if (self.active >= policy.global_limit or
                self.endpoints[endpoint] >= policy.endpoint_limits.get(endpoint, policy.endpoint_default) or
                self.endpoint_cooldowns.get(endpoint, 0) > now):
            return None
        start = self.cursors.get(endpoint, 0) % len(refs)
        for offset in range(len(refs)):
            index = (start + offset) % len(refs)
            ref = refs[index]
            if (self.tokens[ref] < policy.token_limits.get(ref, policy.token_default) and
                    self.cooldowns.get((endpoint, ref), 0) <= now):
                return index
        return None

    async def acquire(self, endpoint_id, token_refs, *, cancel, deadline):
        refs = tuple(dict.fromkeys(token_refs))
        if not refs:
            raise LlmFailure('llm:secret_missing', outcome='not_sent')
        waiter = (object(), endpoint_id, refs)
        self.waiters.append(waiter)
        try:
            while True:
                if cancel is not None and cancel.canceled:
                    raise LlmFailure('llm:canceled', outcome='not_sent')
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise LlmFailure('resource:timeout', outcome='not_sent')
                async with self.lock:
                    eligible = next((w for w in self.waiters if self._select(w[1], w[2]) is not None), None)
                    if eligible is waiter:
                        index = self._select(endpoint_id, refs)
                        self.cursors[endpoint_id] = (index + 1) % len(refs)
                        self.active += 1
                        self.endpoints[endpoint_id] += 1
                        self.tokens[refs[index]] += 1
                        return NetworkLease(self, endpoint_id, refs[index])
                await self.sleep(min(.01, remaining))
        finally:
            self.waiters.remove(waiter)

    def cooldown(self, endpoint_id, token_ref, *, retry_after_s, attempt, endpoint_wide=False):
        delay = max(retry_after_s, min(self.policy.backoff_cap_s,
                    self.policy.backoff_base_s * 2 ** min(attempt, 60)) + max(0.0, self.jitter()))
        until = self.clock() + delay
        key = (endpoint_id, token_ref)
        self.cooldowns[key] = max(until, self.cooldowns.get(key, 0))
        if endpoint_wide:
            self.endpoint_cooldowns[endpoint_id] = max(until, self.endpoint_cooldowns.get(endpoint_id, 0))
