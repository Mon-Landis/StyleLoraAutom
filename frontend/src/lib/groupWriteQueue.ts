/**
 * 同一提示词组（以 refKey 为 scope）的写入串行队列。
 * 组内所有条目共用一个 ETag，并发 PATCH 必然互相 412；离开 flush 与编辑器显式保存都走这里，
 * 后者排在前者之后执行，并在任务体内重新读取草稿，不会拿排队前的旧快照覆盖新文本。
 * 任务失败不会阻塞后续任务；队列空闲后自动移除，Map 不会增长。
 */
const tails = new Map<string, Promise<void>>();

export function runGroupWrite<T>(scope: string, task: () => Promise<T>): Promise<T> {
  const prev = tails.get(scope) ?? Promise.resolve();
  const run = prev.then(() => task());
  const tail = run.then(() => undefined, () => undefined);
  tails.set(scope, tail);
  void tail.then(() => { if (tails.get(scope) === tail) tails.delete(scope); });
  return run;
}

/** 仅供测试：当前仍有排队/执行中任务的 scope 数。 */
export const pendingWriteScopes = (): number => tails.size;
