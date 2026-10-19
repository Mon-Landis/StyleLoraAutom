import { useCallback, useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { GroupRef } from '../../api/promptGroupTypes';
import { hasGroupDrafts, leaveCoordinatorFor } from '../../lib/autoLeave';
import { hasAnyBaseDrafts } from '../../lib/baseDrafts';
import { installHistoryGuard } from '../../lib/historyGuard';
import { registerNavGuard } from '../../lib/navGuard';
import { notify } from '../../state/notices';
import { useVariantDrafts } from '../../state/variantDrafts';

/**
 * 提示词页的「离开触发」接线。F31 §7.3 起离开不再发布任何补齐任务，只处理草稿。
 * - leave(ref)：flush 草稿；返回 false 表示草稿保存失败，调用方应留在原处。
 * - 内部注册：应用内导航守卫（侧边栏跳转前 await）、浏览器前进/后退的 popstate 拦截（有草稿时先保存再放行）、
 *   页面卸载兜底（无草稿的历史导航等），以及有未保存草稿时的 beforeunload 提醒。
 * 协调器是模块级的，同一 ref 并发调用会合并。
 */
export function useAutoCompletion(selected: GroupRef | null): {
  leave: (ref: GroupRef) => Promise<boolean>;
} {
  const qc = useQueryClient();
  const coordinator = leaveCoordinatorFor(qc);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const leave = useCallback(async (ref: GroupRef): Promise<boolean> => {
    const r = await coordinator.leave(ref);
    return r.status !== 'blocked';
  }, [coordinator]);

  useEffect(() => {
    // 应用内离开提示词页：先 flush 草稿，再放行导航；草稿保存失败则留在本页。
    const unregister = registerNavGuard(async () => {
      const ref = selectedRef.current;
      if (!ref) return true;
      const ok = await leave(ref);
      if (!ok) notify('danger', '已留在提示词页：请先处理保存失败的草稿。');
      return ok;
    });

    // 浏览器前进/后退：有该组草稿时在页面卸载前拦截，保存成功才放行，失败留在本页（草稿保留）。
    const offHistory = installHistoryGuard({
      shouldGuard: () => { const ref = selectedRef.current; return !!ref && hasGroupDrafts(ref); },
      guard: async () => { const ref = selectedRef.current; return ref ? leave(ref) : true; },
      onBlocked: () => notify('danger', '已留在提示词页，草稿已保留：请先处理保存失败的草稿。'),
    });

    // 变体草稿 + 条目弹窗里未保存的基础字段（nl / aspect / disable）。
    const hasDrafts = () => Object.keys(useVariantDrafts.getState().drafts).length > 0 || hasAnyBaseDrafts();
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!hasDrafts()) return;
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => {
      unregister();
      offHistory();
      window.removeEventListener('beforeunload', onBeforeUnload);
      // 卸载兜底（无草稿的浏览器后退、守卫之外的导航）：尽力保存草稿。
      // 失败只通知，不阻塞卸载；草稿不会被清除（失败时提示「草稿已保留」）。
      const ref = selectedRef.current;
      if (ref) void coordinator.leave(ref);
    };
  }, [coordinator, leave]);

  return { leave };
}
