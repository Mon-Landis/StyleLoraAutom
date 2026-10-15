"""One atomic file per product task, serialized writes and explicit dispatch receipts."""
import os
import threading
from pathlib import Path
from uuid import uuid4
from .types import PromptError, plain
from .validation import identifier, strict_loads, require, integer, construct
from .dictionaries import _write, digest

LOCK=threading.RLock()
FIELDS={'schema_version','task_id','revision','status','generation','request_key','request_digest','snapshot',
        'snapshot_digest','outcomes','coverage','ready','counts','frozen','append_receipts'}
SNAPSHOT_FIELDS={'input','python_version','algorithm_version','dictionaries','features','candidates','candidate_ratings',
    'candidate_dimensions','candidate_complete','selected_count','bindings','templates','template_hashes','keywords',
    'keywords_digest','catalog','catalog_complete','catalog_translations','append_revision'}


class TaskStore:
    def __init__(self, root=None):
        self.root=Path(root or os.environ.get('APPDATA',Path.home()))/'StyleLoraAutom'/'prompt_tasks'
        self.lock=LOCK

    def path(self, task_id):
        return self.root/(identifier(task_id)+'.json')

    def read(self, task_id):
        try: data=strict_loads(self.path(task_id).read_bytes())
        except FileNotFoundError: raise PromptError('task:not_found') from None
        except (OSError,PromptError): raise PromptError('storage:corrupt_snapshot') from None
        require(isinstance(data,dict) and set(data)==FIELDS and type(data['schema_version']) is int and data['schema_version']==1,'storage:corrupt_snapshot')
        require(isinstance(data['snapshot'],dict) and set(data['snapshot']) in
                (SNAPSHOT_FIELDS, SNAPSHOT_FIELDS | {'profile', 'formatter'}),'storage:corrupt_snapshot')
        require(digest(data['snapshot'])==data['snapshot_digest'],'storage:corrupt_snapshot')
        require(data['task_id']==task_id and digest(data['snapshot']['input'])==data['request_digest'],'storage:corrupt_snapshot')
        snapshot=data['snapshot']
        require(digest(snapshot['keywords'])==snapshot['keywords_digest'] and
            {k:digest(v) for k,v in snapshot['templates'].items()}==snapshot['template_hashes'],'storage:corrupt_snapshot')
        require(len(snapshot['candidates'])==len(snapshot['candidate_ratings'])==len(snapshot['candidate_dimensions'])==len(data['outcomes']),'storage:corrupt_snapshot')
        from .tasks import request_from
        request_from(snapshot['input'])
        integer(data['revision'],1); integer(data['generation'])
        require(data['status'] in ('pending','running','paused','done','failed','canceled'),'storage:corrupt_snapshot')
        require(type(data['ready']) is bool and isinstance(data['outcomes'],list),'storage:corrupt_snapshot')
        from .types import TaskOutcome, PromptEntry, CoverageReport, RatingDecision, CompositionRequest
        for entry,rating,dimensions in zip(snapshot['candidates'],snapshot['candidate_ratings'],snapshot['candidate_dimensions']):
            construct(PromptEntry,entry); construct(RatingDecision,rating); CompositionRequest(dimensions)
        for i,row in enumerate(data['outcomes']):
            require(set(row) in ({'outcome','generation','dispatch_revision'},
                                {'outcome','generation','dispatch_revision','format_result'}),'storage:corrupt_snapshot')
            if row.get('format_result') is not None:
                formatted = row['format_result']
                require(set(formatted) == {'entry','formatter_id','formatter_version','input_digest','output_digest'}, 'storage:corrupt_snapshot')
                require(digest(formatted['entry']) == formatted['output_digest'] and formatted['entry'] == row['outcome']['entry'], 'storage:corrupt_snapshot')
            outcome=dict(row['outcome'])
            if outcome['entry'] is not None: outcome['entry']=construct(PromptEntry,outcome['entry'])
            dto=construct(TaskOutcome,outcome)
            require(dto.task_id==task_id and dto.index==i,'storage:corrupt_snapshot')
        construct(CoverageReport,data['coverage'])
        require(data['counts']==self.counts(data),'storage:corrupt_snapshot')
        return data

    @staticmethod
    def counts(data):
        return {s:sum(r['outcome']['status']==s for r in data['outcomes']) for s in ('pending','running','ok','skipped','failed')}

    def write(self, data, *, expected_revision):
        with LOCK:
            current=self.read(data['task_id'])
            require(current['revision']==expected_revision,'seeds:state_conflict')
            require(current['snapshot']==data['snapshot'],'storage:corrupt_snapshot')
            data=plain(data); data['revision']=expected_revision+1; data['counts']=self.counts(data)
            try: _write(self.path(data['task_id']),data)
            except OSError: raise PromptError('storage:io_error') from None
            return data

    def find_request(self, request_key, request_digest):
        with LOCK:
            for path in sorted(self.root.glob('*.json')):
                data=self.read(path.stem)
                if data['request_key']==request_key:
                    require(data['request_digest']==request_digest,'dictionary:request_key_conflict')
                    return data
        return None

    def create(self, snapshot, coverage, *, request_key):
        with LOCK:
            signature=digest(snapshot['input']); old=self.find_request(request_key,signature)
            if old: return old
            task_id=str(uuid4())
            data={'schema_version':1,'task_id':task_id,'revision':1,'status':'pending','generation':0,
                'request_key':request_key,'request_digest':signature,'snapshot':plain(snapshot),'outcomes':[],
                'snapshot_digest':digest(snapshot),
                'coverage':plain(coverage),'ready':False,'counts':{},'frozen':None,'append_receipts':{}}
            from .types import TaskOutcome
            data['outcomes']=[{'outcome':plain(TaskOutcome(task_id,i)), 'generation':None,'dispatch_revision':None} for i in range(len(snapshot['candidates']))]
            data['counts']=self.counts(data)
            try: _write(self.path(task_id),data)
            except OSError: raise PromptError('storage:io_error') from None
            return data

    def dispatch(self, task_id, index):
        with LOCK:
            data=self.read(task_id); require(data['status']=='running','seeds:state_conflict')
            row=data['outcomes'][index]; require(row['outcome']['status']=='pending','seeds:state_conflict')
            row['outcome']['status']='running'; row['generation']=data['generation']; row['dispatch_revision']=data['revision']+1
            return self.write(data,expected_revision=data['revision'])

    def commit_outcome(self, outcome, *, generation, dispatch_revision, format_result=None):
        with LOCK:
            data=self.read(outcome.task_id); row=data['outcomes'][outcome.index]
            if row['outcome']['status'] in ('ok','skipped','failed'): return data
            if data['status']=='canceled' or data['generation']!=generation: return data
            require(data['status'] in ('running','paused') and row['outcome']['status']=='running' and row['dispatch_revision']==dispatch_revision,'seeds:state_conflict')
            require(outcome.status in ('ok','skipped','failed'))
            row['outcome']=plain(outcome)
            if format_result is not None and outcome.entry is not None:
                row['format_result'] = plain(format_result)
            return self.write(data,expected_revision=data['revision'])

    def recover(self):
        recovered=[]
        with LOCK:
            for path in sorted(self.root.glob('*.json')):
                data=self.read(path.stem)
                if data['status'] not in ('running','paused'): continue
                changed=data['status']=='running'; data['status']='paused'
                for row in data['outcomes']:
                    if row['outcome']['status']=='running':
                        changed=True; row['outcome'].update(status='failed',reason_code='llm:outcome_unknown',entry=None)
                if changed:
                    data['generation']+=1; recovered.append(self.write(data,expected_revision=data['revision'])['task_id'])
        return tuple(recovered)

    def delete(self, task_id, *, expected_revision):
        with LOCK:
            data=self.read(task_id); require(data['revision']==expected_revision,'seeds:state_conflict')
            require(data['status']!='running','seeds:state_conflict')
            self.path(task_id).unlink()
