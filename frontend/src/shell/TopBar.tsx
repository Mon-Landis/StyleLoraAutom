import { NavLink } from 'react-router-dom';
import { useConnection, type ConnState } from '../state/connection';
import { useApp } from '../state/app';
import { Deco } from '../ui/slots';
import { Button } from '../ui/primitives';
import { ProjectBar } from './ProjectBar';

const LABEL: Record<ConnState, string> = {
  booting: '连接中',
  online: '在线',
  degraded: '推送断开',
  offline: '离线',
  reauth: '需重新引导',
  draining: '后端关闭中',
};
const TONE: Record<ConnState, string> = {
  booting: 'info', online: 'success', degraded: 'warning', offline: 'danger', reauth: 'danger', draining: 'warning',
};

export function TopBar() {
  const conn = useConnection((s) => s.state);
  const theme = useApp((s) => s.theme);
  const setTheme = useApp((s) => s.setTheme);
  const density = useApp((s) => s.density);
  const setDensity = useApp((s) => s.setDensity);
  return (
    <header className="topbar">
      <span className="topbar__brand">StyleLora</span>
      <Deco kind="pixel" />
      <nav className="topbar__nav" aria-label="主导航">
        <NavLink to="/workbench">工作台</NavLink>
        <NavLink to="/sources">数据源</NavLink>
        <NavLink to="/prompts">提示词</NavLink>
        <NavLink to="/settings">设置</NavLink>
      </nav>
      <ProjectBar />
      <span className="topbar__spacer" />
      <Button variant="ghost" onClick={() => setDensity(density === 'compact' ? 'comfortable' : 'compact')}>
        {density === 'compact' ? '紧凑' : '舒适'}
      </Button>
      <Button variant="ghost" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>
        {theme === 'light' ? '浅色' : '深色'}
      </Button>
      <span className="conn" role="status" aria-live="polite" title={LABEL[conn]}>
        <span className={`conn__dot conn__dot--${TONE[conn]}`} aria-hidden="true" />
        {LABEL[conn]}
      </span>
    </header>
  );
}
