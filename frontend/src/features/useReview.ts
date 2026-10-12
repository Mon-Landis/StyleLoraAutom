import { useCallback, useState } from 'react';
import { reviewImages } from '../api/endpoints';
import type { ImageItem, ReviewStatus } from '../api/types';

/** 审图动作：有批量选择时作用于选择集，否则作用于当前图。成功后就地 patch 缓存。 */
export function useReview(
  pid: string | null,
  items: ImageItem[],
  selected: Set<number>,
  activeId: number | null,
  patch: (u: ImageItem[]) => void,
) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const apply = useCallback(
    async (target: ReviewStatus) => {
      if (!pid || busy) return;
      const ids = selected.size ? selected : activeId != null ? new Set([activeId]) : new Set<number>();
      const targets = items.filter((i) => ids.has(i.image_id));
      if (!targets.length) return;
      setBusy(true);
      setError(null);
      try {
        patch(await reviewImages(pid, targets, target));
      } catch (e) {
        setError(e);
      } finally {
        setBusy(false);
      }
    },
    [pid, busy, selected, activeId, items, patch],
  );
  return { apply, busy, error, clearError: () => setError(null) };
}
