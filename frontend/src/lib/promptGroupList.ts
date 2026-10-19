import type { Entry, GroupRef, GroupSummary, PromptView, SafetySettings, Scope } from '../api/promptGroupTypes';
import { canSelect } from './nsfw';

export interface GroupFilter {
  q: string;
  scope: Scope | 'all';
  nsfwOnly: boolean;
}
export const EMPTY_FILTER: GroupFilter = { q: '', scope: 'all', nsfwOnly: false };

/**
 * 左栏列表：前端过滤 + 排序。关闭 NSFW 时敏感组整组隐藏，「仅 nsfw」筛选也不生效；
 * 项目级排前，同 scope 按 dictionary_id（与后端排序一致）。
 */
export function visibleGroups(groups: GroupSummary[], f: GroupFilter, safety: SafetySettings): GroupSummary[] {
  const q = f.q.trim().toLowerCase();
  return groups
    .filter((g) => canSelect(g, safety))
    .filter((g) => f.scope === 'all' || g.scope === f.scope)
    .filter((g) => !(f.nsfwOnly && safety.nsfw_enabled) || g.nsfw)
    .filter((g) => !q || g.name.toLowerCase().includes(q) || g.dictionary_id.toLowerCase().includes(q))
    .sort((a, b) => (a.scope === b.scope ? a.dictionary_id.localeCompare(b.dictionary_id) : a.scope === 'project' ? -1 : 1));
}

export function refOf(g: Pick<GroupSummary, 'scope' | 'dictionary_id' | 'project_token'>, projectId: string | null): GroupRef {
  return { scope: g.scope, project_token: g.scope === 'project' ? (g.project_token ?? projectId) : null, group_id: g.dictionary_id };
}

export const sameRef = (a: GroupRef | null, b: GroupRef | null): boolean =>
  !!a && !!b && a.scope === b.scope && a.group_id === b.group_id && (a.project_token ?? null) === (b.project_token ?? null);

export const COMPLETION_LABEL: Record<string, string> = {
  pending: '待补全', queued: '排队中', running: '补全中', complete: '', failed: '补全失败，可重试',
  paused: '补全已暂停', canceled: '补全已取消', stale: '补全结果已过期',
};
export const FIELD_LABEL: Record<string, string> = { nl: '提示词' };

export interface EntryBlock {
  /** v5：条目只显示 nl 原文本（不 trim、不改写）。 */
  nl: string;
  statusLabel: string;
  missingLabels: string[];
}

export function entryBlock(e: Entry): EntryBlock {
  const state = e.completion?.state ?? 'complete';
  return {
    nl: e.nl ?? '',
    statusLabel: COMPLETION_LABEL[state] ?? state,
    missingLabels: (e.completion?.missing ?? []).map((m) => FIELD_LABEL[m] ?? m),
  };
}

/** 本地持久化的旧视图值（default / nl）统一映射为 prompt；未知值回退 prompt。 */
export function normalizeView(v: unknown): PromptView {
  return v === 'variants' ? 'variants' : 'prompt';
}
/** 逗号/换行分隔的 tag 文本 -> 数组（去空白与空项）。 */
export function parseTags(text: string): string[] {
  return text.split(/[,\n，]/).map((t) => t.trim()).filter(Boolean);
}
