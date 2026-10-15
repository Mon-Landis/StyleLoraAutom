"""Explicit installation with injected HF transport; never downloads at startup."""
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import AsyncIterator, Protocol
from uuid import uuid4
from stylelora.executors.cache import digest
from stylelora.executors.types import Unavailable


@dataclass(frozen=True)
class ModelFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    repo_id: str
    revision: str
    files: tuple[ModelFile, ...]
    tensor_schema: dict
    preprocess_version: str
    digest: str


class HfDownloader(Protocol):
    def stream(self, repo_id: str, revision: str, filename: str) -> AsyncIterator[bytes]: ...


def manifest_digest(manifest):
    value = asdict(manifest)
    value.pop('digest')
    return digest(value)


def validate_manifest(manifest):
    safe = lambda s: isinstance(s, str) and re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', s) and s not in ('.', '..')
    if (not safe(manifest.model_id) or not re.fullmatch(r'[0-9a-f]{40}', manifest.revision)
            or not re.fullmatch(r'[\w.-]+/[\w.-]+', manifest.repo_id)
            or manifest_digest(manifest) != manifest.digest or not manifest.preprocess_version):
        raise Unavailable('manifest_invalid')
    if len(manifest.files) != 2 or len({f.name for f in manifest.files}) != 2:
        raise Unavailable('manifest_invalid')
    if {Path(f.name).suffix for f in manifest.files} != {'.onnx', '.csv'}:
        raise Unavailable('manifest_invalid')
    schema = manifest.tensor_schema
    if not isinstance(schema, dict) or schema.get('csv_schema') != {'name': 'name', 'category': 'category'}:
        raise Unavailable('manifest_invalid')
    for file in manifest.files:
        if (not safe(file.name) or not re.fullmatch(r'[0-9a-f]{64}', file.sha256)
                or type(file.size) is not int or file.size <= 0):
            raise Unavailable('manifest_invalid')


class ModelStore:
    def __init__(self, root=None, *, supervisor=None):
        self.root = Path(root) if root is not None else Path(os.environ['LOCALAPPDATA'])/'StyleLoraAutom/models/wd'
        self._locks = {}
        self.supervisor = supervisor

    def directory(self, manifest):
        validate_manifest(manifest)
        return self.root/manifest.model_id/manifest.revision

    def verify_model(self, manifest):
        directory = self.directory(manifest)
        try:
            if directory.is_symlink() or not directory.resolve().is_relative_to(self.root.resolve()):
                raise Unavailable('manifest_invalid')
            if json.loads((directory/'ready.json').read_text()) != asdict_json(manifest):
                raise Unavailable('manifest_invalid')
            for file in manifest.files:
                path = directory/file.name
                if path.is_symlink() or path.stat().st_size != file.size:
                    raise Unavailable('checksum_mismatch')
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != file.sha256:
                        raise Unavailable('checksum_mismatch')
        except (OSError, ValueError) as exc:
            raise Unavailable('model_missing') from exc
        return directory

    async def install_model(self, manifest, *, downloader, cancel, _supervised=False):
        if self.supervisor is not None and not _supervised:
            record = self.supervisor.submit('model_download',
                lambda: self.install_model(manifest, downloader=downloader, cancel=cancel, _supervised=True),
                cancel=cancel.cancel)
            identity = record['operation_id']
            await asyncio.shield(self.supervisor.tasks[identity])
            if identity in self.supervisor.failures:
                raise self.supervisor.failures[identity]
            cancel.raise_if_cancelled()
            return self.verify_model(manifest)
        directory = self.directory(manifest)
        lock = self._locks.setdefault(str(directory), asyncio.Lock())
        async with lock:
            cancel.raise_if_cancelled()
            if directory.exists():
                return self.verify_model(manifest)
            directory.parent.mkdir(parents=True, exist_ok=True)
            if not directory.parent.resolve().is_relative_to(self.root.resolve()):
                raise Unavailable('manifest_invalid')
            staging = directory.parent/(manifest.revision+'.'+uuid4().hex+'.partial')
            staging.mkdir()
            try:
                for file in manifest.files:
                    checksum, size = hashlib.sha256(), 0
                    with (staging/file.name).open('wb') as output:
                        async for chunk in downloader.stream(manifest.repo_id, manifest.revision, file.name):
                            cancel.raise_if_cancelled()
                            size += len(chunk)
                            if size > file.size:
                                raise Unavailable('checksum_mismatch')
                            checksum.update(chunk)
                            output.write(chunk)
                    if size != file.size or checksum.hexdigest() != file.sha256:
                        raise Unavailable('checksum_mismatch')
                cancel.raise_if_cancelled()
                (staging/'ready.json').write_text(json.dumps(asdict_json(manifest)), encoding='utf-8')
                staging.rename(directory)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return self.verify_model(manifest)


def asdict_json(manifest):
    return json.loads(json.dumps(asdict(manifest)))
