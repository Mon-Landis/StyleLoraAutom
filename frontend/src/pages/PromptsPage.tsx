import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query';
import {
  deleteEntry, deleteGroup, exportEntry, getGroup, listEntries, listGroups,
  patchEntry, refKey, getGenerationTask, type EntryPage, type Versioned,
} from '../api/promptGroups';
import type { Entry, EntryPatch, GroupRef, GroupSummary, PromptView, Scope } from '../api/promptGroupTypes';
import { useApp } from '../state/app';
import { useSafety } from '../state/safety';
import { describeGeneration, describePgError, settleGenerationPoll } from '../lib/promptErrors';
import { useAutoCompletion } from '../features/prompts/useAutoCompletion';
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
import { ExportDialog } from '../features/prompts/ExportDialog';

type Dialogs =
  | { kind: 'create' }
  | { kind: 'import' }
  | { kind: 'import-group' }
  | { kind: 'export'; group: GroupSummary }
  | { kind: 'generate' }
  | { kind: 'settings' }
  | { kind: 'entry'; mode: EntryDialogMode; entry?: Entry }
  | { kind: 'group-edit'; group: GroupSummary };

/** 按主题生成只需要选中目标组，空组也可生成。 */
export function generateBlockedReason(hasGroup: boolean): string | null {
  if (!hasGroup) return '请先选择词典。';
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
  const [view, setView] = useState<PromptView>('prompt');
  const [dialog, setDialog] = useState<Dialogs | null>(null);
  const [notice, setNotice] = useState<{ tone: 'danger' | 'info'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [generationIds, setGenerationIds] = useState<string[]>([]);
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

  const generations = useQuery({
    queryKey: ['pg-generation-tasks', generationIds],
    queryFn: async ({ signal }) => settleGenerationPoll(generationIds,
      await Promise.allSettled(generationIds.map((id) => getGenerationTask(id, signal)))),
    enabled: generationIds.length > 0,
    refetchInterval: 1000,
  });
  const pollError = generations.data?.error ?? null;
  useEffect(() => {
    const poll = generations.data;
    if (!poll) return;
    void refresh();
    const { finished, gone } = poll;
    if (!finished.length && !gone.length) return;
    const done = new Set([...finished.map((t) => t.task_id), ...gone]);
    setGenerationIds((ids) => ids.filter((id) => !done.has(id)));
    const texts = finished.map(describeGeneration);
    if (gone.length) texts.push(`${gone.length} 个生成任务已失效，已停止跟踪。`);
    setNotice({ tone: gone.length || finished.some((t) => t.failed || t.status !== 'done') ? 'danger' : 'info',
      text: texts.join(' ') });
  }, [generations.data, refresh]);

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

  const auto = useAutoCompletion(selected);
  const switchSeq = useRef(0);
  /**
   * 切换到另一个组（或取消选择）。离开旧组时先 flush 草稿（不再发布补齐任务）；
   * 草稿保存失败则留在原组并提示。同组重复点击不触发。
   */
  const switchTo = useCallback(async (next: GroupRef | null) => {
    const prev = selected;
    if (sameRef(prev, next)) return;
    const seq = ++switchSeq.current;
    if (prev) {
      const ok = await auto.leave(prev);
      if (!ok) { setNotice({ tone: 'danger', text: '草稿保存失败，已留在当前词典，草稿已保留。' }); return; }
    }
    if (seq !== switchSeq.current) return; // 期间用户又点了别的组：以最后一次为准
    setSelected(next);
    setEntryId(null);
    setNotice(null);
  }, [selected, auto]);
  const selectGroup = useCallback((g: GroupSummary) => { void switchTo(refOf(g, projectId)); }, [switchTo, projectId]);

  // ---- 组操作 ----
  const exportGroup = (g: GroupSummary) => setDialog({ kind: 'export', group: g });
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
  /**
   * 视图内编辑：只提交触及字段。成功后用响应直接更新缓存（生成标记即时清除、拿到新 ETag 供连续保存），
   * 再后台失效重取；失败时重读版本并把错误抛回调用方，由其保留草稿。
   */
  const patchOne = useCallback(async (e: Entry, patch: EntryPatch) => {
    if (!selected) throw new Error('revision:required');
    const ref = selected;
    const k = refKey(ref);
    // 排队后执行：取缓存里最新的 ETag（离开 flush 可能刚刚更新过），不用点击时闭包里的旧值。
    const curEtag = qc.getQueryData<Versioned<unknown>>(['pg-group', k])?.etag ?? etag;
    if (!curEtag) throw new Error('revision:required');
    try {
      const r = await patchEntry(ref, e.entry_id, patch, curEtag);
      qc.setQueryData<Versioned<{ revision: number }>>(['pg-group', k], (old) => old && ({
        data: { ...old.data, revision: r.data.revision ?? old.data.revision + 1 },
        etag: r.etag ?? old.etag,
      }));
      qc.setQueryData<InfiniteData<Versioned<EntryPage>>>(['pg-entries', k], (old) => old && ({
        ...old,
        pages: old.pages.map((pg) => ({
          ...pg,
          data: { ...pg.data, items: pg.data.items.map((it) => (it.entry_id === e.entry_id ? { ...it, ...r.data } : it)) },
        })),
      }));
    } catch (err) {
      void qc.invalidateQueries({ queryKey: ['pg-group', k] });
      throw err;
    } finally {
      void qc.invalidateQueries({ queryKey: ['pg-groups'] });
      void qc.invalidateQueries({ queryKey: ['pg-entries', k] });
    }
  }, [qc, selected, etag]);
  // F31 §7.3：手动 LLM 补全入口已隐藏（后端退化为空任务，不能偷换成重写 nl）；enqueueCompletion 客户端函数保留。

  const onAction = (a: ControlAction) => {
    if (a === 'add' || a === 'append') setDialog({ kind: 'entry', mode: a });
    else if (a === 'import') setDialog({ kind: 'import' });
    else if (a === 'generate') setDialog({ kind: 'generate' });
    else if (a === 'settings') setDialog({ kind: 'settings' });
    else if (a === 'export' && selSummary) exportGroup(selSummary);
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
        onImport={() => setDialog({ kind: 'import-group' })}
        bindMenu={bindGroup}
      />
      <div className="pg-mid-wrap">
        {notice && <Banner tone={notice.tone}>{notice.text}</Banner>}
        {pollError && (
          <Banner tone="danger">
            生成任务状态查询失败：{pollError}{' '}
            <button type="button" className="btn" onClick={() => void generations.refetch()}>重试</button>
          </Banner>
        )}
        <EntryPane
          gref={selected}
          title={groupQ.data?.data.name ?? selSummary?.name ?? ''}
          total={total}
          nsfw={nsfw}
          view={view}
          onView={setView}
          entries={entries}
          loading={entriesQ.isLoading}
          errorText={errText}
          hasMore={!!entriesQ.hasNextPage}
          loadingMore={entriesQ.isFetchingNextPage}
          onLoadMore={() => void entriesQ.fetchNextPage()}
          selectedId={entryId}
          onSelect={setEntryId}
          bindMenu={bindEntry}
          onPatchEntry={patchOne}
        />
      </div>
      <ControlPane
        hasGroup={!!selected}
        busy={busy}
        generateBlockedReason={generateBlockedReason(!!selected)}
        onAction={onAction}
      />

      {gMenu && (
        <ContextMenu
          x={gMenu.x} y={gMenu.y} label="词典菜单" onClose={groupMenu.close}
          items={groupMenuItems({
            entryCount: gMenu.target.entry_count,
            onEdit: () => setDialog({ kind: 'group-edit', group: gMenu.target }),
            onExport: () => exportGroup(gMenu.target),
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
          targetNsfw={nsfw}
          onClose={closeDialog}
          onDone={(ref) => { closeDialog(); void switchTo(ref); void refresh(); }}
        />
      )}
      {dialog?.kind === 'import-group' && (
        <ImportDialog
          fixedLevel="group"
          projectId={projectId}
          target={null}
          etag={null}
          onClose={closeDialog}
          onDone={(ref) => { closeDialog(); void switchTo(ref); void refresh(); }}
        />
      )}
      {dialog?.kind === 'export' && (
        <ExportDialog gref={refOf(dialog.group, projectId)} groupName={dialog.group.name} onClose={closeDialog} />
      )}
      {dialog?.kind === 'generate' && selected && (
        <GenerateDialog gref={selected} groupName={groupQ.data?.data.name ?? selSummary?.name ?? ''} onClose={closeDialog} onSubmitted={(id) => setGenerationIds((ids) => [...ids, id])} />
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
