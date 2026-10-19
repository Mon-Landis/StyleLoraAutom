import { useEffect, type ReactNode } from 'react';
import { Button } from './primitives';

/**
 * busy：有不可中断的提交在途时为 true。此时 Esc / 点遮罩 / 右上角关闭均被忽略，
 * 并在 dialog 上标记 aria-busy，让辅助技术知道为何无法关闭。
 */
export function Dialog({ title, onClose, busy = false, children }: { title: string; onClose: () => void; busy?: boolean; children: ReactNode }) {
  const close = () => { if (!busy) onClose(); };
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && close();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
    // close 只依赖 busy / onClose
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onClose, busy]);
  return (
    <div className="dialog__scrim" onMouseDown={close}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} aria-busy={busy || undefined} onMouseDown={(e) => e.stopPropagation()}>
        <div className="dialog__head">
          <span>{title}</span>
          <Button variant="ghost" aria-label="关闭" disabled={busy} onClick={close}>×</Button>
        </div>
        <div className="dialog__body">{children}</div>
      </div>
    </div>
  );
}
