from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from f09_support import catalog, row, snapshot, origin
from stylelora.tagging.catalog import LocalCatalog, load_catalog
from stylelora.tagging.normalization import normalize_tag, resolve_input_tag
from stylelora.tagging.tokenizer import tokenize_prompt
from stylelora.tagging.parser import parse_original_prompt, normalize_structured
from stylelora.tagging.translations import TranslationStore
from stylelora.tagging.migration import preview_tagcleaner_rules
from stylelora.tagging.jsonio import loads
from stylelora.tagging.trigger_validation import validate_trigger, override_digest
from stylelora.caption.types import CaptionConfig
from stylelora.caption.blocklist import build_blocklist, check_blocklist
from stylelora.caption.sorters import PyramidSorter, SortContext, checked_sort, UnsupportedSorter
from stylelora.caption.assembler import assemble_caption
from stylelora.annotations.merge import merge_annotations
from stylelora.rules.library import RuleLibrary
from stylelora.rules.types import RuleSpec, RuleGroup, parse_rule
from stylelora.rules.engine import evaluate_rule
from stylelora.project_db.types import Tag
from stylelora.project_db.errors import ValidationError, StateConflict
from stylelora.project_db.validation import normalize


class TaggingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def test_T01_normalization_exact_idempotent_and_empty(self):
        for raw,expected in [(' A__B ','a  b'),('Ａ','ａ'),('___',''),(' One Girl ','one girl')]:
            self.assertEqual(normalize_tag(raw),expected)
            self.assertEqual(normalize_tag(expected),expected)
            if expected:
                self.assertEqual(normalize(raw),expected)
        with self.assertRaises(ValidationError):
            normalize_structured((Tag('___'),))
        self.assertEqual(normalize_structured((Tag('unknown',None),)),(Tag('unknown'),))

    def test_T02_raw_offsets_bom_alias_unknown(self):
        text = '\ufeff1girl, long_hair, a girl stands, bow (weapon)'
        parsed = parse_original_prompt(text,catalog=self.catalog)
        self.assertEqual([t.tag for t in parsed.tags],['1girl','long hair','bow (weapon)'])
        self.assertEqual(parsed.natural_language,'a girl stands')
        tokens = tokenize_prompt(text,catalog=self.catalog)
        for token in tokens:
            self.assertEqual(text[token.offset:token.offset+len(token.raw)],token.raw)
        self.assertEqual(parse_original_prompt('1girl\r\n',catalog=self.catalog).tags,(Tag('1girl'),))

    def test_T02_unsupported_syntax_preserves_raw(self):
        for text in ('(1girl)','(1girl:1.2)','[1girl]','{1girl}','1.2::1girl::','1girl\\, smile','((1girl))','BREAK','a | b','a AND b'):
            with self.subTest(text=text):
                parsed = parse_original_prompt(text,catalog=self.catalog)
                self.assertTrue(parsed.unverifiable)
                self.assertEqual(','.join(s.raw for s in parsed.segments),text)

    def test_T03_alias_explicit_and_unavailable(self):
        self.assertEqual(normalize_tag('one girl'),'one girl')
        self.assertEqual(resolve_input_tag('one girl',self.catalog).canonical,'1girl')
        self.assertEqual(resolve_input_tag('one  girl',self.catalog).match,'unknown')
        self.assertEqual(resolve_input_tag('one  girl',self.catalog,collapse_spaces=True).canonical,'1girl')
        with self.assertRaises(ValidationError):
            parse_original_prompt('unknown',catalog=LocalCatalog())
        translated = LocalCatalog(translations={'new tag':'翻译'})
        self.assertEqual(translated.lookup('new_tag').match,'known')
        self.assertFalse(translated.complete)

    def test_T04_translation_cas_empty_conflict_replay(self):
        with tempfile.TemporaryDirectory() as root:
            store = TranslationStore(Path(root)/'translations.json')
            data = '{"long_hair":"长发","empty":""}'.encode()
            first = store.import_tagcleaner_translations(data,request_key='a',expected_revision=0)
            self.assertEqual((first.inserted,first.skipped),(1,1))
            self.assertEqual(store.import_tagcleaner_translations(data,request_key='a',expected_revision=0),first)
            self.assertEqual(store.get_translation('Long Hair'),'长发')
            before = store.path.read_bytes()
            for bad in (b'{',b'{"a":"x","a":"y"}',b'{"a":false}'):
                with self.assertRaises(ValidationError):
                    store.import_tagcleaner_translations(bad,request_key='b')
                self.assertEqual(before,store.path.read_bytes())
            with self.assertRaises(StateConflict):
                store.put_translation('long hair','x',expected_revision=0)
            with self.assertRaises(StateConflict):
                store.import_tagcleaner_translations(b'{"long hair":"x"}',request_key='c')
            self.assertTrue(store.put_translation('long hair','',expected_revision=1).changed)
            self.assertIsNone(store.get_translation('long hair'))
            self.assertFalse(store.put_translation('long hair','',expected_revision=2).changed)

    def test_T04_catalog_versions_and_alias_conflicts(self):
        for data in ({'schema_version':2}, {'schema_version':True}, {'schema_version':1,'kind':'tag_catalog','revision':1,'tags':[], 'extra':1}):
            with self.assertRaises(ValidationError):
                LocalCatalog(data)
        with self.assertRaises(ValidationError):
            loads('{"a":NaN}')

    def test_T05_legacy_rules_preview_atomic_save(self):
        data = b'[{"id":1,"name":"r","delete":"A, b","write":["c"],"condition":"a","deleteMode":"all"}]'
        rows, issues = preview_tagcleaner_rules(data)
        self.assertEqual(rows[0]['rule']['delete'],{'mode':'all','tags':['a','b']})
        self.assertEqual(rows[0]['rule']['write']['mode'],'any')
        self.assertEqual(issues[0].code,'rules:condition_semantics')
        _,bad = preview_tagcleaner_rules(b'[{"surprise":1}]')
        self.assertEqual(bad[-1].severity,'error')
        with tempfile.TemporaryDirectory() as root:
            library = RuleLibrary(Path(root)/'rules.json')
            first = library.import_tagcleaner_rules(data,request_key='a',expected_revision=0)
            again = library.import_tagcleaner_rules(data,request_key='a',expected_revision=0)
            self.assertEqual(first,again)
            self.assertEqual(len(library.load()['rules']),1)
            self.assertNotIn('operation_log',library.path.read_text())


class MergeCaptionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()
        self.config = CaptionConfig()

    def tags(self,rows,config=None,**kwargs):
        return tuple(t.tag for t in merge_annotations(rows,config or self.config,**kwargs).tags)

    def test_T08_A_to_B_new_wd_and_unchanged_cutoff(self):
        original = row(10,'original',('a',))
        rule = row(20,'rule',('b',),meta=snapshot({'b':[origin('rule',20)]},(('a',20),)))
        wd = row(30,'wd',(Tag('a',.9),))
        self.assertEqual(self.tags((original,rule)),('b',))
        self.assertEqual(self.tags((original,rule,wd)),('b','a'))
        second = row(40,'rule',('b',),meta=snapshot({'b':[origin('rule',20)]},(('a',40),)))
        self.assertEqual(self.tags((original,second,wd)),('b',))
        unrelated = row(40,'rule',('b','a','c'),meta=snapshot({'b':[origin('rule',20)],'a':[origin('wd',30,.9)],'c':[origin('rule',40)]},(('a',20),)))
        self.assertEqual(self.tags((original,unrelated,wd)),('b','a','c'))

    def test_T12_all_layers_priority_and_disabled_removals(self):
        rows = [row(i+1,layer,(layer,)) for i,layer in enumerate(('original','wd','llm','rule','manual'))]
        config = replace(self.config,wd_null_confidence='include')
        self.assertEqual(self.tags(rows,config),('manual','rule','llm','wd','original'))
        self.assertEqual(self.tags(rows,replace(config,layer_priority=tuple(reversed(config.layer_priority)))),('original','wd','llm','rule','manual'))
        original = row(10,'original',('a',))
        rule = row(20,'rule',(),meta=snapshot({},(('a',20),)))
        self.assertEqual(self.tags((original,rule)),())
        self.assertEqual(self.tags((original,rule),replace(config,tag_layers=('original',))),('a',))

    def test_T13_nl_fallback_and_trigger_once(self):
        rows = (row(1,'original',('a','custom trigger')),row(2,'original',nl='original'),row(3,'llm',nl='llm'),row(4,'manual',nl=''))
        config = replace(self.config,include_nl=True)
        view = merge_annotations(rows,config)
        self.assertEqual(view.nl,'llm')
        result = assemble_caption(view,config,' Custom_Trigger ',build_blocklist('',self.catalog))
        self.assertEqual(result.serialize(),'custom trigger, a\n\nllm')
        self.assertIsNone(merge_annotations(rows,self.config).nl)

    def test_T14_blocklist_exact_exceptions_warn_remove(self):
        tags = tuple(Tag(t) for t in ('watercolor background','gradient hair','pastel pink dress','border collie','bow (weapon)','test artist','safe','safe pose','custom style'))
        view = merge_annotations((row(1,'original',tags),))
        guard = build_blocklist('custom_style',self.catalog)
        hits = check_blocklist(tags,None,guard)
        self.assertEqual({h.tag for h in hits},{'watercolor background','test artist','safe','custom style'})
        warn = assemble_caption(view,self.config,'zztrigger',guard)
        self.assertEqual(len(warn.tags),len(tags)+1)
        result = assemble_caption(view,self.config,'zztrigger',replace(guard,mode='remove'))
        self.assertEqual(set(result.removed_blocklist),{h.tag for h in hits})
        self.assertEqual(view.tags,tags)
        self.assertFalse(check_blocklist((), 'The framing is an upper body view.',guard))
        view = replace(view,nl='Painted in watercolor.')
        with self.assertRaisesRegex(ValidationError,'nl_review_required'):
            assemble_caption(view,replace(self.config,include_nl=True),'zztrigger',replace(guard,mode='remove'))
        with self.assertRaisesRegex(ValidationError,'blocklist_conflict'):
            assemble_caption(view,self.config,'custom style',replace(guard,mode='remove'))

    def test_T15_trigger_catalog_style_and_syntax(self):
        self.assertEqual(validate_trigger('1girl','',catalog=self.catalog)[0].severity,'warning')
        self.assertTrue(override_digest('1girl',self.catalog))
        issues = validate_trigger('my_token','drawn in my token style',catalog=self.catalog)
        self.assertIn('trigger:in_style_prompt',[i.code for i in issues])
        for trigger in ('','x,y','x\ny','(x)','x:1.2','AND'):
            self.assertEqual(validate_trigger(trigger,'',catalog=self.catalog)[0].severity,'error')
        self.assertIn('trigger:unverifiable',[i.code for i in validate_trigger('new','',catalog=LocalCatalog(translations={'a':'A'}))])
        self.assertIn('trigger:unverifiable',[i.code for i in validate_trigger('new','(watercolor:1.2)',catalog=self.catalog)])

    def test_T16_sort_only_stable_unknowns_and_contract(self):
        sorter = PyramidSorter(SortContext(categories={'girl':'subject','smile':'expression','dress':'clothing','forest':'scene'}))
        self.assertEqual(checked_sort(sorter,('unknown2','forest','dress','smile','girl','unknown1')),('girl','smile','dress','forest','unknown2','unknown1'))
        with self.assertRaises(UnsupportedSorter):
            checked_sort(None,('a',))
        class Bad:
            def sort(self,tags,context):
                return ('renamed',)
        with self.assertRaises(ValidationError):
            checked_sort(Bad(),('a',))

    def test_T29_wd_copied_threshold_null_and_multiple_evidence(self):
        wd = row(10,'wd',(Tag('a',.4),Tag('b',None)))
        rule = row(20,'rule',('a','b'),meta=snapshot({'a':[origin('wd',10,.4)],'b':[origin('wd',10)]}))
        self.assertEqual(self.tags((wd,rule),replace(self.config,wd_min_confidence=.5)),())
        self.assertEqual(self.tags((wd,rule),replace(self.config,wd_null_confidence='include')),('a','b'))
        self.assertEqual(self.tags((wd,rule),editing=True),('a','b'))
        original = row(5,'original',('a',))
        multi = row(20,'rule',('a',),meta=snapshot({'a':[origin('wd',10,.4),origin('original',5)]}))
        self.assertEqual(self.tags((multi,),replace(self.config,wd_min_confidence=.5)),('a',))

    def test_T10_rule_groups_truth_table(self):
        view = merge_annotations((row(1,'original',('a','b')),))
        for mode,delete,write,condition,expected in (
            ('all',('a','z'),('c',),None,('a','b')),
            ('any',('a','z'),('c',),None,('b','c')),
            ('all',('a','b'),('c',),None,('c',)),
            ('all',None,('b','c'),None,('a','b')),
            ('any',None,('b','c'),None,('a','b','c')),
            ('any',None,('c',),('z',),('a','b')),
            ('all',None,('c',),('a','b'),('a','b','c')),
        ):
            with self.subTest(mode=mode,delete=delete,write=write,condition=condition):
                rule = RuleSpec(RuleGroup(mode,delete) if delete else None,RuleGroup(mode,write),RuleGroup(mode,condition) if condition else None)
                delta = evaluate_rule(rule,view,view.tags)
                self.assertEqual(tuple(t.tag for t in delta.after),expected)
        rule = RuleSpec(RuleGroup('any',('a',)),RuleGroup('any',('a',)))
        self.assertEqual(tuple(t.tag for t in evaluate_rule(rule,view,view.tags).after),('b','a'))

    def test_T30_rule_schema_and_empty_groups(self):
        rule = parse_rule({'schema_version':1,'delete':None,'write':{'mode':'any','tags':['A','a']},'condition':{'mode':'all','tags':[]}})
        self.assertEqual(rule.write.tags,('a',))
        self.assertIsNone(rule.condition)
        for value in ({}, {'schema_version':True,'delete':None,'write':None,'condition':None},
                      {'schema_version':1,'delete':None,'write':None,'condition':None},
                      {'schema_version':1,'delete':None,'write':{'mode':'random','tags':['a']},'condition':None}):
            with self.assertRaises(ValidationError):
                parse_rule(value)
