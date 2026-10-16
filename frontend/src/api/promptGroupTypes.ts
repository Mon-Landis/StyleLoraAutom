// F27 提示词组库：前端类型集中定义（docs/F27-提示词组库设计.md §2 / §4 / §8）。
// 后端 docs/frontend-contract-f27.md 到位后，只需对齐本文件与 promptGroups.ts / safety.ts。
export type Scope = 'global' | 'project';
export type Aspect = 'landscape' | 'portrait' | 'square';
export type RenderMode = 'tags_plus_nl' | 'full_nl';

/** 运行时限定引用。project_token 当前取已打开项目的 project_id（待契约确认）。 */
export interface GroupRef {
  scope: Scope;
  project_token: string | null;
  group_id: string;
}

export interface PreviewRef {
  asset_revision: string;
  sha256: string;
  width: number;
  height: number;
  mime: 'image/webp';
}

export type CompletionStateName =
  | 'pending' | 'queued' | 'running' | 'complete' | 'failed' | 'paused' | 'canceled' | 'stale';
export type ContentField = 'tag' | 'expand_nl' | 'nl';

export interface CompletionState {
  state: CompletionStateName;
  missing: ContentField[];
  task_id: string | null;
  error_code: string | null;
}

export interface Entry {
  entry_id: string;
  tag: string[];
  expand_nl: string;
  nl: string;
  disable: boolean;
  aspect: Aspect | null;
  preview: PreviewRef | null;
  completion: CompletionState;
  /** 条目列表行带所属组的 nsfw 标记（后端 plain(e) | {nsfw}）。 */
  nsfw?: boolean;
}

/** 可写内容字段（POST 创建；PATCH 为其部分子集）。 */
export interface EntryDraft {
  tag?: string[];
  expand_nl?: string;
  nl?: string;
  disable?: boolean;
  aspect?: Aspect | null;
}

export interface GroupSummary {
  dictionary_id: string;
  name: string;
  kind: string;
  scope: Scope;
  nsfw: boolean;
  revision: number;
  entry_count: number;
  /** 后端 summary 带回；project 组的限定引用用它，缺省回退当前项目 id。 */
  project_token?: string | null;
  /** 字符串化 revision，等同强 ETag（去引号）。 */
  version?: string;
}

export interface GroupView {
  dictionary_id: string;
  name: string;
  kind: string;
  category: string;
  scope: Scope;
  nsfw: boolean;
  revision: number;
}

export interface GroupCreateBody {
  name: string;
  kind: 'prompts';
  category: 'full_content';
  nsfw: boolean;
  scope: Scope;
  project_token: string | null;
}

export interface DeleteReceipt {
  [k: string]: unknown;
}

export interface PgPage<T> {
  items: T[];
  next_cursor: string | null;
}

// ---- 导入（§5.3；响应字段契约未定，全部可选）----
export type ImportLevel = 'group' | 'entries';
export type ImportSourceFormat = 'standard' | 'nai';

export interface ImportIssue {
  row?: number;
  field?: string;
  code?: string;
  message?: string;
}

export interface ImportPreviewV3 {
  preview_id: string;
  digest: string;
  base_revision?: number;
  valid_count?: number;
  skipped_count?: number;
  /** 后端字段名为 issues（逐行报告）。 */
  issues?: ImportIssue[];
  warnings?: Array<ImportIssue | string>;
  ref?: GroupRef;
  /** scope=project 但未开项目时为 true，禁止提交。 */
  blocked?: boolean;
  metadata?: { name?: string; nsfw?: boolean; scope?: Scope };
}

export interface ImportResult {
  ref?: GroupRef;
  inserted?: number;
  duplicates?: number;
  conflicts?: Array<{ code?: string; entry_id?: string }>;
  revision?: number;
  version?: string;
}

// ---- 安全设置（§4 / §8）----
export type NsfwDisplayMode = 'click_reveal' | 'hover' | 'show';
export interface SafetySettings {
  nsfw_enabled: boolean;
  nsfw_display_mode: NsfwDisplayMode;
}
export interface SessionSafetyState {
  revealed_this_run: boolean;
}
export interface ImageDecision {
  blur: boolean;
  hint: string | null;
  can_reveal: boolean;
}
