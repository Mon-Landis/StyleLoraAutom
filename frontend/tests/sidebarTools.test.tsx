import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Shell } from '../src/shell/Shell';
import { workbenchFor } from '../src/shell/workbenches';
import { useUi } from '../src/state/ui';

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Shell><div>content</div></Shell>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  useUi.setState({ sidebarCollapsed: false });
  vi.stubGlobal('fetch', vi.fn(async (url: string) =>
    new Response(JSON.stringify(String(url).endsWith('/current') ? null : { items: [], next_cursor: null, has_more: false, limit: 50 }), { status: 200 })));
});
afterEach(() => vi.unstubAllGlobals());

describe('sidebar tools menu', () => {
  it('is collapsed by default on a workbench route and toggles with aria-expanded', () => {
    mount('/workbench');
    const btn = screen.getByRole('button', { name: '工具箱' });
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('link', { name: '提示词转换' })).toBeNull();
    fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: '提示词转换' }).getAttribute('href')).toBe('/tools/prompt-convert');
    fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('false');
  });

  it('is highlighted and expanded when the route is under tools; topbar has no stats', () => {
    mount('/tools/prompt-convert');
    const btn = screen.getByRole('button', { name: '工具箱' });
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.className).toContain('active');
    expect(screen.getByRole('link', { name: '提示词转换' })).toBeTruthy();
    expect(workbenchFor('/tools/prompt-convert')).toBeUndefined();
  });

  it('in collapsed sidebar, clicking tools expands the sidebar and opens the submenu', () => {
    useUi.setState({ sidebarCollapsed: true });
    mount('/workbench');
    fireEvent.click(screen.getByRole('button', { name: '工具箱' }));
    expect(useUi.getState().sidebarCollapsed).toBe(false);
    expect(screen.getByRole('button', { name: '工具箱' }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: '提示词转换' })).toBeTruthy();
  });
});
