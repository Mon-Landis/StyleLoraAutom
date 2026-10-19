import { useEffect } from 'react';
import { readTokenMs } from '../tokens';
import { useNotices, type Notice } from '../state/notices';
import { Banner, Button } from '../ui/primitives';
import { Icon } from '../ui/slots';

const FALLBACK_TTL_MS = 8000;

function NoticeItem({ n }: { n: Notice }) {
  const dismiss = useNotices((s) => s.dismiss);
  useEffect(() => {
    // danger 不自动消失，需要用户确认；其余按 --toast-ttl。
    if (n.tone === 'danger') return;
    const t = setTimeout(() => dismiss(n.id), readTokenMs('--toast-ttl', FALLBACK_TTL_MS));
    return () => clearTimeout(t);
  }, [n.id, n.tone, dismiss]);
  return (
    <div className="notice">
      <Banner tone={n.tone}>
        <span className="notice__text">{n.text}</span>
        <Button variant="ghost" aria-label="关闭通知" onClick={() => dismiss(n.id)}><Icon name="close" /></Button>
      </Banner>
    </div>
  );
}

/** 全局通知宿主：页面卸载后仍可见的异步结果（如离开提示词页时的草稿保存失败）。 */
export function NoticeHost() {
  const items = useNotices((s) => s.items);
  if (!items.length) return null;
  return (
    <div className="notice-host" aria-label="通知">
      {items.map((n) => <NoticeItem key={n.id} n={n} />)}
    </div>
  );
}
