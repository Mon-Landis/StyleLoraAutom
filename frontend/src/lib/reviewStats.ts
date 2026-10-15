export interface ReviewCounts {
  total: number;
  unreviewed: number;
  keep: number;
  reject: number;
}

/** 解析 GET /images/stats：{ total, groups:[{ by:'review_status', buckets:[{ key, count }] }] }。 */
export function reviewCounts(raw: Record<string, unknown> | undefined | null): ReviewCounts | null {
  if (!raw || typeof raw.total !== 'number') return null;
  const groups = Array.isArray(raw.groups) ? (raw.groups as Array<{ by?: string; buckets?: Array<{ key: unknown; count: number }> }>) : [];
  const buckets = groups.find((g) => g.by === 'review_status')?.buckets ?? [];
  const n = (key: string) => buckets.find((b) => b.key === key)?.count ?? 0;
  return { total: raw.total, unreviewed: n('unreviewed'), keep: n('keep'), reject: n('reject') };
}
