import { useCallback, useEffect, useMemo, useState } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteEntry, deleteGroup, enqueueCompletion, exportEntry, exportGroupJson, getGroup, listEntries, listGroups,
  patchEntry, refKey,
} from '../api/promptGroups';
import type { Entry, GroupRef, GroupSummary, RenderMode, Scope } from '../api/promptGroupTypes';
import { useApp } from '../state/app';
import { useSafety } from '../state/safety';
import { describePgError } from '../lib/promptErrors';
import { downloadJson, safeFileName } from '../lib/download';
import { EMPTY_FILTER, refOf, sameRef, visibleGroups } from '../lib/promptGroupList';
import { entryMenuItems, groupMenuItems } from '../lib/promptMenus';
import { useDebounced } from '../lib/useDebounced';
import { ContextMenu, useContextMenu } from '../ui/ContextMenu';
import { Banner } from '../ui/primitives';
import { ControlPane, type ControlAction } from '../features/prompts/ControlPane';
import { EntryDialog, type EntryDialogMode } from '../features/prompts/EntryDialog';
import { EntryPane } from '../features/prompts/EntryPane';
import { GenerateDialog } from '../features/prompts/GenerateDialog';
import { GroupCreateDialog, GroupEditDialog } from '../features/prompts/GroupDialogs';
import { GroupListPane } from '../features/prompts/GroupListPane';
import { GroupSettingsDialog } from '../features/prompts/GroupSettingsDialog';
import { ImportDialog } from '../features/prompts/ImportDialog';

type Dialogs =
  | { kind: 'create' }
  | { kind: 'import' }
  | { kind: 'generate' }
  | { kind: 'settings' }
  | { kind: 'entry'; mode: EntryDialogMode; entry?: Entry }
  | { kind: 'group-edit'; group: GroupSummary };

/** 无源/空词典时生成不可用。生成源配置尚未实现，当前以「已选词典且含条目」作为可用判据（占位）。 */
export function generateBlockedReason(hasGroup: boolean, entryCount: number): string | null {
  if (!hasGroup) return '请先选择词典。';
  if (entryCount === 0) return '当前词典没有条目，请先配置生成源并添加条目。';
  return null;
}

export function PromptsPage() {
  const qc = useQueryClient();
  const projectId = useApp((s) => s.projectId);
  const safety = useSafety((s) => s.settings);
  const [filter, setFilter] = useState(EMPTY_FILTER);
  const q = useDebounced(filter.q, 200);
  const [selected, setSelected] = useState<GroupRef | null>(null);
  const [entryId, setEntryId] = useState<string | null>(null);
  const [mode, setMode] = useState<RenderMode>('tags_plus_nl');
  const [dialog, setDialog] = useState<Dialogs | null>(null);
  const [notice, setNotice] = useState<{ tone: 'danger' | 'info'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const groupMenu = useContextMenu<GroupSummary>();
  const entryMenu = useContextMenu<{ entry: Entry; revision: number | null }>();

  const groupsQ = useQuery({ queryKey: ['pg-groups', projectId], queryFn: ({ signal }) => listGroups({}, signal) });
  const groups = useMemo(
    () => visibleGroups(groupsQ.data?.items ?? [], { ...filter, q }, safety),
    [groupsQ.data, filter, q, safety],
  );
  const selSummary = groupsQ.data?.items.find((g) => sameRef(refOf(g, projectId), selected));

  // NSFW 关闭或项目关闭：敏感/项目组消失，先清选择，避免内容残留。
  useEffect(() => {
    if (!selected) return;
    const hiddenNsfw = !safety.nsfw_enabled && !!selSummary?.nsfw;
    const projectGone = selected.scope === 'project' && !projectId;
    if (hiddenNsfw || projectGone) { setSelected(null); setEntryId(null); }
  }, [safety.nsfw_enabled, selSummary?.nsfw, selected, projectId]);

  const key = selected ? refKey(selected) : '';
  const groupQ = useQuery({
    queryKey: ['pg-group', key],
    queryFn: ({ signal }) => getGroup(selected!, signal),
    enabled: !!selected,
  });
  const entriesQ = useInfiniteQuery({
    queryKey: ['pg-entries', key],
    queryFn: ({ pageParam, signal }) => listEntries(selected!, { cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.data.next_cursor,
    enabled: !!selected,
  });
  const entries = useMemo(() => entriesQ.data?.pages.flatMap((p) => p.data.items) ?? [], [entriesQ.data]);
  const etag = groupQ.data?.etag ?? null;
  const revision = groupQ.data?.data.revision ?? null;
  const nsfw = groupQ.data?.data.nsfw ?? selSummary?.nsfw ?? false;

  // 404：清选择；其余错误显示文本。
  useEffect(() => {
    const err = groupQ.error ?? entriesQ.error;
    if (err && describePgError(err).notFound) { setSelected(null); setEntryId(null); void qc.invalidateQueries({ queryKey: ['pg-groups'] }); }
  }, [groupQ.error, entriesQ.error, qc]);

  const refresh = useCallback(async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['pg-groups'] }),
      qc.invalidateQueries({ queryKey: ['pg-group'] }),
      // 组 revision 变化后旧游标会 409 pagination:stale，所以从首页重读而不是沿用游标。
      qc.resetQueries({ queryKey: ['pg-entries'] }),
    ]);
  }, [qc]);

  const run = useCallback(async (work: () => Promise<unknown>, okText?: string) => {
    setBusy(true);
    setNotice(null);
    try {
      await work();
      if (okText) setNotice({ tone: 'info', text: okText });
    } catch (e) {
      setNotice({ tone: 'danger', text: describePgError(e).text });
    } finally {
      await refresh();
      setBusy(false);
    }
  }, [refresh]);

  const selectGroup = useCallback((g: GroupSummary) => {
    setSelected(refOf(g, projectId));
    setEntryId(null);
    setNotice(null);
  }, [projectId]);

  // ---- 组操作 ----
  const exportGroup = (g: GroupSummary) => run(async () => {
    const data = await exportGroupJson(refOf(g, projectId));
    downloadJson(data, safeFileName(g.name, 'json'));
  });
  const removeGroup = (g: GroupSummary) => run(async () => {
    const ref = refOf(g, projectId);
    await deleteGroup(ref, g.version ?? String(g.revision));
    if (sameRef(ref, selected)) { setSelected(null); setEntryId(null); }
  });

  // ---- 条目操作 ----
  const copyJson = (e: Entry) => run(async () => {
    const draft = await exportEntry(selected!, e.entry_id);
    await navigator.clipboard.writeText(JSON.stringify(draft, null, 2));
  }, '已复制条目 JSON');
  const toggleDisable = (e: Entry) => run(async () => {
    if (!etag) throw new Error('revision:required');
    await patchEntry(selected!, e.entry_id, { disable: !e.disable }, etag);
  });
  const removeEntry = (e: Entry, openedRevision: number | null) => run(async () => {
    // 计时中 revision 变化：需重新确认，不静默删除。
    if (!etag || (openedRevision !== null && openedRevision !== revision)) throw Object.assign(new Error('词典在长按期间已变化，请重新确认后再删除'), {});
    await deleteEntry(selected!, e.entry_id, etag);
    if (entryId === e.entry_id) setEntryId(null);
  });
  const complete = (e: Entry) => run(async () => {
    if (revision === null) throw new Error('revision:required');
    await enqueueCompletion(selected!, [e.entry_id], revision);
  }, '已提交 LLM 补全任务');

  const onAction = (a: ControlAction) => {
    if (a === 'add' || a === 'append') setDialog({ kind: 'entry', mode: a });
    else if (a === 'import') setDialog({ kind: 'import' });
    else if (a === 'generate') setDialog({ kind: 'generate' });
    else if (a === 'settings') setDialog({ kind: 'settings' });
    else if (a === 'export' && selSummary) void exportGroup(selSummary);
  };

  const bindGroup = (g: GroupSummary, onKey: (e: React.KeyboardEvent) => void) => groupMenu.bind(g, onKey);
  const bindEntry = (e: Entry, onKey: (ev: React.KeyboardEvent) => void) => entryMenu.bind({ entry: e, revision }, onKey);

  const gMenu = groupMenu.state;
  const eMenu = entryMenu.state;
  const closeDialog = () => setDialog(null);
  const total = groupQ.data?.data.entry_count ?? selSummary?.entry_count ?? null;
  const err = groupQ.error ?? entriesQ.error;
  const errText = err && !describePgError(err).notFound ? describePgError(err).text : null;

  return (
    <div className="pg">
      <GroupListPane
        groups={groups}
        loading={groupsQ.isLoading}
        errorText={groupsQ.error ? describePgError(groupsQ.error).text : null}
        selected={selected}
        projectId={projectId}
        query={filter.q}
        onQuery={(v) => setFilter((f) => ({ ...f, q: v }))}
        scope={filter.scope}
        onScope={(v: Scope | 'all') => setFilter((f) => ({ ...f, scope: v }))}
        nsfwEnabled={safety.nsfw_enabled}
        nsfwOnly={filter.nsfwOnly}
        onNsfwOnly={(v) => setFilter((f) => ({ ...f, nsfwOnly: v }))}
        onSelect={selectGroup}
        onCreate={() => setDialog({ kind: 'create' })}
        bindMenu={bindGroup}
      />
      <div className="pg-mid-wrap">
        {notice && <Banner tone={notice.tone}>{notice.text}</Banner>}
        <EntryPane
          gref={selected}
          title={groupQ.data?.data.name ?? selSummary?.name ?? ''}
          total={total}
          nsfw={nsfw}
          mode={mode}
          onMode={setMode}
          entries={entries}
          loading={entriesQ.isLoading}
          errorText={errText}
          hasMore={!!entriesQ.hasNextPage}
          loadingMore={entriesQ.isFetchingNextPage}
          onLoadMore={() => void entriesQ.fetchNextPage()}
          selectedId={entryId}
          onSelect={setEntryId}
          bindMenu={bindEntry}
        />
      </div>
      <ControlPane
        hasGroup={!!selected}
        busy={busy}
        generateBlockedReason={generateBlockedReason(!!selected, selSummary?.entry_count ?? 0)}
        onAction={onAction}
      />

      {gMenu && (
        <ContextMenu
          x={gMenu.x} y={gMenu.y} label="词典菜单" onClose={groupMenu.close}
          items={groupMenuItems({
            entryCount: gMenu.target.entry_count,
            onEdit: () => setDialog({ kind: 'group-edit', group: gMenu.target }),
            onExport: () => void exportGroup(gMenu.target),
            onDelete: () => void removeGroup(gMenu.target),
          })}
        />
      )}
      {eMenu && (
        <ContextMenu
          x={eMenu.x} y={eMenu.y} label="条目菜单" onClose={entryMenu.close}
          items={entryMenuItems({
            disabled: eMenu.target.entry.disable,
            onEdit: () => setDialog({ kind: 'entry', mode: 'edit', entry: eMenu.target.entry }),
            onCopyJson: () => void copyJson(eMenu.target.entry),
            onToggleDisable: () => void toggleDisable(eMenu.target.entry),
            onDelete: () => void removeEntry(eMenu.target.entry, eMenu.target.revision),
            onComplete: () => void complete(eMenu.target.entry),
          })}
        />
      )}

      {dialog?.kind === 'create' && (
        <GroupCreateDialog
          projectId={projectId}
          onClose={closeDialog}
          onDone={(g) => { closeDialog(); void refresh(); selectGroup(g); }}
        />
      )}
      {dialog?.kind === 'group-edit' && (
        <GroupEditDialog
          group={{ ...dialog.group, project_token: refOf(dialog.group, projectId).project_token }}
          etag={dialog.group.version ?? String(dialog.group.revision)}
          onClose={closeDialog}
          onDone={() => void refresh()}
        />
      )}
      {dialog?.kind === 'entry' && selected && (
        <EntryDialog
          mode={dialog.mode}
          gref={selected}
          etag={etag}
          entry={dialog.entry}
          onClose={closeDialog}
          onSaved={refresh}
        />
      )}
      {dialog?.kind === 'import' && (
        <ImportDialog
          projectId={projectId}
          target={selected}
          etag={etag}
          onClose={closeDialog}
          onDone={(ref) => { closeDialog(); setSelected(ref); setEntryId(null); void refresh(); }}
        />
      )}
      {dialog?.kind === 'generate' && selected && (
        <GenerateDialog groupId={selected.group_id} groupName={groupQ.data?.data.name ?? selSummary?.name ?? ''} onClose={closeDialog} />
      )}
      {dialog?.kind === 'settings' && selected && (
        <GroupSettingsDialog
          gref={selected}
          name={groupQ.data?.data.name ?? ''}
          nsfw={nsfw}
          etag={etag}
          onClose={closeDialog}
          onDone={() => void refresh()}
        />
      )}
    </div>
  );
}
