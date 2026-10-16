import { useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getSettings, patchSettings, revealSession } from '../api/safety';
import type { NsfwDisplayMode } from '../api/promptGroupTypes';
import { useSafety } from '../state/safety';

export const SETTINGS_KEY = ['settings'] as const;

/** 在外壳里挂一次：把后端设置同步进 safety store。读取失败时保持默认（NSFW 关）。 */
export function useSafetySync(): void {
  const q = useQuery({ queryKey: SETTINGS_KEY, queryFn: getSettings });
  useEffect(() => {
    if (!q.data) return;
    const d = q.data.data;
    useSafety.getState().applySettings(
      { nsfw_enabled: d.nsfw_enabled, nsfw_display_mode: d.nsfw_display_mode },
      q.data.etag,
    );
    useSafety.getState().syncRevealed(d.revealed_this_run);
  }, [q.data]);
}

/** 设置页用：改 NSFW 开关 / 显示模式。先本地生效（立即重算模糊），失败则回读服务端。 */
export function useSafetyMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (patch: { nsfw_enabled?: boolean; nsfw_display_mode?: NsfwDisplayMode }) => {
      const etag = useSafety.getState().etag;
      if (!etag) throw new Error('settings:not_loaded');
      return patchSettings(patch, etag);
    },
    onMutate: (patch) => useSafety.getState().applySettings(patch),
    onSuccess: (r) => {
      const d = r.data;
      useSafety.getState().applySettings({ nsfw_enabled: d.nsfw_enabled, nsfw_display_mode: d.nsfw_display_mode }, r.etag);
      void qc.invalidateQueries({ queryKey: SETTINGS_KEY });
    },
    onError: () => void qc.invalidateQueries({ queryKey: SETTINGS_KEY }),
  });
}

/** 点击「显示」：先本地确认（立即清晰），再通知后端；失败不回滚本地（仅内存会话状态）。 */
export async function confirmReveal(): Promise<void> {
  useSafety.getState().markRevealed();
  try {
    await revealSession();
  } catch {
    /* 后端不可达时仍保持本窗口已确认；TODO: 契约确认是否需要回滚 */
  }
}
