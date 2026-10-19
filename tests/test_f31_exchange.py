import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from stylelora.prompts.group_exchange import GroupExchange
from stylelora.prompts.group_store import GroupStore
from stylelora.prompts.groups import GroupRef
from stylelora.prompts.group_txt import HEADER, LEGACY_HEADER
from stylelora.prompts.types import PromptError


class ExchangeV2Tests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 0
        self.exchange = GroupExchange(GroupStore(self.root), clock=lambda: self.now)
        self.meta = dict(format_version=2, prompt_format='nai', normalization_version='nai-storage-v1')

    def preview(self, rows, metadata=None, **options):
        return self.exchange.preview_import(json.dumps(dict(
            prompts=rows, group_metadata=self.meta if metadata is None else metadata)).encode(), options)

    def commit(self, p, key='confirm', exchange=None):
        return (exchange or self.exchange).commit_import(p['preview_id'], p['digest'],
            p['base_revision'], key, GroupRef(**p['ref']))

    def test_v2_normalizes_and_exports_content_only(self):
        p = self.preview([dict(nl=' {blue_hair}\r\n雪 ', disable=True,
                               variants={'a': '[cat]', 'empty': ''})])
        self.assertEqual(p['entries'][0]['nl'], ' 1.05::blue hair::\n雪 ')
        self.assertEqual(p['normalization_version'], 'nai-storage-v1')
        rounded = [w for w in p['warnings'] if w['code'] == 'normalization:rounded']
        self.assertTrue(rounded)
        self.assertEqual((rounded[0]['row'], rounded[0]['field']), (1, "variants['a']"))
        self.commit(p)
        raw = json.loads(self.exchange.export_group(GroupRef(**p['ref'])))
        self.assertEqual(raw['group_metadata']['format_version'], 2)
        self.assertEqual(set(raw['prompts'][0]), {'nl', 'variants', 'disable', 'aspect'})
        copied = self.exchange.preview_import(json.dumps(raw).encode())
        self.assertEqual(copied['entries'][0]['nl'], p['entries'][0]['nl'])
        self.assertEqual(copied['entries'][0]['variants'], p['entries'][0]['variants'])

    def test_legacy_tag_and_expand_become_variants_and_report(self):
        p = self.preview([dict(tag=['(blue_cat:1.3)'], expand_nl=' Window ', nl='A cat sits.'),
                          dict(tag=['(broken'])], metadata={})
        self.assertEqual(p['entries'][0]['nl'], 'A cat sits.')
        self.assertEqual(p['entries'][0]['variants'],
                         {'tag': '1.5::blue cat::', 'expand_nl': ' Window '})
        self.assertEqual(p['skipped_count'], 1)
        self.assertEqual(p['issues'][0]['row'], 2)

    def test_legacy_variant_conflict_and_limit_warn_without_losing_nl(self):
        p = self.preview([dict(tag=['cat'], expand_nl='cat', nl='cat', variants={'tag': 'keep'}),
                          dict(tag=['new'], expand_nl='newer', nl='kept',
                               variants={str(i): 'x' for i in range(63)})], metadata={})
        # Existing variant key wins over the legacy field.
        self.assertEqual(p['entries'][0]['nl'], 'cat')
        self.assertEqual(p['entries'][0]['variants'], {'tag': 'keep', 'expand_nl': 'cat'})
        conflict = [w for w in p['warnings'] if w['code'] == 'migration:variant_conflict']
        self.assertEqual([w['row'] for w in conflict], [1])
        # The 64-key limit drops only the extra variant, never nl.
        self.assertEqual(p['entries'][1]['nl'], 'kept')
        self.assertEqual(len(p['entries'][1]['variants']), 64)
        limited = [w for w in p['warnings'] if w['code'] == 'migration:variant_limit']
        self.assertEqual([w['row'] for w in limited], [2])

    def test_legacy_duplicate_warning_and_nl_fallback(self):
        p = self.preview([dict(tag=['cat'], expand_nl='cat', nl=''),
                          dict(tag=['Sitting.'], nl='A cat sits.')], metadata={})
        # Empty nl falls back to expand_nl first, then tag; the source stays a variant.
        self.assertEqual(p['entries'][0]['nl'], 'cat')
        self.assertEqual(p['entries'][0]['variants'], {'tag': 'cat', 'expand_nl': 'cat'})
        self.assertTrue(any(w['code'] == 'migration:possible_duplicate' for w in p['warnings']))
        self.assertTrue(any(w['code'] == 'migration:nl_fallback' for w in p['warnings']))

    def test_v2_rejects_legacy_and_internal_fields_per_row(self):
        rows = [{'nl': 'ok'}] + [{'nl': 'bad', k: {}} for k in
            ('tag', 'expand_nl', 'generated', 'pending', 'preview', 'edit_versions')]
        p = self.preview(rows)
        self.assertEqual((p['valid_count'], p['skipped_count']), (1, 6))
        self.assertEqual({i['row'] for i in p['issues']}, set(range(2, 8)))

    def test_v2_root_contract(self):
        for metadata, options in ((self.meta, {'source_format': 'anima'}),
                (self.meta | {'prompt_format': 'anima'}, {}),
                (self.meta | {'format_version': 3}, {})):
            with self.subTest(metadata=metadata, options=options), self.assertRaises(PromptError):
                self.preview([{'nl': 'cat'}], metadata=metadata, **options)

    def test_confirm_expired_and_restart_receipts_precede_cas(self):
        p = self.preview([{'nl': 'cat'}])
        result = self.commit(p)
        self.now = 901
        self.assertEqual(self.commit(p), result)
        restarted = GroupExchange(GroupStore(self.root))
        self.assertEqual(self.commit(p, exchange=restarted), result)
        for changed in (p | {'digest': 'wrong'}, p | {'preview_id': 'wrong'},
                        p | {'base_revision': 9}):
            with self.assertRaises(PromptError) as caught:
                self.commit(changed, exchange=restarted)
            self.assertEqual(caught.exception.code, 'dictionary:request_key_conflict')
        self.assertEqual(len(restarted.store.load(GroupRef(**p['ref'])).entries), 1)

    def test_uncommitted_expired_preview(self):
        p = self.preview([{'nl': 'cat'}])
        self.now = 901
        with self.assertRaises(PromptError) as caught:
            self.commit(p)
        self.assertEqual(caught.exception.code, 'preview:expired')

    def test_txt_v1_and_v2_physical_diagnostics(self):
        for header, meta, row in ((LEGACY_HEADER, {}, {'tag': ['cat'], 'nl': '雪'}),
                                 (HEADER, self.meta, {'nl': '{cat}\n雪'})):
            text = '\r\n'.join((header, json.dumps({'group_metadata': meta}),
                                json.dumps(row), '{bad', '{"nl":"{broken"}'))
            p = self.exchange.preview_import(b'\xef\xbb\xbf' + text.encode(),
                {'format': 'txt', 'source_format': 'nai'})
            self.assertEqual(p['valid_count'], 1)
            self.assertEqual({i['row'] for i in p['issues']}, {4, 5})

    def test_old_txt_preserves_order_and_converts_selected_source(self):
        p = self.exchange.preview_import(b'  {blue_hair}, cat\\ncat  ',
            {'format': 'txt', 'source_format': 'nai'})
        self.assertEqual(p['entries'][0]['nl'], '  1.05::blue hair::, cat\ncat  ')
