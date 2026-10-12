import { useMemo, useState } from 'react';
import { useApp } from '../state/app';
import { useImages } from '../features/useImages';
import { useReview } from '../features/useReview';
import { ThumbGrid } from '../features/ThumbGrid';
import { Preview, StageSwitch } from '../features/Preview';
import { AnnotationPanel } from '../features/AnnotationPanel';
import { RuleReplace } from '../features/RuleReplace';
import { ExecutorPanel } from '../features/ExecutorPanel';
import { ExportPanel } from '../features/ExportPanel';
import { FilterBar, useReviewShortcuts } from '../features/FilterBar';
import { Badge, Button, Panel } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Empty, Loading } from '../ui/slots';
import type { ImageItem } from '../api/types';

export function Workbench() {
  const { projectId: pid, filter, selected, activeId, setFilter, setActive, setSelected, toggleSelected } = useApp();
  const [around, setAround] = useState<number | undefined>(undefined);
  const [aroundText, setAroundText] = useState('');
  const imgs = useImages(pid, filter, around);
  const { items } = imgs;
  const review = useReview(pid, items, selected, activeId, imgs.patch);
  const activeIdx = items.findIndex((i) => i.image_id === activeId);
  const active: ImageItem | undefined = activeIdx >= 0 ? items[activeIdx] : undefined;

  const handlers = useMemo(
    () => ({
      move: (d: number) => {
        if (!items.length) return;
        const base = activeIdx < 0 ? 0 : Math.min(items.length - 1, Math.max(0, activeIdx + d));
        setActive(items[base].image_id);
      },
      review: (s: 'keep' | 'reject' | 'unreviewed') => void review.apply(s),
      toggleSelect: () => activeId != null && toggleSelected(activeId),
      selectAllLoaded: () => setSelected(new Set(items.map((i) => i.image_id))),
      clearSelection: () => setSelected(new Set()),
    }),
    [items, activeIdx, activeId, review.apply, setActive, setSelected, toggleSelected],
  );
  useReviewShortcuts(handlers);

  const onClickItem = (item: ImageItem, e: React.MouseEvent) => {
    if (e.shiftKey && activeIdx >= 0) {
      const to = items.findIndex((i) => i.image_id === item.image_id);
      const [a, b] = activeIdx < to ? [activeIdx, to] : [to, activeIdx];
      setSelected(new Set(items.slice(a, b + 1).map((i) => i.image_id)));
    } else if (e.ctrlKey || e.metaKey) {
      toggleSelected(item.image_id);
    } else {
      setActive(item.image_id);
    }
  };

  if (!pid) return <div className="page"><Empty title="尚未打开项目" hint="使用顶栏的「打开」或「新建」选择项目目录。" /></div>;
  const selIds = [...selected];

  return (
    <div className="workbench">
      <section className="workbench__list">
        <FilterBar filter={filter} onChange={setFilter} />
        <div className="toolbar">
          <Badge tone="neutral">共 {imgs.total ?? items.length} / 已载入 {items.length}</Badge>
          <Badge tone={selected.size ? 'accent' : 'neutral'}>选中 {selected.size}</Badge>
          <Button onClick={() => void review.apply('keep')} disabled={review.busy}>保留 (K)</Button>
          <Button onClick={() => void review.apply('reject')} disabled={review.busy}>剔除 (X)</Button>
          <Button onClick={() => void review.apply('unreviewed')} disabled={review.busy}>未审 (U)</Button>
          <input
            className="input-narrow"
            placeholder="定位 ID"
            value={aroundText}
            onChange={(e) => setAroundText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== 'Enter') return;
              const n = Number(aroundText);
              setAround(Number.isInteger(n) && n > 0 ? n : undefined);
              if (Number.isInteger(n) && n > 0) setActive(n);
            }}
          />
        </div>
        {review.error != null && <ErrorView error={review.error} feature="审图" />}
        {imgs.error && <ErrorView error={imgs.error} feature="图片列表" />}
        {imgs.isLoading ? <Loading /> : items.length === 0 ? <Empty title="没有符合条件的图片" /> : (
          <ThumbGrid
            items={items}
            activeId={activeId}
            selected={selected}
            hasMore={!!imgs.hasNextPage}
            loadingMore={imgs.isFetchingNextPage}
            onLoadMore={() => void imgs.fetchNextPage()}
            onClickItem={onClickItem}
            onMove={setActive}
          />
        )}
      </section>
      <aside className="workbench__side">
        <Panel title="预览" actions={<StageSwitch />}>
          {active
            ? <Preview item={active} onPrev={() => handlers.move(-1)} onNext={() => handlers.move(1)} />
            : <Empty title="选择一张图片" />}
        </Panel>
        {active && <Panel title="标注"><AnnotationPanel pid={pid} imageId={active.image_id} /></Panel>}
        <RuleReplace pid={pid} selectedIds={selIds} />
        <ExecutorPanel pid={pid} selectedIds={selIds} />
        <ExportPanel pid={pid} />
      </aside>
    </div>
  );
}
