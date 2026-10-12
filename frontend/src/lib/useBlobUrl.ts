import { useEffect, useState } from 'react';
import { fetchBlob } from '../api/client';

const cache = new Map<string, string>();
const MAX = 400;

/** 带鉴权 fetch -> blob URL（Q47）。简单 LRU，淘汰时 revoke。 */
export function useBlobUrl(path: string | undefined | null): { url: string | null; error: boolean } {
  const [state, set] = useState<{ url: string | null; error: boolean }>({ url: path ? cache.get(path) ?? null : null, error: false });
  useEffect(() => {
    if (!path) return set({ url: null, error: false });
    const hit = cache.get(path);
    if (hit) {
      cache.delete(path);
      cache.set(path, hit);
      return set({ url: hit, error: false });
    }
    set({ url: null, error: false });
    const ctl = new AbortController();
    fetchBlob(path, ctl.signal)
      .then((b) => {
        const u = URL.createObjectURL(b);
        cache.set(path, u);
        if (cache.size > MAX) {
          const oldest = cache.keys().next().value as string;
          URL.revokeObjectURL(cache.get(oldest)!);
          cache.delete(oldest);
        }
        set({ url: u, error: false });
      })
      .catch(() => {
        if (!ctl.signal.aborted) set({ url: null, error: true });
      });
    return () => ctl.abort();
  }, [path]);
  return state;
}

export function clearBlobCache(): void {
  cache.forEach((u) => URL.revokeObjectURL(u));
  cache.clear();
}
