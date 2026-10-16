from fastapi.responses import JSONResponse
from stylelora.runtime.contracts import Failure
from stylelora.project_db import errors as db
from stylelora.llm.types import LlmFailure


def translate(exc):
    from stylelora.modes.profiles import ProfileError
    if isinstance(exc, ProfileError):
        return Failure(422, exc.code)
    if isinstance(exc, Failure):
        return exc
    from stylelora.rules.service import PreviewExpired
    from stylelora.prompts.types import PromptError
    from stylelora.backends.types import BackendError
    if isinstance(exc, PreviewExpired):
        return Failure(410, 'preview:expired')
    if isinstance(exc, PromptError):
        from stylelora.prompts.types import plain
        status = {'dictionary:revision_conflict': 412, 'project:closed': 409,
                  'completion:stale': 409, 'completion:unavailable': 503,
                  'storage:io_error': 500, 'storage:corrupt_snapshot': 500,
                  'preview:expired': 410, 'image:too_large': 413, 'import:limit': 413,
                  'import:unsupported': 422}.get(exc.code, 404 if 'not_found' in exc.code else 409 if 'conflict' in exc.code else 422)
        return Failure(status, exc.code, details={'issues': plain(exc.issues)})
    if isinstance(exc, BackendError):
        return Failure(503, 'dependency:unavailable')
    for cls, status, code in (
        (db.CommitUnknown, 503, 'storage:commit_unknown'),
        (db.BatchTooLarge, 413, 'batch:too_large'),
        (db.UnsupportedScheme, 501, 'feature:unsupported'),
        (db.NotFound, 404, 'resource:not_found'),
        (db.ProjectLocked, 423, 'project:locked'),
        (db.ProjectExists, 409, 'project:exists'),
        (db.VersionMismatch, 409, 'project:version_mismatch'),
        (db.FileMissing, 409, 'image:file_missing'),
        (db.StateConflict, 409, 'concurrency:version_conflict'),
        (db.ValidationError, 422, 'input:invalid'),
        (db.Busy, 503, 'storage:busy'),
        (db.DiskFull, 503, 'storage:disk_full'),
        (db.Corrupt, 500, 'storage:corrupt'),
    ):
        if isinstance(exc, cls):
            return Failure(status, code)
    if isinstance(exc, LlmFailure):
        code = exc.code if hasattr(exc, 'code') else 'dependency:unavailable'
        return Failure(422 if code == 'resource:invalid_policy' else 409 if 'conflict' in code else 503, code)
    return Failure(500, 'internal:error')


def response(exc, request_id):
    error = translate(exc)
    unknown = error.code == 'storage:commit_unknown'
    return JSONResponse({'error': dict(code=error.code, message=error.code, retryable=False,
                        retry_action='reconcile' if unknown else 'read' if error.status in (409, 428) else 'user' if error.status in (401, 422) else 'none',
                        outcome='unknown' if unknown else None, details=error.details, issues=[], request_id=request_id)},
                        status_code=error.status, headers={'X-Request-ID': request_id})
