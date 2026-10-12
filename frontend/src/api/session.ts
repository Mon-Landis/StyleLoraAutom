import { get, post, setToken } from './client';
import { ApiError } from './errors';
import { useConnection } from '../state/connection';

const BOOT = /[#&]bootstrap=([^&]+)/;

/** 从 URL hash 取一次性 bootstrap 码并立即从地址栏抹掉（码仅 60s 有效且一次性）。 */
export function takeBootstrapCode(): string | null {
  const m = BOOT.exec(window.location.hash);
  if (!m) return null;
  const rest = window.location.hash.replace(BOOT, '').replace(/^#&/, '#');
  window.history.replaceState(null, '', window.location.pathname + window.location.search + (rest === '#' ? '' : rest));
  return decodeURIComponent(m[1]);
}

export interface BootResult {
  instanceId: string;
}

export async function redeem(code: string): Promise<BootResult> {
  const { data } = await post<{ token: string; instance_id: string }>('/api/session/bootstrap', { code }, { anonymous: true });
  setToken(data.token);
  useConnection.getState().dispatch('rest_ok');
  return { instanceId: data.instance_id };
}

/** 启动：有码则兑换；无码（刷新页面）token 已丢失 -> reauth，提示从 CLI 重新打开。 */
export async function boot(): Promise<'ok' | 'need_code' | 'error'> {
  const code = takeBootstrapCode();
  if (!code) return 'need_code';
  try {
    await redeem(code);
    await get('/api/session');
    return 'ok';
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) useConnection.getState().dispatch('unauthorized');
    return 'error';
  }
}
