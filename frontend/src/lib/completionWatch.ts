import { ApiError } from '../api/errors';
import { getCompletionTask } from '../api/promptGroups';
import type { CompletionTask } from '../api/promptGroupTypes';
import { notify } from '../state/notices';
import { describeErrorCode } from './promptErrors';

/** 终态：不再轮询。paused/canceled 不会被自动重试（F30 §5.2）。 */
const TERMINAL = new Set(['done', 'failed', 'canceled', 'paused']);
export const isTerminalTask = (status: string): boolean => TERMINAL.has(status);

export interface CompletionSummary {
  tone: 'success' | 'warning' | 'danger';
  text: string;
  applied: number;
  discarded: number;
  failed: number;
}

/** 汇总 reports 里的 applied_fields / discarded_fields / error_code，给出一条简短提示。 */
export function summarizeCompletion(t: CompletionTask, label: string): CompletionSummary {
  const reports = t.reports ?? [];
  const applied = reports.reduce((n, r) => n + (r.applied_fields?.length ?? 0), 0);
  const discarded = reports.reduce((n, r) => n + (r.discarded_fields?.length ?? 0), 0);
  const codes = new Set(reports.map((r) => r.error_code).filter((c): c is string => !!c));
  const failed = reports.filter((r) => r.error_code).length;
  const head = t.status === 'canceled' ? `${label}已取消` : t.status === 'paused' ? `${label}已暂停（不会自动重试）` : `${label}已结束`;
  const parts = [`写入 ${applied} 个字段`];
  if (discarded) parts.push(`${discarded} 个字段因已被手动填写或内容变化而丢弃`);
  if (failed) parts.push(`${failed} 条未完成（${[...codes].map(describeErrorCode).join('；')}）`);
  else if (t.reason_code) parts.push(describeErrorCode(t.reason_code));
  const bad = t.status === 'failed' || t.status === 'paused' || t.status === 'canceled' || failed > 0;
  const tone = t.status === 'failed' ? 'danger' : bad || discarded ? 'warning' : 'success';
  return { tone, text: `${head}：${parts.join('，')}。`, applied, discarded, failed };
}

export type WatchLostReason = 'gone' | 'timeout' | 'unreachable';

export interface WatchOptions {
  label: string;
  onSettled: (t: CompletionTask, s: CompletionSummary) => void;
  /**
   * 跟踪放弃（任务已失效 / 超过跟踪时限 / 连续失败过多）时调用，用于失效相关查询，
   * 避免界面残留「生成中」。通知由 watchCompletion 自己发出，调用方不必重复。
   */
  onLost?: (reason: WatchLostReason) => void;
  /** 正常轮询间隔，默认 1500ms。 */
  intervalMs?: number;
  /** 总跟踪时限，默认 10 分钟。 */
  maxTrackMs?: number;
  /** 暂时失败的退避起点（1s -> 2s -> 4s ...），默认 1000ms。 */
  backoffBaseMs?: number;
  /** 退避上限，默认 30s。 */
  backoffMaxMs?: number;
  /** 连续暂时失败的次数上限，默认 8；任一次成功即清零。 */
  maxFailures?: number;
}

export const LOST_TEXT: Record<WatchLostReason, string> = {
  gone: '补全任务已失效（服务端已找不到该任务），请刷新查看结果',
  timeout: '补全状态未知，已停止跟踪',
  unreachable: '补全状态未知，已停止跟踪（多次查询失败）',
};

/**
 * 明确的「任务不存在」类错误：404/410 及其余 4xx。
 * 401/403（会话需重新认证）、408（超时）、429（限流）属暂时失败，不能当作任务失效。
 */
export function isTaskGone(e: unknown): boolean {
  if (!(e instanceof ApiError)) return false;
  const s = e.status;
  if (s === 401 || s === 403 || s === 408 || s === 429) return false;
  return s >= 400 && s < 500;
}

/**
 * 轮询补全任务直到终态，回调一次。返回取消函数。不属于页面生命周期：
 * 离开提示词页后仍会继续并通过全局通知提示。
 * - 终态：onSettled 一次；
 * - 任务不存在（明确 4xx）：停止，通知「任务已失效」，onLost('gone')；
 * - 暂时失败（网络错误 / 5xx / 408 / 401 / 403 / 429）：指数退避重试（上限 backoffMaxMs），
 *   连续失败超过 maxFailures 则 onLost('unreachable')；
 * - 超过总时限：通知「补全状态未知，已停止跟踪」，onLost('timeout')。
 * 绝不重新发布任务。
 */
export function watchCompletion(taskId: string, o: WatchOptions): () => void {
  const every = o.intervalMs ?? 1500;
  const maxTrackMs = o.maxTrackMs ?? 10 * 60_000;
  const backoffBase = o.backoffBaseMs ?? 1000;
  const backoffMax = o.backoffMaxMs ?? 30_000;
  const maxFailures = o.maxFailures ?? 8;
  const deadline = Date.now() + maxTrackMs;
  let failures = 0;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
  let ctl: AbortController | undefined;

  /** 终止一切：幂等；清定时器并中止悬挂中的请求。 */
  const halt = () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    if (deadlineTimer) clearTimeout(deadlineTimer);
    ctl?.abort();
  };
  /** 通知与 onLost 只触发一次：已停止（终态/取消/已 lost）则忽略。 */
  const lost = (reason: WatchLostReason) => {
    if (stopped) return;
    halt();
    notify('warning', LOST_TEXT[reason]);
    o.onLost?.(reason);
  };
  const schedule = (delay: number) => {
    timer = setTimeout(() => void tick(), delay);
  };
  const tick = async () => {
    if (stopped) return;
    ctl = new AbortController();
    try {
      const t = await getCompletionTask(taskId, ctl.signal);
      if (stopped) return;
      failures = 0;
      if (isTerminalTask(t.status)) { halt(); o.onSettled(t, summarizeCompletion(t, o.label)); return; }
    } catch (e) {
      if (stopped) return; // 截止/取消导致的中止
      if (isTaskGone(e)) return lost('gone');
      if (++failures > maxFailures) return lost('unreachable');
      if (Date.now() >= deadline) return lost('timeout');
      schedule(Math.min(backoffMax, backoffBase * 2 ** (failures - 1)));
      return;
    }
    if (Date.now() >= deadline) return lost('timeout');
    schedule(every);
  };
  // 独立截止定时器：不依赖请求返回，fetch 悬挂时也会到点中止并通知。
  deadlineTimer = setTimeout(() => lost('timeout'), maxTrackMs);
  schedule(every);
  return halt;
}