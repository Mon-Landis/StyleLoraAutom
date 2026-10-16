import { useState, type FormEvent } from 'react';
import { createGroup, patchGroup } from '../../api/promptGroups';
import type { GroupSummary, GroupView, Scope } from '../../api/promptGroupTypes';
import { defaultScope, scopeBlocked } from '../../lib/scope';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button, Field } from '../../ui/primitives';
import { useSubmit } from './useSubmit';

/** 新建词典：名称、作用范围（默认按项目状态，用户选择优先）、是否 nsfw。 */
export function GroupCreateDialog({ projectId, onClose, onDone }: {
  projectId: string | null;
  onClose: () => void;
  onDone: (g: GroupSummary) => void;
}) {
  const [name, setName] = useState('');
  const [choice, setChoice] = useState<Scope | null>(null);
  const [nsfw, setNsfw] = useState(false);
  const scope = defaultScope(projectId, choice);
  const blocked = scopeBlocked(scope, projectId);
  const s = useSubmit(
    (key) => createGroup({
      name: name.trim(), kind: 'prompts', category: 'full_content', nsfw, scope,
      project_token: scope === 'project' ? projectId : null,
    }, key),
    onDone,
  );
  const can = name.trim().length > 0 && name.trim().length <= 128 && !blocked && !s.busy;
  const submit = (e: FormEvent) => { e.preventDefault(); if (can) void s.submit(); };
  return (
    <Dialog title="新建词典" onClose={onClose}>
      <form className="pg-form" onSubmit={submit}>
        <Field label="名称（1–128 字符）"><input value={name} maxLength={128} autoFocus onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="作用范围">
          <select value={scope} onChange={(e) => setChoice(e.target.value as Scope)}>
            <option value="global">全局</option>
            <option value="project">项目</option>
          </select>
        </Field>
        {blocked && <Banner tone="warning">未打开项目，不能创建项目级词典。请先打开项目或改选全局。</Banner>}
        <label className="check"><input type="checkbox" checked={nsfw} onChange={(e) => setNsfw(e.target.checked)} /> 这是 nsfw 词典</label>
        {s.error && <Banner tone="danger">{s.error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" disabled={!can}>{s.busy ? '创建中…' : '创建'}</Button>
        </div>
      </form>
    </Dialog>
  );
}

/** 信息修改（右键「信息修改」与设置里的 nsfw 开关共用）。scope / id 不可变。 */
export function GroupEditDialog({ title = '修改词典信息', group, etag, onClose, onDone }: {
  title?: string;
  group: Pick<GroupView, 'dictionary_id' | 'name' | 'nsfw' | 'scope'> & { project_token?: string | null };
  etag: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(group.name);
  const [nsfw, setNsfw] = useState(group.nsfw);
  const s = useSubmit(
    (key) => patchGroup(
      { scope: group.scope, project_token: group.project_token ?? null, group_id: group.dictionary_id },
      { name: name.trim(), nsfw }, etag, key,
    ),
    () => { onDone(); onClose(); },
  );
  const can = name.trim().length > 0 && !s.busy;
  const submit = (e: FormEvent) => { e.preventDefault(); if (can) void s.submit(); };
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="pg-form" onSubmit={submit}>
        <Field label="名称"><input value={name} maxLength={128} autoFocus onChange={(e) => setName(e.target.value)} /></Field>
        <label className="check"><input type="checkbox" checked={nsfw} onChange={(e) => setNsfw(e.target.checked)} /> 是 nsfw 词典</label>
        <p className="pg-muted">ID 与作用范围创建后不可修改。</p>
        {s.error && <Banner tone="danger">{s.error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" disabled={!can}>{s.busy ? '保存中…' : '保存'}</Button>
        </div>
      </form>
    </Dialog>
  );
}
