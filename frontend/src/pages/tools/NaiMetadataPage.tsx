import { useState } from 'react';
import { ApiError } from '../../api/errors';
import { runNaiMetadata, type NaiMetadataFile, type NaiMetadataResult, type NaiMetadataSource } from '../../api/tools';
import { describeNaiMetadataError, describeNaiIssue, naiStatusLabel } from '../../lib/naiMetadata';
import { useUi, type NaiMetadataDropMode } from '../../state/ui';
import { Banner, Button } from '../../ui/primitives';
import { Unsupported } from '../../ui/slots';

const DROP_MODES: Array<{ value: NaiMetadataDropMode; label: string; hint: string }> = [
  { value: 'none', label: '不丢弃', hint: '' },
  { value: 'head', label: '丢弃头部', hint: '丢弃正面提示词第一个完整空行之前的内容' },
  { value: 'global', label: '丢弃全局提示词', hint: '只保留角色提示词，角色之间隔一个完整空行' },
];

/** 当前运行中的来源：决定「选择图片…/选择文件夹…」哪个按钮显示忙碌态。 */
type Pending = NaiMetadataSource | null;

export function NaiMetadataPage() {
  const dropMode = useUi((s) => s.naiMetadataDropMode);
  const setDropMode = useUi((s) => s.setNaiMetadataDropMode);
  const toAnima = useUi((s) => s.naiMetadataToAnima);
  const setToAnima = useUi((s) => s.setNaiMetadataToAnima);

  const [pending, setPending] = useState<Pending>(null);
  const [result, setResult] = useState<NaiMetadataResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [canceledNote, setCanceledNote] = useState(false);

  const busy = pending !== null;

  const run = async (source: NaiMetadataSource) => {
    if (busy) return;
    setPending(source);
    setError(null);
    setCanceledNote(false);
    try {
      const r = await runNaiMetadata({ source, dropMode, toAnima });
      if (r.canceled) {
        setCanceledNote(true);
      } else {
        setResult(r);
      }
    } catch (e) {
      setError(e);
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="page nai-md">
      <h2 className="page__title">NAI 元数据打印</h2>
      <p className="nai-md__note">
        生成的 txt 与图片同名同目录，已存在的同名 txt 会被覆盖；仅支持 PNG。
      </p>

      <fieldset className="nai-md__fieldset" disabled={busy}>
        <legend>丢弃模式</legend>
        {DROP_MODES.map((o) => (
          <label key={o.value} className="check">
            <input
              type="radio"
              name="nai-md-drop-mode"
              value={o.value}
              checked={dropMode === o.value}
              onChange={() => setDropMode(o.value)}
            />{' '}
            {o.label}
            {o.hint ? <span className="pg-muted"> {o.hint}</span> : null}
          </label>
        ))}
      </fieldset>

      <label className="check">
        <input
          type="checkbox"
          checked={toAnima}
          disabled={busy}
          onChange={(e) => setToAnima(e.target.checked)}
        />{' '}
        转换为 Anima
      </label>

      <div className="nai-md__actions">
        <Button variant="primary" disabled={busy} onClick={() => void run('files')}>
          {pending === 'files' ? '等待选择 / 处理中…' : '选择图片…'}
        </Button>
        <Button disabled={busy} onClick={() => void run('folder')}>
          {pending === 'folder' ? '等待选择 / 处理中…' : '选择文件夹…'}
        </Button>
        <span className="visually-hidden" role="status" aria-live="polite">
          {busy ? '等待选择 / 处理中…' : ''}
        </span>
      </div>

      {canceledNote ? <Banner tone="info">已取消选择，未处理任何文件。</Banner> : null}
      {error ? <NaiMetadataErrorView error={error} /> : null}

      {result && !result.canceled ? <NaiMetadataResultView result={result} /> : null}
    </div>
  );
}

/** 501 走统一占位；其余用本工具的中文文案表。 */
function NaiMetadataErrorView({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.unsupported) return <Unsupported feature="NAI 元数据打印" />;
  return <Banner tone="danger">{describeNaiMetadataError(error)}</Banner>;
}

function NaiMetadataResultView({ result }: { result: NaiMetadataResult }) {
  const { summary, root, files } = result;
  return (
    <div className="nai-md__result" aria-live="polite">
      <p className="nai-md__summary">
        共 {summary.total} 张：写入 {summary.written}，跳过 {summary.skipped}，失败 {summary.failed}
      </p>
      {root ? <p className="nai-md__root">目录：{root}</p> : null}
      <ul className="nai-md__list" aria-label="处理结果">
        {files.map((f) => <NaiMetadataFileRow key={f.path} file={f} />)}
      </ul>
    </div>
  );
}

function NaiMetadataFileRow({ file }: { file: NaiMetadataFile }) {
  const statusLabel = naiStatusLabel(file.status, file.overwritten);
  // issues 更完整时优先展示；否则回退到 reason（两者通常指向同一件事，不重复显示）。
  const issueTexts = file.issues.map((i) => describeNaiIssue(i.code));
  const reasonText = issueTexts.length === 0 && file.reason ? describeNaiIssue(file.reason) : null;
  return (
    <li className={`nai-md__row nai-md__row--${file.status}`}>
      <span className="nai-md__name">{file.name}</span>
      <span className="nai-md__status">{statusLabel}</span>
      {issueTexts.length > 0 ? <span className="nai-md__issues">{issueTexts.join('；')}</span> : null}
      {reasonText ? <span className="nai-md__reason">{reasonText}</span> : null}
      {file.warnings.length > 0 ? <span className="nai-md__warnings">转换告警 {file.warnings.length} 处</span> : null}
      {file.status === 'written' ? <span className="nai-md__chars">{file.chars} 字</span> : null}
    </li>
  );
}
