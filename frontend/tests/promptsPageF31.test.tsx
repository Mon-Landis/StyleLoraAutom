import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import { PromptsPage } from '../src/pages/PromptsPage';
import { downloadBlob } from '../src/lib/download';

vi.mock('../src/lib/download', async (orig) => ({
  ...(await orig<typeof import('../src/lib/download')>()),
  downloadBlob: vi.fn(),
}));

interface Call { method: string; url: string; body: unknown }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const group = (id: string, name: string) =>
  ({ dictionary_id: id, name, kind: 'prompts', scope: 'global', nsfw: false, revision: 1, entry_count: 2, project_token: null });
const GROUPS = [group('g1', '风格A'), group('g2', '风格B')];

let exportResponse: () => Response;

function installFetch() {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    calls.push({ method: init?.method ?? 'GET', url: u, body: init?.body });
    if (u.includes('/export')) return exportResponse();
    if (u.startsWith('/api/prompt-imports/preview')) return json({ preview_id: 'p1', digest: 'd', base_revision: 0, valid_count: 1, skipped_count: 0, issues: [], warnings: [] });
    if (u.includes('/entries')) return json({ items: [], next_cursor: null });
    if (u.startsWith('/api/prompt-groups?')) return json({ items: GROUPS, next_cursor: null });
    const id = /\/api\/prompt-groups\/([^/?]+)/.exec(u)?.[1] ?? 'g1';
    const g = GROUPS.find((x) => x.dictionary_id === id) ?? GROUPS[0];
    return new Response(JSON.stringify({ ...g, revision: 1 }), { status: 200, headers: { ETag: '"1"' } });
  }));
  return calls;
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><PromptsPage /></QueryClientProvider>);
}

const selectGroup = async (name: string) => {
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(name) }));
  await waitFor(() => expect((screen.getByRole('button', { name: '导出词典' }) as HTMLButtonElement).disabled).toBe(false));
};
const isExport = (c: Call) => c.url.includes('/export');

beforeEach(() => {
  setToken('tok');
  exportResponse = () => new Response('{}', { status: 200 });
  vi.mocked(downloadBlob).mockClear();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('F31 T17 左栏「导入词典」入口', () => {
  it('opens the import dialog fixed to level=group (no level choice) without selecting a group', async () => {
    const calls = installFetch();
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '导入词典' }));
    const dlg = screen.getByRole('dialog', { name: '导入词典' });
    expect(within(dlg).queryByLabelText('导入方式')).toBeNull();

    const file = new File(['{"prompts":[]}'], 'a.json', { type: 'application/json' });
    fireEvent.change(within(dlg).getByLabelText(/选择文件/), { target: { files: [file] } });
    fireEvent.click(within(dlg).getByRole('button', { name: '预览' }));
    await waitFor(() => expect(calls.some((c) => c.url === '/api/prompt-imports/preview')).toBe(true));
    const fd = calls.find((c) => c.url === '/api/prompt-imports/preview')!.body as FormData;
    expect(JSON.parse(String(fd.get('options')))).toMatchObject({ format: 'json', level: 'group' });
    expect(JSON.parse(String(fd.get('options'))).ref).toBeUndefined();
  });

  it('keeps the existing batch import entry (kind=import) in the right pane', async () => {
    installFetch();
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '批量导入' }));
    expect(screen.getByRole('dialog', { name: '批量导入' })).toBeTruthy();
  });
});

describe('F31 T17 导出格式选择', () => {
  it('JSON: requests format=json and downloads a .json file', async () => {
    const calls = installFetch();
    mount();
    await selectGroup('风格A');
    fireEvent.click(screen.getByRole('button', { name: '导出词典' }));
    const dlg = screen.getByRole('dialog', { name: '导出词典' });
    expect((within(dlg).getByRole('radio', { name: /JSON/ }) as HTMLInputElement).checked).toBe(true);
    expect(calls.some(isExport)).toBe(false); // 选择前不请求
    fireEvent.click(within(dlg).getByRole('button', { name: '导出' }));
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledTimes(1));
    expect(calls.filter(isExport).map((c) => c.url)).toEqual(['/api/prompt-groups/g1/export?scope=global&format=json']);
    const [blob, name] = vi.mocked(downloadBlob).mock.calls[0];
    expect(blob).toBeInstanceOf(Blob);
    expect(name).toBe('风格A.json');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '导出词典' })).toBeNull());
  });

  it('image ZIP: requests format=nai_image_zip and downloads a .zip file', async () => {
    const calls = installFetch();
    exportResponse = () => new Response('zipbytes', { status: 200 });
    mount();
    await selectGroup('风格A');
    fireEvent.click(screen.getByRole('button', { name: '导出词典' }));
    const dlg = screen.getByRole('dialog', { name: '导出词典' });
    fireEvent.click(within(dlg).getByRole('radio', { name: /图像（ZIP）/ }));
    fireEvent.click(within(dlg).getByRole('button', { name: '导出' }));
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledTimes(1));
    expect(calls.filter(isExport).map((c) => c.url)).toEqual(['/api/prompt-groups/g1/export?scope=global&format=nai_image_zip']);
    expect(vi.mocked(downloadBlob).mock.calls[0][1]).toBe('风格A.zip');
  });

  it('failure shows the error, keeps the dialog and the chosen format, and does not download', async () => {
    const calls = installFetch();
    exportResponse = () => json({ error: { code: 'import:empty', message: 'empty' } }, 422);
    mount();
    await selectGroup('风格A');
    fireEvent.click(screen.getByRole('button', { name: '导出词典' }));
    const dlg = screen.getByRole('dialog', { name: '导出词典' });
    fireEvent.click(within(dlg).getByRole('radio', { name: /图像（ZIP）/ }));
    fireEvent.click(within(dlg).getByRole('button', { name: '导出' }));
    await within(dlg).findByText('没有有效条目可导入');
    expect(downloadBlob).not.toHaveBeenCalled();
    expect((within(dlg).getByRole('radio', { name: /图像（ZIP）/ }) as HTMLInputElement).checked).toBe(true);

    // 重试沿用所选格式
    exportResponse = () => new Response('zipbytes', { status: 200 });
    fireEvent.click(within(dlg).getByRole('button', { name: '导出' }));
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledTimes(1));
    expect(calls.filter(isExport).map((c) => c.url.split('format=')[1])).toEqual(['nai_image_zip', 'nai_image_zip']);
  });
});

describe('F31 T17 离开词典不再发布补齐任务', () => {
  it('switching groups and unmounting send no POST /api/tasks', async () => {
    const calls = installFetch();
    const { unmount } = mount();
    await selectGroup('风格A');
    fireEvent.click(screen.getByRole('option', { name: /风格B/ }));
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/api/prompt-groups/g2'))).toBe(true));
    unmount();
    window.dispatchEvent(new Event('pagehide'));
    await new Promise((r) => setTimeout(r, 20));
    expect(calls.some((c) => c.url.includes('/api/tasks'))).toBe(false);
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });
});
