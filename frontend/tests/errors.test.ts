import { describe, expect, it } from 'vitest';
import { ApiError, parseApiError } from '../src/api/errors';

describe('parseApiError', () => {
  it('parses the unified error envelope', () => {
    const e = parseApiError(409, { error: { code: 'cas:conflict', message: 'stale', retryable: true, retry_action: 'refetch', request_id: 'r1' } });
    expect(e).toBeInstanceOf(ApiError);
    expect(e.code).toBe('cas:conflict');
    expect(e.conflict).toBe(true);
    expect(e.body.retryable).toBe(true);
    expect(e.body.requestId).toBe('r1');
  });

  it('flags 501 as unsupported', () => {
    expect(parseApiError(501, { error: { code: 'not_implemented', message: 'x' } }).unsupported).toBe(true);
  });

  it('treats 428 as conflict and 401 as auth lost', () => {
    expect(parseApiError(428, null).conflict).toBe(true);
    expect(parseApiError(401, null).authLost).toBe(true);
  });

  it('falls back to http:<status> for malformed bodies and uses header request id', () => {
    const e = parseApiError(500, '<html>', 'hdr');
    expect(e.code).toBe('http:500');
    expect(e.body.requestId).toBe('hdr');
  });
});
