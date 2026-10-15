// 后端响应的宽松类型（以 src/stylelora/runtime/*.py 的实际输出为准，字段可缺省）。
export type ReviewStatus = 'unreviewed' | 'keep' | 'reject';
export type Rating = 'general' | 'sensitive' | 'questionable' | 'explicit';
export type Layer = 'original' | 'wd' | 'llm' | 'manual' | 'rule';
export const LAYERS: readonly Layer[] = ['original', 'wd', 'llm', 'manual', 'rule'];
export const REVIEW_STATUSES: readonly ReviewStatus[] = ['unreviewed', 'keep', 'reject'];
export const RATINGS: readonly Rating[] = ['general', 'sensitive', 'questionable', 'explicit'];

export interface ImageItem {
  image_id: number;
  filename?: string;
  source_id?: number | null;
  review_status: ReviewStatus;
  rating?: Rating | null;
  width?: number;
  height?: number;
  file_missing?: boolean;
  has_alpha?: boolean | null;
  annotation_coverage?: { status: string };
  version: string;
  original_url: string;
  thumbnail_url?: string;
}

export interface Page<T> {
  items: T[];
  total?: number;
  start_index?: number;
  anchor_index?: number | null;
  next_cursor: string | null;
  has_more: boolean;
  limit: number;
  upper_id?: number | null;
}

export interface ImageFilter {
  review_status: ReviewStatus[];
  source_id: number[];
  rating: Rating[];
}

export interface Tag {
  tag: string;
  conf?: number | null;
}
export interface AnnotationRow {
  annotation_id: number;
  image_id: number;
  source: Layer;
  type: 'tags' | 'nl';
  content: Tag[] | string;
  confidence?: number | null;
  model_name?: string | null;
}
export interface CurrentRef {
  image_id: number;
  source: Layer;
  type: 'tags' | 'nl';
  annotation_id: number | null;
}
export interface AnnotationView {
  image_id: number;
  tags: Tag[];
  natural_language: string | null;
  layers: Record<string, AnnotationRow | null>;
  refs: CurrentRef[];
  missing_original?: boolean;
  version: string;
}

export interface Operation {
  operation_id: string;
  kind: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'canceled';
  completed: number;
  total: number | null;
  result: unknown;
  error: { code: string } | null;
  version: string;
}

export interface ManualResult {
  annotation_ids: number[];
  changed: boolean;
}

export interface ExportView {
  export_id: string;
  project_id: string;
  status: 'planned' | 'running' | 'finished' | 'failed' | 'canceled' | 'interrupted';
  selected: number;
  exported: number;
  skipped: number;
  failed: number;
  report_url: string | null;
  relative_dir: string | null;
  version: string;
}

export interface ProjectView {
  project_id: string;
  /** mode：后端尚未提供，缺省按 style（见 lib/projectMode.ts）。 */
  metadata: { name?: string; trigger_word?: string; mode?: string };
  version: string;
}

export interface JobRow {
  job_id?: number;
  id?: number;
  executor?: string;
  status?: string;
  progress_json?: { done?: number; total?: number; outcomes?: Record<string, unknown> };
  version?: string;
}

export interface SseEvent {
  id: string;
  kind: string;
  data: { project_id?: string | null; payload?: Record<string, unknown> } & Record<string, unknown>;
}
