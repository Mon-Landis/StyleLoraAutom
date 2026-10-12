import { useEffect, useState } from 'react';
import { useBlobUrl } from '../lib/useBlobUrl';
import { useApp, type Stage } from '../state/app';
import type { ImageItem } from '../api/types';
import { Button } from '../ui/primitives';
import { Icon, Loading } from '../ui/slots';

const STAGES: Array<[Stage, string]> = [['checker', '棋盘'], ['white', '白'], ['gray', '灰'], ['black', '黑'], ['dark', '深色']];

export function StageSwitch() {
  const { stage, setStage } = useApp();
  return (
    <div className="seg" role="group" aria-label="预览背景">
      {STAGES.map(([k, label]) => (
        <Button key={k} variant={stage === k ? 'primary' : 'default'} aria-pressed={stage === k} onClick={() => setStage(k)}>{label}</Button>
      ))}
    </div>
  );
}

/** 大图预览：背景由 data-stage 驱动（样式在 app.css，走令牌）。 */
export function Preview({ item, onPrev, onNext }: { item: ImageItem; onPrev: () => void; onNext: () => void }) {
  const stage = useApp((s) => s.stage);
  const { url, error } = useBlobUrl(item.original_url);
  const [fit, setFit] = useState(true);
  useEffect(() => setFit(true), [item.image_id]);
  return (
    <div className="preview" data-stage={stage}>
      <div className={`preview__canvas${fit ? ' is-fit' : ''}`} onDoubleClick={() => setFit((f) => !f)}>
        {!url && !error && <Loading />}
        {error && <span className="preview__err"><Icon name="warn" /> 无法加载原图</span>}
        {url && <img src={url} alt={item.filename ?? String(item.image_id)} draggable={false} />}
      </div>
      <div className="preview__nav">
        <Button variant="ghost" aria-label="上一张" onClick={onPrev}><Icon name="prev" /></Button>
        <Button variant="ghost" aria-label="下一张" onClick={onNext}><Icon name="next" /></Button>
      </div>
    </div>
  );
}
