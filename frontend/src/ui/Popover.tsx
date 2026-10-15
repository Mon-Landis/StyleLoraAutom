import { useEffect, useRef, type ReactNode } from 'react';

/** 视觉包装：锚点弹层。外部点击 / Esc 关闭；触发器由调用方提供并自行管理 open。 */
export function Popover({ open, onClose, trigger, label, align = 'start', children }: {
  align?: 'start' | 'end';
  open: boolean;
  onClose: () => void;
  trigger: ReactNode;
  label: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('mousedown', down);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('mousedown', down);
      document.removeEventListener('keydown', key);
    };
  }, [open, onClose]);
  return (
    <div className="popover" data-align={align} ref={ref}>
      {trigger}
      {open && <div className="popover__panel" role="dialog" aria-label={label}>{children}</div>}
    </div>
  );
}
