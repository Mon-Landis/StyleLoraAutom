"""Synchronous local worker, frozen inputs, durable F01 receipt recovery."""
from dataclasses import asdict
import math
import os
from pathlib import Path
import shutil
import statistics
from threading import RLock
from uuid import uuid4
from PIL import Image, ImageSequence
from stylelora.project_db import read_snapshot, submit_write, commands
from stylelora.project_db.repositories import sources, images, schemes
from stylelora.project_db.types import SourceDraft, ItemSeed, ItemFilter, ImageArtifact
from stylelora.project_db.receipts import EXTENSIONS, prepare_receipt, receipt_path, read_receipt
from stylelora.project_db.validation import relative_path
from stylelora.project_db.errors import ValidationError, CommitUnknown
from stylelora.tagging.types import Issue
from .types import ManifestEntry, ImportManifest
from .schemes import import_params
from .readers import READERS
from .readers.txt_sidecar import sidecar_path
from .files import sha256, all_pages


def _linked(path):
    return path.is_symlink() or path.is_junction()


def scan_import(root,*,recursive=True,project_root=None):
    root = Path(root)
    if _linked(root) or not root.is_dir():
        raise ValidationError('import:root_unavailable')
    root = root.resolve()
    if project_root and (root.is_relative_to(project_root.resolve()) or project_root.resolve().is_relative_to(root)):
        raise ValidationError('path:unsafe')
    paths = []
    for directory, dirs, files in os.walk(root,followlinks=False):
        parent = Path(directory)
        dirs[:] = [d for d in dirs if d not in ('.pending',) and not d.endswith('.slproj') and not _linked(parent/d)] if recursive else []
        for name in files:
            path = parent/name
            if path.suffix.lower() in EXTENSIONS and not _linked(path):
                paths.append(path)
    paths.sort(key=lambda p:(p.relative_to(root).as_posix().casefold(),p.relative_to(root).as_posix()))
    entries, warnings, stems = [], [], set()
    for seq,path in enumerate(paths,1):
        sidecar = sidecar_path(path)
        stem = (path.parent,path.stem.casefold())
        if stem in stems and sidecar:
            warnings.append(Issue('sidecar:shared_stem',field=path.relative_to(root).as_posix()))
        stems.add(stem)
        entries.append(ManifestEntry(seq,path.relative_to(root).as_posix(),sha256(path),sha256(sidecar) if sidecar else None))
    return ImportManifest(tuple(entries),tuple(warnings))


def image_info(path):
    with Image.open(path) as image:
        width,height = image.size
        meta = {'schema_version':1,'text':dict(image.text)} if image.format == 'PNG' and getattr(image,'text',None) else None
        # Decode every animation frame, retaining the first frame for fingerprinting.
        image.load()
        for frame in ImageSequence.Iterator(image):
            frame.load()
    from stylelora.vision.phash import compute_phash
    return width,height,compute_phash(path),meta


class ImportService:
    def __init__(self,catalog):
        self.catalog = catalog
        self.roots = {}
        self._lock = RLock()

    def register_root(self,root):
        if _linked(Path(root)):
            raise ValidationError('path:unsafe')
        handle = uuid4().hex
        self.roots[handle] = Path(root).resolve()
        return handle

    def create_import(self,project,root_handle,*,scheme_id=None,name='Import'):
        root = self.roots.get(root_handle)
        if root is None:
            raise ValidationError('import:root_unavailable')
        with read_snapshot(project) as db:
            scheme = schemes.get_import_scheme(db,scheme_id) if scheme_id else schemes.get_default_import_scheme(db)
        params = import_params(scheme.params_json)
        manifest = scan_import(root,recursive=params.recursive,project_root=project.root)
        if not manifest.entries:
            raise ValidationError('import:empty')
        if not self.catalog.available:
            raise ValidationError('catalog:unavailable')
        config = {'schema_version':1,'reader':scheme.reader,'params':asdict(params),
                  'catalog_revision':self.catalog.revision,'tokenizer_version':params.tokenizer_version,
                  'import_manifest':[e.relative_path for e in manifest.entries],
                  'manifest_evidence':[asdict(e) for e in manifest.entries]}
        return submit_write(project,lambda db:sources.create_source(db,SourceDraft('import',name,config))).result()

    def start_import(self,project,source_id,root_handle):
        with self._lock:
            return self._run(project,source_id,root_handle)

    def _run(self,project,source_id,root_handle):
        root = self.roots.get(root_handle)
        if root is None or not root.is_dir():
            raise ValidationError('import:root_unavailable')
        if root.is_relative_to(project.root.resolve()) or project.root.resolve().is_relative_to(root):
            raise ValidationError('path:unsafe')
        with read_snapshot(project) as db:
            source = sources.get_source(db,source_id)
        config = source.config_json
        params = import_params(config['params'])
        entries = tuple(ManifestEntry(**e) for e in config['manifest_evidence'])
        if config['catalog_revision'] != self.catalog.revision:
            raise ValidationError('catalog:revision_unavailable')
        if source.status in ('done','canceled'):
            return source
        if source.status == 'draft':
            source = submit_write(project,lambda db:commands.start_source(db,source_id,
                tuple(ItemSeed(e.seq,Path(e.relative_path).name) for e in entries))).result()
        with read_snapshot(project) as db:
            items = tuple(all_pages(lambda page:sources.list_items(db,ItemFilter(source_id),page)))
        # Receipts are authoritative after sent; do not reinterpret bytes/encoding.
        for item in items:
            if item.status == 'sent':
                if not receipt_path(project.root,item.item_id,item.attempts).exists():
                    self._pause(project,source_id)
                    raise ValidationError('import:unresolved_sent')
                _,artifact,originals = read_receipt(project.root,item.item_id,item.attempts)
                self._complete(project,artifact,originals)
        if source.status == 'paused':
            submit_write(project,lambda db:commands.transition_source(db,source_id,expected='paused',target='running',reason='resume')).result()
        for item,entry in zip(items,entries,strict=True):
            if item.status != 'pending':
                continue
            path = relative_path(root,entry.relative_path)
            try:
                sidecar = sidecar_path(path)
                parents = (path, *tuple(p for p in path.parents if p != root and p.is_relative_to(root)))
                if any(_linked(p) for p in parents) or sha256(path)!=entry.image_sha256 or (sha256(sidecar) if sidecar else None)!=entry.sidecar_sha256:
                    raise ValidationError('import:input_changed')
            except (OSError,ValidationError) as exc:
                self._pause(project,source_id)
                raise ValidationError('import:input_changed') from exc
            with read_snapshot(project) as db:
                duplicate = bool(images.find_by_sha256(db,entry.image_sha256))
            if duplicate:
                submit_write(project,lambda db:commands.skip_duplicate(db,item.item_id,sha256=entry.image_sha256)).result()
                continue
            sent = submit_write(project,lambda db:commands.mark_sent(db,item.item_id)).result().value
            try:
                staging = project.root/'images'/'.pending'/str(sent.item_id)/str(sent.attempts)/('image'+path.suffix.lower())
                staging.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,staging)
                if sha256(staging)!=entry.image_sha256:
                    raise ValidationError('import:input_changed')
                width,height,phash,meta = image_info(staging)
                read = READERS[config['reader']].read(path,params,self.catalog)
                if read.sidecar_sha256!=entry.sidecar_sha256:
                    raise ValidationError('import:input_changed')
                artifact = ImageArtifact(sent.item_id,sent.attempts,staging.relative_to(project.root).as_posix(),
                                         path.suffix.lower(),width,height,phash,entry.image_sha256,staging.stat().st_size,meta,'')
                artifact = prepare_receipt(project.root,artifact,read.originals)
            except (OSError,ValueError,ValidationError) as exc:
                code = str(exc) if isinstance(exc,ValidationError) else 'image:decode_failed'
                submit_write(project,lambda db:commands.fail_item(db,sent.item_id,attempt=sent.attempts,error=code)).result()
                continue
            # CommitUnknown propagates; never fail or replay an unknown commit.
            self._complete(project,artifact,read.originals)
        return submit_write(project,lambda db:commands.transition_source(db,source_id,expected='running',target='done',reason='complete')).result().value

    @staticmethod
    def _pause(project,source_id):
        submit_write(project,lambda db:commands.transition_source(db,source_id,expected='running',target='paused',reason='input_changed')).result()

    @staticmethod
    def _complete(project,artifact,originals):
        def work(db):
            item = sources.get_item(db,artifact.item_id)
            if item.status == 'sent' and images.find_by_sha256(db,artifact.sha256):
                return commands.skip_duplicate(db,item.item_id,sha256=artifact.sha256)
            return commands.complete_item(db,artifact,originals)
        try:
            return submit_write(project,work).result()
        except CommitUnknown:
            # Force a read of durable outcome evidence without replaying the command.
            with read_snapshot(project) as db:
                item = sources.get_item(db,artifact.item_id)
                if item.status == 'done':
                    images.get_image(db,item.image_id)
            raise
