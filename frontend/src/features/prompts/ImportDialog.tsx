import { useEffect, useRef, useState } from 'react';
import { uuid } from '../../api/client';
import { ApiError } from '../../api/errors';
import { confirmImport, previewImportFiles, type ImportOptions } from '../../api/promptGroups';
import type { GroupRef, ImportFormat, ImportLevel, ImportPreviewV5, PromptFormat, Scope } from '../../api/promptGroupTypes';
import { describePgError, importFileIssues } from '../../lib/promptErrors';
import { defaultScope, scopeBlocked } from '../../lib/scope';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button, Field } from '../../ui/primitives';
import { ImportErrorBanner, ImportPreview, type ImportErrorView } from './ImportPreview';

export interface ImportDialogProps {
  projectId: string | null;
  /** 当前词典；null 时只能新建词典（level=group）。 */
  target: GroupRef | null;
  /** 当前词典 ETag，仅 level=entries 且 base_revision>0 时作为 If-Match。 */
  etag: string | null;
  /** 当前词典是否 nsfw：level=entries 时预览缩略图按此套用 NSFW 规则。 */
  targetNsfw?: boolean;
  /**
   * 固定导入级别并隐藏级别选择：'group' = 独立「导入词典」（新建，不覆盖当前词典）；
   * 'entries' = 追加条目（需要 target）。缺省 = 旧的批量导入入口（有 target 默认 entries，可切换）。
   */
  fixedLevel?: ImportLevel;
  onClose: () => void;
  /** 确认成功：返回目标词典 ref（新词典或追加的当前词典），调用方负责刷新并选中。 */
  onDone: (ref: GroupRef) => void;
}

type SourceKind = 'file' | 'nai_images';
type ZipKind = 'nai_image_zip' | 'zip';
type Dialect = Extract<PromptFormat, 'anima' | 'nai'>;

const DIALECT_LABEL: Record<Dialect, string> = {
  anima: 'Anima（旧格式默认：下划线 / @画师 / 逆倍率会被转换）',
  nai: 'NAI（直接按 NAI 语义入库）',
};
const DIALECT_HINT = '入库统一为 NAI。v2 导出文件（含 prompt_format）固定按 NAI 处理，不受此选项影响。';
const IMAGES_HINT = '来源固定为 NAI。单独 PNG 只能恢复提示词与参考图，无法恢复变体、禁用状态与词典元数据；需完整还原请导入 ZIP 图像包。';
const STALE_CODES = new Set(['preview:expired', 'dictionary:revision_conflict', 'revision:required']);

const extOf = (name: string) => /\.([a-z0-9]+)$/i.exec(name)?.[1].toLowerCase() ?? '';
const isPng = (f: File) => f.type === 'image/png' || extOf(f.name) === 'png';
const fileKey = (f: File) => `${f.name}:${f.size}:${f.lastModified}`;
const formatSize = (n: number) => (n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MiB` : `${Math.max(1, Math.round(n / 1024))} KiB`);
const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';
const toView = (e: unknown): ImportErrorView => ({ text: describePgError(e).text, files: importFileIssues(e) });

interface Frozen { previewId: string; digest: string; ref: GroupRef; baseRevision: number; etag: string | null; key: string }

/**
 * 导入词典 / 批量导入。来源「文件（JSON/TXT/ZIP）」或「来自标准 NAI 图像集」。
 * 任何输入变化立即作废预览（abort + 请求序号丢弃迟到响应）；确认重试复用同一 ref/digest/key/条件头。
 */
export function ImportDialog({ projectId, target, etag, targetNsfw = false, fixedLevel, onClose, onDone }: ImportDialogProps) {
  const [levelSel, setLevelSel] = useState<ImportLevel>(fixedLevel ?? (target ? 'entries' : 'group'));
  const level: ImportLevel = !target ? 'group' : levelSel;
  const [kind, setKind] = useState<SourceKind>('file');
  const [zipKind, setZipKind] = useState<ZipKind>('nai_image_zip');
  const [dialect, setDialect] = useState<Dialect>('anima');
  const [files, setFiles] = useState<File[]>([]);
  const [text, setText] = useState('');
  const [name, setName] = useState('');
  const [nsfw, setNsfw] = useState(false);
  const [choice, setChoice] = useState<Scope | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreviewV5 | null>(null);
  const [pvBusy, setPvBusy] = useState(false);
  const [pvError, setPvError] = useState<ImportErrorView | null>(null);
  const [cfBusy, setCfBusy] = useState(false);
  const [cfError, setCfError] = useState<ImportErrorView | null>(null);
  const [stale, setStale] = useState(false);

  const seq = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const previewKey = useRef(uuid());
  const frozen = useRef<Frozen | null>(null);
  const cfBusyRef = useRef(false);
  /** 实例存活守卫：卸载后迟到的确认响应不得调用 onDone / onClose，也不再改状态。 */
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; seq.current++; abort.current?.abort(); };
  }, []);
  /** 确认提交在途时不允许关闭（结果未知的幂等键只存在于本实例）。 */
  const close = () => { if (alive.current && !cfBusyRef.current) onClose(); };

  const scope = defaultScope(projectId, choice);
  const blocked = level === 'group' && scopeBlocked(scope, projectId);

  /** 文件/目标/选项变化：作废预览、取消在途请求、丢弃迟到响应、换预览幂等键。 */
  const invalidate = () => {
    seq.current++;
    abort.current?.abort();
    abort.current = null;
    previewKey.current = uuid();
    frozen.current = null;
    setPvBusy(false);
    setPreview(null);
    setPvError(null);
    setCfError(null);
    setStale(false);
    setLocalError(null);
  };

  const pasted = kind === 'file' && files.length === 0 && text.trim().length > 0;
  const picked = pasted ? [new File([text], 'pasted.json', { type: 'application/json' })] : files;
  const ext = picked[0] ? extOf(picked[0].name) : '';
  const format: ImportFormat | null = kind === 'nai_images' ? 'nai_images'
    : ext === 'json' ? 'json' : ext === 'txt' ? 'txt' : ext === 'zip' ? zipKind : null;
  const legacyDialect = kind === 'file' && (format === 'json' || format === 'txt' || format === 'zip');

  const buildOptions = (): ImportOptions => {
    const o: ImportOptions = { format: format!, level };
    // 图像集固定 nai；旧格式选 NAI 才显式携带，缺省交给后端（旧格式默认 anima，v2 文件固定 nai）。
    if (kind === 'nai_images') o.source_format = 'nai';
    else if (legacyDialect && dialect === 'nai') o.source_format = 'nai';
    if (level === 'entries') o.ref = target!;
    else o.metadata = { ...(name.trim() ? { name: name.trim() } : {}), nsfw, scope };
    return o;
  };

  const canPreview = picked.length > 0 && !!format && !blocked && !pvBusy && !cfBusy && (level === 'group' || !!target);

  const runPreview = async () => {
    if (!canPreview) return;
    abort.current?.abort();
    const mine = ++seq.current;
    const ctl = new AbortController();
    abort.current = ctl;
    setPvBusy(true);
    setPvError(null);
    setCfError(null);
    setStale(false);
    setPreview(null);
    try {
      const p = await previewImportFiles(picked, buildOptions(), previewKey.current, ctl.signal);
      if (mine !== seq.current) return;
      previewKey.current = uuid();
      frozen.current = null;
      setPreview(p);
    } catch (e) {
      if (mine !== seq.current || isAbort(e)) return;
      setPvError(toView(e));
    } finally {
      if (mine === seq.current) setPvBusy(false);
    }
  };

  const runConfirm = async () => {
    if (!preview || cfBusyRef.current) return;
    cfBusyRef.current = true;
    setCfBusy(true);
    setCfError(null);
    // 首次确认时冻结请求；重试（网络错误后）原样复用 ref / digest / Idempotency-Key / 条件头。
    const req = frozen.current ??= {
      previewId: preview.preview_id, digest: preview.digest, ref: preview.ref ?? target!,
      baseRevision: preview.base_revision ?? 0, etag, key: uuid(),
    };
    try {
      await confirmImport(req.previewId, req.digest, req.ref, req.baseRevision, req.etag, req.key);
      if (!alive.current) return;
      onDone(req.ref);
    } catch (e) {
      if (!alive.current) return;
      if (e instanceof ApiError && (STALE_CODES.has(e.code) || e.status === 410 || e.status === 412)) setStale(true);
      setCfError(toView(e));
    } finally {
      cfBusyRef.current = false;
      if (alive.current) setCfBusy(false);
    }
  };

  const pickFiles = (list: FileList | null) => {
    const incoming = Array.from(list ?? []);
    if (incoming.length === 0) return;
    invalidate();
    if (kind === 'nai_images') {
      const png = incoming.filter(isPng);
      if (png.length < incoming.length) setLocalError(`已忽略 ${incoming.length - png.length} 个非 PNG 文件`);
      setFiles((cur) => {
        const seen = new Set(cur.map(fileKey));
        return [...cur, ...png.filter((f) => !seen.has(fileKey(f)))];
      });
    } else {
      setFiles([incoming[0]]);
      setText('');
      if (!['json', 'txt', 'zip'].includes(extOf(incoming[0].name))) setLocalError('仅支持 .json / .txt / .zip 文件');
    }
  };

  const switchKind = (k: SourceKind) => { invalidate(); setKind(k); setFiles([]); setText(''); };
  const removeFile = (key: string) => { invalidate(); setFiles((cur) => cur.filter((f) => fileKey(f) !== key)); };
  const totalSize = files.reduce((n, f) => n + f.size, 0);

  const validCount = preview ? preview.valid_count ?? preview.files?.filter((f) => f.status === 'valid').length ?? 1 : 0;
  const canConfirm = !!preview && !preview.blocked && validCount > 0 && !cfBusy && !stale && (preview.base_revision === 0 || !!etag);
  const title = fixedLevel === 'group' ? '导入词典' : '批量导入';
  const sensitive = level === 'group' ? nsfw || !!preview?.metadata?.nsfw : targetNsfw;
  const status = pvBusy ? '正在生成预览' : cfBusy ? '正在导入' : '';

  return (
    <Dialog title={title} onClose={close} busy={cfBusy}>
      <div className="pg-form">
        <fieldset className="pg-imp-fs" disabled={cfBusy}>
          {!fixedLevel && target && (
            <Field label="导入方式">
              <select value={level} onChange={(e) => { setLevelSel(e.target.value as ImportLevel); invalidate(); }}>
                <option value="entries">追加条目到当前词典</option>
                <option value="group">新建词典</option>
              </select>
            </Field>
          )}
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
          <Field label="来源">
            <select value={kind} onChange={(e) => switchKind(e.target.value as SourceKind)}>
              <option value="file">文件（JSON/TXT/ZIP）</option>
              <option value="nai_images">来自标准nai图像集</option>
            </select>
          </Field>

          {kind === 'nai_images' ? (
            <>
              <Field label="选择 PNG 图像（可多选）">
                <input type="file" accept="image/png,.png" multiple onChange={(e) => { pickFiles(e.target.files); e.target.value = ''; }} />
              </Field>
              <p className="pg-muted">{IMAGES_HINT}</p>
              <div className="pg-muted">来源格式：NAI（固定）</div>
              {files.length > 0 && (
                <>
                  <div className="pg-muted">已选 {files.length} 张，共 {formatSize(totalSize)}</div>
                  <ul className="pg-imp-picked" aria-label="已选图片">
                    {files.map((f) => (
                      <li key={fileKey(f)}>
                        <span className="pg-imp-picked__name" title={f.name}>{f.name} · {formatSize(f.size)}</span>
                        <Button variant="ghost" aria-label={`移除 ${f.name}`} onClick={() => removeFile(fileKey(f))}>×</Button>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          ) : (
            <>
              <Field label="选择文件（.json / .txt / .zip）">
                <input type="file" accept="application/json,.json,text/plain,.txt,application/zip,.zip" onChange={(e) => { pickFiles(e.target.files); e.target.value = ''; }} />
              </Field>
              {files[0] && <div className="pg-muted">{files[0].name} · {formatSize(files[0].size)}</div>}
              {ext === 'zip' && (
                <Field label="ZIP 类型">
                  <select value={zipKind} onChange={(e) => { setZipKind(e.target.value as ZipKind); invalidate(); }}>
                    <option value="nai_image_zip">图像包（本工具「JSON / 图像（ZIP）」导出）</option>
                    <option value="zip">旧版组包（previews/*.webp）</option>
                  </select>
                </Field>
              )}
              {legacyDialect && (
                <>
                  <Field label="来源格式">
                    <select value={dialect} onChange={(e) => { setDialect(e.target.value as Dialect); invalidate(); }}>
                      {(Object.keys(DIALECT_LABEL) as Dialect[]).map((d) => <option key={d} value={d}>{DIALECT_LABEL[d]}</option>)}
                    </select>
                  </Field>
                  <p className="pg-muted">{DIALECT_HINT}</p>
                </>
              )}
              {files.length === 0 && (
                <Field label="或粘贴 JSON">
                  <textarea rows={5} value={text} onChange={(e) => { setText(e.target.value); invalidate(); }} />
                </Field>
              )}
            </>
          )}
        </fieldset>

        <div className="visually-hidden" role="status" aria-live="polite">{status}</div>
        {localError && <Banner tone="warning">{localError}</Banner>}
        {pvError && <ImportErrorBanner error={pvError} />}
        {preview && <ImportPreview preview={preview} sensitive={sensitive} />}
        {cfError && <ImportErrorBanner error={cfError} />}
        {stale && <Banner tone="warning">预览已失效或词典已变化，请重新预览后再导入。</Banner>}
        <div className="pg-form__actions">
          <Button disabled={cfBusy} onClick={close}>取消</Button>
          <Button disabled={!canPreview} onClick={() => void runPreview()}>{pvBusy ? '预览中…' : '预览'}</Button>
          <Button variant="primary" disabled={!canConfirm} onClick={() => void runConfirm()}>
            {cfBusy ? '导入中…' : preview ? `导入 ${validCount} 条` : '导入'}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
