import type { ButtonHTMLAttributes, MouseEvent, ReactNode } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { hasNavGuards, runNavGuards } from '../lib/navGuard';
import { Icon, type IconName } from './slots';

/** 侧边栏条目的视觉包装。收起态：文字隐藏（hidden 属性），aria-label 常驻，title 补充悬浮提示。 */
interface Common {
  icon: IconName;
  label: string;
  collapsed: boolean;
}

export function NavLinkItem({ to, icon, label, collapsed, nested }: Common & { to: string; nested?: boolean }) {
  const navigate = useNavigate();
  const here = useLocation().pathname;
  // 当前页注册了离开守卫（如提示词页）时：先 await 守卫（flush 草稿 + 发布），放行后再导航。
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (to === here || !hasNavGuards()) return;
    e.preventDefault();
    void runNavGuards().then((ok) => { if (ok) navigate(to); });
  };
  return (
    <NavLink to={to} onClick={onClick} className={`nav-item${nested ? ' nav-item--nested' : ''}`} aria-label={label} title={collapsed ? label : undefined}>
      <Icon name={icon} />
      <span className="nav-item__label" hidden={collapsed}>{label}</span>
    </NavLink>
  );
}

type ButtonProps = Common & Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> & {
  /** 分组项高亮（当前路由落在其子项下）。 */
  active?: boolean;
  /** 标签后的附加内容（如展开箭头）；收起态随标签一起隐藏。 */
  trailing?: ReactNode;
};

export function NavButtonItem({ icon, label, collapsed, active, trailing, ...rest }: ButtonProps) {
  return (
    <button type="button" className={`nav-item${active ? ' active' : ''}`} title={collapsed ? (rest['aria-label'] ?? label) : undefined} {...rest}>
      <Icon name={icon} />
      <span className="nav-item__label" hidden={collapsed}>{label}</span>
      {trailing ? <span className="nav-item__trail" hidden={collapsed}>{trailing}</span> : null}
    </button>
  );
}

/** 二级菜单容器。 */
export function NavSub({ id, hidden, label, children }: { id: string; hidden: boolean; label: string; children: ReactNode }) {
  return <ul id={id} className="nav-sub" aria-label={label} hidden={hidden}>{children}</ul>;
}
