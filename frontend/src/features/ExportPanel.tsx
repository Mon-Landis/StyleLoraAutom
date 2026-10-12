import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getOperation, listExportSchemes, previewExport, runExport, type ExportPreview } from '../api/more';
import type { Operation } from '../api/types';
import { Badge, Banner, Button, Field, Panel, ProgressBar } from '../ui/primitives';
import { HoldToConfirm } from '../ui/HoldToConfirm';
import { ErrorView } from '../ui/ErrorView';

export function ExportPanel({ pid }: { pid: string }) {
  const schemes = useQuery({ queryKey: ['export-schemes', pid], queryFn: () => listExportSchemes(pid), retry: false });
  const [scheme, setScheme] = useState<number | ''>('');
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [op, setOp] = useState<Operation | null>(null);
  const [err, setErr] = useState<unknown>(null);

  const guard = async (fn: () => Promise<void>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e);
    }
  };
  const follow = async (start: Operation) => {
    let cur = start;
    setOp(cur);
    while (cur.status === 'pending' || cur.status === 'running') {
      await new Promise((r) => setTimeout(r, 500));
      cur = await getOperation(cur.operation_id);
      setOp(cur);
    }
  };
  const blocking = preview?.issues.filter((i) => i.severity === 'error') ?? [];

  return (
    <Panel title="导出">
      {schemes.error && <ErrorView error={schemes.error} feature="导出方案" />}
      <Field label="方案">
        <select value={scheme} onChange={(e) => { setScheme(e.target.value ? Number(e.target.value) : ''); setPreview(null); }}>
          <option value="">选择方案</option>
          {schemes.data?.items.map((s) => <option key={s.scheme_id} value={s.scheme_id}>{s.name}</option>)}
        </select>
      </Field>
      <div className="row">
        <Button disabled={scheme === ''} onClick={() => void guard(async () => setPreview(await previewExport(pid, Number(scheme))))}>预览</Button>
        {preview && <Badge tone="info">将导出 {preview.selected} 张</Badge>}
        <HoldToConfirm kind="secondary" disabled={!preview || blocking.length > 0} onConfirm={() => void guard(async () => follow(await runExport(pid, preview!)))}>
          长按导出
        </HoldToConfirm>
      </div>
      {preview?.issues.map((i, n) => <Banner key={n} tone={i.severity === 'error' ? 'danger' : 'warning'}>{i.code}</Banner>)}
      {op && (
        <div>
          <ProgressBar value={op.completed} max={op.total} />
          <Badge tone={op.status === 'done' ? 'success' : op.status === 'failed' ? 'danger' : 'info'}>{op.status}</Badge>
          {op.error && <Banner tone="danger">{op.error.code}</Banner>}
        </div>
      )}
      {err != null && <ErrorView error={err} feature="导出" />}
    </Panel>
  );
}
