import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import type { GroupRef, ImportPreviewV5 } from '../src/api/promptGroupTypes';
import { ImportDialog, type ImportDialogProps } from '../src/features/prompts/ImportDialog';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };

interface Call { method: string; url: string; headers: Record<string, string>; body: unknown; signal?: AbortSignal | null }
type Handler = (c: Call) => Response | Promise<Response>;

function installFetch(handler: Handler) {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const c: Call = {
      method: init?.method ?? 'GET', url: String(url),
      headers: (init?.headers ?? {}) as Record<string, string>, body: init?.body, signal: init?.signal,
    };
    calls.push(c);
    return handler(c);
  }));
  return calls;
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const apiError = (status: number, code: string, details?: unknown) =>
  json({ error: { code, message: code, retryable: false, details } }, status);
const isPreview = (c: Call) => c.method === 'POST' && c.url === '/api/prompt-imports/preview';
const isThumb = (c: Call) => c.method === 'GET' && /\/entries\/[^/]+\/preview\?size=thumb$/.test(c.url);
const isConfirm = (c: Call) => c.method === 'POST' && /\/api\/prompt-imports\/[^/]+\/confirm$/.test(c.url);
const thumbResponse = () => new Response(new Uint8Array([1, 2, 3]));
const optionsOf = (c: Call) => JSON.parse(String((c.body as FormData).get('options'))) as Record<string, unknown>;

const png = (name: string) => new File([new Uint8Array(8)], name, { type: 'image/png' });
const jsonFile = (name = 'a.json') => new File(['{"prompts":[]}'], name, { type: 'application/json' });

const IMAGE_FILES: ImportPreviewV5['files'] = [
  { file_index: 1, name: 'a.png', status: 'valid', source: 'Comment.v4_prompt', entry_id: 'e1', issues: [] },
  { file_index: 2, name: 'b.png', status: 'skipped', source: null, issues: [{ code: 'metadata:empty', file_index: 2 }] },
];
const preview = (over: Partial<ImportPreviewV5> = {}): ImportPreviewV5 => ({
  preview_id: 'p1', digest: 'd1', base_revision: 0, ref, valid_count: 1, skipped_count: 1,
  normalization_version: 'nai-storage-v1',
  metadata: { name: '导入提示词组', nsfw: false, scope: 'global' },
  entries: [{ entry_id: 'e1', nl: '1girl, solo', file_index: 1 }],
  files: IMAGE_FILES,
  issues: [], warnings: [{ code: 'normalization:rounded', file_index: 1 }],
  ...over,
});
const simple = (over: Partial<ImportPreviewV5> = {}): ImportPreviewV5 =>
  ({ preview_id: 'p2', digest: 'd2', base_revision: 0, ref, valid_count: 1, skipped_count: 0, issues: [], warnings: [], ...over });

const baseProps: ImportDialogProps = { projectId: null, target: null, etag: null, onClose: vi.fn(), onDone: vi.fn() };
const mount = (props: Partial<ImportDialogProps> = {}) => render(<ImportDialog {...baseProps} {...props} />);

const pickSource = (v: 'file' | 'nai_images') => fireEvent.change(screen.getByLabelText('来源'), { target: { value: v } });
const pickFiles = (label: string | RegExp, files: File[]) =>
  fireEvent.change(screen.getByLabelText(label), { target: { files } });
const clickPreview = () => fireEvent.click(screen.getByRole('button', { name: '预览' }));
const confirmBtn = () => screen.getByRole('button', { name: '导入 1 条' }) as HTMLButtonElement;

const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
let createUrl: ReturnType<typeof vi.fn>;
let revokeUrl: ReturnType<typeof vi.fn>;

beforeEach(() => {
  setToken('tok');
  let n = 0;
  createUrl = vi.fn(() => `blob:t${++n}`);
  revokeUrl = vi.fn();
  URL.createObjectURL = createUrl as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = revokeUrl as unknown as typeof URL.revokeObjectURL;
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  vi.clearAllMocks();
});

describe('ImportDialog 来自标准nai图像集', () => {
  it('uses a multi-select PNG picker and uploads one repeated `files` field per image', async () => {
    const calls = installFetch((c) => (isPreview(c) ? json(preview()) : thumbResponse()));
    mount();
    pickSource('nai_images');
    const input = screen.getByLabelText('选择 PNG 图像（可多选）') as HTMLInputElement;
    expect(input.multiple).toBe(true);
    expect(input.accept).toBe('image/png,.png');
    pickFiles('选择 PNG 图像（可多选）', [png('a.png'), png('b.png'), new File(['x'], 'c.txt', { type: 'text/plain' })]);
    expect(screen.getByText(/已选 2 张/)).toBeTruthy();
    expect(screen.getByText('已忽略 1 个非 PNG 文件')).toBeTruthy();
    clickPreview();
    await waitFor(() => expect(calls.some(isPreview)).toBe(true));
    const fd = calls.find(isPreview)!.body as FormData;
    expect(fd.getAll('files').map((f) => (f as File).name)).toEqual(['a.png', 'b.png']);
    expect(fd.get('file')).toBeNull();
    expect(optionsOf(calls.find(isPreview)!)).toMatchObject({ format: 'nai_images', level: 'group' });
  });

  it('fixes the source to NAI and offers no dialect choice', async () => {
    const calls = installFetch((c) => (isPreview(c) ? json(preview()) : thumbResponse()));
    mount();
    pickSource('nai_images');
    expect(screen.queryByLabelText('来源格式')).toBeNull();
    expect(screen.getByText(/来源格式：NAI（固定）/)).toBeTruthy();
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    await waitFor(() => expect(calls.some(isPreview)).toBe(true));
    expect(optionsOf(calls.find(isPreview)!).source_format).toBe('nai');
  });

  it('level=entries passes the current ref', async () => {
    const calls = installFetch((c) => (isPreview(c) ? json(preview({ ref })) : thumbResponse()));
    mount({ target: ref, etag: '3' });
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    await waitFor(() => expect(calls.some(isPreview)).toBe(true));
    expect(optionsOf(calls.find(isPreview)!)).toMatchObject({ level: 'entries', ref, source_format: 'nai' });
  });
});

describe('ImportDialog 文件来源', () => {
  it('JSON defaults to Anima (backend default, nothing forced) and can switch to NAI', async () => {
    const calls = installFetch((c) => (isPreview(c) ? json(simple()) : thumbResponse()));
    mount();
    pickFiles(/选择文件/, [jsonFile()]);
    expect((screen.getByLabelText('来源格式') as HTMLSelectElement).value).toBe('anima');
    clickPreview();
    await waitFor(() => expect(calls.filter(isPreview)).toHaveLength(1));
    const first = calls.find(isPreview)!;
    const fd = first.body as FormData;
    expect((fd.get('file') as File).name).toBe('a.json');
    expect(fd.getAll('files')).toHaveLength(0);
    expect(optionsOf(first)).toMatchObject({ format: 'json', level: 'group' });
    expect(optionsOf(first).source_format).toBeUndefined();

    fireEvent.change(screen.getByLabelText('来源格式'), { target: { value: 'nai' } });
    await waitFor(() => expect((screen.getByRole('button', { name: '预览' }) as HTMLButtonElement).disabled).toBe(false));
    clickPreview();
    await waitFor(() => expect(calls.filter(isPreview)).toHaveLength(2));
    expect(optionsOf(calls.filter(isPreview)[1]).source_format).toBe('nai');
  });

  it('ZIP image packs are previewed as nai_image_zip with a single file field', async () => {
    const calls = installFetch((c) => (isPreview(c) ? json(simple()) : thumbResponse()));
    mount();
    pickFiles(/选择文件/, [new File([new Uint8Array(4)], 'pack.zip', { type: 'application/zip' })]);
    clickPreview();
    await waitFor(() => expect(calls.some(isPreview)).toBe(true));
    const c = calls.find(isPreview)!;
    expect(optionsOf(c).format).toBe('nai_image_zip');
    expect((c.body as FormData).get('file')).toBeTruthy();
  });
});

describe('ImportDialog 预览报告', () => {
  it('renders per-file status, extraction source, issues and normalization warnings', async () => {
    installFetch((c) => (isPreview(c) ? json(preview()) : thumbResponse()));
    const { container } = mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png'), png('b.png')]);
    clickPreview();
    await screen.findByLabelText('逐文件报告');
    const valid = container.querySelector('[data-status="valid"]')!;
    const skipped = container.querySelector('[data-status="skipped"]')!;
    expect(valid.textContent).toContain('有效');
    expect(valid.textContent).toContain('Comment.v4_prompt');
    expect(valid.textContent).toContain('1girl, solo');
    expect(skipped.textContent).toContain('跳过');
    expect(skipped.textContent).toContain('图片没有可用的正面提示词');
    expect(skipped.textContent).toContain('提取来源：无');
    expect(screen.getByText(/标准化警告（1）/)).toBeTruthy();
    expect(screen.getByText(/nai-storage-v1/)).toBeTruthy();
  });

  it('shows per-file reasons for a 422 import:empty and a friendly 413 message', async () => {
    let status = 422;
    installFetch((c) => {
      if (!isPreview(c)) return thumbResponse();
      return status === 422
        ? apiError(422, 'import:empty', { files: [{ file_index: 1, name: 'a.png', issues: [{ code: 'image:invalid' }] }] })
        : apiError(413, 'import:limit');
    });
    mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    const list = await screen.findByLabelText('逐文件原因');
    expect(list.textContent).toContain('a.png');
    expect(list.textContent).toContain('图像无效');
    expect(screen.getByText('没有有效条目可导入')).toBeTruthy();

    status = 413;
    pickFiles(/选择 PNG/, [png('c.png')]);
    clickPreview();
    await screen.findByText('上传超出数量或大小上限，请减少文件后重试');
  });
});

describe('ImportDialog 缩略图 Blob URL', () => {
  it('creates object URLs for valid entries and revokes them when the selection changes', async () => {
    installFetch((c) => (isPreview(c) ? json(preview()) : thumbResponse()));
    const { container } = mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png'), png('b.png')]);
    clickPreview();
    await waitFor(() => expect(createUrl).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:t1'));
    expect(revokeUrl).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '移除 b.png' }));
    expect(revokeUrl).toHaveBeenCalledWith('blob:t1');
    expect(screen.queryByLabelText('逐文件报告')).toBeNull();
  });

  it('revokes on unmount and ignores thumbnails that arrive after unmount', async () => {
    let release: (r: Response) => void = () => {};
    const calls = installFetch((c) => {
      if (isPreview(c)) return json(preview({ files: [IMAGE_FILES![0]], skipped_count: 0 }));
      return new Promise<Response>((res) => { release = res; });
    });
    const { unmount } = mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    await waitFor(() => expect(calls.some(isThumb)).toBe(true));
    unmount();
    expect(calls.find(isThumb)!.signal?.aborted).toBe(true);
    await act(async () => { release(thumbResponse()); await new Promise((r) => setTimeout(r, 10)); });
    expect(createUrl).not.toHaveBeenCalled();
  });

  it('revokes created URLs on unmount', async () => {
    installFetch((c) => (isPreview(c) ? json(preview()) : thumbResponse()));
    const { unmount } = mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    await waitFor(() => expect(createUrl).toHaveBeenCalledTimes(1));
    unmount();
    expect(revokeUrl).toHaveBeenCalledWith('blob:t1');
  });
});

describe('ImportDialog 迟到响应', () => {
  it('discards a stale preview response after the selection changed', async () => {
    let resolveFirst: (r: Response) => void = () => {};
    let n = 0;
    const calls = installFetch((c) => {
      if (!isPreview(c)) return thumbResponse();
      n += 1;
      if (n === 1) return new Promise<Response>((res) => { resolveFirst = res; });
      return json(preview({
        preview_id: 'pB',
        files: [{ file_index: 1, name: 'fresh.png', status: 'valid', source: 'Description', entry_id: 'e1', issues: [] }],
        skipped_count: 0,
      }));
    });
    mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    await waitFor(() => expect(calls.filter(isPreview)).toHaveLength(1));

    pickFiles(/选择 PNG/, [png('b.png')]); // 改选：作废在途请求
    expect(calls.find(isPreview)!.signal?.aborted).toBe(true);
    clickPreview();
    await screen.findByText(/fresh\.png/);

    await act(async () => {
      resolveFirst(json(preview({
        preview_id: 'pA',
        files: [{ file_index: 1, name: 'late.png', status: 'valid', source: 'Description', entry_id: 'e1', issues: [] }],
      })));
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(screen.queryByText(/late\.png/)).toBeNull();
    expect(screen.getByText(/fresh\.png/)).toBeTruthy();
  });

  it('discards a late response when the dialog selection was cleared (no preview appears)', async () => {
    let resolveFirst: (r: Response) => void = () => {};
    const calls = installFetch((c) => (isPreview(c) ? new Promise<Response>((res) => { resolveFirst = res; }) : thumbResponse()));
    mount();
    pickSource('nai_images');
    pickFiles(/选择 PNG/, [png('a.png')]);
    clickPreview();
    await waitFor(() => expect(calls.some(isPreview)).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: '移除 a.png' }));
    await act(async () => { resolveFirst(json(preview())); await new Promise((r) => setTimeout(r, 10)); });
    expect(screen.queryByLabelText('逐文件报告')).toBeNull();
    expect(calls.some(isThumb)).toBe(false);
  });
});

describe('ImportDialog 确认重试', () => {
  it('retry reuses the same key, digest, ref and If-None-Match after a network failure', async () => {
    let attempts = 0;
    const onDone = vi.fn();
    const calls = installFetch((c) => {
      if (isPreview(c)) return json(simple({ preview_id: 'p9', digest: 'dg' }));
      if (isConfirm(c)) {
        attempts += 1;
        if (attempts === 1) throw new TypeError('offline');
        return json({ ref, inserted: 1, duplicates: 0, conflicts: [], revision: 1, version: 'v' });
      }
      return thumbResponse();
    });
    mount({ onDone });
    pickFiles(/选择文件/, [jsonFile()]);
    clickPreview();
    fireEvent.click(await screen.findByRole('button', { name: '导入 1 条' }));
    await screen.findByText('network:unreachable');
    await waitFor(() => expect(confirmBtn().disabled).toBe(false));
    fireEvent.click(confirmBtn());
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(ref));

    const [a, b] = calls.filter(isConfirm);
    expect(a.url).toBe('/api/prompt-imports/p9/confirm');
    expect(b.url).toBe(a.url);
    expect(a.headers['Idempotency-Key']).toBeTruthy();
    expect(b.headers['Idempotency-Key']).toBe(a.headers['Idempotency-Key']);
    expect(a.headers['If-None-Match']).toBe('*');
    expect(b.headers['If-None-Match']).toBe('*');
    expect(JSON.parse(String(a.body))).toEqual({ digest: 'dg', ref });
    expect(b.body).toBe(a.body);
  });

  it('level=entries keeps the original If-Match even if the etag prop changes before the retry', async () => {
    let attempts = 0;
    const onDone = vi.fn();
    const calls = installFetch((c) => {
      if (isPreview(c)) return json(simple({ preview_id: 'p3', digest: 'dg3', base_revision: 3 }));
      if (isConfirm(c)) {
        attempts += 1;
        if (attempts === 1) throw new TypeError('offline');
        return json({ ref, inserted: 1, duplicates: 0, conflicts: [], revision: 4, version: 'v' });
      }
      return thumbResponse();
    });
    const props: ImportDialogProps = { ...baseProps, target: ref, etag: 'e3', onDone };
    const { rerender } = render(<ImportDialog {...props} />);
    fireEvent.change(screen.getByLabelText(/粘贴 JSON/), { target: { value: '{"prompts":[]}' } });
    clickPreview();
    fireEvent.click(await screen.findByRole('button', { name: '导入 1 条' }));
    await screen.findByText('network:unreachable');
    rerender(<ImportDialog {...props} etag="e4" />);
    await waitFor(() => expect(confirmBtn().disabled).toBe(false));
    fireEvent.click(confirmBtn());
    await waitFor(() => expect(onDone).toHaveBeenCalled());

    const [a, b] = calls.filter(isConfirm);
    expect(a.headers['If-Match']).toBe('"e3"');
    expect(b.headers['If-Match']).toBe('"e3"');
    expect(a.headers['If-None-Match']).toBeUndefined();
    expect(b.headers['Idempotency-Key']).toBe(a.headers['Idempotency-Key']);
    expect(optionsOf(calls.find(isPreview)!)).toMatchObject({ level: 'entries', ref });
  });

  it('a 410 on confirm asks for a fresh preview and blocks re-submitting the expired one', async () => {
    installFetch((c) => (isPreview(c) ? json(simple()) : isConfirm(c) ? apiError(410, 'preview:expired') : thumbResponse()));
    mount();
    pickFiles(/选择文件/, [jsonFile()]);
    clickPreview();
    fireEvent.click(await screen.findByRole('button', { name: '导入 1 条' }));
    await screen.findByText('导入预览已过期，请重新预览');
    await screen.findByText(/请重新预览后再导入/);
    await waitFor(() => expect(confirmBtn().disabled).toBe(true));
  });
});

describe('ImportDialog 确认提交期间不可关闭 / 卸载守卫', () => {
  /** 预览后点击导入，确认请求挂起；返回挂起控制。 */
  async function startConfirm(props: Partial<ImportDialogProps> = {}) {
    let release!: (r: Response) => void;
    const calls = installFetch((c) => {
      if (isPreview(c)) return json(simple());
      if (isConfirm(c)) return new Promise<Response>((res) => { release = res; });
      return thumbResponse();
    });
    const utils = mount(props);
    pickFiles(/选择文件/, [jsonFile()]);
    clickPreview();
    fireEvent.click(await screen.findByRole('button', { name: '导入 1 条' }));
    await waitFor(() => expect(calls.some(isConfirm)).toBe(true));
    return { ...utils, calls, release: (r: Response) => release(r) };
  }
  const okConfirm = () => json({ ref, inserted: 1, duplicates: 0, conflicts: [], revision: 1, version: 'v' });

  it('disables cancel and ignores Esc / scrim / header close while confirming, and exposes aria-busy', async () => {
    const onClose = vi.fn();
    const onDone = vi.fn();
    const { container, release } = await startConfirm({ onClose, onDone });
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-busy')).toBe('true');
    expect((screen.getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '关闭' }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.mouseDown(container.ownerDocument.querySelector('.dialog__scrim')!);
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => { release(okConfirm()); await new Promise((r) => setTimeout(r, 10)); });
    expect(onDone).toHaveBeenCalledWith(ref);
    expect(dialog.getAttribute('aria-busy')).toBeNull();
  });

  it('closes normally (cancel / Esc) when not confirming', async () => {
    const onClose = vi.fn();
    installFetch(() => thumbResponse());
    mount({ onClose });
    expect(screen.getByRole('dialog').getAttribute('aria-busy')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('does not call onDone / onClose when the confirm succeeds after unmount', async () => {
    const onClose = vi.fn();
    const onDone = vi.fn();
    const { unmount, release } = await startConfirm({ onClose, onDone });
    unmount();
    await act(async () => { release(okConfirm()); await new Promise((r) => setTimeout(r, 10)); });
    expect(onDone).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not touch callbacks when the confirm fails after unmount', async () => {
    const onClose = vi.fn();
    const onDone = vi.fn();
    const { unmount, release } = await startConfirm({ onClose, onDone });
    unmount();
    await act(async () => { release(apiError(500, 'storage:io_error')); await new Promise((r) => setTimeout(r, 10)); });
    expect(onDone).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
