import asyncio
import json
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from uuid import uuid4
from unittest.mock import patch
from stylelora.prompts import *
from stylelora.prompts.routing import fenced, refused
from stylelora.prompts.task_store import TaskStore
from stylelora.prompts.dictionaries import digest
from stylelora.prompts.seeds import deliver
from stylelora.llm.types import RoleBinding, JsonReply, LlmReply, LlmFailure
from test_f06_prompts import plan, dictionary, catalog


def result(tags=('forest',), **kwargs):
    data={'status':'ok','tags':list(tags),'natural_language':None,'out_of_frame':[],'added':[],'dropped':[],'aspect':'portrait'}
    data.update(kwargs); return data
def refusal(): return result((),status='refused_nsfw',aspect=None,reason='Refused.')


class Fake:
    def __init__(self,replies=(),*,roles=('prompt','prompt_nsfw','rating'),protocol='openai_chat'):
        self.replies=list(replies); self.roles=roles; self.protocol=protocol; self.calls=[]; self.active=0; self.maximum=0; self.gate=asyncio.Semaphore(2)
    async def resolve_role(self,role): return RoleBinding('fake',role,'model',self.protocol,1) if role in self.roles else None
    async def complete_json(self,request,*,repair_limit=1):
        async with self.gate:
            self.active+=1; self.maximum=max(self.maximum,self.active); self.calls.append(request)
            try:
                await asyncio.sleep(0)
                value=self.replies.pop(0) if self.replies else result(tuple(request.payload['features']['content']))
                if isinstance(value,Exception): raise value
                if callable(value): value=await value(request)
                text=value if isinstance(value,str) else json.dumps(value)
                return JsonReply(json.loads(text) if not isinstance(value,str) else {},LlmReply(text,'model',request.request_id),False)
            finally: self.active-=1


class ValidationTests(unittest.TestCase):
    def test_T08_contract_and_content(self):
        req=CompositionRequest({'scene':('forest',)},nl_enabled=False,tag_range=(1,10))
        self.assertEqual(validate_composer_result(json.dumps(result()),req).status,'ok')
        self.assertEqual(validate_composer_result(json.dumps(refusal()),req).status,'refused_nsfw')
        invalid=[dict(result(),reason='bad'),dict(result(),aspect=None),dict(result(),extra=True),dict(result(),tags=['watercolor'],added=['watercolor'],dropped=[{'tag':'forest','reason':'x'}]),dict(result(),out_of_frame=['forest'])]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(PromptError): validate_composer_result(json.dumps(value),req)
        for text in ('```json\n{}\n```','{"status":"ok","status":"ok"}','NaN'):
            with self.assertRaises(PromptError): validate_composer_result(text,req)
        for tag in ('soft brushstrokes','dreamy atmosphere','masterpiece','(forest:1.2)','test artist'):
            with self.assertRaises(PromptError): validate_content(PromptEntry((tag,)),catalog=catalog())
        validate_content(PromptEntry(('gradient hair','bow (weapon)')),catalog=catalog())

    def test_T09_rating_boundaries_and_lower_bound(self):
        policy=FencePolicy()
        for level in ('general','sensitive','questionable','explicit'):
            decision=classify_rating(PromptEntry(('forest',),rating=level),policy)
            self.assertEqual(fenced(decision,policy),level in ('questionable','explicit'))
            self.assertTrue(decision.verified)
        decision=classify_rating(PromptEntry(('forest',)),policy,ratings=('explicit',None),keywords={'forest':'general'})
        self.assertEqual(decision.rating,'explicit'); self.assertFalse(decision.verified)
        self.assertEqual(refused(decision).rating,'explicit')
        unknown=classify_rating(PromptEntry(('forest',)),FencePolicy(nsfwfallback=True))
        self.assertEqual(unknown.route,'nsfw'); self.assertFalse(unknown.verified); self.assertTrue(fenced(unknown,FencePolicy()))

    def test_T13_dedup_aspect_projection(self):
        entries=[PromptEntry(('forest','tree'),'A tree.','landscape'),PromptEntry(('tree','forest'),'Another tree.','portrait')]
        with self.assertRaises(PromptError): to_item_seeds(entries)
        self.assertEqual(to_item_seeds(entries[:1])[0].aspect,'landscape')
        self.assertIsNone(to_item_seeds([PromptEntry(('forest',))])[0].aspect)


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    async def call(self,fake,*,entry=None,policy=None,rating=None,templates=None):
        entry=entry or PromptEntry(('forest',))
        return await refine(entry,CompositionRequest({'content':entry.tags},nl_enabled=False,tag_range=(1,10)),task_id=str(uuid4()),index=0,
            rating=rating or classify_rating(entry,policy or FencePolicy()),llm=fake,policy=policy,templates=templates)

    async def test_T10_empty_missing_template_reroute(self):
        fake=Fake(roles=()); out=await self.call(fake); self.assertEqual(out.reason_code,'composer:role_empty'); self.assertIsNotNone(out.entry)
        fake=Fake(); out=await self.call(fake,entry=PromptEntry(('forest',),rating='explicit'),policy=FencePolicy(enabled=False))
        self.assertEqual(out.reason_code,'composer:nsfw_contract_missing'); self.assertFalse(fake.calls)
        fake=Fake([refusal(),refusal()]); out=await self.call(fake,policy=FencePolicy(enabled=False),templates={'sfw':'sfw contract','nsfw':'nsfw contract'})
        self.assertEqual(out.reason_code,'composer:refusal_repeated'); self.assertEqual(len(fake.calls),2)
        self.assertEqual([c.payload['rating_policy'] for c in fake.calls],['sfw','nsfw'])
        fake=Fake([refusal()]); out=await self.call(fake); self.assertEqual(out.reason_code,'routing:fenced'); self.assertEqual(len(fake.calls),1)

    async def test_T11_protocol_repair_invalid_unknown(self):
        for protocol in ('openai_chat','openai_responses','anthropic_messages'):
            fake=Fake([dict(result(),aspect=None),result()],protocol=protocol)
            out=await self.call(fake); self.assertEqual(out.status,'ok'); self.assertEqual(len(fake.calls),2); self.assertEqual(fake.calls[0].binding.protocol,protocol)
        fake=Fake([dict(refusal(),status='invalid_input')]); out=await self.call(fake); self.assertEqual(out.reason_code,'composer:invalid_input'); self.assertEqual(len(fake.calls),1)
        fake=Fake([LlmFailure('llm:timeout',outcome='unknown')]); out=await self.call(fake); self.assertEqual(out.reason_code,'llm:outcome_unknown'); self.assertEqual(len(fake.calls),1)
        fake=Fake([LlmFailure('llm:rate_limited')]); await self.call(fake); self.assertEqual(len(fake.calls),1)

    async def test_T11_service_gate_and_aspect_priority(self):
        fake=Fake(); await asyncio.gather(*(self.call(fake) for _ in range(8))); self.assertLessEqual(fake.maximum,2)
        fake=Fake([result(aspect='landscape')]); out=await self.call(fake,entry=PromptEntry(('forest',),aspect='landscape'))
        self.assertEqual(out.entry.aspect,'landscape'); self.assertEqual(fake.calls[0].payload['aspect_hint'],'landscape')

    async def test_T09_rating_model_uncertain(self):
        fake=Fake([{'rating':'uncertain'}]); decision=await resolve_rating(PromptEntry(('forest',)),FencePolicy(use_rating_model=True),fake)
        self.assertIsNone(decision.rating); self.assertFalse(decision.verified)


class TaskTests(unittest.IsolatedAsyncioTestCase):
    def request(self,n=1,optimize=False): return SamplingRequest(plan(n),coverage_enabled=False,optimize=optimize,options=CompositionOptions(nl_enabled=False,tag_range=(1,10)))

    async def test_T12_T15_atomic_outcome_replay_recovery(self):
        with TemporaryDirectory() as root:
            service=TaskService(root=root); request=self.request(); d=dictionary([PromptEntry(('forest',))])
            task=await service.create_task(request,request_key='key',dictionaries=(d,))
            self.assertEqual((await service.create_task(request,request_key='key')).task_id,task.task_id)
            with self.assertRaises(PromptError): await service.create_task(self.request(2),request_key='key')
            data=service.store.read(task.task_id); data['status']='running'; data['generation']=1; data=service.store.write(data,expected_revision=data['revision'])
            sent=service.store.dispatch(task.task_id,0); receipt=sent['outcomes'][0]
            service.store.recover(); recovered=service.get_task(task.task_id)
            self.assertEqual(recovered.status,'paused'); self.assertEqual(recovered.outcomes[0].reason_code,'llm:outcome_unknown')
            outcome=TaskOutcome(task.task_id,0,'ok',entry=PromptEntry(('forest',)))
            before=service.store.read(task.task_id)
            service.store.commit_outcome(outcome,generation=1,dispatch_revision=receipt['dispatch_revision'])
            self.assertEqual(service.store.read(task.task_id),before)
            self.assertNotIn('project_id',str(before)); self.assertNotIn('source_id',str(before))

    async def test_T12_pause_cancel_late_result(self):
        for action in ('pause','cancel'):
            with TemporaryDirectory() as root:
                service=TaskService(root=root)
                task=await service.create_task(self.request(),request_key='k',dictionaries=(dictionary([PromptEntry(('forest',))]),))
                data=service.store.read(task.task_id); data['status']='running'; data['generation']=1; service.store.write(data,expected_revision=data['revision'])
                sent=service.store.dispatch(task.task_id,0); receipt=sent['outcomes'][0]
                await service.control_task(task.task_id,action,expected_revision=sent['revision'])
                outcome=TaskOutcome(task.task_id,0,'ok',entry=PromptEntry(('forest',)))
                committed=service.store.commit_outcome(outcome,generation=1,dispatch_revision=receipt['dispatch_revision'])
                self.assertEqual(committed['outcomes'][0]['outcome']['status'],'ok' if action=='pause' else 'running')
                self.assertEqual(service.store.commit_outcome(outcome,generation=1,dispatch_revision=receipt['dispatch_revision']),committed)

    async def test_T13_refill_and_not_ready(self):
        with TemporaryDirectory() as root:
            fake=Fake([dict(refusal(),status='invalid_input'),result(('tree',)),result(('rock',))])
            service=TaskService(root=root,llm=fake)
            task=await service.create_task(self.request(optimize=True),request_key='k',dictionaries=(dictionary([PromptEntry(('forest',)),PromptEntry(('tree',)),PromptEntry(('rock',))]),))
            # Fake returns each dispatched input after the first semantic failure.
            async def echo(req): return result(tuple(req.payload['features']['content']))
            fake.replies=[dict(refusal(),status='invalid_input'),echo]
            done=await service.run(task.task_id); self.assertTrue(done.ready); self.assertEqual(len(fake.calls),2)
            frozen=service.freeze_seeds(task.task_id,expected_revision=done.revision); self.assertEqual(frozen.count,1)
            self.assertEqual((await service.run(task.task_id)).revision,done.revision)
        with TemporaryDirectory() as root:
            service=TaskService(root=root); task=await service.create_task(self.request(2),request_key='short',dictionaries=(dictionary([PromptEntry(('forest',))]),))
            done=await service.run(task.task_id); self.assertFalse(done.ready)
            with self.assertRaises(PromptError): service.freeze_seeds(task.task_id,expected_revision=done.revision)

    async def test_T14_frozen_delivery_and_no_resampling(self):
        with TemporaryDirectory() as root:
            service=TaskService(root=root); task=await service.create_task(self.request(2),request_key='k',dictionaries=(dictionary([PromptEntry(('forest',)),PromptEntry(('tree',),aspect='landscape')]),))
            done=await service.run(task.task_id); self.assertTrue(done.ready)
            reopened=TaskService(root=root); frozen=reopened.freeze_seeds(task.task_id,expected_revision=done.revision)
            calls=[]
            async def start(project,source,items): calls.append(items); return items
            await deliver(frozen,None,1,start=start)
            self.assertEqual([i.seq for i in calls[0]],[1,2]); self.assertEqual(len(calls),1)
            self.assertTrue((await reopened.run(task.task_id)).ready)

    async def test_T12_unknown_pauses_without_resend(self):
        with TemporaryDirectory() as root:
            fake=Fake([LlmFailure('llm:timeout',outcome='unknown')]); service=TaskService(root=root,llm=fake)
            task=await service.create_task(self.request(optimize=True),request_key='k',dictionaries=(dictionary([PromptEntry(('forest',))]),))
            paused=await service.run(task.task_id); self.assertEqual(paused.status,'paused')
            await service.control_task(task.task_id,'resume',expected_revision=paused.revision)
            done=await service.run(task.task_id); self.assertFalse(done.ready); self.assertEqual(len(fake.calls),1)

    async def test_T12_append_two_file_crash_window(self):
        with TemporaryDirectory() as root:
            target=dictionary([]); save_dictionary(target,expected_revision=0,request_key='create',root=root)
            fake=Fake(); service=TaskService(root=root,llm=fake)
            task=await service.create_task(self.request(optimize=True),request_key='k',dictionaries=(dictionary([PromptEntry(('forest',))]),))
            await service.run(task.task_id)
            with patch.object(service.store,'write',side_effect=PromptError('storage:io_error')),self.assertRaises(PromptError):
                service.append_results(task.task_id,target.dictionary_id,expected_revision=1)
            self.assertEqual(len(load_dictionary(target.dictionary_id,root=root).entries),1)
            result=service.append_results(task.task_id,target.dictionary_id,expected_revision=1)
            self.assertEqual(result.inserted,1); self.assertEqual(len(load_dictionary(target.dictionary_id,root=root).entries),1)

    async def test_T12_unavailable_frozen_binding_and_corruption(self):
        with TemporaryDirectory() as root:
            service=TaskService(root=root,llm=Fake())
            task=await service.create_task(self.request(optimize=True),request_key='k',dictionaries=(dictionary([PromptEntry(('forest',))]),))
            reopened=TaskService(root=root,llm=Fake(roles=()))
            paused=await reopened.run(task.task_id); self.assertEqual(paused.status,'paused'); self.assertFalse(reopened.llm.calls)
            path=service.store.path(task.task_id); obj=json.loads(path.read_text(encoding='utf8')); obj['snapshot']['candidates'][0]['tags']=['changed']
            path.write_text(json.dumps(obj),encoding='utf8')
            with self.assertRaises(PromptError): service.get_task(task.task_id)

    async def test_T13_coverage_drift_refill(self):
        with TemporaryDirectory() as root:
            async def drift(req):
                inputs=req.payload['features']['content']; tags=[t for t in inputs if t!='forest']+['beach']
                return result(tags,added=['beach'],dropped=[{'tag':'forest','reason':'changed'}])
            async def echo(req): return result(req.payload['features']['content'])
            fake=Fake([drift,echo]); service=TaskService(root=root,llm=fake)
            request=replace(self.request(optimize=True),coverage_enabled=True)
            task=await service.create_task(request,request_key='k',dictionaries=(dictionary([PromptEntry(('forest','sunlight','tree')),PromptEntry(('forest','sunlight','rock'))]),))
            done=await service.run(task.task_id); self.assertTrue(done.ready)
            self.assertEqual(done.outcomes[0].reason_code,'sampling:coverage_drift'); self.assertEqual(len(fake.calls),2)


from support import ProjectCase, cmd


class F01HandoffTests(ProjectCase):
    def test_T14_atomic_source_start_replay_and_resume(self):
        source,_=self.source(count=2,kind='nai',start=False)
        frozen=freeze_entries((PromptEntry(('forest',)),PromptEntry(('tree',),aspect='landscape')),2)
        self.write(lambda db:cmd.start_source(db,source.source_id,frozen.items))
        self.write(lambda db:cmd.start_source(db,source.source_id,frozen.items))
        items=self.items(source)
        self.assertEqual([(x.seq,x.prompt,x.aspect) for x in items],[(x.seq,x.prompt,x.aspect) for x in frozen.items])
        self.assertEqual(self.count('jobs'),0)
        self.write(lambda db:cmd.transition_source(db,source.source_id,expected='running',target='paused',reason='test'))
        self.write(lambda db:cmd.transition_source(db,source.source_id,expected='paused',target='running',reason='test'))
        self.assertEqual(self.items(source),items)
