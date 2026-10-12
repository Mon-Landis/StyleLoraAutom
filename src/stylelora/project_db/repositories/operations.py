from ..types import OperationRow, AnnotationRow, PageRequest
from ..types import Page
from ..connection import ReadSession
from ..errors import InvalidChangeReference, NotFound
from .common import get, row, page as paginate


def get_operation(db: ReadSession, op_id: int) -> OperationRow:
    return get(db,'operation_log',OperationRow,op_id)


def latest_applied(db: ReadSession) -> OperationRow | None:
    value = db.execute("SELECT * FROM operation_log WHERE status='applied' ORDER BY op_id DESC LIMIT 1").fetchone()
    return row(OperationRow,value) if value else None


def list_operations(db: ReadSession, page: PageRequest) -> Page[OperationRow]:
    return paginate(db,'operation_log',OperationRow,page)


def validate_change(db, change):
    if set(change) != {'image_id','new_annotation_id','previous_annotation_id'}:
        raise InvalidChangeReference('Invalid operation change')
    for key in ('new_annotation_id','previous_annotation_id'):
        if change[key] is None and key == 'previous_annotation_id':
            continue
        try:
            annotation = get(db,'annotations',AnnotationRow,change[key])
        except NotFound as exc:
            raise InvalidChangeReference('Missing operation annotation') from exc
        if (annotation.image_id,annotation.source,annotation.type) != (change['image_id'],'rule','tags'):
            raise InvalidChangeReference('Operation annotation provenance mismatch')


def audit_references(db) -> tuple[str, ...]:
    errors = []
    try:
        for value in db.execute('SELECT * FROM operation_log'):
            operation = row(OperationRow,value)
            for change in operation.changes_json:
                validate_change(db,change)
    except (InvalidChangeReference, TypeError, ValueError):
        errors.append('Invalid operation references')
    return tuple(errors)
