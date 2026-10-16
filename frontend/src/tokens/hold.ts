// 长按确认时长（F27 §9）。HoldToConfirm 通过 durationMs 参数接收，集中在此定义。
export const HOLD_ENTRY_DELETE_MS = 500;
export const HOLD_GROUP_DELETE_MS = 2000;

/** 组删除所需长按时长：空组直接删除返回 null（无需读条），非空组 2s。 */
export function groupDeleteHoldMs(entryCount: number): number | null {
  return entryCount > 0 ? HOLD_GROUP_DELETE_MS : null;
}
