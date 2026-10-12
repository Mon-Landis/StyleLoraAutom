"""Sanitized domain errors: never expose SQL or external service responses."""


class ProjectError(Exception):
    pass


class ValidationError(ProjectError):
    pass


class UnsafePath(ValidationError):
    pass


class StateConflict(ProjectError):
    pass


class NotFound(ProjectError):
    pass


class ConstraintViolation(StateConflict):
    pass


class LockedSource(StateConflict):
    pass


class SeedConflict(StateConflict):
    pass


class CurrentConflict(StateConflict):
    pass


class SnapshotMismatch(StateConflict):
    pass


class OutcomeConflict(StateConflict):
    pass


class ArtifactConflict(StateConflict):
    pass


class UndoConflict(StateConflict):
    pass


class PreviewStale(StateConflict):
    pass


class InvalidChangeReference(StateConflict):
    pass


class FileMissing(StateConflict):
    pass


class BatchTooLarge(ValidationError):
    pass


class UnsupportedScheme(ValidationError):
    pass


class UnsupportedSQLite(ProjectError):
    pass


class ProjectExists(ProjectError):
    pass


class ProjectLocked(ProjectError):
    pass


class VersionMismatch(ProjectError):
    pass


class NewerProject(VersionMismatch):
    pass


class MigrationFailed(ProjectError):
    pass


class MigrationRecoveryRequired(MigrationFailed):
    pass


class WrongThread(ProjectError):
    pass


class Busy(ProjectError):
    pass


class DiskFull(ProjectError):
    pass


class Corrupt(ProjectError):
    pass


class StorageError(ProjectError):
    pass


class CommitUnknown(StorageError):
    pass
