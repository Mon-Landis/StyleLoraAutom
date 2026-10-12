export * from './base';
export * from './motion';

function rootStyle(): CSSStyleDeclaration | null {
  return typeof document === 'undefined' ? null : getComputedStyle(document.documentElement);
}

/** 读取以 px 为单位的 CSS 令牌（虚拟滚动需要数值）。取不到时返回 fallback。 */
export function readTokenPx(name: string, fallback: number): number {
  const raw = rootStyle()?.getPropertyValue(name).trim();
  const n = raw ? Number.parseFloat(raw) : Number.NaN;
  return Number.isFinite(n) ? n : fallback;
}

/** 读取以 ms 为单位的 CSS 令牌（如 --hold-danger-ms）。 */
export function readTokenMs(name: string, fallback: number): number {
  const raw = rootStyle()?.getPropertyValue(name).trim();
  if (!raw) return fallback;
  const n = Number.parseFloat(raw);
  if (!Number.isFinite(n)) return fallback;
  return raw.endsWith('ms') ? n : raw.endsWith('s') ? n * 1000 : n;
}
