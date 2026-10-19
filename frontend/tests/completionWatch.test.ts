import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../src/api/client';
import { ApiError } from '../src/api/errors';
import { isTaskGone, watchCompletion } from '../src/lib/completionWatch';
import { useNotices } from '../src/state/notices';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const running = () => json({ task_id: 't1', status: 'running', reports: [] });
const done = () => json({ task_id: 't1', status: 'done', reports: [] });
const errBody = (status: number, code = 'x') => json({ error: { code, message: code } }, status);
const netFail = () => { throw new TypeError('Failed to fetch'); };

/** 依次返回 steps 里的响应（函数则调用，可抛错）；用完后重复最后一项。记录每次请求的（假）时间。 */
function scriptFetch(steps: Array<() => Response>) {
  const times: number[] = [];
  vi.stubGlobal('fetch', vi.fn(async () => {
    const i = Math.min(times.length, steps.length - 1);
    times.push(Date.now());
    return steps[i]();
  }));
  return times;
}
const texts = () => useNotices.getState().items.map((n) => n.text);

beforeEach(() => {
  setToken('tok');
  useNotices.getState().reset();
  vi.useFakeTimers();
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('watchCompletion backoff', () => {
  it('retries transient failures with 1s -> 2s -> 4s backoff, then settles', async () => {
    const times = scriptFetch([netFail, netFail, netFail, running, done]);
    const onSettled = vi.fn();
    const onLost = vi.fn();
    const t0 = Date.now();
    watchCompletion('t1', { label: 'x', onSettled, onLost, intervalMs: 1500 });
    await vi.advanceTimersByTimeAsync(60_000);
    const rel = times.map((t) => t - t0);
    expect(rel.slice(0, 4)).toEqual([1500, 2500, 4500, 8500]); // 间隔 1s / 2s / 4s
    expect(rel[4] - rel[3]).toBe(1500); // 成功后恢复正常间隔
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onLost).not.toHaveBeenCalled();
    expect(texts()).toEqual([]);
  });

  it('caps the backoff delay at 30s', async () => {
    const times = scriptFetch([netFail]);
    watchCompletion('t1', { label: 'x', onSettled: vi.fn(), onLost: vi.fn(), intervalMs: 1000, maxFailures: 9, maxTrackMs: 3_600_000 });
    await vi.advanceTimersByTimeAsync(600_000);
    const gaps = times.slice(1).map((t, i) => t - times[i]);
    expect(gaps).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000, 30000]);
  });

  it.each([[500], [502], [408], [429]])('treats HTTP %i as transient', async (status) => {
    scriptFetch([() => errBody(status), done]);
    const onSettled = vi.fn();
    const onLost = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, onLost, intervalMs: 100 });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onLost).not.toHaveBeenCalled();
  });

  it('stops with a notice after too many consecutive failures, and success resets the counter', async () => {
    const times = scriptFetch([netFail, netFail, running, netFail, netFail, running, netFail, netFail, done]);
    const onSettled = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, intervalMs: 100, maxFailures: 2 });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onSettled).toHaveBeenCalledTimes(1); // 每次连续 2 次失败后都有成功，不触发上限
    expect(times).toHaveLength(9);

    const onLost = vi.fn();
    scriptFetch([netFail]);
    watchCompletion('t2', { label: 'x', onSettled: vi.fn(), onLost, intervalMs: 100, maxFailures: 3 });
    await vi.advanceTimersByTimeAsync(600_000);
    expect(onLost).toHaveBeenCalledWith('unreachable');
    expect(texts().some((t) => t.includes('补全状态未知，已停止跟踪'))).toBe(true);
  });
});

describe('watchCompletion task gone', () => {
  it.each([[404], [410]])('stops on HTTP %i with a "task invalid" notice and calls onLost', async (status) => {
    const times = scriptFetch([() => errBody(status, 'task:not_found')]);
    const onSettled = vi.fn();
    const onLost = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, onLost, intervalMs: 100 });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(times).toHaveLength(1);
    expect(onSettled).not.toHaveBeenCalled();
    expect(onLost).toHaveBeenCalledExactlyOnceWith('gone');
    expect(texts()).toEqual([expect.stringContaining('任务已失效')]);
  });

  it('classifies errors', () => {
    const api = (s: number) => new ApiError(s, { code: 'x', message: 'x', retryable: false, retryAction: 'none', outcome: null, details: null, requestId: null });
    expect([404, 410, 400, 422].map((s) => isTaskGone(api(s)))).toEqual([true, true, true, true]);
    expect([401, 403, 408, 429, 500, 503].map((s) => isTaskGone(api(s)))).toEqual([false, false, false, false, false, false]);
    expect(isTaskGone(new Error('net'))).toBe(false);
  });

  it('does not re-publish: only GET requests are ever made', async () => {
    scriptFetch([() => errBody(404)]);
    watchCompletion('t1', { label: 'x', onSettled: vi.fn(), intervalMs: 100 });
    await vi.advanceTimersByTimeAsync(10_000);
    const methods = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => (c[1] as RequestInit | undefined)?.method ?? 'GET');
    expect(new Set(methods)).toEqual(new Set(['GET']));
  });
});

describe('watchCompletion tracking deadline', () => {
  it('notifies "status unknown" and calls onLost when the deadline passes while still running', async () => {
    const times = scriptFetch([running]);
    const onSettled = vi.fn();
    const onLost = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, onLost, intervalMs: 1000, maxTrackMs: 5000 });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onLost).toHaveBeenCalledExactlyOnceWith('timeout');
    expect(onSettled).not.toHaveBeenCalled();
    expect(texts()).toEqual(['补全状态未知，已停止跟踪']);
    const n = times.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(times).toHaveLength(n); // 停止后不再请求
  });

  it('the deadline also applies while backing off from errors', async () => {
    scriptFetch([netFail]);
    const onLost = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled: vi.fn(), onLost, intervalMs: 1000, maxTrackMs: 20_000, maxFailures: 50 });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(onLost).toHaveBeenCalledExactlyOnceWith('timeout');
  });

  it('cancel function stops polling silently', async () => {
    const times = scriptFetch([running]);
    const onLost = vi.fn();
    const stop = watchCompletion('t1', { label: 'x', onSettled: vi.fn(), onLost, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(2500);
    stop();
    const n = times.length;
    await vi.advanceTimersByTimeAsync(700_000);
    expect(times).toHaveLength(n);
    expect(onLost).not.toHaveBeenCalled();
    expect(texts()).toEqual([]);
  });
});

describe('N5 截止定时器独立于请求（fetch 悬挂）', () => {
  /** fetch 永不返回，只在 signal 中止时以 AbortError 拒绝。 */
  function hangingFetch() {
    const signals: AbortSignal[] = [];
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_res, rej) => {
      const s = init?.signal as AbortSignal;
      signals.push(s);
      s.addEventListener('abort', () => rej(new DOMException('Aborted', 'AbortError')));
    })));
    return signals;
  }

  it('aborts the hung request at the deadline; notice and onLost fire exactly once', async () => {
    const signals = hangingFetch();
    const onLost = vi.fn();
    const onSettled = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, onLost, intervalMs: 1000, maxTrackMs: 10_000 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(9000);
    expect(signals[0].aborted).toBe(true);
    expect(onLost).toHaveBeenCalledExactlyOnceWith('timeout');
    expect(texts()).toEqual(['补全状态未知，已停止跟踪']);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(onLost).toHaveBeenCalledTimes(1);
    expect(texts()).toHaveLength(1);
    expect(signals).toHaveLength(1); // 不再请求
    expect(onSettled).not.toHaveBeenCalled();
  });

  it('times out even when the first request is still hanging at the default 10 min', async () => {
    hangingFetch();
    const onLost = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled: vi.fn(), onLost });
    await vi.advanceTimersByTimeAsync(10 * 60_000 - 1);
    expect(onLost).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2);
    expect(onLost).toHaveBeenCalledExactlyOnceWith('timeout');
  });

  it('cancel aborts the in-flight request silently', async () => {
    const signals = hangingFetch();
    const onLost = vi.fn();
    const stop = watchCompletion('t1', { label: 'x', onSettled: vi.fn(), onLost, intervalMs: 100, maxTrackMs: 5000 });
    await vi.advanceTimersByTimeAsync(150);
    stop();
    expect(signals[0].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onLost).not.toHaveBeenCalled();
    expect(texts()).toEqual([]);
  });

  it('settling before the deadline clears the deadline timer (no late onLost)', async () => {
    scriptFetch([done]);
    const onLost = vi.fn();
    const onSettled = vi.fn();
    watchCompletion('t1', { label: 'x', onSettled, onLost, intervalMs: 100, maxTrackMs: 5000 });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onLost).not.toHaveBeenCalled();
    expect(texts()).toEqual([]);
  });
});
