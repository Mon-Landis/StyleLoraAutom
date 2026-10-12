"""Trusted package migration registry and recoverable DB/metadata publication."""
from dataclasses import asdict, dataclass
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Callable
from uuid import uuid4

from .connection import ReadSession, WriteSession
from .errors import MigrationFailed, MigrationRecoveryRequired, NewerProject, VersionMismatch
from .types import MigrationInfo, ProjectMetadata
from .validation import dumps, relative_path

CURRENT_VERSION = 1


@dataclass(frozen=True)
class Migration:
    info: MigrationInfo
    upgrade: Callable[[WriteSession], None]


# Version 0 is unpublished initialization, never an accepted old project.
REGISTRY: tuple[Migration, ...] = ()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def initialize(connection: sqlite3.Connection) -> None:
    sql = (Path(__file__).parent / 'migrations' / '0001_initial.sql').read_text(encoding='utf-8')
    connection.execute('BEGIN IMMEDIATE')
    try:
        for statement in sql.split(';'):
            if statement.strip():
                connection.execute(statement)
        from .repositories.schemes import initialize_defaults
        initialize_defaults(WriteSession(connection, Path('.')))
        connection.execute('PRAGMA user_version=1')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def plan_migrations(format_version: int, user_version: int) -> tuple[MigrationInfo, ...]:
    if max(format_version, user_version) > CURRENT_VERSION:
        raise NewerProject('Project requires a newer application')
    if format_version != user_version or format_version < 1:
        raise VersionMismatch('Untrusted or mismatched project versions')
    result = []
    version = user_version
    while version < CURRENT_VERSION:
        step = next((x for x in REGISTRY if x.info.from_version == version), None)
        if step is None or step.info.to_version != version + 1:
            raise VersionMismatch('Missing trusted migration step')
        result.append(step.info)
        version += 1
    return tuple(result)


def verify_integrity(db: ReadSession, *, full: bool = False) -> tuple[str, ...]:
    errors = []
    for row in db.execute('PRAGMA integrity_check' if full else 'PRAGMA quick_check'):
        if row[0] != 'ok':
            errors.append('SQLite integrity failure')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        errors.append('Foreign key violation')
    expected_tables = {'sources', 'source_items', 'images', 'annotations', 'jobs', 'operation_log', 'export_schemes', 'import_schemes'}
    actual = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if actual != expected_tables:
        errors.append('Unexpected business schema')
    if actual == expected_tables:
        from .types import SourceRow, SourceItemRow, ImageRow, AnnotationRow, JobRow, OperationRow, ExportSchemeRow, ImportSchemeRow
        from dataclasses import fields
        for table, dto in zip(('sources','source_items','images','annotations','jobs','operation_log','export_schemes','import_schemes'),
                              (SourceRow,SourceItemRow,ImageRow,AnnotationRow,JobRow,OperationRow,ExportSchemeRow,ImportSchemeRow)):
            if [r[1] for r in db.execute(f'PRAGMA table_info({table})')] != [f.name for f in fields(dto)]:
                errors.append('Unexpected schema columns')
        if db.execute("SELECT count(*) FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL").fetchone()[0] != 15:
            errors.append('Unexpected indexes')
        if db.execute('SELECT count(*) FROM import_schemes WHERE is_default=1').fetchone()[0] != 1:
            errors.append('Missing default import scheme')
        if not db.execute('SELECT 1 FROM export_schemes LIMIT 1').fetchone():
            errors.append('Missing export scheme')
        from .repositories.operations import audit_references
        errors.extend(audit_references(db))
    return tuple(errors)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate_project(root: Path, *, target_version: int) -> ProjectMetadata:
    # Public migration acquires the same OS lock; open uses the locked helper.
    from .project import ProjectLock
    with ProjectLock(root):
        return migrate_locked(root, target_version=target_version)


def migrate_locked(root: Path, *, target_version: int) -> ProjectMetadata:
    if target_version != CURRENT_VERSION:
        raise VersionMismatch('Only current target supported')
    meta_path = root / 'project.json'
    marker = root / '.migration-state.json'
    metadata = ProjectMetadata(**json.loads(meta_path.read_text(encoding='utf-8')))
    connection = sqlite3.connect((root / 'project.db').as_uri() + '?mode=rw', uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if max(version, metadata.format_version) > CURRENT_VERSION:
            raise NewerProject('Project requires a newer application')
        state = None
        if marker.exists():
            state = json.loads(marker.read_text(encoding='utf-8'))
            steps = plan_migrations(state['from'], state['from'])
            if state['to'] != target_version or state['checksums'] != [s.checksum for s in steps]:
                raise MigrationRecoveryRequired('Untrusted migration checksum')
            backup = relative_path(root, state['backup'])
            if not (backup / 'project.db').is_file() or _hash(backup / 'project.json') != state['old_hash']:
                raise MigrationRecoveryRequired('Incomplete migration backup')
            old_metadata = json.loads((backup/'project.json').read_text(encoding='utf-8'))
            target_metadata = {**old_metadata,'format_version':target_version}
            if state['metadata'] != target_metadata or hashlib.sha256(dumps(target_metadata).encode()).hexdigest() != state['new_hash']:
                raise MigrationRecoveryRequired('Target metadata does not match trusted transition')
            if _hash(meta_path) not in (state['old_hash'], state['new_hash']):
                raise MigrationRecoveryRequired('Metadata edited during migration')
            if state.get('phase') == 'restoring':
                raise MigrationRecoveryRequired('Explicit backup restore must be completed')
            if version == target_version:
                if verify_integrity(ReadSession(connection, root), full=True):
                    raise MigrationRecoveryRequired('Upgraded schema invalid')
                atomic_json(meta_path, state['metadata'])
                marker.unlink()
                return ProjectMetadata(**state['metadata'])
            if version != state['from'] or metadata.format_version != state['from']:
                raise MigrationRecoveryRequired('Unknown mixed migration state')
        steps = plan_migrations(metadata.format_version, version)
        if not steps:
            return metadata
        if shutil.disk_usage(root).free < (root / 'project.db').stat().st_size * 3 + 1024 * 1024:
            raise MigrationFailed('Insufficient migration space')
        target = {**asdict(metadata), 'format_version': target_version}
        if state is None:
            backup = root / '.migration-backup' / f'{version}-{uuid4().hex}'
            backup.mkdir(parents=True)
            with closing(sqlite3.connect(backup / 'project.db')) as destination:
                connection.backup(destination)
            shutil.copyfile(meta_path, backup / 'project.json')
            state = {'from': version, 'to': target_version, 'backup': backup.relative_to(root).as_posix(),
                     'checksums': [s.checksum for s in steps], 'old_hash': _hash(meta_path),
                     'new_hash': hashlib.sha256(dumps(target).encode()).hexdigest(), 'metadata': target, 'phase': 'prepared'}
            atomic_json(marker, state)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        try:
            for info in steps:
                step = next(s for s in REGISTRY if s.info == info)
                if connection.execute('PRAGMA user_version').fetchone()[0] != info.from_version:
                    raise MigrationFailed('Migration version changed')
                step.upgrade(WriteSession(connection, root))
                connection.execute(f'PRAGMA user_version={info.to_version}')
            if verify_integrity(ReadSession(connection, root), full=True):
                raise MigrationFailed('Migration integrity failure')
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            raise MigrationFailed('Migration rolled back') from exc
        atomic_json(meta_path, target)
        marker.unlink()
        return ProjectMetadata(**target)
    finally:
        connection.close()


def restore_migration_backup(root: Path) -> ProjectMetadata:
    """Explicit recovery restores both files under lock, retaining the backup.

    The marker is retained until both publications finish, so interruption is
    detectable and the same explicit restore can safely be repeated.
    """
    from .project import ProjectLock
    with ProjectLock(root):
        marker = root / '.migration-state.json'
        state = json.loads(marker.read_text(encoding='utf-8'))
        steps = plan_migrations(state['from'],state['from'])
        if state['checksums'] != [s.checksum for s in steps]:
            raise MigrationRecoveryRequired('Untrusted backup marker')
        backup = relative_path(root,state['backup'])
        if _hash(backup/'project.json') != state['old_hash']:
            raise MigrationRecoveryRequired('Backup metadata differs')
        metadata = ProjectMetadata(**json.loads((backup/'project.json').read_text(encoding='utf-8')))
        with closing(sqlite3.connect((backup/'project.db').as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory = sqlite3.Row
            if source.execute('PRAGMA user_version').fetchone()[0] != state['from'] or verify_integrity(ReadSession(source,root),full=True):
                raise MigrationRecoveryRequired('Backup database invalid')
            state['phase'] = 'restoring'
            atomic_json(marker,state)
            # backup() writes through SQLite and handles existing WAL correctly;
            # replacing project.db alone would leave incompatible live WAL pages.
            with closing(sqlite3.connect(root/'project.db')) as destination:
                source.backup(destination)
                destination.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        atomic_json(root/'project.json',asdict(metadata))
        marker.unlink()
        return metadata
