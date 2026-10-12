import asyncio
from stylelora.prompts.tasks import TaskService
from stylelora.prompts.types import SamplingRequest, CoveragePlan, CompositionOptions, FencePolicy, ConstraintSpec, Predicate
from stylelora.prompts.validation import construct
from .contracts import Failure, fields, plain, version, check


def sampling(value):
    value = dict(value)
    plan = dict(value.get('plan', {}))
    plan['constraints'] = tuple(construct(ConstraintSpec, v) for v in plan.get('constraints', ()))
    plan['exclude'] = tuple(construct(Predicate, v) for v in plan.get('exclude', ()))
    value['plan'] = construct(CoveragePlan, plan)
    value['options'] = construct(CompositionOptions, value.get('options', {}))
    value['fence'] = construct(FencePolicy, value.get('fence', {}))
    return construct(SamplingRequest, value)


class Prompts:
    def __init__(self, rt):
        self.rt = rt
        self.service = TaskService(root=rt.product.root.parent, llm=rt.llm, catalog=rt.catalog)
        self.active = set()
        self.service.store.recover()

    def view(self, value):
        raw = plain(value)
        outcomes = raw.pop('outcomes')
        raw.update(completed=sum(o['status'] in ('ok', 'failed', 'skipped') for o in outcomes), total=len(outcomes),
                   outcomes_url='/api/prompt-tasks/'+raw['task_id']+'/outcomes',
                   version=version(self.rt.secret, 'prompt', raw['task_id'], raw['revision']))
        return raw

    def get(self, identity):
        return self.view(self.service.get_task(identity))

    def launch(self, identity):
        if identity in self.active:
            return
        self.active.add(identity)
        async def work():
            try:
                result = await self.service.run(identity)
                self.rt.events.publish('prompt.'+result.status, resource={'kind':'prompt', 'id':identity},
                                       payload={'task_id':identity, 'status':result.status, 'ready':result.ready})
                return self.view(result)
            finally:
                self.active.discard(identity)
        self.rt.supervisor.submit('prompt', work)

    async def create(self, value, key):
        request = sampling(value)
        if request.optimize and self.rt.llm is None:
            raise Failure(503, 'dependency:unavailable')
        result = await self.service.create_task(request, request_key=key)
        if result.status in ('pending', 'running'):
            self.launch(result.task_id)
        return self.view(result)

    async def control(self, identity, value, expected):
        fields(value, {'action'}, {'action'})
        current = self.service.get_task(identity)
        check(expected, self.view(current)['version'])
        result = await self.service.control_task(identity, value['action'], expected_revision=current.revision)
        if value['action'] == 'resume':
            self.launch(identity)
        return self.view(result)

    async def pause(self):
        for identity in tuple(self.active):
            current = self.service.get_task(identity)
            if current.status == 'running':
                await self.service.control_task(identity, 'pause', expected_revision=current.revision)
