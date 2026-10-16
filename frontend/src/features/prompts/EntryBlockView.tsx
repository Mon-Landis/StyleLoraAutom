import { memo } from 'react';
import type { Entry, GroupRef, RenderMode } from '../../api/promptGroupTypes';
import { entryBlock } from '../../lib/promptGroupList';
import { Badge } from '../../ui/primitives';
import { EntryPreview } from '../../ui/PreviewImage';

/** 单条块：左 tag、右 expand_nl + nl（full_nl 模式整块显示 nl）。结构与交互在此，视觉在 css。 */
export const EntryBlockView = memo(function EntryBlockView(p: {
  entry: Entry;
  mode: RenderMode;
  gref: GroupRef;
  groupNsfw: boolean;
  selected: boolean;
  menuProps: Record<string, unknown>;
  onSelect: () => void;
}) {
  const e = p.entry;
  const b = entryBlock(e, p.mode);
  const running = e.completion?.state === 'queued' || e.completion?.state === 'running';
  return (
    <div
      className={`pg-entry${e.disable ? ' is-disabled' : ''}${p.selected ? ' is-selected' : ''}`}
      role="option"
      tabIndex={0}
      aria-selected={p.selected}
      aria-label={`条目 ${b.tagText || b.nl || b.expandNl}${e.disable ? '（已禁用）' : ''}`}
      data-entry-id={e.entry_id}
      onClick={p.onSelect}
      {...p.menuProps}
    >
      {e.preview && (
        <div className="pg-entry__thumb">
          <EntryPreview gref={p.gref} entryId={e.entry_id} sensitive={p.groupNsfw} preview={e.preview} />
        </div>
      )}
      {p.mode === 'tags_plus_nl' ? (
        <>
          <div className="pg-entry__tags">{b.tagText || <span className="pg-muted">（无 tag）</span>}</div>
          <div className="pg-entry__nl">
            {b.expandNl && <p className="pg-entry__seg"><span className="pg-entry__seglabel">追加</span>{b.expandNl}</p>}
            {b.nl && <p className="pg-entry__seg"><span className="pg-entry__seglabel">自然语言</span>{b.nl}</p>}
            {!b.expandNl && !b.nl && <span className="pg-muted">（无自然语言）</span>}
          </div>
        </>
      ) : (
        <div className="pg-entry__full">
          {b.missingNl ? <span className="pg-muted">待补全：缺少自然语言</span> : b.nl}
        </div>
      )}
      <div className="pg-entry__marks">
        {e.disable && <Badge tone="neutral">已禁用</Badge>}
        {b.statusLabel && <Badge tone={e.completion?.state === 'failed' ? 'danger' : running ? 'info' : 'warning'}>{b.statusLabel}</Badge>}
        {b.missingLabels.length > 0 && !running && <span className="pg-muted">缺：{b.missingLabels.join('、')}</span>}
      </div>
    </div>
  );
});
