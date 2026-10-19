import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import type { GroupRef, ImportPreviewV5 } from '../src/api/promptGroupTypes';
import { ImportPreview } from '../src/features/prompts/ImportPreview';
import { DEFAULT_SAFETY } from '../src/lib/nsfw';
import { useSafety } from '../src/state/safety';

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };
const base = (over: Partial<ImportPreviewV5> = {}): ImportPreviewV5 => ({
  preview_id: 'p1', digest: 'd', ref, valid_count: 0, skipped_count: 0, issues: [], warnings: [], ...over,
});
const entries = (n: number) => Array.from({ length: n }, (_, i) => ({ entry_id: `e${i + 1}`, nl: `prompt ${i + 1}` }));

const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;

beforeEach(() => {
  setToken('tok');
  useSafety.setState({ settings: DEFAULT_SAFETY, etag: null, loaded: false, revealed: false, policyVersion: 0 });
  URL.createObjectURL = vi.fn(() => 'blob:t') as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
  vi.stubGlobal('fetch', vi.fn(async () => new Response(new Uint8Array([1, 2, 3]))));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
});

describe('R-F4 无 files 报告的预览也展示规范化 nl', () => {
  it('lists the normalized nl of JSON/TXT/legacy previews', () => {
    render(<ImportPreview sensitive={false} preview={base({ valid_count: 2, entries: [
      { entry_id: 'a', nl: '1girl, solo' }, { entry_id: 'b', nl: 'blue sky' },
    ] })} />);
    const list = screen.getByLabelText('转换后的提示词');
    expect(list.querySelectorAll('li')).toHaveLength(2);
    expect(list.textContent).toContain('1girl, solo');
    expect(list.textContent).toContain('blue sky');
    expect(screen.queryByLabelText('逐文件报告')).toBeNull();
  });

  it('pages 50 at a time with a 「显示更多」 button', () => {
    render(<ImportPreview sensitive={false} preview={base({ valid_count: 120, entries: entries(120) })} />);
    const count = () => screen.getByLabelText('转换后的提示词').querySelectorAll('li').length;
    expect(count()).toBe(50);
    fireEvent.click(screen.getByRole('button', { name: '显示更多（还有 70 条）' }));
    expect(count()).toBe(100);
    fireEvent.click(screen.getByRole('button', { name: '显示更多（还有 20 条）' }));
    expect(count()).toBe(120);
    expect(screen.queryByRole('button', { name: /显示更多/ })).toBeNull();
  });

  it('keeps showing row issues next to the entries, and renders nothing extra when there are no entries', () => {
    const { rerender } = render(<ImportPreview sensitive={false} preview={base({
      entries: entries(1), skipped_count: 1, issues: [{ row: 3, code: 'x', message: 'bad row' }],
    })} />);
    expect(screen.getByLabelText('转换后的提示词').textContent).toContain('prompt 1');
    expect(screen.getByText(/第 3 行/)).toBeTruthy();
    rerender(<ImportPreview sensitive={false} preview={base({ entries: [] })} />);
    expect(screen.queryByLabelText('转换后的提示词')).toBeNull();
  });
});

describe('R-F5 缩略图可被键盘到达', () => {
  const filePreview = base({
    valid_count: 1,
    entries: [{ entry_id: 'e1', nl: 'x', file_index: 1 }],
    files: [{ file_index: 1, name: 'a.png', status: 'valid', source: 'Comment', entry_id: 'e1', issues: [] }],
  });

  it('the thumbnail is focusable (tabIndex 0) and has an accessible name', async () => {
    render(<ImportPreview sensitive={false} preview={filePreview} />);
    const thumb = screen.getByRole('img', { name: 'a.png 预览' });
    expect(thumb.getAttribute('tabindex')).toBe('0');
    await waitFor(() => expect(document.querySelector('img')?.getAttribute('src')).toBe('blob:t'));
  });

  it('keyboard focus on a sensitive thumbnail reveals the 「显示」 button', () => {
    useSafety.setState({ settings: { nsfw_enabled: true, nsfw_display_mode: 'click_reveal' } });
    render(<ImportPreview sensitive preview={filePreview} />);
    expect(screen.queryByRole('button', { name: '显示' })).toBeNull();
    const thumb = screen.getByRole('img', { name: /a\.png 预览/ });
    expect(thumb.getAttribute('tabindex')).toBe('0');
    fireEvent.focus(thumb);
    expect(screen.getByRole('button', { name: '显示' })).toBeTruthy();
  });
});
