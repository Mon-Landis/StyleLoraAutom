from ..errors import FileMissing, StateConflict, ValidationError
from collections.abc import Sequence
from ..connection import ReadSession, WriteSession
from ..types import Page, ReviewStatus
from ..types import ImageRow, ImageFilter, PageRequest, RatingValue, WriteResult
from ..validation import choice, sha256 as validate_sha256, timestamp
from ..validation import relative_path
import hashlib
from .common import get, page as paginate, row, cas


def get_image(db: ReadSession, image_id: int) -> ImageRow:
    return get(db, 'images', ImageRow, image_id)


def matches(image: ImageRow, query: ImageFilter) -> bool:
    return ((query.source_ids is None or image.source_id in query.source_ids)
            and (query.review_statuses is None or image.review_status in query.review_statuses)
            and (query.include_missing or not image.file_missing))


def list_images(db: ReadSession, query: ImageFilter, page: PageRequest) -> Page[ImageRow]:
    return paginate(db, 'images', ImageRow, page, lambda r: matches(r, query))


def find_by_sha256(db: ReadSession, sha256: str) -> tuple[ImageRow, ...]:
    validate_sha256(sha256)
    return tuple(row(ImageRow, r) for r in db.execute('SELECT * FROM images WHERE sha256=? ORDER BY image_id', (sha256,)))


def set_review(db: WriteSession, image_id: int, *, expected: ReviewStatus, target: ReviewStatus) -> WriteResult[ImageRow]:
    choice(target, ('unreviewed', 'keep', 'reject'))
    image = get_image(db, image_id)
    if image.review_status == target:
        return WriteResult(image, False)
    if image.review_status != expected:
        raise StateConflict('Review changed')
    if image.file_missing and target != 'reject':
        raise FileMissing('Cannot restore review without verified file')
    cas(db, 'images', image_id, {'review_status': target}, {'review_status': expected})
    return WriteResult(get_image(db, image_id), True)


def set_file_missing(db: WriteSession, image_id: int, *, missing: bool, verified_sha256: str | None = None) -> WriteResult[ImageRow]:
    image = get_image(db, image_id)
    if type(missing) is not bool:
        raise ValidationError('Boolean required')
    if not missing:
        path = relative_path(db.root / 'images',image.filename,filename=True)
        if verified_sha256 != image.sha256 or not path.is_file():
            raise FileMissing('Restored file requires matching fingerprint')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest() != image.sha256:
                raise FileMissing('Restored file fingerprint differs')
    if missing == image.file_missing:
        return WriteResult(image, False)
    cas(db, 'images', image_id, {'file_missing': int(missing)}, {'file_missing': int(image.file_missing)})
    return WriteResult(get_image(db, image_id), True)


def rating_of(image: ImageRow) -> RatingValue | None:
    return None if image.rating is None else RatingValue(image.rating, image.rating_source, image.rating_verified, image.rated_at)


def set_rating(db: WriteSession, image_id: int, *, expected: RatingValue | None, value: RatingValue | None) -> WriteResult[ImageRow]:
    image = get_image(db, image_id)
    if value:
        choice(value.rating, ('general', 'sensitive', 'questionable', 'explicit'))
        choice(value.source, ('keyword', 'model', 'fallback', 'refusal', 'manual'))
        timestamp(value.rated_at)
        if type(value.verified) is not bool or (value.source == 'fallback' and value.verified) or (value.source in ('manual','refusal') and not value.verified) or (value.source == 'refusal' and value.rating != 'questionable'):
            raise ValidationError('Inconsistent rating')
    if rating_of(image) == value:
        return WriteResult(image, False)
    if rating_of(image) != expected:
        raise StateConflict('Rating changed')
    cas(db, 'images', image_id, {'rating': value.rating if value else None, 'rating_source': value.source if value else None,
                               'rating_verified': int(value.verified) if value else 0, 'rated_at': value.rated_at if value else None},
        {'rating': image.rating, 'rating_source': image.rating_source, 'rating_verified': int(image.rating_verified), 'rated_at': image.rated_at})
    return WriteResult(get_image(db, image_id), True)


def clear_recomputable_ratings(db: WriteSession, image_ids: Sequence[int]) -> int:
    count = 0
    for identity in set(image_ids):
        image = get_image(db, identity)
        if image.rating_source in ('keyword', 'model', 'fallback'):
            count += set_rating(db, identity, expected=rating_of(image), value=None).changed
    return count
