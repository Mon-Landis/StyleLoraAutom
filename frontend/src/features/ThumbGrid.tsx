import { memo, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ImageItem } from '../api/types';
import { useBlobUrl } from '../lib/useBlobUrl';
import { computeWindow, scrollTopFor } from '../lib/virtual';
import { readTokenPx } from '../tokens';
import { Icon } from '../ui/slots';

const Thumb = memo(function Thumb(p: {
  item: ImageItem; active: boolean; selected: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  const { url } = useBlobUrl(p.item.thumbnail_url ?? p.item.original_url);
  return (
    <div
      className={`thumb thumb--${p.item.review_status}${p.active ? ' is-active' : ''}${p.selected ? ' is-selected' : ''}`}
      onClick={p.onClick}
      role="option"
      aria-selected={p.selected}
      aria-label={`${p.item.filename ?? p.item.image_id} ${p.item.review_status}`}
    >
      {url ? <img src={url} alt="" draggable={false} /> : <span className="thumb__ph" aria-hidden="true" />}
      <span className="thumb__status"><Icon name={p.item.review_status === 'keep' ? 'keep' : p.item.review_status === 'reject' ? 'reject' : 'unreviewed'} /></span>
      {p.item.file_missing && <span className="thumb__warn"><Icon name="warn" /></span>}
    </div>
  );
});

export interface GridProps {
  items: ImageItem[];
  activeId: number | null;
  selected: Set<number>;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onClickItem: (item: ImageItem, e: React.MouseEvent) => void;
  onMove: (id: number) => void;
}

export function ThumbGrid(p: GridProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [top, setTop] = useState(0);
  const cellMin = readTokenPx('--thumb-min', 120);
  const gap = readTokenPx('--space-2', 8);
  const win = computeWindow({ width: size.w, height: size.h, scrollTop: top, cellMin, cellHeight: cellMin, gap, count: p.items.length });

  useLayoutEffect(() => {
    const el = ref.current!;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (p.hasMore && !p.loadingMore && win.lastIndex >= p.items.length - win.cols * 3) p.onLoadMore();
  }, [win.lastIndex, win.cols, p]);

  const cellW = (size.w - gap * (win.cols - 1)) / win.cols;
  const activeIdx = p.items.findIndex((i) => i.image_id === p.activeId);

  useEffect(() => {
    if (activeIdx < 0 || !ref.current) return;
    const next = scrollTopFor(activeIdx, win.cols, win.rowHeight, size.h, ref.current.scrollTop);
    if (next !== ref.current.scrollTop) ref.current.scrollTop = next;
  }, [activeIdx, win.cols, win.rowHeight, size.h]);

  const onKey = (e: React.KeyboardEvent) => {
    const d = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -win.cols, ArrowDown: win.cols }[e.key];
    if (d === undefined || !p.items.length) return;
    e.preventDefault();
    const idx = Math.min(p.items.length - 1, Math.max(0, (activeIdx < 0 ? 0 : activeIdx) + (activeIdx < 0 ? 0 : d)));
    p.onMove(p.items[idx].image_id);
  };

  return (
    <div ref={ref} className="grid" role="listbox" aria-multiselectable="true" tabIndex={0} onKeyDown={onKey} onScroll={(e) => setTop(e.currentTarget.scrollTop)}>
      <div style={{ height: win.totalHeight, position: 'relative' }}>
        {p.items.slice(win.firstIndex, win.lastIndex).map((item, k) => {
          const idx = win.firstIndex + k;
          return (
            <div key={item.image_id} className="grid__cell" style={{ width: cellW, height: cellMin, transform: `translate(${(idx % win.cols) * (cellW + gap)}px, ${Math.floor(idx / win.cols) * win.rowHeight}px)` }}>
              <Thumb item={item} active={item.image_id === p.activeId} selected={p.selected.has(item.image_id)} onClick={(e) => p.onClickItem(item, e)} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
