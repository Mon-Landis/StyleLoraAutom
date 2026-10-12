"""Session previews and supervised copies using F10 public entry points."""
import asyncio
from dataclasses import asdict
from threading import Event
from uuid import uuid4
from stylelora.project_db import read_snapshot
from stylelora.project_db.repositories import schemes, sources
from stylelora.project_db.types import SourceFilter, ImageFilter
from stylelora.exchange.files import all_pages
from stylelora.exchange.importer import ImportService, scan_import
from stylelora.exchange.exporter import export_project
from stylelora.exchange.schemes import import_params
from stylelora.tagging.trigger_validation import validate_trigger, override_digest
from .contracts import Failure, fields, positive, plain, version


class Exchange:
    def __init__(self, rt):
        self.rt, self.previews, self.exports = rt, {}, {}
        self.importer = ImportService(rt.catalog) if rt.catalog else None

    def preview(self, ctx, kind, value):
        if self.rt.catalog is None or not self.rt.catalog.available:
            raise Failure(503, 'dependency:unavailable')
        fields(value, {'scheme_id', 'root_handle'} if kind == 'import' else {'scheme_id'}, {'scheme_id'})
        identity, scheme_id = str(uuid4()), positive(value['scheme_id'])
        with read_snapshot(ctx.handle) as db:
            scheme = (schemes.get_import_scheme if kind == 'import' else schemes.get_export_scheme)(db, scheme_id)
            source_rows = tuple(all_pages(lambda p: sources.list_sources(db, SourceFilter(), p)))
        issues, root_handle, members = [], None, []
        if kind == 'import':
            path = self.rt.grant_path(value.get('root_handle'), 'import_root')
            manifest = scan_import(path, recursive=import_params(scheme.params_json).recursive, project_root=ctx.handle.root)
            members = plain(manifest)
            root_handle = value['root_handle']
            issues = plain(manifest.warnings)
        else:
            if scheme.pipeline_json:
                raise Failure(501, 'pipeline:unsupported')
            if scheme.output_config_json.get('image_mode') == 'hardlink':
                raise Failure(501, 'feature:unsupported')
            for style in [r.style_prompt for r in source_rows] or ['']:
                issues.extend(plain(validate_trigger(ctx.handle.metadata.trigger_word, style, catalog=self.rt.catalog)))
            if any(i['severity'] == 'error' for i in issues):
                raise Failure(422, next(i['code'] for i in issues if i['severity'] == 'error'))
            with read_snapshot(ctx.handle) as db:
                rows, _ = self.rt.images.scan(db, {})
                members = [(r.image_id, self.rt.images.image_version(ctx, r)) for r, _ in rows]
        digest = version(self.rt.secret, ctx.project_id, kind, scheme, members, ctx.handle.metadata)
        self.previews[identity] = dict(project_id=ctx.project_id, kind=kind, scheme_id=scheme_id,
                                      digest=digest, expiry=self.rt.clock()+900, root_handle=root_handle)
        return dict(preview_id=identity, digest=digest, version=digest, expires_at=self.rt.clock()+900,
                    issues=issues, selected=len(members), trigger_override_digest=override_digest(ctx.handle.metadata.trigger_word, self.rt.catalog) if kind == 'export' else None)

    async def execute(self, ctx, kind, value):
        fields(value, {'scheme_id', 'preview_id', 'digest', 'trigger_override_digest'} if kind == 'export' else {'preview_id', 'digest', 'name'}, {'preview_id', 'digest'})
        preview = self.previews.get(value['preview_id'])
        if not preview or preview['project_id'] != ctx.project_id or preview['kind'] != kind or preview['expiry'] <= self.rt.clock():
            raise Failure(410, 'preview:expired')
        if preview['digest'] != value['digest'] or (kind == 'export' and value.get('scheme_id') != preview['scheme_id']):
            raise Failure(409, 'preview:stale')
        fresh = await self.rt.blocking(lambda: self.preview(ctx, kind, {'scheme_id': preview['scheme_id'], **({'root_handle': preview['root_handle']} if kind == 'import' else {})}))
        if fresh['digest'] != preview['digest']:
            raise Failure(409, 'preview:stale')
        if kind == 'import':
            root = self.importer.register_root(self.rt.grant_path(preview['root_handle'], 'import_root'))
            async def work():
                if ctx.closing:
                    return None
                source = await self.rt.blocking(lambda: self.importer.create_import(ctx.handle, root, scheme_id=preview['scheme_id'], name=value.get('name', 'Import')))
                try:
                    return plain(await self.rt.blocking(lambda: self.importer.start_import(ctx.handle, source.source_id, root)))
                except Exception:
                    raise Failure(503, 'dependency:unavailable', details={'resource_id': source.source_id}) from None
            return self.rt.supervisor.submit('import', work, project_id=ctx.project_id)
        identity, stop = str(uuid4()), Event()
        record = dict(export_id=identity, project_id=ctx.project_id, status='planned', selected=0, exported=0,
                      skipped=0, failed=0, report_url=None, relative_dir=None, version='0')
        self.exports[identity] = (record, stop, None)
        async def work():
            if ctx.closing:
                record.update(status='interrupted', version='2')
                return dict(record)
            record.update(status='running', version='1')
            try:
                report = await self.rt.blocking(lambda: export_project(ctx.handle, preview['scheme_id'], catalog=self.rt.catalog,
                    cancel=stop.is_set, trigger_override_digest=value.get('trigger_override_digest')))
                data = plain(report)
                record.update(status='finished', selected=data['selected'], exported=data['exported'], skipped=data['skipped'], failed=data['failed'],
                              relative_dir=data['dir'], report_url=f'/api/projects/{ctx.project_id}/exports/{identity}/report')
                self.exports[identity] = (record, stop, data)
            except Exception:
                record['status'] = 'canceled' if stop.is_set() else 'failed'
                raise
            finally:
                record['version'] = '2'
                self.rt.events.publish('export.'+record['status'], project_id=ctx.project_id,
                                       resource={'kind':'export', 'id':identity}, payload={k:v for k,v in record.items() if k not in ('relative_dir',)})
            return dict(record)
        self.rt.supervisor.submit('export', work, project_id=ctx.project_id)
        return dict(record)

    def get(self, ctx, identity, report=False):
        entry = self.exports.get(identity)
        if not entry or entry[0]['project_id'] != ctx.project_id:
            raise Failure(410, 'export:session_expired')
        if report and entry[0]['status'] != 'finished':
            raise Failure(409, 'export:not_finished')
        return entry[2] if report else dict(entry[0])
