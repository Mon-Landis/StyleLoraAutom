import { useRef, useState } from 'react';
import { uuid } from '../../api/client';
import { confirmImport, previewImport, type ImportOptions } from '../../api/promptGroups';
import type { GroupRef, ImportLevel, ImportPreviewV3, ImportSourceFormat, Scope } from '../../api/promptGroupTypes';
import { defaultScope, scopeBlocked } from '../../lib/scope';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button, Field } from '../../ui/primitives';
import { useSubmit } from './useSubmit';

/**
 * 批量导入（JSON）。group 级新建一组（scope 默认按项目状态），entries 级追加到当前词典。
 * 任一选项变化后作废旧预览，不沿用旧 preview_id。zip / 文件夹组包入口 TODO。
 */
export function ImportDialog({ projectId, target, etag, onClose, onDone }: {
  projectId: string | null;
  /** 当前词典；null 时只能新建组。 */
  target: GroupRef | null;
  etag: string | null;
  onClose: () => void;
  onDone: (ref: GroupRef) => void;
}) {
  const [level, setLevel] = useState<ImportLevel>(target ? 'entries' : 'group');
  const [source, setSource] = useState<ImportSourceFormat>('standard');
  const [text, setText] = useState('');
  const [name, setName] = useState('');
  const [nsfw, setNsfw] = useState(false);
  const [choice, setChoice] = useState<Scope | null>(null);
  const [preview, setPreview] = useState<ImportPreviewV3 | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const confirmKey = useRef(uuid());
  const scope = defaultScope(projectId, choice);
  const blocked = level === 'group' && scopeBlocked(scope, projectId);
  const invalidate = () => setPreview(null);

  const pv = useSubmit(async (key) => {
    let data: unknown;
    try { data = JSON.parse(text); } catch { throw new Error('不是合法的 JSON（需 UTF-8，组对象请使用键名 prompts）'); }
    const options: ImportOptions = { level, source_format: source === 'nai' ? 'nai' : 'anima' };
    if (level === 'entries') options.ref = target!;
    else options.metadata = { ...(name.trim() ? { name: name.trim() } : {}), nsfw, scope };
    void key;
    return previewImport(data, options);
  }, (p) => { confirmKey.current = uuid(); setPreview(p); });

  const cf = useSubmit(async () => {
    const p = preview!;
    const ref = p.ref ?? target!;
    return confirmImport(p.preview_id, p.digest, ref, p.base_revision ?? 0, etag, confirmKey.current);
  }, () => onDone(preview!.ref ?? target!));

  const onFile = async (f: File | undefined) => {
    invalidate();
    setParseError(null);
    if (f) setText(await f.text());
  };
  const issues = preview?.issues ?? [];
  const canPreview = text.trim().length > 0 && !blocked && !pv.busy && (level === 'group' || !!target);
  const canConfirm = !!preview && !preview.blocked && (preview.valid_count ?? 1) > 0 && !cf.busy && (preview.base_revision === 0 || !!etag);

  return (
    <Dialog title="批量导入" onClose={onClose}>
      <div className="pg-form">
        <Field label="导入方式">
          <select value={level} onChange={(e) => { setLevel(e.target.value as ImportLevel); invalidate(); }}>
            {target && <option value="entries">追加条目到当前词典</option>}
            <option value="group">新建词典</option>
          </select>
        </Field>
        <Field label="来源格式">
          <select value={source} onChange={(e) => { setSource(e.target.value as ImportSourceFormat); invalidate(); }}>
            <option value="standard">标准 JSON</option>
            <option value="nai">NAI 提示词（转换为 Anima）</option>
          </select>
        </Field>
        {level === 'group' && (
          <>
            <Field label="名称（空则「导入提示词组」）"><input value={name} onChange={(e) => { setName(e.target.value); invalidate(); }} /></Field>
            <Field label="作用范围">
              <select value={scope} onChange={(e) => { setChoice(e.target.value as Scope); invalidate(); }}>
                <option value="global">全局</option>
                <option value="project">项目</option>
              </select>
            </Field>
            <label className="check"><input type="checkbox" checked={nsfw} onChange={(e) => { setNsfw(e.target.checked); invalidate(); }} /> 这是 nsfw 词典（请核对）</label>
            {blocked && <Banner tone="warning">未打开项目：请打开项目或改选全局。</Banner>}
          </>
        )}
        <Field label="选择 JSON 文件">
          <input type="file" accept="application/json,.json" onChange={(e) => void onFile(e.target.files?.[0])} />
        </Field>
        <Field label="或粘贴 JSON">
          <textarea rows={6} value={text} onChange={(e) => { setText(e.target.value); invalidate(); setParseError(null); }} />
        </Field>
        {parseError && <Banner tone="danger">{parseError}</Banner>}
        {pv.error && <Banner tone="danger">{pv.error}</Banner>}
        {preview && (
          <div className="pg-import-preview" role="status">
            <div>有效 <b>{preview.valid_count ?? 0}</b> 条，跳过 <b>{preview.skipped_count ?? issues.length}</b> 条</div>
            {preview.blocked && <Banner tone="warning">目标为项目级但未打开项目，不能提交。</Banner>}
            {(preview.warnings ?? []).map((w, i) => <div key={i} className="pg-muted">{typeof w === 'string' ? w : w.message ?? w.code}</div>)}
            {issues.slice(0, 20).map((it, i) => (
              <div key={i} className="pg-muted">第 {it.row ?? '?'} 行{it.field ? ` · ${it.field}` : ''}：{it.message ?? it.code}</div>
            ))}
          </div>
        )}
        {cf.error && <Banner tone="danger">{cf.error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>取消</Button>
          <Button disabled={!canPreview} onClick={() => void pv.submit()}>{pv.busy ? '预览中…' : '预览'}</Button>
          <Button variant="primary" disabled={!canConfirm} onClick={() => void cf.submit()}>
            {cf.busy ? '导入中…' : preview ? `导入 ${preview.valid_count ?? ''} 条` : '导入'}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
