import json
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
from stylelora.prompts import *
from stylelora.prompts.validation import strict_loads, construct, tag_key
from stylelora.prompts.dictionaries import _path
from stylelora.prompts.exchange import export_value, valid_rows_preview
from stylelora.prompts.coverage import quotas, bounds
from stylelora.prompts.sampling import select
from stylelora.prompts.launcher_features import ReadOnlyAdapter
from stylelora.tagging.catalog import LocalCatalog
from stylelora.tagging.trigger_validation import validate_trigger


def dictionary(entries): return PromptDictionary(str(uuid4()),'test',entries=tuple(entries))
def catalog():
    return LocalCatalog({'schema_version':1,'kind':'tag_catalog','revision':1,'tags':[
        {'tag':'forest','aliases':['woods']},{'tag':'no humans'},{'tag':'wide shot'},
        {'tag':'bow (weapon)'},{'tag':'test artist','category':'artist'}]},complete=True)
def plan(n=1): return CoveragePlan(('scene','lighting'),{'scene':('forest',),'lighting':('sunlight',)},target_count=n,random_seed=42)


class SchemaDictionaryTests(unittest.TestCase):
    def test_T01_strict_boundaries(self):
        for call in (lambda:PromptEntry(()),lambda:PromptEntry(('forest',),aspect='bad'),lambda:PromptEntry(('forest',),rating='bad'),
                     lambda:replace(plan(),target_count=True),lambda:replace(plan(),schema_version=2),lambda:ConstraintSpec('x','forest',minimum_ratio=float('nan')),
                     lambda:strict_loads('{"a":1,"a":2}'),lambda:strict_loads('{"a":NaN}'),lambda:construct(PromptEntry,{'tags':['forest'],'extra':1})):
            with self.subTest(call=call),self.assertRaises(PromptError): call()
        self.assertEqual(normalize_tag('  WIDE__ Shot  '),'wide shot')
        self.assertEqual(normalize_tag(normalize_tag(' WIDE__Shot ')),'wide shot')
        self.assertEqual(normalize_tag('woods',catalog()),'forest')
        with self.assertRaises(TypeError): plan().cell_values['scene']=('beach',)

    def test_T02_atomic_CAS_replay_variants_conflicts(self):
        with TemporaryDirectory() as root:
            d=dictionary([PromptEntry(('forest',),rating='general')])
            self.assertEqual(save_dictionary(d,expected_revision=0,request_key='create',root=root),1)
            variants=(PromptEntry(('forest',),'A tree.',rating='general'),PromptEntry(('forest',),aspect='portrait',rating='general'))
            a=append_entries(d.dictionary_id,variants,expected_revision=1,request_key='append',root=root)
            self.assertEqual(a.inserted,2)
            self.assertEqual(append_entries(d.dictionary_id,variants,expected_revision=1,request_key='append',root=root),a)
            with self.assertRaises(PromptError): append_entries(d.dictionary_id,(),expected_revision=1,request_key='new',root=root)
            conflict=append_entries(d.dictionary_id,[PromptEntry(('forest',),rating='explicit')],expected_revision=2,request_key='conflict',root=root)
            self.assertEqual(conflict.conflicts[0].code,'dictionary:rating_conflict')
            path=_path(d.dictionary_id,'prompts',root); before=path.read_bytes()
            with patch('stylelora.prompts.dictionaries.os.replace',side_effect=OSError),self.assertRaises(PromptError):
                append_entries(d.dictionary_id,[PromptEntry(('beach',))],expected_revision=2,request_key='fail',root=root)
            self.assertEqual(path.read_bytes(),before)

    def test_T03_exchange_roundtrip_and_txt(self):
        d=dictionary([PromptEntry(('forest','no humans'),'A tree, beside a sign reading "oak".','landscape','general')])
        for fmt in ('json','csv'):
            preview=preview_import(export_value(d,fmt),format=fmt,kind='prompts',catalog=catalog())
            self.assertFalse(preview.issues); self.assertEqual(preview.entries,d.entries)
        with self.assertWarns(UserWarning): export_value(d,'txt')
        p=preview_import(b'\xef\xbb\xbfforest, A tree grows.\nonly unknown words',format='txt',kind='prompts',catalog=catalog())
        self.assertEqual(p.entries[0].natural_language,'A tree grows.'); self.assertEqual(p.issues[0].row,2)
        self.assertFalse(valid_rows_preview(p).issues)
        # U26: NL paragraphs are stored with real newlines and escaped as literal \n in TXT.
        multi=dictionary([PromptEntry(('forest',),'A tree grows.\n\nA bird sits on it.')])
        with self.assertWarns(UserWarning): out=export_value(multi,'txt')
        self.assertEqual(out,b'forest, A tree grows.\\n\\nA bird sits on it.')
        self.assertEqual(preview_import(out,format='txt',kind='prompts',catalog=catalog()).entries[0].natural_language,
                         'A tree grows.\n\nA bird sits on it.')
        self.assertEqual(preview_import(export_value(multi,'csv'),format='csv',kind='prompts',catalog=catalog()).entries,multi.entries)
        self.assertEqual(preview_import(b'forest\tgeneral',format='txt',kind='features',dimension='scene').features[0].rating,'general')
        with self.assertRaises(PromptError): preview_import(b'\xff',format='txt',kind='prompts')
        with TemporaryDirectory() as root:
            empty=dictionary([]); save_dictionary(empty,expected_revision=0,request_key='create',root=root)
            with self.assertRaises(PromptError): commit_import(empty.dictionary_id,p,request_key='bad',root=root)
            valid=valid_rows_preview(p)
            first=commit_import(empty.dictionary_id,valid,request_key='ok',root=root)
            self.assertEqual(commit_import(empty.dictionary_id,valid,request_key='ok',root=root),first)

    def test_T05_T06_default_quota_and_overlap(self):
        p=default_plan(); q=quotas(p)
        self.assertEqual(sorted(q.values()),[16,16,17,17,17,17]); self.assertEqual(sum(q.values()),100)
        entries=[]
        for scene in ('forest','beach','city street'):
            for light in ('sunlight','night'):
                entries.extend(PromptEntry(('no humans','wide shot',scene,light,'tree '+str(i)),rating='general') for i in range(20))
        request=SamplingRequest(p)
        a=sample(request,dictionaries=(dictionary(entries),))
        b=sample(request,dictionaries=(dictionary(reversed(entries)),))
        self.assertEqual(a.entries,b.entries); self.assertEqual(a.report.status,'satisfied')
        self.assertEqual(a.report.constraint_counts,{'0':100,'1':100})
        self.assertEqual(bounds(ConstraintSpec('x','x',minimum_ratio=.14),100)[0],14)

    def test_T06_T07_unsatisfiable_exhaustion_and_fence(self):
        entries=(PromptEntry(('forest','sunlight',str(i)),rating='general') for i in range(5))
        d=dictionary(entries); request=SamplingRequest(plan(3))
        self.assertEqual(sample(request,dictionaries=(d,),search_nodes=0).report.status,'search_exhausted')
        self.assertEqual(sample(replace(request,plan=plan(6)),dictionaries=(d,)).report.status,'unsatisfiable')
        self.assertEqual(sample(request,dictionaries=(d,),candidate_limit=1).report.status,'search_exhausted')
        self.assertEqual(sample(SamplingRequest(default_plan(target_count=5)),dictionaries=(d,)).report.status,'unsatisfiable')
        bad=replace(request,plan=replace(plan(3),constraints=(ConstraintSpec('scene','forest',maximum_count=0),)))
        self.assertEqual(sample(bad,dictionaries=(d,)).report.status,'unsatisfiable')
        fenced_d=dictionary([PromptEntry(('forest','sunlight'),rating='explicit')])
        self.assertEqual(sample(SamplingRequest(plan()),dictionaries=(fenced_d,)).report.diagnostics['fenced'],1)
        self.assertEqual(sample(request,dictionaries=(d,d)).entries,sample(request,dictionaries=(d,)).entries)

    def test_T07_mixed_pool_and_feature_compatibility(self):
        p=replace(plan(4),compatibility_version='default-v1')
        features=FeatureDictionary(str(uuid4()),'f',dimensions={
            'count':(FeatureCandidate('count','no humans','general'),),
            'scene':(FeatureCandidate('scene','forest','general'),),
            'lighting':(FeatureCandidate('lighting','sunlight','general'),),
            'clothing':(FeatureCandidate('clothing','dress','general'),),
            'object':tuple(FeatureCandidate('object','tree '+str(i),'general') for i in range(10))})
        d=dictionary([PromptEntry(('forest','sunlight','rock '+str(i)),rating='general') for i in range(10)])
        result=sample(SamplingRequest(p),dictionaries=(d,),features=features)
        self.assertEqual(result.report.status,'satisfied'); self.assertNotIn('dress',{t for e in result.entries for t in e.tags})
        self.assertEqual(sum(result.report.source_counts.values()),4)

    def test_T05_schema_weighted_quota_exclusion_and_storage(self):
        p=default_plan(target_count=30,random_seed=7)
        cells=quotas(p); first=next(iter(cells))
        weighted=replace(p,cell_weights={first:5})
        self.assertGreater(quotas(weighted)[first],cells[first])
        self.assertEqual(sum(quotas(weighted).values()),30)
        excluded=replace(p,exclude=(Predicate(('forest',),('night',)),))
        self.assertEqual(len(quotas(excluded)),5)
        self.assertEqual(plan_from_json(export_plan(p)),p)
        with TemporaryDirectory() as root:
            key=str(uuid4()); save_plan(key,p,root=root); self.assertEqual(load_plan(key,root=root),p)

    def test_T06_unknown_feature_rating_stays_unverified(self):
        f=FeatureDictionary(str(uuid4()),'mixed',dimensions={
            'scene':(FeatureCandidate('scene','forest','general'),),
            'lighting':(FeatureCandidate('lighting','sunlight'),)})
        sampled=sample(SamplingRequest(plan()),features=f)
        decision=sampled.ratings[tag_key(sampled.entries[0])]
        self.assertEqual(decision.rating,'general'); self.assertFalse(decision.verified)

    def test_T03_feature_conflicts_unknown_fields_and_tamper(self):
        preview=preview_import(b'dimension,tag,rating\nscene,forest,general\nscene,forest,explicit\n',format='csv',kind='features')
        self.assertEqual(preview.features[0].rating,'explicit'); self.assertEqual(preview.issues[0].severity,'warning')
        with TemporaryDirectory() as root:
            d=FeatureDictionary(str(uuid4()),'f'); save_dictionary(d,expected_revision=0,request_key='create',root=root)
            first=commit_import(d.dictionary_id,preview,request_key='import',root=root)
            self.assertEqual(commit_import(d.dictionary_id,preview,request_key='import',root=root),first)
            with self.assertRaises(PromptError): commit_import(d.dictionary_id,replace(preview,base_revision=5),request_key='tamper',root=root)
        obj=json.loads(export_value(dictionary([]),'json')); obj['unknown']='no'
        with self.assertRaises(PromptError): preview_import(json.dumps(obj).encode(),format='json',kind='prompts')

    def test_T16_reuse_trigger_validation(self):
        self.assertIn('trigger:existing_tag',[i.code for i in validate_trigger('woods','',catalog=catalog())])
        self.assertIn('trigger:in_style_prompt',[i.code for i in validate_trigger('mytrigger','a mytrigger phrase',catalog=catalog())])
        self.assertIn('trigger:unverifiable',[i.code for i in validate_trigger('newword','')])
        self.assertEqual(serialize_content(PromptEntry(('forest',))),'forest')


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def test_T04_readonly_fixture_pages_and_schema(self):
        calls=[]
        class Client:
            async def list_tools(self): return [{'name':'fixture_list','inputSchema':{'type':'object'}}]
            async def call_tool(self,name,args):
                calls.append((name,args)); return {'rows':[{'group_path':['plants'],'text':'forest' if args['cursor'] is None else 'tree','rating':None}], 'next':1 if args['cursor'] is None else None}
        adapter=ReadOnlyAdapter('fixture-v1','fixture_list',{'type':'object'},lambda p:(p['rows'],p['next']))
        selection={'version':'fixture-v1','group_mapping':{'plants':'scene'}}
        result=await read_launcher_features(selection,client=Client(),adapter=adapter)
        self.assertEqual(len(result),2); self.assertTrue(all(c.rating is None for c in result)); self.assertEqual(len(calls),2)
        with self.assertRaises(PromptError): await read_launcher_features(dict(selection,version='unknown'),client=Client(),adapter=adapter)
        with self.assertRaises(PromptError): await read_launcher_features(selection,client=Client())
