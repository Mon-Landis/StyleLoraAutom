import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { holdProgress, type HoldKind } from '../lib/hold';
import { readTokenMs } from '../tokens';

interface Props {
  kind?: HoldKind;
  onConfirm: () => void;
  children: ReactNode;
  disabled?: boolean;
  /** 测试注入；默认读令牌 --hold-danger-ms / --hold-secondary-ms。 */
  durationMs?: number;
}

/** 长按确认：按住达到令牌时长才触发；松开/移出/Esc 取消。键盘：按住 Space/Enter。 */
export function HoldToConfirm({ kind = 'danger', onConfirm, children, disabled, durationMs }: Props) {
  const [p, setP] = useState(0);
  const raf = useRef(0);
  const start = useRef(0);
  const dur = useRef(0);

  const stop = useCallback(() => {
    cancelAnimationFrame(raf.current);
    start.current = 0;
    setP(0);
  }, []);

  const tick = useCallback(() => {
    const prog = holdProgress(performance.now() - start.current, dur.current);
    setP(prog);
    if (prog >= 1) {
      start.current = 0;
      setP(0);
      onConfirm();
    } else raf.current = requestAnimationFrame(tick);
  }, [onConfirm]);

  const begin = () => {
    if (disabled || start.current) return;
    dur.current = durationMs ?? readTokenMs(kind === 'danger' ? '--hold-danger-ms' : '--hold-secondary-ms', kind === 'danger' ? 1200 : 800);
    start.current = performance.now();
    raf.current = requestAnimationFrame(tick);
  };

  useEffect(() => () => cancelAnimationFrame(raf.current), []);

  return (
    <button
      type="button"
      className={`btn btn--${kind} hold`}
      disabled={disabled}
      data-holding={p > 0 ? 'true' : undefined}
      style={{ ['--hold-progress' as string]: p }}
      onPointerDown={begin}
      onPointerUp={stop}
      onPointerLeave={stop}
      onPointerCancel={stop}
      onBlur={stop}
      onKeyDown={(e) => {
        if (e.key === ' ' || e.key === 'Enter') {
          e.preventDefault();
          if (!e.repeat) begin();
        } else if (e.key === 'Escape') stop();
      }}
      onKeyUp={(e) => (e.key === ' ' || e.key === 'Enter') && stop()}
    >
      <span className="hold__fill" aria-hidden="true" />
      <span className="hold__label">{children}</span>
    </button>
  );
}
