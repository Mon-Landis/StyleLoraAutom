import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stylelora.llm import (ConfigStore, EndpointConfig, LlmFailure, ModelConfig,
                           SecretStore, mask_secret)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = ConfigStore(self.path / 'llm.json')
        self.endpoint = EndpointConfig('local', 'http://localhost:9999/v1', 'openai_chat',
                                       ('PRIMARY',), (ModelConfig('model'),))

    def test_round_trip_cas_and_noop(self):
        saved = self.config.save_endpoint(self.endpoint, expected_revision=0)
        self.assertEqual(saved.revision, 1)
        self.assertEqual(self.config.load_endpoints(), (saved,))
        self.assertFalse(self.config.save_endpoint(saved, expected_revision=1).changed)
        with self.assertRaises(LlmFailure) as caught:
            self.config.save_endpoint(saved, expected_revision=0)
        self.assertEqual(caught.exception.code, 'llm:revision_conflict')

    def test_roles_and_reserved_empty(self):
        self.config.save_endpoint(self.endpoint, expected_revision=0)
        for role in ('prompt', 'prompt_nsfw', 'vision', 'vision_nsfw', 'rating', 'clean', 'clean_nsfw'):
            self.assertIsNone(self.config.resolve_role(role))
            binding = self.config.bind_role(role, 'local', 'model', expected_revision=0)
            self.assertEqual(binding.protocol, 'openai_chat')
            self.assertEqual(binding.model, 'model')
            self.assertIsNone(self.config.bind_role(role, None, expected_revision=1))
        with self.assertRaises(LlmFailure):
            self.config.resolve_role('other')

    def test_reject_url_credentials(self):
        from dataclasses import replace
        for url in ('https://user:password@example.com', 'https://example.com?key=secret'):
            with self.assertRaises(LlmFailure):
                self.config.save_endpoint(replace(self.endpoint, base_url=url), expected_revision=0)

    def test_env_precedence_empty_and_mask(self):
        store = SecretStore(self.path / 'secrets.json')
        store.path.write_text('not valid encrypted storage', encoding='utf-8')
        with patch.dict(os.environ, {'STYLELORA_LLM_TOKEN_PRIMARY': 'private-token'}):
            self.assertEqual(store.resolve_secret('primary'), 'private-token')
            public = json.dumps(store.public_endpoint(self.endpoint))
            self.assertNotIn('private-token', public)
            self.assertNotIn('localhost', public)
            self.assertNotIn('STYLELORA', public)
            self.assertTrue(store.has_secret('PRIMARY'))
        with patch.dict(os.environ, {'STYLELORA_LLM_TOKEN_PRIMARY': ''}):
            with self.assertRaises(LlmFailure) as caught:
                store.resolve_secret('PRIMARY')
            self.assertEqual(caught.exception.code, 'llm:secret_empty')
        self.assertEqual(mask_secret('x'), '********')

    @unittest.skipUnless(os.name == 'nt', 'DPAPI requires Windows')
    def test_dpapi_roundtrip_encrypted_and_noop(self):
        store = SecretStore(self.path / 'secrets.json')
        store.put_secret('DPAPI_TEST', 'private-dpapi-token')
        content = store.path.read_bytes()
        self.assertNotIn(b'private-dpapi-token', content)
        self.assertEqual(store.resolve_secret('DPAPI_TEST'), 'private-dpapi-token')
        store.put_secret('DPAPI_TEST', 'private-dpapi-token')
        self.assertEqual(content, store.path.read_bytes())
        raw = json.loads(content)
        raw['DPAPI_TEST'] = 'AAAA'
        store.path.write_text(json.dumps(raw), encoding='utf-8')
        with self.assertRaises(LlmFailure):
            store.resolve_secret('DPAPI_TEST')

    def test_non_windows_no_plaintext_fallback(self):
        store = SecretStore(self.path / 'secrets.json')
        with patch('stylelora.llm.secrets.os.name', 'posix'):
            with self.assertRaises(LlmFailure) as caught:
                store.put_secret('PRIMARY', 'private-token')
        self.assertEqual(caught.exception.code, 'llm:dpapi_unavailable')
        self.assertFalse(store.path.exists())

    def test_concurrent_cas_has_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        def save(_):
            try:
                ConfigStore(self.config.path).save_endpoint(self.endpoint, expected_revision=0)
                return True
            except LlmFailure:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(save, range(4))), 1)
