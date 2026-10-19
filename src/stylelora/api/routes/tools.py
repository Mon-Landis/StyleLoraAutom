"""Project-independent, pure prompt conversion tools."""
from fastapi import APIRouter, Request
from stylelora.runtime.contracts import Failure
from stylelora.prompts.nai_convert import ConversionPolicy, convert_nai_to_anima
from stylelora.prompts.anima_convert import convert_anima_to_nai

router = APIRouter(prefix='/api/tools')
POLICY = ConversionPolicy(max_chars=200000)


@router.post('/prompt-convert')
async def prompt_convert(request: Request):
    try:
        value = await request.json()
    except ValueError:
        raise Failure(422, 'input:invalid') from None
    if (not isinstance(value, dict) or set(value) != {'direction', 'text'}
            or not isinstance(value['direction'], str)
            or value['direction'] not in ('nai_to_anima', 'anima_to_nai')
            or not isinstance(value['text'], str) or len(value['text']) > POLICY.max_chars):
        raise Failure(422, 'input:invalid')
    convert = convert_nai_to_anima if value['direction'] == 'nai_to_anima' else convert_anima_to_nai
    result = convert(value['text'], policy=POLICY)
    return {'text': result.text, 'warnings': [
        {'code': w.code.value, 'start': w.span.start, 'end': w.span.end} for w in result.warnings]}
