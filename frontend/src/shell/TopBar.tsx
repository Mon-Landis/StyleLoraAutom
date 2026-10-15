import { useLocation } from 'react-router-dom';
import { modeLabel } from '../lib/projectMode';
import { useApp } from '../state/app';
import { useUi } from '../state/ui';
import { Badge } from '../ui/primitives';
import { Logo } from '../ui/slots';
import { ProjectBar } from './ProjectBar';
import { TaskTray } from './TaskTray';
import { workbenchFor } from './workbenches';

/** 顶栏（左→右）：品牌区 | 项目块 | 项目类型 | 统计区(statsSlot) | …弹性空白… | 任务托盘 */
export function TopBar() {
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const hasProject = useApp((s) => s.projectId != null);
  const mode = useApp((s) => s.projectMode);
  const Stats = workbenchFor(useLocation().pathname)?.Stats;
  return (
    <header className="topbar">
      {/* 宽度 = var(--shell-nav-w)，与侧边栏同源联动 */}
      <div className="topbar__brand">
        <Logo />
        <span className="topbar__name" hidden={collapsed}>StyleLora</span>
      </div>
      <ProjectBar />
      {/* TODO(backend): metadata.mode 尚未提供，缺省显示「画风模式」 */}
      {hasProject && <Badge tone="accent">{modeLabel(mode)}</Badge>}
      <div className="topbar__stats" aria-label="统计">{Stats ? <Stats /> : null}</div>
      <span className="topbar__spacer" />
      <TaskTray />
    </header>
  );
}
