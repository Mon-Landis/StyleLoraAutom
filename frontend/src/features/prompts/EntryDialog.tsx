import { useContext, useEffect, useRef, useState, type FormEvent } from 'react';
import { QueryClientContext } from '@tanstack/react-query';
import { uuid } from '../../api/client';
import { ApiError } from '../../api/errors';
import { addEntry, getGroup, patchEntry, putPreview, refKey, type Versioned } from '../../api/promptGroups';
import type { Aspect, BaseField, Entry, EntryDraft, EntryPatch, GroupRef, PromptFormat } from '../../api/promptGroupTypes';
import { readOverwritten, registerBaseDraftSource, type BaseFlushContext } from '../../lib/baseDrafts';
import { runGroupWrite } from '../../lib/groupWriteQueue';
import { isDefinitive } from '../../lib/promptErrors';
import {
  buildEntryPatch, hasBaseContent, isFieldGenerated, isFieldPending, labelOverwritten, sameValue, type Overwritten,
} from '../../lib/promptVariants';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button, Field } from '../../ui/primitives';
import { GeneratedField } from './GeneratedField';
import { useSubmit } from './useSubmit';

export type EntryDialogMode = 'add' | 'append' | 'edit';
const TITLE: Record<EntryDialogMode, string> = { add: '添加提示词', append: '追加自然语言', edit: '修改条目' };
const EMPTY_MSG = '提示词不能为空';
const TEXT_FIELDS: readonly BaseField[] = ['nl'];
const SOURCE_LABEL: Record<PromptFormat, string> = {
  nai: 'NAI（默认）',
  anima: 'Anima（下划线 / @画师 / 逆倍率会被转换）',
};
const SOURCE_HINT = '保存时统一转为 NAI 格式；只转换本次编辑的文本，未改动的文本不会被再次转换。';
/** 后端默认来源为 nai：缺省即 nai，所以只在选 anima 时显式携带，且只随 nl 一起提交。 */
const sourceBody = (f: PromptFormat): { source_format?: PromptFormat } => (f === 'nai' ? {} : { source_format: f });

const overwriteText = (list: readonly Overwritten[]) => `服务端已更新${labelOverwritten(list)}，再次点击保存将覆盖`;
const STALE_TEXT = '词典已被他人修改，已刷新最新版本，表单内容已保留，请再次点击保存';

const UNSAVED_MSG = '保存期间又有新的输入，已保留在表单中，请再次点击保存';
/** 保存/离开 flush 为吸收「请求期间的新输入」而追加 PATCH 的最大轮数。 */
const MAX_ROUNDS = 3;

interface Saved { id: string; etag: string }
interface FrozenPost {
  draft: { nl: string; aspect: Aspect | null; disable: boolean };
  body: EntryDraft;
  etag: string;
  key: string;
}

/** 服务端明确拒绝：4xx 且不是 408 / 429（这两个不代表请求未被处理）。 */
const isRejected = (e: unknown): boolean =>
  e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429;

/** 把已成功提交的 PATCH 应用到本地条目（响应缺字段时也能把编辑起点前移）。 */
function applyPatch(b: Entry, p: EntryPatch): Entry {
  const next: Entry = { ...b };
  if (p.nl !== undefined) next.nl = p.nl;
  if (p.disable !== undefined) next.disable = p.disable;
  if (p.aspect !== undefined) next.aspect = p.aspect;
  return next;
}

/**
 * 条目表单（v5）：只有 nl 一个文本字段，原样提交不 trim；append 复用同一 nl 表单。
 * 来源格式（NAI / Anima）只是本次提交的请求参数，不属于条目内容：仅在提交 nl 时携带，服务端统一保存为 NAI；
 * 成功后若服务端规范文本不同于提交文本且期间无新输入，则回填规范文本（来源重置为 NAI，避免二次转换）。
 * 图像是第二步：文本先保存，上传失败时文本不丢，可单独重试上传。
 * 未保存的基础字段向离开协调器注册（registerBaseDraftSource）：离开/切组时先经同组写入队列保存，
 * 失败则阻止离开并保留表单与 touched；beforeunload 也据此提醒。
 */
export function EntryDialog({ mode, gref, etag, entry, onClose, onSaved }: {
  mode: EntryDialogMode;
  gref: GroupRef;
  etag: string | null;
  entry?: Entry;
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const [nl, setNl] = useState(entry?.nl ?? '');
  const [aspect, setAspect] = useState<Aspect | ''>(entry?.aspect ?? '');
  const [disable, setDisable] = useState(entry?.disable ?? false);
  const [file, setFile] = useState<File | null>(null);
  const [sourceFormat, setSourceFormat] = useState<PromptFormat>('nai');
  // 用户真实输入过的文本字段（含改后恢复原值）：只提交这些，并在本地立即清除生成标记。
  const [touched, setTouched] = useState<ReadonlySet<BaseField>>(new Set());
  const touch = (f: BaseField) => setTouched((s) => (s.has(f) ? s : new Set(s).add(f)));
  const [localError, setLocalError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const saved = useRef<Saved | null>(null);
  const uploadKey = useRef(uuid());
  const flushKey = useRef(uuid());
  const qc = useContext(QueryClientContext);

  // 最新表单快照：注册给协调器的回调在卸载后仍可能被调用，必须读 ref 而不是闭包。
  const latest = useRef({ mode, entry, nl, aspect, disable, file, touched, sourceFormat, onClose, qc });
  latest.current = { mode, entry, nl, aspect, disable, file, touched, sourceFormat, onClose, qc };
  /** 编辑起点：保存成功后前移到服务端返回值，后续只比较相对它的改动。 */
  const base = useRef<Entry | undefined>(entry);
  /** 已持久化的文本字段值：保存后 touched 仍保留，靠它判断是否还有未保存内容。 */
  const persisted = useRef<Partial<Record<BaseField, unknown>>>({});
  const fileDone = useRef<File | null>(null);
  /** 最新已知 ETag：prop 变化时跟随；412 重读后先于 prop 生效。 */
  const etagRef = useRef(etag);
  const lastProp = useRef(etag);
  if (lastProp.current !== etag) { lastProp.current = etag; etagRef.current = etag; }
  /** 412 对账发现服务端字段被他人改动：用户再次点击保存才覆盖，离开 flush 不得静默覆盖。 */
  const needConfirm = useRef<Overwritten[] | null>(null);
  /** 新增/追加的 POST 已成功：此后表单按「已创建条目的编辑」处理（base 为创建时的快照 + 服务端返回）。 */
  const created = useRef(false);
  /** 首次提交之后的追加 PATCH 不能复用 useSubmit 的幂等键（载荷不同会 request_key_conflict）。 */
  const followKey = useRef(uuid());
  /** 结果未知（网络错误等）的新增 POST：冻结完整请求，重试先原样重放，防止同键异载荷 409 / 换键重复创建。 */
  const pendingPost = useRef<FrozenPost | null>(null);
  /**
   * 编辑起点的 ETag：不随 prop 刷新，只在本弹窗自己提交成功或 412 对账后前移。
   * 离开 flush 首次提交用它，让服务端在组已被他人改动时返回 412，交给 reconcile412 对账，而不是静默覆盖。
   */
  const startEtag = useRef(etag);

  const gen = (f: BaseField) => !!entry && isFieldGenerated(entry, f) && !touched.has(f);
  const pend = (f: BaseField) => !!entry && isFieldPending(entry, f);

  const form = () => {
    const L = latest.current;
    return { nl: L.nl, aspect: L.aspect, disable: L.disable };
  };
  /** 编辑模式尚未保存的 PATCH（只含 touched 文本字段 + 改动的 aspect/disable，去掉已保存且未再改的）。 */
  const pendingPatch = (): EntryPatch => {
    const b = base.current;
    if (!b) return {};
    const p = buildEntryPatch(b, form(), latest.current.touched);
    for (const f of TEXT_FIELDS) if (f in p && f in persisted.current && sameValue(persisted.current[f], p[f])) delete p[f];
    // 来源格式只随本次提交的 nl 走；未编辑文本不会因此被再次转换。
    if (p.nl !== undefined) Object.assign(p, sourceBody(latest.current.sourceFormat));
    return p;
  };
  const addHasContent = () => latest.current.nl.trim().length > 0;
  const pendingFile = (): File | null => (latest.current.file && latest.current.file !== fileDone.current ? latest.current.file : null);
  /** 当前按「编辑已有条目」处理：编辑模式，或新增/追加的 POST 已成功。 */
  const editing = (): boolean => !!base.current && (created.current || (latest.current.mode === 'edit' && !!latest.current.entry));
  const textDirty = (): boolean => (editing() ? Object.keys(pendingPatch()).length > 0 : addHasContent() || pendingPost.current !== null);
  const isDirty = (): boolean => textDirty() || pendingFile() !== null;

  /**
   * 保存文本（编辑/已创建：只 PATCH 待保存字段；新增：POST）。不改 saved，由调用方决定。
   * 提交的是调用瞬间的表单快照；请求期间的新输入留在表单里，成功后靠 persisted/base 对比仍判为未保存。
   */
  const commitText = async (useEtag: string, key: string): Promise<Saved & { resp: Versioned<Entry> | null }> => {
    const L = latest.current;
    const b = base.current;
    if (editing() && b) {
      const patch = pendingPatch();
      if (!hasBaseContent(b, buildEntryPatch(b, form(), L.touched))) throw new Error(EMPTY_MSG);
      if (Object.keys(patch).length === 0) return { id: b.entry_id, etag: useEtag, resp: null };
      const r = await patchEntry(gref, b.entry_id, patch, useEtag, key);
      for (const f of TEXT_FIELDS) if (f in patch) persisted.current[f] = patch[f];
      base.current = { ...applyPatch(b, patch), ...r.data };
      if (patch.nl !== undefined) backfillCanonical(patch.nl, r.data.nl);
      return { id: r.data.entry_id ?? b.entry_id, etag: r.etag ?? useEtag, resp: r };
    }
    // 结果未知的 POST 已冻结：原样重放（载荷 + If-Match + 幂等键），忽略当前表单；期间新输入在创建后以 PATCH 追加。
    let req = pendingPost.current;
    if (!req) {
      if (!addHasContent()) throw new Error(EMPTY_MSG);
      const d = { nl: L.nl, aspect: L.aspect || null, disable: L.disable } satisfies EntryDraft;
      // source_format 是请求参数，不进入条目快照（base）。
      req = { draft: d, body: { ...d, ...sourceBody(L.sourceFormat) }, etag: useEtag, key };
      pendingPost.current = req;
    }
    const draft = req.draft;
    let r: Versioned<Entry>;
    try {
      r = await addEntry(gref, req.body, req.etag, req.key);
    } catch (e) {
      // 只有服务端明确拒绝（4xx，非 408/429）才丢弃冻结请求；网络错误 / 5xx / 408 / 429 结果未知，保留。
      if (isRejected(e)) pendingPost.current = null;
      throw e;
    }
    pendingPost.current = null;
    // 创建成功：以提交快照为编辑起点，请求期间的新输入随后作为差异 PATCH 到这个条目。
    created.current = true;
    persisted.current = { nl: draft.nl };
    const fallback: Partial<Entry> = {
      preview: null, variants: {}, completion: { state: 'complete', missing: [], task_id: null, error_code: null },
    };
    base.current = { ...fallback, ...draft, ...r.data };
    backfillCanonical(draft.nl, r.data.nl);
    return { id: r.data.entry_id, etag: r.etag ?? useEtag, resp: r };
  };

  /**
   * 成功回填服务器规范文本（F31 §6.2）：仅当请求期间没有新输入时替换表单，
   * 并把来源重置为 NAI（规范文本已是 NAI，不能再按 Anima 转换第二次）。有新输入则保持不动。
   */
  const backfillCanonical = (submitted: string, canonical: unknown) => {
    if (typeof canonical !== 'string' || canonical === submitted) return;
    if (latest.current.nl !== submitted) return;
    latest.current.nl = canonical;
    persisted.current.nl = canonical;
    setNl(canonical);
    setSourceFormat('nai');
  };

  /** 412 后：重读最新 ETag（并让该组查询失效），编辑时对账服务端字段，保留表单与 touched。 */
  const reconcile412 = async (): Promise<{ etag: string | null; overwritten: Overwritten[] }> => {
    const fresh = (await getGroup(gref)).etag;
    if (fresh) { etagRef.current = fresh; startEtag.current = fresh; }
    const c = latest.current.qc;
    if (c) {
      void c.invalidateQueries({ queryKey: ['pg-group', refKey(gref)] });
      void c.invalidateQueries({ queryKey: ['pg-entries', refKey(gref)] });
    }
    let overwritten: Overwritten[] = [];
    if (editing() && base.current) {
      const p = pendingPatch();
      if (Object.keys(p).length) overwritten = await readOverwritten(gref, base.current, p);
    }
    needConfirm.current = overwritten.length ? overwritten : null;
    return { etag: fresh, overwritten };
  };

  // ---- 离开协调器：保存基础字段 ----
  const flushDrafts = async (ctx: BaseFlushContext): Promise<void> => {
    if (needConfirm.current?.length) {
      const msg = overwriteText(needConfirm.current);
      setConflict(msg);
      throw new Error(msg);
    }
    let cur: string | null;
    if (textDirty() && editing()) {
      // 编辑起点的 ETag 随首次 PATCH 发出：组在此之后被他人改过则 412，走 reconcile412 对账，不静默覆盖。
      cur = startEtag.current;
      if (!cur) {
        // 没有起点 ETag：先取最新 ETag，再读服务端条目对账（顺序不能反，之间的改动会被该 ETag 的 412 兜住）。
        cur = (await getGroup(gref)).etag;
        if (cur) {
          const over = await readOverwritten(gref, base.current!, pendingPatch());
          if (over.length) {
            needConfirm.current = over;
            const msg = overwriteText(over);
            setConflict(msg);
            throw new Error(msg);
          }
        }
      }
    } else {
      cur = (await getGroup(gref)).etag;
    }
    if (!cur) throw new Error('revision:required');
    let result: Saved | null = null;
    // 外层按轮：POST/PATCH 期间的新输入留在表单里，下一轮作为差异 PATCH 提交；超过轮数仍有则阻止离开。
    for (let round = 0; textDirty(); round++) {
      if (round >= MAX_ROUNDS) throw new Error(UNSAVED_MSG);
      for (let attempt = 0; ; attempt++) {
        try {
          const out = await commitText(cur, flushKey.current);
          flushKey.current = uuid();
          if (out.resp) ctx.onSaved?.(gref, out.id, out.resp);
          result = out;
          cur = out.etag;
          etagRef.current = out.etag;
          startEtag.current = out.etag;
          if (latest.current.mode !== 'edit') saved.current = { id: out.id, etag: out.etag };
          break;
        } catch (e) {
          if (isDefinitive(e)) flushKey.current = uuid();
          if (!(e instanceof ApiError && e.status === 412) || attempt > 0) throw e;
          ctx.onConflict?.(gref);
          const rec = await reconcile412();
          if (rec.etag) cur = rec.etag;
          if (rec.overwritten.length) {
            const msg = overwriteText(rec.overwritten);
            setConflict(msg);
            throw new Error(msg);
          }
        }
      }
    }
    const f = pendingFile();
    if (f) {
      const target = result ?? saved.current ?? (base.current ? { id: base.current.entry_id, etag: cur } : null);
      if (target) {
        await putPreview(gref, target.id, f, result ? result.etag : cur, uploadKey.current);
        uploadKey.current = uuid();
        fileDone.current = f;
      }
    }
    if (!isDirty()) latest.current.onClose();
  };

  useEffect(() => registerBaseDraftSource(gref, {
    isDirty,
    flush: flushDrafts,
  // 回调只读 ref；仅组引用变化时重新注册
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [gref.scope, gref.project_token, gref.group_id]);

  // ---- 显式保存 ----
  const s = useSubmit(async (key) => {
    setConflict(null);
    needConfirm.current = null; // 再次点击保存即确认覆盖
    let uploadErr: unknown = null;
    await runGroupWrite(refKey(gref), async () => {
      if (!saved.current) {
        const useEtag = etagRef.current;
        if (!useEtag) throw new Error('revision:required');
        const out = await commitText(useEtag, key);
        saved.current = { id: out.id, etag: out.etag };
        etagRef.current = out.etag;
        startEtag.current = out.etag;
      }
      // POST/PATCH 期间表单仍可编辑：与提交快照有差异的字段，对已创建/已更新的条目再 PATCH 一次。
      for (let round = 0; textDirty() && round < MAX_ROUNDS; round++) {
        const useEtag = etagRef.current ?? saved.current.etag;
        try {
          const out = await commitText(useEtag, followKey.current);
          followKey.current = uuid();
          saved.current = { id: out.id, etag: out.etag };
          etagRef.current = out.etag;
          startEtag.current = out.etag;
        } catch (e) {
          if (isDefinitive(e)) followKey.current = uuid();
          throw e;
        }
      }
      const target = saved.current;
      try {
        const f = pendingFile();
        if (f) {
          await putPreview(gref, target.id, f, target.etag, uploadKey.current);
          uploadKey.current = uuid();
          fileDone.current = f;
        }
      } catch (e) {
        uploadErr = e;
        if (e instanceof ApiError && e.status === 412) {
          // 版本已变：刷新 ETag，下次「重试上传」用新值，不再持续 412
          try {
            const fresh = (await getGroup(gref)).etag;
            if (fresh) { target.etag = fresh; etagRef.current = fresh; }
          } catch { /* 刷新失败时保持原值，用户可再次重试 */ }
        }
      }
    });
    await onSaved();
    if (uploadErr) {
      const e = uploadErr;
      throw new Error(`文本已保存，图像上传失败：${e instanceof Error ? e.message : String(e)}`);
    }
    // 仍有未保存的输入（请求期间又改了）：保持弹窗打开，不丢草稿。
    if (isDirty()) throw new Error(UNSAVED_MSG);
  }, onClose, {
    onConflict: async () => {
      const rec = await reconcile412();
      return rec.overwritten.length ? overwriteText(rec.overwritten) : STALE_TEXT;
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!saved.current && !pendingPost.current) {
      const empty = mode === 'edit' && entry && base.current
        ? !hasBaseContent(base.current, buildEntryPatch(base.current, form(), touched))
        : !nl.trim();
      if (empty) {
        setLocalError(EMPTY_MSG);
        return;
      }
    }
    setLocalError(null);
    void s.submit();
  };

  return (
    <Dialog title={TITLE[mode]} onClose={onClose}>
      <form className="pg-form" onSubmit={submit}>
        <GeneratedField generated={gen('nl')} pending={pend('nl')}>
          {({ describedBy }) => (
            <Field label="提示词（nl）">
              <textarea rows={mode === 'append' ? 6 : 4} value={nl} aria-describedby={describedBy} onChange={(e) => { setNl(e.target.value); touch('nl'); }} autoFocus />
            </Field>
          )}
        </GeneratedField>
        <Field label="来源格式">
          <select value={sourceFormat} onChange={(e) => setSourceFormat(e.target.value as PromptFormat)}>
            {(Object.keys(SOURCE_LABEL) as PromptFormat[]).map((f) => <option key={f} value={f}>{SOURCE_LABEL[f]}</option>)}
          </select>
        </Field>
        <p className="pg-muted">{SOURCE_HINT}</p>
        <Field label="图幅（空则沿用生成源）">
          <select value={aspect} onChange={(e) => setAspect(e.target.value as Aspect | '')}>
            <option value="">沿用生成源</option>
            <option value="landscape">横图</option>
            <option value="portrait">竖图</option>
            <option value="square">方图</option>
          </select>
        </Field>
        {mode === 'edit' && (
          <label className="check"><input type="checkbox" checked={disable} onChange={(e) => setDisable(e.target.checked)} /> 禁用此条目</label>
        )}
        {mode !== 'append' && (
          <Field label="预览图（可选，PNG / JPEG / WebP，≤10 MiB）">
            <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </Field>
        )}
        {localError && <Banner tone="danger">{localError}</Banner>}
        {conflict && <Banner tone="warning">{conflict}</Banner>}
        {s.error && <Banner tone={s.error === STALE_TEXT || s.error.startsWith('服务端已更新') ? 'warning' : 'danger'}>{s.error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" disabled={s.busy || !etag}>{s.busy ? '提交中…' : saved.current && !textDirty() ? '重试上传' : '保存'}</Button>
        </div>
      </form>
    </Dialog>
  );
}
