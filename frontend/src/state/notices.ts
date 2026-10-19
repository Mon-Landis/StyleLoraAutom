import { create } from 'zustand';

/**
 * 全局通知（页面卸载后仍需可见的结果，如离开提示词页时的草稿保存失败）。
 * 只存内存；展示在 NoticeHost，存活时长取令牌 --toast-ttl（由 NoticeHost 读取）。
 */
export interface Notice {
  id: number;
  tone: 'info' | 'danger' | 'warning' | 'success';
  text: string;
}

interface NoticeStore {
  items: Notice[];
  push: (tone: Notice['tone'], text: string) => number;
  dismiss: (id: number) => void;
  reset: () => void;
}

const MAX_NOTICES = 4;
let seq = 0;

export const useNotices = create<NoticeStore>((set) => ({
  items: [],
  push: (tone, text) => {
    const id = ++seq;
    set((s) => ({ items: [...s.items, { id, tone, text }].slice(-MAX_NOTICES) }));
    return id;
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((n) => n.id !== id) })),
  reset: () => set({ items: [] }),
}));

export const notify = (tone: Notice['tone'], text: string): number => useNotices.getState().push(tone, text);
