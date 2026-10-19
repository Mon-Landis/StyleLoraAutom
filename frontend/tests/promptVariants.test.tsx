import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Entry, GroupRef } from '../src/api/promptGroupTypes';
import { patchEntry, patchGroup } from '../src/api/promptGroups';
import { EntryBlockView } from '../src/features/prompts/EntryBlockView';
import { EntryDialog } from '../src/features/prompts/EntryDialog';
import { EntryPane } from '../src/features/prompts/EntryPane';
import { GeneratedField } from '../src/features/prompts/GeneratedField';
import { GroupSettingsDialog } from '../src/features/prompts/GroupSettingsDialog';
import { PromptViewSwitch } from '../src/features/prompts/PromptViewSwitch';
import { VariantEditor } from '../src/features/prompts/VariantEditor';
import { buildEntryPatch, checkVariantKey, hasBaseContent } from '../src/lib/promptVariants';
import { useVariantDrafts } from '../src/state/variantDrafts';

vi.mock('../src/api/promptGroups', async (orig) => ({
  ...(await orig<typeof import('../src/api/promptGroups')>()),
  patchEntry: vi.fn(),
  addEntry: vi.fn(),
  putPreview: vi.fn(),
  patchGroup: vi.fn(),
}));

const ref: GroupRef = { scope: 'global', project_token: null, group_id: 'g1' };
const entry = (over: Partial<Entry> = {}): Entry => ({
  entry_id: 'e1', nl: 'a cat', disable: false, aspect: null, preview: null,
  completion: { state: 'complete', missing: [], task_id: null, error_code: null },
  variants: {},
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  useVariantDrafts.getState().reset();
});

describe('promptVariants helpers', () => {
  it('checkVariantKey trims, rejects empty / duplicate / control / too long / too many', () => {
    expect(checkVariantKey('  short ', [])).toEqual({ ok: true, key: 'short' });
    expect(checkVariantKey('   ', []).ok).toBe(false);
    expect(checkVariantKey('a', ['a']).ok).toBe(false);
    expect(checkVariantKey('A', ['a']).ok).toBe(true); // 区分大小写
    expect(checkVariantKey('a\nb', []).ok).toBe(false);
    expect(checkVariantKey('x'.repeat(129), []).ok).toBe(false);
    expect(checkVariantKey('new', Array.from({ length: 64 }, (_, i) => `k${i}`)).ok).toBe(false);
  });

  it('buildEntryPatch sends only touched text fields and changed aspect/disable', () => {
    const e = entry();
    const form = { nl: 'a cat', aspect: '' as const, disable: false };
    expect(buildEntryPatch(e, form, new Set())).toEqual({});
    // v5：nl 原样提交，不 trim
    expect(buildEntryPatch(e, { ...form, nl: ' changed ' }, new Set(['nl']))).toEqual({ nl: ' changed ' });
    // 改后恢复原值仍算触及（后端同值提交也清标记）
    expect(buildEntryPatch(e, form, new Set(['nl']))).toEqual({ nl: 'a cat' });
    expect(buildEntryPatch(e, { ...form, aspect: 'square', disable: true }, new Set())).toEqual({ aspect: 'square', disable: true });
  });

  it('hasBaseContent honors patched clearing', () => {
    const e = entry({ nl: 'only nl' });
    expect(hasBaseContent(e, {})).toBe(true);
    expect(hasBaseContent(e, { nl: '' })).toBe(false);
    expect(hasBaseContent(e, { nl: '  ' })).toBe(false);
    expect(hasBaseContent(e, { nl: 'x' })).toBe(true);
  });
});

describe('PromptViewSwitch', () => {
  it('exposes tabs with aria-selected and moves with arrow keys', () => {
    const onChange = vi.fn();
    render(<PromptViewSwitch value="prompt" onChange={onChange} />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['提示词', '变体']);
    expect(tabs.map((t) => t.getAttribute('aria-selected'))).toEqual(['true', 'false']);
    expect(tabs.map((t) => t.tabIndex)).toEqual([0, -1]);
    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith('variants');
    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenLastCalledWith('variants');
    fireEvent.keyDown(tabs[0], { key: 'End' });
    expect(onChange).toHaveBeenLastCalledWith('variants');
  });
});

describe('EntryPane header', () => {
  const base = {
    gref: ref, title: 'My Dict', total: 128, nsfw: true, view: 'prompt' as const, onView: vi.fn(),
    entries: [entry()], loading: false, errorText: null, hasMore: true, loadingMore: false,
    onLoadMore: vi.fn(), selectedId: null, onSelect: vi.fn(), bindMenu: () => ({}), onPatchEntry: vi.fn(),
  };
  it('puts the nsfw badge right after the title and total under it (not page length)', () => {
    render(<EntryPane {...base} />);
    const title = screen.getByRole('heading', { name: 'My Dict' });
    const badge = screen.getByText('NSFW');
    expect(title.nextElementSibling).toBe(badge);
    expect(title.parentElement).toBe(badge.parentElement);
    expect(screen.getByText('共 128 条')).toBeTruthy();
    expect(screen.queryByText('共 1 条')).toBeNull();
  });
  it('shows no nsfw badge for a safe group', () => {
    render(<EntryPane {...base} nsfw={false} />);
    expect(screen.queryByText('NSFW')).toBeNull();
  });
});

describe('GeneratedField', () => {
  it('uses text + icon, not only color, and links description to the control', () => {
    render(
      <GeneratedField generated pending={false}>
        {({ describedBy }) => <textarea aria-label="x" aria-describedby={describedBy} />}
      </GeneratedField>,
    );
    expect(screen.getByText(/AI 生成/)).toBeTruthy();
    const ta = screen.getByLabelText('x');
    const desc = document.getElementById(ta.getAttribute('aria-describedby')!)!;
    expect(desc.textContent).toContain('AI 生成');
  });
  it('shows pending as 生成中 and neutral state renders nothing extra', () => {
    const { rerender } = render(<GeneratedField generated={false} pending><textarea aria-label="y" /></GeneratedField>);
    expect(screen.getByText(/生成中/)).toBeTruthy();
    rerender(<GeneratedField generated={false} pending={false}><textarea aria-label="y" /></GeneratedField>);
    expect(screen.queryByText(/AI 生成|生成中/)).toBeNull();
  });
});

describe('EntryBlockView views', () => {
  const props = { gref: ref, groupNsfw: false, selected: false, menuProps: {}, onSelect: vi.fn(), onPatch: vi.fn() };
  it('prompt view shows only nl, verbatim', () => {
    const e = entry({ nl: '  line1\nline2  ' });
    const { container } = render(<EntryBlockView {...props} entry={e} view="prompt" />);
    expect(container.querySelector('.pg-entry__full')?.textContent).toBe('  line1\nline2  ');
    expect(container.querySelector('.pg-entry__tags')).toBeNull();
    expect(container.querySelector('.pg-entry__seglabel')).toBeNull();
  });
  it('marks a generated nl with text (single mark)', () => {
    const e = entry({ generated: { nl: true, variants: {} } });
    render(<EntryBlockView {...props} entry={e} view="prompt" />);
    expect(screen.getAllByText(/AI 生成/)).toHaveLength(1);
    expect(screen.queryByText(/生成中/)).toBeNull();
  });
  it('marks a pending nl as 生成中 and takes precedence over the generated mark', () => {
    const e = entry({
      generated: { nl: true, variants: {} },
      pending: { nl: { state: 'pending', task_id: 't', attempt: 1, edit_version: 0, input_digest: 'd' } },
    });
    render(<EntryBlockView {...props} entry={e} view="prompt" />);
    expect(screen.getAllByText(/生成中/)).toHaveLength(1);
    expect(screen.queryByText(/AI 生成/)).toBeNull();
  });
  it('does not render an old-style tag / expand_nl layout for a plain entry', () => {
    render(<EntryBlockView {...props} entry={entry()} view="prompt" />);
    expect(screen.getByText('a cat')).toBeTruthy();
    expect(screen.queryByText(/无 tag|追加/)).toBeNull();
  });
});

describe('VariantEditor', () => {
  it('empty dictionary shows 添加变体 and an add flow selects + focuses the new key editor', async () => {
    const onPatch = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<VariantEditor entry={entry()} onPatch={onPatch} />);
    expect(screen.getByRole('button', { name: '添加变体' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '添加变体' }));
    const input = screen.getByLabelText('新变体 key');
    fireEvent.change(input, { target: { value: '   ' } });
    expect((screen.getByRole('button', { name: '添加' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: '  short ' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(onPatch).toHaveBeenCalledWith({ variants: { short: '' } }));
    rerender(<VariantEditor entry={entry({ variants: { short: '' } })} onPatch={onPatch} />);
    const editor = await screen.findByLabelText(/变体内容/);
    await waitFor(() => expect(document.activeElement).toBe(editor));
    expect((editor as HTMLTextAreaElement).value).toBe('');
  });

  it('rejects duplicate keys locally without a request', () => {
    const onPatch = vi.fn();
    render(<VariantEditor entry={entry({ variants: { a: 'x' } })} onPatch={onPatch} />);
    fireEvent.click(screen.getByRole('button', { name: '添加变体' }));
    fireEvent.change(screen.getByLabelText('新变体 key'), { target: { value: 'a' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByRole('alert').textContent).toContain('已存在');
    expect(onPatch).not.toHaveBeenCalled();
  });

  it('keeps multiple lines, patches only the edited key, and clears the generated mark on first input', async () => {
    const onPatch = vi.fn().mockResolvedValue(undefined);
    const e = entry({
      variants: { a: 'one', b: 'two' },
      generated: { nl: false, variants: { a: true, b: false } },
    });
    render(<VariantEditor entry={e} onPatch={onPatch} />);
    expect(screen.getByText(/AI 生成/, { selector: '.gen-mark' })).toBeTruthy();
    const editor = screen.getByLabelText(/变体内容/) as HTMLTextAreaElement;
    // focus / blur alone does not clear
    fireEvent.focus(editor);
    fireEvent.blur(editor);
    expect(screen.getByText(/AI 生成/, { selector: '.gen-mark' })).toBeTruthy();
    fireEvent.change(editor, { target: { value: 'line1\nline2\n' } });
    expect(screen.queryByText(/AI 生成/, { selector: '.gen-mark' })).toBeNull();
    expect((screen.getByLabelText(/变体内容/) as HTMLTextAreaElement).value).toBe('line1\nline2\n');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onPatch).toHaveBeenCalledTimes(1));
    expect(onPatch).toHaveBeenCalledWith({ variants: { a: 'line1\nline2\n' } });
  });

  it('keeps the draft when saving fails', async () => {
    const onPatch = vi.fn().mockRejectedValue(new Error('boom'));
    render(<VariantEditor entry={entry({ variants: { a: 'one' } })} onPatch={onPatch} />);
    fireEvent.change(screen.getByLabelText(/变体内容/), { target: { value: 'draft' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText(/草稿已保留/);
    expect((screen.getByLabelText(/变体内容/) as HTMLTextAreaElement).value).toBe('draft');
  });

  it('switches key by keyboard and request-generation stays disabled with a planning description', () => {
    render(<VariantEditor entry={entry({ variants: { a: 'one', b: 'two' } })} onPatch={vi.fn()} />);
    const list = screen.getByRole('listbox', { name: '变体 key' });
    const [ka, kb] = within(list).getAllByRole('option');
    expect(ka.getAttribute('aria-selected')).toBe('true');
    fireEvent.keyDown(ka, { key: 'ArrowDown' });
    expect(kb.getAttribute('aria-selected')).toBe('true');
    expect((screen.getByLabelText(/变体内容/) as HTMLTextAreaElement).value).toBe('two');
    const req = screen.getByRole('button', { name: '请求生成' }) as HTMLButtonElement;
    expect(req.disabled).toBe(true);
    expect(document.getElementById(req.getAttribute('aria-describedby')!)!.textContent).toContain('规划中');
  });
});

describe('EntryDialog partial PATCH', () => {
  it('edit sends only touched fields and shows the generated mark until typed', async () => {
    vi.mocked(patchEntry).mockResolvedValue({ data: entry(), etag: '"2"' });
    const e = entry({ generated: { nl: true, variants: {} } });
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={e} onClose={onClose} onSaved={onSaved} />);
    expect(screen.getByText(/AI 生成/, { selector: '.gen-mark' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/提示词（nl）/), { target: { value: 'new nl' } });
    expect(screen.queryByText(/AI 生成/, { selector: '.gen-mark' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(patchEntry).toHaveBeenCalledTimes(1));
    expect(vi.mocked(patchEntry).mock.calls[0][2]).toEqual({ nl: 'new nl' });
  });

  it('edit without changes does not call PATCH', async () => {
    const onClose = vi.fn();
    render(<EntryDialog mode="edit" gref={ref} etag={'"1"'} entry={entry()} onClose={onClose} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(patchEntry).not.toHaveBeenCalled();
  });
});

describe('GroupSettingsDialog (F31: auto_complete 开关已隐藏)', () => {
  it('has no auto-complete toggle and patches only nsfw', async () => {
    vi.mocked(patchGroup).mockResolvedValue({ data: {} as never, etag: '"2"' });
    render(<GroupSettingsDialog gref={ref} nsfw={false} name="G" etag={'"1"'} onClose={vi.fn()} onDone={vi.fn()} />);
    expect(screen.queryByLabelText(/自动补齐缺失字段/)).toBeNull();
    expect(screen.getByText(/没有可自动补齐的基础字段/)).toBeTruthy();
    const save = screen.getByRole('button', { name: '保存' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/这是 nsfw 词典/));
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(patchGroup).toHaveBeenCalledTimes(1));
    expect(vi.mocked(patchGroup).mock.calls[0][1]).toEqual({ nsfw: true });
  });
});
