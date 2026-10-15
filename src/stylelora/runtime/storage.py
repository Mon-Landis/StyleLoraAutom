"""Read-only project accounting and explicitly confirmed file cleanup."""
import os
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db.repositories import images
from stylelora.exchange.files import image_lease
from .contracts import Failure, fields, version


def inventory(root):
    files, errors = {}, []
    def error(exc):
        errors.append({'code': 'storage:unavailable'})
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise Failure(503, 'storage:unavailable')
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=error):
        from pathlib import Path
        parent = Path(directory)
        dirs[:] = [d for d in dirs if not (parent/d).is_symlink() and not (parent/d).is_junction()
                   and d != 'models']
        for name in names:
            path = parent/name
            try:
                if path.is_symlink() or path.is_junction():
                    continue
                stat = path.stat()
                files[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            except OSError:
                error(None)
    return files, errors


class Storage:
    def __init__(self, rt):
        self.rt, self.summaries, self.previews = rt, {}, {}

    def busy(self, ctx):
        if ctx.users or any(r['project_id'] == ctx.project_id and r['kind'] in
            ('export', 'import', 'recovery', 'cleanup', 'source', 'job', 'ratings', 'image_verify') and r['status'] in ('pending', 'running')
            for r in self.rt.supervisor.records.values()):
            raise Failure(409, 'storage:leased')

    def scan(self, root, ctx=None, known_id=None):
        files, errors = inventory(root)
        partitions, reviews, sources = Counter(), Counter(), Counter()
        rows = {}
        if ctx:
            with read_snapshot(ctx.handle) as db:
                rows = {r.filename: r for r, _ in self.rt.images.scan(db, {})[0]}
        for path, identity in files.items():
            head = path.split('/')[0]
            kind = ('pending' if '/.pending/' in '/' + path else
                    'database' if head.startswith('project.db') else
                    head if head in ('images', 'cache', 'exports', 'backups') else 'other')
            partitions[kind] += identity[0]
            row = rows.get(path.removeprefix('images/')) if kind == 'images' else None
            if row:
                reviews[row.review_status] += identity[0]
                sources[str(row.source_id)] += identity[0]
        threshold = self.rt.product.get()['storage_thresholds']['project_bytes']
        result = dict(known_id=known_id, project_id=ctx.project_id if ctx else None,
            total_bytes=sum(partitions.values()), partitions=dict(partitions),
            review_bytes=dict(reviews) if ctx else None, source_bytes=dict(sources) if ctx else None,
            missing_count=sum(r.file_missing for r in rows.values()) if ctx else None,
            threshold_bytes=threshold, unit='decimal_bytes', models_excluded=True,
            as_of=datetime.now(timezone.utc).isoformat(), errors=errors, partial=bool(errors), stale=False)
        result['over_threshold'] = result['total_bytes'] > threshold
        if ctx:
            self.summaries[ctx.project_id] = result
        return result

    def candidates(self, ctx, kind):
        files, errors = inventory(ctx.handle.root)
        if errors:
            raise Failure(503, 'storage:unavailable')
        if kind == 'thumbnails':
            import re
            return {p: v for p, v in files.items() if re.fullmatch(r'cache/[0-9a-f]{64}\.webp', p)}
        if kind == 'exports':
            finished = {p.rsplit('/', 1)[0] for p in files if p.endswith('/report.json')
                        and p.startswith('exports/') and '/.partial-' not in p
                        and p.rsplit('/', 1)[0] + '/interrupted.json' not in files}
            return {p: v for p, v in files.items() if any(p.startswith(d + '/') for d in finished)}
        with read_snapshot(ctx.handle) as db:
            rows, _ = self.rt.images.scan(db, {'review_statuses': ['reject'], 'file_missing': False})
        return {'images/' + r.filename: files['images/' + r.filename] for r, _ in rows
                if 'images/' + r.filename in files}

    def preview(self, ctx, value):
        fields(value, {'kind'}, {'kind'})
        kind = value['kind']
        if kind not in ('thumbnails', 'exports', 'rejected_files'):
            raise Failure(422, 'input:invalid')
        candidates = self.candidates(ctx, kind)
        identity = str(uuid4())
        digest = version(self.rt.secret, ctx.project_id, kind, candidates)
        self.previews[identity] = (ctx.project_id, kind, candidates, digest, self.rt.clock()+900)
        return dict(preview_id=identity, kind=kind, digest=digest, version=digest,
                    count=len(candidates), bytes=sum(v[0] for v in candidates.values()),
                    irreversible=True, expires_at=self.rt.clock()+900)

    def cleanup(self, ctx, value, *, cancel=lambda: False):
        fields(value, {'preview_id', 'digest', 'confirm'}, {'preview_id', 'digest', 'confirm'})
        if value['confirm'] is not True:
            raise Failure(422, 'cleanup:confirmation_required')
        entry = self.previews.get(value['preview_id'])
        if not entry or entry[0] != ctx.project_id or entry[4] <= self.rt.clock():
            raise Failure(410, 'preview:expired')
        _, kind, candidates, digest, _ = entry
        if digest != value['digest'] or candidates != self.candidates(ctx, kind):
            raise Failure(409, 'cleanup:preview_stale')
        results = []
        for relative, identity in candidates.items():
            if cancel():
                break
            path = ctx.handle.root/relative
            try:
                if kind == 'rejected_files':
                    image_id = int(path.stem)
                    with image_lease(ctx.handle, image_id):
                        def remove(db):
                            row = images.get_image(db, image_id)
                            if row.review_status != 'reject' or row.filename != path.name:
                                raise Failure(409, 'cleanup:preview_stale')
                            self.unlink(ctx, path, identity)
                            images.set_file_missing(db, image_id, missing=True)
                        submit_write(ctx.handle, remove).result()
                else:
                    self.unlink(ctx, path, identity)
                results.append({'relative_path': relative, 'status': 'deleted'})
            except (OSError, Failure) as exc:
                results.append({'relative_path': relative, 'status': 'failed',
                                'code': exc.code if isinstance(exc, Failure) else 'storage:unavailable'})
        self.summaries.pop(ctx.project_id, None)
        return {'items': results, 'stopped': len(results) < len(candidates)}

    @staticmethod
    def unlink(ctx, path, identity):
        if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)
               if p.is_relative_to(ctx.handle.root)):
            raise Failure(409, 'cleanup:preview_stale')
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != identity:
            raise Failure(409, 'cleanup:preview_stale')
        path.unlink()
