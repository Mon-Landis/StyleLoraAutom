import { ApiError, NetworkError, parseApiError } from './errors';

// token 只存内存：不写 localStorage / sessionStorage。
let token: string | null = null;
export const setToken = (t: string | null): void => {
  token = t;
};
export const getToken = (): string | null => token;

/** 由 state 层注入，避免 api <-> state 循环依赖。 */
export const hooks: {
  onUnauthorized?: () => void;
  onNetwork?: (reachable: boolean) => void;
  /** 非 GET 请求开始(+1)/结束(-1)，用于推导「保存中」。 */
  onWrite?: (delta: 1 | -1) => void;
} = {};

type QueryValue = string | number | boolean | undefined | null | Array<string | number>;
export interface ReqOpts {
  query?: Record<string, QueryValue>;
  body?: unknown;
  ifMatch?: string | null;
  /** 创建新资源：If-None-Match: *（F27 §8）。 */
  ifNoneMatch?: boolean;
  idempotencyKey?: string;
  signal?: AbortSignal;
  anonymous?: boolean;
  /** 非持久化的 POST（纯计算类，如提示词转换）：不计入「保存中」的写入计数。 */
  silent?: boolean;
  /** 页面卸载时尽力发送（fetch keepalive）；body 需小于 64 KiB。 */
  keepalive?: boolean;
}
export interface Resp<T> {
  data: T;
  etag: string | null;
  status: number;
  location: string | null;
}

export function uuid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  const h = (n: number) => Math.floor(Math.random() * 16 ** n).toString(16).padStart(n, '0');
  return `${h(8)}-${h(4)}-4${h(3)}-a${h(3)}-${h(12)}`;
}

export function buildUrl(path: string, query?: ReqOpts['query']): string {
  if (!query) return path;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue;
    for (const item of Array.isArray(v) ? v : [v]) sp.append(k, String(item));
  }
  const qs = sp.toString();
  return qs ? `${path}?${qs}` : path;
}

export const stripEtag = (v: string | null): string | null => (v ? v.replace(/^W\//, '').replace(/"/g, '') : null);

function headersFor(method: string, o: ReqOpts): Record<string, string> {
  const h: Record<string, string> = {};
  if (!o.anonymous && token) h.Authorization = `Bearer ${token}`;
  // FormData 由浏览器自行带 multipart boundary。
  if (o.body !== undefined && !(o.body instanceof FormData)) h['Content-Type'] = 'application/json';
  if (o.ifMatch) h['If-Match'] = `"${o.ifMatch}"`;
  if (o.ifNoneMatch) h['If-None-Match'] = '*';
  // POST 默认生成幂等键；PATCH/PUT/DELETE 显式传入 idempotencyKey 才携带（F27 §8 要求所有写入带键）。
  const writes = method === 'POST' || (method !== 'GET' && !!o.idempotencyKey);
  if (writes && !o.anonymous) h['Idempotency-Key'] = o.idempotencyKey ?? uuid();
  return h;
}

async function send(method: string, path: string, o: ReqOpts): Promise<Response> {
  let res: Response;
  const isWrite = method !== 'GET' && !o.silent;
  if (isWrite) hooks.onWrite?.(1);
  try {
    res = await fetch(buildUrl(path, o.query), {
      method,
      headers: headersFor(method, o),
      body: o.body === undefined ? undefined : o.body instanceof FormData ? o.body : JSON.stringify(o.body),
      signal: o.signal,
      ...(o.keepalive ? { keepalive: true } : {}),
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    hooks.onNetwork?.(false);
    throw new NetworkError(e);
  } finally {
    if (isWrite) hooks.onWrite?.(-1);
  }
  hooks.onNetwork?.(true);
  if (res.status === 401 && !o.anonymous) hooks.onUnauthorized?.();
  return res;
}

async function failure(res: Response): Promise<ApiError> {
  const payload: unknown = await res.json().catch(() => null);
  return parseApiError(res.status, payload, res.headers.get('X-Request-ID'));
}

export async function request<T>(method: string, path: string, o: ReqOpts = {}): Promise<Resp<T>> {
  const res = await send(method, path, o);
  if (!res.ok) throw await failure(res);
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  return { data: data as T, etag: stripEtag(res.headers.get('ETag')), status: res.status, location: res.headers.get('Location') };
}

/** 带鉴权取二进制（缩略图/原图 -> blob URL，Q47）。 */
export async function fetchBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const res = await send('GET', path, { signal });
  if (!res.ok) throw await failure(res);
  return res.blob();
}

/** 带鉴权的原始流（SSE 用 fetch 流，因 EventSource 无法带 Authorization）。 */
export const openStream = (path: string, headers: Record<string, string>, signal: AbortSignal): Promise<Response> =>
  fetch(path, { headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers }, signal });

export const get = <T>(p: string, o?: ReqOpts) => request<T>('GET', p, o);
export const post = <T>(p: string, body?: unknown, o?: ReqOpts) => request<T>('POST', p, { ...o, body: body ?? {} });
export const patch = <T>(p: string, body: unknown, o?: ReqOpts) => request<T>('PATCH', p, { ...o, body });
export const put = <T>(p: string, body: unknown, o?: ReqOpts) => request<T>('PUT', p, { ...o, body });
