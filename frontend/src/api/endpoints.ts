import { get, post, put } from './client';
import type { ImageFilter, ImageItem, Page, ReviewStatus } from './types';

const P = (pid: string) => `/api/projects/${pid}`;

export function filterQuery(f: Partial<ImageFilter>): Record<string, string | number | boolean | Array<string | number> | undefined> {
  return {
    review_status: f.review_status?.length ? f.review_status : undefined,
    source_id: f.source_id?.length ? f.source_id : undefined,
    rating: f.rating?.length ? f.rating : undefined,
  };
}

export interface ListArgs {
  cursor?: string | null;
  around_id?: number;
  limit?: number;
  filter: Partial<ImageFilter>;
}
export const listImages = (pid: string, a: ListArgs, signal?: AbortSignal) =>
  get<Page<ImageItem>>(`${P(pid)}/images`, {
    query: { ...filterQuery(a.filter), limit: a.limit ?? 100, cursor: a.cursor ?? undefined, around_id: a.around_id },
    signal,
  }).then((r) => r.data);

export const getStats = (pid: string, filter: Partial<ImageFilter>) =>
  get<Record<string, unknown>>(`${P(pid)}/images/stats`, { query: filterQuery(filter) }).then((r) => r.data);

/** 单张/批量审图：统一走 POST /review，expected_versions 为每张图当前 version。 */
export async function reviewImages(pid: string, items: Array<Pick<ImageItem, 'image_id' | 'version'>>, target: ReviewStatus) {
  const expected: Record<string, string> = {};
  for (const i of items) expected[String(i.image_id)] = i.version;
  const { data } = await post<{ items: Array<{ image: ImageItem }> }>(`${P(pid)}/images/review`, {
    ids: items.map((i) => i.image_id).sort((a, b) => a - b),
    target,
    expected_versions: expected,
  });
  return data.items.map((x) => x.image);
}

export interface AnnotationTag {
  tag: string;
  conf?: number | null;
}
export interface LayerRow {
  annotation_id: number;
  content: AnnotationTag[] | string;
  confidence?: number | null;
  model_name?: string | null;
}
export interface AnnotationView {
  image_id: number;
  tags: AnnotationTag[];
  natural_language: string | null;
  layers: Record<string, LayerRow | null>;
  refs: Array<{ image_id: number; source: string; type: string; annotation_id: number | null }>;
  missing_original?: boolean;
  version: string;
}
export const getAnnotation = (pid: string, id: number) =>
  get<AnnotationView>(`${P(pid)}/annotations/${id}`).then((r) => r.data);

export const putManual = (pid: string, id: number, view: AnnotationView, tags: AnnotationTag[], nl: string | null) =>
  put<AnnotationView>(
    `${P(pid)}/annotations/${id}/manual`,
    { tags, natural_language: nl ?? undefined, write_nl: nl !== null, read_set: view.refs },
    { ifMatch: view.version },
  ).then((r) => r.data);

export interface Preview {
  preview_id: string;
  digest: string;
  affected_count?: number;
  total?: number;
}
export const previewRule = (pid: string, rule: unknown, selectedIds: number[]) =>
  post<Preview>(`${P(pid)}/rules/preview`, { rule, scope: { selected_ids: selectedIds } }).then((r) => r.data);
export const applyRule = (pid: string, p: Preview) =>
  post<Record<string, unknown>>(`${P(pid)}/rules/apply`, { preview_id: p.preview_id, digest: p.digest }, { ifMatch: p.digest }).then((r) => r.data);
export interface OpLogRow {
  operation_id?: number;
  id?: number;
  kind?: string;
  status?: string;
  version?: string;
  applied_at?: string;
}
export const listOperationLog = (pid: string) =>
  get<Page<OpLogRow>>(`${P(pid)}/operation-log`, { query: { limit: 20 } }).then((r) => r.data);
export const undoOperation = (pid: string, id: number, version: string) =>
  post<unknown>(`${P(pid)}/operation-log/${id}/undo`, {}, { ifMatch: version }).then((r) => r.data);
