import { create } from 'zustand';
import { persist } from 'zustand/middleware';

/**
 * 纯 UI 偏好（可持久化到 localStorage）。注意：token、项目数据一律不进这里。
 * partialize 白名单保证以后新增的运行态字段不会被误存。
 */
export const UI_STORAGE_KEY = 'stylelora.ui';

interface UiStore {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (v: boolean) => void;
  toggleSidebar: () => void;
}

export const useUi = create<UiStore>()(
  persist(
    (set, get) => ({
      sidebarCollapsed: false,
      setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
      toggleSidebar: () => set({ sidebarCollapsed: !get().sidebarCollapsed }),
    }),
    {
      name: UI_STORAGE_KEY,
      version: 1,
      partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed }),
    },
  ),
);
