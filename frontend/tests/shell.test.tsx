import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Shell } from '../src/shell/Shell';
import { UI_STORAGE_KEY, useUi } from '../src/state/ui';
import { deriveSaveStatus } from '../src/state/saveStatus';
import { modeLabel } from '../src/lib/projectMode';

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/workbench']}>
        <Shell><div>content</div></Shell>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  useUi.setState({ sidebarCollapsed: false });
  // 外壳内的查询都给空结果，避免真实网络
  vi.stubGlobal('fetch', vi.fn(async (url: string) =>
    new Response(JSON.stringify(String(url).endsWith('/current') ? null : { items: [], next_cursor: null, has_more: false, limit: 50 }), { status: 200 })));
});
afterEach(() => vi.unstubAllGlobals());

describe('sidebar collapse linkage', () => {
  it('collapsing hides brand name + labels, syncs data-nav, keeps aria-label/title on icons', () => {
    const { container } = mount();
    const shell = container.querySelector('.shell')!;
    expect(shell.getAttribute('data-nav')).toBe('expanded');
    expect(screen.getByText('StyleLora').hasAttribute('hidden')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: '收起侧边栏' }));

    expect(shell.getAttribute('data-nav')).toBe('collapsed');
    expect(screen.getByText('StyleLora').hasAttribute('hidden')).toBe(true);
    const link = screen.getByRole('link', { name: '数据集工作台' });
    expect(link.getAttribute('title')).toBe('数据集工作台');
    expect(screen.getByRole('button', { name: '展开侧边栏' }).getAttribute('aria-expanded')).toBe('false');
    // 底部三个固定按钮仍在
    expect(screen.getByRole('link', { name: '设置' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '项目信息' })).toBeTruthy();
  });

  it('persists only the collapsed preference to localStorage', () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: '收起侧边栏' }));
    const saved = JSON.parse(localStorage.getItem(UI_STORAGE_KEY)!);
    expect(saved.state).toEqual({ sidebarCollapsed: true });
    expect(JSON.stringify(saved)).not.toMatch(/token/i);
  });
});

describe('topbar derived text', () => {
  it('derives save status from project / connection / pending writes', () => {
    expect(deriveSaveStatus({ hasProject: false, conn: 'online', pendingWrites: 0 }).label).toBe('未打开项目');
    expect(deriveSaveStatus({ hasProject: true, conn: 'online', pendingWrites: 2 }).label).toBe('保存中');
    expect(deriveSaveStatus({ hasProject: true, conn: 'offline', pendingWrites: 0 }).key).toBe('offline');
    expect(deriveSaveStatus({ hasProject: true, conn: 'degraded', pendingWrites: 0 }).label).toBe('已保存');
  });

  it('maps project mode to label, defaulting to style', () => {
    expect(modeLabel(undefined)).toBe('画风模式');
    expect(modeLabel('character')).toBe('角色模式');
    expect(modeLabel('action')).toBe('动作模式');
    expect(modeLabel('object')).toBe('物体模式');
  });
});
