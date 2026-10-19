import { ApiError } from '../api/errors';

export interface PgErrorInfo {
  text: string;
  /** 412/428/409：应重新读取后再试，草稿保留。 */
  conflict: boolean;
  /** 404：移除失效选择。 */
  notFound: boolean;
}

const TEXT: Record<string, string> = {
  'dictionary:revision_conflict': '词典已被修改，请重新读取后再试（草稿已保留）',
  'revision:required': '缺少版本信息，请重新读取后再试',
  'settings:revision_conflict': '设置已被修改，请重新读取后再试',
  'dictionary:not_found': '词典不存在，已移除选择',
  'entry:not_found': '条目不存在，请刷新列表',
  'project:closed': '项目已关闭，无法写入项目级词典',
  'group:id_conflict': '词典 ID 已存在，请更换或使用新 ID',
  'pagination:stale': '列表已变化，请重新读取',
  'pagination:invalid': '分页游标无效，请重新读取',
  'image:invalid': '图像无效（仅支持静态 PNG / JPEG / WebP）',
  'image:too_large': '图像过大（上限 10 MiB）',
  'schema:invalid': '内容校验失败，请检查输入',
  'import:empty': '没有有效条目可导入',
  'preview:expired': '导入预览已过期，请重新预览',
  'completion:unavailable': 'LLM 补全暂不可用，请先配置提示词 skill 与 LLM 端点',
  'completion:stale': '条目已变化，补全结果需复核',
  'completion:incomplete': '模型未能补全全部字段',
  'completion:failed': '补全失败',
  'llm:outcome_unknown': '模型调用结果未知，已暂停，不会自动重试',
  'storage:io_error': '存储失败，旧数据未改动，请重试',
  'prompt:schema_changed': '词典结构已升级到 v5，旧任务已暂停，请重新提交',
  'import:limit': '上传超出数量或大小上限，请减少文件后重试',
  'import:unsafe_archive': '压缩包不安全，已拒绝',
  'import:metadata_mismatch': '图片元数据与清单内容不一致，已拒绝',
  'metadata:empty': '图片没有可用的正面提示词',
  'metadata:invalid': '图片元数据损坏',
  'metadata:limit': '图片元数据超出大小限制',
  'normalization:syntax': '权重/括号结构不合法，该条未导入',
  'normalization:rounded': '权重已按 8 位小数舍入',
};

/** 错误码 -> 中文；未收录的原样返回。 */
export const describeErrorCode = (code: string): string => TEXT[code] ?? code;

export interface ImportFileFailure { file_index?: number; name?: string; code?: string; message?: string }

/** 全部无效的 422 import:empty：从 error.details.files 取逐文件原因（兼容 issues 数组）。 */
export function importFileIssues(e: unknown): ImportFileFailure[] {
  if (!(e instanceof ApiError)) return [];
  const d = e.body.details;
  const files = d && typeof d === 'object' ? (d as { files?: unknown }).files : null;
  if (!Array.isArray(files)) return [];
  const out: ImportFileFailure[] = [];
  for (const f of files) {
    if (!f || typeof f !== 'object') continue;
    const r = f as { file_index?: number; name?: string; issues?: Array<{ code?: string; message?: string }> };
    const issues = Array.isArray(r.issues) && r.issues.length ? r.issues : [{}];
    for (const i of issues) out.push({ file_index: r.file_index, name: r.name, code: i.code, message: i.message });
  }
  return out;
}

export function describePgError(e: unknown): PgErrorInfo {
  if (e instanceof ApiError) {
    const code = e.code;
    return {
      text: TEXT[code] ?? `${code}: ${e.message}`,
      conflict: e.status === 412 || e.status === 428 || code === 'pagination:stale',
      notFound: e.status === 404 && (code === 'dictionary:not_found' || code === 'http:404'),
    };
  }
  return { text: e instanceof Error ? e.message : String(e), conflict: false, notFound: false };
}

const GENERATION_TEXT: Record<string, string> = {
  count_short: '模型返回条数不足',
  'entry:invalid': '部分条目未通过校验已丢弃',
  'dictionary:revision_conflict': '词典持续被修改，批次写入失败',
  'dictionary:not_found': '目标词典不存在',
  'project:closed': '项目已关闭',
  'task:canceled': '任务已取消',
  'llm:timeout': 'LLM 请求超时',
  'llm:rate_limited': 'LLM 请求被限流',
  'llm:unavailable': 'LLM 服务不可用',
  'llm:schema_contract': 'LLM 返回格式不符合约定',
  'llm:refused': 'LLM 拒绝生成',
};

const generationText = (code: string): string =>
  GENERATION_TEXT[code] ?? (code.startsWith('llm:') ? `LLM 调用失败（${code}）` : code);

export interface GenerationLike {
  status: string;
  generated: number;
  failed: number;
  reason_code?: string | null;
  batches?: { batch: number; status: string; error_code?: string | null; issues?: { code?: string }[] }[];
  issues?: { code?: string }[];
  reports?: { batch: number; error_code?: string | null; issues?: { code?: string }[] }[];
}

const STATUS_TEXT: Record<string, string> = {
  done: '已完成', failed: '失败', canceled: '已取消', paused: '已暂停',
};

/** 生成任务结束时的中文摘要：状态、条数、失败原因与问题（去重）。 */
export function describeGeneration(t: GenerationLike): string {
  const head = `生成任务${STATUS_TEXT[t.status] ?? t.status}：已追加 ${t.generated} 条，失败/不足 ${t.failed} 条。`;
  const reasons = new Set<string>();
  if (t.reason_code) reasons.add(generationText(t.reason_code));
  for (const b of [...(t.batches ?? []), ...(t.reports ?? [])]) {
    if (b.error_code) reasons.add(`第 ${b.batch + 1} 批：${generationText(b.error_code)}`);
  }
  const issues = new Set<string>();
  for (const i of t.issues ?? []) if (i.code) issues.add(generationText(i.code));
  const parts = [head];
  if (reasons.size) parts.push(`原因：${[...reasons].join('；')}。`);
  if (issues.size) parts.push(`问题：${[...issues].join('；')}。`);
  return parts.join(' ');
}

const ACTIVE = ['pending', 'running', 'canceling'];

export interface GenerationPoll<T> {
  /** 已结束（非 pending/running/canceling）的任务。 */
  finished: T[];
  /** 404/410：任务已失效，应停止轮询。 */
  gone: string[];
  /** 其他查询失败（网络、5xx 等），保留轮询并提示可重试。 */
  error: string | null;
}

/** 汇总一轮 allSettled 轮询结果；单个任务失败不影响其他任务。 */
export function settleGenerationPoll<T extends { task_id: string; status: string }>(
  ids: string[], settled: PromiseSettledResult<T>[],
): GenerationPoll<T> {
  const finished: T[] = [];
  const gone: string[] = [];
  const errors = new Set<string>();
  settled.forEach((r, i) => {
    if (r.status === 'fulfilled') {
      if (!ACTIVE.includes(r.value.status)) finished.push(r.value);
    } else if (r.reason instanceof ApiError && (r.reason.status === 404 || r.reason.status === 410)) {
      gone.push(ids[i]);
    } else {
      errors.add(describePgError(r.reason).text);
    }
  });
  return { finished, gone, error: errors.size ? [...errors].join('；') : null };
}

/** 确定性失败（服务端已给出结果）后应更换幂等键；网络/未知结果保留原键。 */
export const isDefinitive = (e: unknown): boolean => e instanceof ApiError;
