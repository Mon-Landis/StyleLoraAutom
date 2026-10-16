import { useRef, useState, type FormEvent } from 'react';
import { uuid } from '../../api/client';
import { addEntry, patchEntry, putPreview } from '../../api/promptGroups';
import type { Aspect, Entry, EntryDraft, GroupRef } from '../../api/promptGroupTypes';
import { parseTags } from '../../lib/promptGroupList';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button, Field } from '../../ui/primitives';
import { useSubmit } from './useSubmit';

export type EntryDialogMode = 'add' | 'append' | 'edit';
const TITLE: Record<EntryDialogMode, string> = { add: '添加元素', append: '追加自然语言', edit: '修改条目' };

/**
 * 条目表单。append 以 expand_nl 为主要输入（缺口由后端 §6 推导）。
 * 图像是第二步：文本先保存，上传失败时文本不丢，可单独重试上传。
 */
export function EntryDialog({ mode, gref, etag, entry, onClose, onSaved }: {
  mode: EntryDialogMode;
  gref: GroupRef;
  etag: string | null;
  entry?: Entry;
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const [tags, setTags] = useState((entry?.tag ?? []).join(', '));
  const [expandNl, setExpandNl] = useState(entry?.expand_nl ?? '');
  const [nl, setNl] = useState(entry?.nl ?? '');
  const [aspect, setAspect] = useState<Aspect | ''>(entry?.aspect ?? '');
  const [disable, setDisable] = useState(entry?.disable ?? false);
  const [file, setFile] = useState<File | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const saved = useRef<{ id: string; etag: string } | null>(null);
  const uploadKey = useRef(uuid());

  const s = useSubmit(async (key) => {
    if (!saved.current) {
      if (!etag) throw new Error('revision:required');
      const draft: EntryDraft = {
        tag: parseTags(tags), expand_nl: expandNl.trim(), nl: nl.trim(), aspect: aspect || null, disable,
      };
      const r = mode === 'edit' && entry
        ? await patchEntry(gref, entry.entry_id, draft, etag, key)
        : await addEntry(gref, draft, etag, key);
      saved.current = { id: r.data.entry_id ?? entry!.entry_id, etag: r.etag ?? etag };
    }
    try {
      if (file) {
        await putPreview(gref, saved.current.id, file, saved.current.etag, uploadKey.current);
        uploadKey.current = uuid();
      }
    } catch (e) {
      await onSaved();
      throw new Error(`文本已保存，图像上传失败：${e instanceof Error ? e.message : String(e)}`);
    }
    await onSaved();
  }, onClose);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!saved.current && !parseTags(tags).length && !expandNl.trim() && !nl.trim()) {
      setLocalError('tag、追加自然语言、自然语言至少填写一项');
      return;
    }
    setLocalError(null);
    void s.submit();
  };

  return (
    <Dialog title={TITLE[mode]} onClose={onClose}>
      <form className="pg-form" onSubmit={submit}>
        {mode !== 'append' && (
          <Field label="tag（逗号分隔）">
            <textarea rows={3} value={tags} onChange={(e) => setTags(e.target.value)} autoFocus />
          </Field>
        )}
        <Field label="追加自然语言（expand_nl，接在 tag 后）">
          <textarea rows={mode === 'append' ? 6 : 3} value={expandNl} onChange={(e) => setExpandNl(e.target.value)} autoFocus={mode === 'append'} />
        </Field>
        {mode !== 'append' && (
          <Field label="自然语言（nl，独立完整描述）">
            <textarea rows={3} value={nl} onChange={(e) => setNl(e.target.value)} />
          </Field>
        )}
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
        {mode === 'append' && <p className="pg-muted">缺失的 tag / 自然语言稍后可用「LLM 补全」推导，不会自动调用付费服务。</p>}
        {localError && <Banner tone="danger">{localError}</Banner>}
        {s.error && <Banner tone="danger">{s.error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" disabled={s.busy || !etag}>{s.busy ? '提交中…' : saved.current ? '重试上传' : '保存'}</Button>
        </div>
      </form>
    </Dialog>
  );
}
