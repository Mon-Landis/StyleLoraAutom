import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { connectSse } from '../api/sse';
import { createJobs, listJobs, type ExecutorId, type JobMode } from '../api/more';
import { useConnection } from '../state/connection';
import { Badge, Button, Panel, ProgressBar } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';

const EXECUTORS: Array<[ExecutorId, string]> = [['wd', 'WD'], ['llm_vision', 'LLM']];
const MODES: Array<[JobMode, string]> = [['selected', '执行选中'], ['uncovered', '执行未覆盖'], ['all', '执行全部']];

export function ExecutorPanel({ pid, selectedIds }: { pid: string; selectedIds: number[] }) {
  const qc = useQueryClient();
  const dispatch = useConnection((s) => s.dispatch);
  const [picked, setPicked] = useState<Set<ExecutorId>>(new Set(['wd']));
  const [err, setErr] = useState<unknown>(null);
  const jobs = useQuery({ queryKey: ['jobs', pid], queryFn: () => listJobs(pid), retry: false });

  useEffect(() => {
    const refetch = () => void qc.invalidateQueries({ queryKey: ['jobs', pid] });
    const h = connectSse({
      projectId: pid,
      topics: ['job'],
      onEvent: (e) => { if (e.kind.startsWith('job')) refetch(); },
      onUp: () => dispatch('sse_up'),
      onDown: () => dispatch('sse_down'),
      onAuthLost: () => dispatch('unauthorized'),
      onResync: refetch,
    });
    return () => h.close();
  }, [pid, qc, dispatch]);

  const toggle = (id: ExecutorId) => {
    const n = new Set(picked);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    setPicked(n);
  };
  const run = async (mode: JobMode) => {
    setErr(null);
    try {
      await createJobs(pid, [...picked], mode, selectedIds);
      void qc.invalidateQueries({ queryKey: ['jobs', pid] });
    } catch (e) {
      setErr(e);
    }
  };

  return (
    <Panel title="执行器">
      <div className="row">
        {EXECUTORS.map(([id, label]) => (
          <label key={id} className="check">
            <input type="checkbox" checked={picked.has(id)} onChange={() => toggle(id)} /> {label}
          </label>
        ))}
      </div>
      <div className="row">
        {MODES.map(([m, label]) => (
          <Button key={m} disabled={!picked.size || (m === 'selected' && !selectedIds.length)} onClick={() => void run(m)}>{label}</Button>
        ))}
      </div>
      {err != null && <ErrorView error={err} feature="执行器" />}
      {jobs.error && <ErrorView error={jobs.error} feature="任务列表" />}
      {jobs.data?.items.map((j, n) => (
        <div key={j.job_id ?? j.id ?? n} className="job">
          <Badge tone="neutral">{j.executor}</Badge> <Badge tone="info">{j.status}</Badge>
          <ProgressBar value={j.progress_json?.done ?? 0} max={j.progress_json?.total ?? null} />
        </div>
      ))}
    </Panel>
  );
}
