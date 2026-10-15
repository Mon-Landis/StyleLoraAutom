"""Own accepted work through completion, including uncancelable file workers."""
import asyncio
from uuid import uuid4
from .contracts import Failure, plain


class TaskSupervisor:
    def __init__(self, events):
        self.events, self.records, self.tasks = events, {}, {}
        self.draining = False
        self.failures = {}
        self.cancel_handles = {}
        self.product = None
        self.project_scopes = {}

    def attach_store(self, product):
        from stylelora.llm.config import read_json
        self.product = product
        for record in read_json(product.root/'operations.json', {}).values():
            if record['kind'] not in ('export', 'cleanup', 'model_download'):
                continue
            if record['status'] in ('pending', 'running', 'canceling'):
                record['status'] = 'paused'
                record['stop_reason'] = 'shutdown'
            record.setdefault('result', None)
            self.records[record['operation_id']] = record

    def persist(self):
        if self.product is None:
            return
        from stylelora.llm.config import atomic_json
        records = {}
        for identity, record in self.records.items():
            if record['kind'] in ('export', 'cleanup', 'model_download'):
                records[identity] = {k:v for k,v in record.items() if k != 'result'}
        atomic_json(self.product.root/'operations.json', records)

    def submit(self, kind, work, *, project_id=None, cancel=None, resource_id=None):
        if self.draining:
            raise Failure(503, 'process:draining')
        identity = str(uuid4())
        record = dict(operation_id=identity, kind=kind, status='pending', completed=0,
                      total=None, result=None, error=None, version='0', project_id=project_id,
                      project_scope=self.project_scopes.get(project_id))
        record['resource_id'] = str(resource_id) if resource_id is not None else identity
        self.records[identity] = record
        if kind in ('export', 'cleanup', 'model_download'):
            self.persist()
        if cancel is not None:
            self.cancel_handles[identity] = cancel

        async def execute():
            record.update(status='running', version='1')
            try:
                record['result'] = plain(await work())
                record['status'] = 'paused' if isinstance(record['result'], dict) and record['result'].get('stopped') else 'done'
            except asyncio.CancelledError:
                record['status'] = 'canceled'
            except Exception as exc:
                self.failures[identity] = exc
                record['status'] = 'failed'
                from stylelora.api.errors import translate
                record['error'] = {'code': translate(exc).code}
            finally:
                if record.get('stop_reason') and record['status'] != 'done':
                    record['status'] = 'paused'
                record['version'] = str(int(record['version'])+1)
                if kind in ('export', 'cleanup', 'model_download'):
                    if isinstance(record['result'], dict) and 'items' in record['result']:
                        record['completed'] = len(record['result']['items'])
                    try:
                        self.persist()
                    except OSError:
                        record['status'] = 'canceling'
                        record['error'] = {'code':'task:stop_persist_failed'}
                        raise
                self.events.publish('operation.' + record['status'], project_id=project_id,
                                    resource={'kind': 'operation', 'id': identity},
                                    payload={'operation_id': identity, 'kind': kind,
                                             'completed': record['completed'], 'total': record['total']})
        self.tasks[identity] = asyncio.create_task(execute(), name='stylelora-' + kind)
        return dict(record)

    async def join(self, project_id=None):
        current = asyncio.current_task()
        tasks = [task for key, task in self.tasks.items() if task is not current and
                 (project_id is None or self.records[key]['project_id'] == project_id)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get(self, identity):
        if identity not in self.records:
            raise Failure(410, 'session:expired')
        return dict(self.records[identity])
