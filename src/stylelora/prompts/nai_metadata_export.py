"""Write NAI positive prompts as same-name .txt files beside picked images.

Paths only come from the native picker; this module never accepts a client
path. Text is written UTF-8 without BOM and with LF line endings via a temp
file in the target directory followed by os.replace.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import threading

from stylelora.runtime.contracts import Failure, plain
from .nai_metadata import MetadataLimits, extract_positive_parts
from .nai_convert import ConversionPolicy, convert_nai_to_anima
from .types import Issue, PromptError

DROP_MODES = ('none', 'head', 'global')
MAX_IMAGES = 1000
POLICY = ConversionPolicy()

_picker_lock = threading.Lock()
_BUSY = object()


@contextmanager
def _exclusive():
    """Hold the single native-picker slot; acquisition never blocks."""
    if not _picker_lock.acquire(blocking=False):
        yield _BUSY
        return
    try:
        yield None
    finally:
        _picker_lock.release()


def _drop_head(text: str) -> str:
    """Drop everything through the first complete blank line and its run.

    A blank line is empty or whitespace-only. Without one the text is kept
    as-is (then the caller's outer trim still applies); outer blank lines and
    whitespace are trimmed in every case.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            start = index
            while start < len(lines) and not lines[start].strip():
                start += 1
            lines = lines[start:]
            break
    return '\n'.join(lines).strip()


def _global_text(chars) -> str:
    return '\n\n'.join(chars)


def _write(path: Path, text: str) -> bool:
    """Atomically write text; returns True when an old file was replaced."""
    overwritten = path.exists()
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='strict', newline='\n') as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return overwritten


def _encoded(text: str) -> str:
    """Reject text that cannot be stored as UTF-8 (e.g. lone surrogates)."""
    text.encode('utf-8')
    return text


def _issue(code, message):
    return plain(Issue(code, message, severity='error'))


def _failure(entry, issues, status, reason, code, message):
    issues.append(_issue(code, message))
    entry.update(status=status, reason=reason, txt=None)
    return entry


def process_image(path: Path, drop_mode: str, to_anima: bool) -> dict:
    """Process one image; per-file failures become a status, never an exception."""
    issues: list[Issue] = []
    entry = dict(name=path.name, path=str(path), txt=None, status='skipped', source=None,
                 overwritten=False, chars=0, issues=issues, warnings=[])
    try:
        if path.stat().st_size > MetadataLimits.file_bytes:
            return _failure(entry, issues, 'failed', 'metadata:limit', 'metadata:limit',
                            'Image exceeds metadata file limit.')
        data = path.read_bytes()
    except OSError:
        return _failure(entry, issues, 'failed', 'storage:io_error', 'storage:io_error',
                        'Image could not be read.')

    try:
        extracted = extract_positive_parts(data)
        result = extracted.extraction
        entry['source'] = result.source
        issues.extend(plain(issue) for issue in result.issues)
        if result.source is None:
            if any(i.severity == 'error' for i in result.issues):
                entry.update(status='failed', reason='metadata:invalid')
            else:
                entry['reason'] = 'metadata:empty'
            return entry

        if drop_mode == 'head':
            text = _drop_head(result.text)
        elif drop_mode == 'global':
            text = _global_text(extracted.chars)
        else:
            text = result.text
        text = _encoded(text.strip())
        if not text:
            entry['reason'] = 'metadata:empty'
            return entry

        if to_anima:
            converted = convert_nai_to_anima(text, policy=POLICY)
            entry['warnings'] = [warning.code.value for warning in converted.warnings]
            text = converted.text.strip()
            if not text:
                # A refused conversion reports its own first warning (e.g. W_INPUT_LIMIT).
                reason = entry['warnings'][0] if entry['warnings'] else 'metadata:empty'
                entry['reason'] = reason
                return entry
    except UnicodeError:
        return _failure(entry, issues, 'failed', 'metadata:invalid', 'metadata:invalid',
                        'Prompt text is not encodable as UTF-8.')
    except PromptError as exc:
        return _failure(entry, issues, 'failed', 'metadata:invalid', exc.code,
                        'Prompt metadata is invalid.')
    except (OSError, ValueError):
        return _failure(entry, issues, 'failed', 'storage:io_error', 'storage:io_error',
                        'Image metadata could not be processed.')

    target = path.with_suffix('.txt')
    try:
        overwritten = _write(target, text)
    except (OSError, UnicodeError):
        return _failure(entry, issues, 'failed', 'storage:io_error', 'storage:io_error',
                        'Prompt text could not be written.')
    entry.update(status='written', txt=str(target), overwritten=overwritten,
                 chars=len(text))
    return entry


def _regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and not (
            hasattr(path, 'is_junction') and path.is_junction())
    except OSError:
        return False


def _is_png(path: Path) -> bool:
    return path.suffix.lower() == '.png'


def select_images(source: str, picked) -> tuple[Path | None, list[Path]]:
    """Normalize picker output to (root, images) in picker/name order."""
    if source == 'files':
        images = [p.absolute() for p in (Path(item) for item in picked) if _regular_file(p)]
        try:
            root = Path(os.path.commonpath([str(p.parent) for p in images])) if images else None
        except ValueError:
            # Different drives have no common path; root simply stays unknown.
            root = None
        return root, images
    folder = Path(picked).absolute()
    try:
        entries = list(folder.iterdir())
    except OSError:
        raise Failure(422, 'path:invalid') from None
    images = sorted((p for p in entries if _is_png(p) and _regular_file(p)), key=lambda p: p.name)
    return folder, images


def _collision_key(path: Path) -> str:
    value = os.path.normcase(str(path))
    return value.lower() if sys.platform == 'win32' else value


def _collisions(images, targets) -> set[int]:
    """Indexes of images whose output collides with another output or an input."""
    owners = {}
    for group in (images, targets):
        for index, path in enumerate(group):
            owners.setdefault(_collision_key(path), set()).add(index)
    return {index for indexes in owners.values() if len(indexes) > 1 for index in indexes}


def _run_images(images, drop_mode, to_anima) -> list[dict]:
    targets = [image.with_suffix('.txt') for image in images]
    clashing = _collisions(images, targets)
    files = []
    for index, image in enumerate(images):
        if not _is_png(image):
            code, message = 'image:unsupported', 'Only PNG images are processed.'
        elif index in clashing:
            code, message = 'output:conflict', 'Output text path collides with another file.'
        else:
            files.append(process_image(image, drop_mode, to_anima))
            continue
        files.append(dict(name=image.name, path=str(image), txt=None, status='failed',
                          source=None, overwritten=False, chars=0, warnings=[], reason=code,
                          issues=[_issue(code, message)]))
    return files


def run(source: str, drop_mode: str, to_anima: bool, picked) -> dict:
    """Process a picker result. ``picked`` is None when the user canceled."""
    if picked is None:
        return dict(canceled=True, root=None,
                    summary=dict(total=0, written=0, skipped=0, failed=0), files=[])
    root, images = select_images(source, picked)
    if len(images) > MAX_IMAGES:
        raise Failure(413, 'import:limit')
    files = _run_images(images, drop_mode, to_anima)
    summary = dict(total=len(files),
                   written=sum(f['status'] == 'written' for f in files),
                   skipped=sum(f['status'] == 'skipped' for f in files),
                   failed=sum(f['status'] == 'failed' for f in files))
    return dict(canceled=False, root=str(root) if root is not None else None,
                summary=summary, files=files)
