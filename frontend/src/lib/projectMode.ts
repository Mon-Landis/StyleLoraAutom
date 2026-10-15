/** 项目类型：mode -> 显示字样。后端 metadata.mode 尚未提供，缺省按 style。 */
export type ProjectMode = 'style' | 'character' | 'action' | 'object';

export const MODE_LABEL: Record<ProjectMode, string> = {
  style: '画风模式',
  character: '角色模式',
  action: '动作模式',
  object: '物体模式',
};

export const DEFAULT_MODE: ProjectMode = 'style';

export function modeLabel(mode?: string | null): string {
  if (!mode) return MODE_LABEL[DEFAULT_MODE];
  return mode in MODE_LABEL ? MODE_LABEL[mode as ProjectMode] : mode;
}
