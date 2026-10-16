// NSFW 显示策略（F27 §4）：纯函数。开关只影响前端显示，后端不过滤。
import type { Rating } from '../api/types';
import type { ImageDecision, NsfwDisplayMode, SafetySettings } from '../api/promptGroupTypes';

export const DEFAULT_SAFETY: SafetySettings = { nsfw_enabled: false, nsfw_display_mode: 'click_reveal' };
export const DISPLAY_MODES: readonly NsfwDisplayMode[] = ['click_reveal', 'hover', 'show'];
export const DISPLAY_MODE_LABEL: Record<NsfwDisplayMode, string> = {
  click_reveal: '点击显示', hover: '悬停显示', show: '直接显示',
};

export const HINT_BLOCKED = 'nsfw 屏蔽';
export const HINT_REVEAL = 'nsfw 内容';

/** 敏感图谓词 S 的数据集部分：rating ∈ {questionable, explicit}。未评级不视为敏感也不宣称安全。 */
export function isSensitiveRating(rating: Rating | null | undefined): boolean {
  return rating === 'questionable' || rating === 'explicit';
}

/** 提示词组可见性：仅供前端列表/提示词源列表过滤。 */
export function canSelect(group: { nsfw: boolean }, settings: SafetySettings): boolean {
  return settings.nsfw_enabled || !group.nsfw;
}

/** 图像显示决策，状态表见 §4。focused = 鼠标悬停或键盘聚焦。 */
export function imagePolicy(sensitive: boolean, focused: boolean, settings: SafetySettings, revealed: boolean): ImageDecision {
  if (!sensitive) return { blur: false, hint: null, can_reveal: false };
  if (!settings.nsfw_enabled) return { blur: true, hint: focused ? HINT_BLOCKED : null, can_reveal: false };
  switch (settings.nsfw_display_mode) {
    case 'show':
      return { blur: false, hint: null, can_reveal: false };
    case 'hover':
      return { blur: !focused, hint: null, can_reveal: false };
    case 'click_reveal':
    default:
      return revealed
        ? { blur: false, hint: null, can_reveal: false }
        : { blur: true, hint: focused ? HINT_REVEAL : null, can_reveal: true };
  }
}
