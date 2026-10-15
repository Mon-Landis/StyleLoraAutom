"""A single product-file worker; CAS and replacement execute in FIFO order."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from stylelora.llm.config import atomic_json, read_json, ConfigStore
from .contracts import fields, check, Failure


class ProductStore:
    def __init__(self, root):
        self.root = Path(root)
        self.queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix='stylelora-config')
        self.llm = ConfigStore(self.root / 'llm.json')
        self.defaults = dict(revision=0, cost_policy='ask', storage_thresholds={'project_bytes': 5_000_000_000},
                             llm_limits={'concurrency': 2, 'timeout_s': 120}, wd_enabled=False,
                             mcp={'discovery_mode': 'default'}, capabilities={})

    async def call(self, work):
        return await asyncio.wrap_future(self.queue.submit(work))

    def get(self):
        data = read_json(self.root / 'config.json', deepcopy(self.defaults))
        return data | {'version': str(data['revision'])}

    def update(self, patch, expected):
        fields(patch, {'cost_policy', 'storage_thresholds', 'llm_limits', 'wd_enabled', 'mcp'})
        data = self.get()
        check(expected, data.pop('version'))
        if 'cost_policy' in patch and patch['cost_policy'] not in ('deny', 'ask', 'allow'):
            raise Failure(422, 'input:invalid')
        if 'wd_enabled' in patch and type(patch['wd_enabled']) is not bool:
            raise Failure(422, 'input:invalid')
        if 'storage_thresholds' in patch:
            threshold = fields(patch['storage_thresholds'], {'project_bytes'}, {'project_bytes'})['project_bytes']
            if type(threshold) is not int or threshold <= 0:
                raise Failure(422, 'input:invalid')
        if 'llm_limits' in patch:
            limits = fields(patch['llm_limits'], {'concurrency', 'timeout_s'}, {'concurrency', 'timeout_s'})
            if type(limits['concurrency']) is not int or not 1 <= limits['concurrency'] <= 2 or type(limits['timeout_s']) is not int or limits['timeout_s'] <= 0:
                raise Failure(422, 'input:invalid')
        if 'mcp' in patch and fields(patch['mcp'], {'discovery_mode'}, {'discovery_mode'})['discovery_mode'] not in ('default', 'configured'):
            raise Failure(422, 'input:invalid')
        if any(data[key] != value for key, value in patch.items()):
            data.update(patch)
            data['revision'] += 1
            atomic_json(self.root / 'config.json', data)
        return data | {'version': str(data['revision'])}

    def close(self):
        self.queue.shutdown(wait=True)

    def get_resources(self):
        from stylelora.resources.network import NetworkPolicy
        from stylelora.resources.compute import ComputePolicy
        from .contracts import plain
        return read_json(self.root/'resources.json', dict(revision='0', network=plain(NetworkPolicy()), compute=plain(ComputePolicy())))

    def update_resources(self, value):
        from stylelora.resources.network import NetworkPolicy
        from stylelora.resources.compute import ComputePolicy
        from .contracts import plain
        fields(value, {'expected_revision', 'network', 'compute'}, {'expected_revision', 'network', 'compute'})
        try:
            network, compute = NetworkPolicy(**value['network']), ComputePolicy(**value['compute'])
        except (TypeError, ValueError):
            raise Failure(422, 'resource:invalid_policy') from None
        current = self.get_resources()
        if value['expected_revision'] != current['revision']:
            raise Failure(409, 'resource:revision_conflict')
        result = dict(revision=str(int(current['revision'])+1), network=plain(network), compute=plain(compute))
        atomic_json(self.root/'resources.json', result)
        return result
