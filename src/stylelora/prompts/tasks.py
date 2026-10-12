"""Product task coordinator. Every external call follows a persisted dispatch receipt."""
import asyncio
import platform
from dataclasses import replace
from uuid import uuid4
from stylelora.llm.types import LlmFailure
from stylelora.tagging.catalog import LocalCatalog
from .types import *
from .validation import require, plain, construct, skill_text, validate_content, tag_key
from .sampling import sample, select
from .coverage import evaluate, classify
from .composer import refine
from .routing import resolve_rating, classify_rating, fenced
from .seeds import freeze_entries
from .task_store import TaskStore
from .dictionaries import load_dictionary, append_entries, digest


def request_from(data):
    data=dict(data); plan=dict(data['plan'])
    plan['constraints']=tuple(construct(ConstraintSpec,c) for c in plan['constraints'])
    plan['exclude']=tuple(construct(Predicate,p) for p in plan['exclude'])
    data['plan']=construct(CoveragePlan,plan); data['options']=construct(CompositionOptions,data['options']); data['fence']=construct(FencePolicy,data['fence'])
    return construct(SamplingRequest,data)


def outcome_from(data):
    data=dict(data)
    if data['entry'] is not None: data['entry']=construct(PromptEntry,data['entry'])
    return construct(TaskOutcome,data)


def view(data):
    return TaskView(data['task_id'],data['revision'],data['status'],tuple(outcome_from(r['outcome']) for r in data['outcomes']),construct(CoverageReport,data['coverage']),data['ready'])


class TaskService:
    def __init__(self, *, root=None, llm=None, catalog=None, keywords=None, templates=None, launcher=None):
        self.root=root; self.store=TaskStore(root); self.llm=llm; self.catalog=catalog or LocalCatalog()
        self.keywords=dict(keywords or {}); self.templates=templates or {'sfw':skill_text()}; self.launcher=launcher
        self._bindings={}; self._running=set()
        self._gate=asyncio.Semaphore(2)

    async def create_task(self, request, *, request_key, dictionaries=None, features=None):
        require(isinstance(request,SamplingRequest) and isinstance(request_key,str) and bool(request_key))
        # Request replay is checked before dictionary/model access: saved results stand alone.
        input_value=plain(request)
        old=self.store.find_request(request_key,digest(input_value))
        if old: return view(old)
        dictionaries=tuple(dictionaries) if dictionaries is not None else tuple(load_dictionary(i,root=self.root) for i in sorted(set(request.prompt_dictionary_ids)))
        require(all(isinstance(d,PromptDictionary) for d in dictionaries))
        feature_dicts=[]
        if features is not None: feature_dicts.append(features)
        feature_dicts.extend(load_dictionary(i,root=self.root) for i in sorted(set(request.feature_dictionary_ids)))
        if request.live_features and request.launcher_selection is not None:
            require(self.launcher is not None,'launcher:unsupported_schema')
            candidates=await self.launcher(request.launcher_selection); dims={}
            for c in candidates: dims.setdefault(c.dimension,[]).append(c)
            feature_dicts.append(FeatureDictionary(str(uuid4()),'Frozen Launcher',dimensions=dims))
        merged={}
        for dictionary in feature_dicts:
            require(isinstance(dictionary,FeatureDictionary))
            for dim,candidates in dictionary.dimensions.items():
                for c in candidates:
                    old=merged.setdefault(dim,{}).get(c.tag)
                    if old is None or (c.rating is not None and (old.rating is None or RATINGS.index(c.rating)>RATINGS.index(old.rating))): merged[dim][c.tag]=c
        features=FeatureDictionary(str(uuid4()),'Frozen features',dimensions={d:tuple(cs[k] for k in sorted(cs)) for d,cs in merged.items()}) if merged else None
        result=sample(request,dictionaries=dictionaries,features=features,catalog=self.catalog,keywords=self.keywords)
        bindings={}
        for role in ('prompt','prompt_nsfw','rating'):
            bindings[role]=await self.llm.resolve_role(role) if self.llm and (request.optimize or role=='rating' and request.fence.use_rating_model) else None
        # Only public catalog data, not a client or endpoint, is persisted.
        require(isinstance(self.catalog,LocalCatalog),'catalog:snapshot_required')
        catalog_data={'schema_version':1,'kind':'tag_catalog','revision':1,'tags':[
            {'tag':tag,'category':category,'aliases':[a for a,k in self.catalog.aliases.items() if k==tag]}
            for tag,category in sorted(self.catalog.entries.items())]} if self.catalog.available else None
        ordered=result.entries+tuple(e for e in result.candidates if tag_key(e) not in {tag_key(x) for x in result.entries})
        snapshot={'input':input_value,'python_version':platform.python_version(),'algorithm_version':request.plan.algorithm_version,
            'dictionaries':[plain(d) for d in dictionaries],'features':plain(features),'candidates':plain(ordered),
            'candidate_ratings':[plain(result.ratings[tag_key(e)]) for e in ordered],
            'candidate_dimensions':[plain(result.dimensions[tag_key(e)]) for e in ordered],
            'candidate_complete':result.complete,'selected_count':len(result.entries),'bindings':plain(bindings),
            'templates':dict(self.templates),'template_hashes':{k:digest(v) for k,v in self.templates.items()},
            'keywords':self.keywords,'keywords_digest':digest(self.keywords),'catalog':catalog_data,
            'catalog_complete':self.catalog.complete,'catalog_translations':dict(self.catalog.translations),
            'append_revision':load_dictionary(request.append_target_id,root=self.root).revision if request.append_target_id else None}
        data=self.store.create(snapshot,result.report,request_key=request_key); self._bindings[data['task_id']]=bindings
        return view(data)

    def get_task(self, task_id): return view(self.store.read(task_id))

    async def control_task(self, task_id, action, *, expected_revision):
        require(action in ('pause','resume','cancel'))
        with self.store.lock:
            data=self.store.read(task_id); require(data['revision']==expected_revision,'seeds:state_conflict')
            target={'pause':'paused','resume':'running','cancel':'canceled'}[action]
            if data['status']==target: return view(data)
            require(data['status'] not in ('done','failed','canceled'),'seeds:state_conflict')
            if action=='resume': require(data['status']=='paused','seeds:state_conflict')
            data['status']=target
            if action=='cancel': data['generation']+=1; data['ready']=False
            return view(self.store.write(data,expected_revision=expected_revision))

    async def _restore_bindings(self,data):
        task_id=data['task_id']
        if task_id in self._bindings: return self._bindings[task_id]
        result={}
        for role,snapshot in data['snapshot']['bindings'].items():
            binding=await self.llm.resolve_role(role) if self.llm and snapshot else None
            if plain(binding)!=snapshot: raise LlmFailure('llm:binding_unavailable',outcome='not_sent')
            result[role]=binding
        self._bindings[task_id]=result; return result

    async def run(self,task_id):
        require(task_id not in self._running,'seeds:state_conflict')
        self._running.add(task_id)
        try:
            async with self._gate:
                return await self._run(task_id)
        finally: self._running.discard(task_id)

    async def _run(self,task_id):
        data=self.store.read(task_id)
        if data['status'] in ('done','failed','canceled'): return view(data)
        require(data['status'] in ('pending','running'),'seeds:state_conflict')
        try: bindings=await self._restore_bindings(data)
        except LlmFailure:
            data['status']='paused'; return view(self.store.write(data,expected_revision=data['revision']))
        snapshot=data['snapshot']; request=request_from(snapshot['input'])
        catalog=LocalCatalog(snapshot['catalog'],snapshot['catalog_translations'],complete=snapshot['catalog_complete'])
        candidates=tuple(construct(PromptEntry,e) for e in snapshot['candidates'])
        if data['status']=='pending':
            data['status']='running'; data['generation']+=1; data=self.store.write(data,expected_revision=data['revision'])
        # One initial batch plus at most two deterministic refill batches.
        n=request.plan.target_count; max_dispatch=min(len(candidates),3*n)
        for index in range(max_dispatch):
            data=self.store.read(task_id)
            if data['status']!='running': return view(data)
            accepted=[outcome_from(r['outcome']).entry for r in data['outcomes'] if r['outcome']['status'] in ('ok','skipped') and r['outcome']['entry']]
            selected,report=select(request,accepted,complete=snapshot['candidate_complete'])
            if report.status=='satisfied': break
            if data['outcomes'][index]['outcome']['status']!='pending': continue
            data=self.store.dispatch(task_id,index); row=data['outcomes'][index]; generation=row['generation']; dispatched=row['dispatch_revision']
            entry=candidates[index]
            try:
                rating=construct(RatingDecision,snapshot['candidate_ratings'][index])
                if rating.rating is None and request.fence.use_rating_model and self.llm and bindings['rating'] is not None:
                    rating=await resolve_rating(entry,request.fence,self.llm,keywords=snapshot['keywords'],binding=bindings['rating'])
                opts=request.options
                composition=CompositionRequest(snapshot['candidate_dimensions'][index],nl_enabled=opts.nl_enabled,composition_mode=opts.composition_mode,tag_range=opts.tag_range,aspect_hint=entry.aspect or opts.aspect_hint)
                outcome=await refine(entry,composition,task_id=task_id,index=index,rating=rating,llm=self.llm,policy=request.fence,
                    optimize=request.optimize,catalog=catalog,keywords=snapshot['keywords'],templates=snapshot['templates'],bindings=bindings)
                if outcome.entry is not None and outcome.status in ('ok','skipped'):
                    validate_content(outcome.entry,catalog=catalog)
                    if request.coverage_enabled and classify(request.plan,entry)!=classify(request.plan,outcome.entry):
                        outcome=TaskOutcome(task_id,index,'failed','sampling:coverage_drift')
                    elif tag_key(outcome.entry,catalog) in {tag_key(e,catalog) for e in accepted}:
                        outcome=TaskOutcome(task_id,index,'failed','sampling:duplicate')
            except LlmFailure as exc:
                outcome=TaskOutcome(task_id,index,'failed','llm:outcome_unknown' if exc.outcome=='unknown' else exc.code)
            except PromptError as exc: outcome=TaskOutcome(task_id,index,'failed',exc.code)
            data=self.store.commit_outcome(outcome,generation=generation,dispatch_revision=dispatched)
            if outcome.reason_code in ('llm:unauthorized','llm:binding_unavailable','llm:model_mismatch','llm:secret_missing','llm:outcome_unknown') and data['status']=='running':
                data['status']='paused'; return view(self.store.write(data,expected_revision=data['revision']))
        data=self.store.read(task_id)
        if data['status']!='running': return view(data)
        accepted=[outcome_from(r['outcome']).entry for r in data['outcomes'] if r['outcome']['status'] in ('ok','skipped') and r['outcome']['entry']]
        selected,report=select(request,accepted,complete=snapshot['candidate_complete'] and max_dispatch==len(candidates))
        for row in data['outcomes']:
            if row['outcome']['status']=='pending': row['outcome'].update(status='skipped',reason_code='sampling:not_selected',entry=None)
        data['coverage']=plain(report); data['status']='done'; data['ready']=report.status=='satisfied'
        if data['ready']: data['frozen']=plain(freeze_entries(selected,n,report=report))
        data=self.store.write(data,expected_revision=data['revision'])
        if request.append_target_id:
            # Failure does not discard the completed task; caller can re-preview conflicts.
            try: self.append_results(task_id,request.append_target_id,expected_revision=snapshot['append_revision'])
            except PromptError: pass
        return self.get_task(task_id)

    def freeze_seeds(self,task_id,*,expected_revision):
        data=self.store.read(task_id); require(data['revision']==expected_revision,'seeds:state_conflict'); require(data['ready'] and data['frozen'] is not None,'seeds:not_ready')
        frozen=data['frozen']; items=tuple(construct(ItemSeed,i) for i in frozen['items'])
        require(digest(items)==frozen['digest'],'storage:corrupt_snapshot')
        return FrozenSeeds(frozen['digest'],items,frozen['count'])

    def append_results(self,task_id,dictionary_id,*,expected_revision,indices=None):
        data=self.store.read(task_id)
        outcomes=[outcome_from(r['outcome']) for r in data['outcomes']]
        entries=tuple(o.entry for o in outcomes if o.status=='ok' and o.entry is not None and (indices is None or o.index in indices))
        require(bool(entries),'seeds:not_ready')
        key=task_id+':'+digest({'dictionary_id':dictionary_id,'entries':entries})
        result=append_entries(dictionary_id,entries,expected_revision=expected_revision,request_key=key,root=self.root)
        if not result.conflicts:
            with self.store.lock:
                data=self.store.read(task_id); data['append_receipts'][key]=plain(result); self.store.write(data,expected_revision=data['revision'])
        return result


PromptTask=TaskService
_default=None
def default_service():
    global _default
    if _default is None: _default=TaskService()
    return _default
async def create_task(request,*,request_key): return await default_service().create_task(request,request_key=request_key)
def get_task(task_id): return default_service().get_task(task_id)
async def control_task(task_id,action,*,expected_revision): return await default_service().control_task(task_id,action,expected_revision=expected_revision)
def freeze_seeds(task_id,*,expected_revision): return default_service().freeze_seeds(task_id,expected_revision=expected_revision)
