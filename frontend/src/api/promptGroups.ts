// F27 提示词组 API（docs/F27 §8）。契约文档 docs/frontend-contract-f27.md 到位后在此对齐。
import { buildUrl, fetchBlob, get, patch, post, put, request, uuid, type Resp } from './client';
import type {
  CompletionTask, CompletionTrigger, TaskView, ExportFormat,
  DeleteReceipt, Entry, EntryDraft, EntryPatch, GroupCreateBody, GroupRef, GroupSummary, GroupView,
  ImportFormat, ImportLevel, ImportPreviewV3, ImportPreviewV5, ImportResult, PgPage, PromptFormat,
} from './promptGroupTypes';

const G = (id: string) => `/api/prompt-groups/${encodeURIComponent(id)}`;
const E = (id: string, eid: string) => `${G(id)}/entries/${encodeURIComponent(eid)}`;
/** 限定引用放 query（组创建除外）。 */
export const refQuery = (r: GroupRef) => ({ scope: r.scope, project_token: r.project_token });
export const refKey = (r: GroupRef) => `${r.scope}:${r.project_token ?? ''}:${r.group_id}`;

export interface GroupListParams {
  q?: string;
  scope?: 'global' | 'project';
  limit?: number;
  cursor?: string | null;
}
export const listGroups = (p: GroupListParams = {}, signal?: AbortSignal) =>
  get<PgPage<GroupSummary>>('/api/prompt-groups', { query: { limit: 200, ...p }, signal }).then((r) => r.data);

export const createGroup = (body: GroupCreateBody, key = uuid()) =>
  post<GroupSummary>('/api/prompt-groups', body, { ifNoneMatch: true, idempotencyKey: key }).then((r) => r.data);

export interface Versioned<T> { data: T; etag: string | null }
const wrap = <T>(r: Resp<T>): Versioned<T> => ({ data: r.data, etag: r.etag });

export const getGroup = (ref: GroupRef, signal?: AbortSignal) =>
  get<GroupView & { entry_count?: number }>(G(ref.group_id), { query: refQuery(ref), signal }).then(wrap);

export const patchGroup = (ref: GroupRef, body: { name?: string; nsfw?: boolean; auto_complete?: boolean }, etag: string, key = uuid()) =>
  patch<GroupView>(G(ref.group_id), body, { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

export const deleteGroup = (ref: GroupRef, etag: string, key = uuid()) =>
  request<DeleteReceipt>('DELETE', G(ref.group_id), { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

export interface EntryListParams { disable?: boolean; limit?: number; cursor?: string | null }
export type EntryPage = PgPage<Entry> & { revision?: number; nsfw?: boolean };
export const listEntries = (ref: GroupRef, p: EntryListParams = {}, signal?: AbortSignal) =>
  get<EntryPage>(`${G(ref.group_id)}/entries`, { query: { ...refQuery(ref), limit: 50, ...p }, signal }).then(wrap);

export const addEntry = (ref: GroupRef, draft: EntryDraft, etag: string, key = uuid()) =>
  post<Entry>(`${G(ref.group_id)}/entries`, draft, { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

/** body 只放实际改动字段；variants 为 {key: string|null} 合并语义。 */
export const patchEntry = (ref: GroupRef, entryId: string, body: EntryPatch, etag: string, key = uuid()) =>
  patch<Entry>(E(ref.group_id, entryId), body, { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

export const deleteEntry = (ref: GroupRef, entryId: string, etag: string, key = uuid()) =>
  request<DeleteReceipt>('DELETE', E(ref.group_id, entryId), { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

/** 单条导出/复制 JSON：EntryDraft，不含内部字段与图。 */
export const exportEntry = (ref: GroupRef, entryId: string) =>
  get<EntryDraft>(`${E(ref.group_id, entryId)}/export`, { query: { ...refQuery(ref), format: 'json' } }).then((r) => r.data);

export const putPreview = (ref: GroupRef, entryId: string, file: Blob, etag: string, key = uuid()) => {
  const fd = new FormData();
  fd.append('image', file);
  return put<unknown>(`${E(ref.group_id, entryId)}/preview`, fd, { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);
};

/** 带鉴权取图用的路径（配合 useBlobUrl）；asset_revision 进 URL，换图自然换缓存键。 */
export const previewPath = (ref: GroupRef, entryId: string, size: 'full' | 'thumb', assetRevision?: string) => {
  const sp = new URLSearchParams({ scope: ref.scope, size });
  if (ref.project_token) sp.set('project_token', ref.project_token);
  if (assetRevision) sp.set('asset_revision', assetRevision);
  return `${E(ref.group_id, entryId)}/preview?${sp.toString()}`;
};

// ---- 导入导出（F31 §2–§5）----
export interface ImportOptions {
  format?: ImportFormat;
  level?: ImportLevel;
  /** level=entries（追加到已有组）必须带 ref。 */
  ref?: GroupRef;
  /** nai_images 固定 nai，调用方不应传 anima。 */
  source_format?: PromptFormat;
  metadata?: { group_id?: string; name?: string; nsfw?: boolean; scope?: 'global' | 'project'; auto_complete?: boolean };
}

/** JSON body 预览（粘贴文本/已解析 JSON）。文件类来源用 previewImportFiles。 */
export const previewImport = (data: unknown, options: ImportOptions, signal?: AbortSignal) =>
  post<ImportPreviewV3>('/api/prompt-imports/preview', { data, options }, { idempotencyKey: uuid(), signal }).then((r) => r.data);

/**
 * multipart 预览：options 为 JSON 字符串字段。
 * format=nai_images：每个文件各 append 一次到重复的 `files` 字段（不按文件名去重，顺序即 file_index）；
 * 其余格式（json/txt/zip/nai_image_zip）为单文件字段 `file`。
 * 错误：全部无效 422 import:empty（逐文件原因在 error.details.files，见 importFileIssues）；超限 413 import:limit。
 */
export const previewImportFiles = (files: readonly File[], options: ImportOptions, key = uuid(), signal?: AbortSignal) => {
  if (files.length === 0) return Promise.reject(new Error('请先选择文件'));
  const fd = new FormData();
  fd.append('options', JSON.stringify(options));
  if (options.format === 'nai_images') for (const f of files) fd.append('files', f, f.name);
  else fd.append('file', files[0], files[0].name);
  return post<ImportPreviewV5>('/api/prompt-imports/preview', fd, { idempotencyKey: key, signal }).then((r) => r.data);
};

/** 导入预览条目缩略图/大图路径（需 Bearer：用 useBlobUrl 或 fetchImportPreviewBlob 取）。 */
export const importPreviewPath = (previewId: string, entryId: string, size: 'thumb' | 'full' = 'thumb') =>
  `/api/prompt-imports/${encodeURIComponent(previewId)}/entries/${encodeURIComponent(entryId)}/preview?size=${size}`;

/** 带鉴权取预览图 Blob；调用方负责 URL.createObjectURL / revoke。 */
export const fetchImportPreviewBlob = (previewId: string, entryId: string, size: 'thumb' | 'full' = 'thumb', signal?: AbortSignal) =>
  fetchBlob(importPreviewPath(previewId, entryId, size), signal);

export const confirmImport = (previewId: string, digest: string, ref: GroupRef, baseRevision: number, etag: string | null, key = uuid()) =>
  post<ImportResult>(`/api/prompt-imports/${encodeURIComponent(previewId)}/confirm`, { digest, ref }, {
    idempotencyKey: key,
    ...(baseRevision === 0 ? { ifNoneMatch: true } : { ifMatch: etag }),
  }).then((r) => r.data);

export const exportGroupJson = (ref: GroupRef) =>
  get<unknown>(`${G(ref.group_id)}/export`, { query: { ...refQuery(ref), format: 'json' } }).then((r) => r.data);

/** 导出为附件 Blob：json（application/json）或 nai_image_zip（application/zip）。下载见 lib/download.downloadBlob。 */
export const exportGroup = (ref: GroupRef, format: ExportFormat, signal?: AbortSignal): Promise<Blob> =>
  fetchBlob(buildUrl(`${G(ref.group_id)}/export`, { ...refQuery(ref), format }), signal);

// ---- LLM 补全（统一任务入口）----
/**
 * 发布补全任务。manual：ids 省略=全组；auto_leave：后端禁止 entry_ids 字段（含 null），这里一律不带。
 * 202 返回 TaskView；无候选时 status=done 的空任务。412=revision 过期，409=同键不同请求。
 */
export const enqueueCompletion = (
  ref: GroupRef, ids: string[] | undefined, baseRevision: number, key = uuid(),
  trigger: CompletionTrigger = 'manual', opts: { keepalive?: boolean } = {},
) => {
  const body: Record<string, unknown> = { type: 'prompt_completion', ref, base_revision: baseRevision, trigger };
  if (trigger === 'manual' && ids !== undefined) body.entry_ids = ids;
  return post<TaskView>('/api/tasks', body, { idempotencyKey: key, keepalive: opts.keepalive }).then((r) => r.data);
};

export const getCompletionTask = (id: string, signal?: AbortSignal) =>
  get<CompletionTask>(`/api/tasks/${encodeURIComponent(id)}`, { signal }).then((r) => r.data);

export const enqueueGeneration = (ref: GroupRef, brief: string, count: number, key = uuid()) =>
  post<{ task_id: string }>('/api/tasks', { type: 'prompt_generation', ref, brief, count }, { idempotencyKey: key }).then((r) => r.data);

export interface GenerationIssue { code?: string; detail?: string; group?: string; index?: number }
export interface GenerationBatch {
  batch: number; requested: number; generated: number; failed: number; status: string;
  issues?: GenerationIssue[]; error_code?: string | null;
}
export interface GenerationTask {
  task_id: string; status: string; generated: number; failed: number;
  requested?: number; reason_code?: string | null;
  batches?: GenerationBatch[]; issues?: GenerationIssue[];
  reports?: { batch: number; error_code?: string | null; issues?: GenerationIssue[] }[];
}

export const getGenerationTask = (id: string, signal?: AbortSignal) =>
  get<GenerationTask>(`/api/tasks/${encodeURIComponent(id)}`, { signal }).then((r) => r.data);
