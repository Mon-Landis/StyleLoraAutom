import tempfile
import unittest
import io
from pathlib import Path
from PIL import Image
from stylelora.vision.phash import compute_phash, hamming, find_candidates, HashRecord


class F08PhashTests(unittest.TestCase):
    def test_T01_golden_including_DC(self):
        image = Image.new('RGB', (32, 32))
        image.putdata([((x*7+y*3)%256, (x*x+y*11)%256, (x*13+y*y)%256)
                       for y in range(32) for x in range(32)])
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        stream.seek(0)
        self.assertEqual(compute_phash(stream), '862b0e1cf90bffa0')

    def test_T02_nontransitive_pairs_and_sort(self):
        pairs = find_candidates((HashRecord(3, '0000000000000003'),
            HashRecord(1, '0000000000000000'), HashRecord(2, '0000000000000001')), threshold=1)
        self.assertEqual([(p.left_id, p.right_id) for p in pairs], [(1, 2), (2, 3)])
        self.assertEqual(hamming('0'*16, 'f'*16), 64)

    def test_T01_transparency_white_and_import_generation_contract(self):
        from stylelora.exchange.importer import image_info
        from stylelora.generation.metadata import inspect_png
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'transparent.png'
            white = Path(temp)/'white.png'
            Image.new('RGBA', (32, 32), (0, 0, 0, 0)).save(path)
            Image.new('RGB', (32, 32), 'white').save(white)
            value = compute_phash(path)
            self.assertEqual(value, compute_phash(white))
            self.assertEqual(image_info(path)[2], value)
            self.assertEqual(inspect_png(path, style_prompt='', sent_prompt='').phash, value)

    def test_stable_64_bit_and_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'x.png'
            Image.new('RGB', (40, 40), 'white').save(path)
            value = compute_phash(path)
            self.assertEqual(value, compute_phash(path))
            self.assertRegex(value, r'^[0-9a-f]{16}$')
            self.assertEqual(hamming(value, value), 0)
            self.assertEqual(find_candidates((HashRecord(2, value), HashRecord(1, value)), threshold=0),
                             (find_candidates((HashRecord(1, value), HashRecord(2, value)), threshold=0)[0],))
            self.assertEqual(find_candidates((HashRecord(1, '0'*16), HashRecord(2, 'f'*16)), threshold=63), ())

    def test_threshold_required_range_and_invalid_hash(self):
        with self.assertRaises(ValueError):
            find_candidates((), threshold=-1)
        with self.assertRaises(ValueError):
            find_candidates((), threshold=65)
        with self.assertRaises(ValueError):
            hamming('bad', '0'*16)


from support import ProjectCase
from stylelora.project_db.types import Scope
from stylelora.vision.phash import scan_duplicates
from stylelora.executors.types import CancelToken
import asyncio


class ScanTests(ProjectCase):
    def test_T02_null_missing_cancel_and_no_mutation(self):
        image = self.image(payload=b'first')
        missing = self.image(payload=b'second')
        (self.root/'images'/missing.filename).unlink()
        counts = []
        async def scan(token):
            return [block async for block in scan_duplicates(self.project,
                Scope(selected_ids=(image.image_id, missing.image_id)), threshold=0,
                cancel=token, report=counts.append)]
        self.assertEqual(asyncio.run(scan(CancelToken())), [])
        self.assertEqual(counts[0], {'missing': 1, 'null': 1, 'invalid_hash': 0})
        self.assertEqual((self.root/'images'/image.filename).read_bytes(), b'first')
