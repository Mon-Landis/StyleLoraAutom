import { create } from 'zustand';

/**
 * connection 状态机（纯函数，便于测试）。
 * booting：启动中 / online：REST+SSE 均正常 / degraded：REST 可用但 SSE 断开
 * offline：REST 不可达 / reauth：token 失效需重新引导 / draining：后端正在关闭
 */
export type ConnState = 'booting' | 'online' | 'degraded' | 'offline' | 'reauth' | 'draining';
export type ConnEvent = 'rest_ok' | 'rest_fail' | 'sse_up' | 'sse_down' | 'unauthorized' | 'draining' | 'reset';

export function transition(s: ConnState, e: ConnEvent): ConnState {
  if (e === 'reset') return 'booting';
  if (e === 'unauthorized') return 'reauth';
  if (s === 'reauth') return s; // 只有 reset 能离开
  if (e === 'draining') return 'draining';
  if (s === 'draining') return s;
  switch (e) {
    case 'rest_fail':
      return 'offline';
    case 'rest_ok':
      return s === 'booting' || s === 'offline' ? 'degraded' : s;
    case 'sse_up':
      return 'online';
    case 'sse_down':
      return s === 'online' ? 'degraded' : s;
    default:
      return s;
  }
}

interface ConnStore {
  state: ConnState;
  dispatch: (e: ConnEvent) => void;
}

export const useConnection = create<ConnStore>((set, get) => ({
  state: 'booting',
  dispatch: (e) => {
    const next = transition(get().state, e);
    if (next !== get().state) set({ state: next });
  },
}));
