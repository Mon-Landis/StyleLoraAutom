"""JSON Schema 2020-12 subset used by the complete local scheme schema.

The validator implements only keywords present below; no external refs/network.
"""
from dataclasses import asdict
import math
from pathlib import PurePosixPath, PureWindowsPath
from stylelora.caption.types import CaptionConfig
from stylelora.project_db.types import ExportSchemeInput, ImportSchemeInput
from stylelora.project_db import submit_write
from stylelora.project_db.repositories import schemes
from stylelora.project_db.errors import ValidationError, UnsupportedScheme
from stylelora.tagging.jsonio import loads, canonical_json
from .types import ImportParams, OutputConfig


def obj(properties):
    return {'type':'object','additionalProperties':False,'required':list(properties),'properties':properties}


def enum(*values):
    return {'enum':list(values)}


def array(items, **extra):
    return {'type':'array','items':items,'uniqueItems':True,**extra}


LAYERS = ('original','wd','llm','manual','rule')
DEFS = {
 'reader':obj({'schema_version':{'const':1},'encoding':enum('utf-8-sig','gbk','auto'),'recursive':{'type':'boolean'},
               'layout':{'const':'first_line_blank_nl'},'tokenizer_version':{'const':'comma-v1'},'on_unverifiable':{'const':'preview_error'}}),
 'caption':obj({'schema_version':{'const':1},'tag_layers':array(enum(*LAYERS)),
                'layer_priority':array(enum(*LAYERS),minItems=5,maxItems=5),
                'wd_min_confidence':{'type':'number','minimum':0,'maximum':1},'wd_null_confidence':enum('include','exclude'),
                'include_nl':{'type':'boolean'},'nl_priority':{'const':['manual','llm','original']},
                'tag_sort':enum('layer_first','prompt_pyramid'),'blocklist_mode':enum('warn','remove'),
                'separator':{'const':', '},'trigger_position':{'const':'first'}}),
 'filter':obj({'schema_version':{'const':1},'review_statuses':array(enum('keep','unreviewed')),
               'source_ids':{'type':['array','null'],'uniqueItems':True,'items':{'type':'integer','minimum':1}},
               'include_missing':{'const':False}}),
 'output':obj({'schema_version':{'const':1},'image_mode':enum('copy','hardlink'),
               'filename':enum('{image_id}{ext}','{stem}-{image_id}{ext}'),'caption_encoding':{'const':'utf-8'},
               'include_json':{'type':'boolean'},'require_nonempty_tags':{'type':'boolean'},
               'subdirectory':{'type':'string'},'overwrite':enum('error','skip')}),
 'pipeline':{'type':'array','items':obj({'processor':enum('metadata_strip','local_upscale'),'params':{'type':'object'}})},
}
DEFS['import'] = obj({'name':{'type':'string','minLength':1},'reader':{'const':'txt_sidecar'},'params_json':{'$ref':'#/$defs/reader'},'is_default':{'type':'boolean'}})
DEFS['export'] = obj({'name':{'type':'string','minLength':1},'exporter':{'const':'image_txt'},
                      'filter_json':{'$ref':'#/$defs/filter'},'caption_config_json':{'$ref':'#/$defs/caption'},
                      'output_config_json':{'$ref':'#/$defs/output'},'pipeline_json':{'$ref':'#/$defs/pipeline'}})
SCHEME_SCHEMA = {'$schema':'https://json-schema.org/draft/2020-12/schema',
                 'oneOf':[{'$ref':'#/$defs/import'},{'$ref':'#/$defs/export'}],'$defs':DEFS}


def validate_schema(value, schema, *, defs=DEFS):
    def fail():
        raise ValidationError('schema:invalid')
    if '$ref' in schema:
        return validate_schema(value,defs[schema['$ref'].rsplit('/',1)[1]],defs=defs)
    if 'oneOf' in schema:
        count = 0
        for branch in schema['oneOf']:
            try:
                validate_schema(value,branch,defs=defs)
                count += 1
            except ValidationError:
                pass
        if count != 1:
            fail()
    if 'const' in schema and canonical_json(value) != canonical_json(schema['const']):
        fail()
    if 'enum' in schema and not any(canonical_json(value)==canonical_json(v) for v in schema['enum']):
        fail()
    types = schema.get('type',[])
    types = [types] if isinstance(types,str) else types
    tests = {'object':type(value) is dict,'array':type(value) is list,'null':value is None,
             'boolean':type(value) is bool,'integer':type(value) is int,
             'number':type(value) in (int,float) and math.isfinite(value),'string':type(value) is str}
    if types and not any(tests[t] for t in types):
        fail()
    if isinstance(value,dict):
        properties = schema.get('properties',{})
        if not set(schema.get('required',[])) <= value.keys():
            fail()
        if schema.get('additionalProperties') is False and value.keys()-properties.keys():
            fail()
        for key, child in value.items():
            if key in properties:
                validate_schema(child,properties[key],defs=defs)
    if isinstance(value,list):
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',len(value)):
            fail()
        if schema.get('uniqueItems') and len({canonical_json(v) for v in value}) != len(value):
            fail()
        if 'items' in schema:
            for child in value:
                validate_schema(child,schema['items'],defs=defs)
    if isinstance(value,str) and len(value)<schema.get('minLength',0):
        fail()
    if type(value) in (int,float) and (not math.isfinite(value) or not schema.get('minimum',value)<=value<=schema.get('maximum',value)):
        fail()


def _json_dto(value):
    return loads(canonical_json(asdict(value)))


def caption_config(value=None):
    data = {**_json_dto(CaptionConfig()),**(value or {})}
    validate_schema(data,DEFS['caption'])
    return CaptionConfig(**{k:tuple(v) if isinstance(v,list) else v for k,v in data.items()})


def import_params(value=None):
    data = {**asdict(ImportParams()),**(value or {})}
    validate_schema(data,DEFS['reader'])
    return ImportParams(**data)


def output_config(value=None):
    data = {**asdict(OutputConfig()),**(value or {})}
    validate_schema(data,DEFS['output'])
    if data['image_mode'] != 'copy':
        raise UnsupportedScheme('image_mode:unsupported')
    path = data['subdirectory']
    if path and ('\\' in path or ':' in path or PureWindowsPath(path).drive or path.startswith('/')
                 or any(p in ('','.','..') or p.endswith((' ','.')) for p in path.split('/'))):
        raise ValidationError('path:unsafe')
    return OutputConfig(**data)


def validate_scheme(value):
    if isinstance(value,(bytes,str)):
        value = loads(value)
    if not isinstance(value,dict):
        raise ValidationError('schema:invalid')
    canonical_json(value)
    value = dict(value)
    if 'reader' in value or 'params_json' in value or 'is_default' in value:
        value.setdefault('reader','txt_sidecar')
        value.setdefault('is_default',False)
        value['params_json'] = asdict(import_params(value.get('params_json')))
        validate_schema(value,SCHEME_SCHEMA)
        return ImportSchemeInput(**value)
    value.setdefault('exporter','image_txt')
    value['caption_config_json'] = _json_dto(caption_config(value.get('caption_config_json')))
    value['output_config_json'] = asdict(output_config(value.get('output_config_json')))
    value['filter_json'] = {**{'schema_version':1,'review_statuses':['keep'],'source_ids':None,'include_missing':False},**value.get('filter_json',{})}
    value.setdefault('pipeline_json',[])
    validate_schema(value,SCHEME_SCHEMA)
    return ExportSchemeInput(**{**value,'pipeline_json':tuple(value['pipeline_json'])})


def save_scheme(project,value,*,scheme_id=None,expected_updated_at=None):
    value = validate_scheme(value)
    kind = 'import' if isinstance(value,ImportSchemeInput) else 'export'
    if scheme_id is None:
        return submit_write(project,lambda db:getattr(schemes,'create_'+kind+'_scheme')(db,value)).result()
    if expected_updated_at is None:
        raise ValidationError('scheme:expected_updated_at_required')
    return submit_write(project,lambda db:getattr(schemes,'update_'+kind+'_scheme')(
        db,scheme_id,value,expected_updated_at=expected_updated_at)).result()
