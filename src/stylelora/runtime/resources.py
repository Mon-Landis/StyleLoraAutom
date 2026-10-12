"""Adapters over existing public services; never exposes connections to HTTP."""
import asyncio
from dataclasses import fields as dataclass_fields
from uuid import UUID
from stylelora.project_db import read_snapshot, submit_write, commands
from stylelora.project_db.repositories import sources, jobs, schemes, operations
from stylelora.project_db.types import SourceDraft, SourcePatch, SourceFilter, ItemFilter, PageRequest, ItemSeed, CurrentRef, Tag, Scope, ImageFilter
from stylelora.annotations.projection import get_annotation_view
from stylelora.annotations.editing import ManualEdit, edit_manual
from stylelora.rules.service import RuleService, undo_latest_rule
from stylelora.sources.runner import SourceRunner
from stylelora.prompts.dictionaries import digest
from .contracts import Failure, plain, version, check, fields, positive


class Resources:
    def __init__(self, runtime):
        self.rt, self.rules, self.locks, self.revisions = runtime, RuleService(catalog=runtime.catalog), {}, {}

    def token(self, ctx, kind, row):
        raw = plain(row)
        if kind == 'jobs':
            raw = {k: raw[k] for k in ('job_id', 'status', 'batch_id', 'config_snapshot_json')}
        return version(self.rt.secret, ctx.project_id, kind, raw, self.revisions.get((ctx.project_id, kind, self.identity(kind, row)), 0))

    @staticmethod
    def identity(kind, row):
        return getattr(row, {'sources': 'source_id', 'source-items': 'item_id', 'jobs': 'job_id',
                            'operation-log': 'op_id', 'import-schemes': 'scheme_id', 'export-schemes': 'scheme_id'}[kind])

    def view(self, ctx, kind, row, db=None):
        result = plain(row)
        result['version'] = self.token(ctx, kind, row)
        if kind == 'sources':
            runner = self.rt.runners.get((ctx.project_id, row.source_id))
            result['counts'] = sources.count_item_states(db, row.source_id) if db else {}
            result['runtime'] = dict(cycle_id=runner.cycle_id if runner else None, pause_reason=runner.reason if runner else None,
                                     restore_pending=bool(runner and runner.blocked_backend), in_flight=int(bool(runner and runner.task and not runner.task.done())))
        return result

    def get_row(self, db, kind, identity):
        return {'sources': sources.get_source, 'source-items': sources.get_item, 'jobs': jobs.get_job,
                'operation-log': operations.get_operation, 'import-schemes': schemes.get_import_scheme,
                'export-schemes': schemes.get_export_scheme}[kind](db, identity)

    def get(self, ctx, kind, identity):
        with read_snapshot(ctx.handle) as db:
            return self.view(ctx, kind, self.get_row(db, kind, identity), db)

    def list(self, ctx, kind, params, source_id=None):
        fields(params, {'limit', 'cursor', 'sort', 'status', 'batch_id', 'type', 'q', 'executor'})
        try:
            limit = int(params.get('limit', 50))
        except (ValueError, TypeError):
            raise Failure(422, 'input:invalid') from None
        if not 1 <= limit <= 1000 or params.get('sort', 'id') not in ('id', '-id'):
            raise Failure(422, 'input:invalid')
        filters = {k: v for k, v in params.items() if k not in ('limit', 'cursor')}
        binding = [ctx.project_id, kind, filters, source_id]
        token = self.rt.images.decode(params['cursor'], binding) if params.get('cursor') else {}
        page, rows = PageRequest(limit=1000, upper_id=token.get('upper')), []
        with read_snapshot(ctx.handle) as db:
            while True:
                if kind == 'sources':
                    result = sources.list_sources(db, SourceFilter((params['status'],) if 'status' in params else ()), page)
                elif kind == 'source-items':
                    result = sources.list_items(db, ItemFilter(source_id, (params['status'],) if 'status' in params else ()), page)
                elif kind == 'jobs':
                    result = jobs.list_jobs(db, status=params.get('status'), batch_id=params.get('batch_id'), page=page)
                elif kind == 'operation-log':
                    result = operations.list_operations(db, page)
                else:
                    result = (schemes.list_import_schemes if kind == 'import-schemes' else schemes.list_export_schemes)(db, page)
                for row in result.items:
                    if any(k in params and getattr(row, k, None) != params[k] for k in ('type', 'executor')):
                        continue
                    if 'q' in params and params['q'].casefold() not in getattr(row, 'name', '').casefold():
                        continue
                    rows.append(row)
                if result.next_after_id is None:
                    break
                page = PageRequest(result.next_after_id, 1000, result.upper_id)
            descending = params.get('sort') == '-id'
            if descending:
                rows.reverse()
            if token:
                rows = [r for r in rows if (self.identity(kind, r) < token['last'] if descending else self.identity(kind, r) > token['last'])]
            selected, more = rows[:limit], len(rows) > limit
            items = [self.view(ctx, kind, row, db) for row in selected]
            for value in items:
                if kind == 'jobs':
                    value.pop('progress_json', None)
                if kind == 'operation-log':
                    value.pop('changes_json', None)
        cursor = self.rt.images.cursor(dict(binding=binding, upper=result.upper_id, last=self.identity(kind, selected[-1]))) if more else None
        return dict(items=items, next_cursor=cursor, has_more=more, limit=limit, upper_id=result.upper_id)

    async def source_create(self, ctx, value):
        fields(value, {f.name for f in dataclass_fields(SourceDraft)}, {'type', 'name'})
        if 'target_count' in value and value['target_count'] is not None:
            positive(value['target_count'])
        value = dict(value)
        value.setdefault('cost_policy', (await self.rt.product.call(self.rt.product.get))['cost_policy'])
        row = await asyncio.wrap_future(submit_write(ctx.handle, lambda db: sources.create_source(db, SourceDraft(**value))))
        return await self.rt.blocking(lambda: self.get(ctx, 'sources', row.source_id))

    async def source_update(self, ctx, identity, value, expected, delete=False):
        fields(value, {f.name for f in dataclass_fields(SourcePatch)})
        def work(db):
            old = sources.get_source(db, identity)
            check(expected, self.token(ctx, 'sources', old))
            if delete:
                sources.delete_empty_draft(db, identity)
                return None
            result = sources.update_source(db, identity, SourcePatch(**value))
            if result.changed:
                key = (ctx.project_id, 'sources', identity)
                db.after_commit.append(lambda: self.revisions.__setitem__(key, self.revisions.get(key, 0)+1))
            return result.value
        row = await asyncio.wrap_future(submit_write(ctx.handle, work))
        return await self.rt.blocking(lambda: self.get(ctx, 'sources', identity)) if row else None

    def runner(self, ctx, identity):
        key = (ctx.project_id, identity)
        if key not in self.rt.runners:
            if self.rt.backend_factory is None:
                raise Failure(503, 'dependency:unavailable')
            bridge = self.rt.events
            class SourceEvents:
                async def publish(self, event):
                    if event.phase == 'complete' and event.current_item_id is not None:
                        with read_snapshot(ctx.handle) as db:
                            item = sources.get_item(db, event.current_item_id)
                        bridge.publish('source.item_completed', project_id=ctx.project_id,
                                       resource={'kind':'source', 'id':identity}, payload=dict(source_id=identity,
                                       item_id=item.item_id, attempt=item.attempts, image_id=item.image_id,
                                       outcome=item.status, counts=dict(event.counts), reason_code=item.error))
                    kind = 'source.' + ('cost_confirmation_required' if event.phase == 'cost' and event.reason_code == 'cost:confirmation_required' else event.status if event.status in ('paused', 'done', 'canceled') else 'progress')
                    bridge.publish(kind, project_id=ctx.project_id, resource={'kind': 'source', 'id': identity},
                                   payload=dict(source_id=identity, cycle_id=event.cycle_id, status=event.status,
                                                counts=dict(event.counts), reason_code=event.reason_code,
                                                current_item_id=event.current_item_id, phase=event.phase,
                                                estimated_anlas=event.estimated_anlas))
            self.rt.runners[key] = SourceRunner(ctx.handle, identity, self.rt.backend_factory, events=SourceEvents())
        return self.rt.runners[key]

    async def source_action(self, ctx, identity, action, value, expected):
        lock = self.locks.setdefault((ctx.project_id, 'sources', identity), asyncio.Lock())
        async with lock:
            current = await self.rt.blocking(lambda: self.get(ctx, 'sources', identity))
            check(expected, current['version'])
            runner = self.runner(ctx, identity)
            if action == 'start':
                fields(value, {'kind', 'seeds'}, {'kind', 'seeds'})
                if value['kind'] != 'nai':
                    raise Failure(501, 'feature:unsupported')
                frozen = fields(value['seeds'], {'digest', 'items', 'count'}, {'digest', 'items', 'count'})
                positive(frozen['count'])
                if not isinstance(frozen['items'], list) or frozen['count'] != len(frozen['items']) or len(frozen['items']) > 10000:
                    raise Failure(422, 'input:invalid')
                seeds = []
                for item in frozen['items']:
                    fields(item, {'seq', 'prompt', 'aspect'}, {'seq', 'prompt'})
                    positive(item['seq'])
                    seeds.append(ItemSeed(**item))
                if digest(tuple(seeds)) != frozen['digest']:
                    raise Failure(409, 'seeds:not_ready')
                async def work():
                    if ctx.closing:
                        return None
                    await runner.start(tuple(seeds))
                    if runner.task:
                        await asyncio.shield(runner.task)
                    return await self.rt.blocking(lambda: self.get(ctx, 'sources', identity))
                return self.rt.supervisor.submit('source', work, project_id=ctx.project_id)
            if action == 'confirm-cost':
                fields(value, {'cycle_id'}, {'cycle_id'})
                async def work():
                    if ctx.closing:
                        return None
                    await runner.confirm_cost_and_resume(value['cycle_id'])
                    if runner.task:
                        await asyncio.shield(runner.task)
                return self.rt.supervisor.submit('source', work, project_id=ctx.project_id)
            fields(value, {'action'}, {'action'})
            if value['action'] not in ('pause', 'resume', 'cancel'):
                raise Failure(422, 'input:invalid')
            if value['action'] == 'resume':
                async def work():
                    if ctx.closing:
                        return None
                    await runner.resume()
                    if runner.task:
                        await asyncio.shield(runner.task)
                return self.rt.supervisor.submit('source', work, project_id=ctx.project_id)
            await (runner.pause() if value['action'] == 'pause' else runner.stop())
            return await self.rt.blocking(lambda: self.get(ctx, 'sources', identity))

    def annotation(self, ctx, identity):
        result = plain(get_annotation_view(ctx.handle, identity))
        result['version'] = version(self.rt.secret, ctx.project_id, result['refs'])
        return result

    def manual(self, ctx, identity, value, expected):
        fields(value, {'tags', 'natural_language', 'read_set', 'write_nl'}, {'tags', 'read_set'})
        check(expected, self.annotation(ctx, identity)['version'])
        if 'write_nl' in value and type(value['write_nl']) is not bool:
            raise Failure(422, 'input:invalid')
        refs = []
        for ref in value['read_set']:
            fields(ref, {'image_id', 'source', 'type', 'annotation_id'}, {'image_id', 'source', 'type', 'annotation_id'})
            positive(ref['image_id'])
            if ref['image_id'] != identity:
                raise Failure(422, 'input:invalid')
            refs.append(CurrentRef(**ref))
        tags = []
        for tag in value['tags']:
            fields(tag, {'tag', 'conf'}, {'tag'})
            tags.append(Tag(**tag))
        return plain(edit_manual(ctx.handle, ManualEdit(identity, tuple(tags), value.get('natural_language'), tuple(refs), value.get('write_nl', False))))

    def scope(self, value):
        fields(value, {'selected_ids', 'filter'})
        if len(value) != 1:
            raise Failure(422, 'input:invalid')
        if 'selected_ids' in value:
            if not isinstance(value['selected_ids'], list) or not value['selected_ids']:
                raise Failure(422, 'input:invalid')
            return Scope(selected_ids=tuple(positive(i) for i in value['selected_ids']))
        filters = fields(value['filter'], {'source_ids', 'review_statuses', 'include_missing'})
        return Scope(filter=ImageFilter(**filters))

    def rule_preview(self, ctx, value):
        fields(value, {'rule', 'scope'}, {'rule', 'scope'})
        preview = self.rules.preview_rule(ctx.handle, value['rule'], self.scope(value['scope']))
        return dict(preview_id=preview.preview_id, digest=preview.digest, version=preview.digest,
                    expires_at=self.rt.clock()+900, issues=[], affected_count=preview.changed_count)

    def rule_apply(self, ctx, value, expected):
        fields(value, {'preview_id', 'digest'}, {'preview_id', 'digest'})
        check(expected, value['digest'])
        return plain(self.rules.apply_preview(ctx.handle, value['preview_id'], value['digest']))

    async def undo(self, ctx, identity, expected):
        def work(db):
            row = operations.get_operation(db, identity)
            check(expected, self.token(ctx, 'operation-log', row))
            return commands.undo_rule(db, identity)
        result = await asyncio.wrap_future(submit_write(ctx.handle, work))
        return self.view(ctx, 'operation-log', result.value)

    async def retry(self, ctx, identity, value, expected):
        fields(value, {'expected_attempt'}, {'expected_attempt'})
        positive(value['expected_attempt'])
        def work(db):
            row = sources.get_item(db, identity)
            check(expected, self.token(ctx, 'source-items', row))
            return commands.retry_failed_item(db, identity, expected_attempt=value['expected_attempt']).value
        row = await asyncio.wrap_future(submit_write(ctx.handle, work))
        return self.view(ctx, 'source-items', row)

    async def scheme_write(self, ctx, kind, value, identity=None, expected=None, delete=False):
        from stylelora.exchange.schemes import validate_scheme
        parsed = None if delete else validate_scheme(value)
        if parsed is not None:
            from stylelora.project_db.types import ImportSchemeInput
            if isinstance(parsed, ImportSchemeInput) != (kind == 'import-schemes'):
                raise Failure(422, 'input:invalid')
        def work(db):
            if identity is None:
                return (schemes.create_import_scheme if kind == 'import-schemes' else schemes.create_export_scheme)(db, parsed)
            old = self.get_row(db, kind, identity)
            check(expected, self.token(ctx, kind, old))
            if delete:
                (schemes.delete_import_scheme if kind == 'import-schemes' else schemes.delete_export_scheme)(db, identity)
                return None
            return (schemes.update_import_scheme if kind == 'import-schemes' else schemes.update_export_scheme)(db, identity, parsed, expected_updated_at=old.updated_at).value
        row = await asyncio.wrap_future(submit_write(ctx.handle, work))
        return self.view(ctx, kind, row) if row else None

    def job_runner(self, ctx):
        from stylelora.executors.runner import JobRunner
        if ctx.project_id not in self.rt.job_runners:
            runner = JobRunner(ctx.handle, self.rt.executors, snapshots=self.rt.snapshots)
            self.rt.events.attach_jobs(runner.events, ctx.project_id)
            self.rt.job_runners[ctx.project_id] = runner
        return self.rt.job_runners[ctx.project_id]

    async def jobs_create(self, ctx, value):
        from stylelora.executors.runner import create_jobs
        from stylelora.project_db.types import JobRequest
        fields(value, {'batch_id', 'executors', 'mode', 'scope'}, {'batch_id', 'executors', 'mode', 'scope'})
        try:
            batch = str(UUID(value['batch_id']))
        except (ValueError, TypeError):
            raise Failure(422, 'input:invalid') from None
        if not isinstance(value['executors'], list) or not value['executors'] or any(e not in ('wd', 'llm_vision') for e in value['executors']) or value['mode'] not in ('selected', 'uncovered', 'all'):
            raise Failure(422, 'input:invalid')
        if any(e not in self.rt.executors or e not in self.rt.snapshots for e in value['executors']):
            raise Failure(503, 'dependency:unavailable')
        scope = self.scope(value['scope'])
        requests = [JobRequest(e, value['mode'], scope) for e in ('wd', 'llm_vision') if e in value['executors']]
        rows = await create_jobs(ctx.handle, requests, batch_id=batch)
        runner = self.job_runner(ctx)
        async def work():
            for row in rows:
                if ctx.closing:
                    break
                await runner.run(row.job_id)
        self.rt.supervisor.submit('job', work, project_id=ctx.project_id)
        return [self.view(ctx, 'jobs', row) for row in rows]

    async def job_control(self, ctx, identity, value, expected):
        fields(value, {'action'}, {'action'})
        if value['action'] not in ('pause', 'resume', 'cancel'):
            raise Failure(422, 'input:invalid')
        def control(db):
            row = jobs.get_job(db, identity)
            check(expected, self.token(ctx, 'jobs', row))
            if value['action'] == 'resume' and row.executor not in self.rt.executors:
                raise Failure(503, 'dependency:unavailable')
            return commands.transition_job(db, identity, expected=row.status,
                target={'pause':'paused', 'resume':'running', 'cancel':'canceled'}[value['action']]).value
        row = await asyncio.wrap_future(submit_write(ctx.handle, control))
        if value['action'] == 'resume':
            self.rt.supervisor.submit('job', lambda: self.job_runner(ctx).run(identity), project_id=ctx.project_id)
        return self.view(ctx, 'jobs', row)
