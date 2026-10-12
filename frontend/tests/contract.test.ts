import { afterEach, describe, expect, it, vi } from 'vitest';
import { listOperationLog, putManual } from '../src/api/endpoints';
import { getExport, listJobs, previewExport, runExport } from '../src/api/more';
import { setToken } from '../src/api/client';

function replies(...values: unknown[]) {
  const fetch = vi.fn();
  for (const value of values) fetch.mockResolvedValueOnce(new Response(JSON.stringify(value), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  setToken('test-only');
  return fetch;
}
afterEach(() => { vi.unstubAllGlobals(); setToken(null); });

describe('frontend wire contracts', () => {
  it('refreshes annotation refs/version after ManualResult instead of caching the write receipt', async () => {
    const view = { image_id: 1, tags: [], natural_language: null, layers: {}, refs: [], version: 'old' };
    const fresh = { ...view, tags: [{ tag: 'cat' }], version: 'new' };
    const fetch = replies({ annotation_ids: [10], changed: true }, fresh);
    expect(await putManual('p', 1, view, [{ tag: 'cat' }], null)).toEqual(fresh);
    expect(fetch.mock.calls[0][0]).toBe('/api/projects/p/annotations/1/manual');
    expect(fetch.mock.calls[0][1].headers['If-Match']).toBe('"old"');
    expect(fetch.mock.calls[1][0]).toBe('/api/projects/p/annotations/1');
    expect(fetch.mock.calls[1][1].method).toBe('GET');
  });

  it('keeps export preview operations and export resources separate and sends the scheme', async () => {
    const operation = { operation_id: 'preview-op', status: 'pending' };
    const preview = { preview_id: 'preview', digest: 'digest', selected: 1, issues: [] };
    const exported = { export_id: 'export', status: 'planned' };
    const fetch = replies(operation, exported, { ...exported, status: 'finished' });
    expect(await previewExport('p', 3)).toEqual(operation);
    expect(await runExport('p', 3, preview)).toEqual(exported);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ scheme_id: 3, preview_id: 'preview', digest: 'digest' });
    expect((await getExport('p', 'export')).status).toBe('finished');
    expect(fetch.mock.calls[2][0]).toBe('/api/projects/p/exports/export');
  });

  it('requests newest operation log first and preserves op_id', async () => {
    const fetch = replies({ items: [{ op_id: 7, status: 'applied' }], next_cursor: null, has_more: false, limit: 20 });
    expect((await listOperationLog('p')).items[0].op_id).toBe(7);
    expect(fetch.mock.calls[0][0]).toBe('/api/projects/p/operation-log?limit=20&sort=-id');
  });

  it('loads job details for progress omitted by list responses', async () => {
    const fetch = replies({ items: [{ job_id: 4 }], next_cursor: null, has_more: false, limit: 50 },
      { job_id: 4, progress_json: { done: 2, total: 3 } });
    expect((await listJobs('p')).items[0].progress_json?.done).toBe(2);
    expect(fetch.mock.calls[1][0]).toBe('/api/projects/p/jobs/4');
  });

  it('does not refresh over a manual CAS failure', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'concurrency:version_conflict' } }), { status: 409 }));
    vi.stubGlobal('fetch', fetch);
    await expect(putManual('p', 1, { image_id: 1, tags: [], natural_language: null, layers: {}, refs: [], version: 'old' }, [], null)).rejects.toMatchObject({ status: 409 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
