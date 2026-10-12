import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createPromptTask, listPromptTasks } from '../api/more';
import { Badge, Button, Field, Panel } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Empty, Loading } from '../ui/slots';

export function PromptsPage() {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const q = useQuery({ queryKey: ['prompt-tasks'], queryFn: listPromptTasks });
  const m = useMutation({
    mutationFn: () => createPromptTask({ plan: { primary_dimensions: ['scene', 'lighting'], cell_values: { scene: ['forest'], lighting: ['sunlight'] }, target_count: 1 }, prompt_dictionary_ids: [name.trim()], coverage_enabled: false, optimize: false }),
    onSuccess: () => { setName(''); void qc.invalidateQueries({ queryKey: ['prompt-tasks'] }); },
  });
  return (
    <div className="page">
      <Panel title="提示词任务">
        {q.isLoading && <Loading />}
        {q.error && <ErrorView error={q.error} feature="提示词任务" />}
        {q.data && q.data.items.length === 0 && <Empty title="暂无任务" />}
        <ul className="list">
          {q.data?.items.map((t, i) => (
            <li key={String(t.task_id ?? t.id ?? i)} className="list__row">
              <span>{t.name ?? `#${t.task_id ?? t.id}`}</span>{t.status && <Badge tone="info">{t.status}</Badge>}
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="新建任务">
        <div className="form-row">
          <Field label="提示词字典 ID"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Button variant="primary" disabled={!name.trim() || m.isPending} onClick={() => m.mutate()}>创建</Button>
        </div>
        {m.error && <ErrorView error={m.error} feature="创建提示词任务" />}
      </Panel>
    </div>
  );
}
