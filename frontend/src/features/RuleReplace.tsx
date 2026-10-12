import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { applyRule, listOperationLog, previewRule, undoOperation, type Preview } from '../api/endpoints';
import { Badge, Banner, Button, Field, Panel } from '../ui/primitives';
import { HoldToConfirm } from '../ui/HoldToConfirm';
import { ErrorView } from '../ui/ErrorView';

export function RuleReplace({ pid, selectedIds }: { pid: string; selectedIds: number[] }) {
  const qc = useQueryClient();
  const [find, setFind] = useState('');
  const [repl, setRepl] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const log = useQuery({ queryKey: ['oplog', pid], queryFn: () => listOperationLog(pid), retry: false });
  const last = log.data?.items.find((r) => r.kind?.includes('rule') && r.status !== 'undone');

  const run = async (fn: () => Promise<void>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e);
    }
  };
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['oplog', pid] });
    void qc.invalidateQueries({ queryKey: ['annotation', pid] });
  };

  return (
    <Panel title="规则替换">
      <Field label="查找"><input value={find} onChange={(e) => { setFind(e.target.value); setPreview(null); setDone(false); }} /></Field>
      <Field label="替换为"><input value={repl} onChange={(e) => { setRepl(e.target.value); setPreview(null); setDone(false); }} /></Field>
      <div className="row">
        <Button disabled={!find || !selectedIds.length} onClick={() => void run(async () => setPreview(await previewRule(pid, { op: 'replace', find, replace: repl, layer: 'manual' }, selectedIds)))}>预览</Button>
        {preview && <Badge tone="info">影响 {preview.affected_count ?? preview.total ?? '?'} 张</Badge>}
        <HoldToConfirm kind="danger" disabled={!preview} onConfirm={() => void run(async () => { await applyRule(pid, preview!); setPreview(null); setDone(true); refresh(); })}>
          长按执行
        </HoldToConfirm>
      </div>
      {!selectedIds.length && <Banner tone="info">先在网格中选择图片作为规则范围。</Banner>}
      {done && <Banner tone="success">已执行。</Banner>}
      {err != null && <ErrorView error={err} feature="规则替换" />}
      <div className="row">
        <HoldToConfirm
          kind="secondary"
          disabled={!last}
          onConfirm={() => {
            const id = last?.operation_id ?? last?.id;
            if (id != null && last?.version) void run(async () => { await undoOperation(pid, id, last.version!); refresh(); });
          }}
        >
          长按撤销最近一次
        </HoldToConfirm>
      </div>
    </Panel>
  );
}
