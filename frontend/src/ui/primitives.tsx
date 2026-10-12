import type { ButtonHTMLAttributes, ReactNode } from 'react';

/** 视觉包装层：业务组件只用这些，换皮肤时整体替换本文件 + ui.css。 */
type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'default' | 'primary' | 'danger' | 'ghost' };

export function Button({ variant = 'default', className, type = 'button', ...rest }: BtnProps) {
  return <button type={type} className={`btn btn--${variant}${className ? ' ' + className : ''}`} {...rest} />;
}

export function Panel({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel${className ? ' ' + className : ''}`}>
      {title || actions ? (
        <header className="panel__head">
          <h3 className="panel__title">{title}</h3>
          <div className="panel__actions">{actions}</div>
        </header>
      ) : null}
      <div className="panel__body">{children}</div>
    </section>
  );
}

export function Badge({ tone = 'neutral', children }: { tone?: 'neutral' | 'success' | 'danger' | 'warning' | 'info' | 'accent'; children: ReactNode }) {
  return <span className={`badge badge--${tone}`}>{children}</span>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
    </label>
  );
}

export function Banner({ tone = 'info', children }: { tone?: 'info' | 'warning' | 'danger' | 'success'; children: ReactNode }) {
  return <div className={`banner banner--${tone}`} role={tone === 'danger' ? 'alert' : 'status'}>{children}</div>;
}

export function ProgressBar({ value, max }: { value: number; max: number | null }) {
  const pct = max ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="progress" role="progressbar" aria-valuenow={value} aria-valuemax={max ?? undefined}>
      <div className={`progress__bar${max ? '' : ' progress__bar--indeterminate'}`} style={max ? { width: `${pct}%` } : undefined} />
    </div>
  );
}
