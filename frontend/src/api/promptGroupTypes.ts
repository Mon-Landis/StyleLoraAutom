// F27/F31 提示词组库：前端类型集中定义（docs/F27 §2 / §4 / §8；v5 合同见 docs/F31 §2 / §7）。
// 后端 docs/frontend-contract-f27.md 到位后，只需对齐本文件与 promptGroups.ts / safety.ts。
export type Scope = 'global' | 'project';
export type Aspect = 'landscape' | 'portrait' | 'square';

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
/** F31 §7.1：v5 只保留 nl 一个基础文本字段。 */
export type ContentField = 'nl';
export type BaseField = ContentField;
/** F31 §7.2：视图只有「提示词 / 变体」；旧本地值 default/nl 由 normalizeView 映射为 prompt。 */
export type PromptView = 'prompt' | 'variants';
/** 提示词格式：存储统一 NAI，anima 仅作来源标注。 */
export type PromptFormat = 'nai' | 'anima';

/** 生成标记：模型生成且尚未经用户编辑。只读，客户端不得提交。 */
export type GeneratedFields = Record<BaseField, boolean> & { variants: Record<string, boolean> };
export type FieldVersions = Record<BaseField, number> & { variants: Record<string, number> };
export interface PendingField {
  state: 'pending';
  task_id: string;
  attempt: number;
  edit_version: number;
  input_digest: string;
}

export interface CompletionState {
  state: CompletionStateName;
  missing: ContentField[];
  task_id: string | null;
  error_code: string | null;
}

export interface Entry {
  entry_id: string;
  nl: string;
  disable: boolean;
  aspect: Aspect | null;
  preview: PreviewRef | null;
  completion: CompletionState;
  /** F30：变体字典；v4 之前的响应可能缺省，读取处用 variantsOf 兜底。 */
  variants: Record<string, string>;
  /** F30 内部状态：只读。缺省视为全 false / 空。 */
  generated?: GeneratedFields;
  pending?: Partial<Record<BaseField, PendingField>>;
  edit_versions?: FieldVersions;
  /** 条目列表行带所属组的 nsfw 标记（后端 plain(e) | {nsfw}）。 */
  nsfw?: boolean;
  /** PATCH/POST 响应随带的组版本。 */
  revision?: number;
  version?: string;
}

/** 可写内容字段（POST 创建；PATCH 为其部分子集）。POST 的 variants 是完整字典，不含 null。 */
export interface EntryDraft {
  nl?: string;
  disable?: boolean;
  aspect?: Aspect | null;
  variants?: Record<string, string>;
  /** 仅 POST/PATCH 提交 nl 时有意义；缺省按 nai。 */
  source_format?: PromptFormat;
}

/** PATCH 体：只含实际改动字段；variants 按 key 合并，null 删除，{} 为无操作。 */
export type EntryPatch = Partial<Pick<EntryDraft, 'nl' | 'disable' | 'aspect' | 'source_format'>>
  & { variants?: Record<string, string | null> };

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
  /** F30：离开该组时自动补齐缺字段。 */
  auto_complete?: boolean;
  schema_version?: number;
}

export interface GroupView {
  dictionary_id: string;
  name: string;
  kind: string;
  category: string;
  scope: Scope;
  nsfw: boolean;
  revision: number;
  auto_complete?: boolean;
}

export interface GroupCreateBody {
  name: string;
  kind: 'prompts';
  category: 'full_content';
  nsfw: boolean;
  scope: Scope;
  project_token: string | null;
  auto_complete?: boolean;
}

// ---- F30 补全任务（POST /api/tasks 的 202 响应与 GET /api/tasks/{id}）----
export type CompletionTrigger = 'manual' | 'auto_leave';
/** 统一任务视图（只列前端用到的字段）。无候选时 status=done 且无 reports。 */
export interface TaskView {
  task_id: string;
  status: string;
  kind?: string;
  reason_code?: string | null;
  version?: string;
}
export interface CompletionReport {
  entry_id: string;
  error_code?: string | null;
  applied_fields?: BaseField[];
  discarded_fields?: BaseField[];
  warnings?: unknown[];
}
export interface CompletionTask extends TaskView {
  reports?: CompletionReport[];
}

export interface DeleteReceipt {
  [k: string]: unknown;
}

export interface PgPage<T> {
  items: T[];
  next_cursor: string | null;
}

// ---- 导入（F31 §2 / §3 / §4）----
export type ImportLevel = 'group' | 'entries';
/** 旧 JSON 对话框的来源选择（T16 前保留）。 */
export type ImportSourceFormat = 'standard' | 'nai';
export type ImportFormat = 'json' | 'txt' | 'zip' | 'folder' | 'nai_images' | 'nai_image_zip';
export type ExportFormat = 'json' | 'nai_image_zip';

export interface ImportIssue {
  row?: number;
  field?: string;
  code?: string;
  message?: string;
  file_index?: number;
}

/** 图像批量导入的逐文件结果（file_index 从 1 起，按 multipart 顺序）。 */
export interface ImportFileReport {
  file_index: number;
  name: string;
  status: 'valid' | 'skipped';
  source: string | null;
  entry_id?: string;
  issues: ImportIssue[];
}

/** 预览冻结的条目（图像导入时 file_index 指向 files 报告）。 */
export interface ImportPreviewEntry {
  entry_id: string;
  nl?: string;
  file_index?: number;
}

export interface ImportPreviewV3 {
  preview_id: string;
  entries?: ImportPreviewEntry[];
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
  metadata?: { name?: string; nsfw?: boolean; scope?: Scope; auto_complete?: boolean };
  normalization_version?: string;
  /** 仅 nai_images / nai_image_zip：逐文件报告。 */
  files?: ImportFileReport[];
}
/** F31 §8 命名别名。 */
export type ImportPreviewV5 = ImportPreviewV3;

export interface ImportResult {
  ref?: GroupRef;
  inserted?: number;
  duplicates?: number;
  conflicts?: Array<ImportIssue & { entry_id?: string }>;
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
