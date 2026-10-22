import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import { NaiMetadataPage } from '../src/pages/tools/NaiMetadataPage';
import { toolFor } from '../src/shell/tools';
import { UI_STORAGE_KEY, useUi } from '../src/state/ui';
import type { NaiMetadataResult } from '../src/api/tools';

interface Call { method: string; url: string; body: unknown }

function installFetch(handler: (c: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const c: Call = { method: init?.method ?? 'GET', url: String(url), body: init?.body };
    calls.push(c);
    return handler(c);
  }));
  return calls;
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const apiError = (status: number, code: string, details?: unknown) =>
  json({ error: { code, message: code, retryable: false, details } }, status);

const mount = () => render(<MemoryRouter><NaiMetadataPage /></MemoryRouter>);

const okResult = (over: Partial<NaiMetadataResult> = {}): NaiMetadataResult => ({
  canceled: false,
  root: 'D:\\pics',
  summary: { total: 1, written: 1, skipped: 0, failed: 0 },
  files: [
    { name: 'a.png', path: 'D:\\pics\\a.png', txt: '1girl', status: 'written', source: 'Comment', overwritten: false, chars: 5, issues: [], warnings: [] },
  ],
  ...over,
});

beforeEach(() => {
  localStorage.clear();
  setToken('tok');
  // 恢复默认选项（不依赖上个用例残留的 zustand 状态）。
  useUi.setState({ naiMetadataDropMode: 'head', naiMetadataToAnima: true });
});
afterEach(() => {
  vi.unstubAllGlobals();
  setToken(null);
});

describe('NaiMetadataPage defaults and options', () => {
  it('defaults to drop_mode=head and to_anima checked', () => {
    mount();
    expect((screen.getByRole('radio', { name: /丢弃头部/ }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('radio', { name: '不丢弃' }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('radio', { name: /丢弃全局提示词/ }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('checkbox', { name: '转换为 Anima' }) as HTMLInputElement).checked).toBe(true);
  });

  it('persists option changes to localStorage via the ui store', () => {
    mount();
    fireEvent.click(screen.getByRole('radio', { name: /丢弃全局提示词/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: '转换为 Anima' }));
    expect(useUi.getState().naiMetadataDropMode).toBe('global');
    expect(useUi.getState().naiMetadataToAnima).toBe(false);
    const raw = localStorage.getItem(UI_STORAGE_KEY);
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw!);
    expect(parsed.state.naiMetadataDropMode).toBe('global');
    expect(parsed.state.naiMetadataToAnima).toBe(false);
  });
});

describe('NaiMetadataPage run requests', () => {
  it('sends {source:"files", drop_mode, to_anima} and no paths when clicking 选择图片…', async () => {
    const calls = installFetch(() => json(okResult()));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0].url).toBe('/api/tools/nai-metadata/run');
    expect(calls[0].method).toBe('POST');
    const body = JSON.parse(String(calls[0].body));
    expect(body).toEqual({ source: 'files', drop_mode: 'head', to_anima: true });
    expect(Object.keys(body)).toEqual(['source', 'drop_mode', 'to_anima']);
  });

  it('sends source:"folder" when clicking 选择文件夹…', async () => {
    const calls = installFetch(() => json(okResult()));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择文件夹…' }));
    await waitFor(() => expect(calls.length).toBe(1));
    const body = JSON.parse(String(calls[0].body));
    expect(body.source).toBe('folder');
  });

  it('disables both buttons while a run is pending', async () => {
    let resolveFetch!: (r: Response) => void;
    installFetch(() => new Promise((resolve) => { resolveFetch = resolve; }));
    mount();
    const filesBtn = screen.getByRole('button', { name: /选择图片/ });
    const folderBtn = screen.getByRole('button', { name: '选择文件夹…' });
    fireEvent.click(filesBtn);
    await waitFor(() => expect(filesBtn.hasAttribute('disabled')).toBe(true));
    expect(folderBtn.hasAttribute('disabled')).toBe(true);
    expect(filesBtn.textContent).toBe('等待选择 / 处理中…');
    resolveFetch(json(okResult()));
    await waitFor(() => expect(filesBtn.hasAttribute('disabled')).toBe(false));
  });
});

describe('NaiMetadataPage result rendering', () => {
  it('renders summary, root, and per-file rows including overwritten and skipped reasons', async () => {
    installFetch(() => json(okResult({
      summary: { total: 3, written: 1, skipped: 1, failed: 1 },
      files: [
        { name: 'a.png', path: 'p/a.png', txt: '1girl', status: 'written', source: 'Comment', overwritten: true, chars: 5, issues: [], warnings: [] },
        { name: 'b.png', path: 'p/b.png', txt: null, status: 'skipped', source: null, overwritten: false, chars: 0, reason: 'metadata:empty', issues: [{ code: 'metadata:empty', severity: 'warning', message: 'x' }], warnings: [] },
        { name: 'c.png', path: 'p/c.png', txt: null, status: 'failed', source: null, overwritten: false, chars: 0, reason: 'storage:io_error', issues: [], warnings: [] },
      ],
    })));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText(/共 3 张/)).toBeTruthy());
    expect(screen.getByText('共 3 张：写入 1，跳过 1，失败 1')).toBeTruthy();
    expect(screen.getByText('目录：D:\\pics')).toBeTruthy();
    expect(screen.getByText('覆盖写入')).toBeTruthy();
    expect(screen.getByText('未找到正面提示词')).toBeTruthy();
    expect(screen.getByText('写入失败')).toBeTruthy();
  });

  it('shows a neutral note (not an error) when the picker is canceled', async () => {
    installFetch(() => json({ canceled: true, root: null, summary: { total: 0, written: 0, skipped: 0, failed: 0 }, files: [] }));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText('已取消选择，未处理任何文件。')).toBeTruthy());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows a readable message for 501 picker:unavailable', async () => {
    installFetch(() => apiError(501, 'picker:unavailable'));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText(/无法打开系统文件选择器|未实现/)).toBeTruthy());
  });

  it('shows a readable message for 413 import:limit', async () => {
    installFetch(() => apiError(413, 'import:limit'));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText('一次最多处理 1000 张图片')).toBeTruthy());
  });

  it('shows a readable message for 409 picker:busy', async () => {
    installFetch(() => apiError(409, 'picker:busy'));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText('已有文件选择或处理正在进行，请稍后再试')).toBeTruthy());
  });

  it('shows a readable message for 501 picker:unavailable even with details.reason', async () => {
    installFetch(() => apiError(501, 'picker:unavailable', { reason: 'native_picker_failed' }));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText(/无法打开系统文件选择器|未实现/)).toBeTruthy());
  });

  it('maps output:conflict and image:unsupported per-file codes', async () => {
    installFetch(() => json(okResult({
      summary: { total: 2, written: 0, skipped: 2, failed: 0 },
      files: [
        { name: 'a.png', path: 'p/a.png', txt: null, status: 'skipped', source: null, overwritten: false, chars: 0, reason: 'output:conflict', issues: [{ code: 'output:conflict', severity: 'warning', message: 'x' }], warnings: [] },
        { name: 'b.jpg', path: 'p/b.jpg', txt: null, status: 'skipped', source: null, overwritten: false, chars: 0, reason: 'image:unsupported', issues: [{ code: 'image:unsupported', severity: 'warning', message: 'x' }], warnings: [] },
      ],
    })));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText('输出 txt 与其它图片冲突（同名不同大小写/扩展名），未写入')).toBeTruthy());
    expect(screen.getByText('不是 PNG 图片，未处理')).toBeTruthy();
  });

  it('falls back to 处理失败（code） for unknown per-file reason/issue codes', async () => {
    installFetch(() => json(okResult({
      summary: { total: 1, written: 0, skipped: 0, failed: 1 },
      files: [
        { name: 'a.png', path: 'p/a.png', txt: null, status: 'failed', source: null, overwritten: false, chars: 0, reason: 'W_INPUT_LIMIT', issues: [], warnings: [] },
      ],
    })));
    mount();
    fireEvent.click(screen.getByRole('button', { name: '选择图片…' }));
    await waitFor(() => expect(screen.getByText('处理失败（W_INPUT_LIMIT）')).toBeTruthy());
  });
});

describe('tool registry', () => {
  it('registers the nai-metadata tool at /tools/nai-metadata', () => {
    const t = toolFor('/tools/nai-metadata');
    expect(t?.label).toBe('NAI 元数据打印');
    expect(t?.path).toBe('/tools/nai-metadata');
  });
});
