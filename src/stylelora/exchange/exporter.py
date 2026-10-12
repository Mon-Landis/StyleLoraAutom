"""Freeze DAL inputs once, then publish complete image/caption pairs by copy."""
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import errno
from pathlib import Path
import shutil
from uuid import uuid4
from stylelora.project_db.project import read_project_snapshot
from stylelora.project_db.repositories import images, sources, schemes
from stylelora.project_db.types import ImageFilter, SourceFilter
from stylelora.project_db.validation import relative_path
from stylelora.project_db.errors import ValidationError, UnsupportedScheme
from stylelora.annotations.projection import read_rows
from stylelora.annotations.merge import merge_annotations
from stylelora.caption.assembler import assemble_caption
from stylelora.caption.blocklist import build_blocklist, check_blocklist
from stylelora.caption.sorters import PyramidSorter, SortContext
from stylelora.tagging.catalog import load_catalog
from stylelora.tagging.jsonio import atomic_json, canonical_json
from stylelora.tagging.types import Issue
from stylelora.tagging.trigger_validation import validate_trigger, override_digest
from .schemes import validate_scheme, caption_config, output_config
from .files import all_pages, image_lease, sha256
from .pipeline import execute_pipeline
from .report import ExportReport, ImageResult
from .types import FileResult
from .writers import WRITERS


class ExportCanceled(ValidationError):
    pass


def _utc():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def export_project(project,scheme_id,*,request_key=None,trigger_override_digest=None,catalog=None,
                   writer=None,cancel=None,clock=_utc,after_freeze=None):
    catalog = catalog if catalog is not None else load_catalog()
    with read_project_snapshot(project) as (db,metadata):
        row = schemes.get_export_scheme(db,scheme_id)
        scheme = validate_scheme({k:getattr(row,k) for k in ('name','exporter','filter_json','caption_config_json','output_config_json')} |
                                 {'pipeline_json':list(row.pipeline_json)})
        config = caption_config(scheme.caption_config_json)
        output = output_config(scheme.output_config_json)
        execute_pipeline(None,scheme.pipeline_json)
        source_rows = tuple(all_pages(lambda p:sources.list_sources(db,SourceFilter(),p)))
        query = ImageFilter(source_ids=tuple(scheme.filter_json['source_ids']) if scheme.filter_json['source_ids'] is not None else None,
                            include_missing=True)
        selected = tuple(all_pages(lambda p:images.list_images(db,query,p)))
        frozen = tuple((image,read_rows(db,image.image_id)[0]) for image in selected)
    warnings = []
    style_prompts = tuple(s.style_prompt for s in source_rows) or ('',)
    for style in style_prompts:
        issues = validate_trigger(metadata.trigger_word,style,catalog=catalog)
        if any(i.severity == 'error' for i in issues):
            raise ValidationError(next(i.code for i in issues if i.severity == 'error'))
        warnings.extend(issues)
    if any(i.code == 'trigger:existing_tag' for i in warnings) and trigger_override_digest != override_digest(metadata.trigger_word,catalog):
        raise ValidationError('trigger:existing_tag')
    guards = {s.source_id:build_blocklist(s.style_prompt,catalog,mode=config.blocklist_mode) for s in source_rows}
    sorter = PyramidSorter(SortContext(catalog.revision,categories=getattr(catalog,'entries',{}))) if config.tag_sort == 'prompt_pyramid' else None
    writer = writer or WRITERS[scheme.exporter]
    plans, used = [], set()
    for image,rows in frozen:
        reason = ('export:file_missing' if image.file_missing else 'export:reject' if image.review_status == 'reject'
                  else 'export:review_filter' if image.review_status not in scheme.filter_json['review_statuses'] else None)
        name = output.filename.format(image_id=image.image_id,ext=Path(image.filename).suffix,stem=Path(image.filename).stem)
        names = (name,Path(name).with_suffix('.txt').name) + ((Path(name).with_suffix('.json').name,) if output.include_json else ())
        folded = {n.casefold() for n in names}
        if not reason and (len(folded)!=len(names) or folded & used):
            if output.overwrite == 'error':
                raise ValidationError('export:name_collision')
            reason = 'export:name_collision'
        if not reason:
            used.update(folded)
        plans.append((image,rows,reason,names))
    if after_freeze:
        after_freeze()
    export_id = clock()+'-'+uuid4().hex[:8]
    parent = relative_path(project.root,f'exports/{scheme_id}')
    parent.mkdir(parents=True,exist_ok=True)
    partial = parent/('.partial-'+export_id)
    final = parent/export_id
    partial.mkdir()
    destination = relative_path(partial,output.subdirectory) if output.subdirectory else partial
    destination.mkdir(parents=True,exist_ok=True)
    results, files, hits = [], [], []
    frequency, skipped, failures = Counter(), Counter(), Counter()
    exported = 0
    try:
        for image,rows,reason,names in plans:
            if cancel and cancel():
                raise ExportCanceled('export:canceled')
            if reason:
                skipped[reason] += 1
                results.append(ImageResult(image.image_id,'skipped',reason))
                continue
            paths = tuple(destination/name for name in names)
            try:
                view = merge_annotations(rows,config)
                if output.require_nonempty_tags and not view.tags:
                    raise ValidationError('caption:empty_tags')
                guard = guards[image.source_id]
                image_hits = check_blocklist(view.tags,view.nl,guard)
                hits.extend(replace(h,image_id=image.image_id,action=guard.mode) for h in image_hits)
                caption = assemble_caption(view,config,metadata.trigger_word,guard,sorter)
                with image_lease(project,image.image_id):
                    source = relative_path(project.root/'images',image.filename,filename=True)
                    if not source.is_file() or sha256(source)!=image.sha256:
                        raise ValidationError('export:image_changed')
                    pair = [writer.write_image(source,paths[0],mode='copy'),
                            writer.write_caption(paths[1],caption.serialize(),encoding=output.caption_encoding)]
                    if sha256(source)!=image.sha256 or pair[0].sha256!=image.sha256:
                        raise ValidationError('export:image_changed')
                if output.include_json:
                    with paths[2].open('xb') as stream:
                        stream.write(canonical_json(image.png_meta_json).encode('utf-8'))
                        stream.flush()
                        import os
                        os.fsync(stream.fileno())
                    pair.append(FileResult(paths[2].name,paths[2].stat().st_size,sha256(paths[2]),'metadata'))
                if any(sha256(path)!=result.sha256 for path,result in zip(paths,pair,strict=True)):
                    raise ValidationError('export:write_failed')
                if cancel and cancel():
                    raise ExportCanceled('export:canceled')
                pair = tuple(replace(result,path=path.relative_to(partial).as_posix()) for path,result in zip(paths,pair,strict=True))
                exported += 1
                files.extend(pair)
                frequency.update({t.tag for t in caption.tags})
                warnings.extend(Issue(code,field=str(image.image_id)) for code in caption.warnings)
                results.append(ImageResult(image.image_id,'exported',None,pair))
            except ExportCanceled:
                raise
            except (OSError,ValidationError) as exc:
                for path in paths:
                    path.unlink(missing_ok=True)
                if isinstance(exc,OSError) and exc.errno in (errno.ENOSPC,errno.EDQUOT):
                    raise
                reason = str(exc) if isinstance(exc,ValidationError) else 'export:write_failed'
                failures[reason] += 1
                results.append(ImageResult(image.image_id,'failed',reason))
        report = ExportReport(1,export_id,final.relative_to(project.root).as_posix(),len(selected),exported,
                              sum(skipped.values()),sum(failures.values()),dict(skipped),dict(failures),tuple(hits),
                              dict(sorted(frequency.items())),exported,tuple(files),tuple(warnings),tuple(results),
                              trigger_override_digest=trigger_override_digest)
        atomic_json(partial/'report.json',asdict(report))
        if cancel and cancel():
            raise ExportCanceled('export:canceled')
        if final.exists():
            raise ValidationError('export:directory_exists')
        partial.rename(final)
        return report
    except BaseException:
        # Never publish incomplete sessions. Keep an inspectable interruption receipt.
        for child in tuple(partial.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        try:
            atomic_json(partial/'interrupted.json',{'schema_version':1,'export_id':export_id,'status':'interrupted'})
        except OSError:
            pass
        raise
