"""Image snapshots, keyset cursors, review CAS and media outside writer callbacks."""
import asyncio
import base64
from collections import Counter
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import threading
from uuid import uuid4
from PIL import Image, ImageOps
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db.repositories import images
from stylelora.project_db.types import ImageFilter, PageRequest
from stylelora.annotations.projection import read_rows
from stylelora.exchange.files import image_lease
from .contracts import Failure, check, fields, plain, positive, version, canonical

LAYERS = ('original', 'wd', 'llm', 'manual', 'rule')
REVIEWS = ('unreviewed', 'keep', 'reject')
RATINGS = ('general', 'sensitive', 'questionable', 'explicit')


def query_input(value):
    fields(value, {'source_ids', 'review_statuses', 'rating', 'file_missing', 'layer', 'annotation_coverage'})
    result = dict(value)
    for key, choices in [('review_statuses', REVIEWS), ('rating', RATINGS)]:
        if key in result:
            if not isinstance(result[key], list) or any(v not in choices for v in result[key]):
                raise Failure(422, 'input:invalid')
            result[key] = sorted(set(result[key]))
    if 'source_ids' in result:
        if not isinstance(result['source_ids'], list):
            raise Failure(422, 'input:invalid')
        result['source_ids'] = sorted(set(positive(v) for v in result['source_ids']))
    if 'file_missing' in result and type(result['file_missing']) is not bool:
        raise Failure(422, 'input:invalid')
    if 'layer' in result and result['layer'] not in LAYERS:
        raise Failure(422, 'input:invalid')
    if 'annotation_coverage' in result and result['annotation_coverage'] not in ('complete', 'partial', 'none'):
        raise Failure(422, 'input:invalid')
    return result


class ImageService:
    def __init__(self, runtime):
        self.rt, self.generations, self.previews, self.alpha = runtime, {}, {}, {}
        self.guard = threading.RLock()

    def image_version(self, ctx, row):
        return version(self.rt.secret, ctx.project_id, row, self.generations.get((ctx.project_id, row.image_id), 0))

    def coverage(self, db, identity, layer=None):
        rows, refs = read_rows(db, identity)
        selected = [ref for ref in refs if layer is None or ref.source == layer]
        available = {r.annotation_id for r in rows if r.content}
        count = sum(ref.annotation_id in available for ref in selected)
        return 'none' if count == 0 else 'complete' if count == len(selected) else 'partial'

    def view(self, ctx, row, coverage=None, *, metadata=False):
        value = plain(row)
        if not metadata:
            value.pop('png_meta_json', None)
        key = (str(ctx.handle.root), row.sha256)
        alpha = self.alpha.get(key)
        if alpha is None and not row.file_missing:
            path = ctx.handle.root / 'images' / row.filename
            if path.is_file() and path.resolve().is_relative_to(ctx.handle.root.resolve()):
                try:
                    with Image.open(path) as image:
                        alpha = 'A' in image.getbands() or 'transparency' in image.info
                    self.alpha[key] = alpha
                except (OSError, ValueError):
                    pass
        value.update(has_alpha=alpha, annotation_coverage={'status': coverage or 'none'},
                     version=self.image_version(ctx, row),
                     original_url=f'/api/projects/{ctx.project_id}/images/{row.image_id}/content',
                     thumbnail_url=f'/api/projects/{ctx.project_id}/images/{row.image_id}/thumbnail')
        return value

    def scan(self, db, query, upper=None):
        found, page = [], PageRequest(limit=1000, upper_id=upper)
        while True:
            result = images.list_images(db, ImageFilter(include_missing=True), page)
            for row in result.items:
                coverage = self.coverage(db, row.image_id, query.get('layer'))
                if (('source_ids' not in query or row.source_id in query['source_ids']) and
                    ('review_statuses' not in query or row.review_status in query['review_statuses']) and
                    ('rating' not in query or row.rating in query['rating']) and
                    ('file_missing' not in query or row.file_missing == query['file_missing']) and
                    ('annotation_coverage' not in query or coverage == query['annotation_coverage'])):
                    found.append((row, coverage))
            if result.next_after_id is None:
                return found, result.upper_id
            page = PageRequest(result.next_after_id, 1000, result.upper_id)

    def cursor(self, value):
        raw = canonical(value)
        return base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(self.rt.secret, raw, hashlib.sha256).hexdigest()

    def decode(self, token, binding):
        try:
            raw, signature = token.split('.')
            raw = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
            if not hmac.compare_digest(signature, hmac.new(self.rt.secret, raw, hashlib.sha256).hexdigest()):
                raise ValueError
            value = json.loads(raw)
            if value['binding'] != binding:
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise Failure(422, 'input:invalid') from None

    def list(self, ctx, query, *, limit=50, sort='id', cursor=None, around_id=None):
        query = query_input(query)
        if type(limit) is not int or not 1 <= limit <= 1000 or sort not in ('id', '-id') or (cursor and around_id is not None):
            raise Failure(422, 'input:invalid')
        if around_id is not None:
            positive(around_id)
        binding = [ctx.project_id, 'images', query, sort]
        token = self.decode(cursor, binding) if cursor else {}
        with read_snapshot(ctx.handle) as db:
            rows, upper = self.scan(db, query, token.get('upper'))
        if sort == '-id':
            rows.reverse()
        start, anchor = 0, None
        if around_id is not None:
            anchor = next((i for i, (r, _) in enumerate(rows) if r.image_id == around_id), None)
            if anchor is None:
                raise Failure(404, 'resource:not_found')
            start = anchor // limit * limit
        elif token:
            start = next((i for i, (r, _) in enumerate(rows) if (r.image_id > token['last'] if sort == 'id' else r.image_id < token['last'])), len(rows))
        selected = rows[start:start+limit]
        more = start+len(selected) < len(rows)
        next_cursor = self.cursor(dict(binding=binding, upper=upper, last=selected[-1][0].image_id)) if more else None
        return dict(items=[self.view(ctx, r, c) for r, c in selected], total=len(rows), start_index=start,
                    anchor_index=anchor, next_cursor=next_cursor, has_more=more, limit=limit, upper_id=upper)

    def detail(self, ctx, identity, *, metadata=False):
        with read_snapshot(ctx.handle) as db:
            row = images.get_image(db, identity)
            coverage = self.coverage(db, identity)
        return self.view(ctx, row, coverage, metadata=metadata)

    def stats(self, ctx, query, groups):
        from stylelora.modes.profiles import get_profile
        from stylelora.modes.statistics import ImageSummary, summarize
        from stylelora.caption.blocklist import build_blocklist, check_blocklist
        from stylelora.annotations.projection import read_rows
        from stylelora.annotations.merge import merge_annotations
        from stylelora.caption.types import CaptionConfig
        from stylelora.tagging.catalog import LocalCatalog
        from stylelora.project_db.repositories import sources
        profile = get_profile(ctx.handle.metadata.mode)
        query = query_input(query)
        if not groups or any(g not in ('review_status', 'rating', 'source_id', 'annotation_coverage') for g in groups):
            raise Failure(422, 'input:invalid')
        with read_snapshot(ctx.handle) as db:
            rows, _ = self.scan(db, query)
            summaries = []
            catalog = self.rt.catalog or LocalCatalog()
            for row, _ in rows:
                if row.file_missing:
                    continue
                view = merge_annotations(read_rows(db, row.image_id)[0], CaptionConfig(), editing=True)
                source = sources.get_source(db, row.source_id)
                guard = build_blocklist(source.style_prompt, catalog, profile=profile)
                hits = check_blocklist(view.tags, view.nl, guard)
                aspect = 'square' if row.width == row.height else 'landscape' if row.width > row.height else 'portrait'
                summaries.append(ImageSummary(row.image_id, row.rating, row.review_status,
                    {'aspect': (aspect,), 'content': tuple(t.tag for t in view.tags)},
                    tuple('blocklist:hit' for _ in hits)))
            mode_stats = summarize(summaries, profile)
        result = []
        for group in dict.fromkeys(groups):
            counts = Counter(c if group == 'annotation_coverage' else getattr(r, group) for r, c in rows)
            result.append(dict(by=group, buckets=[dict(key=k, count=n) for k, n in sorted(counts.items(), key=lambda p: str(p[0]))]))
        return dict(total=len(rows), groups=result, mode_statistics=plain(mode_stats))

    def preview(self, ctx, query):
        query = query_input(query)
        with read_snapshot(ctx.handle) as db:
            rows, _ = self.scan(db, query)
        if len(rows) > 5000:
            raise Failure(413, 'batch:too_large')
        identity = str(uuid4())
        versions = {str(r.image_id): self.image_version(ctx, r) for r, _ in rows}
        digest = version(self.rt.secret, ctx.project_id, query, versions)
        self.previews[identity] = (ctx.project_id, query, versions, self.rt.clock()+900, digest)
        return dict(preview_id=identity, digest=digest, expires_at=self.rt.clock()+900,
                    total=len(rows), issues=[], version=digest)

    async def review(self, ctx, value):
        fields(value, {'ids', 'target', 'targets', 'expected_versions', 'query', 'expected_total', 'preview_id', 'digest'})
        target, targets = value.get('target'), value.get('targets')
        if (target is None) == (targets is None) or (target is not None and target not in REVIEWS):
            raise Failure(422, 'input:invalid')
        if 'query' in value:
            if any(k in value for k in ('ids', 'expected_versions', 'targets')):
                raise Failure(422, 'input:invalid')
            if not {'expected_total', 'preview_id', 'digest'} <= value.keys():
                raise Failure(428, 'concurrency:precondition_required')
            preview = self.previews.get(value['preview_id'])
            if not preview or preview[0] != ctx.project_id or preview[3] <= self.rt.clock():
                raise Failure(410, 'preview:expired')
            query = query_input(value['query'])
            if preview[1] != query or preview[4] != value['digest'] or type(value['expected_total']) is not int or value['expected_total'] != len(preview[2]):
                raise Failure(409, 'concurrency:version_conflict')
            expected = preview[2]
            identities = sorted(int(k) for k in expected)
        else:
            query = None
            identities = value.get('ids')
            if not isinstance(identities, list) or not identities:
                raise Failure(422, 'input:invalid')
            identities = sorted(positive(i) for i in identities)
            if len(set(identities)) != len(identities):
                raise Failure(422, 'input:invalid')
            expected = value.get('expected_versions')
            if expected is None:
                raise Failure(428, 'concurrency:precondition_required')
        if len(identities) > 5000:
            raise Failure(413, 'batch:too_large')
        keys = {str(i) for i in identities}
        if not isinstance(expected, dict) or set(expected) != keys:
            raise Failure(422, 'input:invalid')
        if targets is not None and (not isinstance(targets, dict) or set(targets) != keys or any(t not in REVIEWS for t in targets.values())):
            raise Failure(422, 'input:invalid')
        def work(db):
            if query is not None:
                current, _ = self.scan(db, query)
                if [r.image_id for r, _ in current] != identities:
                    raise Failure(409, 'concurrency:version_conflict')
            rows = [images.get_image(db, i) for i in identities]
            for row in rows:
                check(expected[str(row.image_id)], self.image_version(ctx, row))
            result, changed = [], []
            for row in rows:
                change = images.set_review(db, row.image_id, expected=row.review_status,
                                          target=targets[str(row.image_id)] if targets else target)
                result.append((change.value, row.review_status))
                if change.changed:
                    changed.append(row.image_id)
            def committed():
                for identity in changed:
                    key = (ctx.project_id, identity)
                    self.generations[key] = self.generations.get(key, 0)+1
                if changed:
                    self.rt.events.publish('image.updated', project_id=ctx.project_id,
                                           payload={'image_ids': changed if len(changed) <= 500 else None,
                                                    'invalidate_scope': len(changed) > 500, 'fields': ['review_status'], 'annotation_ids': []})
            db.after_commit.append(committed)
            return result
        result = await asyncio.wrap_future(submit_write(ctx.handle, work))
        return {'items': [dict(image=self.view(ctx, row), old_review_status=old) for row, old in result]}

    def media(self, ctx, identity, size=None):
        if size not in (None, 256, 512):
            raise Failure(422, 'input:invalid')
        with read_snapshot(ctx.handle) as db:
            row = images.get_image(db, identity)
        if row.file_missing:
            raise Failure(410, 'image:file_missing')
        with image_lease(ctx.handle, identity):
            root = ctx.handle.root.resolve()
            path = ctx.handle.root / 'images' / row.filename
            if not path.resolve().is_relative_to(root) or path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
                raise Failure(422, 'path:unsafe')
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                submit_write(ctx.handle, lambda db: images.set_file_missing(db, identity, missing=True)).result()
                raise Failure(410, 'image:file_missing') from None
            if hashlib.sha256(data).hexdigest() != row.sha256:
                raise Failure(409, 'image:hash_mismatch')
            try:
                with Image.open(io.BytesIO(data)) as original:
                    if original.format not in ('PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'TIFF') or original.width*original.height > 100_000_000:
                        raise ValueError
                    original.load()
                    mime = Image.MIME[original.format]
                    self.alpha[(str(ctx.handle.root), row.sha256)] = 'A' in original.getbands() or 'transparency' in original.info
                    if size is not None:
                        etag = hashlib.sha256(f'{row.sha256}:{size}:webp-white-exif-v1:{Image.__version__}'.encode()).hexdigest()
                        cache = root / 'cache' / (etag+'.webp')
                        if cache.is_file():
                            data = cache.read_bytes()
                        else:
                            image = ImageOps.exif_transpose(original).convert('RGBA')
                            image.thumbnail((size, size), Image.Resampling.LANCZOS)
                            white = Image.new('RGBA', image.size, 'white')
                            white.alpha_composite(image)
                            output = io.BytesIO()
                            white.convert('RGB').save(output, 'WEBP', quality=85)
                            data = output.getvalue()
                            cache.parent.mkdir(exist_ok=True)
                            temporary = cache.with_suffix('.'+uuid4().hex+'.tmp')
                            try:
                                temporary.write_bytes(data)
                                os.replace(temporary, cache)
                            finally:
                                temporary.unlink(missing_ok=True)
                        mime = 'image/webp'
                    else:
                        etag = row.sha256
            except (OSError, ValueError, Image.DecompressionBombError):
                raise Failure(422, 'image:decode_failed') from None
        return data, mime, '"'+etag+'"'
