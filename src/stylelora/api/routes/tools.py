"""Project-independent prompt tools."""
import asyncio
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


@router.post('/nai-metadata/run')
async def nai_metadata_run(request: Request):
    """Pick images or a folder natively, then write same-name prompt .txt files.

    Synchronous by design: the request resolves only after the native dialog
    closes and processing ends. Paths never come from the client; canceling the
    dialog returns 200 with ``canceled=true``. Per-file problems are reported in
    ``files`` and never abort the batch.
    """
    from stylelora.prompts import nai_metadata_export
    try:
        value = await request.json()
    except ValueError:
        raise Failure(422, 'input:invalid') from None
    if (not isinstance(value, dict) or set(value) != {'source', 'drop_mode', 'to_anima'}
            or value['source'] not in ('files', 'folder')
            or not isinstance(value['drop_mode'], str)
            or value['drop_mode'] not in nai_metadata_export.DROP_MODES
            or type(value['to_anima']) is not bool):
        raise Failure(422, 'input:invalid')
    rt = request.app.state.runtime
    picker = getattr(rt, 'image_picker', None)
    if picker is None:
        raise Failure(501, 'picker:unavailable', details={'reason': 'native_picker_not_configured'})

    def work():
        # One native dialog and its batch at a time; the lock is never awaited.
        with nai_metadata_export._exclusive() as busy:
            if busy is not None:
                raise Failure(409, 'picker:busy')
            try:
                picked = picker(value['source'])
            except Failure:
                raise
            except Exception:
                raise Failure(501, 'picker:unavailable',
                              details={'reason': 'native_picker_failed'}) from None
            return nai_metadata_export.run(value['source'], value['drop_mode'],
                                           value['to_anima'], picked)
    # Dialog, extraction and writes all stay off the event loop.
    return await asyncio.to_thread(work)
