import type { ComponentType } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getStats } from '../api/endpoints';
import { listPromptTasks, listSources } from '../api/more';
import { reviewCounts } from '../lib/reviewStats';
import { useApp } from '../state/app';
import { StatsLine } from '../ui/primitives';
import type { IconName } from '../ui/slots';

/** 工作台注册表：侧边栏列表与顶栏 statsSlot 都从这里取。新增页面只改这里。 */
export interface WorkbenchDef {
  id: string;
  path: string;
  label: string;
  icon: IconName;
  /** statsSlot：顶栏统计区渲染的组件；不提供则留空。 */
  Stats?: ComponentType;
}

function DatasetStats() {
  const pid = useApp((s) => s.projectId);
  const q = useQuery({
    queryKey: ['stats', pid],
    queryFn: () => getStats(pid!, {}),
    enabled: !!pid,
    refetchInterval: 5000, // TODO: 审图后主动失效，替代轮询
  });
  const c = reviewCounts(q.data);
  if (!c) return null;
  return <StatsLine items={[['总数', c.total], ['未审', c.unreviewed], ['保留', c.keep], ['剔除', c.reject]]} />;
}

function SourcesStats() {
  const pid = useApp((s) => s.projectId);
  const q = useQuery({ queryKey: ['sources', pid], queryFn: () => listSources(pid!), enabled: !!pid });
  if (!q.data) return null;
  const running = q.data.items.filter((s) => s.status === 'running').length;
  return <StatsLine items={[['数据源', q.data.items.length], ['运行中', running]]} />;
}

function PromptsStats() {
  const q = useQuery({ queryKey: ['prompt-tasks'], queryFn: listPromptTasks });
  if (!q.data) return null;
  const running = q.data.items.filter((t) => t.status === 'running').length;
  return <StatsLine items={[['任务', q.data.items.length], ['运行中', running]]} />;
}

export const WORKBENCHES: WorkbenchDef[] = [
  { id: 'dataset', path: '/workbench', label: '数据集工作台', icon: 'nav-workbench', Stats: DatasetStats },
  { id: 'sources', path: '/sources', label: '数据源', icon: 'nav-sources', Stats: SourcesStats },
  { id: 'prompts', path: '/prompts', label: '提示词组库', icon: 'nav-prompts', Stats: PromptsStats },
];

export function workbenchFor(pathname: string): WorkbenchDef | undefined {
  return WORKBENCHES.find((w) => pathname === w.path || pathname.startsWith(w.path + '/'));
}
