import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import type { Entry, GroupRef } from '../src/api/promptGroupTypes';
import { EntryDialog } from '../src/features/prompts/EntryDialog';
import { ControlPane } from '../src/features/prompts/ControlPane';
import { entryMenuItems } from '../src/lib/promptMenus';
import { useNotices } from '../src/state/notices';
import { useVariantDrafts } from '../src/state/variantDrafts';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };
const entry = (over: Partial<Entry> = {}): Entry => ({
  entry_id: 'e1', nl: 'a cat', disable: false, aspect: null, preview: null,
  variants: {},
  ...over,
});

interface Call { method: string; url: string; body: Record<string, unknown> | undefined }
function installFetch(handler: (c: Call) => Response) {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const c: Call = {
      method: init?.method ?? 'GET', url: String(url),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(c);
    return handler(c);
  }));
  return calls;
}
const json = (body: unknown, status = 200, etag?: string) =>
  new Response(JSON.stringify(body), { status, headers: etag ? { ETag: `"${etag}"` } : {} });
const isPost = (c: Call) => c.method === 'POST' && /\/entries(\?|$)/.test(c.url);

beforeEach(() => {
  setToken('tok');
  useVariantDrafts.getState().reset();
  useNotices.getState().reset();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const nlBox = () => screen.getByLabelText(/提示词（nl）/) as HTMLTextAreaElement;
const srcBox = () => screen.getByLabelText('来源格式') as HTMLSelectElement;

describe('EntryDialog 来源格式', () => {
  it('defaults to NAI and omits source_format on POST', async () => {
    const calls = installFetch(() => json({ entry_id: 'n1', nl: 'a' }, 201, '2'));
    const onClose = vi.fn();
    render(<EntryDialog mode="add" gref={ref} etag="1" onClose={onClose} onSaved={vi.fn()} />);
    expect(srcBox().value).toBe('nai');
    fireEvent.change(nlBox(), { target: { value: 'a' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.find(isPost)!.body).toEqual({ nl: 'a', aspect: null, disable: false });
  });

  it('sends source_format=anima with the POST and backfills the canonical text', async () => {
    const calls = installFetch(() => json({ entry_id: 'n1', nl: '1girl, artist:foo' }, 201, '2'));
    const onClose = vi.fn();
    render(<EntryDialog mode="add" gref={ref} etag="1" onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(srcBox(), { target: { value: 'anima' } });
    fireEvent.change(nlBox(), { target: { value: '1girl, @foo' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.find(isPost)!.body).toEqual({ nl: '1girl, @foo', aspect: null, disable: false, source_format: 'anima' });
    // 回填规范文本后不应再追加 PATCH（否则会二次转换）。
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(0);
  });

  it('PATCH carries source_format only when nl is submitted', async () => {
    const calls = installFetch((c) => (c.method === 'PATCH' ? json({ entry_id: 'e1', nl: c.body?.nl ?? 'a cat', disable: true }, 200, '2') : json({}, 200, '1')));
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag="1" entry={entry()} onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(srcBox(), { target: { value: 'anima' } });
    fireEvent.click(screen.getByLabelText('禁用此条目'));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const patches = calls.filter((c) => c.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].body).toEqual({ disable: true }); // 没改 nl：不带 source_format
  });

  it('PATCH with edited nl and anima source sends source_format, raw text not trimmed', async () => {
    const calls = installFetch((c) => (c.method === 'PATCH' ? json({ entry_id: 'e1', nl: ' x ' }, 200, '2') : json({}, 200, '1')));
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag="1" entry={entry()} onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.change(srcBox(), { target: { value: 'anima' } });
    fireEvent.change(nlBox(), { target: { value: ' x ' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ nl: ' x ', source_format: 'anima' });
  });

  it('append mode uses the same single nl form with its own title', () => {
    installFetch(() => json({}, 200, '1'));
    render(<EntryDialog mode="append" gref={ref} etag="1" onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByText('追加自然语言')).toBeTruthy();
    expect(nlBox()).toBeTruthy();
    expect(srcBox().value).toBe('nai');
  });
});

describe('T18 隐藏补齐入口', () => {
  it('entry context menu has no LLM completion item', () => {
    const noop = () => {};
    const keys = entryMenuItems({ disabled: false, onEdit: noop, onCopyJson: noop, onToggleDisable: noop, onDelete: noop }).map((i) => i.key);
    expect(keys).not.toContain('complete');
  });

  it('control pane uses single-nl wording', () => {
    render(<ControlPane hasGroup busy={false} generateBlockedReason={null} onAction={vi.fn()} />);
    expect(screen.getByRole('button', { name: '添加提示词' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '追加自然语言' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '添加元素' })).toBeNull();
  });
});
