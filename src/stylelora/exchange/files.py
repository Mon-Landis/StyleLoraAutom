from contextlib import contextmanager
from threading import RLock
from collections import defaultdict
import hashlib
from stylelora.project_db.types import PageRequest

_leases = defaultdict(RLock)
_guard = RLock()


@contextmanager
def image_lease(project,image_id):
    """Shared per-image lease; future cleanup must acquire this before deleting."""
    with _guard:
        lock = _leases[(project.root,image_id)]
    with lock:
        yield


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def all_pages(get_page):
    page = PageRequest(limit=1000)
    while True:
        result = get_page(page)
        yield from result.items
        if result.next_after_id is None:
            break
        page = PageRequest(result.next_after_id,1000,result.upper_id)
