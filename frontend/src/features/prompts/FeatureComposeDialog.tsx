import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createPromptTask, listPromptTasks } from '../../api/more';
import { Dialog } from '../../ui/Dialog';
import { Badge, Banner, Button, Field } from '../../ui/primitives';
import { Empty, Loading } from '../../ui/slots';
import { ErrorView } from '../../ui/ErrorView';
import { useSubmit } from './useSubmit';

/**
 * 保留旧抽样流程，供后续特征库组合生成接入；不挂到当前「生成」入口。
 * 沿用 POST /api/prompt-tasks，plan 待特征库配置落地后替换。
 */
export function FeatureComposeDialog({ groupId, groupName, onClose }: { groupId: string; groupName: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [count, setCount] = useState(1);
  const tasks = useQuery({ queryKey: ['prompt-tasks'], queryFn: listPromptTasks });
  const s = useSubmit(
    () => createPromptTask({
      // TODO(backend): plan 的维度/取值沿用旧页面占位，待生成源配置落地后改为由源提供
      plan: { primary_dimensions: ['scene', 'lighting'], cell_values: { scene: ['forest'], lighting: ['sunlight'] }, target_count: count },
      prompt_dictionary_ids: [groupId],
      coverage_enabled: false,
      optimize: false,
    }),
    () => void qc.invalidateQueries({ queryKey: ['prompt-tasks'] }),
  );
  return (
    <Dialog title={`生成 · ${groupName}`} onClose={onClose}>
      <div className="pg-form">
        <div className="form-row">
          <Field label="目标条数">
            <input type="number" min={1} max={1000} value={count} onChange={(e) => setCount(Math.max(1, Math.floor(Number(e.target.value)) || 1))} />
          </Field>
          <Button variant="primary" disabled={s.busy} onClick={() => void s.submit()}>{s.busy ? '创建中…' : '创建任务'}</Button>
        </div>
        {s.error && <Banner tone="danger">{s.error}</Banner>}
        <h4 className="pg-form__h">提示词任务</h4>
        {tasks.isLoading && <Loading />}
        {tasks.error && <ErrorView error={tasks.error} feature="提示词任务" />}
        {tasks.data && tasks.data.items.length === 0 && <Empty title="暂无任务" />}
        <ul className="list" aria-live="polite">
          {tasks.data?.items.map((t, i) => (
            <li key={String(t.task_id ?? t.id ?? i)} className="list__row">
              <span>{t.name ?? `#${t.task_id ?? t.id}`}</span>{t.status && <Badge tone="info">{t.status}</Badge>}
            </li>
          ))}
        </ul>
      </div>
    </Dialog>
  );
}
