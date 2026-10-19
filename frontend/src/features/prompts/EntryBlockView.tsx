import { memo } from 'react';
import type { Entry, EntryPatch, GroupRef, PromptView } from '../../api/promptGroupTypes';
import { entryBlock } from '../../lib/promptGroupList';
import { isFieldGenerated, isFieldPending } from '../../lib/promptVariants';
import { refKey } from '../../api/promptGroups';
import { Badge } from '../../ui/primitives';
import { EntryPreview } from '../../ui/PreviewImage';
import { FieldMark } from './GeneratedField';
import { VariantEditor } from './VariantEditor';

/**
 * 单条块（v5）：提示词=只显示 nl 原文本；变体=条目摘要 + 变体编辑器。
 * 结构与交互在此，视觉在 css。
 */
export const EntryBlockView = memo(function EntryBlockView(p: {
  entry: Entry;
  view: PromptView;
  gref: GroupRef;
  groupNsfw: boolean;
  selected: boolean;
  menuProps: Record<string, unknown>;
  onSelect: () => void;
  onPatch: (entry: Entry, patch: EntryPatch) => Promise<void>;
}) {
  const e = p.entry;
  const b = entryBlock(e);
  const running = e.completion?.state === 'queued' || e.completion?.state === 'running';
  const label = `条目 ${b.nl}${e.disable ? '（已禁用）' : ''}`;
  const marks = (
    <div className="pg-entry__marks">
      {e.disable && <Badge tone="neutral">已禁用</Badge>}
      {b.statusLabel && <Badge tone={e.completion?.state === 'failed' ? 'danger' : running ? 'info' : 'warning'}>{b.statusLabel}</Badge>}
      {b.missingLabels.length > 0 && !running && <span className="pg-muted">缺：{b.missingLabels.join('、')}</span>}
    </div>
  );
  const thumb = e.preview && (
    <div className="pg-entry__thumb">
      <EntryPreview gref={p.gref} entryId={e.entry_id} sensitive={p.groupNsfw} preview={e.preview} />
    </div>
  );

  if (p.view === 'variants') {
    // 编辑器含输入控件：不能放进 role=option，键盘行为只挂在标题行上，避免吞掉文本框里的方向键/空格。
    return (
      <div className={`pg-entry pg-entry--variants${e.disable ? ' is-disabled' : ''}${p.selected ? ' is-selected' : ''}`} role="group" aria-label={label}>
        <div
          className="pg-entry__head"
          role="button"
          tabIndex={0}
          aria-pressed={p.selected}
          aria-label={`${label}，标题行`}
          data-entry-id={e.entry_id}
          onClick={p.onSelect}
          {...p.menuProps}
        >
          {thumb}
          <div className="pg-entry__tags">{b.nl || <span className="pg-muted">（无内容）</span>}</div>
        </div>
        {marks}
        <VariantEditor entry={e} draftScope={refKey(p.gref)} onPatch={(patch) => p.onPatch(e, patch)} />
      </div>
    );
  }

  const nlGen = isFieldGenerated(e, 'nl');
  const nlPending = isFieldPending(e, 'nl');
  return (
    <div
      className={`pg-entry${e.disable ? ' is-disabled' : ''}${p.selected ? ' is-selected' : ''}`}
      role="option"
      tabIndex={0}
      aria-selected={p.selected}
      aria-label={label}
      data-entry-id={e.entry_id}
      onClick={p.onSelect}
      {...p.menuProps}
    >
      {thumb}
      <div className="pg-entry__full">
        {b.nl || <span className="pg-muted">（无内容）</span>}
        <FieldMark generated={nlGen} pending={nlPending} />
      </div>
      {marks}
    </div>
  );
});
