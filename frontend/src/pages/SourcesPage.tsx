import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createSource, listSources } from '../api/more';
import { useApp } from '../state/app';
import { Badge, Button, Field, Panel } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Empty, Loading } from '../ui/slots';

export function SourcesPage() {
  const pid = useApp((s) => s.projectId);
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [type, setType] = useState('local_folder');
  const q = useQuery({ queryKey: ['sources', pid], queryFn: () => listSources(pid!), enabled: !!pid });
  const m = useMutation({
    mutationFn: () => createSource(pid!, { type, name: name.trim() }),
    onSuccess: () => { setName(''); void qc.invalidateQueries({ queryKey: ['sources', pid] }); },
  });
  if (!pid) return <Empty title="请先打开项目" />;
  return (
    <div className="page">
      <Panel title="数据源">
        {q.isLoading && <Loading />}
        {q.error && <ErrorView error={q.error} feature="数据源列表" />}
        {q.data && q.data.items.length === 0 && <Empty title="暂无数据源" />}
        <ul className="list">
          {q.data?.items.map((s) => (
            <li key={s.source_id} className="list__row">
              <span>{s.name}</span><Badge>{s.type}</Badge>{s.status && <Badge tone="info">{s.status}</Badge>}
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="新建数据源">
        <div className="form-row">
          <Field label="名称"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="类型"><input value={type} onChange={(e) => setType(e.target.value)} /></Field>
          <Button variant="primary" disabled={!name.trim() || m.isPending} onClick={() => m.mutate()}>创建</Button>
        </div>
        {m.error && <ErrorView error={m.error} feature="创建数据源" />}
      </Panel>
    </div>
  );
}
