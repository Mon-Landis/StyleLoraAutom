import { openStream } from './client';
import type { SseEvent } from './types';

/** 增量解析 SSE 文本帧；返回已完整的事件，残余留在 buffer。纯函数，便于测试。 */
export function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() ?? '';
  for (const block of parts) {
    let id = '';
    let kind = 'message';
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(':')) continue;
      if (line.startsWith('id:')) id = line.slice(3).trim();
      else if (line.startsWith('event:')) kind = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).trim());
    }
    if (!data.length) continue;
    try {
      events.push({ id, kind, data: JSON.parse(data.join('\n')) });
    } catch {
      /* 忽略损坏帧 */
    }
  }
  return { events, rest };
}

export interface SseHandle {
  close: () => void;
}
export interface SseOpts {
  projectId?: string;
  topics?: string[];
  onEvent: (e: SseEvent) => void;
  onUp: () => void;
  onDown: () => void;
  onAuthLost: () => void;
  onResync: () => void;
}

/** 基于 fetch 流的 SSE（需要 Authorization 头）；断线指数退避重连并带 Last-Event-ID。 */
export function connectSse(o: SseOpts): SseHandle {
  const ctl = new AbortController();
  let lastId = '';
  let delay = 500;
  const run = async (): Promise<void> => {
    while (!ctl.signal.aborted) {
      try {
        const sp = new URLSearchParams();
        if (o.projectId) sp.set('project_id', o.projectId);
        o.topics?.forEach((t) => sp.append('topics', t));
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (lastId) headers['Last-Event-ID'] = lastId;
        const res = await openStream(`/api/events?${sp}`, headers, ctl.signal);
        if (res.status === 401) return o.onAuthLost();
        if (!res.ok || !res.body) throw new Error(`sse:${res.status}`);
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const { events, rest } = parseSse(buf);
          buf = rest;
          for (const ev of events) {
            if (ev.id) lastId = ev.id;
            if (ev.kind === 'stream.ready') {
              delay = 500;
              o.onUp();
            } else if (ev.kind === 'stream.resync_required') {
              lastId = '';
              o.onResync();
            } else o.onEvent(ev);
          }
        }
      } catch {
        if (ctl.signal.aborted) return;
      }
      o.onDown();
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 8000);
    }
  };
  void run();
  return { close: () => ctl.abort() };
}
