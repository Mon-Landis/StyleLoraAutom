import { useId, type ReactNode } from 'react';
import { Icon } from '../../ui/slots';

/** 行内标记：图标 + 文字，不只靠颜色。用于卡片与文本框状态行。 */
export function FieldMark({ generated, pending }: { generated: boolean; pending: boolean }) {
  if (pending) {
    return <span className="gen-mark gen-mark--pending"><Icon name="generating" /> 生成中</span>;
  }
  if (generated) {
    return <span className="gen-mark gen-mark--generated"><Icon name="ai-generated" /> AI 生成</span>;
  }
  return null;
}

type RenderArgs = { describedBy: string | undefined; busy: boolean };

/**
 * 字段级生成标记容器。包裹位置恒定，标记出现/消失不会让子文本框重新挂载（输入中清标记不丢焦点）。
 * children 可为函数以拿到 aria-describedby。pending 仍允许人工编辑，仅提示。
 */
export function GeneratedField({ generated, pending, children }: {
  generated: boolean;
  pending: boolean;
  children: ReactNode | ((a: RenderArgs) => ReactNode);
}) {
  const id = useId();
  const state = pending ? 'pending' : generated ? 'generated' : 'none';
  const describedBy = state === 'none' ? undefined : id;
  return (
    <div className="gen" data-gen={state} aria-busy={pending || undefined}>
      {state !== 'none' && (
        <div className="gen__status" id={id}>
          <FieldMark generated={generated} pending={pending} />
          <span className="gen__hint">
            {pending ? '正在由模型填写，仍可手动编辑' : '模型生成，尚未人工编辑；修改后标记自动清除'}
          </span>
        </div>
      )}
      {typeof children === 'function' ? children({ describedBy, busy: pending }) : children}
    </div>
  );
}
