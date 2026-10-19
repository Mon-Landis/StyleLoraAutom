import { QueryClient } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import { ApiError } from '../src/api/errors';
import { enqueueCompletion, refKey } from '../src/api/promptGroups';
import type { GroupRef, TaskView } from '../src/api/promptGroupTypes';
import { patchSettings } from '../src/api/safety';
import { apiLeaveDeps, createLeaveCoordinator, flushVariantDrafts, VariantConflictError, type LeaveDeps } from '../src/lib/autoLeave';
import { pendingWriteScopes, runGroupWrite } from '../src/lib/groupWriteQueue';
import { summarizeCompletion, watchCompletion } from '../src/lib/completionWatch';
import { hasNavGuards, registerNavGuard, runNavGuards } from '../src/lib/navGuard';
import { useNotices } from '../src/state/notices';
import { draftId, useVariantDrafts } from '../src/state/variantDrafts';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };
const other: GroupRef = { scope: 'global', project_token: null, group_id: 'g2' };
const task = (over: Partial<TaskView> = {}): TaskView => ({ task_id: 't1', status: 'pending', ...over });
const apiErr = (status: number, code: string) =>
  new ApiError(status, { code, message: code, retryable: false, retryAction: 'none', outcome: null, details: null, requestId: null });

// ---- 协调器单测（fake deps）----
function makeDeps(over: Partial<LeaveDeps> = {}) {
  const deps: LeaveDeps = {
    flush: vi.fn(async () => {}),
    onError: vi.fn(),
    ...over,
  };
  return { deps };
}

describe('leave coordinator (F31: flush only, never publishes)', () => {
  it('flushes drafts and reports done', async () => {
    const { deps } = makeDeps();
    const r = await createLeaveCoordinator(deps).leave(ref);
    expect(r.status).toBe('done');
    expect(deps.flush).toHaveBeenCalledWith(ref);
    expect(deps.onError).not.toHaveBeenCalled();
  });

  it('concurrent leaves of the same ref (StrictMode replay) share one flush', async () => {
    const { deps } = makeDeps();
    const c = createLeaveCoordinator(deps);
    const [a, b] = await Promise.all([c.leave(ref), c.leave(ref)]);
    expect(a).toBe(b);
    expect(deps.flush).toHaveBeenCalledTimes(1);
  });

  it('every leave flushes again (no duplicate marker), different groups are independent', async () => {
    const { deps } = makeDeps();
    const c = createLeaveCoordinator(deps);
    await c.leave(ref);
    await c.leave(ref);
    await Promise.all([c.leave(ref), c.leave(other)]);
    expect(deps.flush).toHaveBeenCalledTimes(4);
  });

  it('flush failure blocks leaving and can be retried', async () => {
    const boom = apiErr(500, 'storage:io_error');
    const { deps } = makeDeps({ flush: vi.fn(async () => { throw boom; }) });
    const c = createLeaveCoordinator(deps);
    const r = await c.leave(ref);
    expect(r.status).toBe('blocked');
    expect(deps.onError).toHaveBeenCalledWith(boom, ref);
    await c.leave(ref);
    expect(deps.flush).toHaveBeenCalledTimes(2);
  });

  it('re-flushes while hasDrafts stays true, then blocks after a bounded number of passes', async () => {
    let left = 1;
    const { deps } = makeDeps({ hasDrafts: vi.fn(() => left-- > 0) });
    expect((await createLeaveCoordinator(deps).leave(ref)).status).toBe('done');
    expect(deps.flush).toHaveBeenCalledTimes(2);

    const { deps: d2 } = makeDeps({ hasDrafts: vi.fn(() => true) });
    const r2 = await createLeaveCoordinator(d2).leave(ref);
    expect(r2.status).toBe('blocked');
    expect(vi.mocked(d2.flush).mock.calls.length).toBeGreaterThan(1);
    expect(vi.mocked(d2.flush).mock.calls.length).toBeLessThanOrEqual(5);
    expect(d2.onError).toHaveBeenCalled();
  });

  it('exposes no publish API', () => {
    const c = createLeaveCoordinator(makeDeps().deps) as Record<string, unknown>;
    expect(Object.keys(c)).toEqual(['leave']);
  });
});
// ---- HTTP 级（fake fetch）----
interface Call { method: string; url: string; body: unknown; headers: Record<string, string>; keepalive?: boolean }
function installFetch(handler: (c: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const f = vi.fn(async (url: string, init?: RequestInit) => {
    const c: Call = {
      method: init?.method ?? 'GET', url: String(url),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>, keepalive: init?.keepalive,
    };
    calls.push(c);
    return handler(c);
  });
  vi.stubGlobal('fetch', f);
  return calls;
}
const json = (body: unknown, status = 200, etag?: string) =>
  new Response(JSON.stringify(body), { status, headers: etag ? { ETag: `"${etag}"` } : {} });
const err = (status: number, code: string) => json({ error: { code, message: code } }, status);

beforeEach(() => {
  setToken('tok');
  useVariantDrafts.getState().reset();
  useNotices.getState().reset();
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('enqueueCompletion request body', () => {
  it('auto_leave never sends entry_ids (not even null) and carries trigger/base_revision/key', async () => {
    const calls = installFetch(() => json(task({ status: 'done' }), 202));
    await enqueueCompletion(ref, undefined, 7, 'key-1', 'auto_leave', { keepalive: true });
    expect(calls[0].body).toEqual({ type: 'prompt_completion', ref, base_revision: 7, trigger: 'auto_leave' });
    expect('entry_ids' in (calls[0].body as object)).toBe(false);
    expect(calls[0].headers['Idempotency-Key']).toBe('key-1');
    expect(calls[0].keepalive).toBe(true);
    // 即使误传 ids，auto_leave 也不带
    await enqueueCompletion(ref, ['e1'], 7, 'key-2', 'auto_leave');
    expect('entry_ids' in (calls[1].body as object)).toBe(false);
  });

  it('manual keeps entry_ids and defaults trigger to manual', async () => {
    const calls = installFetch(() => json(task(), 202));
    await enqueueCompletion(ref, ['e1'], 3);
    expect(calls[0].body).toEqual({ type: 'prompt_completion', ref, base_revision: 3, trigger: 'manual', entry_ids: ['e1'] });
    await enqueueCompletion(ref, undefined, 3);
    expect('entry_ids' in (calls[1].body as object)).toBe(false);
  });
});

describe('patchSettings', () => {
  it('sends prompt_auto_complete_default with If-Match', async () => {
    const calls = installFetch(() => json({ prompt_auto_complete_default: false }, 200, '4'));
    const r = await patchSettings({ prompt_auto_complete_default: false }, '3');
    expect(calls[0].method).toBe('PATCH');
    expect(calls[0].url).toBe('/api/settings');
    expect(calls[0].body).toEqual({ prompt_auto_complete_default: false });
    expect(calls[0].headers['If-Match']).toBe('"3"');
    expect(r.data.prompt_auto_complete_default).toBe(false);
  });
});

describe('flushVariantDrafts', () => {
  const seed = (r: GroupRef, entryId: string, key: string, value: string) =>
    useVariantDrafts.getState().set(draftId(refKey(r), entryId, key), value);

  it('merges drafts per entry into one PATCH, clears them, and leaves other groups alone', async () => {
    seed(ref, 'e1', 'short', 'a\nb');
    seed(ref, 'e1', 'long', 'c');
    seed(ref, 'e2', 'short', 'd');
    seed(other, 'e1', 'short', 'keep');
    let etag = 10;
    const calls = installFetch((c) => (c.method === 'GET' ? json({ revision: etag }, 200, String(etag)) : json({ entry_id: 'x' }, 200, String(++etag))));
    await flushVariantDrafts(ref);
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(2);
    expect(patches[0].body).toEqual({ variants: { short: 'a\nb', long: 'c' } });
    expect(patches[0].headers['If-Match']).toBe('"10"');
    expect(patches[1].headers['If-Match']).toBe('"11"'); // 沿用上一次响应的 ETag
    const left = Object.keys(useVariantDrafts.getState().drafts);
    expect(left).toEqual([draftId(refKey(other), 'e1', 'short')]);
  });

  it('412 re-reads the ETag, reconciles against the server entry, and retries once', async () => {
    seed(ref, 'e1', 'short', 'x');
    let first = true;
    const calls = installFetch((c) => {
      if (c.method === 'GET') return c.url.includes('/export') ? json({ variants: { short: 'old' } }) : json({ revision: 12 }, 200, '12');
      if (first) { first = false; return err(412, 'dictionary:revision_conflict'); }
      return json({ entry_id: 'e1' }, 200, '13');
    });
    await flushVariantDrafts(ref);
    // group ETag, export 对账, PATCH(412), group ETag, export 对账, PATCH
    expect(calls.map((c) => c.method)).toEqual(['GET', 'GET', 'PATCH', 'GET', 'GET', 'PATCH']);
    expect(Object.keys(useVariantDrafts.getState().drafts)).toHaveLength(0);
  });

  it('keeps drafts and throws when saving fails', async () => {
    seed(ref, 'e1', 'short', 'x');
    installFetch((c) => (c.method === 'GET' ? json({ revision: 1 }, 200, '1') : err(500, 'storage:io_error')));
    await expect(flushVariantDrafts(ref)).rejects.toBeInstanceOf(ApiError);
    expect(Object.keys(useVariantDrafts.getState().drafts)).toHaveLength(1);
  });

  it('drops drafts of an entry that no longer exists, saves the rest', async () => {
    seed(ref, 'gone', 'short', 'x');
    seed(ref, 'e2', 'short', 'y');
    installFetch((c) => {
      if (c.method === 'GET') return json({ revision: 1 }, 200, '1');
      return c.url.includes('/entries/gone') ? err(404, 'entry:not_found') : json({ entry_id: 'e2' }, 200, '2');
    });
    await flushVariantDrafts(ref);
    expect(Object.keys(useVariantDrafts.getState().drafts)).toHaveLength(0);
  });

  it('does nothing (no requests) when there are no drafts', async () => {
    const calls = installFetch(() => json({}));
    await flushVariantDrafts(ref);
    expect(calls).toHaveLength(0);
  });
});


describe('completion result hints', () => {
  it('summarizes applied / discarded / failed fields', () => {
    const s = summarizeCompletion({
      ...task({ status: 'done' }),
      reports: [
        { entry_id: 'a', applied_fields: ['nl', 'nl'], discarded_fields: ['nl'] },
        { entry_id: 'b', error_code: 'completion:incomplete', applied_fields: [], discarded_fields: [] },
      ],
    }, '自动补齐');
    expect(s).toMatchObject({ applied: 2, discarded: 1, failed: 1, tone: 'warning' });
    expect(s.text).toContain('写入 2 个字段');
    expect(s.text).toContain('1 个字段');
  });

  it('paused tasks are reported as not auto-retried', () => {
    const s = summarizeCompletion({ ...task({ status: 'paused' }), reports: [] }, '自动补齐');
    expect(s.text).toContain('不会自动重试');
    expect(s.tone).toBe('warning');
  });

  it('watchCompletion polls until terminal and settles once', async () => {
    vi.useFakeTimers();
    let n = 0;
    installFetch(() => json({ task_id: 't1', status: ++n < 3 ? 'running' : 'done', reports: [{ entry_id: 'a', applied_fields: ['nl'], discarded_fields: [] }] }));
    const onSettled = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, intervalMs: 10 });
    await vi.advanceTimersByTimeAsync(200);
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onSettled.mock.calls[0][1]).toMatchObject({ applied: 1, tone: 'success' });
    expect(n).toBe(3);
  });
});

describe('nav guard', () => {
  it('runs registered guards and stops navigation when one refuses', async () => {
    expect(hasNavGuards()).toBe(false);
    const off = registerNavGuard(() => false);
    expect(hasNavGuards()).toBe(true);
    expect(await runNavGuards()).toBe(false);
    off();
    expect(await runNavGuards()).toBe(true);
    expect(hasNavGuards()).toBe(false);
  });
});

describe('variantDrafts.clearScope', () => {
  it('removes only drafts in the given scope', () => {
    const s = useVariantDrafts.getState();
    s.set(draftId('a', 'e', 'k'), '1');
    s.set(draftId('b', 'e', 'k'), '2');
    s.clearScope('a');
    expect(Object.keys(useVariantDrafts.getState().drafts)).toEqual([draftId('b', 'e', 'k')]);
  });
});

// ---- 审查修复：竞态与幂等 ----
const deferred = () => {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => { resolve = r; });
  return { promise, resolve };
};
const drafts = () => useVariantDrafts.getState().drafts;
const draftKey = (r: GroupRef, entryId: string, key: string) => draftId(refKey(r), entryId, key);

describe('F1 保存期间的新输入不丢', () => {
  it('flush keeps text typed while the PATCH is in flight; the coordinator saves it in another pass', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'a');
    const patches: unknown[] = [];
    installFetch((c) => {
      if (c.method === 'PATCH') {
        patches.push(c.body);
        if (patches.length === 1) useVariantDrafts.getState().set(id, 'ab'); // 保存期间继续输入
        return json({ entry_id: 'e1', revision: 2 }, 200, String(patches.length + 1));
      }
      return json({ revision: 2 }, 200, '2');
    });
    await flushVariantDrafts(ref);
    expect(drafts()[id]).toBe('ab'); // 不被无条件 clearScope 清掉
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('done');
    expect(patches).toEqual([{ variants: { short: 'a' } }, { variants: { short: 'ab' } }]);
    expect(Object.keys(drafts())).toHaveLength(0);
  });

  it('typing during the flush is saved by a follow-up pass', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'a');
    const patches: unknown[] = [];
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') {
        patches.push(c.body);
        if (patches.length === 1) useVariantDrafts.getState().set(id, 'ab');
        return json({ entry_id: 'e1', revision: 2 }, 200, '2');
      }
      return json({ revision: 2 }, 200, '2');
    });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('done');
    expect(patches).toEqual([{ variants: { short: 'a' } }, { variants: { short: 'ab' } }]);
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    expect(Object.keys(drafts())).toHaveLength(0);
  });

  it('blocks leaving (draft kept, nothing posted) when the group keeps getting new drafts', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'a');
    let n = 0;
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') {
        useVariantDrafts.getState().set(id, `${drafts()[id]}x`);
        n += 1;
        return json({ entry_id: 'e1' }, 200, String(n + 1));
      }
      return json({ revision: 1 }, 200, '1');
    });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('blocked');
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    expect(drafts()[id]).toBeDefined();
    expect(useNotices.getState().items[0].text).toContain('草稿已保留');
  });

});

describe('F5 草稿 PATCH 成功即更新缓存', () => {
  const seedCache = (qc: QueryClient) => {
    const k = refKey(ref);
    qc.setQueryData(['pg-group', k], { data: { revision: 1 }, etag: '1' });
    qc.setQueryData(['pg-entries', k], {
      pageParams: [null],
      pages: [{ data: { items: [{ entry_id: 'e1', variants: { short: 'old' } }] }, etag: '1' }],
    });
    return k;
  };

  it("refreshes the old group's ETag and entries after the flush PATCH, without any POST", async () => {
    useVariantDrafts.getState().set(draftKey(ref, 'e1', 'short'), 'new');
    const calls = installFetch((c) => (c.method === 'PATCH'
      ? json({ entry_id: 'e1', revision: 9, variants: { short: 'new' } }, 200, '9')
      : json({ revision: 9 }, 200, '9')));
    const qc = new QueryClient();
    const k = seedCache(qc);
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const r = await createLeaveCoordinator(apiLeaveDeps(qc)).leave(ref);
    expect(r.status).toBe('done');
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    const group = qc.getQueryData<{ data: { revision: number }; etag: string | null }>(['pg-group', k]);
    expect(group?.etag).toBe('9');
    expect(group?.data.revision).toBe(9);
    const entries = qc.getQueryData<{ pages: { data: { items: { variants: Record<string, string> }[] } }[] }>(['pg-entries', k]);
    expect(entries?.pages[0].data.items[0].variants.short).toBe('new');
    const keys = spy.mock.calls.map((x) => JSON.stringify((x[0] as { queryKey: unknown }).queryKey));
    expect(keys).toContain(JSON.stringify(['pg-group', k]));
    expect(keys).toContain(JSON.stringify(['pg-entries', k]));
  });

  it('does not touch another group\'s cache', async () => {
    useVariantDrafts.getState().set(draftKey(ref, 'e1', 'short'), 'new');
    installFetch((c) => (c.method === 'PATCH' ? json({ entry_id: 'e1', revision: 9 }, 200, '9') : json({ revision: 9 }, 200, '9')));
    const qc = new QueryClient();
    qc.setQueryData(['pg-group', refKey(other)], { data: { revision: 1 }, etag: '1' });
    await createLeaveCoordinator(apiLeaveDeps(qc)).leave(ref);
    expect(qc.getQueryData<{ etag: string }>(['pg-group', refKey(other)])?.etag).toBe('1');
  });
});

describe('F8 同组写入串行与 412 对账', () => {
  it('runGroupWrite runs tasks of one scope in order, survives failures, and cleans up', async () => {
    const order: string[] = [];
    const gate = deferred();
    const a = runGroupWrite('s', async () => { await gate.promise; order.push('a'); throw new Error('boom'); });
    const b = runGroupWrite('s', async () => { order.push('b'); return 2; });
    const c = runGroupWrite('t', async () => { order.push('c'); });
    await c;
    expect(order).toEqual(['c']); // 另一个 scope 不被阻塞
    gate.resolve();
    await expect(a).rejects.toThrow('boom');
    expect(await b).toBe(2);
    expect(order).toEqual(['c', 'a', 'b']);
    await Promise.resolve();
    await Promise.resolve();
    expect(pendingWriteScopes()).toBe(0);
  });

  it('flush waits for an in-flight editor save and then has nothing left to write (no stale overwrite)', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'old');
    const gate = deferred();
    // 模拟编辑器保存：持有队列，完成后清掉自己保存的草稿。
    const editor = runGroupWrite(refKey(ref), async () => { await gate.promise; useVariantDrafts.getState().clear(id); });
    const calls = installFetch(() => json({}));
    const flush = flushVariantDrafts(ref);
    await new Promise((r) => setTimeout(r, 10));
    expect(calls).toHaveLength(0); // 还在排队，没有抢跑
    gate.resolve();
    await editor;
    await flush;
    expect(calls).toHaveLength(0); // 草稿已被保存：不再用旧快照 PATCH
  });

  it('flush started while the editor save is running picks up the newest draft text when its turn comes', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'v1');
    const gate = deferred();
    const editor = runGroupWrite(refKey(ref), async () => { await gate.promise; });
    const patches: unknown[] = [];
    installFetch((c) => {
      if (c.method === 'PATCH') { patches.push(c.body); return json({ entry_id: 'e1' }, 200, '2'); }
      return json({ revision: 1 }, 200, '1');
    });
    const flush = flushVariantDrafts(ref);
    useVariantDrafts.getState().set(id, 'v2'); // 排队期间继续输入
    gate.resolve();
    await editor;
    await flush;
    expect(patches).toEqual([{ variants: { short: 'v2' } }]);
  });

  it('412: the server already holds the draft text -> no second PATCH, draft cleared', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'same');
    let patches = 0;
    installFetch((c) => {
      if (c.method === 'PATCH') { patches += 1; return err(412, 'dictionary:revision_conflict'); }
      // 提交前服务端还是旧值；412 之后别处已写成同样的文本。
      if (c.url.includes('/export')) return json({ variants: { short: patches === 0 ? 'old' : 'same' } });
      return json({ revision: 3 }, 200, '3');
    });
    await flushVariantDrafts(ref);
    expect(patches).toBe(1);
    expect(drafts()[id]).toBeUndefined();
  });

  it('412: a draft edited meanwhile is retried with the current text, never the stale snapshot', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'stale');
    const bodies: unknown[] = [];
    installFetch((c) => {
      if (c.method === 'PATCH') {
        bodies.push(c.body);
        if (bodies.length === 1) { useVariantDrafts.getState().set(id, 'fresh'); return err(412, 'dictionary:revision_conflict'); }
        return json({ entry_id: 'e1' }, 200, '4');
      }
      return c.url.includes('/export') ? json({ variants: { short: 'server' } }) : json({ revision: 3 }, 200, '3');
    });
    await flushVariantDrafts(ref);
    expect(bodies).toEqual([{ variants: { short: 'stale' } }, { variants: { short: 'fresh' } }]);
    expect(drafts()[id]).toBeUndefined();
  });

  it('412: a draft already saved and cleared meanwhile is skipped', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'x');
    let patches = 0;
    installFetch((c) => {
      if (c.method === 'PATCH') { patches += 1; useVariantDrafts.getState().clear(id); return err(412, 'dictionary:revision_conflict'); }
      if (c.url.includes('/export')) return json({ variants: { short: patches === 0 ? 'old' : 'x' } });
      return json({ revision: 3 }, 200, '3');
    });
    await flushVariantDrafts(ref);
    expect(patches).toBe(1);
  });

  it('412 twice: throws, the draft is kept, and leaving is blocked', async () => {
    const id = draftKey(ref, 'e1', 'short');
    useVariantDrafts.getState().set(id, 'x');
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') return err(412, 'dictionary:revision_conflict');
      return c.url.includes('/export') ? json({ variants: { short: 'other' } }) : json({ revision: 3 }, 200, '3');
    });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('blocked');
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(2);
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    expect(drafts()[id]).toBe('x');
  });
});

describe('R-F3 离开 flush 核对草稿编辑起点', () => {
  const id = () => draftKey(ref, 'e1', 'short');
  const seedWithStart = (value: string, start?: string) => useVariantDrafts.getState().set(id(), value, start);
  /** 服务端：export 返回 serverValue，group ETag 固定；记录 PATCH。 */
  function server(serverValue: string | (() => string)) {
    const patches: unknown[] = [];
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') { patches.push(c.body); return json({ entry_id: 'e1' }, 200, '6'); }
      if (c.url.includes('/export')) return json({ variants: { short: typeof serverValue === 'function' ? serverValue() : serverValue } });
      return json({ revision: 5 }, 200, '5');
    });
    return { calls, patches };
  }

  it('records the start value once; later typing does not move it; rebase moves it', () => {
    const st = useVariantDrafts.getState();
    st.set(id(), 'a', 'orig');
    st.set(id(), 'ab', 'IGNORED');
    expect(useVariantDrafts.getState().starts[id()]).toBe('orig');
    st.rebase(id(), 'a');
    expect(useVariantDrafts.getState().starts[id()]).toBe('a');
    st.clear(id());
    expect(useVariantDrafts.getState().starts[id()]).toBeUndefined();
  });

  it('server changed by someone else (!= start, != draft): no PATCH, draft kept, VariantConflictError', async () => {
    seedWithStart('mine', 'orig');
    const { patches } = server('theirs');
    const err1 = await flushVariantDrafts(ref).catch((e) => e);
    expect(err1).toBeInstanceOf(VariantConflictError);
    expect((err1 as VariantConflictError).keys).toEqual(['short']);
    expect(patches).toHaveLength(0);
    expect(drafts()[id()]).toBe('mine');
  });

  it('the leave coordinator blocks and notifies with a 「服务端已更新…」 message', async () => {
    seedWithStart('mine', 'orig');
    const { patches } = server('theirs');
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('blocked');
    expect(patches).toHaveLength(0);
    expect(drafts()[id()]).toBe('mine');
    const text = useNotices.getState().items[0].text;
    expect(text).toContain('服务端已更新');
    expect(text).toContain('「short」');
    expect(text).toContain('草稿已保留');
  });

  it('server still equals the start: the draft is committed', async () => {
    seedWithStart('mine', 'orig');
    const { patches } = server('orig');
    await flushVariantDrafts(ref);
    expect(patches).toEqual([{ variants: { short: 'mine' } }]);
    expect(drafts()[id()]).toBeUndefined();
  });

  it('server already equals the draft: no PATCH, draft cleared', async () => {
    seedWithStart('mine', 'orig');
    const { patches } = server('mine');
    await flushVariantDrafts(ref);
    expect(patches).toHaveLength(0);
    expect(drafts()[id()]).toBeUndefined();
  });

  it('a draft without a start value is still committed (nothing to compare against)', async () => {
    seedWithStart('mine');
    const { patches } = server('theirs');
    await flushVariantDrafts(ref);
    expect(patches).toEqual([{ variants: { short: 'mine' } }]);
  });

  it('reads the ETag before the server entry (the order that a 412 can guard)', async () => {
    seedWithStart('mine', 'orig');
    const { calls } = server('orig');
    await flushVariantDrafts(ref);
    const order = calls.map((c) => (c.method === 'PATCH' ? 'patch' : c.url.includes('/export') ? 'export' : 'etag'));
    expect(order).toEqual(['etag', 'export', 'patch']);
  });

  it('412 branch: a change by someone else made between the check and the PATCH is detected, not overwritten', async () => {
    seedWithStart('mine', 'orig');
    let patched = 0;
    installFetch((c) => {
      if (c.method === 'PATCH') { patched += 1; return err(412, 'dictionary:revision_conflict'); }
      // 首次对账时服务端还是起点；412 之后已被他人改掉。
      if (c.url.includes('/export')) return json({ variants: { short: patched === 0 ? 'orig' : 'theirs' } });
      return json({ revision: 5 }, 200, '5');
    });
    const e = await flushVariantDrafts(ref).catch((x) => x);
    expect(e).toBeInstanceOf(VariantConflictError);
    expect(patched).toBe(1); // 没有第二次 PATCH
    expect(drafts()[id()]).toBe('mine');
  });

  it('only the conflicting key is held back; other drafts of the entry are saved', async () => {
    useVariantDrafts.getState().set(draftKey(ref, 'e1', 'short'), 'mine', 'orig');
    useVariantDrafts.getState().set(draftKey(ref, 'e1', 'long'), 'L', 'l0');
    const patches: unknown[] = [];
    installFetch((c) => {
      if (c.method === 'PATCH') { patches.push(c.body); return json({ entry_id: 'e1' }, 200, '6'); }
      if (c.url.includes('/export')) return json({ variants: { short: 'theirs', long: 'l0' } });
      return json({ revision: 5 }, 200, '5');
    });
    const e = await flushVariantDrafts(ref).catch((x) => x);
    expect(e).toBeInstanceOf(VariantConflictError);
    expect(patches).toEqual([{ variants: { long: 'L' } }]);
    expect(drafts()[draftKey(ref, 'e1', 'short')]).toBe('mine');
    expect(drafts()[draftKey(ref, 'e1', 'long')]).toBeUndefined();
  });

  it('after a conflict the start moves to the server value, so leaving again confirms the overwrite', async () => {
    seedWithStart('mine', 'orig');
    const { patches } = server('theirs');
    await flushVariantDrafts(ref).catch(() => {});
    expect(useVariantDrafts.getState().starts[id()]).toBe('theirs');
    await flushVariantDrafts(ref);
    expect(patches).toEqual([{ variants: { short: 'mine' } }]);
    expect(drafts()[id()]).toBeUndefined();
  });

  it('typing during a save keeps the draft and moves its start to the saved value', async () => {
    seedWithStart('a', 'orig');
    let n = 0;
    installFetch((c) => {
      if (c.method === 'PATCH') {
        n += 1;
        if (n === 1) useVariantDrafts.getState().set(id(), 'ab');
        return json({ entry_id: 'e1' }, 200, '6');
      }
      if (c.url.includes('/export')) return json({ variants: { short: n === 0 ? 'orig' : 'a' } });
      return json({ revision: 5 }, 200, '5');
    });
    await flushVariantDrafts(ref);
    expect(drafts()[id()]).toBe('ab');
    expect(useVariantDrafts.getState().starts[id()]).toBe('a');
    // 第二轮：服务端是 'a'（== 新起点），可以提交，不误报冲突。
    await flushVariantDrafts(ref);
    expect(drafts()[id()]).toBeUndefined();
  });
});
