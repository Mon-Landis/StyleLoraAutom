import { useRef, type KeyboardEvent } from 'react';
import type { PromptView } from '../../api/promptGroupTypes';
import { Button } from '../../ui/primitives';

export const VIEW_PANEL_ID = 'pg-view-panel';
export const viewTabId = (v: PromptView) => `pg-view-tab-${v}`;

const VIEWS: ReadonlyArray<{ value: PromptView; label: string }> = [
  { value: 'prompt', label: '提示词' },
  { value: 'variants', label: '变体' },
];

/** 分段视图切换（tablist）：方向键/Home/End 移动并激活，roving tabindex。 */
export function PromptViewSwitch({ value, onChange }: { value: PromptView; onChange: (v: PromptView) => void }) {
  const refs = useRef<Partial<Record<PromptView, HTMLButtonElement | null>>>({});
  const onKeyDown = (e: KeyboardEvent, i: number) => {
    let next = i;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (i + 1) % VIEWS.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (i - 1 + VIEWS.length) % VIEWS.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = VIEWS.length - 1;
    else return;
    e.preventDefault();
    onChange(VIEWS[next].value);
    refs.current[VIEWS[next].value]?.focus();
  };
  return (
    <div className="seg" role="tablist" aria-label="提示词视图">
      {VIEWS.map((v, i) => {
        const selected = v.value === value;
        return (
          <Button
            key={v.value}
            ref={(el: HTMLButtonElement | null) => { refs.current[v.value] = el; }}
            id={viewTabId(v.value)}
            role="tab"
            variant={selected ? 'primary' : 'default'}
            aria-selected={selected}
            aria-controls={VIEW_PANEL_ID}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(v.value)}
            onKeyDown={(e) => onKeyDown(e, i)}
          >
            {v.label}
          </Button>
        );
      })}
    </div>
  );
}
