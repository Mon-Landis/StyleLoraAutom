import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getAnnotation, putManual, type AnnotationTag, type AnnotationView, type LayerRow } from '../api/endpoints';
import { ApiError } from '../api/errors';
import { LAYERS } from '../api/types';
import { Badge, Banner, Button } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Loading } from '../ui/slots';

const LAYER_LABEL: Record<string, string> = { original: '原始', wd: 'WD', llm: 'LLM', manual: '手动', rule: '规则' };

function tagsText(c: LayerRow['content'] | undefined): string {
  if (!c) return '';
  if (typeof c === 'string') return c;
  return c.map((t) => t.tag).join(', ');
}

function Layers({ view }: { view: AnnotationView }) {
  return (
    <div className="layers">
      {LAYERS.map((l) => {
        const row = view.layers[l];
        return (
          <div key={l} className="layer">
            <Badge tone={row ? 'accent' : 'neutral'}>{LAYER_LABEL[l]}</Badge>
            <span className="layer__text">{row ? tagsText(row.content) : '—'}</span>
          </div>
        );
      })}
    </div>
  );
}

function parseTags(text: string): AnnotationTag[] {
  return text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean).map((tag) => ({ tag }));
}

export function AnnotationPanel({ pid, imageId }: { pid: string; imageId: number }) {
  const qc = useQueryClient();
  const key = ['annotation', pid, imageId];
  const q = useQuery({ queryKey: key, queryFn: () => getAnnotation(pid, imageId) });
  const [text, setText] = useState('');
  const [nl, setNl] = useState('');
  const [conflict, setConflict] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    if (q.data) {
      setText(q.data.tags.map((t) => t.tag).join(', '));
      setNl(q.data.natural_language ?? '');
      setConflict(false);
      setErr(null);
    }
  }, [q.data]);

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorView error={q.error} feature="标注" />;
  const view = q.data!;

  const save = async () => {
    setErr(null);
    try {
      const next = await putManual(pid, imageId, view, parseTags(text), nl.trim() ? nl : null);
      qc.setQueryData(key, next);
      setConflict(false);
    } catch (e) {
      if (e instanceof ApiError && e.conflict) setConflict(true);
      else setErr(e);
    }
  };

  return (
    <div className="anno">
      <Layers view={view} />
      <div className="anno__merged">
        <div className="field__label">合并结果</div>
        <div className="anno__tags">{view.tags.map((t) => t.tag).join(', ') || '—'}</div>
        {view.natural_language && <div className="anno__nl">{view.natural_language}</div>}
      </div>
      <div className="field__label">手动编辑</div>
      <textarea className="anno__edit" rows={3} value={text} onChange={(e) => setText(e.target.value)} aria-label="手动标签" />
      <textarea className="anno__edit" rows={2} value={nl} onChange={(e) => setNl(e.target.value)} aria-label="手动自然语言" />
      {conflict && (
        <Banner tone="warning">
          标注已被其他操作修改（版本冲突）。
          <Button variant="ghost" onClick={() => void qc.invalidateQueries({ queryKey: key })}>重新加载最新</Button>
        </Banner>
      )}
      {err != null && <ErrorView error={err} feature="保存手动标注" />}
      <Button variant="primary" onClick={() => void save()}>保存手动层</Button>
    </div>
  );
}
