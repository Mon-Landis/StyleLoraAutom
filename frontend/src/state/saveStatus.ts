import { useApp } from './app';
import { useConnection, type ConnState } from './connection';
import { usePendingWrites } from './writes';

export type SaveKey = 'none' | 'offline' | 'saving' | 'saved';
export interface SaveStatus {
  key: SaveKey;
  label: string;
  tone: 'muted' | 'warning' | 'danger' | 'success';
}

/**
 * TODO(backend): 后端目前没有「项目保存状态」。这里先用连接状态 + 在途写请求推导：
 * 有写请求在途 -> 保存中；连接异常 -> 连接断开；否则 -> 已保存。
 * 后端提供真实 dirty/saving 字段后，只替换本函数的输入即可。
 */
export function deriveSaveStatus(i: { hasProject: boolean; conn: ConnState; pendingWrites: number }): SaveStatus {
  if (!i.hasProject) return { key: 'none', label: '未打开项目', tone: 'muted' };
  if (i.conn === 'offline' || i.conn === 'reauth' || i.conn === 'draining') return { key: 'offline', label: '连接断开', tone: 'danger' };
  if (i.pendingWrites > 0) return { key: 'saving', label: '保存中', tone: 'warning' };
  return { key: 'saved', label: '已保存', tone: 'success' };
}

export function useSaveStatus(): SaveStatus {
  const hasProject = useApp((s) => s.projectId != null);
  const conn = useConnection((s) => s.state);
  const pendingWrites = usePendingWrites((s) => s.pending);
  return deriveSaveStatus({ hasProject, conn, pendingWrites });
}
