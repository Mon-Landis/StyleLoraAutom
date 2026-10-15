import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import { cancelTask, fetchTasks, runningCount, type TaskItem } from '../src/api/tasks';

const page = (items: unknown[]) => ({ items, next_cursor: null, has_more: false, limit: 50 });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

/** 按 method+path 路由的 fetch 桩；未登记的路径返回 404 以暴露多余请求。 */
function stubApi(routes: Record<string, unknown | (() => Response)>) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${String(url).split('?')[0]}`;
    const hit = routes[key];
    if (hit === undefined) return json({ error: { code: 'test:unrouted', message: key } }, 404);
    return typeof hit === 'function' ? (hit as () => Response)() : json(hit);
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

beforeEach(() => setToken('test-only'));
afterEach(() => { vi.unstubAllGlobals(); setToken(null); });

describe('tasks adapter', () => {
  it('aggregates jobs / sources / prompt tasks / exports into unified active TaskItems', async () => {
    stubApi({
      'GET /api/prompt-tasks': page([
        { task_id: 'pt1', status: 'running', version: 'v1', completed: 2, total: 5 },
        { task_id: 'pt2', status: 'done', version: 'v9' },
      ]),
      'GET /api/projects/p/jobs': page([{ job_id: 4, executor: 'wd', status: 'running', version: 'j1' }]),
      'GET /api/projects/p/jobs/4': { job_id: 4, executor: 'wd', status: 'running', version: 'j1', progress_json: { done: 3, total: 10 } },
      'GET /api/projects/p/sources': page([
        { source_id: 1, type: 'local_folder', name: 'src-a', status: 'running', version: 's1', target_count: 20 },
        { source_id: 2, type: 'local_folder', name: 'draft', status: 'draft', version: 's2' },
      ]),
      'GET /api/projects/p/exports': page([
        { export_id: 'abcdef123456', status: 'running', selected: 8, exported: 3, skipped: 1, failed: 0, version: 'e1' },
        { export_id: 'old', status: 'finished', selected: 1, exported: 1, skipped: 0, failed: 0, version: 'e0' },
      ]),
    });
    const snap = await fetchTasks('p');
    expect(snap.unavailable).toEqual([]);
    // 终态与草稿被过滤
    expect(snap.items.map((t) => t.key).sort()).toEqual(['export:p:abcdef123456', 'job:p:4', 'prompt:pt1', 'source:p:1']);
    const job = snap.items.find((t) => t.kind === 'job')!;
    expect(job.progress).toEqual({ completed: 3, total: 10 });
    expect(job.cancelable).toBe(true);
    expect(snap.items.find((t) => t.kind === 'export')!.progress).toEqual({ completed: 4, total: 8 });
    expect(runningCount(snap.items)).toBe(4);
  });

  it('keeps other sources when one fails (501) and reports it as unavailable', async () => {
    stubApi({
      'GET /api/prompt-tasks': page([]),
      'GET /api/projects/p/jobs': page([]),
      'GET /api/projects/p/sources': page([{ source_id: 1, type: 't', name: 'n', status: 'paused', version: 's1' }]),
      'GET /api/projects/p/exports': () => json({ error: { code: 'unsupported' } }, 501),
    });
    const snap = await fetchTasks('p');
    expect(snap.unavailable).toEqual(['export']);
    expect(snap.items).toHaveLength(1);
    // 暂停的任务不计入「运行中」
    expect(runningCount(snap.items)).toBe(0);
  });

  it('marks items without a version token as not cancelable and refuses to cancel them', async () => {
    stubApi({
      'GET /api/prompt-tasks': page([{ task_id: 'x', status: 'running' }]),
    });
    const [t] = (await fetchTasks(null)).items;
    expect(t.cancelable).toBe(false);
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    await expect(cancelTask(t)).rejects.toThrow('task:not_cancelable');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('routes cancel to the existing per-domain endpoints with If-Match', async () => {
    const fetch = stubApi({
      'POST /api/projects/p/jobs/4/control': {},
      'POST /api/projects/p/sources/1/control': {},
      'POST /api/prompt-tasks/pt1/control': {},
      'POST /api/projects/p/exports/e1/cancel': {},
    });
    const base: Omit<TaskItem, 'kind' | 'resourceId' | 'projectId' | 'key'> = {
      title: 't', status: 'running', rawStatus: 'running', progress: { completed: 0, total: null }, version: 'ver', cancelable: true,
    };
    await cancelTask({ ...base, key: 'a', kind: 'job', resourceId: '4', projectId: 'p' });
    await cancelTask({ ...base, key: 'b', kind: 'source', resourceId: '1', projectId: 'p' });
    await cancelTask({ ...base, key: 'c', kind: 'prompt', resourceId: 'pt1', projectId: null });
    await cancelTask({ ...base, key: 'd', kind: 'export', resourceId: 'e1', projectId: 'p' });
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      '/api/projects/p/jobs/4/control',
      '/api/projects/p/sources/1/control',
      '/api/prompt-tasks/pt1/control',
      '/api/projects/p/exports/e1/cancel',
    ]);
    for (const c of fetch.mock.calls.slice(0, 3)) expect(JSON.parse(String(c[1]?.body))).toEqual({ action: 'cancel' });
    for (const c of fetch.mock.calls) expect((c[1]?.headers as Record<string, string>)['If-Match']).toBe('"ver"');
  });
});
