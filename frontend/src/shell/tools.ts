import type { IconName } from '../ui/slots';

/** 工具箱注册表：侧边栏二级菜单从这里取。工具页不属于 WORKBENCHES（无顶栏统计）。新增工具只改这里 + App 路由。 */
export interface ToolDef {
  id: string;
  path: string;
  label: string;
  icon: IconName;
}

export const TOOLS_LABEL = '工具箱';

export const TOOLS: ToolDef[] = [
  { id: 'prompt-convert', path: '/tools/prompt-convert', label: '提示词转换', icon: 'tool-convert' },
];

export function toolFor(pathname: string): ToolDef | undefined {
  return TOOLS.find((t) => pathname === t.path || pathname.startsWith(t.path + '/'));
}
