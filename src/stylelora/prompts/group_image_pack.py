"""Independent NAI PNG package codec; legacy WebP packages are unchanged."""
import hashlib
import io
import json
import stat
import zipfile

from PIL import Image, ImageDraw, PngImagePlugin

from .group_store import (JSON_LIMIT, IMAGE_LIMIT, PIXEL_LIMIT, ZIP_LIMIT,
                          EXPANDED_LIMIT, ENTRY_LIMIT)
from .groups import content
from .nai_metadata import extract_positive
from .types import PromptError
from .validation import require, identifier


def _pixels(data):
    require(len(data) <= IMAGE_LIMIT, 'image:too_large')
    try:
        with Image.open(io.BytesIO(data)) as source:
            require(source.width * source.height <= PIXEL_LIMIT, 'image:too_large')
            source.load()
            # Composite onto an opaque background to destroy alpha-channel stego.
            rgba = source.convert('RGBA')
            clean = Image.new('RGB', source.size, (240, 240, 240))
            clean.paste(rgba, mask=rgba.getchannel('A'))
            return clean
    except PromptError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        raise PromptError('image:invalid') from None


def encode_png(nl: str, preview: bytes | None = None) -> bytes:
    """Encode clean pixels and an NAI Comment; None produces a marked placeholder."""
    require(isinstance(nl, str))
    if preview is None:
        image = Image.new('RGB', (256, 256), (224, 224, 224))
        draw = ImageDraw.Draw(image)
        for y in range(0, 256, 32):
            for x in range(0, 256, 32):
                if (x + y) // 32 % 2:
                    draw.rectangle((x, y, x+31, y+31), fill=(200, 200, 200))
        draw.rectangle((40, 112, 216, 144), fill=(240, 240, 240))
        draw.text((68, 122), 'NO REFERENCE', fill=(64, 64, 64))
    else:
        image = _pixels(preview)
    info = PngImagePlugin.PngInfo()
    comment = json.dumps(dict(prompt=nl, v4_prompt=dict(caption=dict(
        base_caption=nl, char_captions=[]))), ensure_ascii=True, separators=(',', ':'))
    info.add_text('Comment', comment, zip=False)
    stream = io.BytesIO()
    image.save(stream, format='PNG', pnginfo=info, compress_level=6, optimize=False)
    result = stream.getvalue()
    require(len(result) <= IMAGE_LIMIT, 'image:too_large')
    return result


def write_image_pack(store, ref, group, metadata: dict) -> bytes:
    """Write a leased Group snapshot. Caller must hold store.lease(ref)."""
    require(bool(group.entries), 'import:empty')
    require(len(group.entries) <= ENTRY_LIMIT, 'import:limit')
    files, manifest, rows = {}, {}, []
    total = 0
    for entry in sorted(group.entries, key=lambda e: e.entry_id):
        preview = None
        if entry.preview:
            try:
                preview, _ = store.image(ref, entry)
            except PromptError as exc:
                if exc.code == 'image:not_found':
                    raise PromptError('image:invalid') from None
                raise
            require(hashlib.sha256(preview).hexdigest() == entry.preview.sha256, 'image:invalid')
        data = encode_png(entry.nl, preview)
        name = 'images/' + entry.entry_id + '.png'
        files[name] = data
        total += len(data)
        require(total <= EXPANDED_LIMIT and total <= ZIP_LIMIT, 'import:limit')
        manifest[entry.entry_id] = dict(file=name, sha256=hashlib.sha256(data).hexdigest(),
                                       placeholder=entry.preview is None)
        rows.append(dict(content(entry), entry_id=entry.entry_id))
    files['group.json'] = json.dumps(dict(group_metadata=metadata, prompts=rows,
        preview_manifest=manifest), ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')
    require(len(files['group.json']) <= JSON_LIMIT, 'import:limit')
    require(total + len(files['group.json']) <= EXPANDED_LIMIT, 'import:limit')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    require(stream.tell() <= ZIP_LIMIT, 'import:limit')
    return stream.getvalue()


def read_image_pack(data: bytes) -> dict[str, bytes]:
    """Read bounded ZIP members without extracting to disk."""
    from .group_exchange import safe_name
    require(isinstance(data, (bytes, bytearray)), 'import:unsafe_archive')
    require(len(data) <= ZIP_LIMIT, 'import:limit')
    files, names, total = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(len(archive.infolist()) <= ENTRY_LIMIT + 1, 'import:limit')
            for info in archive.infolist():
                name = safe_name(info.orig_filename)
                mode = info.external_attr >> 16
                require(name == info.filename == info.orig_filename and not info.is_dir() and
                        name.casefold() not in names and
                        stat.S_IFMT(mode) in (0, stat.S_IFREG) and
                        not info.external_attr & 0x400 and not info.flag_bits & 1,
                        'import:unsafe_archive')
                names.add(name.casefold())
                limit = JSON_LIMIT if name == 'group.json' else IMAGE_LIMIT
                require(info.file_size <= limit and
                        info.file_size <= max(1, info.compress_size) * 100 and
                        total + info.file_size <= EXPANDED_LIMIT, 'import:limit')
                with archive.open(info) as member:
                    body = member.read(min(limit, EXPANDED_LIMIT-total) + 1)
                require(len(body) <= limit and total + len(body) <= EXPANDED_LIMIT, 'import:limit')
                require(len(body) == info.file_size, 'import:unsafe_archive')
                total += len(body)
                files[name] = body
    except PromptError:
        raise
    except (OSError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise PromptError('import:unsafe_archive') from None
    require('group.json' in files, 'schema:invalid')
    return files


def validate_image_pack(files: dict[str, bytes], raw: dict) -> None:
    """Validate exhaustive manifest, PNG integrity and both positive prompt fields."""
    require(raw.get('group_metadata', {}).get('format_version') == 2)
    rows, manifest = raw['prompts'], raw.get('preview_manifest')
    require(isinstance(rows, list) and isinstance(manifest, dict))
    require(bool(rows), 'import:empty')
    require(len(rows) <= ENTRY_LIMIT, 'import:limit')
    ids, declared = set(), {'group.json'}
    for row in rows:
        require(isinstance(row, dict))
        identity = row.get('entry_id')
        identifier(identity)
        require(identity not in ids, 'entry:id_conflict')
        ids.add(identity)
        spec = manifest.get(identity)
        require(isinstance(spec, dict) and set(spec) == {'file', 'sha256', 'placeholder'})
        require(type(spec['placeholder']) is bool)
        name = 'images/' + identity + '.png'
        require(spec['file'] == name, 'import:unsafe_archive')
        require(name in files, 'image:not_found')
        data = files[name]
        require(hashlib.sha256(data).hexdigest() == spec['sha256'], 'image:invalid')
        require(data.startswith(b'\x89PNG\r\n\x1a\n'), 'image:invalid')
        extracted = extract_positive(data)
        require(not any(i.severity == 'error' for i in extracted.issues), 'image:invalid')
        _pixels(data)
        try:
            with Image.open(io.BytesIO(data)) as image:
                comment = json.loads(image.info.get('Comment', ''))
            caption = comment['v4_prompt']['caption']
            require(comment['prompt'] == caption['base_caption'] == row.get('nl') == extracted.text
                    and caption['char_captions'] == [], 'import:metadata_mismatch')
        except (ValueError, TypeError, KeyError):
            raise PromptError('import:metadata_mismatch') from None
        declared.add(name)
    require(set(manifest) == ids, 'schema:invalid')
    require(set(files) == declared, 'import:unsafe_archive')
