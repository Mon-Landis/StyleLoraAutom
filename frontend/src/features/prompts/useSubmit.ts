import { useCallback, useRef, useState } from 'react';
import { uuid } from '../../api/client';
import { describePgError, isDefinitive } from '../../lib/promptErrors';

/**
 * 弹窗提交：提交中禁用避免连点；幂等键在「结果未知」（网络错误）时保留，
 * 服务端给出确定结果（4xx）后更换，避免改载荷重试撞 request_key_conflict。
 */
export function useSubmit<T>(run: (key: string) => Promise<T>, onDone?: (r: T) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = useRef(uuid());
  const latest = useRef({ run, onDone });
  latest.current = { run, onDone };
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
      setError(describePgError(e).text);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  return { busy, error, submit };
}
