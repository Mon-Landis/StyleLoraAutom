import { create } from 'zustand';
import { DEFAULT_SAFETY } from '../lib/nsfw';
import type { SafetySettings } from '../api/promptGroupTypes';

/**
 * NSFW 显示状态（F27 §4）。只存内存：不进 localStorage / 配置。
 * revealed 对应后端 revealed_this_run；关闭 NSFW 开关不清除它（再开启按原状态显示，Q01）。
 * policyVersion 在任一策略输入变化时递增，用作图像元素 key，强制丢弃已解码的清晰图。
 */
interface SafetyStore {
  settings: SafetySettings;
  /** 设置 CAS 令牌（强 ETag）。 */
  etag: string | null;
  loaded: boolean;
  revealed: boolean;
  policyVersion: number;
  applySettings: (s: Partial<SafetySettings>, etag?: string | null) => void;
  markRevealed: () => void;
  /** 与后端 revealed_this_run 对齐（多窗口共享、刷新不重置）。只升不降：本窗口已确认的不被迟到的旧响应覆盖。 */
  syncRevealed: (serverValue: boolean | undefined) => void;
}

export const useSafety = create<SafetyStore>((set, get) => ({
  settings: DEFAULT_SAFETY,
  etag: null,
  loaded: false,
  revealed: false,
  policyVersion: 0,
  applySettings: (s, etag) => {
    const cur = get().settings;
    const next: SafetySettings = {
      nsfw_enabled: s.nsfw_enabled ?? cur.nsfw_enabled,
      nsfw_display_mode: s.nsfw_display_mode ?? cur.nsfw_display_mode,
    };
    const changed = next.nsfw_enabled !== cur.nsfw_enabled || next.nsfw_display_mode !== cur.nsfw_display_mode;
    set({
      settings: next,
      etag: etag === undefined ? get().etag : etag,
      loaded: true,
      policyVersion: changed ? get().policyVersion + 1 : get().policyVersion,
    });
  },
  markRevealed: () => {
    if (get().revealed) return;
    set({ revealed: true, policyVersion: get().policyVersion + 1 });
  },
  syncRevealed: (serverValue) => {
    if (serverValue === true) get().markRevealed();
  },
}));
