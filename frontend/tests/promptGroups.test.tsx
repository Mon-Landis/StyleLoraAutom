import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { canSelect, DEFAULT_SAFETY, HINT_BLOCKED, HINT_REVEAL, imagePolicy, isSensitiveRating } from '../src/lib/nsfw';
import { defaultScope, scopeBlocked } from '../src/lib/scope';
import { HOLD_ENTRY_DELETE_MS, HOLD_GROUP_DELETE_MS, groupDeleteHoldMs } from '../src/tokens/hold';
import { entryMenuItems, groupMenuItems } from '../src/lib/promptMenus';
import { EMPTY_FILTER, visibleGroups } from '../src/lib/promptGroupList';
import { ContextMenu } from '../src/ui/ContextMenu';
import { useSafety } from '../src/state/safety';
import type { GroupSummary, SafetySettings } from '../src/api/promptGroupTypes';

const on = (m: SafetySettings['nsfw_display_mode']): SafetySettings => ({ nsfw_enabled: true, nsfw_display_mode: m });
const off: SafetySettings = { nsfw_enabled: false, nsfw_display_mode: 'click_reveal' };

describe('imagePolicy (F27 §4 state table)', () => {
  it('S=false: always clear, no hint', () => {
    for (const s of [off, on('click_reveal'), on('hover'), on('show')]) {
      for (const focused of [false, true]) {
        expect(imagePolicy(false, focused, s, false)).toEqual({ blur: false, hint: null, can_reveal: false });
      }
    }
  });
  it('S=true, switch off: blurred, hint only on focus, no reveal button (any mode / revealed)', () => {
    for (const mode of ['click_reveal', 'hover', 'show'] as const) {
      const s = { nsfw_enabled: false, nsfw_display_mode: mode };
      expect(imagePolicy(true, false, s, true)).toEqual({ blur: true, hint: null, can_reveal: false });
      expect(imagePolicy(true, true, s, true)).toEqual({ blur: true, hint: HINT_BLOCKED, can_reveal: false });
    }
  });
  it('S=true, on, click_reveal: blurred + reveal until confirmed, then clear', () => {
    expect(imagePolicy(true, true, on('click_reveal'), false)).toEqual({ blur: true, hint: HINT_REVEAL, can_reveal: true });
    expect(imagePolicy(true, false, on('click_reveal'), false).blur).toBe(true);
    expect(imagePolicy(true, false, on('click_reveal'), true)).toEqual({ blur: false, hint: null, can_reveal: false });
  });
  it('S=true, on, hover: blurred by default, clear on focus, not affected by confirmation', () => {
    expect(imagePolicy(true, false, on('hover'), true).blur).toBe(true);
    expect(imagePolicy(true, true, on('hover'), false).blur).toBe(false);
    expect(imagePolicy(true, true, on('hover'), false).can_reveal).toBe(false);
  });
  it('S=true, on, show: clear', () => {
    expect(imagePolicy(true, false, on('show'), false)).toEqual({ blur: false, hint: null, can_reveal: false });
  });
  it('is pure: same input, same output', () => {
    expect(imagePolicy(true, true, on('hover'), false)).toEqual(imagePolicy(true, true, on('hover'), false));
  });
});

describe('sensitivity + group visibility', () => {
  it('rating questionable/explicit are sensitive; unrated is not claimed safe nor sensitive', () => {
    expect(isSensitiveRating('questionable')).toBe(true);
    expect(isSensitiveRating('explicit')).toBe(true);
    expect(isSensitiveRating('general')).toBe(false);
    expect(isSensitiveRating('sensitive')).toBe(false);
    expect(isSensitiveRating(null)).toBe(false);
    expect(isSensitiveRating(undefined)).toBe(false);
  });
  it('canSelect hides nsfw groups only when switch is off', () => {
    expect(canSelect({ nsfw: true }, DEFAULT_SAFETY)).toBe(false);
    expect(canSelect({ nsfw: false }, DEFAULT_SAFETY)).toBe(true);
    expect(canSelect({ nsfw: true }, on('show'))).toBe(true);
  });
  const g = (id: string, scope: 'global' | 'project', nsfw = false): GroupSummary =>
    ({ dictionary_id: id, name: id, kind: 'prompts', scope, nsfw, revision: 1, entry_count: 0 });
  it('list: project first, then id; nsfw groups removed when off', () => {
    const all = [g('b', 'global'), g('a', 'global', true), g('z', 'project'), g('c', 'project')];
    expect(visibleGroups(all, EMPTY_FILTER, off).map((x) => x.dictionary_id)).toEqual(['c', 'z', 'b']);
    expect(visibleGroups(all, EMPTY_FILTER, on('show')).map((x) => x.dictionary_id)).toEqual(['c', 'z', 'a', 'b']);
  });
});

describe('safety store: session reveal in memory', () => {
  beforeEach(() => useSafety.setState({ settings: DEFAULT_SAFETY, etag: null, loaded: false, revealed: false, policyVersion: 0 }));
  it('markRevealed bumps policy version once; disabling NSFW keeps confirmation', () => {
    const s = useSafety.getState();
    s.applySettings({ nsfw_enabled: true }, '"1"');
    const v1 = useSafety.getState().policyVersion;
    s.markRevealed();
    s.markRevealed();
    expect(useSafety.getState().policyVersion).toBe(v1 + 1);
    s.applySettings({ nsfw_enabled: false });
    expect(useSafety.getState().revealed).toBe(true);
    const st = useSafety.getState();
    // switch off overrides history confirmation; switch back on restores clear display
    expect(imagePolicy(true, false, st.settings, st.revealed).blur).toBe(true);
    s.applySettings({ nsfw_enabled: true });
    const st2 = useSafety.getState();
    expect(imagePolicy(true, false, st2.settings, st2.revealed).blur).toBe(false);
  });
  it('does not persist to localStorage', () => {
    useSafety.getState().markRevealed();
    expect(JSON.stringify({ ...localStorage })).not.toMatch(/reveal/i);
  });
});

describe('scope default', () => {
  it('no project -> global; project open -> project; explicit choice wins', () => {
    expect(defaultScope(null)).toBe('global');
    expect(defaultScope(undefined)).toBe('global');
    expect(defaultScope('p1')).toBe('project');
    expect(defaultScope('p1', 'global')).toBe('global');
    expect(defaultScope(null, 'project')).toBe('project');
  });
  it('project scope without project is blocked', () => {
    expect(scopeBlocked('project', null)).toBe(true);
    expect(scopeBlocked('project', 'p1')).toBe(false);
    expect(scopeBlocked('global', null)).toBe(false);
  });
});

describe('hold durations', () => {
  it('entry 0.5s, group 2s; empty group needs no hold', () => {
    expect(HOLD_ENTRY_DELETE_MS).toBe(500);
    expect(HOLD_GROUP_DELETE_MS).toBe(2000);
    expect(groupDeleteHoldMs(0)).toBeNull();
    expect(groupDeleteHoldMs(3)).toBe(2000);
  });
  it('menu items wire the durations', () => {
    const noop = () => {};
    const entry = entryMenuItems({ disabled: false, onEdit: noop, onCopyJson: noop, onToggleDisable: noop, onDelete: noop });
    expect(entry.find((i) => i.key === 'delete')?.hold?.ms).toBe(500);
    expect(entry.map((i) => i.key)).toEqual(['edit', 'copy', 'disable', 'delete']);
    expect(entryMenuItems({ disabled: true, onEdit: noop, onCopyJson: noop, onToggleDisable: noop, onDelete: noop }).find((i) => i.key === 'disable')?.label).toBe('启用');
    const full = groupMenuItems({ entryCount: 2, onEdit: noop, onExport: noop, onDelete: noop });
    expect(full.find((i) => i.key === 'delete')?.hold?.ms).toBe(2000);
    const empty = groupMenuItems({ entryCount: 0, onEdit: noop, onExport: noop, onDelete: noop });
    expect(empty.find((i) => i.key === 'delete')?.hold).toBeUndefined();
    expect(empty.find((i) => i.key === 'delete')?.onSelect).toBeTypeOf('function');
  });
});

describe('context menu long-press delete', () => {
  let now = 0;
  beforeEach(() => {
    now = 0;
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(now), 16) as unknown as number);
    vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id));
    vi.spyOn(performance, 'now').mockImplementation(() => now);
    vi.useFakeTimers();
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
  const advance = (ms: number) => {
    for (let t = 0; t < ms; t += 16) { now += 16; act(() => { vi.advanceTimersByTime(16); }); }
  };

  it('entry delete fires once after 0.5s, not before; early release cancels', () => {
    const onDelete = vi.fn();
    const noop = () => {};
    const items = entryMenuItems({ disabled: false, onEdit: noop, onCopyJson: noop, onToggleDisable: noop, onDelete });
    render(<ContextMenu x={0} y={0} label="条目菜单" items={items} onClose={noop} />);
    const btn = screen.getByText('长按删除（0.5 秒）').closest('button')!;
    fireEvent.pointerDown(btn);
    advance(300);
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.pointerUp(btn);
    advance(400);
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.pointerDown(btn);
    advance(560);
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it('non-empty group delete needs 2s', () => {
    const onDelete = vi.fn();
    const noop = () => {};
    render(<ContextMenu x={0} y={0} label="词典菜单" items={groupMenuItems({ entryCount: 5, onEdit: noop, onExport: noop, onDelete })} onClose={noop} />);
    const btn = screen.getByText('长按删除（2 秒）').closest('button')!;
    fireEvent.pointerDown(btn);
    advance(1500);
    expect(onDelete).not.toHaveBeenCalled();
    advance(600);
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it('empty group deletes on a plain click', () => {
    const onDelete = vi.fn();
    const noop = () => {};
    render(<ContextMenu x={0} y={0} label="词典菜单" items={groupMenuItems({ entryCount: 0, onEdit: noop, onExport: noop, onDelete })} onClose={noop} />);
    fireEvent.click(screen.getByRole('menuitem', { name: '删除' }));
    expect(onDelete).toHaveBeenCalledTimes(1);
  });
});
