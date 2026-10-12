import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { holdProgress } from '../src/lib/hold';
import { HoldToConfirm } from '../src/ui/HoldToConfirm';

describe('holdProgress', () => {
  it('clamps to 0..1', () => {
    expect(holdProgress(-5, 100)).toBe(0);
    expect(holdProgress(50, 100)).toBe(0.5);
    expect(holdProgress(500, 100)).toBe(1);
    expect(holdProgress(1, 0)).toBe(1);
  });
});

describe('HoldToConfirm', () => {
  let now = 0;
  beforeEach(() => {
    now = 0;
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(now), 16) as unknown as number);
    vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id));
    vi.spyOn(performance, 'now').mockImplementation(() => now);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const advance = (ms: number) => {
    for (let t = 0; t < ms; t += 16) {
      now += 16;
      act(() => {
        vi.advanceTimersByTime(16);
      });
    }
  };

  it('fires only after the full hold duration', () => {
    const onConfirm = vi.fn();
    render(<HoldToConfirm durationMs={200} onConfirm={onConfirm}>删除</HoldToConfirm>);
    fireEvent.pointerDown(screen.getByRole('button'));
    advance(100);
    expect(onConfirm).not.toHaveBeenCalled();
    advance(200);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('cancels when released early', () => {
    const onConfirm = vi.fn();
    render(<HoldToConfirm durationMs={200} onConfirm={onConfirm}>删除</HoldToConfirm>);
    const b = screen.getByRole('button');
    fireEvent.pointerDown(b);
    advance(100);
    fireEvent.pointerUp(b);
    advance(400);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('does not start when disabled', () => {
    const onConfirm = vi.fn();
    render(<HoldToConfirm durationMs={50} disabled onConfirm={onConfirm}>删除</HoldToConfirm>);
    fireEvent.pointerDown(screen.getByRole('button'));
    advance(200);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
