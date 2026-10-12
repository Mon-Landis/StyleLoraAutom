"""Project directory publication, exclusive OS lock, lifecycle and metadata."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

from .connection import WriterQueue, ReadSession, connect
from .errors import ProjectExists, ProjectLocked, VersionMismatch, Corrupt, ValidationError
from .types import ProjectHandle, ProjectMetadata
from .validation import now, text, timestamp
from . import migrations

_guard = threading.RLock()
_open: dict[Path, tuple[ProjectHandle, 'ProjectLock']] = {}


class ProjectLock:
    def __init__(self, root: Path):
        self.path = root / '.project.lock'
        self.file = None

    def __enter__(self):
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'\0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ProjectLocked('Project is open in another process') from exc
        return self

    def __exit__(self,*args):
        if self.file:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def _root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute() or root.suffix != '.slproj':
        raise ValidationError('Absolute .slproj directory required')
    return root.resolve()


def create_project(root: Path, *, name: str, trigger_word: str) -> ProjectHandle:
    root = _root(root)
    text(name,nonempty=True)
    text(trigger_word)
    with _guard:
        if root.exists():
            raise ProjectExists('Project already exists')
        if _open:
            raise ProjectLocked('Close current project before creating another')
        temporary = Path(tempfile.mkdtemp(prefix='.stylelora-',dir=root.parent))
        try:
            for directory in ('images','cache','exports'):
                (temporary/directory).mkdir()
            metadata = ProjectMetadata(1,name,trigger_word,now())
            migrations.atomic_json(temporary/'project.json',asdict(metadata))
            connection = sqlite3.connect(temporary/'project.db',isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute('PRAGMA foreign_keys=ON')
                migrations.initialize(connection)
                if migrations.verify_integrity(ReadSession(connection,temporary),full=True):
                    raise Corrupt('Initialized project failed integrity validation')
            finally:
                connection.close()
            if root.exists():
                raise ProjectExists('Project appeared during creation')
            temporary.rename(root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return open_project(root)


def open_project(root: Path, *, migrate: bool = True) -> ProjectHandle:
    root = _root(root)
    with _guard:
        if root in _open:
            return _open[root][0]
        if _open:
            raise ProjectLocked('Only one project may be open')
        for entry in ('project.json','project.db','images','cache','exports'):
            if not (root/entry).resolve().is_relative_to(root):
                raise ValidationError('Project component escapes root')
            if not (root/entry).exists():
                # Cache is expendable; recreate only after version verification.
                if entry != 'cache':
                    raise ValidationError('Incomplete project container')
        lock = ProjectLock(root)
        lock.__enter__()
        writer = None
        try:
            metadata = ProjectMetadata(**json.loads((root/'project.json').read_text(encoding='utf-8')))
            if type(metadata.format_version) is not int:
                raise VersionMismatch('Invalid format version')
            text(metadata.name,nonempty=True)
            text(metadata.trigger_word)
            timestamp(metadata.created_at)
            connection = connect(root/'project.db',readonly=True)
            try:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if max(version,metadata.format_version)>migrations.CURRENT_VERSION:
                    from .errors import NewerProject
                    raise NewerProject('Newer project version')
                marker = (root/'.migration-state.json').exists()
                if not marker:
                    steps = migrations.plan_migrations(metadata.format_version,version)
                    if steps and not migrate:
                        raise VersionMismatch('Migration required')
                elif not migrate:
                    raise VersionMismatch('Migration recovery required')
            finally:
                connection.close()
            if marker or version != migrations.CURRENT_VERSION:
                metadata = migrations.migrate_locked(root,target_version=migrations.CURRENT_VERSION)
            connection = connect(root/'project.db',readonly=True)
            try:
                if migrations.verify_integrity(ReadSession(connection,root)):
                    raise Corrupt('Project integrity check failed')
            finally:
                connection.close()
            (root/'cache').mkdir(exist_ok=True)
            writer = WriterQueue(root)
            handle = ProjectHandle(root,metadata,migrations.CURRENT_VERSION,writer)
            from .recovery import recover_project
            writer.report = recover_project(handle)
            _open[root] = (handle,lock)
            return handle
        except BaseException:
            if writer:
                writer.close()
            lock.__exit__()
            raise


def close_project(project: ProjectHandle) -> None:
    with _guard:
        entry = _open.get(project.root)
        if entry is None or entry[0] is not project:
            return
        project.writer.close()
        entry[1].__exit__()
        del _open[project.root]


def update_metadata(project: ProjectHandle, *, name: str, trigger_word: str) -> ProjectMetadata:
    text(name,nonempty=True)
    text(trigger_word)
    with _guard:
        if project.root not in _open:
            raise ProjectLocked('Project not open')
        metadata = ProjectMetadata(project.schema_version,name,trigger_word,project.metadata.created_at)
        migrations.atomic_json(project.root/'project.json',asdict(metadata))
        object.__setattr__(project,'metadata',metadata)
        return metadata
