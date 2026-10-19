import type { InfiniteData, QueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/errors';
import { exportEntry, getGroup, patchEntry, refKey, type EntryPage, type Versioned } from '../api/promptGroups';
import type { Entry, GroupRef } from '../api/promptGroupTypes';
import { notify } from '../state/notices';
import { parseDraftId, useVariantDrafts } from '../state/variantDrafts';
import { flushBaseDrafts, hasBaseDrafts } from './baseDrafts';
import { runGroupWrite } from './groupWriteQueue';
import { describePgError } from './promptErrors';

/**
 * 离开协调器。F31 §7.3 起离开词典不再发布任何补齐任务，只负责保存草稿并决定能否离开。
 * 放在组件之外（模块级、按 QueryClient 单例），所以卸载/StrictMode 重放不会产生重复保存：
 * 同一 ref 并发的离开共用一个 promise。
 * 顺序固定：flush 草稿（有限次，直到该组无草稿）；保存失败或仍有草稿则 blocked（草稿保留）。
 */
export type LeaveStatus = 'done' | 'blocked';
export interface LeaveOutcome {
  status: LeaveStatus;
  error?: unknown;
}

export interface LeaveDeps {
  /** 保存该组全部未保存草稿（条目弹窗基础字段 + 变体）；失败抛错（阻止离开）。 */
  flush: (ref: GroupRef) => Promise<void>;
  /** 该组是否仍有未保存草稿（保存期间继续输入的内容会留下）。缺省视为没有。 */
  hasDrafts?: (ref: GroupRef) => boolean;
  onError: (e: unknown, ref: GroupRef) => void;
}

/** flush 有限次后该组仍有草稿（用户一直在输入或保存被跳过）：阻止离开，草稿保留。 */
export class DraftsPendingError extends Error {
  constructor() { super('draft:unsaved'); this.name = 'DraftsPendingError'; }
}

const isStatus = (e: unknown, status: number): boolean => e instanceof ApiError && e.status === status;

const MAX_FLUSH_PASSES = 3;

export function createLeaveCoordinator(deps: LeaveDeps) {
  const inflight = new Map<string, Promise<LeaveOutcome>>();
  const hasDrafts = (ref: GroupRef) => deps.hasDrafts?.(ref) === true;

  function leave(ref: GroupRef): Promise<LeaveOutcome> {
    const k = refKey(ref);
    const existing = inflight.get(k);
    if (existing) return existing;
    const blocked = (e: unknown): LeaveOutcome => {
      deps.onError(e, ref);
      return { status: 'blocked', error: e };
    };
    const run = async (): Promise<LeaveOutcome> => {
      // flush -> 检查无草稿；期间有新输入则再来一轮（有限次）。
      for (let pass = 0; ; pass++) {
        if (pass >= MAX_FLUSH_PASSES) return blocked(new DraftsPendingError());
        try {
          await deps.flush(ref);
        } catch (e) {
          return blocked(e);
        }
        if (!hasDrafts(ref)) return { status: 'done' };
      }
    };
    const p = run().finally(() => { inflight.delete(k); });
    inflight.set(k, p);
    return p;
  }

  return { leave };
}
export type LeaveCoordinator = ReturnType<typeof createLeaveCoordinator>;

// ---- 草稿 flush ----

/** start：编辑起点（草稿开始时服务端该变体的值）；缺失表示无法核对，沿用 412 对账。 */
type DraftItem = { id: string; value: string; start?: string };

/** 草稿与他人在编辑期间的改动分歧：未提交，草稿保留，阻止离开。 */
export class VariantConflictError extends Error {
  constructor(readonly keys: string[]) { super('variants:server_changed'); this.name = 'VariantConflictError'; }
}

/** 取出 scope 下全部草稿（含编辑起点），按 entryId 归并。 */
function collectDrafts(scope: string): Map<string, Map<string, DraftItem>> {
  const { drafts, starts } = useVariantDrafts.getState();
  const byEntry = new Map<string, Map<string, DraftItem>>();
  for (const [id, value] of Object.entries(drafts)) {
    const p = parseDraftId(id);
    if (!p || p.scope !== scope) continue;
    const m = byEntry.get(p.entryId) ?? new Map();
    m.set(p.key, { id, value, start: starts[id] });
    byEntry.set(p.entryId, m);
  }
  return byEntry;
}

export const hasVariantDrafts = (ref: GroupRef): boolean =>
  collectDrafts(refKey(ref)).size > 0;

export interface FlushOptions {
  /** 每次草稿 PATCH 成功后调用（按原 ref 更新/失效缓存）。 */
  onSaved?: (ref: GroupRef, entryId: string, saved: Versioned<Entry>) => void;
}

const currentItems = (scope: string, entryId: string): Map<string, DraftItem> | undefined =>
  collectDrafts(scope).get(entryId);

/**
 * 只清除仍等于已保存值的草稿：保存期间继续输入的新内容保留，
 * 且其编辑起点推进到刚保存的值（服务端现在就是这个值）。
 */
function clearSaved(items: Map<string, DraftItem>): void {
  const st = useVariantDrafts.getState();
  for (const d of items.values()) {
    if (st.drafts[d.id] === d.value) st.clear(d.id);
    else if (st.drafts[d.id] !== undefined) st.rebase(d.id, d.value);
  }
}

interface Verdict {
  /** 可以提交的草稿。 */
  todo: Map<string, DraftItem>;
  /** 服务端在编辑期间被他人改过、且与草稿不同的 key：不提交。 */
  conflicts: string[];
}

/**
 * 读取服务端该条目的变体（对账用 exportEntry）。条目已不存在返回 null，并丢弃其草稿。
 */
async function readServerVariants(ref: GroupRef, scope: string, entryId: string): Promise<Record<string, string> | null> {
  try {
    return (await exportEntry(ref, entryId)).variants ?? {};
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      const gone = currentItems(scope, entryId);
      if (gone) for (const d of gone.values()) useVariantDrafts.getState().clear(d.id);
      return null;
    }
    throw e;
  }
}

/**
 * 逐 key 对账（与 EntryDialog 的 overwrittenFields 同口径）：
 * - 服务端值 == 草稿：已经一致，清草稿跳过；
 * - 有起点且服务端值既不同于起点（他人改过）又不同于草稿：冲突，不提交、草稿保留，
 *   并把起点推进到服务端当前值（再次离开即视为确认覆盖，仍会重新核对）；
 * - 其余（服务端未变、服务端无此 key、或没有起点可核对）：提交。
 */
function classify(items: Map<string, DraftItem>, server: Record<string, string>): Verdict {
  const st = useVariantDrafts.getState();
  const todo = new Map<string, DraftItem>();
  const conflicts: string[] = [];
  for (const [key, d] of items) {
    const s = server[key];
    if (s === d.value) {
      if (st.drafts[d.id] === d.value) st.clear(d.id);
    } else if (d.start !== undefined && s !== undefined && s !== d.start) {
      conflicts.push(key);
      st.rebase(d.id, s);
    } else {
      todo.set(key, d);
    }
  }
  return { todo, conflicts };
}

/**
 * 412 后的对账：重新读取服务端变体并与「当前」草稿比较，绝不重放旧快照。
 * 条目已不存在返回 null（草稿已丢弃）。
 */
async function reconcileAfter412(ref: GroupRef, scope: string, entryId: string): Promise<Verdict | null> {
  const server = await readServerVariants(ref, scope, entryId);
  if (!server) return null;
  return classify(currentItems(scope, entryId) ?? new Map<string, DraftItem>(), server);
}

async function flushNow(ref: GroupRef, scope: string, opts: FlushOptions): Promise<void> {
  const store = useVariantDrafts;
  const entryIds = [...collectDrafts(scope).keys()];
  if (!entryIds.length) return;
  const readEtag = async (): Promise<string> => {
    const etag = (await getGroup(ref)).etag;
    if (!etag) throw new Error('revision:required');
    return etag;
  };
  // 顺序固定：先取 ETag，再读服务端变体对账。两者之间的他人改动会被该 ETag 的 412 兜住。
  let etag = await readEtag();
  const conflicts: string[] = [];
  for (const entryId of entryIds) {
    // 在队列里执行时才取快照：排队期间编辑器已保存/清除/继续输入的内容以当前为准。
    const queued = currentItems(scope, entryId);
    if (!queued?.size) continue;
    const server = await readServerVariants(ref, scope, entryId);
    if (!server) continue; // 条目已删除，草稿已丢弃
    const first = classify(queued, server);
    conflicts.push(...first.conflicts);
    let items: Map<string, DraftItem> | undefined = first.todo;
    let retried = false;
    while (items?.size) {
      const body = { variants: Object.fromEntries([...items].map(([key, d]) => [key, d.value])) };
      try {
        const r = await patchEntry(ref, entryId, body, etag);
        etag = r.etag ?? etag;
        opts.onSaved?.(ref, entryId, r);
        clearSaved(items);
        break;
      } catch (e) {
        if (e instanceof ApiError && e.status === 404 && e.code === 'entry:not_found') {
          for (const d of items.values()) store.getState().clear(d.id);
          break;
        }
        if (!isStatus(e, 412) || retried) throw e;
        retried = true;
        etag = await readEtag();
        const v = await reconcileAfter412(ref, scope, entryId);
        if (v) conflicts.push(...v.conflicts);
        items = v?.todo;
      }
    }
  }
  // 其余条目已照常保存；有分歧的草稿原样保留，由调用方阻止离开并提示。
  if (conflicts.length) throw new VariantConflictError([...new Set(conflicts)]);
}

/**
 * 把该组未保存的变体草稿经 patchEntry 保存。每个条目一次合并 PATCH，沿用上一次响应的 ETag。
 * - 与编辑器保存共用同组写入队列（runGroupWrite），不并发；
 * - 只清除与已保存值相同的草稿，保存期间的新输入保留（由调用方再次检查并继续保存或阻止离开）；
 * - 提交前先读服务端变体，与草稿编辑起点逐 key 核对：同字段分歧（服务端 != 起点 且 != 草稿）不提交、
 *   草稿保留并抛 VariantConflictError；服务端 == 草稿则清草稿跳过；
 * - 412 后重读 ETag 并用同样口径对账，不自动用旧快照覆盖；只重试一次；
 * - 条目已被删除则丢弃其草稿；其余失败原样抛出，草稿保留，由调用方阻止离开。
 */
export function flushVariantDrafts(ref: GroupRef, opts: FlushOptions = {}): Promise<void> {
  const scope = refKey(ref);
  return runGroupWrite(scope, () => flushNow(ref, scope, opts));
}

// ---- 默认接线 ----

/** 该组是否有任何未保存草稿：变体草稿（store）或条目弹窗里的基础字段。 */
export const hasGroupDrafts = (ref: GroupRef): boolean => hasVariantDrafts(ref) || hasBaseDrafts(ref);

/**
 * 离开前保存：先弹窗里的基础字段（经 runGroupWrite，成功后更新缓存），再变体草稿。
 * 基础字段保存失败直接抛错：不继续、阻止离开，表单与 touched 保留。
 * flushBaseDrafts 在同步阶段收集来源，页面卸载兜底调用时弹窗随后注销也不受影响。
 */
export async function flushGroupDrafts(qc: QueryClient, ref: GroupRef): Promise<void> {
  const base = flushBaseDrafts(ref, {
    onSaved: (r, id, saved) => applySavedEntry(qc, r, id, saved),
    onConflict: (r) => {
      void qc.invalidateQueries({ queryKey: ['pg-group', refKey(r)] });
      void qc.invalidateQueries({ queryKey: ['pg-entries', refKey(r)] });
    },
  });
  await base;
  await flushVariantDrafts(ref, { onSaved: (r, id, saved) => applySavedEntry(qc, r, id, saved) });
}

/** 草稿 PATCH 成功后：按原 ref 用响应更新缓存（新 ETag/revision/条目），再失效重取。 */
export function applySavedEntry(qc: QueryClient, ref: GroupRef, entryId: string, r: Versioned<Entry>): void {
  const k = refKey(ref);
  qc.setQueryData<Versioned<{ revision: number }>>(['pg-group', k], (old) => old && ({
    data: { ...old.data, revision: r.data.revision ?? old.data.revision + 1 },
    etag: r.etag ?? null, // 拿不到新 ETag 时旧值必然过期，置空等待重取
  }));
  qc.setQueryData<InfiniteData<Versioned<EntryPage>>>(['pg-entries', k], (old) => old && ({
    ...old,
    pages: old.pages.map((pg) => ({
      ...pg,
      data: { ...pg.data, items: pg.data.items.map((it) => (it.entry_id === entryId ? { ...it, ...r.data } : it)) },
    })),
  }));
  void qc.invalidateQueries({ queryKey: ['pg-group', k] });
  void qc.invalidateQueries({ queryKey: ['pg-entries', k] });
}

export function describeLeaveError(e: unknown): { tone: 'danger'; text: string } {
  if (e instanceof DraftsPendingError) return { tone: 'danger', text: '仍有未保存的草稿，已留在当前词典（草稿已保留）' };
  if (e instanceof VariantConflictError) {
    const names = e.keys.map((k) => `「${k}」`).join('、');
    return { tone: 'danger', text: `服务端已更新变体${names}，已留在当前词典（草稿已保留），再次离开将覆盖` };
  }
  return { tone: 'danger', text: `草稿保存失败，已留在当前词典：${describePgError(e).text}（草稿已保留）` };
}

export function apiLeaveDeps(qc: QueryClient): LeaveDeps {
  return {
    // 不再无条件 clearScope：flushVariantDrafts 只清已保存的草稿，保存期间的新输入保留。
    flush: (ref) => flushGroupDrafts(qc, ref),
    hasDrafts: hasGroupDrafts,
    onError: (e) => { const m = describeLeaveError(e); notify(m.tone, m.text); },
  };
}

const coordinators = new WeakMap<QueryClient, LeaveCoordinator>();
/** 每个 QueryClient 一个协调器，生命周期长于任何页面组件。 */
export function leaveCoordinatorFor(qc: QueryClient): LeaveCoordinator {
  let c = coordinators.get(qc);
  if (!c) { c = createLeaveCoordinator(apiLeaveDeps(qc)); coordinators.set(qc, c); }
  return c;
}
