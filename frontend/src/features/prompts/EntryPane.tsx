import { useEffect, useRef, type KeyboardEvent } from 'react';
import type { Entry, GroupRef, RenderMode } from '../../api/promptGroupTypes';
import { arrowNav } from '../../lib/focusNav';
import { Badge, Button } from '../../ui/primitives';
import { Empty, Loading } from '../../ui/slots';
import { EntryBlockView } from './EntryBlockView';

type BindMenu = (e: Entry, onKey: (ev: KeyboardEvent) => void) => Record<string, unknown>;

export function EntryPane(p: {
  gref: GroupRef | null;
  title: string;
  total: number | null;
  nsfw: boolean;
  mode: RenderMode;
  onMode: (m: RenderMode) => void;
  entries: Entry[];
  loading: boolean;
  errorText: string | null;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  bindMenu: BindMenu;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);

  // 近底自动加载下一页；不重置已选条目与显示模式。
  useEffect(() => {
    if (!p.hasMore || p.loadingMore || typeof IntersectionObserver === 'undefined' || !sentinel.current) return;
    const io = new IntersectionObserver((rows) => { if (rows.some((r) => r.isIntersecting)) p.onLoadMore(); });
    io.observe(sentinel.current);
    return () => io.disconnect();
  }, [p.hasMore, p.loadingMore, p.onLoadMore, p.entries.length]);

  if (!p.gref) {
    return <section className="pg-mid" aria-label="条目内容"><Empty title="选择左侧词典" hint="或新建 / 导入一个词典。" /></section>;
  }
  return (
    <section className="pg-mid" aria-label="条目内容">
      <header className="pg-mid__head">
        <div className="pg-mid__title">
          <h2 className="pg-mid__name">{p.title}</h2>
          <span className="pg-muted">ID {p.gref.group_id}</span>
          <span className="pg-muted">共 {p.total ?? '…'} 条</span>
        </div>
        <div className="pg-mid__tools">
          {p.nsfw && <Badge tone="danger">NSFW</Badge>}
          <div className="seg" role="group" aria-label="显示模式">
            <Button variant={p.mode === 'tags_plus_nl' ? 'primary' : 'default'} aria-pressed={p.mode === 'tags_plus_nl'} onClick={() => p.onMode('tags_plus_nl')}>tag + nl</Button>
            <Button variant={p.mode === 'full_nl' ? 'primary' : 'default'} aria-pressed={p.mode === 'full_nl'} onClick={() => p.onMode('full_nl')}>完全 nl</Button>
          </div>
        </div>
      </header>
      <div ref={listRef} className="pg-mid__body" role="listbox" aria-label="条目" aria-live="off">
        {p.loading && <Loading />}
        {p.errorText && <div className="pg-muted" role="alert">{p.errorText}</div>}
        {!p.loading && !p.errorText && p.entries.length === 0 && <Empty title="词典为空" hint="使用右侧「添加元素」「追加自然语言」或「批量导入」。" />}
        {p.entries.map((e) => (
          <EntryBlockView
            key={e.entry_id}
            entry={e}
            mode={p.mode}
            gref={p.gref!}
            groupNsfw={p.nsfw}
            selected={p.selectedId === e.entry_id}
            onSelect={() => p.onSelect(e.entry_id)}
            menuProps={p.bindMenu(e, (ev) => {
              if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); p.onSelect(e.entry_id); }
              else arrowNav(ev, listRef.current, '[data-entry-id]');
            })}
          />
        ))}
        {p.hasMore && (
          <div ref={sentinel} className="pg-mid__more">
            <Button variant="ghost" disabled={p.loadingMore} onClick={p.onLoadMore}>{p.loadingMore ? '加载中…' : '加载更多'}</Button>
          </div>
        )}
      </div>
    </section>
  );
}
