from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from stylelora.project_db import create_project, close_project, open_project, read_snapshot, submit_write
from stylelora.project_db import commands as cmd
from stylelora.project_db.types import *
from stylelora.project_db.repositories import sources, images, annotations, jobs
from stylelora.project_db.receipts import prepare_receipt


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'test.slproj'
        self.project = create_project(self.root,name='Test',trigger_word='')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: close_project(self.project))

    def write(self, function):
        return submit_write(self.project,function).result(timeout=15)

    def read(self, function):
        with read_snapshot(self.project) as db:
            return function(db)

    def reopen(self):
        close_project(self.project)
        self.project = open_project(self.root)

    def source(self, count=1, kind='import', policy='ask', start=True):
        if kind == 'import':
            config = {'schema_version':1,'reader':'txt_sidecar','params':{'schema_version':1,'encoding':'utf-8-sig'},
                      'import_manifest':[f'in/{i}.png' for i in range(1,count+1)]}
        else:
            config = {'schema_version':1,'model':'nai','params':{'seed':123},'quality_toggle':False,'uc_preset':None,
                      'default_aspect':'square','dimensions':{'landscape':[768,512],'portrait':[512,768],'square':[512,512]}}
        source = self.write(lambda db: sources.create_source(db,SourceDraft(kind,'Source',config,policy,target_count=count)))
        seeds = tuple(ItemSeed(i,f'prompt {i}','square' if kind=='nai' else None) for i in range(1,count+1))
        if start:
            source = self.write(lambda db: cmd.start_source(db,source.source_id,seeds))
        return source,seeds

    def items(self, source):
        return self.read(lambda db: sources.list_items(db,ItemFilter(source.source_id),PageRequest()).items)

    def artifact(self, item, payload=b'image payload', originals=()):
        item = self.write(lambda db: cmd.mark_sent(db,item.item_id)).value
        path = self.root / 'images' / '.pending' / str(item.item_id) / str(item.attempts) / 'out.png'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(payload)
        value = ImageArtifact(item.item_id,item.attempts,path.relative_to(self.root).as_posix(),'.png',64,64,None,
                              hashlib.sha256(payload).hexdigest(),len(payload),None,'')
        return prepare_receipt(self.root,value,originals)

    def image(self, tags=(), payload=b'image payload', kind='import'):
        source,_ = self.source(kind=kind)
        item = self.items(source)[0]
        originals = (OriginalInput('tags',tuple(Tag(t) for t in tags)),) if tags else ()
        artifact = self.artifact(item,payload,originals)
        image = self.write(lambda db: cmd.complete_item(db,artifact,originals)).value
        return image

    def job(self, image_ids, executor='wd', mode='selected', batch=None, targets=None):
        from uuid import uuid4
        request = JobRequest(executor,mode,Scope(selected_ids=tuple(image_ids)))
        job = self.write(lambda db: jobs.create_batch(db,batch or uuid4().hex,(request,)))[0]
        snapshot = {'schema_version':1,'role':executor,'model':'test-model','protocol':'test','params':{'schema_version':1},
                    'target_types':targets or ['tags'],'skill_hash':'a'*64,'concurrency':2,'timeout':30,'retries':0,'routing':{}}
        return self.write(lambda db: jobs.freeze_job(db,job.job_id,snapshot,image_ids=image_ids)).value

    def outcome(self, job, image, tag='a', key='cache-a', types=None):
        values = []
        for kind in types or job.config_snapshot_json['target_types']:
            values.append(AnnotationInput(image.image_id,'wd' if job.executor=='wd' else 'llm',kind,
                          (Tag(tag,0.8),) if kind=='tags' else tag,model_name='test-model',
                          params={'schema_version':1,'requested':{},'actual':{'setting':1}},job_id=job.job_id,cache_key=key+'-'+kind))
        return JobOutcome(image.image_id,'done',tuple(values))

    def refs(self, image_id):
        def read(db):
            result = []
            for layer in ('original','wd','llm','manual','rule'):
                current = annotations.get_current(db,image_id,layer,'tags')
                result.append(CurrentRef(image_id,layer,'tags',current.annotation_id if current else None))
            return tuple(result)
        return self.read(read)

    def rule(self, image, tags):
        refs = self.refs(image.image_id)
        prior = next(r.annotation_id for r in refs if r.source=='rule')
        plan = RulePlan({'schema_version':1,'action':'replace'},Scope(selected_ids=(image.image_id,)),
                        (RuleImageChange(image.image_id,tuple(Tag(t) for t in tags),prior,{}),),refs,'')
        return replace(plan,preview_digest=cmd.rule_digest(plan))

    def count(self, table):
        return self.read(lambda db: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0])
