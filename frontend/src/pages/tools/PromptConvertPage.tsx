import { useEffect, useRef, useState } from 'react';
import { convertPrompt, type PromptConvertWarning } from '../../api/tools';
import { createConvertController, warningText, type ConvertSide } from '../../lib/promptConvert';
import { Banner, Button } from '../../ui/primitives';
import { ErrorView } from '../../ui/ErrorView';
import { Icon } from '../../ui/slots';

const SIDES: Array<{ side: ConvertSide; label: string; id: string }> = [
  { side: 'nai', label: 'NAI', id: 'convert-nai' },
  { side: 'anima', label: 'Anima', id: 'convert-anima' },
];

/** 同 code 合并计数，保持首次出现顺序。 */
function groupWarnings(list: PromptConvertWarning[]): Array<{ code: string; count: number }> {
  const m = new Map<string, number>();
  for (const w of list) m.set(w.code, (m.get(w.code) ?? 0) + 1);
  return [...m].map(([code, count]) => ({ code, count }));
}

export function PromptConvertPage() {
  const [text, setText] = useState<Record<ConvertSide, string>>({ nai: '', anima: '' });
  const [warnings, setWarnings] = useState<PromptConvertWarning[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<ConvertSide | null>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [ctl] = useState(() =>
    createConvertController(
      (direction, t, signal) => convertPrompt(direction, t, signal),
      {
        onResult: (target, r) => {
          setText((s) => ({ ...s, [target]: r.text }));
          setWarnings(r.warnings);
          setError(null);
        },
        onCleared: (target) => {
          setText((s) => ({ ...s, [target]: '' }));
          setWarnings([]);
          setError(null);
        },
        // 失败只显示错误，不动任何一侧输入。
        onError: (e) => setError(e),
        onBusy: setBusy,
      },
    ),
  );
  useEffect(() => () => {
    ctl.dispose();
    if (copyTimer.current) clearTimeout(copyTimer.current);
  }, [ctl]);

  const edit = (side: ConvertSide, value: string) => {
    setText((s) => ({ ...s, [side]: value }));
    ctl.edit(side, value);
  };

  const copy = async (side: ConvertSide) => {
    try {
      await navigator.clipboard.writeText(text[side]);
      setCopied(side);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(null), 1500);
    } catch {
      setError(new Error('复制失败：浏览器未授予剪贴板权限'));
    }
  };

  const grouped = groupWarnings(warnings);

  return (
    <div className="page convert">
      <h2 className="page__title">提示词转换</h2>
      <p className="convert__rules">
        规则：下划线 ⇄ 空格；非画师 tag 权重大于 1 时 NAI→Anima 按 1+(w−1)×0.6，反向相除；权重 ≤1 两侧数值不变；Anima 侧钳制 ≤3.2；画师权重不缩放。
      </p>

      {SIDES.map(({ side, label, id }) => (
        <div key={side} className="convert__box">
          <div className="convert__head">
            <label htmlFor={id} className="convert__label">{label}</label>
            <div className="convert__actions">
              <Button variant="ghost" aria-label={`复制 ${label}`} disabled={text[side] === ''} onClick={() => void copy(side)}>
                <Icon name="copy" />{copied === side ? '已复制' : '复制'}
              </Button>
              <Button variant="ghost" aria-label={`清空 ${label}`} disabled={text[side] === ''} onClick={() => edit(side, '')}>
                <Icon name="close" />清空
              </Button>
            </div>
          </div>
          <textarea
            id={id}
            className="convert__text"
            rows={8}
            spellCheck={false}
            value={text[side]}
            onChange={(e) => edit(side, e.target.value)}
          />
        </div>
      ))}

      <div className="convert__status" aria-live="polite">
        {busy ? <span className="convert__busy">转换中…</span> : null}
        {grouped.length > 0 ? (
          <Banner tone="warning">
            <ul className="convert__warnings" aria-label="转换告警">
              {grouped.map((w) => (
                <li key={w.code}>{warningText(w.code)}{w.count > 1 ? `（${w.count} 处）` : ''}</li>
              ))}
            </ul>
          </Banner>
        ) : null}
      </div>
      {error ? <ErrorView error={error} feature="提示词转换" /> : null}
    </div>
  );
}
