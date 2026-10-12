// 统一错误合同：{ error: { code, message, retryable, retry_action, outcome, details, issues, request_id } }
export interface ApiErrorBody {
  code: string;
  message: string;
  retryable: boolean;
  retryAction: string;
  outcome: string | null;
  details: unknown;
  requestId: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || body.code);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }

  get code(): string {
    return this.body.code;
  }
  /** 后端未实现的端点（501）：前端显示占位而不是报错。 */
  get unsupported(): boolean {
    return this.status === 501;
  }
  /** CAS / 版本冲突：409 或 428，应重新读取后重试。 */
  get conflict(): boolean {
    return this.status === 409 || this.status === 428;
  }
  get authLost(): boolean {
    return this.status === 401;
  }
}

/** 网络层失败（fetch 抛错 / 无响应）。 */
export class NetworkError extends Error {
  constructor(cause?: unknown) {
    super('network:unreachable');
    this.name = 'NetworkError';
    this.cause = cause;
  }
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

/** 解析任意响应体为 ApiError；格式不符时退化为 http:<status>。 */
export function parseApiError(status: number, payload: unknown, headerRequestId?: string | null): ApiError {
  const raw =
    payload && typeof payload === 'object' && 'error' in payload
      ? (payload as { error: unknown }).error
      : null;
  if (raw && typeof raw === 'object') {
    const e = raw as Record<string, unknown>;
    const code = str(e.code, `http:${status}`);
    return new ApiError(status, {
      code,
      message: str(e.message, code),
      retryable: e.retryable === true,
      retryAction: str(e.retry_action, 'none'),
      outcome: typeof e.outcome === 'string' ? e.outcome : null,
      details: e.details ?? null,
      requestId: str(e.request_id) || headerRequestId || null,
    });
  }
  const code = `http:${status}`;
  return new ApiError(status, {
    code,
    message: code,
    retryable: false,
    retryAction: 'none',
    outcome: null,
    details: null,
    requestId: headerRequestId ?? null,
  });
}
