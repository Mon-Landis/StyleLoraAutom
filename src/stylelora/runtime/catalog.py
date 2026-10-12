"""Product resource facade, serialized through ProductStore.call."""
from dataclasses import replace
from uuid import UUID, uuid4
from stylelora.llm.types import EndpointConfig, ModelConfig, ROLES
from stylelora.llm.secrets import SecretStore
from stylelora.rules.library import RuleLibrary
from stylelora.prompts import dictionaries
from stylelora.prompts.types import FeatureDictionary, FeatureCandidate, PromptDictionary, PromptEntry
from stylelora.tagging.trigger_validation import validate_trigger
from .contracts import Failure, check, fields, plain, version


class Catalog:
    def __init__(self, rt):
        self.rt, self.store = rt, rt.product
        self.rules = RuleLibrary(self.store.root/'replacement_rules.json')

    def endpoint_view(self, endpoint):
        result = plain(endpoint)
        result.pop('changed', None)
        secrets = self.store.secret_store
        result['token_refs'] = [{'token_ref': str(UUID(ref)), 'has_secret': bool(secrets and secrets.has_secret(ref))} for ref in endpoint.token_refs]
        result['version'] = version(self.rt.secret, 'endpoint', endpoint.endpoint_id, endpoint.revision)
        return result

    def endpoint(self, identity):
        try:
            UUID(identity)
        except ValueError:
            raise Failure(422, 'input:invalid') from None
        found = next((e for e in self.store.llm.load_endpoints() if e.endpoint_id == identity), None)
        if found is None:
            raise Failure(404, 'resource:not_found')
        return found

    def save_endpoint(self, value, identity=None, expected=None):
        fields(value, {'base_url', 'protocol', 'models', 'token_refs', 'supports_json_schema'}, {'base_url', 'protocol', 'models'})
        if value['protocol'] not in ('openai_chat', 'openai_responses', 'anthropic_messages'):
            raise Failure(422, 'input:invalid')
        old = self.endpoint(identity) if identity else None
        if old:
            check(expected, self.endpoint_view(old)['version'])
        models = []
        if not isinstance(value['models'], list):
            raise Failure(422, 'input:invalid')
        for model in value['models']:
            fields(model, {'name', 'supports_images', 'wire_name', 'response_aliases'}, {'name'})
            if type(model.get('supports_images', False)) is not bool:
                raise Failure(422, 'input:invalid')
            models.append(ModelConfig(**model))
        if 'supports_json_schema' in value and type(value['supports_json_schema']) is not bool:
            raise Failure(422, 'input:invalid')
        refs = []
        for ref in value.get('token_refs', []):
            try:
                refs.append(UUID(ref).hex)
            except (ValueError, TypeError):
                raise Failure(422, 'input:invalid') from None
        result = self.store.llm.save_endpoint(EndpointConfig(identity or str(uuid4()), value['base_url'], value['protocol'],
            tuple(refs), tuple(models), supports_json_schema=value.get('supports_json_schema', False)), expected_revision=old.revision if old else 0)
        return self.endpoint_view(result)

    def secret(self, identity, value, expected, token_ref=None):
        fields(value, {'value'}, {'value'})
        if not isinstance(value['value'], str) or not value['value']:
            raise Failure(422, 'input:invalid')
        endpoint = self.endpoint(identity)
        if self.store.secret_store is None:
            raise Failure(503, 'secret:unavailable')
        ref = UUID(token_ref).hex if token_ref else uuid4().hex
        if token_ref:
            if ref not in endpoint.token_refs:
                raise Failure(404, 'resource:not_found')
            check(expected, self.endpoint_view(endpoint)['version'])
        self.store.secret_store.put_secret(ref, value['value'])
        saved = self.store.llm.save_endpoint(replace(endpoint, token_refs=tuple(dict.fromkeys((*endpoint.token_refs, ref)))), expected_revision=endpoint.revision)
        return {'token_ref': str(UUID(ref)), 'has_secret': True, 'version': self.endpoint_view(saved)['version']}

    def roles(self):
        revision, values = self.store.llm.role_map()
        return {'roles': values, 'revision': revision, 'version': version(self.rt.secret, 'roles', revision)}

    def save_roles(self, value, expected):
        fields(value, {'roles'}, {'roles'})
        roles = fields(value['roles'], set(ROLES), set(ROLES))
        current = self.roles()
        check(expected, current['version'])
        for role, binding in roles.items():
            if role in ('clean', 'clean_nsfw') and binding is not None:
                raise Failure(501, 'feature:unsupported')
            if binding is not None:
                fields(binding, {'endpoint_id', 'model'}, {'endpoint_id', 'model'})
                endpoint = self.endpoint(binding['endpoint_id'])
                model = next((m for m in endpoint.models if m.name == binding['model']), None)
                if model is None:
                    raise Failure(422, 'input:invalid')
                if role in ('vision', 'vision_nsfw', 'rating') and not model.supports_images:
                    raise Failure(422, 'llm:image_unsupported')
        self.store.llm.save_role_map(roles, expected_revision=current['revision'])
        return self.roles()

    def rule_library(self):
        value = self.rules.load()
        value.pop('operations', None)
        value['version'] = version(self.rt.secret, 'rules', value['revision'])
        return value

    def save_rules(self, value, expected, key):
        fields(value, {'rules'}, {'rules'})
        current = self.rule_library()
        check(expected, current['version'])
        self.rules.save_rule_library(dict(schema_version=1, kind='replacement_rules', revision=current['revision'],
                                     rules=value['rules'], operations={}), expected_revision=current['revision'], request_key=key)
        return self.rule_library()

    def dictionary_view(self, item):
        value = plain(item)
        value['kind'] = 'features' if isinstance(item, FeatureDictionary) else 'prompts'
        value['version'] = version(self.rt.secret, 'dictionary', item.dictionary_id, item.revision)
        return value

    @property
    def dictionary_root(self):
        # F06 takes the APPDATA parent and appends its product directory.
        return self.store.root.parent

    def dictionary(self, identity):
        return self.dictionary_view(dictionaries.load_dictionary(identity, root=self.dictionary_root))

    def save_dictionary(self, value, key, identity=None, expected=None):
        fields(value, {'kind', 'name', 'dimensions', 'entries'}, {'kind', 'name'})
        old = self.dictionary(identity) if identity else None
        if old:
            check(expected, old['version'])
        identity = identity or str(uuid4())
        if value['kind'] == 'features':
            if 'entries' in value or not isinstance(value.get('dimensions'), dict):
                raise Failure(422, 'input:invalid')
            dimensions = {}
            for name, rows in value['dimensions'].items():
                dimensions[name] = tuple(FeatureCandidate(name, **fields(row, {'tag', 'rating'}, {'tag'})) for row in rows)
            item = FeatureDictionary(identity, value['name'], 1, dimensions, old['revision'] if old else 1)
        elif value['kind'] == 'prompts':
            if 'dimensions' in value or not isinstance(value.get('entries'), list):
                raise Failure(422, 'input:invalid')
            from stylelora.prompts.validation import construct
            item = PromptDictionary(identity, value['name'], 1, tuple(construct(PromptEntry, row) for row in value['entries']), old['revision'] if old else 1)
        else:
            raise Failure(422, 'input:invalid')
        dictionaries.save_dictionary(item, expected_revision=old['revision'] if old else 0, request_key=key, root=self.dictionary_root)
        return self.dictionary(identity)

    def page(self, rows, params, resource, revision):
        fields(params, {'limit', 'cursor', 'q', 'kind', 'dimension'})
        try:
            limit = int(params.get('limit', 50))
        except ValueError:
            raise Failure(422, 'input:invalid') from None
        if not 1 <= limit <= 1000:
            raise Failure(422, 'input:invalid')
        query = {k:v for k,v in params.items() if k not in ('limit', 'cursor')}
        binding = [None, resource, query]
        token = self.rt.images.decode(params['cursor'], binding) if params.get('cursor') else {}
        if token and token['revision'] != revision:
            raise Failure(409, 'page:stale')
        start = token.get('index', 0)
        selected, more = rows[start:start+limit], start+limit < len(rows)
        cursor = self.rt.images.cursor(dict(binding=binding, revision=revision, index=start+limit)) if more else None
        return dict(items=selected, next_cursor=cursor, has_more=more, limit=limit, upper_id=None)
