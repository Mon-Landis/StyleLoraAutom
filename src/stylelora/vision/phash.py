"""phash64-v1: Pillow 12 Lanczos and separable, unnormalised DCT-II."""
import asyncio
from dataclasses import dataclass
from math import cos, pi
import re
from statistics import median
from PIL import Image, ImageOps
from stylelora.project_db import read_snapshot
from stylelora.project_db.repositories.images import get_image
from stylelora.executors.scope import resolve_in_snapshot

_COS = tuple(tuple(cos(pi * (2*x+1)*u/64) for x in range(32)) for u in range(8))
_HASH = re.compile(r'[0-9a-f]{16}\Z')


@dataclass(frozen=True)
class HashRecord:
    image_id: int
    phash: str


@dataclass(frozen=True)
class DuplicatePair:
    left_id: int
    right_id: int
    distance: int


def compute_phash(path, *, algorithm='phash64-v1'):
    if algorithm != 'phash64-v1':
        raise ValueError('unsupported_algorithm')
    with Image.open(path) as source:
        source.seek(0)
        rgba = ImageOps.exif_transpose(source).convert('RGBA')
        white = Image.new('RGBA', rgba.size, 'white')
        pixels = list(Image.alpha_composite(white, rgba).convert('L').resize(
            (32, 32), Image.Resampling.LANCZOS).getdata())
    rows = [[sum(pixels[y*32+x]*_COS[u][x] for x in range(32)) for u in range(8)]
            for y in range(32)]
    coefficients = [sum(rows[y][u]*_COS[v][y] for y in range(32))
                    for v in range(8) for u in range(8)]
    middle = median(coefficients)
    bits = 0
    for value in coefficients:
        bits = (bits << 1) | (value > middle)
    return f'{bits:016x}'


def hamming(left, right):
    if not all(isinstance(v, str) and _HASH.fullmatch(v) for v in (left, right)):
        raise ValueError('invalid_hash')
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _threshold(value):
    if type(value) is not int or not 0 <= value <= 64:
        raise ValueError('threshold_invalid')


def find_candidates(records, *, threshold):
    _threshold(threshold)
    records = sorted(records, key=lambda r: r.image_id)
    if len({r.image_id for r in records}) != len(records):
        raise ValueError('duplicate_image_id')
    result = []
    for index, left in enumerate(records):
        for right in records[index+1:]:
            distance = hamming(left.phash, right.phash)
            if distance <= threshold:
                result.append(DuplicatePair(left.image_id, right.image_id, distance))
    return tuple(sorted(result, key=lambda p: (p.distance, p.left_id, p.right_id)))


async def scan_duplicates(project, scope, *, threshold, cancel, report=None):
    _threshold(threshold)
    cancel.raise_if_cancelled()
    counts = {'missing': 0, 'null': 0, 'invalid_hash': 0}
    records = []
    with read_snapshot(project) as db:
        ids = resolve_in_snapshot(db, scope, mode='all')
        if len(ids) > 10000:
            raise ValueError('scan_scope_too_large')
        for identity in ids:
            image = get_image(db, identity)
            if image.file_missing or not (project.root/'images'/image.filename).is_file():
                counts['missing'] += 1
            elif image.phash is None:
                counts['null'] += 1
            elif not _HASH.fullmatch(image.phash):
                counts['invalid_hash'] += 1
            else:
                records.append((identity, int(image.phash, 16)))
    if report:
        report(counts)
    # Distance buckets preserve global order with bounded memory. No pair graph
    # is materialised; cancellation is checked between small work blocks.
    for distance in range(threshold+1):
        block = []
        for index, (left, bits) in enumerate(records):
            cancel.raise_if_cancelled()
            for right, other in records[index+1:]:
                if (bits ^ other).bit_count() == distance:
                    block.append(DuplicatePair(left, right, distance))
                    if len(block) == 256:
                        yield tuple(block)
                        block.clear()
            await asyncio.sleep(0)
        if block:
            yield tuple(block)
