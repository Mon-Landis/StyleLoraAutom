import { describe, expect, it } from 'vitest';
import { transition } from '../src/state/connection';

describe('connection state machine', () => {
  it('booting -> degraded on rest_ok -> online on sse_up', () => {
    let s = transition('booting', 'rest_ok');
    expect(s).toBe('degraded');
    s = transition(s, 'sse_up');
    expect(s).toBe('online');
  });

  it('online -> degraded on sse_down; rest_fail -> offline; recovers via rest_ok', () => {
    expect(transition('online', 'sse_down')).toBe('degraded');
    expect(transition('degraded', 'rest_fail')).toBe('offline');
    expect(transition('offline', 'rest_ok')).toBe('degraded');
  });

  it('unauthorized sticks until reset', () => {
    const s = transition('online', 'unauthorized');
    expect(s).toBe('reauth');
    expect(transition(s, 'rest_ok')).toBe('reauth');
    expect(transition(s, 'sse_up')).toBe('reauth');
    expect(transition(s, 'reset')).toBe('booting');
  });

  it('draining sticks except unauthorized/reset', () => {
    const s = transition('online', 'draining');
    expect(transition(s, 'rest_ok')).toBe('draining');
    expect(transition(s, 'reset')).toBe('booting');
  });
});
