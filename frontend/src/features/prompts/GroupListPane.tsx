import { useRef, type KeyboardEvent } from 'react';
import type { GroupRef, GroupSummary, Scope } from '../../api/promptGroupTypes';
import { arrowNav } from '../../lib/focusNav';
import { refOf, sameRef } from '../../lib/promptGroupList';
import { Badge, Button } from '../../ui/primitives';
import { Empty, Loading } from '../../ui/slots';

const KIND_LABEL: Record<string, string> = { prompts: '提示词' };

type BindMenu = (g: GroupSummary, onKey: (e: KeyboardEvent) => void) => Record<string, unknown>;

export function GroupListPane(p: {
  groups: GroupSummary[];
  loading: boolean;
  errorText: string | null;
  selected: GroupRef | null;
  projectId: string | null;
  query: string;
  onQuery: (v: string) => void;
  scope: Scope | 'all';
  onScope: (v: Scope | 'all') => void;
  nsfwEnabled: boolean;
  nsfwOnly: boolean;
  onNsfwOnly: (v: boolean) => void;
  onSelect: (g: GroupSummary) => void;
  onCreate: () => void;
  bindMenu: BindMenu;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  return (
    <aside className="pg-left" aria-label="词典导航">
      <div className="pg-left__top">
        <input
          type="search"
          aria-label="搜索词典"
          placeholder="搜索名称或 ID"
          value={p.query}
          onChange={(e) => p.onQuery(e.target.value)}
        />
        <div className="pg-left__filters" role="group" aria-label="过滤">
          <select aria-label="作用范围" value={p.scope} onChange={(e) => p.onScope(e.target.value as Scope | 'all')}>
            <option value="all">全部范围</option>
            <option value="project">仅项目</option>
            <option value="global">仅全局</option>
          </select>
          {/* NSFW 关闭时移除敏感筛选选项 */}
          {p.nsfwEnabled && (
            <label className="check">
              <input type="checkbox" checked={p.nsfwOnly} onChange={(e) => p.onNsfwOnly(e.target.checked)} /> 仅 nsfw
            </label>
          )}
        </div>
      </div>
      <div ref={listRef} className="pg-left__list" role="listbox" aria-label="已有词典">
        {p.loading && <Loading />}
        {p.errorText && <div className="pg-muted" role="alert">{p.errorText}</div>}
        {!p.loading && !p.errorText && p.groups.length === 0 && <Empty title="没有词典" hint="点击下方「新建词典」或批量导入。" />}
        {p.groups.map((g) => {
          const active = sameRef(refOf(g, p.projectId), p.selected);
          return (
            <div
              key={`${g.scope}:${g.dictionary_id}`}
              role="option"
              tabIndex={0}
              aria-selected={active}
              data-group-row
              className={`pg-group${active ? ' is-selected' : ''}`}
              onClick={() => p.onSelect(g)}
              {...p.bindMenu(g, (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); p.onSelect(g); }
                else arrowNav(e, listRef.current, '[data-group-row]');
              })}
            >
              <span className="pg-group__name">{g.name}</span>
              <span className="pg-group__meta">
                <Badge tone="neutral">{KIND_LABEL[g.kind] ?? g.kind}</Badge>
                <Badge tone={g.scope === 'project' ? 'accent' : 'neutral'}>{g.scope === 'project' ? '项目' : '全局'}</Badge>
                {g.nsfw && <Badge tone="danger">NSFW</Badge>}
                <span className="pg-muted">{g.entry_count} 条</span>
              </span>
            </div>
          );
        })}
      </div>
      <div className="pg-left__foot">
        <Button variant="primary" onClick={p.onCreate}>新建词典</Button>
      </div>
    </aside>
  );
}
