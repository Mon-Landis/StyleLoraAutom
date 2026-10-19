import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import { ApiError } from '../src/api/errors';
import {
  addEntry, confirmImport, enqueueCompletion, exportGroup, fetchImportPreviewBlob, importPreviewPath,
  patchEntry, previewImportFiles,
} from '../src/api/promptGroups';
import type { GroupRef } from '../src/api/promptGroupTypes';
import { downloadBlob } from '../src/lib/download';
import { entryBlock, normalizeView } from '../src/lib/promptGroupList';
import { describeErrorCode, importFileIssues } from '../src/lib/promptErrors';
import { summarizeCompletion } from '../src/lib/completionWatch';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };

interface Call { method: string; url: string; body: unknown; headers: Record<string, string> }
function installFetch(handler: (c: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const c: Call = {
      method: init?.method ?? 'GET', url: String(url), body: init?.body,
      headers: (init?.headers ?? {}) as Record<string, string>,
    };
    calls.push(c);
    return handler(c);
  }));
  return calls;
}
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers });

beforeEach(() => setToken('tok'));
afterEach(() => vi.unstubAllGlobals());

describe('T13 导入预览上传', () => {
  const png = (name: string) => new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

  it('nai_images 逐个 append 重复 files 字段，同名文件不去重，options 为 JSON 字符串', async () => {
    const calls = installFetch(() => json({ preview_id: 'p1', digest: 'd', files: [] }));
    const opts = { format: 'nai_images', level: 'entries', ref, source_format: 'nai' } as const;
    await previewImportFiles([png('a.png'), png('a.png'), png('b.png')], opts, 'key-1');
    const c = calls[0];
    expect(c.method).toBe('POST');
    expect(c.url).toBe('/api/prompt-imports/preview');
    expect(c.headers['Idempotency-Key']).toBe('key-1');
    expect(c.headers['Content-Type']).toBeUndefined(); // 由浏览器带 boundary
    expect(c.headers.Authorization).toBe('Bearer tok');
    const fd = c.body as FormData;
    expect(fd.getAll('files').map((f) => (f as File).name)).toEqual(['a.png', 'a.png', 'b.png']);
    expect(fd.get('file')).toBeNull();
    expect(JSON.parse(String(fd.get('options')))).toEqual(opts);
  });

  it('单文件格式用 file 字段，只取第一个', async () => {
    const calls = installFetch(() => json({ preview_id: 'p1', digest: 'd' }));
    const zip = new File(['x'], 'g.zip');
    await previewImportFiles([zip, png('x.png')], { format: 'nai_image_zip', level: 'group' });
    const fd = calls[0].body as FormData;
    expect((fd.get('file') as File).name).toBe('g.zip');
    expect(fd.getAll('files')).toHaveLength(0);
  });

  it('未选文件时直接拒绝，不发请求', async () => {
    const calls = installFetch(() => json({}));
    await expect(previewImportFiles([], { format: 'nai_images', level: 'group' })).rejects.toThrow();
    expect(calls).toHaveLength(0);
  });

  it('全部无效 422 的逐文件原因可从 error.details.files 取出；413 保持 import:limit', async () => {
    installFetch(() => json({
      error: {
        code: 'import:empty', message: 'empty',
        details: { files: [
          { file_index: 1, name: 'a.png', issues: [{ code: 'metadata:empty' }] },
          { file_index: 2, name: 'b.png', issues: [{ code: 'image:invalid' }, { code: 'metadata:invalid' }] },
          { file_index: 3, name: 'c.png', issues: [] },
        ] },
      },
    }, 422));
    const e = await previewImportFiles([png('a.png')], { format: 'nai_images', level: 'group' }).catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(422);
    expect(importFileIssues(e)).toEqual([
      { file_index: 1, name: 'a.png', code: 'metadata:empty', message: undefined },
      { file_index: 2, name: 'b.png', code: 'image:invalid', message: undefined },
      { file_index: 2, name: 'b.png', code: 'metadata:invalid', message: undefined },
      { file_index: 3, name: 'c.png', code: undefined, message: undefined },
    ]);
    expect(importFileIssues(new Error('x'))).toEqual([]);

    installFetch(() => json({ error: { code: 'import:limit', message: 'too many' } }, 413));
    const e2 = await previewImportFiles([png('a.png')], { format: 'nai_images', level: 'group' }).catch((x) => x);
    expect(e2.status).toBe(413);
    expect(describeErrorCode(e2.code)).toContain('上限');
  });
});

describe('T13 预览缩略图与确认', () => {
  it('缩略图路径带 size，取图带 Bearer 并返回 Blob', async () => {
    expect(importPreviewPath('p 1', 'e/1')).toBe('/api/prompt-imports/p%201/entries/e%2F1/preview?size=thumb');
    const calls = installFetch(() => new Response('img', { status: 200, headers: { 'Content-Type': 'image/webp' } }));
    const b = await fetchImportPreviewBlob('p1', 'e1', 'full');
    expect(calls[0].url).toBe('/api/prompt-imports/p1/entries/e1/preview?size=full');
    expect(calls[0].headers.Authorization).toBe('Bearer tok');
    expect(b.size).toBe(3);
  });

  it('确认：新组带 If-None-Match，追加带 If-Match；重试保留原 Idempotency-Key', async () => {
    const calls = installFetch(() => json({ ref, inserted: 1, duplicates: 0, conflicts: [], revision: 3, version: '3' }, 200, { ETag: '"3"' }));
    await confirmImport('p1', 'dg', ref, 0, null, 'k-1');
    await confirmImport('p1', 'dg', ref, 2, '2', 'k-1');
    expect(calls[0].headers['If-None-Match']).toBe('*');
    expect(calls[0].headers['If-Match']).toBeUndefined();
    expect(calls[1].headers['If-Match']).toBe('"2"');
    expect(calls.map((c) => c.headers['Idempotency-Key'])).toEqual(['k-1', 'k-1']);
    expect(JSON.parse(String(calls[0].body))).toEqual({ digest: 'dg', ref });
  });
});

describe('T13 导出', () => {
  it('format=json|nai_image_zip 作为 query，返回附件 Blob', async () => {
    const calls = installFetch((c) => new Response(c.url.includes('nai_image_zip') ? 'zip' : '{}', { status: 200 }));
    const z = await exportGroup(ref, 'nai_image_zip');
    const j = await exportGroup(ref, 'json');
    expect(calls[0].url).toBe('/api/prompt-groups/g1/export?scope=global&format=nai_image_zip');
    expect(calls[1].url).toBe('/api/prompt-groups/g1/export?scope=global&format=json');
    expect(z.size).toBe(3);
    expect(j.size).toBe(2);
  });

  it('导出失败保留 ApiError（如 422 import:empty）', async () => {
    installFetch(() => json({ error: { code: 'import:empty', message: 'empty' } }, 422));
    const e = await exportGroup(ref, 'nai_image_zip').catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.code).toBe('import:empty');
  });

  it('downloadBlob 创建并回收 object URL', () => {
    const create = vi.fn(() => 'blob:x');
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    downloadBlob(new Blob(['z']), 'g.zip');
    expect(create).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith('blob:x');
    click.mockRestore();
  });
});

describe('T13 条目写入体与补齐任务', () => {
  it('POST/PATCH 只带 nl，source_format 可选；不再有 tag/expand_nl', async () => {
    const calls = installFetch(() => json({ entry_id: 'e1', nl: 'x' }, 201, { ETag: '"2"' }));
    await addEntry(ref, { nl: ' raw ', source_format: 'anima' }, '1', 'k1');
    await patchEntry(ref, 'e1', { nl: ' raw\n' }, '2', 'k2');
    expect(JSON.parse(String(calls[0].body))).toEqual({ nl: ' raw ', source_format: 'anima' });
    expect(JSON.parse(String(calls[1].body))).toEqual({ nl: ' raw\n' });
    // @ts-expect-error 旧字段已从类型中移除
    void patchEntry(ref, 'e1', { tag: ['a'] }, '2', 'k3').catch(() => {});
  });

  it('补齐任务 v5 直接返回 202 + done 的空任务，客户端原样透传', async () => {
    const calls = installFetch(() => json({ task_id: 't0', status: 'done' }, 202));
    const t = await enqueueCompletion(ref, ['e1'], 5, 'k', 'manual');
    expect(t).toEqual({ task_id: 't0', status: 'done' });
    expect(JSON.parse(String(calls[0].body))).toMatchObject({ type: 'prompt_completion', entry_ids: ['e1'], base_revision: 5 });
  });

  it('prompt:schema_changed 给出重新提交提示，任务摘要里也带出', () => {
    expect(describeErrorCode('prompt:schema_changed')).toContain('重新提交');
    const s = summarizeCompletion({ task_id: 't', status: 'paused', reason_code: 'prompt:schema_changed', reports: [] }, '补齐');
    expect(s.text).toContain('重新提交');
    expect(s.tone).toBe('warning');
  });
});

describe('T14 显示基础', () => {
  it('normalizeView 把旧值 default/nl 与未知值映射到 prompt', () => {
    expect(normalizeView('default')).toBe('prompt');
    expect(normalizeView('nl')).toBe('prompt');
    expect(normalizeView(undefined)).toBe('prompt');
    expect(normalizeView('prompt')).toBe('prompt');
    expect(normalizeView('variants')).toBe('variants');
  });

  it('entryBlock 只暴露 nl 原文本，缺字段标签只有提示词', () => {
    const b = entryBlock({
      entry_id: 'e', nl: ' a ', disable: false, aspect: null, preview: null, variants: {},
      completion: { state: 'pending', missing: ['nl'], task_id: null, error_code: null },
    });
    expect(b).toEqual({ nl: ' a ', statusLabel: '待补全', missingLabels: ['提示词'] });
  });
});
