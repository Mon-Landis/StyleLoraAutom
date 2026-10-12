import { useEffect } from 'react';
import { REVIEW_STATUSES, RATINGS, type ImageFilter, type Rating, type ReviewStatus } from '../api/types';
import { Button } from '../ui/primitives';

function toggle<T>(arr: T[] | undefined, v: T): T[] {
  const a = arr ?? [];
  return a.includes(v) ? a.filter((x) => x !== v) : [...a, v];
}

const RS_LABEL: Record<ReviewStatus, string> = { unreviewed: '未审', keep: '保留', reject: '剔除' };

export function FilterBar({ filter, onChange }: { filter: Partial<ImageFilter>; onChange: (f: Partial<ImageFilter>) => void }) {
  return (
    <div className="filterbar" role="group" aria-label="筛选">
      <span className="seg">
        {REVIEW_STATUSES.map((s) => (
          <Button key={s} variant={filter.review_status?.includes(s) ? 'primary' : 'default'}
            onClick={() => onChange({ ...filter, review_status: toggle(filter.review_status, s) })}>{RS_LABEL[s]}</Button>
        ))}
      </span>
      <span className="seg">
        {RATINGS.map((r: Rating) => (
          <Button key={r} variant={filter.rating?.includes(r) ? 'primary' : 'default'}
            onClick={() => onChange({ ...filter, rating: toggle(filter.rating, r) })}>{r}</Button>
        ))}
      </span>
      <label className="field__label">
        数据源 ID{' '}
        <input
          className="input-narrow"
          inputMode="numeric"
          value={(filter.source_id ?? []).join(',')}
          onChange={(e) => {
            const ids = e.target.value.split(',').map((x) => Number(x.trim())).filter((n) => Number.isInteger(n) && n > 0);
            onChange({ ...filter, source_id: ids });
          }}
        />
      </label>
      <Button variant="ghost" onClick={() => onChange({})}>清除</Button>
    </div>
  );
}

export interface ShortcutHandlers {
  move: (delta: number) => void;
  review: (s: ReviewStatus) => void;
  toggleSelect: () => void;
  selectAllLoaded: () => void;
  clearSelection: () => void;
}

/** 审图快捷键：K/X/U 审图，←→/J/K? 导航，Space 选择，Ctrl+A 全选已加载，Esc 清空。输入控件内不响应。 */
export function useReviewShortcuts(h: ShortcutHandlers) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (e.altKey || e.metaKey || e.defaultPrevented) return;
      const k = e.key.toLowerCase();
      if (e.ctrlKey) {
        if (k === 'a') { e.preventDefault(); h.selectAllLoaded(); }
        return;
      }
      if (k === 'arrowright' || k === 'd') h.move(1);
      else if (k === 'arrowleft' || k === 'a') h.move(-1);
      else if (k === 'k' || k === '1') h.review('keep');
      else if (k === 'x' || k === '2') h.review('reject');
      else if (k === 'u' || k === '0') h.review('unreviewed');
      else if (k === ' ') h.toggleSelect();
      else if (k === 'escape') h.clearSelection();
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [h]);
}
