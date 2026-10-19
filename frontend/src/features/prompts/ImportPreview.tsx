import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchImportPreviewBlob } from '../../api/promptGroups';
import type { ImportFileReport, ImportIssue, ImportPreviewV5 } from '../../api/promptGroupTypes';
import { describeErrorCode, type ImportFileFailure } from '../../lib/promptErrors';
import { PreviewImage } from '../../ui/PreviewImage';
import { Badge, Banner, Button } from '../../ui/primitives';

/** 每次渲染的逐文件行数：缩略图只为可见行拉取，避免一次请求上千张。 */
const PAGE = 50;
const THUMB_CONCURRENCY = 4;
const ISSUE_CAP = 20;
const FAILURE_CAP = 50;

export interface ImportErrorView {
  text: string;
  files: ImportFileFailure[];
}

/** 问题文案：已收录的错误码用中文，否则退回后端 message / code。 */
export function issueText(i: ImportIssue | string): string {
  if (typeof i === 'string') return i;
  const t = i.code ? describeErrorCode(i.code) : '';
  return t && t !== i.code ? t : i.message ?? i.code ?? '未知问题';
}

/**
 * 预览缩略图 -> Blob URL。
 * previewId 变化/卸载：abort 在途请求并 revoke 全部 URL；迟到的响应在创建 URL 之前被丢弃。
 */
function useImportThumbs(previewId: string, ids: readonly string[]): Record<string, string> {
  const [state, setState] = useState<{ id: string; urls: Record<string, string> }>({ id: previewId, urls: {} });
  const owned = useRef(new Map<string, string>());
  const pending = useRef(new Set<string>());
  const ctl = useRef<AbortController | null>(null);
  const key = ids.join('|');

  useEffect(() => {
    const c = new AbortController();
    ctl.current = c;
    const map = owned.current;
    const pend = pending.current;
    return () => {
      c.abort();
      map.forEach((u) => URL.revokeObjectURL(u));
      map.clear();
      pend.clear();
    };
  }, [previewId]);

  useEffect(() => {
    const c = ctl.current;
    if (!c || c.signal.aborted || !key) return;
    const todo = key.split('|').filter((id) => !owned.current.has(id) && !pending.current.has(id));
    todo.forEach((id) => pending.current.add(id));
    const worker = async () => {
      for (let id = todo.shift(); id !== undefined; id = todo.shift()) {
        try {
          const blob = await fetchImportPreviewBlob(previewId, id, 'thumb', c.signal);
          if (c.signal.aborted) return;
          const url = URL.createObjectURL(blob);
          owned.current.set(id, url);
          setState((s) => ({ id: previewId, urls: { ...(s.id === previewId ? s.urls : {}), [id]: url } }));
        } catch {
          if (c.signal.aborted) return;
          // 取图失败：保留占位，不重试。
        }
      }
    };
    for (let n = 0; n < Math.min(THUMB_CONCURRENCY, todo.length); n++) void worker();
  }, [previewId, key]);

  return state.id === previewId ? state.urls : {};
}

/** 预览/确认失败：友好文案 + 逐文件原因（422 import:empty 的 details.files）。 */
export function ImportErrorBanner({ error }: { error: ImportErrorView }) {
  const rows = error.files.slice(0, FAILURE_CAP);
  return (
    <Banner tone="danger">
      <div>{error.text}</div>
      {rows.length > 0 && (
        <ul className="pg-imp-fail" aria-label="逐文件原因">
          {rows.map((f, i) => (
            <li key={i}>
              {f.file_index != null ? `#${f.file_index} ` : ''}{f.name ?? ''}：{issueText({ code: f.code, message: f.message })}
            </li>
          ))}
          {error.files.length > rows.length && <li>…另有 {error.files.length - rows.length} 条</li>}
        </ul>
      )}
    </Banner>
  );
}

function FileRow({ report, nl, url, sensitive }: { report: ImportFileReport; nl: string; url: string | null; sensitive: boolean }) {
  const skipped = report.status === 'skipped';
  return (
    <li className="pg-imp-file" data-status={report.status}>
      <span className="pg-imp-thumb">
        {/* 保持可聚焦：纯键盘用户聚焦缩略图后才会出现「显示」按钮（NSFW click_reveal）。 */}
        <PreviewImage path={null} url={url} sensitive={sensitive} alt={`${report.name} 预览`} />
      </span>
      <div className="pg-imp-file__main">
        <div className="pg-imp-file__head">
          <span className="pg-imp-file__name" title={report.name}>#{report.file_index} {report.name}</span>
          <Badge tone={skipped ? 'warning' : 'success'}>{skipped ? '跳过' : '有效'}</Badge>
        </div>
        <div className="pg-muted">提取来源：{report.source ?? '无'}</div>
        {!skipped && nl && <div className="pg-imp-file__nl">{nl}</div>}
        {skipped && report.issues.length === 0 && <div className="pg-muted">已过滤</div>}
        {report.issues.length > 0 && (
          <ul className="pg-imp-fail">
            {report.issues.map((it, i) => <li key={i} className="pg-muted">{issueText(it)}</li>)}
          </ul>
        )}
      </div>
    </li>
  );
}

/** 导入预览：汇总、标准化警告、逐文件报告（图像导入）或逐行问题（JSON/TXT/ZIP）。 */
export function ImportPreview({ preview, sensitive }: { preview: ImportPreviewV5; sensitive: boolean }) {
  const files = preview.files;
  const [shown, setShown] = useState(PAGE);
  const visible = (files ?? []).slice(0, shown);
  const nlById = useMemo(() => new Map((preview.entries ?? []).map((e) => [e.entry_id, e.nl ?? ''])), [preview.entries]);
  const thumbs = useImportThumbs(preview.preview_id, visible.flatMap((f) => (f.entry_id ? [f.entry_id] : [])));

  const validFiles = files ? files.filter((f) => f.status === 'valid').length : null;
  const valid = preview.valid_count ?? validFiles ?? 0;
  const skipped = preview.skipped_count ?? (files ? files.length - (validFiles ?? 0) : (preview.issues ?? []).length);
  const entries = preview.entries ?? [];
  const warnings = preview.warnings ?? [];
  const issues = preview.issues ?? [];

  return (
    <div className="pg-import-preview">
      <div role="status" aria-live="polite">
        有效 <b>{valid}</b> 条，跳过 <b>{skipped}</b> 条
        {preview.normalization_version && <span className="pg-muted">（标准化 {preview.normalization_version}）</span>}
      </div>
      {preview.metadata?.name && (
        <div className="pg-muted">
          词典：{preview.metadata.name}{preview.metadata.scope ? ` · ${preview.metadata.scope === 'project' ? '项目' : '全局'}` : ''}{preview.metadata.nsfw ? ' · nsfw' : ''}
        </div>
      )}
      {preview.blocked && <Banner tone="warning">目标为项目级但未打开项目，不能提交。</Banner>}
      {warnings.length > 0 && (
        <details>
          <summary>标准化警告（{warnings.length}）</summary>
          <ul className="pg-imp-fail">
            {warnings.slice(0, ISSUE_CAP).map((w, i) => <li key={i} className="pg-muted">{issueText(w)}</li>)}
            {warnings.length > ISSUE_CAP && <li className="pg-muted">…另有 {warnings.length - ISSUE_CAP} 条</li>}
          </ul>
        </details>
      )}
      {files ? (
        <>
          <ul className="pg-imp-list" aria-label="逐文件报告">
            {visible.map((f) => (
              <FileRow
                key={f.file_index}
                report={f}
                nl={f.entry_id ? nlById.get(f.entry_id) ?? '' : ''}
                url={f.entry_id ? thumbs[f.entry_id] ?? null : null}
                sensitive={sensitive}
              />
            ))}
          </ul>
          {files.length > shown && (
            <Button onClick={() => setShown((n) => n + PAGE)}>显示更多（还有 {files.length - shown} 个）</Button>
          )}
        </>
      ) : (
        <>
          {entries.length > 0 && (
            <>
              <ul className="pg-imp-list" aria-label="转换后的提示词">
                {entries.slice(0, shown).map((e, i) => (
                  <li key={e.entry_id} className="pg-imp-file" data-status="valid">
                    <span className="pg-muted">#{i + 1}</span>
                    <div className="pg-imp-file__main">
                      <div className="pg-imp-file__nl">{e.nl ?? ''}</div>
                    </div>
                  </li>
                ))}
              </ul>
              {entries.length > shown && (
                <Button onClick={() => setShown((n) => n + PAGE)}>显示更多（还有 {entries.length - shown} 条）</Button>
              )}
            </>
          )}
          {issues.slice(0, ISSUE_CAP).map((it, i) => (
            <div key={i} className="pg-muted">第 {it.row ?? '?'} 行{it.field ? ` · ${it.field}` : ''}：{issueText(it)}</div>
          ))}
        </>
      )}
    </div>
  );
}
