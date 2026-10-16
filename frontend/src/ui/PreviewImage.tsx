import { useState } from 'react';
import { useBlobUrl } from '../lib/useBlobUrl';
import { imagePolicy } from '../lib/nsfw';
import { previewPath } from '../api/promptGroups';
import type { GroupRef, PreviewRef } from '../api/promptGroupTypes';
import { confirmReveal } from '../features/useSafetySync';
import { useSafety } from '../state/safety';
import { Button } from './primitives';
import { Icon } from './slots';

export interface PreviewImageProps {
  /** 带鉴权的图像 API 路径；null 显示占位。数据集图与提示词组预览共用本组件。 */
  path: string | null | undefined;
  /** 敏感图谓词 S（组 nsfw=true，或 rating ∈ {questionable, explicit}）。 */
  sensitive: boolean;
  alt: string;
  /** 额外聚焦来源（如网格里的当前项）。组件自身还跟踪悬停与键盘聚焦。 */
  forceFocused?: boolean;
  /** 组件自身可接受键盘聚焦（缩略图网格里由网格管理焦点，传 false）。 */
  focusable?: boolean;
  /** 填满容器（大图预览适配）。 */
  fill?: boolean;
  className?: string;
}

/**
 * F27 §4/§9 的统一图像组件。严格按 imagePolicy 计算 blur/hint/can_reveal，
 * 不自行保存确认；策略版本进入 img key，策略变化时丢弃已解码图像。
 */
export function PreviewImage(p: PreviewImageProps) {
  const settings = useSafety((s) => s.settings);
  const revealed = useSafety((s) => s.revealed);
  const version = useSafety((s) => s.policyVersion);
  const [hover, setHover] = useState(false);
  const [within, setWithin] = useState(false);
  const { url, error } = useBlobUrl(p.path);
  const focused = hover || within || !!p.forceFocused;
  const d = imagePolicy(p.sensitive, focused, settings, revealed);
  const label = p.sensitive
    ? `${p.alt ? p.alt + '，' : ''}${settings.nsfw_enabled ? 'nsfw 内容' : 'nsfw 屏蔽'}`
    : p.alt;

  return (
    <span
      className={`pimg${p.fill ? ' pimg--fill' : ''}${p.className ? ' ' + p.className : ''}`}
      data-blur={d.blur ? 'true' : 'false'}
      data-sensitive={p.sensitive ? 'true' : undefined}
      tabIndex={p.focusable === false ? undefined : 0}
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : true}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setWithin(true)}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setWithin(false); }}
    >
      {url && !error
        ? <img key={version} src={url} alt="" draggable={false} />
        : <span className="pimg__ph" aria-hidden="true"><Icon name={error ? 'warn' : 'image'} /></span>}
      {p.sensitive && <span className="pimg__tag">NSFW</span>}
      {d.hint && <span className="pimg__hint">{d.hint}</span>}
      {d.can_reveal && focused && (
        <Button
          className="pimg__reveal"
          onClick={(e) => { e.stopPropagation(); void confirmReveal(); }}
          onDoubleClick={(e) => e.stopPropagation()}
        >
          显示
        </Button>
      )}
    </span>
  );
}

/** 提示词组条目预览：按 F27 §9 接口 `PreviewImage({ref, entry_id, sensitive, preview})`。 */
export function EntryPreview(p: { gref: GroupRef; entryId: string; sensitive: boolean; preview: PreviewRef | null; alt?: string }) {
  if (!p.preview) return <span className="pimg pimg--empty"><span className="pimg__ph" aria-hidden="true"><Icon name="image" /></span></span>;
  return (
    <PreviewImage
      path={previewPath(p.gref, p.entryId, 'thumb', p.preview.asset_revision)}
      sensitive={p.sensitive}
      alt={p.alt ?? '条目预览图'}
    />
  );
}
