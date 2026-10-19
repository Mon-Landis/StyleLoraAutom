"""A single product-file worker; CAS and replacement execute in FIFO order."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from stylelora.llm.config import atomic_json, read_json, ConfigStore
from .contracts import fields, check, Failure

# resources.json 网络策略版本；v1 的默认值为 global 2 / endpoint 2 / token 1（U27 前）
RESOURCE_POLICY_VERSION = 2
LEGACY_NETWORK_DEFAULTS = {'global_limit': 2, 'endpoint_default': 2, 'token_default': 1}
# Settings retired from the API contract: old product files may still carry them.
RETIRED_SETTINGS = frozenset({'prompt_auto_complete_default'})
INTERNAL_SETTINGS = frozenset({'_safety_operations'})


def public_settings(data):
    """Drop retired/internal keys so they never leak into a response."""
    return {k: v for k, v in data.items() if k not in RETIRED_SETTINGS | INTERNAL_SETTINGS}


class ProductStore:
    def __init__(self, root):
        self.root = Path(root)
        self.queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix='stylelora-config')
        self.llm = ConfigStore(self.root / 'llm.json')
        self.defaults = dict(revision=0, cost_policy='ask', storage_thresholds={'project_bytes': 5_000_000_000},
                             llm_limits={'concurrency': 2, 'timeout_s': 120}, wd_enabled=False,
                             mcp={'discovery_mode': 'default'}, capabilities={},
                             nsfw_enabled=False, nsfw_display_mode='click_reveal')

    async def call(self, work):
        return await asyncio.wrap_future(self.queue.submit(work))

    def get(self):
        data = deepcopy(self.defaults) | {k: v for k, v in read_json(self.root / 'config.json', {}).items()
                                          if k not in RETIRED_SETTINGS}
        return public_settings(data) | {'version': str(data['revision'])}

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
            operations = read_json(self.root/'config.json', {}).get('_safety_operations')
            atomic_json(self.root / 'config.json', data | ({'_safety_operations': operations} if operations else {}))
        return public_settings(data) | {'version': str(data['revision'])}

    def close(self):
        self.queue.shutdown(wait=True)

    def update_safety(self, patch, expected, request_key):
        from stylelora.prompts.dictionaries import LOCK, digest
        with LOCK:
            fields(patch, {'nsfw_enabled', 'nsfw_display_mode', 'image_display'})
            if 'image_display' in patch:
                if 'nsfw_display_mode' in patch and patch['image_display'] != patch['nsfw_display_mode']:
                    raise Failure(422, 'schema:invalid')
                patch = dict(patch); patch['nsfw_display_mode'] = patch.pop('image_display')
            if 'nsfw_enabled' in patch and type(patch['nsfw_enabled']) is not bool or 'nsfw_display_mode' in patch and patch['nsfw_display_mode'] not in ('click_reveal', 'hover', 'show'):
                raise Failure(422, 'schema:invalid')
            data = self.get(); data.pop('version')
            operations = read_json(self.root/'config.json', {}).get('_safety_operations', {})
            signature = digest([patch, expected])
            if request_key in operations:
                if operations[request_key]['digest'] != signature:
                    raise Failure(409, 'dictionary:request_key_conflict')
                # Receipts cached before retirement may still embed the old field.
                return public_settings(operations[request_key]['result'])
            if data['revision'] != expected:
                raise Failure(412, 'settings:revision_conflict')
            data.update(patch); data['revision'] += 1
            result = public_settings(data | {'version': str(data['revision'])})
            operations[request_key] = {'digest': signature, 'result': result}
            atomic_json(self.root/'config.json', data | {'_safety_operations': operations})
            return result

    def get_resources(self):
        from stylelora.resources.network import NetworkPolicy
        from stylelora.resources.compute import ComputePolicy
        from .contracts import plain
        path = self.root/'resources.json'
        if not path.exists():
            return dict(revision='0', network=plain(NetworkPolicy()), compute=plain(ComputePolicy()))
        data = read_json(path, {})
        if data.get('_policy_version', 1) < RESOURCE_POLICY_VERSION:
            # v1 -> v2：仅把仍等于旧默认值的字段迁到新默认（-1 不限 / 端点 4），用户显式改过的值保留
            network = dict(data.get('network') or {})
            fresh = plain(NetworkPolicy())
            for key, old in LEGACY_NETWORK_DEFAULTS.items():
                if network.get(key) == old:
                    network[key] = fresh[key]
            data['network'] = network
            data['_policy_version'] = RESOURCE_POLICY_VERSION
            atomic_json(path, data)
        return {k: v for k, v in data.items() if k != '_policy_version'}

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
        atomic_json(self.root/'resources.json', result | {'_policy_version': RESOURCE_POLICY_VERSION})
        return result
