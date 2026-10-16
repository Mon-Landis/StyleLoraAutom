import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react';
import { HoldToConfirm } from './HoldToConfirm';

/** 视觉+交互包装：右键菜单。方向键/Enter/Esc/Tab 键盘可达；删除类条目用长按读条（时长参数化）。 */
export interface MenuItem {
  key: string;
  label: string;
  danger?: boolean;
  disabled?: boolean;
  onSelect?: () => void;
  /** 有值则渲染为长按确认项，完成时调用 onConfirm（只调用一次）。 */
  hold?: { ms: number; onConfirm: () => void };
}

export interface MenuState<T> { x: number; y: number; target: T }

/** 触发器：鼠标右键、ContextMenu 键、Shift+F10。 */
export function useContextMenu<T>() {
  const [state, set] = useState<MenuState<T> | null>(null);
  const close = useCallback(() => set(null), []);
  const bind = useCallback((target: T, onKey?: (e: KeyboardEvent) => void) => ({
    onContextMenu: (e: MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      set({ x: e.clientX, y: e.clientY, target });
    },
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
        e.preventDefault();
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        set({ x: r.left + r.width / 2, y: r.top + r.height / 2, target });
        return;
      }
      onKey?.(e);
    },
  }), []);
  return { state, close, bind };
}

const FOCUSABLE = 'button:not(:disabled)';

export function ContextMenu({ x, y, label, items, onClose }: {
  x: number; y: number; label: string; items: MenuItem[]; onClose: () => void;
}) {
  const ref = useRef<HTMLUListElement>(null);
  const back = useRef<Element | null>(null);

  useEffect(() => {
    back.current = document.activeElement;
    ref.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    const down = (e: globalThis.MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener('mousedown', down);
    return () => {
      document.removeEventListener('mousedown', down);
      if (back.current instanceof HTMLElement && back.current.isConnected) back.current.focus();
    };
  }, [onClose]);

  const onKeyDown = (e: KeyboardEvent) => {
    const els = [...(ref.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    else if (e.key === 'Tab') onClose();
    else if (e.key === 'ArrowDown') { e.preventDefault(); els[(i + 1) % els.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); els[(i - 1 + els.length) % els.length]?.focus(); }
    else if (e.key === 'Home') { e.preventDefault(); els[0]?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); els[els.length - 1]?.focus(); }
  };

  return (
    <ul ref={ref} className="ctxmenu" role="menu" aria-label={label} style={{ left: x, top: y }} onKeyDown={onKeyDown}>
      {items.map((it) => (
        <li key={it.key} role="none">
          {it.hold ? (
            <HoldToConfirm
              kind="danger"
              disabled={it.disabled}
              durationMs={it.hold.ms}
              onConfirm={() => { it.hold!.onConfirm(); onClose(); }}
            >
              {it.label}
            </HoldToConfirm>
          ) : (
            <button
              type="button"
              role="menuitem"
              className={`ctxmenu__item${it.danger ? ' ctxmenu__item--danger' : ''}`}
              disabled={it.disabled}
              onClick={() => { it.onSelect?.(); onClose(); }}
            >
              {it.label}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
