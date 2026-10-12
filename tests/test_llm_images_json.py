from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from stylelora.llm import (DEFAULT_TIERS, ImageInput, LlmFailure, RepairBudget,
                           encode_for_vision, encode_image, next_timeout_tier,
                           repair_json_text, validate_json_reply)


SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}


class ImageJsonTests(unittest.TestCase):
    def test_scale_all_sizes_from_original_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'original.png'
            Image.new('RGBA', (2200, 1400), (255, 0, 0, 128)).save(path)
            original = path.read_bytes()
            for tier in DEFAULT_TIERS:
                encoded = encode_for_vision(path, file_size=10_000_000, tier=tier)
                self.assertLessEqual(encoded.width * encoded.height, tier.max_pixels)
                self.assertEqual(encoded.original_sha256, hashlib.sha256(original).hexdigest())
                with Image.open(io.BytesIO(encoded.data)) as jpeg:
                    self.assertEqual(jpeg.format, 'JPEG')
                    self.assertEqual(jpeg.mode, 'RGB')
                self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_small_image_no_upscale_and_white_alpha(self):
        raw = io.BytesIO()
        Image.new('RGBA', (8, 4), (0, 0, 0, 0)).save(raw, 'PNG')
        encoded = encode_image(ImageInput(data=raw.getvalue()))
        self.assertEqual((encoded.width, encoded.height), (8, 4))
        with Image.open(io.BytesIO(encoded.data)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_only_timeout_downgrades(self):
        self.assertEqual(next_timeout_tier(DEFAULT_TIERS[0], 'timeout'), DEFAULT_TIERS[1])
        self.assertIsNone(next_timeout_tier(DEFAULT_TIERS[-1], 'timeout'))
        for cause in ('429', '503', 'invalid_json', 'refusal'):
            self.assertIsNone(next_timeout_tier(DEFAULT_TIERS[0], cause))

    def test_bad_image_safe_failure(self):
        with self.assertRaises(LlmFailure):
            encode_image(ImageInput(data=b'bad secret bytes'))

    def test_decode_limit_rejects_before_loading_pixels(self):
        opened = MagicMock()
        opened.__enter__.return_value = opened
        opened.width, opened.height = 10_001, 10_000
        with patch('stylelora.llm.images.Image.open', return_value=opened):
            with self.assertRaises(LlmFailure) as caught:
                encode_image(ImageInput(data=b'header-only'))
        self.assertEqual(caught.exception.code, 'llm:image_too_large')
        opened.load.assert_not_called()

    def test_strict_json_rejects_malformed_or_unknown(self):
        for text in ('```json\n{"answer":"x"}\n```', 'prose {"answer":"x"}',
                     '{"answer":"a","answer":"b"}', '{"answer":NaN}',
                     '{"answer":"x","extra":1}', '{"answer":4}', '{}', '[]'):
            with self.subTest(text=text), self.assertRaises(LlmFailure):
                validate_json_reply(text, SCHEMA)

    def test_local_recovery_handles_escaped_braces(self):
        text = '```json\n{"answer":"a } \\\" { b"}\n``` trailing'
        recovered = repair_json_text(text)
        self.assertEqual(validate_json_reply(recovered, SCHEMA)['answer'], 'a } " { b')
        self.assertEqual(repair_json_text('before {"answer":"first"} {"answer":"second"}'),
                         '{"answer":"first"}')

    def test_schema_branches_and_hook(self):
        schema = {'oneOf': [SCHEMA, {'type': 'object', 'properties': {'status': {'const': 'refused_nsfw'}},
                                    'required': ['status']}]}
        self.assertEqual(validate_json_reply('{"status":"refused_nsfw"}', schema)['status'], 'refused_nsfw')
        seen = []
        validate_json_reply('{"answer":"x"}', SCHEMA, validator=lambda value, schema: seen.append(value))
        self.assertEqual(len(seen), 1)
        with self.assertRaises(LlmFailure):
            validate_json_reply('{"answer":"x"}', {'$ref': '#/unknown'})

    def test_budget_atomic_single_claim(self):
        budget = RepairBudget()
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(lambda _: budget.claim(), range(20))), 1)
