// 动效集中定义（与 styles/tokens/motion.css 同值）。时长/曲线只在此处和 motion.css 出现。
export const motion = {
  fastMs: 90,
  baseMs: 160,
  slowMs: 280,
  easeStandard: 'cubic-bezier(0.2, 0, 0, 1)',
  easeEnter: 'cubic-bezier(0, 0, 0, 1)',
  easeExit: 'cubic-bezier(0.3, 0, 1, 1)',
} as const;

export function prefersReducedMotion(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}
