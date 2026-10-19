import { create } from 'zustand';

/**
 * 变体编辑草稿（F30）。放在模块级 store 而不是组件 state：切换视图/卸载编辑器不丢未保存文本，
 * 保存失败（412/网络）时草稿原样保留；T6 的 flushEdits 可从这里取出待保存内容。
 * 只存内存，不进 localStorage。键由 draftId() 生成。
 */
interface VariantDraftStore {
  drafts: Record<string, string>;
  /** 编辑起点：草稿开始时服务端该变体的值。离开 flush 用它判断服务端是否在编辑期间被他人改过。 */
  starts: Record<string, string>;
  /** start 只在该 id 还没有草稿时记录（草稿的起点不随后续输入变化）。 */
  set: (id: string, value: string, start?: string) => void;
  /** 草稿已部分保存（保存期间又有新输入）：把起点推进到刚保存的值。 */
  rebase: (id: string, start: string) => void;
  clear: (id: string) => void;
  /** 清掉某个命名空间（通常是离开的组 refKey）下的全部草稿。 */
  clearScope: (scope: string) => void;
  reset: () => void;
}

const SEP = '\u0001';
export const draftId = (scope: string, entryId: string, key: string): string => `${scope}${SEP}${entryId}${SEP}${key}`;
export const parseDraftId = (id: string): { scope: string; entryId: string; key: string } | null => {
  const [scope, entryId, ...rest] = id.split(SEP);
  return rest.length ? { scope, entryId, key: rest.join(SEP) } : null;
};

const without = (o: Record<string, string>, id: string): Record<string, string> => {
  const { [id]: _drop, ...rest } = o;
  return rest;
};

export const useVariantDrafts = create<VariantDraftStore>((set) => ({
  drafts: {},
  starts: {},
  set: (id, value, start) => set((s) => ({
    drafts: { ...s.drafts, [id]: value },
    // 起点只在草稿首次出现时记录；之后的输入不改它。
    starts: !(id in s.drafts) && start !== undefined ? { ...s.starts, [id]: start } : s.starts,
  })),
  rebase: (id, start) => set((s) => (id in s.drafts ? { starts: { ...s.starts, [id]: start } } : s)),
  clear: (id) => set((s) => {
    if (!(id in s.drafts) && !(id in s.starts)) return s;
    return { drafts: without(s.drafts, id), starts: without(s.starts, id) };
  }),
  clearScope: (scope) => set((s) => {
    const inScope = (id: string) => parseDraftId(id)?.scope === scope;
    const keep = Object.entries(s.drafts).filter(([id]) => !inScope(id));
    if (keep.length === Object.keys(s.drafts).length) return s;
    return {
      drafts: Object.fromEntries(keep),
      starts: Object.fromEntries(Object.entries(s.starts).filter(([id]) => !inScope(id))),
    };
  }),
  reset: () => set({ drafts: {}, starts: {} }),
}));
