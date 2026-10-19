import { useState } from 'react';
import { patchGroup } from '../../api/promptGroups';
import type { GroupRef } from '../../api/promptGroupTypes';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button } from '../../ui/primitives';
import { useSubmit } from './useSubmit';

/** 词典设置：标签列表设置 / 生成源配置（占位，不提供无效保存）与是否 nsfw 组。 */
export function GroupSettingsDialog({ gref, nsfw, name, etag, onClose, onDone }: {
  gref: GroupRef;
  nsfw: boolean;
  name: string;
  etag: string | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [value, setValue] = useState(nsfw);
  const changed = value !== nsfw;
  const s = useSubmit(
    (key) => patchGroup(gref, { nsfw: value }, etag!, key),
    () => { onDone(); onClose(); },
  );
  return (
    <Dialog title={`设置 · ${name}`} onClose={onClose}>
      <div className="pg-form">
        <section aria-labelledby="pg-set-tags">
          <h4 id="pg-set-tags" className="pg-form__h">标签列表设置</h4>
          {/* TODO(spec): 文档未定义标签列表设置的字段，待产品给出后接入 */}
          <p className="pg-muted">暂未实现：设计文档尚未定义标签列表设置项。</p>
        </section>
        <section aria-labelledby="pg-set-src">
          <h4 id="pg-set-src" className="pg-form__h">生成源配置</h4>
          <p className="pg-muted">暂未实现：生成源配置将在后续版本提供。</p>
        </section>
        <section aria-labelledby="pg-set-nsfw">
          <h4 id="pg-set-nsfw" className="pg-form__h">内容分级</h4>
          <label className="check"><input type="checkbox" checked={value} onChange={(e) => setValue(e.target.checked)} /> 这是 nsfw 词典</label>
          <p className="pg-muted">仅影响前端显示：关闭全局 NSFW 开关时，该词典会被隐藏，预览图会被模糊。</p>
        </section>
        {s.error && <Banner tone="danger">{s.error}</Banner>}
        <div className="pg-form__actions">
          <Button onClick={onClose}>关闭</Button>
          <Button variant="primary" disabled={s.busy || !etag || !changed} onClick={() => void s.submit()}>
            {s.busy ? '保存中…' : '保存'}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
