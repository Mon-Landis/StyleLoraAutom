"""Thread-owned connections, FIFO transactions and WAL read snapshots."""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from queue import Queue
import sqlite3
import threading
from typing import Callable, Iterator, TypeVar

from .errors import Busy, CommitUnknown, ConstraintViolation, Corrupt, DiskFull, StorageError, UnsupportedSQLite, WrongThread
from .types import ProjectHandle

T = TypeVar('T')


def translate(exc: sqlite3.Error) -> Exception:
    code = getattr(exc, 'sqlite_errorcode', 0) & 255
    cls = {sqlite3.SQLITE_BUSY: Busy, sqlite3.SQLITE_LOCKED: Busy,
           sqlite3.SQLITE_FULL: DiskFull, sqlite3.SQLITE_CORRUPT: Corrupt,
           sqlite3.SQLITE_NOTADB: Corrupt, sqlite3.SQLITE_CONSTRAINT: ConstraintViolation}.get(code, StorageError)
    return cls('Database operation failed')


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, isolation_level=None)
    try:
        try:
            json_available = db.execute("SELECT json_valid('{}')").fetchone()[0] == 1
        except sqlite3.Error:
            json_available = False
        if sqlite3.sqlite_version_info < (3, 38) or not json_available:
            raise UnsupportedSQLite('SQLite >=3.38 with JSON required')
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        if readonly:
            db.execute('PRAGMA query_only=ON')
        else:
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise UnsupportedSQLite('WAL unavailable')
            db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise UnsupportedSQLite('Foreign keys unavailable')
        return db
    except BaseException:
        db.close()
        raise


class ReadSession:
    def __init__(self, connection: sqlite3.Connection, root: Path):
        self._connection = connection
        self.root = root
        self._thread = threading.get_ident()
        self._active = True

    def execute(self, sql: str, parameters: tuple | list = ()) -> sqlite3.Cursor:
        if not self._active or self._thread != threading.get_ident():
            raise WrongThread('Session expired or used from another thread')
        return self._connection.execute(sql, parameters)


class WriteSession(ReadSession):
    def __init__(self, connection: sqlite3.Connection, root: Path, writer: WriterQueue | None = None):
        super().__init__(connection, root)
        self.writer = writer
        self.after_commit: list[Callable[[], None]] = []
        self.authority: str | None = None


class WriterQueue:
    def __init__(self, root: Path):
        self.root = root
        self._queue: Queue = Queue()
        self._guard = threading.Lock()
        self._readers = threading.Condition()
        self._reader_count = 0
        self._closed = False
        self._poisoned = False
        self._ready: Future = Future()
        self.cycles: dict[int, tuple[str, bool]] = {}
        self.report = None
        self._thread = threading.Thread(target=self._run, name='stylelora-db', daemon=True)
        self._thread.start()
        self._ready.result()

    def _run(self) -> None:
        try:
            self._connection = connect(self.root / 'project.db')
            self._ready.set_result(None)
        except BaseException as exc:
            self._ready.set_exception(exc)
            return
        while True:
            task = self._queue.get()
            if task is None:
                break
            work, future = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if self._poisoned:
                    raise CommitUnknown('Reopen project to reconcile unknown commit')
                with self.transaction() as session:
                    result = work(session)
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)
        with self._readers:
            self._readers.wait_for(lambda: self._reader_count == 0)
        self.cycles.clear()
        try:
            self._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            self._connection.close()

    def submit(self, work: Callable[[WriteSession], T]) -> Future[T]:
        with self._guard:
            if self._closed:
                raise StorageError('Project closed')
            future: Future[T] = Future()
            self._queue.put((work, future))
            return future

    @contextmanager
    def transaction(self) -> Iterator[WriteSession]:
        if threading.current_thread() is not self._thread:
            raise WrongThread('Write transactions belong to writer thread')
        if self._connection.in_transaction:
            raise WrongThread('Nested transaction prohibited')
        session = WriteSession(self._connection, self.root, self)
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            yield session
            try:
                self._connection.execute('COMMIT')
            except sqlite3.Error as exc:
                self._poisoned = True
                raise CommitUnknown('Commit outcome must be verified; do not replay') from exc
        except BaseException as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            if isinstance(exc, sqlite3.Error):
                raise translate(exc) from exc
            raise
        else:
            for callback in session.after_commit:
                callback()
        finally:
            session._active = False

    def close(self) -> None:
        if threading.current_thread() is self._thread:
            raise WrongThread('Cannot close from writer callback')
        with self._guard:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._thread.join()


def submit_write(project: ProjectHandle, work: Callable[[WriteSession], T]) -> Future[T]:
    return project.writer.submit(work)


def write_transaction(project: ProjectHandle) -> AbstractContextManager[WriteSession]:
    return project.writer.transaction()


@contextmanager
def read_snapshot(project: ProjectHandle) -> Iterator[ReadSession]:
    writer = project.writer
    with writer._guard, writer._readers:
        if writer._closed:
            raise StorageError('Project closed')
        writer._reader_count += 1
    connection = None
    session = None
    try:
        connection = connect(project.root / 'project.db', readonly=True)
        connection.execute('BEGIN')
        # Materialize snapshot immediately, not on a later first repository read.
        connection.execute('SELECT count(*) FROM sqlite_schema').fetchone()
        session = ReadSession(connection, project.root)
        yield session
    finally:
        if session:
            session._active = False
        if connection:
            connection.close()
        with writer._readers:
            writer._reader_count -= 1
            writer._readers.notify_all()
