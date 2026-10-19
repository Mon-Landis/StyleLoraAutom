import { useState } from 'react';
import { enqueueGeneration } from '../../api/promptGroups';
import type { GroupRef } from '../../api/promptGroupTypes';
import { Dialog } from '../../ui/Dialog';
import { Banner, Button, Field } from '../../ui/primitives';
import { useSubmit } from './useSubmit';

/** 与后端 runtime/prompt_generation.py BRIEF_LIMIT 一致。 */
export const BRIEF_LIMIT = 4000;

export function GenerateDialog({ gref, groupName, onClose, onSubmitted }: {
  gref: GroupRef; groupName: string; onClose: () => void; onSubmitted: (taskId: string) => void;
}) {
  const [brief, setBrief] = useState('');
  const [count, setCount] = useState('1');
  const valid = !!brief.trim() && brief.length <= BRIEF_LIMIT && Number.isInteger(Number(count)) && Number(count) >= 1 && Number(count) <= 200;
  const s = useSubmit(async (key) => {
    const task = await enqueueGeneration(gref, brief.trim(), Number(count), key);
    onSubmitted(task.task_id);
    onClose();
  });
  return <Dialog title={`生成 · ${groupName}`} onClose={onClose}>
    <form className="pg-form" onSubmit={(e) => { e.preventDefault(); if (valid) void s.submit(); }}>
      <Field label="主题"><textarea aria-label="主题" required maxLength={BRIEF_LIMIT} value={brief} onChange={(e) => setBrief(e.target.value)} aria-describedby="generation-brief-count" /></Field>
      <p id="generation-brief-count" aria-live="polite">{brief.length}/{BRIEF_LIMIT}</p>
      <Field label="条数"><input aria-label="条数" type="number" required min={1} max={200} step={1} value={count} onChange={(e) => setCount(e.target.value)} aria-describedby="generation-count-hint" /></Field>
      <p id="generation-count-hint">填写主题和 1–200 的整数条数，生成结果将追加到当前组。</p>
      {s.error && <div role="alert"><Banner tone="danger">{s.error}</Banner></div>}
      <Button type="submit" variant="primary" disabled={!valid || s.busy}>{s.busy ? '提交中…' : '生成'}</Button>
    </form>
  </Dialog>;
}
