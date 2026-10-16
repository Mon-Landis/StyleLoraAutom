import type { KeyboardEvent } from 'react';

/** 列表内上下键移动焦点：items 为同一容器内匹配 selector 的元素。 */
export function arrowNav(e: KeyboardEvent, container: HTMLElement | null, selector: string): void {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  if (!container) return;
  const items = [...container.querySelectorAll<HTMLElement>(selector)];
  const i = items.indexOf(e.currentTarget as HTMLElement);
  if (i < 0) return;
  e.preventDefault();
  items[Math.min(items.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))]?.focus();
}
