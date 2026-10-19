/**
 * 应用内导航守卫。页面（如提示词页）注册一个异步守卫，侧边栏等导航入口在跳转前先 await 它；
 * 守卫返回 false 则留在当前页。HashRouter 不是 data router，不能用 useBlocker，所以走这里。
 * 浏览器前进/后退不经过这里：由 historyGuard.ts 在 popstate 捕获阶段拦截（有草稿时先恢复 URL、
 * 保存成功后再放行）；无草稿时直接离开，由页面卸载时的离开协调器尽力发布。
 */
export type NavGuard = () => Promise<boolean> | boolean;

const guards = new Set<NavGuard>();

export function registerNavGuard(g: NavGuard): () => void {
  guards.add(g);
  return () => { guards.delete(g); };
}
export const hasNavGuards = (): boolean => guards.size > 0;

export async function runNavGuards(): Promise<boolean> {
  for (const g of [...guards]) if (!(await g())) return false;
  return true;
}
