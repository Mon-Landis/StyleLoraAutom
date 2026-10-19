import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createConvertController, directionFrom, otherSide, warningText, type ConvertFn } from '../src/lib/promptConvert';
import type { PromptConvertResult } from '../src/api/tools';

interface Pending { dir: string; text: string; signal: AbortSignal; resolve: (r: PromptConvertResult) => void; reject: (e: unknown) => void }

function setup() {
  const calls: Pending[] = [];
  const convert: ConvertFn = (dir, text, signal) =>
    new Promise((resolve, reject) => { calls.push({ dir, text, signal, resolve, reject }); });
  const h = { onResult: vi.fn(), onCleared: vi.fn(), onError: vi.fn(), onBusy: vi.fn() };
  const ctl = createConvertController(convert, h, 300);
  return { calls, h, ctl };
}
const ok = (text: string): PromptConvertResult => ({ text, warnings: [] });
const flush = () => Promise.resolve().then(() => Promise.resolve());

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('prompt convert controller', () => {
  it('debounces: only the last edit within 300ms triggers one request', () => {
    const { calls, ctl } = setup();
    ctl.edit('nai', 'a');
    vi.advanceTimersByTime(200);
    ctl.edit('nai', 'ab');
    vi.advanceTimersByTime(299);
    expect(calls).toHaveLength(0);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ dir: 'nai_to_anima', text: 'ab' });
  });

  it('direction follows the edited side and result is written to the other side', async () => {
    const { calls, h, ctl } = setup();
    ctl.edit('anima', 'x y');
    vi.advanceTimersByTime(300);
    expect(calls[0].dir).toBe('anima_to_nai');
    calls[0].resolve(ok('x_y'));
    await flush();
    expect(h.onResult).toHaveBeenCalledWith('nai', ok('x_y'));
    expect(h.onBusy).toHaveBeenLastCalledWith(false);
  });

  it('drops a stale response when a newer edit supersedes an in-flight request', async () => {
    const { calls, h, ctl } = setup();
    ctl.edit('nai', 'one');
    vi.advanceTimersByTime(300);
    ctl.edit('nai', 'two');
    expect(calls[0].signal.aborted).toBe(true);
    vi.advanceTimersByTime(300);
    expect(calls).toHaveLength(2);
    // 新请求先返回，旧请求后返回（即使忽略 abort 也必须丢弃）
    calls[1].resolve(ok('TWO'));
    await flush();
    calls[0].resolve(ok('ONE'));
    await flush();
    expect(h.onResult).toHaveBeenCalledTimes(1);
    expect(h.onResult).toHaveBeenCalledWith('anima', ok('TWO'));
  });

  it('drops a stale error and does not report it', async () => {
    const { calls, h, ctl } = setup();
    ctl.edit('nai', 'one');
    vi.advanceTimersByTime(300);
    ctl.edit('anima', 'other');
    calls[0].reject(new DOMException('aborted', 'AbortError'));
    await flush();
    expect(h.onError).not.toHaveBeenCalled();
  });

  it('empty text clears the other side without requesting, and cancels pending work', async () => {
    const { calls, h, ctl } = setup();
    ctl.edit('nai', 'abc');
    vi.advanceTimersByTime(300);
    ctl.edit('nai', '   ');
    expect(h.onCleared).toHaveBeenCalledWith('anima');
    expect(calls[0].signal.aborted).toBe(true);
    calls[0].resolve(ok('late'));
    await flush();
    expect(h.onResult).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    expect(calls).toHaveLength(1);
  });

  it('reports a current failure via onError', async () => {
    const { calls, h, ctl } = setup();
    ctl.edit('nai', 'abc');
    vi.advanceTimersByTime(300);
    const err = new Error('boom');
    calls[0].reject(err);
    await flush();
    expect(h.onError).toHaveBeenCalledWith(err);
    expect(h.onResult).not.toHaveBeenCalled();
  });

  it('dispose cancels timers and in-flight requests', async () => {
    const { calls, h, ctl } = setup();
    ctl.edit('nai', 'a');
    ctl.dispose();
    vi.advanceTimersByTime(1000);
    expect(calls).toHaveLength(0);
    ctl.edit('nai', 'b');
    vi.advanceTimersByTime(300);
    ctl.dispose();
    expect(calls[0].signal.aborted).toBe(true);
    calls[0].resolve(ok('x'));
    await flush();
    expect(h.onResult).not.toHaveBeenCalled();
  });
});

describe('prompt convert helpers', () => {
  it('maps sides and warning codes', () => {
    expect(directionFrom('nai')).toBe('nai_to_anima');
    expect(directionFrom('anima')).toBe('anima_to_nai');
    expect(otherSide('nai')).toBe('anima');
    expect(warningText('W_WEIGHT_CLAMPED')).toBe('权重已限制变化（>3.2 截断）');
    expect(warningText('W_SOMETHING_NEW')).toBe('W_SOMETHING_NEW');
  });
});
