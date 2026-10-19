import { describe, expect, it } from 'vitest';
import { parseApiError } from '../src/api/errors';
import { describeGeneration, settleGenerationPoll } from '../src/lib/promptErrors';

describe('settleGenerationPoll', () => {
  it('separates finished, gone and failed polls', () => {
    const settled: PromiseSettledResult<{ task_id: string; status: string }>[] = [
      { status: 'fulfilled', value: { task_id: 'a', status: 'done' } },
      { status: 'fulfilled', value: { task_id: 'b', status: 'running' } },
      { status: 'rejected', reason: parseApiError(404, { error: { code: 'task:not_found', message: 'x' } }) },
      { status: 'rejected', reason: parseApiError(410, { error: { code: 'task:expired', message: 'x' } }) },
      { status: 'rejected', reason: new Error('network down') },
    ];
    const poll = settleGenerationPoll(['a', 'b', 'c', 'd', 'e'], settled);
    expect(poll.finished.map((t) => t.task_id)).toEqual(['a']);
    expect(poll.gone).toEqual(['c', 'd']);
    expect(poll.error).toBeTruthy();
  });

  it('reports no error when all polls succeed', () => {
    const poll = settleGenerationPoll(['a'], [{ status: 'fulfilled', value: { task_id: 'a', status: 'pending' } }]);
    expect(poll).toEqual({ finished: [], gone: [], error: null });
  });
});

describe('describeGeneration', () => {
  it('maps reason, batch errors and issues to Chinese text', () => {
    const text = describeGeneration({
      status: 'failed', generated: 3, failed: 2, reason_code: 'llm:timeout',
      reports: [{ batch: 1, error_code: 'dictionary:revision_conflict' }],
      issues: [{ code: 'count_short' }],
    });
    expect(text).toContain('已追加 3 条');
    expect(text).toContain('第 2 批');
    expect(text).not.toContain('llm:timeout：');
  });
});
