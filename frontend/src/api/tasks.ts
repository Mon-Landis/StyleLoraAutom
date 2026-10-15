import { get, post } from './client';
import { listExports, listPromptTasks, listSources, type PromptTask, type SourceRow } from './more';
import type { ExportView, JobRow, Page } from './types';

/**
 * 统一任务适配层（过渡）。
 * TODO(backend): 设计文档 F25-F26 规划了 GET /api/tasks、POST /api/tasks/{id}/cancel 与 task.* SSE；
 * 接口落地后只需把 fetchTasks / cancelTask 内部换成单次调用，TaskItem 形状与上层托盘不变。
 */
export type TaskKind = 'job' | 'source' | 'prompt' | 'export';
export type TaskStatus = 'pending' | 'running' | 'paused' | 'done' | 'failed' | 'canceled' | 'canceling';

export interface TaskItem {
  /** 全局唯一：kind + 项目 + 资源 id */
  key: string;
  kind: TaskKind;
  title: string;
  resourceId: string;
  projectId: string | null;
  status: TaskStatus;
  rawStatus: string;
  progress: { completed: number; total: number | null };
  /** 并发控制令牌（If-Match）；缺失则无法发起取消 */
  version: string | null;
  cancelable: boolean;
}

export interface TaskSnapshot {
  items: TaskItem[];
  /** 本轮拉取失败（含 501 未实现）的来源，托盘据此提示「部分来源不可用」 */
  unavailable: TaskKind[];
}

export const KIND_LABEL: Record<TaskKind, string> = { job: '执行器', source: '数据源', prompt: '提示词', export: '导出' };
export const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: '排队中', running: '运行中', paused: '已暂停', done: '已完成', failed: '失败', canceled: '已取消', canceling: '取消中',
};

const ACTIVE: ReadonlySet<TaskStatus> = new Set(['pending', 'running', 'paused', 'canceling']);
const RUNNING: ReadonlySet<TaskStatus> = new Set(['pending', 'running', 'canceling']);
export const isActive = (s: TaskStatus) => ACTIVE.has(s);
/** 「运行中」徽标计数口径：暂停的不算。 */
export const isRunning = (s: TaskStatus) => RUNNING.has(s);
export const runningCount = (items: TaskItem[]) => items.filter((t) => isRunning(t.status)).length;

const P = (pid: string) => `/api/projects/${pid}`;

function normalize(raw: string | undefined): TaskStatus {
  switch (raw) {
    case 'pending': case 'planned': return 'pending';
    case 'running': return 'running';
    case 'paused': return 'paused';
    case 'done': case 'finished': return 'done';
    case 'canceled': return 'canceled';
    default: return 'failed'; // failed / interrupted / 未知
  }
}
const cancelableNow = (status: TaskStatus, version: string | null | undefined) =>
  !!version && (status === 'pending' || status === 'running' || status === 'paused');

export function fromJob(pid: string, j: JobRow): TaskItem | null {
  const id = j.job_id ?? j.id;
  if (id == null) return null;
  const status = normalize(j.status);
  return {
    key: `job:${pid}:${id}`, kind: 'job', title: `${j.executor ?? '执行器'} #${id}`, resourceId: String(id), projectId: pid,
    status, rawStatus: j.status ?? '', version: j.version ?? null, cancelable: cancelableNow(status, j.version),
    progress: { completed: j.progress_json?.done ?? 0, total: j.progress_json?.total ?? null },
  };
}

export function fromSource(pid: string, s: SourceRow): TaskItem | null {
  if (s.status === 'draft') return null; // 草稿不是任务
  const status = normalize(s.status);
  return {
    key: `source:${pid}:${s.source_id}`, kind: 'source', title: s.name, resourceId: String(s.source_id), projectId: pid,
    status, rawStatus: s.status ?? '', version: s.version ?? null, cancelable: cancelableNow(status, s.version),
    // TODO(backend): 数据源列表没有已完成计数，先按不定进度显示
    progress: { completed: 0, total: s.target_count ?? null },
  };
}

export function fromPrompt(t: PromptTask): TaskItem | null {
  const id = t.task_id ?? t.id;
  if (id == null) return null;
  const status = normalize(t.status);
  return {
    key: `prompt:${id}`, kind: 'prompt', title: t.name ?? `提示词任务 #${id}`, resourceId: String(id), projectId: null,
    status, rawStatus: t.status ?? '', version: t.version ?? null, cancelable: cancelableNow(status, t.version),
    progress: { completed: t.completed ?? 0, total: t.total ?? null },
  };
}

export function fromExport(pid: string, e: ExportView): TaskItem {
  const status = normalize(e.status);
  return {
    key: `export:${pid}:${e.export_id}`, kind: 'export', title: `导出 ${e.export_id.slice(0, 8)}`, resourceId: e.export_id, projectId: pid,
    status, rawStatus: e.status, version: e.version ?? null,
    // 导出仅 planned/running 可取消；paused 不存在
    cancelable: (status === 'pending' || status === 'running') && !!e.version,
    progress: { completed: e.exported + e.skipped + e.failed, total: e.selected },
  };
}

/** 只拉未结束的 job（后端支持 status 过滤），避免对历史任务做 N+1 详情请求。 */
async function activeJobs(pid: string): Promise<JobRow[]> {
  const pages = await Promise.all(
    (['pending', 'running', 'paused'] as const).map((status) =>
      get<Page<JobRow>>(`${P(pid)}/jobs`, { query: { status, limit: 50 } }).then((r) => r.data),
    ),
  );
  // 按 id 去重（防止后端忽略 status 过滤时重复）；列表响应不含 progress_json，需逐个取详情，失败时退回列表行。
  const unique = new Map<number | undefined, JobRow>();
  for (const row of pages.flatMap((p) => p.items)) unique.set(row.job_id ?? row.id, row);
  return Promise.all(
    [...unique.values()].map((row) =>
      get<JobRow>(`${P(pid)}/jobs/${row.job_id ?? row.id}`).then((r) => r.data).catch(() => row),
    ),
  );
}

const rank: Record<TaskStatus, number> = { running: 0, canceling: 1, pending: 2, paused: 3, failed: 4, canceled: 5, done: 6 };

/** 聚合 jobs / sources / 提示词任务 / 导出；单个来源失败不影响其它来源。 */
export async function fetchTasks(pid: string | null): Promise<TaskSnapshot> {
  const sources: Array<[TaskKind, Promise<Array<TaskItem | null>>]> = [
    ['prompt', listPromptTasks().then((p) => p.items.map(fromPrompt))],
  ];
  if (pid) {
    sources.push(
      ['job', activeJobs(pid).then((rows) => rows.map((j) => fromJob(pid, j)))],
      ['source', listSources(pid).then((p) => p.items.map((s) => fromSource(pid, s)))],
      ['export', listExports(pid).then((p) => p.items.map((e) => fromExport(pid, e)))],
    );
  }
  const settled = await Promise.allSettled(sources.map(([, p]) => p));
  const items: TaskItem[] = [];
  const unavailable: TaskKind[] = [];
  settled.forEach((r, i) => {
    if (r.status === 'fulfilled') items.push(...r.value.filter((x): x is TaskItem => x !== null && isActive(x.status)));
    else unavailable.push(sources[i][0]);
  });
  items.sort((a, b) => rank[a.status] - rank[b.status] || a.key.localeCompare(b.key));
  return { items, unavailable };
}

/** 取消：各领域沿用现有接口。不可取消的条目直接抛错，调用方应禁用按钮。 */
export async function cancelTask(t: TaskItem): Promise<void> {
  if (!t.cancelable || !t.version) throw new Error('task:not_cancelable');
  const ifMatch = t.version;
  switch (t.kind) {
    case 'job':
      await post(`${P(t.projectId!)}/jobs/${t.resourceId}/control`, { action: 'cancel' }, { ifMatch });
      return;
    case 'source':
      await post(`${P(t.projectId!)}/sources/${t.resourceId}/control`, { action: 'cancel' }, { ifMatch });
      return;
    case 'prompt':
      await post(`/api/prompt-tasks/${t.resourceId}/control`, { action: 'cancel' }, { ifMatch });
      return;
    case 'export':
      await post(`${P(t.projectId!)}/exports/${t.resourceId}/cancel`, {}, { ifMatch });
      return;
  }
}
