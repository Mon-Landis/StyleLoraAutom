"""Own accepted work through completion, including uncancelable file workers."""
import asyncio
from uuid import uuid4
from .contracts import Failure, plain


class TaskSupervisor:
    def __init__(self, events):
        self.events, self.records, self.tasks = events, {}, {}
        self.draining = False
        self.failures = {}

    def submit(self, kind, work, *, project_id=None):
        if self.draining:
            raise Failure(503, 'process:draining')
        identity = str(uuid4())
        record = dict(operation_id=identity, kind=kind, status='pending', completed=0,
                      total=None, result=None, error=None, version='0', project_id=project_id)
        self.records[identity] = record

        async def execute():
            record.update(status='running', version='1')
            try:
                record['result'] = plain(await work())
                record['status'] = 'done'
            except asyncio.CancelledError:
                record['status'] = 'canceled'
            except Exception as exc:
                self.failures[identity] = exc
                record['status'] = 'failed'
                record['error'] = {'code': exc.code if isinstance(exc, Failure) else 'internal:error'}
            finally:
                record['version'] = '2'
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
