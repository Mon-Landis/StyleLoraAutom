import { create } from 'zustand';

/** 在途写请求（非 GET）计数；由 api/client 的 hooks.onWrite 驱动。 */
interface WritesStore {
  pending: number;
  bump: (delta: 1 | -1) => void;
}

export const usePendingWrites = create<WritesStore>((set, get) => ({
  pending: 0,
  bump: (delta) => set({ pending: Math.max(0, get().pending + delta) }),
}));
