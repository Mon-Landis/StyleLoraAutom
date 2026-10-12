import { useEffect, type ReactNode } from 'react';
import { Button } from './primitives';

export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);
  return (
    <div className="dialog__scrim" onMouseDown={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(e) => e.stopPropagation()}>
        <div className="dialog__head">
          <span>{title}</span>
          <Button variant="ghost" aria-label="关闭" onClick={onClose}>×</Button>
        </div>
        <div className="dialog__body">{children}</div>
      </div>
    </div>
  );
}
