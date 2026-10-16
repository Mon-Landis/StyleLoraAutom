import type { Entry, GroupRef, GroupSummary, RenderMode, SafetySettings, Scope } from '../api/promptGroupTypes';
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
export const FIELD_LABEL: Record<string, string> = { tag: 'tag', expand_nl: '追加自然语言', nl: '自然语言' };

export interface EntryBlock {
  /** tags_plus_nl：左 tag，右 expand_nl + nl 分段。 */
  tagText: string;
  expandNl: string;
  nl: string;
  /** full_nl：整块 nl；缺失时 missingNl=true，显示占位而不用回退文本伪装。 */
  missingNl: boolean;
  statusLabel: string;
  missingLabels: string[];
}

export function entryBlock(e: Entry, mode: RenderMode): EntryBlock {
  const state = e.completion?.state ?? 'complete';
  return {
    tagText: e.tag.join(', '),
    expandNl: e.expand_nl,
    nl: e.nl,
    missingNl: mode === 'full_nl' && !e.nl.trim(),
    statusLabel: COMPLETION_LABEL[state] ?? state,
    missingLabels: (e.completion?.missing ?? []).map((m) => FIELD_LABEL[m] ?? m),
  };
}

/** 逗号/换行分隔的 tag 文本 -> 数组（去空白与空项）。 */
export function parseTags(text: string): string[] {
  return text.split(/[,\n，]/).map((t) => t.trim()).filter(Boolean);
}
