import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useUi } from '../state/ui';
import { NavButtonItem, NavLinkItem, NavSub } from '../ui/Nav';
import { Icon } from '../ui/slots';
import { TOOLS, TOOLS_LABEL, toolFor } from './tools';
import { WORKBENCHES } from './workbenches';

export const PROJECT_INFO_PATH = '/project';
const TOOLS_MENU_ID = 'sidebar-tools-menu';

export function Sidebar() {
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const toggle = useUi((s) => s.toggleSidebar);
  const setCollapsed = useUi((s) => s.setSidebarCollapsed);
  const inTools = toolFor(useLocation().pathname) !== undefined;
  // 当前路由在工具箱下时默认展开；用户之后可手动收起。
  const [toolsOpen, setToolsOpen] = useState(inTools);
  useEffect(() => {
    if (inTools) setToolsOpen(true);
  }, [inTools]);

  // 折叠态（仅图标）没有放二级菜单的空间：点击 = 先展开侧边栏，并同时展开子菜单。
  const onToolsClick = () => {
    if (collapsed) {
      setCollapsed(false);
      setToolsOpen(true);
    } else setToolsOpen((v) => !v);
  };
  const expanded = toolsOpen && !collapsed;

  return (
    <aside className="sidebar" aria-label="侧边栏">
      <nav className="sidebar__list" aria-label="工作台">
        {WORKBENCHES.map((w) => (
          <NavLinkItem key={w.id} to={w.path} icon={w.icon} label={w.label} collapsed={collapsed} />
        ))}
        <NavButtonItem
          icon="nav-tools"
          label={TOOLS_LABEL}
          collapsed={collapsed}
          active={inTools}
          aria-label={TOOLS_LABEL}
          aria-expanded={expanded}
          aria-controls={TOOLS_MENU_ID}
          trailing={<Icon name={expanded ? 'chevron-down' : 'chevron-right'} />}
          onClick={onToolsClick}
        />
        <NavSub id={TOOLS_MENU_ID} label={TOOLS_LABEL} hidden={!expanded}>
          {TOOLS.map((t) => (
            <li key={t.id}>
              <NavLinkItem to={t.path} icon={t.icon} label={t.label} collapsed={collapsed} nested />
            </li>
          ))}
        </NavSub>
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
