import { describe, expect, it, vi } from 'vitest';
import { hashPath, installHistoryGuard, type GuardWindow } from '../src/lib/historyGuard';

/** 最小可用的假 window：带历史栈的 hash 路由，popstate 先派发捕获监听器，再派发冒泡监听器（路由器）。 */
function fakeWindow(stack: string[], index: number) {
  const entries = stack.map((href) => ({ href: `http://h/${href}`, state: null as unknown }));
  let i = index;
  const capture: Array<(e: Event) => void> = [];
  const bubble: Array<(e: Event) => void> = [];
  const router = vi.fn();
  bubble.push(() => router(entries[i].href.slice(entries[i].href.indexOf('#'))));
  const pop = () => {
    let stopped = false;
    const ev = { stopImmediatePropagation: () => { stopped = true; } } as unknown as Event;
    for (const l of [...capture]) { l(ev); if (stopped) return; }
    for (const l of [...bubble]) l(ev);
  };
  const win: GuardWindow = {
    location: {
      get hash() { const h = entries[i].href; return h.slice(h.indexOf('#')); },
      get href() { return entries[i].href; },
    },
    history: {
      get state() { return entries[i].state; },
      pushState: (state, _t, url) => { entries.splice(i + 1); entries.push({ href: url, state }); i++; },
      go: (d) => { i += d; pop(); },
    },
    addEventListener: (_t, l, c) => { (c ? capture : bubble).push(l); },
    removeEventListener: (_t, l, c) => {
      const arr = c ? capture : bubble;
      const k = arr.indexOf(l);
      if (k >= 0) arr.splice(k, 1);
    },
  };
  return { win, router, user: { back: () => win.history.go(-1) }, hash: () => win.location.hash, index: () => i, size: () => entries.length };
}
const settle = () => new Promise<void>((r) => setTimeout(r, 0));

describe('hashPath', () => {
  it('extracts the route path of a hash URL', () => {
    expect(hashPath('#/prompts')).toBe('/prompts');
    expect(hashPath('#/prompts?x=1#y')).toBe('/prompts');
    expect(hashPath('')).toBe('/');
    expect(hashPath('#')).toBe('/');
  });
});

describe('installHistoryGuard', () => {
  it('with drafts: restores the URL, runs the guard, then completes the back navigation on success', async () => {
    const w = fakeWindow(['#/workbench', '#/prompts'], 1);
    const guard = vi.fn(async () => true);
    const onBlocked = vi.fn();
    installHistoryGuard({ shouldGuard: () => true, guard, onBlocked, win: w.win });
    w.user.back();
    // 拦截期间：路由器没有收到导航，URL 仍是提示词页
    expect(w.router).not.toHaveBeenCalled();
    expect(w.hash()).toBe('#/prompts');
    expect(guard).toHaveBeenCalledTimes(1);
    await settle();
    expect(w.router).toHaveBeenCalledExactlyOnceWith('#/workbench');
    expect(w.hash()).toBe('#/workbench');
    expect(onBlocked).not.toHaveBeenCalled();
  });

  it('with drafts: stays on the page and notifies when the guard refuses', async () => {
    const w = fakeWindow(['#/workbench', '#/prompts'], 1);
    const onBlocked = vi.fn();
    installHistoryGuard({ shouldGuard: () => true, guard: async () => false, onBlocked, win: w.win });
    w.user.back();
    await settle();
    expect(w.router).not.toHaveBeenCalled();
    expect(w.hash()).toBe('#/prompts');
    expect(onBlocked).toHaveBeenCalledTimes(1);
  });

  it('a throwing guard counts as blocked', async () => {
    const w = fakeWindow(['#/workbench', '#/prompts'], 1);
    const onBlocked = vi.fn();
    installHistoryGuard({ shouldGuard: () => true, guard: async () => { throw new Error('boom'); }, onBlocked, win: w.win });
    w.user.back();
    await settle();
    expect(w.router).not.toHaveBeenCalled();
    expect(onBlocked).toHaveBeenCalledTimes(1);
  });

  it('without drafts: lets the navigation through untouched and does not run the guard', async () => {
    const w = fakeWindow(['#/workbench', '#/prompts'], 1);
    const guard = vi.fn(async () => true);
    installHistoryGuard({ shouldGuard: () => false, guard, onBlocked: vi.fn(), win: w.win });
    w.user.back();
    await settle();
    expect(guard).not.toHaveBeenCalled();
    expect(w.router).toHaveBeenCalledExactlyOnceWith('#/workbench');
  });

  it('repeated back presses while saving run the guard once and keep the URL', async () => {
    const w = fakeWindow(['#/a', '#/b', '#/prompts'], 2);
    let release!: (ok: boolean) => void;
    const guard = vi.fn(() => new Promise<boolean>((r) => { release = r; }));
    installHistoryGuard({ shouldGuard: () => true, guard, onBlocked: vi.fn(), win: w.win });
    w.user.back();
    w.user.back();
    expect(guard).toHaveBeenCalledTimes(1);
    expect(w.hash()).toBe('#/prompts');
    expect(w.router).not.toHaveBeenCalled();
    release(false);
    await settle();
    expect(w.hash()).toBe('#/prompts');
  });

  it('ignores history changes that stay on the same route path', async () => {
    const w = fakeWindow(['#/prompts', '#/prompts?g=2'], 1);
    const guard = vi.fn(async () => true);
    installHistoryGuard({ shouldGuard: () => true, guard, onBlocked: vi.fn(), win: w.win });
    w.user.back();
    await settle();
    expect(guard).not.toHaveBeenCalled();
    expect(w.router).toHaveBeenCalledTimes(1);
  });

  it('uninstall removes the listener', async () => {
    const w = fakeWindow(['#/workbench', '#/prompts'], 1);
    const guard = vi.fn(async () => true);
    const off = installHistoryGuard({ shouldGuard: () => true, guard, onBlocked: vi.fn(), win: w.win });
    off();
    w.user.back();
    await settle();
    expect(guard).not.toHaveBeenCalled();
    expect(w.router).toHaveBeenCalledTimes(1);
  });
});