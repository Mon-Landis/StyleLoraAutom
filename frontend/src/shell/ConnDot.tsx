import { useConnection, type ConnState } from '../state/connection';

const LABEL: Record<ConnState, string> = {
  booting: '连接中',
  online: '在线',
  degraded: '推送断开',
  offline: '离线',
  reauth: '需重新引导',
  draining: '后端关闭中',
};
const TONE: Record<ConnState, string> = {
  booting: 'info', online: 'success', degraded: 'warning', offline: 'danger', reauth: 'danger', draining: 'warning',
};

/** 连接状态点。紧凑模式只显示点（title/aria-label 带文字），设置页可显示文字。 */
export function ConnDot({ showLabel = false }: { showLabel?: boolean }) {
  const conn = useConnection((s) => s.state);
  return (
    <span className="conn" role="status" aria-live="polite" aria-label={`连接状态：${LABEL[conn]}`} title={LABEL[conn]}>
      <span className={`conn__dot conn__dot--${TONE[conn]}`} aria-hidden="true" />
      {showLabel && LABEL[conn]}
    </span>
  );
}
