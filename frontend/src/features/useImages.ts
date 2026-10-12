import { useCallback, useMemo } from 'react';
import { useInfiniteQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query';
import { listImages } from '../api/endpoints';
import type { ImageFilter, ImageItem, Page } from '../api/types';

type Pages = InfiniteData<Page<ImageItem>, string | null>;

/** keyset 分页的图片列表；patch() 就地更新缓存，避免审图后整体重拉。 */
export function useImages(pid: string | null, filter: Partial<ImageFilter>, aroundId?: number) {
  const qc = useQueryClient();
  const key = useMemo(() => ['images', pid, filter, aroundId ?? null] as const, [pid, filter, aroundId]);
  const q = useInfiniteQuery({
    queryKey: key,
    enabled: !!pid,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      listImages(pid!, { filter, cursor: pageParam, around_id: pageParam ? undefined : aroundId, limit: 100 }, signal),
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
  });
  const items = useMemo(() => q.data?.pages.flatMap((p) => p.items) ?? [], [q.data]);
  const total = q.data?.pages[0]?.total;
  const patch = useCallback(
    (updated: ImageItem[]) => {
      const by = new Map(updated.map((i) => [i.image_id, i]));
      qc.setQueriesData<Pages>({ queryKey: ['images', pid] }, (old) =>
        old && { ...old, pages: old.pages.map((p) => ({ ...p, items: p.items.map((i) => by.get(i.image_id) ?? i) })) },
      );
    },
    [qc, pid],
  );
  return { ...q, items, total, patch };
}
