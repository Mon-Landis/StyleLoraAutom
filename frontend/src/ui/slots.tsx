import type { ComponentType, ReactNode } from 'react';

/**
 * 可替换槽位：图标 / 空状态 / 加载态 / 装饰。业务组件只引用名字，
 * 加精加工时调用 registerSlots() 替换实现，不改业务代码。默认实现只用文本字形，不含 SVG 字面量。
 */
export type IconName =
  | 'keep' | 'reject' | 'unreviewed' | 'search' | 'refresh' | 'close' | 'play'
  | 'undo' | 'export' | 'warn' | 'image' | 'prev' | 'next' | 'plus'
  | 'nav-workbench' | 'nav-sources' | 'nav-prompts' | 'settings' | 'project' | 'project-open'
  | 'sidebar-collapse' | 'sidebar-expand' | 'tasks' | 'theme';

const glyphs: Record<IconName, string> = {
  keep: '✓', reject: '✕', unreviewed: '○', search: '⌕', refresh: '↻', close: '×', play: '▶',
  undo: '↶', export: '⇪', warn: '!', image: '▣', prev: '‹', next: '›', plus: '+',
  'nav-workbench': '▦', 'nav-sources': '☰', 'nav-prompts': '✎', settings: '⚙', project: 'ⓘ', 'project-open': '▤',
  'sidebar-collapse': '«', 'sidebar-expand': '»', tasks: '≣', theme: '◐',
};

export interface SlotSet {
  Icon: ComponentType<{ name: IconName }>;
  Logo: ComponentType;
  Empty: ComponentType<{ title: string; hint?: ReactNode }>;
  Loading: ComponentType<{ label?: string }>;
  Unsupported: ComponentType<{ feature: string }>;
  Deco: ComponentType<{ kind: 'pixel' | 'halftone' | 'index'; text?: string }>;
}

export const slots: SlotSet = {
  Icon: ({ name }) => <span className="icon" aria-hidden="true">{glyphs[name]}</span>,
  Logo: () => <span className="logo" aria-hidden="true"><slots.Deco kind="pixel" /></span>,
  Empty: ({ title, hint }) => (
    <div className="empty" role="status">
      <div className="empty__title">{title}</div>
      {hint ? <div className="empty__hint">{hint}</div> : null}
    </div>
  ),
  Loading: ({ label }) => <div className="loading" role="status">{label ?? '加载中…'}</div>,
  Unsupported: ({ feature }) => (
    <div className="empty" role="status">
      <div className="empty__title">未实现</div>
      <div className="empty__hint">后端暂未提供「{feature}」（501）。</div>
    </div>
  ),
  // 装饰：像素方格 / 半调网点 / 等宽编号。全部 aria-hidden，纯 CSS 令牌驱动。
  Deco: ({ kind, text }) => (
    <span className={`deco deco--${kind}`} aria-hidden="true">{kind === 'index' ? text : null}</span>
  ),
};

export function registerSlots(next: Partial<SlotSet>): void {
  Object.assign(slots, next);
}

export const Icon = (p: { name: IconName }) => <slots.Icon {...p} />;
export const Logo = () => <slots.Logo />;
export const Empty = (p: { title: string; hint?: ReactNode }) => <slots.Empty {...p} />;
export const Loading = (p: { label?: string }) => <slots.Loading {...p} />;
export const Unsupported = (p: { feature: string }) => <slots.Unsupported {...p} />;
export const Deco = (p: { kind: 'pixel' | 'halftone' | 'index'; text?: string }) => <slots.Deco {...p} />;
