import { exportEntry, refKey, type Versioned } from '../api/promptGroups';
import type { Entry, EntryPatch, GroupRef } from '../api/promptGroupTypes';
import { runGroupWrite } from './groupWriteQueue';
import { overwrittenFields, type Overwritten } from './promptVariants';

/**
 * 基础字段（nl / aspect / disable）草稿的注册表。
 * 条目弹窗的表单 state 不在 store 里，所以由弹窗注册「是否有未保存内容」与「保存」回调，
 * 离开协调器与 beforeunload 通过这里统一判断/保存，与变体草稿一起处理。
 */
export interface BaseFlushContext {
  /** 基础字段 PATCH 成功后调用（按原 ref 更新缓存）。 */
  onSaved?: (ref: GroupRef, entryId: string, saved: Versioned<Entry>) => void;
  /** 遇到 412 后调用（按原 ref 失效缓存）。 */
  onConflict?: (ref: GroupRef) => void;
}

export interface BaseDraftSource {
  isDirty: () => boolean;
  /** 保存表单里的未保存内容；失败抛错（阻止离开，表单与 touched 保留）。 */
  flush: (ctx: BaseFlushContext) => Promise<void>;
}

const sources = new Map<string, Set<BaseDraftSource>>();

export function registerBaseDraftSource(ref: GroupRef, src: BaseDraftSource): () => void {
  const k = refKey(ref);
  const set = sources.get(k) ?? new Set<BaseDraftSource>();
  set.add(src);
  sources.set(k, set);
  return () => {
    const cur = sources.get(k);
    if (!cur) return;
    cur.delete(src);
    if (!cur.size) sources.delete(k);
  };
}

export const hasBaseDrafts = (ref: GroupRef): boolean =>
  [...(sources.get(refKey(ref)) ?? [])].some((s) => s.isDirty());

/** 任意组是否有未保存的基础字段（beforeunload 用）。 */
export const hasAnyBaseDrafts = (): boolean => {
  for (const set of sources.values()) for (const s of set) if (s.isDirty()) return true;
  return false;
};

/**
 * 依次保存该组全部有未保存内容的来源，经同组写入队列串行（与弹窗显式保存、变体 flush 不并发）。
 * 来源在调用时同步收集：页面卸载时弹窗随后注销，也不影响这次保存。
 * 排到队时再检查一次 isDirty：排队期间弹窗已保存则跳过，不用旧快照重复提交。
 */
export async function flushBaseDrafts(ref: GroupRef, ctx: BaseFlushContext = {}): Promise<void> {
  const dirty = [...(sources.get(refKey(ref)) ?? [])].filter((s) => s.isDirty());
  for (const s of dirty) {
    await runGroupWrite(refKey(ref), async () => {
      if (s.isDirty()) await s.flush(ctx);
    });
  }
}

/**
 * 412 后读取服务端条目，列出「保存会覆盖他人改动」的字段
 * （服务端值既不同于编辑起点、也不同于草稿值）。读取失败向上抛。
 */
export async function readOverwritten(ref: GroupRef, base: Entry, patch: EntryPatch): Promise<Overwritten[]> {
  const server = await exportEntry(ref, base.entry_id);
  return overwrittenFields(base, patch, server as Partial<Entry>);
}
