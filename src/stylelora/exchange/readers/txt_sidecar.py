import hashlib
from pathlib import Path
from dataclasses import asdict
from stylelora.project_db.types import OriginalInput
from stylelora.project_db.errors import ValidationError
from stylelora.tagging.types import Issue
from stylelora.tagging.parser import parse_original_prompt
from ..types import ReadResult, DecodedText
from ..schemes import import_params


def decode_sidecar(data,encoding='utf-8-sig'):
    if len(data)>1024*1024:
        raise ValidationError('sidecar:too_large')
    if encoding not in ('utf-8-sig','gbk','auto'):
        raise ValidationError('encoding:invalid')
    try:
        if data.startswith(b'\xef\xbb\xbf'):
            return DecodedText(data.decode('utf-8-sig'),'utf-8-sig',False)
        if encoding == 'gbk':
            return DecodedText(data.decode('gbk'),'gbk',False)
        try:
            return DecodedText(data.decode('utf-8'),'utf-8',False)
        except UnicodeDecodeError:
            if encoding != 'auto':
                raise
            return DecodedText(data.decode('gbk'),'gbk',True)
    except UnicodeDecodeError as exc:
        raise ValidationError('encoding:invalid') from exc


def sidecar_path(image):
    matches = [p for p in image.parent.iterdir() if p.name.casefold() == (image.stem+'.txt').casefold()]
    if len(matches)>1:
        raise ValidationError('sidecar:ambiguous')
    if matches and (matches[0].is_symlink() or not matches[0].is_file()):
        raise ValidationError('path:unsafe')
    return matches[0] if matches else None


class TxtSidecarReader:
    name = 'txt_sidecar'

    def read(self,image,params,catalog):
        params = import_params(asdict(params))
        path = sidecar_path(Path(image))
        if path is None:
            return ReadResult((),None,(Issue('sidecar:missing'),),None)
        data = path.read_bytes()
        decoded = decode_sidecar(data,params.encoding)
        text = decoded.text.replace('\r\n','\n').replace('\r','\n')
        lines = text.split('\n')
        if len(lines)>1 and lines[1].strip():
            raise ValidationError('sidecar:invalid_layout')
        parsed = parse_original_prompt(lines[0],catalog=catalog)
        if parsed.unverifiable:
            raise ValidationError('tag:unverifiable')
        explicit = '\n'.join(lines[2:]).strip() if len(lines)>1 else ''
        nl = '\n\n'.join(v for v in (parsed.natural_language,explicit) if v)
        sha = hashlib.sha256(data).hexdigest()
        evidence = {'schema_version':1,'tokenizer_version':parsed.tokenizer_version,'catalog_revision':catalog.revision,
                    'raw':lines[0],'encoding':decoded.encoding,'sidecar_sha256':sha}
        originals = [OriginalInput('tags',parsed.tags,params=evidence)]
        if nl:
            originals.append(OriginalInput('nl',nl,params=evidence))
        warnings = []
        if decoded.fallback:
            warnings.append(Issue('encoding:gbk_fallback'))
        if parsed.natural_language:
            warnings.append(Issue('sidecar:unknown_to_nl'))
        return ReadResult(tuple(originals),decoded.encoding,tuple(warnings),sha)
