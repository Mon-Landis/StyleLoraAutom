import { QueryClient } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import type { Entry, GroupRef } from '../src/api/promptGroupTypes';
import { EntryDialog } from '../src/features/prompts/EntryDialog';
import { apiLeaveDeps, createLeaveCoordinator } from '../src/lib/autoLeave';
import { hasAnyBaseDrafts } from '../src/lib/baseDrafts';
import { useNotices } from '../src/state/notices';
import { useVariantDrafts } from '../src/state/variantDrafts';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };
const entry = (over: Partial<Entry> = {}): Entry => ({
  entry_id: 'e1', nl: 'a cat', disable: false, aspect: null, preview: null,
  completion: { state: 'complete', missing: [], task_id: null, error_code: null },
  variants: {},
  ...over,
});

interface Call { method: string; url: string; body: unknown; headers: Record<string, string> }
function installFetch(handler: (c: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const c: Call = {
      method: init?.method ?? 'GET', url: String(url),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
    };
    calls.push(c);
    return handler(c);
  }));
  return calls;
}
const json = (body: unknown, status = 200, etag?: string) =>
  new Response(JSON.stringify(body), { status, headers: etag ? { ETag: `"${etag}"` } : {} });
const err = (status: number, code: string) => json({ error: { code, message: code } }, status);
const isPost = (c: Call) => c.method === 'POST' && /\/entries(\?|$)/.test(c.url);

beforeEach(() => {
  setToken('tok');
  useVariantDrafts.getState().reset();
  useNotices.getState().reset();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const nlBox = () => screen.getByLabelText(/提示词（nl）/) as HTMLTextAreaElement;
const saveBtn = () => screen.getByRole('button', { name: '保存' });
const created = (over: Record<string, unknown> = {}) =>
  json({ entry_id: 'n1', nl: 'a', disable: false, aspect: null, ...over }, 201, '2');

describe('N2 新增 POST 期间的新输入不丢', () => {
  function setup(patchResp: () => Response = () => json({ entry_id: 'n1', nl: 'late' }, 200, '3')) {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const calls = installFetch(async (c) => {
      if (isPost(c)) { await gate; return created(); }
      if (c.method === 'PATCH') return patchResp();
      return json({ revision: 1 }, 200, '1');
    });
    const onClose = vi.fn();
    const onSaved = vi.fn();
    render(<EntryDialog mode="add" gref={ref} etag="1" onClose={onClose} onSaved={onSaved} />);
    fireEvent.change(nlBox(), { target: { value: 'a' } });
    fireEvent.click(saveBtn());
    return { calls, onClose, release };
  }

  it('PATCHes the diff typed while the POST was in flight onto the created entry, then closes', async () => {
    const { calls, onClose, release } = setup();
    await waitFor(() => expect(calls.some(isPost)).toBe(true));
    fireEvent.change(nlBox(), { target: { value: 'late' } }); // 请求期间的新输入
    release();
    await waitFor(() => expect(onClose).toHaveBeenCalled());

    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].url).toContain('/entries/n1');
    expect(patches[0].body).toEqual({ nl: 'late' }); // 只含差异字段
    expect(patches[0].headers['If-Match']).toBe('"2"'); // 用 POST 返回的新 ETag
    expect(calls.findIndex(isPost)).toBeLessThan(calls.findIndex((c) => c.method === 'PATCH'));
    expect(hasAnyBaseDrafts()).toBe(false);
  });

  it('closes without an extra PATCH when nothing changed during the POST', async () => {
    const { calls, onClose, release } = setup();
    await waitFor(() => expect(calls.some(isPost)).toBe(true));
    release();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(0);
  });

  it('keeps the dialog open with the new input when the follow-up PATCH fails', async () => {
    const { calls, onClose, release } = setup(() => err(500, 'storage:io_error'));
    await waitFor(() => expect(calls.some(isPost)).toBe(true));
    fireEvent.change(nlBox(), { target: { value: 'late' } });
    release();
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    await waitFor(() => expect(saveBtn()).toBeTruthy());
    expect(onClose).not.toHaveBeenCalled();
    expect(nlBox().value).toBe('late');
    expect(hasAnyBaseDrafts()).toBe(true); // 仍算未保存，离开/beforeunload 会拦
    expect(calls.filter(isPost)).toHaveLength(1); // 不会重复创建
  });

  it('a leave flush during the POST does not create a duplicate entry', async () => {
    const { calls, release } = setup();
    await waitFor(() => expect(calls.some(isPost)).toBe(true));
    fireEvent.change(nlBox(), { target: { value: 'late' } });
    const leaving = createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    release();
    await leaving;
    expect(calls.filter(isPost)).toHaveLength(1);
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].body).toEqual({ nl: 'late' });
  });
});

describe('N4 离开 flush 不静默覆盖他人的改动', () => {
  /** 服务端按 If-Match 判断：组 ETag 为 7，编辑起点 ETag 为 1 即过期。 */
  function server(serverNl: string) {
    return installFetch((c) => {
      if (c.method === 'PATCH') {
        return c.headers['If-Match'] === '"7"'
          ? json({ entry_id: 'e1', nl: 'mine' }, 200, '8')
          : err(412, 'dictionary:revision_conflict');
      }
      if (c.url.includes('/export')) return json({ nl: serverNl });
      if (c.method === 'POST') return json({ task_id: 't', status: 'done' }, 202);
      return json({ revision: 7, auto_complete: true }, 200, '7');
    });
  }

  it('blocks leaving when the group moved on and someone else changed the same field', async () => {
    const calls = server('someone else');
    render(<EntryDialog mode="edit" gref={ref} etag="1" entry={entry()} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'mine' } });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('blocked');
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].headers['If-Match']).toBe('"1"'); // 首次提交带编辑起点的 ETag，由服务端 412 触发对账
    expect(patches.every((p) => p.headers['If-Match'] !== '"7"')).toBe(true); // 从未带新 ETag 覆盖
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    expect(nlBox().value).toBe('mine');
    expect(hasAnyBaseDrafts()).toBe(true);
    await screen.findByText(/服务端已更新「提示词」/);
  });

  it('proceeds with the fresh ETag when the group moved on but the field was untouched by others', async () => {
    const calls = server('a cat');
    render(<EntryDialog mode="edit" gref={ref} etag="1" entry={entry()} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'mine' } });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('done');
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches.map((p) => p.headers['If-Match'])).toEqual(['"1"', '"7"']);
    expect(hasAnyBaseDrafts()).toBe(false);
  });
});

describe('R-F1 结果未知的新增 POST：冻结请求并原样重放', () => {
  const offline = () => { throw new TypeError('offline'); };
  const postHeaders = (c: Call) => ({ key: c.headers['Idempotency-Key'], match: c.headers['If-Match'] });

  function setup(first: (c: Call) => Response, patchResp: () => Response = () => json({ entry_id: 'n1', nl: 'b' }, 200, '3')) {
    let n = 0;
    const calls = installFetch((c) => {
      if (isPost(c)) { n += 1; return n === 1 ? first(c) : created(); }
      if (c.method === 'PATCH') return patchResp();
      return json({ revision: 1 }, 200, '1');
    });
    const onClose = vi.fn();
    render(<EntryDialog mode="add" gref={ref} etag="1" onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'a' } });
    fireEvent.click(saveBtn());
    return { calls, onClose };
  }
  const retryWith = async (value: string) => {
    await waitFor(() => expect((saveBtn() as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(nlBox(), { target: { value } });
    fireEvent.click(saveBtn());
  };

  it('replays the identical POST (body, If-Match, key) then PATCHes the new input onto the created entry', async () => {
    const { calls, onClose } = setup(offline);
    await screen.findByText('network:unreachable');
    await retryWith('b');
    await waitFor(() => expect(onClose).toHaveBeenCalled());

    const posts = calls.filter(isPost);
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toEqual(posts[0].body);
    expect(postHeaders(posts[1])).toEqual(postHeaders(posts[0]));
    expect(posts[0].body).toMatchObject({ nl: 'a' });
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].url).toContain('/entries/n1');
    expect(patches[0].body).toEqual({ nl: 'b' });
    expect(patches[0].headers['If-Match']).toBe('"2"');
    expect(patches[0].headers['Idempotency-Key']).not.toBe(posts[0].headers['Idempotency-Key']);
  });

  it('keeps the frozen request after a 503 as well (outcome unknown)', async () => {
    const { calls, onClose } = setup(() => err(503, 'server:unavailable'));
    await waitFor(() => expect(calls.filter(isPost)).toHaveLength(1));
    await retryWith('b');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const posts = calls.filter(isPost);
    expect(posts[1].body).toEqual(posts[0].body);
    expect(postHeaders(posts[1])).toEqual(postHeaders(posts[0]));
  });

  it('drops the frozen request on a definitive 4xx rejection and builds a new one from the form', async () => {
    const { calls, onClose } = setup(() => err(422, 'entry:invalid'));
    await waitFor(() => expect(calls.filter(isPost)).toHaveLength(1));
    await retryWith('b');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const posts = calls.filter(isPost);
    expect(posts[1].body).toMatchObject({ nl: 'b' });
    expect(posts[1].headers['Idempotency-Key']).not.toBe(posts[0].headers['Idempotency-Key']);
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(0);
  });

  it('treats 429 as unknown outcome and keeps the frozen request', async () => {
    const { calls, onClose } = setup(() => err(429, 'rate_limited'));
    await waitFor(() => expect(calls.filter(isPost)).toHaveLength(1));
    await retryWith('b');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const posts = calls.filter(isPost);
    expect(posts[1].body).toEqual(posts[0].body);
    expect(postHeaders(posts[1])).toEqual(postHeaders(posts[0]));
  });
});
