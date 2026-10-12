"""Durable F05 receipt protocol; fingerprints are checked, images decoded by F05.

Receipt JSON schema was unspecified: v1 stores artifact (without digest), original
inputs, complete=true, and optional candidate_id/filename. Digest covers only the
immutable part, so publication updates remain verifiable after a crash.
"""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path

from .errors import ArtifactConflict, ValidationError
from .types import ImageArtifact, OriginalInput, AnnotationInput, CurrentRef, Tag, WriteResult
from .validation import digest, integer, relative_path, sha256, safe_snapshot
from .migrations import atomic_json
from .repositories import images, sources, annotations
from .repositories.common import cas

EXTENSIONS = ('.png','.jpg','.jpeg','.webp','.bmp','.gif','.tif','.tiff')


def receipt_path(root: Path, item_id: int, attempt: int) -> Path:
    integer(item_id,1)
    integer(attempt,1)
    return relative_path(root,f'images/.pending/{item_id}/{attempt}/receipt.json')


def immutable_part(receipt):
    return {k:receipt[k] for k in ('schema_version','complete','artifact','originals')}


def validate_artifact(root, artifact):
    for value in (artifact.item_id,artifact.attempt,artifact.width,artifact.height,artifact.file_size):
        integer(value,1)
    sha256(artifact.sha256)
    if artifact.extension not in EXTENSIONS:
        raise ValidationError('Unsupported or non-lowercase extension')
    path = relative_path(root,artifact.relative_staging_path)
    expected = root / 'images' / '.pending' / str(artifact.item_id) / str(artifact.attempt)
    if path.parent.resolve() != expected.resolve():
        raise ValidationError('Artifact staging must belong to item attempt')
    if artifact.png_meta is not None:
        safe_snapshot(artifact.png_meta)
    return path


def file_matches(path, artifact):
    if not path.is_file() or path.stat().st_size != artifact.file_size:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest() == artifact.sha256


def prepare_receipt(root: Path, artifact: ImageArtifact, originals=()) -> ImageArtifact:
    """F05 calls after decoding/validating and flushing the staging file."""
    path = validate_artifact(root,artifact)
    if not file_matches(path,artifact):
        raise ArtifactConflict('Incomplete staging artifact')
    # Windows _commit requires a writable descriptor.
    with path.open('r+b') as stream:
        os.fsync(stream.fileno())
    payload = asdict(artifact)
    payload.pop('receipt_digest')
    receipt = {'schema_version':1,'complete':True,'artifact':payload,'originals':[asdict(v) for v in originals]}
    receipt['digest'] = digest(immutable_part(receipt))
    target = receipt_path(root,artifact.item_id,artifact.attempt)
    if target.exists():
        old = json.loads(target.read_text(encoding='utf-8'))
        if old['digest'] != receipt['digest']:
            raise ArtifactConflict('Receipt already belongs to different result')
    else:
        atomic_json(target,receipt)
    return replace(artifact,receipt_digest=receipt['digest'])


def read_receipt(root, item_id, attempt):
    path = receipt_path(root,item_id,attempt)
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if receipt.get('schema_version') != 1 or receipt.get('complete') is not True or digest(immutable_part(receipt)) != receipt.get('digest'):
        raise ArtifactConflict('Receipt verification failed')
    artifact = ImageArtifact(**receipt['artifact'],receipt_digest=receipt['digest'])
    if (artifact.item_id,artifact.attempt) != (item_id,attempt):
        raise ArtifactConflict('Receipt belongs to another attempt')
    validate_artifact(root,artifact)
    originals = []
    for data in receipt['originals']:
        data = dict(data)
        if data['type'] == 'tags':
            data['content'] = tuple(Tag(**t) for t in data['content'])
        originals.append(OriginalInput(**data))
    return receipt,artifact,tuple(originals)


def register_artifact(db, artifact, originals):
    from .commands import authority
    from .validation import dumps, now
    item = sources.get_item(db,artifact.item_id)
    if item.attempts != artifact.attempt:
        raise ArtifactConflict('Artifact attempt differs')
    validate_artifact(db.root,artifact)
    if item.status == 'done':
        image = images.get_image(db,item.image_id)
        payload = asdict(artifact)
        payload.pop('receipt_digest')
        evidence_digest = digest({'schema_version':1,'complete':True,'artifact':payload,'originals':[asdict(v) for v in originals]})
        if (image.sha256,image.source_id,image.width,image.height,image.file_size,image.phash,image.png_meta_json) != (artifact.sha256,item.source_id,artifact.width,artifact.height,artifact.file_size,artifact.phash,artifact.png_meta) or artifact.receipt_digest != evidence_digest:
            raise ArtifactConflict('Completed item differs')
        for original in originals:
            stored_original = annotations.get_current(db,image.image_id,'original',original.type)
            if stored_original is None or (stored_original.content,stored_original.confidence,stored_original.params_json,stored_original.cache_key) != (original.content,original.confidence,original.params,original.cache_key):
                raise ArtifactConflict('Original annotations differ')
        if db.execute("SELECT count(*) FROM annotations WHERE image_id=? AND source='original' AND is_current=1",(image.image_id,)).fetchone()[0] != len(originals):
            raise ArtifactConflict('Original annotation set differs')
        db.after_commit.append(lambda: receipt_path(db.root,item.item_id,item.attempts).unlink(missing_ok=True))
        return WriteResult(image,False)
    if item.status != 'sent':
        raise ArtifactConflict('Artifact needs a sent item')
    receipt, stored, stored_originals = read_receipt(db.root,item.item_id,item.attempts)
    if stored != artifact or stored_originals != originals:
        raise ArtifactConflict('Receipt evidence differs')
    staging = relative_path(db.root,artifact.relative_staging_path)
    candidate = receipt.get('candidate_id')
    previous_file = None
    if candidate is not None:
        integer(candidate,1)
        filename = receipt.get('filename')
        if filename != f'{candidate}{artifact.extension}':
            raise ArtifactConflict('Receipt candidate filename differs')
        previous_file = relative_path(db.root / 'images',filename,filename=True)
        occupied = db.execute('SELECT sha256,source_id FROM images WHERE image_id=? OR filename=?',(candidate,filename)).fetchone()
        if occupied:
            # A rolled-back numeric ID is not ownership. Never move a committed
            # image, even if its bytes happen to match the receipt's fingerprint.
            previous_file = None
    source_path = previous_file if previous_file and file_matches(previous_file,artifact) else staging
    if not file_matches(source_path,artifact):
        raise ArtifactConflict('No complete artifact matching receipt')
    identity = db.execute('SELECT coalesce(max(image_id),0)+1 FROM images').fetchone()[0]
    if candidate and candidate >= identity and not db.execute('SELECT 1 FROM images WHERE image_id=?',(candidate,)).fetchone():
        identity = candidate
    # Occupied unrelated filenames are never overwritten or adopted by hash alone.
    while True:
        target = relative_path(db.root / 'images',f'{identity}{artifact.extension}',filename=True)
        if not target.exists() or (previous_file == target and source_path == target):
            break
        identity += 1
    receipt.update(candidate_id=identity,filename=target.name)
    atomic_json(receipt_path(db.root,item.item_id,item.attempts),receipt)
    if source_path != target:
        # Windows rename fails if destination exists. Other OSes use link/unlink
        # to retain the same no-clobber guarantee on the same volume.
        if os.name == 'nt':
            source_path.rename(target)
        else:
            os.link(source_path,target)
            source_path.unlink()
    db.execute('INSERT INTO images(image_id,filename,width,height,phash,sha256,file_size,source_id,png_meta_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (identity,target.name,artifact.width,artifact.height,artifact.phash,artifact.sha256,artifact.file_size,item.source_id,
                dumps(artifact.png_meta) if artifact.png_meta is not None else None,now()))
    with authority(db,'original'):
        for original in originals:
            value = AnnotationInput(identity,'original',original.type,original.content,original.confidence,params=original.params,cache_key=original.cache_key)
            annotations.append_version(db,value,expected=CurrentRef(identity,'original',original.type,None),policy='content')
    cas(db,'source_items',item.item_id,{'status':'done','image_id':identity,'error':None,'updated_at':now()}, {'status':'sent','attempts':artifact.attempt})
    # Cleanup is best effort: committed outcome must not appear failed because an
    # antivirus temporarily holds the receipt. Recovery can clean it next open.
    def cleanup():
        try:
            receipt_path(db.root,item.item_id,item.attempts).unlink(missing_ok=True)
        except OSError:
            pass
    db.after_commit.append(cleanup)
    return WriteResult(images.get_image(db,identity),True)
