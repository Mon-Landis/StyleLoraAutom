"""Versioned UTF-8 prompt exchange with physical-line diagnostics."""
import json
from dataclasses import replace
from uuid import uuid4
from .groups import Group, content
from .group_store import JSON_LIMIT, ENTRY_LIMIT
from .types import PromptError, Issue, plain
from .validation import require, strict_loads

HEADER = '# stylelora-prompts-txt v2'
LEGACY_HEADER = '# stylelora-prompts-txt v1'


def encode_group_txt(metadata, entries):
    def normalize(value):
        if isinstance(value, str):
            return value.replace('\r\n', '\n').replace('\r', '\n')
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value
    metadata = metadata | dict(format_version=2, prompt_format='nai', normalization_version='nai-storage-v1')
    rows = [{'group_metadata': metadata}, *(content(e) for e in entries)]
    return (HEADER+'\n'+'\n'.join(json.dumps(normalize(row), ensure_ascii=False,
                                           allow_nan=False) for row in rows)+'\n').encode('utf-8')


def decode_group_txt(data):
    """Return metadata/prompts plus issues, warnings and source row_numbers."""
    require(len(data) <= JSON_LIMIT, 'import:limit')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise PromptError('import:encoding') from None
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    versioned = bool(lines and lines[0].startswith('# stylelora-prompts-txt'))
    metadata, issues, warnings, rows, numbers = {}, [], [], [], []
    if versioned:
        if lines[0] not in (HEADER, LEGACY_HEADER):
            raise PromptError('schema:invalid', issues=(Issue('schema:invalid', row=1),))
        try:
            require(len(lines) >= 2)
            header = strict_loads(lines[1])
            require(isinstance(header, dict) and set(header) == {'group_metadata'})
            metadata = header['group_metadata']
            require(isinstance(metadata, dict))
            require(set(metadata) <= {'format_version', 'group_id', 'name', 'nsfw', 'scope', 'category', 'auto_complete', 'prompt_format', 'normalization_version'})
            version = 2 if lines[0] == HEADER else 1
            require(type(metadata.get('format_version', 1)) is int)
            require(metadata.get('format_version', version) == version, 'newer_version')
            metadata = metadata | {'format_version': version}
            if version == 2:
                require(metadata.get('prompt_format') == 'nai')
                require(metadata.get('normalization_version') == 'nai-storage-v1')
            Group(metadata.get('group_id', str(uuid4())), metadata.get('name', '导入提示词组'),
                  metadata.get('scope', 'global'), metadata.get('nsfw', False),
                  category=metadata.get('category', 'full_content'))
        except PromptError as exc:
            raise PromptError(exc.code, issues=tuple(replace(i, row=2) for i in
                              exc.issues or (Issue(exc.code),))) from None
        source = list(enumerate(lines[2:], 3))
    else:
        warnings.append(plain(Issue('import:legacy_txt', message='旧 TXT 无法恢复组元数据。', severity='warning')))
        source = [(n, line) for n, line in enumerate(lines, 1) if line.strip()]
    require(len(source) <= ENTRY_LIMIT, 'import:limit')
    for number, line in source:
        try:
            if versioned:
                row = strict_loads(line)
            else:
                row = dict(nl=line.replace('\\n', '\n'), variants={})
            rows.append(row); numbers.append(number)
        except PromptError as exc:
            issues.extend(plain(replace(i, row=number)) for i in exc.issues or (Issue(exc.code),))
    return dict(group_metadata=metadata, prompts=rows, issues=issues,
                warnings=warnings, row_numbers=numbers)
