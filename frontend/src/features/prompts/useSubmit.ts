import { useCallback, useRef, useState } from 'react';
import { uuid } from '../../api/client';
import { ApiError } from '../../api/errors';
import { describePgError, isDefinitive } from '../../lib/promptErrors';

export interface SubmitOptions {
  /**
   * 收到 412（版本过期）时调用：调用方在这里重读最新版本（ETag）并对账。
   * 返回的文案替代默认错误提示；抛错则在默认提示后附上重读失败原因。表单与已输入内容不受影响。
   */
  onConflict?: (e: ApiError) => Promise<string | void> | string | void;
}

/**
 * 弹窗提交：提交中禁用避免连点；幂等键在「结果未知」（网络错误）时保留，
 * 服务端给出确定结果（4xx）后更换，避免改载荷重试撞 request_key_conflict。
 */
export function useSubmit<T>(run: (key: string) => Promise<T>, onDone?: (r: T) => void, opts: SubmitOptions = {}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = useRef(uuid());
  const latest = useRef({ run, onDone, onConflict: opts.onConflict });
  latest.current = { run, onDone, onConflict: opts.onConflict };
  const busyRef = useRef(false);

  const submit = useCallback(async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const r = await latest.current.run(key.current);
      key.current = uuid();
      latest.current.onDone?.(r);
    } catch (e) {
      if (isDefinitive(e)) key.current = uuid();
      let text = describePgError(e).text;
      if (e instanceof ApiError && e.status === 412 && latest.current.onConflict) {
        try {
          const m = await latest.current.onConflict(e);
          if (m) text = m;
        } catch (re) {
          text = `${text}；重新读取最新版本失败：${describePgError(re).text}`;
        }
      }
      setError(text);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  return { busy, error, submit };
}
