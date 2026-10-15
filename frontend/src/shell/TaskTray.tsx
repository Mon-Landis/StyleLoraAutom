import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { cancelTask, fetchTasks, isRunning, KIND_LABEL, runningCount, STATUS_LABEL, type TaskItem } from '../api/tasks';
import { useApp } from '../state/app';
import { Badge, Banner, Button, ProgressBar } from '../ui/primitives';
import { Popover } from '../ui/Popover';
import { ErrorView } from '../ui/ErrorView';
import { Empty, Icon } from '../ui/slots';
import { ConnDot } from './ConnDot';

const POLL_IDLE_MS = 15000;
const POLL_ACTIVE_MS = 2000;

function TaskRow({ t, onCancel, busy }: { t: TaskItem; onCancel: (t: TaskItem) => void; busy: boolean }) {
  return (
    <li className="task">
      <div className="task__head">
        <Badge tone="neutral">{KIND_LABEL[t.kind]}</Badge>
        <span className="task__title" title={t.title}>{t.title}</span>
        <span className="task__id">#{t.resourceId}</span>
      </div>
      <div className="task__body">
        <ProgressBar value={t.progress.completed} max={t.progress.total} />
        <span className="task__count">{t.progress.completed}{t.progress.total != null ? ` / ${t.progress.total}` : ''}</span>
        <Badge tone={isRunning(t.status) ? 'info' : 'warning'}>{STATUS_LABEL[t.status]}</Badge>
        <Button
          variant="danger"
          disabled={!t.cancelable || busy}
          title={t.cancelable ? '取消任务' : '该任务暂不支持取消'}
          aria-label={`取消 ${t.title}`}
          onClick={() => onCancel(t)}
        >
          取消
        </Button>
      </div>
    </li>
  );
}

/** 顶栏右侧：运行中任务数按钮 + 任务托盘 + 连接状态点。 */
export function TaskTray() {
  const pid = useApp((s) => s.projectId);
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ['tasks', pid],
    queryFn: () => fetchTasks(pid),
    refetchInterval: (query) => (open || (query.state.data && runningCount(query.state.data.items) > 0) ? POLL_ACTIVE_MS : POLL_IDLE_MS),
  });
  const cancel = useMutation({
    mutationFn: cancelTask,
    onSettled: () => void qc.invalidateQueries({ queryKey: ['tasks'] }),
  });
  const items = q.data?.items ?? [];
  const n = runningCount(items);

  return (
    <div className="task-tray">
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        label="任务托盘"
        align="end"
        trigger={
          <Button
            variant="ghost"
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-label={`任务托盘，运行中 ${n}`}
            title="任务托盘"
            onClick={() => setOpen((v) => !v)}
          >
            <Icon name="tasks" />
            <Badge tone={n > 0 ? 'accent' : 'neutral'}>{n}</Badge>
          </Button>
        }
      >
        <div className="task-tray__panel">
          {q.data && q.data.unavailable.length > 0 && (
            <Banner tone="warning">部分来源暂不可用：{q.data.unavailable.map((k) => KIND_LABEL[k]).join('、')}</Banner>
          )}
          {q.error != null && <ErrorView error={q.error} feature="任务列表" />}
          {cancel.error != null && <ErrorView error={cancel.error} feature="取消任务" />}
          {items.length === 0 ? (
            <Empty title="没有进行中的任务" />
          ) : (
            <ul className="task-tray__list">
              {items.map((t) => (
                <TaskRow key={t.key} t={t} busy={cancel.isPending} onCancel={(x) => cancel.mutate(x)} />
              ))}
            </ul>
          )}
        </div>
      </Popover>
      <ConnDot />
    </div>
  );
}
