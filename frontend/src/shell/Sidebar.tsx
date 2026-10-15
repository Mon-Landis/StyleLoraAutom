import { useUi } from '../state/ui';
import { NavButtonItem, NavLinkItem } from '../ui/Nav';
import { WORKBENCHES } from './workbenches';

export const PROJECT_INFO_PATH = '/project';

export function Sidebar() {
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const toggle = useUi((s) => s.toggleSidebar);
  return (
    <aside className="sidebar" aria-label="侧边栏">
      <nav className="sidebar__list" aria-label="工作台">
        {WORKBENCHES.map((w) => (
          <NavLinkItem key={w.id} to={w.path} icon={w.icon} label={w.label} collapsed={collapsed} />
        ))}
      </nav>
      <div className="sidebar__foot">
        <NavLinkItem to="/settings" icon="settings" label="设置" collapsed={collapsed} />
        {/* TODO(backend): 暂无「打开项目目录」接口，先导航到项目信息占位页 */}
        <NavLinkItem to={PROJECT_INFO_PATH} icon="project" label="项目信息" collapsed={collapsed} />
        <NavButtonItem
          icon={collapsed ? 'sidebar-expand' : 'sidebar-collapse'}
          label="收起"
          collapsed={collapsed}
          aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
          aria-expanded={!collapsed}
          onClick={toggle}
        />
      </div>
    </aside>
  );
}
