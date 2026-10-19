import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { EndpointParallelSettings } from '../src/features/EndpointParallelSettings';

afterEach(() => vi.unstubAllGlobals());

it('associates the label, rejects invalid limits and saves unlimited with the endpoint version', async () => {
  const endpoint = {
    endpoint_id: 'endpoint-a', base_url: 'http://localhost:9999', protocol: 'openai_chat',
    models: [{ name: 'model', supports_images: false, capability_source: 'manual' }],
    token_refs: [{ token_ref: 'token-a', has_secret: true }], supports_json_schema: false,
    max_parallel: 4, version: 'v1',
  };
  const fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => new Response(JSON.stringify(
    init?.method === 'PUT' ? { ...endpoint, max_parallel: -1 } : { items: [endpoint], next_cursor: null },
  ), { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}><EndpointParallelSettings /></QueryClientProvider>);
  const input = await screen.findByLabelText('最大并行') as HTMLInputElement;
  expect(input.value).toBe('4');
  expect(screen.getByText(/-1 为不限制/)).toBeTruthy();
  for (const value of ['0', '-2', '1.5', '']) {
    fireEvent.change(input, { target: { value } });
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  }
  fireEvent.change(input, { target: { value: '-1' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
  const [url, init] = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
  expect(String(url)).toContain('/api/llm/endpoints/endpoint-a');
  expect(new Headers(init?.headers).get('If-Match')).toBe('"v1"');
  expect(JSON.parse(init?.body as string)).toEqual({
    base_url: endpoint.base_url, protocol: endpoint.protocol,
    models: [{ name: 'model', supports_images: false }], token_refs: ['token-a'],
    supports_json_schema: false, max_parallel: -1,
  });
});
