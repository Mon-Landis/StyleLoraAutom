from dataclasses import replace
from pathlib import Path
import unittest
from stylelora.executors.cache import cache_keys
from stylelora.executors.types import ImageContext, ImageInput, Evidence, CancelToken


class CacheTests(unittest.TestCase):
    def context(self):
        return ImageContext(ImageInput(1, Path('local.png'), 'a'*64, 1, 1, 1),
            Evidence((), '', (), (), 'b'*64), None,
            {'role': 'vision', 'protocol': 'test', 'params': {'D1_hash': 'c'*64},
             'routing': {}, 'target_types': ['tags', 'nl'], 'skill_hash': 'd'*64}, CancelToken())

    def test_T19_semantic_inputs_change_keys(self):
        context = self.context()
        def keys(value=context, model='m', actual=None):
            return cache_keys(value, actual_model=model, actual_params=actual or {'tier': {'max_pixels': 2000000, 'quality': 92}})
        original = keys()
        self.assertNotEqual(original, keys(model='other'))
        self.assertNotEqual(original, keys(replace(context, image=replace(context.image, sha256='e'*64))))
        self.assertNotEqual(original, keys(replace(context, evidence=replace(context.evidence, digest='e'*64))))
        self.assertNotEqual(original, keys(replace(context, snapshot=dict(context.snapshot, skill_hash='e'*64))))
        self.assertNotEqual(original, keys(replace(context, snapshot=dict(context.snapshot, params={'D1_hash': 'e'*64}))))
        self.assertNotEqual(original, keys(actual={'tier': {'max_pixels': 1000000, 'quality': 84}}))
        self.assertNotEqual(original['llm', 'tags'], original['llm', 'nl'])

    def test_T19_scheduling_does_not_change_keys(self):
        context = self.context()
        first = cache_keys(context, actual_model='m', actual_params={'provider': 'cpu', 'attempts': 1, 'actual_batch': 1})
        second = cache_keys(replace(context, snapshot=dict(context.snapshot, concurrency=2, timeout=3)),
                            actual_model='m', actual_params={'provider': 'cpu', 'attempts': 3, 'actual_batch': 4})
        self.assertEqual(first, second)
