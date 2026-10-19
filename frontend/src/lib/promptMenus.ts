import type { MenuItem } from '../ui/ContextMenu';
import { HOLD_ENTRY_DELETE_MS, groupDeleteHoldMs } from '../tokens/hold';

/** 组右键菜单（F27 §9）：信息修改 / 导出 / 删除。非空组删除长按 2s，空组直接删。 */
export function groupMenuItems(p: {
  entryCount: number;
  onEdit: () => void;
  onExport: () => void;
  onDelete: () => void;
}): MenuItem[] {
  const ms = groupDeleteHoldMs(p.entryCount);
  return [
    { key: 'edit', label: '信息修改', onSelect: p.onEdit },
    { key: 'export', label: '导出', onSelect: p.onExport },
    ms === null
      ? { key: 'delete', label: '删除', danger: true, onSelect: p.onDelete }
      : { key: 'delete', label: '长按删除（2 秒）', danger: true, hold: { ms, onConfirm: p.onDelete } },
  ];
}

/** 条目右键菜单：修改 / 复制 JSON / 禁用·启用 / 删除（长按 0.5s）。F31 §7.3 起不再有手动 LLM 补全入口。 */
export function entryMenuItems(p: {
  disabled: boolean;
  onEdit: () => void;
  onCopyJson: () => void;
  onToggleDisable: () => void;
  onDelete: () => void;
}): MenuItem[] {
  return [
    { key: 'edit', label: '修改', onSelect: p.onEdit },
    { key: 'copy', label: '复制 JSON', onSelect: p.onCopyJson },
    { key: 'disable', label: p.disabled ? '启用' : '禁用', onSelect: p.onToggleDisable },
    { key: 'delete', label: '长按删除（0.5 秒）', danger: true, hold: { ms: HOLD_ENTRY_DELETE_MS, onConfirm: p.onDelete } },
  ];
}
