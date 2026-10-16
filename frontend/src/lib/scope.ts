import type { Scope } from '../api/promptGroupTypes';

/** 创建/导入的默认 scope（F27 §3）：未开项目 global，已开项目 project；用户选择优先。 */
export function defaultScope(projectId: string | null | undefined, userChoice?: Scope | null): Scope {
  if (userChoice) return userChoice;
  return projectId ? 'project' : 'global';
}

/** 指定 project 但未开项目时不可提交（§5.1）。 */
export function scopeBlocked(scope: Scope, projectId: string | null | undefined): boolean {
  return scope === 'project' && !projectId;
}
