import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/errors';
import { closeProject, createProject, currentProject, listKnownProjects, openProject, pickFolder } from '../api/more';
import { waitOperation } from '../lib/operation';
import { useApp } from '../state/app';
import { Button, Field } from '../ui/primitives';
import { Dialog } from '../ui/Dialog';

export function ProjectBar() {
  const qc = useQueryClient();
  const { projectId, projectName, projectVersion, setProject } = useApp();
  const known = useQuery({ queryKey: ['known-projects'], queryFn: listKnownProjects });
  const cur = useQuery({ queryKey: ['current-project'], queryFn: currentProject });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [trigger, setTrigger] = useState('');
  const [msg, setMsg] = useState('');

  useEffect(() => {
    const p = cur.data;
    if (p) setProject(p.project_id, p.version, p.metadata?.name ?? p.project_id);
  }, [cur.data, setProject]);

  const fail = (e: unknown) =>
    setMsg(e instanceof ApiError && e.status === 501 ? '后端未实现（目录选择器不可用）' : e instanceof Error ? e.message : String(e));

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

  return (
    <div className="projectbar">
      {projectId ? (
        <>
          <span className="projectbar__name" title={projectId}>{projectName}</span>
          <Button variant="ghost" onClick={() => {
            if (projectVersion) void closeProject(projectId, projectVersion).then(() => { setProject(null, null, ''); void refresh(); }).catch(fail);
          }}>关闭</Button>
        </>
      ) : (
        <>
          <select aria-label="最近项目" defaultValue="" onChange={(e) => e.target.value && void open({ known_id: e.target.value })}>
            <option value="">最近项目…</option>
            {(known.data?.items ?? []).map((k) => <option key={k.known_id} value={k.known_id}>{k.display_name}</option>)}
          </select>
          <Button onClick={() => void grantFromPicker('project_open').then((g) => open({ grant_id: g })).catch(fail)}>打开…</Button>
          <Button variant="primary" onClick={() => setCreating(true)}>新建</Button>
        </>
      )}
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
