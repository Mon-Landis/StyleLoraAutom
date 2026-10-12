import hashlib
import json
from dataclasses import dataclass
from PIL import Image
from stylelora.backends.types import Immutable, BackendError
from stylelora.tagging.tokenizer import tokenize_prompt
from stylelora.tagging.catalog import LocalCatalog

QUALITY_TAGS = {'very aesthetic', 'masterpiece', 'no text'}


@dataclass(frozen=True)
class ArtifactInspection(Immutable):
    width: int
    height: int
    file_size: int
    sha256: str
    phash: str | None
    png_meta: dict | None
    validation: str
    reason_codes: tuple[str, ...]


def prompt_tags(text, catalog):
    tokens = tokenize_prompt(text, catalog=catalog)
    if any(t.syntax != 'plain' for t in tokens if t.normalized):
        return None
    return {t.normalized for t in tokens if t.normalized}


def inspect_png(path, *, style_prompt, sent_prompt, metadata_stripped=None, expected_size=None, catalog=None):
    try:
        with Image.open(path) as image:
            if image.format != 'PNG':
                raise ValueError()
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            comment = image.info.get('Comment')
        if expected_size is not None and (width, height) != tuple(expected_size):
            raise BackendError('artifact:size_mismatch', 'artifact')
        with path.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        size = path.stat().st_size
    except BackendError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise BackendError('artifact:invalid_png', 'artifact') from None
    meta, validation, reasons = None, 'unverifiable', []
    if comment is None:
        reasons.append('meta:stripped' if metadata_stripped else 'meta:missing')
    else:
        try:
            parsed = json.loads(comment)
            if not isinstance(parsed, dict):
                raise ValueError()
            meta = {'schema_version': 1, 'comment': parsed}
            prompt = parsed.get('prompt')
            if not isinstance(prompt, str):
                raise ValueError()
        except (ValueError, TypeError):
            reasons.append('meta:invalid_comment')
        else:
            catalog = catalog or LocalCatalog()
            style, sent, actual = (prompt_tags(s, catalog) for s in (style_prompt, sent_prompt, prompt))
            if any(v is None for v in (style, sent, actual)):
                reasons.append('meta:unsupported_syntax')
            else:
                if not style <= actual:
                    reasons.append('meta:style_tag_missing')
                if (actual - sent) & QUALITY_TAGS:
                    reasons.append('meta:unexpected_quality_tags')
                validation = 'mismatch' if reasons else 'pass'
    return ArtifactInspection(width, height, size, sha, None, meta, validation, tuple(reasons))
