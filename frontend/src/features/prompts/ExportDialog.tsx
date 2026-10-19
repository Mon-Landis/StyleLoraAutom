import { useEffect, useRef, useState } from 'react';
import { exportGroup } from '../../api/promptGroups';
import type { ExportFormat, GroupRef } from '../../api/promptGroupTypes';
import { downloadBlob, safeFileName } from '../../lib/download';
import { describePgError } from '../../lib/promptErrors';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button } from '../../ui/primitives';

const OPTIONS: Array<{ value: ExportFormat; label: string; hint: string; ext: string }> = [
  { value: 'json', label: 'JSON', hint: '仅提示词内容（不含图片与内部状态），可直接导入。', ext: 'json' },
  {
    value: 'nai_image_zip', label: '图像（ZIP）',
    hint: '每个条目一张 PNG，提示词写入图片元数据；没有参考图的条目使用占位图。整包可完整回导；单独 PNG 只能恢复提示词与参考图。',
    ext: 'zip',
  },
];

/**
 * 导出格式选择。下载失败时留在对话框并保留所选格式，可直接重试。
 * 请求随关闭/卸载中止，迟到的响应不会触发下载。
 */
export function ExportDialog({ gref, groupName, onClose }: {
  gref: GroupRef;
  groupName: string;
  onClose: () => void;
}) {
  const [format, setFormat] = useState<ExportFormat>('json');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);

  const submit = async () => {
    if (busy) return;
    const ctl = new AbortController();
    abort.current = ctl;
    setBusy(true);
    setError(null);
    try {
      const blob = await exportGroup(gref, format, ctl.signal);
      if (ctl.signal.aborted) return;
      downloadBlob(blob, safeFileName(groupName, OPTIONS.find((o) => o.value === format)!.ext));
      onClose();
    } catch (e) {
      if (ctl.signal.aborted) return;
      setError(describePgError(e).text);
      setBusy(false);
    }
  };

  return (
    <Dialog title="导出词典" onClose={onClose}>
      <div className="pg-form">
        <fieldset disabled={busy}>
          <legend>导出格式</legend>
          {OPTIONS.map((o) => (
            <label key={o.value} className="check">
              <input
                type="radio"
                name="pg-export-format"
                value={o.value}
                checked={format === o.value}
                onChange={() => { setFormat(o.value); setError(null); }}
              />{' '}
              {o.label}
              <span className="pg-muted"> {o.hint}</span>
            </label>
          ))}
        </fieldset>
        {error && <Banner tone="danger">{error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={busy} onClick={() => void submit()}>{busy ? '导出中…' : '导出'}</Button>
        </div>
      </div>
    </Dialog>
  );
}
