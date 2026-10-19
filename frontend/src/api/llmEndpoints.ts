import { get, put } from './client';
import type { Page } from './types';

export interface LlmEndpoint {
  endpoint_id: string;
  base_url: string;
  protocol: string;
  models: Array<{ name: string; supports_images: boolean; wire_name?: string | null; response_aliases?: string[] }>;
  token_refs: Array<{ token_ref: string }>;
  supports_json_schema: boolean;
  max_parallel: number;
  version: string;
}

export async function listLlmEndpoints() {
  const items: LlmEndpoint[] = [];
  let cursor: string | null = null;
  do {
    const { data }: { data: Page<LlmEndpoint> } = await get<Page<LlmEndpoint>>('/api/llm/endpoints', {
      query: { limit: 1000, cursor: cursor ?? undefined },
    });
    items.push(...data.items);
    cursor = data.next_cursor;
  } while (cursor);
  return items;
}

export const saveEndpointParallel = (endpoint: LlmEndpoint, maxParallel: number) =>
  put<LlmEndpoint>(`/api/llm/endpoints/${endpoint.endpoint_id}`, {
    base_url: endpoint.base_url,
    protocol: endpoint.protocol,
    models: endpoint.models.map(({ name, supports_images, wire_name, response_aliases }) =>
      ({ name, supports_images, wire_name, response_aliases })),
    token_refs: endpoint.token_refs.map((ref) => ref.token_ref),
    supports_json_schema: endpoint.supports_json_schema,
    max_parallel: maxParallel,
  }, { ifMatch: endpoint.version }).then((r) => r.data);
