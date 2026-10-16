import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/errors';
import { closeProject, createProject, currentProject, listKnownProjects, openProject, pickFolder } from '../api/more';
import { waitOperation } from '../lib/operation';
import { useApp } from '../state/app';
import { useSaveStatus } from '../state/saveStatus';
import { Button, Field } from '../ui/primitives';
import { Dialog } from '../ui/Dialog';
import { Popover } from '../ui/Popover';
import { Icon } from '../ui/slots';

/** 顶栏项目块：「打开项目」按钮 + 项目名（右下角对齐一行保存状态小字）。 */
export function ProjectBar() {
  const qc = useQueryClient();
  const { projectId, projectName, projectVersion, setProject } = useApp();
  const save = useSaveStatus();
  const known = useQuery({ queryKey: ['known-projects'], queryFn: listKnownProjects });
  const cur = useQuery({ queryKey: ['current-project'], queryFn: currentProject });
  const [menu, setMenu] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [trigger, setTrigger] = useState('');
  const [msg, setMsg] = useState('');

  useEffect(() => {
    const p = cur.data;
    if (p) setProject(p.project_id, p.version, p.metadata?.name ?? p.project_id, p.metadata?.mode ?? null);
  }, [cur.data, setProject]);

  const fail = (e: unknown) =>
    setMsg(e instanceof ApiError && e.status === 501 ? '系统目录选择器不可用（后端未加载 tkinter）'
      : e instanceof ApiError && e.status === 409 ? '该位置已存在同名项目'
      : e instanceof Error ? e.message : String(e));

  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ['current-project'] });
    await qc.invalidateQueries({ queryKey: ['known-projects'] });
  };
  const grantFromPicker = async (purpose: 'project_create' | 'project_open') => {
    const done = await waitOperation(await pickFolder(purpose));
    const r = done.result as { grant_id?: string } | null;
    if (!r?.grant_id) throw new Error('未选择目录');
    return r.grant_id;
  };
  const open = async (body: { grant_id?: string; known_id?: string }) => {
    setMsg('');
    setMenu(false);
    try {
      await waitOperation(await openProject(body));
      await refresh();
    } catch (e) { fail(e); }
  };
  const create = async () => {
    setMsg('');
    try {
      const grant = await grantFromPicker('project_create');
      await waitOperation(await createProject({ grant_id: grant, name, trigger_word: trigger || undefined }));
      setCreating(false);
      await refresh();
    } catch (e) { fail(e); }
  };
  const close = () => {
    if (!projectId || !projectVersion) return;
    setMenu(false);
    void closeProject(projectId, projectVersion).then(() => { setProject(null, null, ''); void refresh(); }).catch(fail);
  };

  return (
    <div className="projectbar">
      <Popover
        open={menu}
        onClose={() => setMenu(false)}
        label="项目菜单"
        trigger={
          <Button aria-haspopup="dialog" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>
            <Icon name="project-open" />打开项目
          </Button>
        }
      >
        <ul className="menu">
          {(known.data?.items ?? []).map((k) => (
            <li key={k.known_id}>
              <button type="button" className="menu__item" onClick={() => void open({ known_id: k.known_id })}>{k.display_name}</button>
            </li>
          ))}
          {(known.data?.items.length ?? 0) === 0 && <li className="menu__hint">没有最近项目</li>}
          <li><button type="button" className="menu__item" onClick={() => void grantFromPicker('project_open').then((g) => open({ grant_id: g })).catch(fail)}>选择目录…</button></li>
          <li><button type="button" className="menu__item" onClick={() => { setMenu(false); setCreating(true); }}>新建项目…</button></li>
          {projectId && <li><button type="button" className="menu__item menu__item--danger" onClick={close}>关闭当前项目</button></li>}
        </ul>
      </Popover>
      <div className="projectbar__title">
        <span className="projectbar__name" title={projectId ?? undefined}>{projectId ? projectName : '—'}</span>
        <small className={`projectbar__save projectbar__save--${save.tone}`} role="status" aria-live="polite">{save.label}</small>
      </div>
      {msg && <span className="projectbar__msg" role="alert">{msg}</span>}
      {creating && (
        <Dialog title="新建项目" onClose={() => setCreating(false)}>
          <Field label="名称"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="触发词（可选）"><input value={trigger} onChange={(e) => setTrigger(e.target.value)} /></Field>
          <Button variant="primary" disabled={!name.trim()} onClick={() => void create()}>选择目录并创建</Button>
        </Dialog>
      )}
    </div>
  );
}
