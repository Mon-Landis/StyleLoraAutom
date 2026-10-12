import { getOperation } from '../api/more';
import type { Operation } from '../api/types';

/** 轮询 operation 直到终态（SSE 之外的兜底，足够 MVP 使用）。 */
export async function waitOperation(op: Operation, timeoutMs = 120000): Promise<Operation> {
  let cur = op;
  const end = Date.now() + timeoutMs;
  while (cur.status === 'pending' || cur.status === 'running') {
    if (Date.now() > end) throw new Error('operation timeout');
    await new Promise((r) => setTimeout(r, 400));
    cur = await getOperation(cur.operation_id);
  }
  if (cur.status !== 'done') throw new Error(cur.error?.code ?? `operation ${cur.status}`);
  return cur;
}
