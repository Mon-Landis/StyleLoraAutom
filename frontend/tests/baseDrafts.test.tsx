import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { setToken } from '../src/api/client';
import { refKey } from '../src/api/promptGroups';
import type { Entry, GroupRef } from '../src/api/promptGroupTypes';
import { EntryDialog } from '../src/features/prompts/EntryDialog';
import { useAutoCompletion } from '../src/features/prompts/useAutoCompletion';
import { apiLeaveDeps, createLeaveCoordinator } from '../src/lib/autoLeave';
import { hasAnyBaseDrafts } from '../src/lib/baseDrafts';
import { overwrittenFields } from '../src/lib/promptVariants';
import { useNotices } from '../src/state/notices';
import { useVariantDrafts } from '../src/state/variantDrafts';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };
const entry = (over: Partial<Entry> = {}): Entry => ({
  entry_id: 'e1', nl: 'a cat', disable: false, aspect: null, preview: null,
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
const isGroupGet = (c: Call) => c.method === 'GET' && /\/api\/prompt-groups\/g1(\?|$)/.test(c.url);

beforeEach(() => {
  setToken('tok');
  useVariantDrafts.getState().reset();
  useNotices.getState().reset();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const nlBox = () => screen.getByLabelText(/提示词（nl）/) as HTMLTextAreaElement;
const saveBtn = () => screen.getByRole('button', { name: '保存' });

describe('F3 基础字段草稿参与离开 flush', () => {
  it('leave saves touched base fields (only touched) via PATCH, updates caches, and publishes nothing', async () => {
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') return json({ entry_id: 'e1', nl: 'new nl', revision: 9 }, 200, '9');
      return json({ revision: 9 }, 200, '9');
    });
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={onClose} onSaved={vi.fn()} />);
    expect(hasAnyBaseDrafts()).toBe(false);
    fireEvent.change(nlBox(), { target: { value: 'new nl' } });
    expect(hasAnyBaseDrafts()).toBe(true);

    const qc = new QueryClient();
    const k = refKey(ref);
    qc.setQueryData(['pg-group', k], { data: { revision: 1 }, etag: '1' });
    qc.setQueryData(['pg-entries', k], { pageParams: [null], pages: [{ data: { items: [entry()] }, etag: '1' }] });
    const r = await createLeaveCoordinator(apiLeaveDeps(qc)).leave(ref);

    expect(r.status).toBe('done');
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].body).toEqual({ nl: 'new nl' });
    // 离开不再发布补齐任务
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    const group = qc.getQueryData<{ etag: string; data: { revision: number } }>(['pg-group', k]);
    expect(group?.etag).toBe('9');
    const cached = qc.getQueryData<{ pages: { data: { items: Entry[] } }[] }>(['pg-entries', k]);
    expect(cached?.pages[0].data.items[0].nl).toBe('new nl');
    expect(hasAnyBaseDrafts()).toBe(false);
    expect(onClose).toHaveBeenCalled();
  });

  it('a failed base-field save blocks leaving, publishes nothing, and keeps the form and touched marks', async () => {
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') return err(500, 'storage:io_error');
      return json({ revision: 1 }, 200, '1');
    });
    const onClose = vi.fn();
    const e = entry({ generated: { nl: true, variants: {} } });
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={e} onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'typed' } });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('blocked');
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    expect(nlBox().value).toBe('typed');
    expect(hasAnyBaseDrafts()).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    // touched 保留：生成标记仍已清除
    expect(screen.queryByText(/AI 生成/, { selector: '.gen-mark' })).toBeNull();
    expect(useNotices.getState().items[0].text).toContain('草稿已保留');
  });

  it('untouched forms are not dirty and issue no PATCH on leave', async () => {
    const calls = installFetch(() => json({ revision: 1 }, 200, '1'));
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(hasAnyBaseDrafts()).toBe(false);
    await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false);
  });

  it('unregisters on unmount', () => {
    const { unmount } = render(<EntryDialog mode="add" gref={ref} etag={'"1"'} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'x' } });
    expect(hasAnyBaseDrafts()).toBe(true);
    unmount();
    expect(hasAnyBaseDrafts()).toBe(false);
  });

  it('beforeunload warns when a base field is dirty (and only then)', () => {
    const qc = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
    renderHook(() => useAutoCompletion(null), { wrapper });
    const fire = () => {
      const ev = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(ev);
      return ev.defaultPrevented;
    };
    expect(fire()).toBe(false);
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(fire()).toBe(false);
    fireEvent.change(nlBox(), { target: { value: 'dirty' } });
    expect(fire()).toBe(true);
  });
});

describe('F6 412 刷新 ETag 并允许重试', () => {
  it('re-reads the ETag, keeps the form, shows a conflict hint, and the retry uses the fresh ETag', async () => {
    let patches = 0;
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') {
        patches += 1;
        return patches === 1 ? err(412, 'dictionary:revision_conflict') : json({ entry_id: 'e1', nl: 'mine', revision: 8 }, 200, '8');
      }
      if (c.url.includes('/export')) return json({ nl: 'a cat' }); // 服务端该字段未被他人改
      return json({ revision: 7 }, 200, '7');
    });
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag="1" entry={entry()} onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'mine' } });
    fireEvent.click(saveBtn());
    await screen.findByText(/已刷新最新版本/);
    expect(nlBox().value).toBe('mine');
    expect(onClose).not.toHaveBeenCalled();
    expect(calls.some(isGroupGet)).toBe(true);

    fireEvent.click(saveBtn());
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const sent = calls.filter((c) => c.method === 'PATCH');
    expect(sent).toHaveLength(2);
    expect(sent[0].headers['If-Match']).toBe('"1"');
    expect(sent[1].headers['If-Match']).toBe('"7"'); // 不再持续用旧 ETag
    expect(sent[1].body).toEqual({ nl: 'mine' }); // 仍只提交 touched 字段
  });

  it('asks for confirmation when someone else changed the same field on the server', async () => {
    let patches = 0;
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') {
        patches += 1;
        return patches === 1 ? err(412, 'dictionary:revision_conflict') : json({ entry_id: 'e1' }, 200, '8');
      }
      if (c.url.includes('/export')) return json({ nl: 'someone else' });
      return json({ revision: 7 }, 200, '7');
    });
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'mine' } });
    fireEvent.click(saveBtn());
    await screen.findByText(/服务端已更新「提示词」，再次点击保存将覆盖/);
    expect(nlBox().value).toBe('mine');
    expect(patches).toBe(1); // 没有静默覆盖
    fireEvent.click(saveBtn());
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.filter((c) => c.method === 'PATCH')[1].headers['If-Match']).toBe('"7"');
  });

  it('leaving never silently overwrites a field flagged by the 412 reconciliation', async () => {
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') return err(412, 'dictionary:revision_conflict');
      if (c.url.includes('/export')) return json({ nl: 'someone else' });
      if (c.method === 'POST') return json({ task_id: 't', status: 'done' }, 202);
      return json({ revision: 7 }, 200, '7');
    });
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'mine' } });
    const co = createLeaveCoordinator(apiLeaveDeps(new QueryClient()));
    expect((await co.leave(ref)).status).toBe('blocked');
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(1);
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    expect(nlBox().value).toBe('mine');
    expect(hasAnyBaseDrafts()).toBe(true);
    await screen.findByText(/服务端已更新「提示词」/);
  });

  it('leave retries once after a harmless 412 with the fresh ETag', async () => {
    let patches = 0;
    const calls = installFetch((c) => {
      if (c.method === 'PATCH') {
        patches += 1;
        return patches === 1 ? err(412, 'dictionary:revision_conflict') : json({ entry_id: 'e1', nl: 'mine' }, 200, '8');
      }
      if (c.url.includes('/export')) return json({ nl: 'a cat' });
      if (c.method === 'POST') return json({ task_id: 't', status: 'done' }, 202);
      return json({ revision: 7 }, 200, '7');
    });
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.change(nlBox(), { target: { value: 'mine' } });
    const r = await createLeaveCoordinator(apiLeaveDeps(new QueryClient())).leave(ref);
    expect(r.status).toBe('done');
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(2);
    expect(hasAnyBaseDrafts()).toBe(false);
  });

  it('overwrittenFields lists only fields the server changed away from both base and draft', () => {
    const base = entry();
    const patch = { nl: 'mine', disable: true };
    expect(overwrittenFields(base, patch, { nl: 'a cat', disable: false })).toEqual([]);
    expect(overwrittenFields(base, patch, { nl: 'theirs', disable: false }).map((o) => o.field)).toEqual(['nl']);
    // 服务端值恰好等于草稿：无需提示
    expect(overwrittenFields(base, patch, { nl: 'mine' })).toEqual([]);
  });
});
