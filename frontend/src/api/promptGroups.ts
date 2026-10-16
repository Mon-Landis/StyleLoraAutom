// F27 提示词组 API（docs/F27 §8）。契约文档 docs/frontend-contract-f27.md 到位后在此对齐。
import { get, patch, post, put, request, uuid, type Resp } from './client';
import type {
  DeleteReceipt, Entry, EntryDraft, GroupCreateBody, GroupRef, GroupSummary, GroupView,
  ImportLevel, ImportPreviewV3, ImportResult, ImportSourceFormat, PgPage,
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

export const patchGroup = (ref: GroupRef, body: { name?: string; nsfw?: boolean }, etag: string, key = uuid()) =>
  patch<GroupView>(G(ref.group_id), body, { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

export const deleteGroup = (ref: GroupRef, etag: string, key = uuid()) =>
  request<DeleteReceipt>('DELETE', G(ref.group_id), { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

export interface EntryListParams { disable?: boolean; limit?: number; cursor?: string | null }
export type EntryPage = PgPage<Entry> & { revision?: number; nsfw?: boolean };
export const listEntries = (ref: GroupRef, p: EntryListParams = {}, signal?: AbortSignal) =>
  get<EntryPage>(`${G(ref.group_id)}/entries`, { query: { ...refQuery(ref), limit: 50, ...p }, signal }).then(wrap);

export const addEntry = (ref: GroupRef, draft: EntryDraft, etag: string, key = uuid()) =>
  post<Entry>(`${G(ref.group_id)}/entries`, draft, { query: refQuery(ref), ifMatch: etag, idempotencyKey: key }).then(wrap);

export const patchEntry = (ref: GroupRef, entryId: string, body: EntryDraft, etag: string, key = uuid()) =>
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

// ---- 导入导出 ----
export interface ImportOptions {
  format?: 'json';
  level?: ImportLevel;
  ref?: GroupRef;
  source_format?: ImportSourceFormat | 'anima';
  metadata?: { group_id?: string; name?: string; nsfw?: boolean; scope?: 'global' | 'project' };
}
export const previewImport = (data: unknown, options: ImportOptions) =>
  post<ImportPreviewV3>('/api/prompt-imports/preview', { data, options }, { idempotencyKey: uuid() }).then((r) => r.data);

export const confirmImport = (previewId: string, digest: string, ref: GroupRef, baseRevision: number, etag: string | null, key = uuid()) =>
  post<ImportResult>(`/api/prompt-imports/${encodeURIComponent(previewId)}/confirm`, { digest, ref }, {
    idempotencyKey: key,
    ...(baseRevision === 0 ? { ifNoneMatch: true } : { ifMatch: etag }),
  }).then((r) => r.data);

export const exportGroupJson = (ref: GroupRef) =>
  get<unknown>(`${G(ref.group_id)}/export`, { query: { ...refQuery(ref), format: 'json' } }).then((r) => r.data);

// ---- LLM 补全（统一任务入口）----
export const enqueueCompletion = (ref: GroupRef, entryIds: string[], baseRevision: number, key = uuid()) =>
  post<{ task_id?: string }>('/api/tasks', { type: 'prompt_completion', ref, entry_ids: entryIds, base_revision: baseRevision }, { idempotencyKey: key }).then((r) => r.data);
