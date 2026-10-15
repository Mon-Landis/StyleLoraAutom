import type { ButtonHTMLAttributes } from 'react';
import { NavLink } from 'react-router-dom';
import { Icon, type IconName } from './slots';

/** 侧边栏条目的视觉包装。收起态：文字隐藏（hidden 属性），aria-label 常驻，title 补充悬浮提示。 */
interface Common {
  icon: IconName;
  label: string;
  collapsed: boolean;
}

export function NavLinkItem({ to, icon, label, collapsed }: Common & { to: string }) {
  return (
    <NavLink to={to} className="nav-item" aria-label={label} title={collapsed ? label : undefined}>
      <Icon name={icon} />
      <span className="nav-item__label" hidden={collapsed}>{label}</span>
    </NavLink>
  );
}

export function NavButtonItem({ icon, label, collapsed, ...rest }: Common & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" className="nav-item" title={collapsed ? (rest['aria-label'] ?? label) : undefined} {...rest}>
      <Icon name={icon} />
      <span className="nav-item__label" hidden={collapsed}>{label}</span>
    </button>
  );
}
