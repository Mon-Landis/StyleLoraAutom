from uuid import uuid4
from stylelora.llm.config import read_json, atomic_json
from stylelora.generation.profile import LARGE
from .contracts import fields, plain, Failure, check


class Presets:
    def __init__(self, rt):
        self.path = rt.product.root/'presets.json'

    def rows(self):
        return [{'preset_id': 'large', 'name': 'Large', 'sizes': plain(LARGE),
                 'builtin': True, 'version': '1'}] + read_json(self.path, [])

    def get(self, identity):
        row = next((r for r in self.rows() if r['preset_id'] == identity), None)
        if row is None:
            raise Failure(404, 'resource:not_found')
        return row

    def write(self, value, identity=None, token=None, delete=False):
        old = self.get(identity) if identity else None
        if old:
            check(token, old['version'])
            if old['builtin']:
                raise Failure(409, 'preset:builtin_locked')
        if not delete:
            fields(value, {'name', 'sizes'}, {'name', 'sizes'})
            if not isinstance(value['name'], str) or not value['name'].strip():
                raise Failure(422, 'input:invalid')
            fields(value['sizes'], {'landscape', 'portrait', 'square'}, {'landscape', 'portrait', 'square'})
            for size in value['sizes'].values():
                fields(size, {'width', 'height'}, {'width', 'height'})
                if any(type(v) is not int or v <= 0 or v % 64 for v in size.values()):
                    raise Failure(422, 'preset:invalid_size')
        rows = [r for r in self.rows() if not r['builtin'] and r['preset_id'] != identity]
        result = None if delete else dict(value, preset_id=identity or str(uuid4()),
            builtin=False, version=str(int(old['version'])+1 if old else 1))
        atomic_json(self.path, rows + ([result] if result else []))
        return result
