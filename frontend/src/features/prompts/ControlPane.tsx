import { Button } from '../../ui/primitives';

export type ControlAction = 'add' | 'append' | 'import' | 'export' | 'generate' | 'settings';

/** 右栏控制面板。上：编辑/导入导出；下对齐：生成、设置。 */
export function ControlPane(p: {
  hasGroup: boolean;
    /** 非空表示尚未选择目标组。 */
  generateBlockedReason: string | null;
  busy: boolean;
  onAction: (a: ControlAction) => void;
}) {
  const need = !p.hasGroup || p.busy;
  return (
    <aside className="pg-right" aria-label="词典控制面板">
      <div className="pg-right__top">
        <Button disabled={need} onClick={() => p.onAction('add')}>添加提示词</Button>
        <Button disabled={need} onClick={() => p.onAction('append')}>追加自然语言</Button>
        {/* 批量导入可新建词典，不依赖已选词典 */}
        <Button disabled={p.busy} onClick={() => p.onAction('import')}>批量导入</Button>
        <Button disabled={need} onClick={() => p.onAction('export')}>导出词典</Button>
      </div>
      <div className="pg-right__bottom">
        {p.generateBlockedReason && <p className="pg-muted" id="pg-gen-hint">{p.generateBlockedReason}</p>}
        {p.hasGroup && <Button
          variant="primary"
          disabled={!!p.generateBlockedReason}
          aria-describedby={p.generateBlockedReason ? 'pg-gen-hint' : undefined}
          onClick={() => p.onAction('generate')}
        >
          生成
        </Button>}
        <Button disabled={!p.hasGroup} onClick={() => p.onAction('settings')}>设置</Button>
      </div>
    </aside>
  );
}
