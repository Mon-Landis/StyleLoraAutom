// HoldToConfirm 的进度计算（纯函数），时长来自令牌 --hold-danger-ms / --hold-secondary-ms。
export type HoldKind = 'danger' | 'secondary';

export function holdProgress(elapsed: number, duration: number): number {
  if (duration <= 0) return 1;
  return Math.min(1, Math.max(0, elapsed / duration));
}
