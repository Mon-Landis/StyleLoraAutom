import { create } from 'zustand';
import type { ImageFilter } from '../api/types';

export type Theme = 'light' | 'dark';
export type Density = 'compact' | 'comfortable';
export type Stage = 'checker' | 'white' | 'gray' | 'black' | 'dark';

interface AppStore {
  theme: Theme;
  density: Density;
  stage: Stage;
  projectId: string | null;
  projectVersion: string | null;
  projectName: string;
  filter: Partial<ImageFilter>;
  selected: Set<number>;
  activeId: number | null;
  setTheme: (t: Theme) => void;
  setDensity: (d: Density) => void;
  setStage: (s: Stage) => void;
  setProject: (id: string | null, version?: string | null, name?: string) => void;
  setFilter: (f: Partial<ImageFilter>) => void;
  setActive: (id: number | null) => void;
  setSelected: (s: Set<number>) => void;
  toggleSelected: (id: number) => void;
}

export const useApp = create<AppStore>((set, get) => ({
  theme: 'light',
  density: 'compact',
  stage: 'checker',
  projectId: null,
  projectVersion: null,
  projectName: '',
  filter: {},
  selected: new Set(),
  activeId: null,
  setTheme: (theme) => set({ theme }),
  setDensity: (density) => set({ density }),
  setStage: (stage) => set({ stage }),
  setProject: (projectId, projectVersion = null, projectName = '') =>
    set({ projectId, projectVersion, projectName, selected: new Set(), activeId: null, filter: {} }),
  setFilter: (filter) => set({ filter, selected: new Set() }),
  setActive: (activeId) => set({ activeId }),
  setSelected: (selected) => set({ selected }),
  toggleSelected: (id) => {
    const next = new Set(get().selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    set({ selected: next });
  },
}));
