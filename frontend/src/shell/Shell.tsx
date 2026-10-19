import type { ReactNode } from 'react';
import { useUi } from '../state/ui';
import { useSafetySync } from '../features/useSafetySync';
import { NoticeHost } from './NoticeHost';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

/** 外壳：顶栏 + 侧边栏 + 内容。收起状态只通过 data-nav 属性驱动 CSS 变量，品牌区与侧边栏同宽。 */
export function Shell({ children }: { children: ReactNode }) {
  const collapsed = useUi((s) => s.sidebarCollapsed);
  useSafetySync();
  return (
    <div className="shell" data-nav={collapsed ? 'collapsed' : 'expanded'}>
      <TopBar />
      <div className="shell__body">
        <Sidebar />
        <main className="shell__main">{children}</main>
      </div>
      <NoticeHost />
    </div>
  );
}
