import { get, post, put, patch, uuid } from './client';
import type { ExportView, JobRow, Operation, Page, ProjectView } from './types';

export interface KnownProject {
  known_id: string;
  display_name: string;
}
export const listKnownProjects = () => get<Page<KnownProject>>('/api/projects').then((r) => r.data);
export const currentProject = () => get<ProjectView | null>('/api/projects/current').then((r) => r.data);
export const getOperation = (id: string) => get<Operation>(`/api/operations/${id}`).then((r) => r.data);

/** 弹出系统目录选择器（后端 picker 不可用时 501 -> 调用方显示占位）。返回 operation。 */
export const pickFolder = (purpose: 'project_create' | 'project_open') =>
  post<Operation>('/api/filesystem/grants', { purpose }).then((r) => r.data);
export const openProject = (body: { grant_id?: string; known_id?: string }) =>
  post<Operation>('/api/projects/open', body).then((r) => r.data);
export const createProject = (body: { grant_id: string; name: string; trigger_word?: string }) =>
  post<Operation>('/api/projects', body).then((r) => r.data);
export const closeProject = (pid: string, version: string) =>
  post<unknown>(`/api/projects/${pid}/close`, {}, { ifMatch: version }).then((r) => r.data);

const P = (pid: string) => `/api/projects/${pid}`;

export type ExecutorId = 'wd' | 'llm_vision';
export type JobMode = 'selected' | 'uncovered' | 'all';
export const createJobs = (pid: string, executors: ExecutorId[], mode: JobMode, selectedIds: number[]) =>
  post<JobRow[]>(
    `${P(pid)}/jobs/batches`,
    {
      batch_id: uuid(),
      executors,
      mode,
      scope: mode === 'selected' ? { selected_ids: selectedIds } : { filter: {} },
    },
    { idempotencyKey: uuid() },
  ).then((r) => r.data);
export const listJobs = async (pid: string) => {
  const { data } = await get<Page<JobRow>>(`${P(pid)}/jobs`, { query: { limit: 50 } });
  const items = await Promise.all(data.items.map((job) => get<JobRow>(`${P(pid)}/jobs/${job.job_id}`).then((r) => r.data)));
  return { ...data, items };
};
export const controlJob = (pid: string, id: number, action: string, version: string) =>
  post<unknown>(`${P(pid)}/jobs/${id}/control`, { action }, { ifMatch: version }).then((r) => r.data);

export interface SourceRow {
  source_id: number;
  type: string;
  name: string;
  status?: string;
  target_count?: number | null;
  version?: string;
}
export const listSources = (pid: string) => get<Page<SourceRow>>(`${P(pid)}/sources`, { query: { limit: 200 } }).then((r) => r.data);
export const createSource = (pid: string, body: { type: string; name: string; target_count?: number }) =>
  post<SourceRow>(`${P(pid)}/sources`, body).then((r) => r.data);

export interface PromptTask {
  task_id?: number | string;
  id?: number | string;
  name?: string;
  status?: string;
  version?: string;
}
export const listPromptTasks = () => get<Page<PromptTask>>('/api/prompt-tasks', { query: { limit: 50 } }).then((r) => r.data);
export interface SamplingRequest {
  plan: { primary_dimensions: string[]; cell_values: Record<string, string[]>; target_count: number };
  prompt_dictionary_ids: string[];
  coverage_enabled: boolean;
  optimize: boolean;
}
export const createPromptTask = (body: SamplingRequest) => post<PromptTask>('/api/prompt-tasks', body).then((r) => r.data);

export interface ExportScheme {
  scheme_id: number;
  name: string;
  version?: string;
}
export const listExportSchemes = (pid: string) =>
  get<Page<ExportScheme>>(`${P(pid)}/export-schemes`, { query: { limit: 100 } }).then((r) => r.data);
export interface ExportPreview {
  preview_id: string;
  digest: string;
  selected: number;
  issues: Array<{ severity: string; code: string }>;
  trigger_override_digest?: string | null;
}
export const previewExport = (pid: string, schemeId: number) =>
  post<Operation>(`${P(pid)}/exports/preview`, { scheme_id: schemeId }).then((r) => r.data);
export const runExport = (pid: string, schemeId: number, p: ExportPreview) =>
  post<ExportView>(`${P(pid)}/exports`, { scheme_id: schemeId, preview_id: p.preview_id, digest: p.digest }, { idempotencyKey: uuid() }).then((r) => r.data);
export const getExport = (pid: string, id: string) =>
  get<ExportView>(`${P(pid)}/exports/${id}`).then((r) => r.data);

export const getConfig = () => get<Record<string, unknown>>('/api/config').then((r) => r.data);
export { patch, put };
