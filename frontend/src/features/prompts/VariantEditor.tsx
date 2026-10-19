import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import type { Entry, EntryPatch } from '../../api/promptGroupTypes';
import { runGroupWrite } from '../../lib/groupWriteQueue';
import { describePgError } from '../../lib/promptErrors';
import { checkVariantKey, isVariantGenerated, variantsOf } from '../../lib/promptVariants';
import { draftId, useVariantDrafts } from '../../state/variantDrafts';
import { Banner, Button } from '../../ui/primitives';
import { Icon } from '../../ui/slots';
import { GeneratedField } from './GeneratedField';

/**
 * 变体编辑：左侧窄 key 列表（独立滚动）+ 下方加号，右侧多行编辑器。
 * 草稿放在 useVariantDrafts：草稿存在即「用户真实输入过」，用于本地立即清除生成标记；
 * 保存失败草稿保留。focus / blur 本身不改标记，保存按钮或 Ctrl+Enter 才提交。
 */
export function VariantEditor({ entry, onPatch, draftScope = '' }: {
  entry: Entry;
  onPatch: (p: EntryPatch) => Promise<void>;
  /** 草稿命名空间（通常是 refKey(gref)），避免不同组同 entry_id 串草稿。 */
  draftScope?: string;
}) {
  const uid = useId();
  const variants = variantsOf(entry);
  const keys = Object.keys(variants);
  const [sel, setSel] = useState<string | null>(null);
  const active = sel !== null && keys.includes(sel) ? sel : (keys[0] ?? null);
  const drafts = useVariantDrafts((s) => s.drafts);
  const setDraft = useVariantDrafts((s) => s.set);
  const [adding, setAdding] = useState(false);
  const [newKey, setNewKey] = useState('');
  const [keyError, setKeyError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<{ key: string; text: string } | null>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const keyInputRef = useRef<HTMLInputElement>(null);
  const addBtnRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const focusAfterAdd = useRef<string | null>(null);

  // 新增 key 成功且列表更新后：选中并聚焦编辑器。
  useEffect(() => {
    if (active !== null && focusAfterAdd.current === active) {
      focusAfterAdd.current = null;
      editorRef.current?.focus();
    }
  }, [active, keys.length]);
  useEffect(() => { if (adding) keyInputRef.current?.focus(); }, [adding]);

  const idOf = (key: string) => draftId(draftScope, entry.entry_id, key);
  const draftOf = (key: string) => drafts[idOf(key)];
  const valueOf = (key: string) => draftOf(key) ?? variants[key] ?? '';

  const closeAdd = () => {
    setAdding(false);
    setNewKey('');
    setKeyError(null);
    addBtnRef.current?.focus();
  };

  const submitKey = async () => {
    const c = checkVariantKey(newKey, keys);
    if (!c.ok) { setKeyError(c.error); return; }
    setBusy(true);
    setKeyError(null);
    focusAfterAdd.current = c.key;
    try {
      await runGroupWrite(draftScope, () => onPatch({ variants: { [c.key]: '' } }));
      setSel(c.key);
      setAdding(false);
      setNewKey('');
    } catch (e) {
      focusAfterAdd.current = null;
      setKeyError(describePgError(e).text);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (active === null || busy) return;
    const id = idOf(active);
    if (useVariantDrafts.getState().drafts[id] === undefined) return;
    setBusy(true);
    setSaveError(null);
    try {
      // 与离开 flush 共用同组写入队列；草稿在轮到自己时才读取：
      // 若排队期间已被 flush 保存/清除则跳过，不拿点击时的旧快照覆盖更新的文本。
      await runGroupWrite(draftScope, async () => {
        const draft = useVariantDrafts.getState().drafts[id];
        if (draft === undefined) return;
        await onPatch({ variants: { [active]: draft } });
        // 保存期间继续输入的新草稿不丢；其编辑起点推进到刚保存的值。
        const st = useVariantDrafts.getState();
        if (st.drafts[id] === draft) st.clear(id);
        else if (st.drafts[id] !== undefined) st.rebase(id, draft);
      });
    } catch (e) {
      setSaveError({ key: active, text: describePgError(e).text });
    } finally {
      setBusy(false);
    }
  };

  const onKeyNav = (e: KeyboardEvent, i: number) => {
    let next = i;
    if (e.key === 'ArrowDown') next = Math.min(keys.length - 1, i + 1);
    else if (e.key === 'ArrowUp') next = Math.max(0, i - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = keys.length - 1;
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel(keys[i]); editorRef.current?.focus(); return; }
    else return;
    e.preventDefault();
    setSel(keys[next]);
    listRef.current?.querySelectorAll<HTMLElement>('[data-variant-key]')[next]?.focus();
  };

  const textId = `${uid}-text`;
  const errId = `${uid}-err`;
  const planId = `${uid}-plan`;
  const keyErrId = `${uid}-keyerr`;
  const activeDraft = active !== null ? draftOf(active) : undefined;
  const dirty = activeDraft !== undefined;
  const shownGenerated = active !== null && isVariantGenerated(entry, active) && !dirty;
  const error = saveError && saveError.key === active ? saveError.text : null;

  return (
    <div className="vedit">
      <div className="vedit__side">
        <div ref={listRef} className="vedit__keys" role="listbox" aria-label="变体 key" aria-orientation="vertical">
          {keys.length === 0 && <p className="pg-muted vedit__none">还没有变体</p>}
          {keys.map((k, i) => {
            const selected = k === active;
            const gen = isVariantGenerated(entry, k) && draftOf(k) === undefined;
            const unsaved = draftOf(k) !== undefined;
            return (
              <div
                key={k}
                role="option"
                tabIndex={selected ? 0 : -1}
                aria-selected={selected}
                data-variant-key={k}
                className={`vedit__key${selected ? ' is-selected' : ''}`}
                onClick={() => setSel(k)}
                onKeyDown={(e) => onKeyNav(e, i)}
              >
                <span className="vedit__keyname">{k}</span>
                {gen && <><Icon name="ai-generated" /><span className="visually-hidden">（AI 生成）</span></>}
                {unsaved && <><Icon name="unsaved" /><span className="visually-hidden">（未保存）</span></>}
              </div>
            );
          })}
        </div>
        {adding ? (
          <div className="vedit__add" role="group" aria-label="新增变体">
            <input
              ref={keyInputRef}
              value={newKey}
              maxLength={128}
              aria-label="新变体 key"
              aria-invalid={keyError ? true : undefined}
              aria-describedby={keyError ? keyErrId : undefined}
              disabled={busy}
              onChange={(e) => { setNewKey(e.target.value); setKeyError(null); }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); void submitKey(); }
                else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeAdd(); }
              }}
            />
            {keyError && <div id={keyErrId} className="vedit__err" role="alert">{keyError}</div>}
            <div className="vedit__addbtns">
              <Button variant="primary" disabled={busy || !newKey.trim()} onClick={() => void submitKey()}>添加</Button>
              <Button disabled={busy} onClick={closeAdd}>取消</Button>
            </div>
          </div>
        ) : (
          <Button ref={addBtnRef} className="vedit__plus" aria-label="添加变体" onClick={() => setAdding(true)}>
            <Icon name="plus" />{keys.length === 0 && <span>添加变体</span>}
          </Button>
        )}
      </div>

      <div className="vedit__main">
        {active === null ? (
          <p className="pg-muted">该条目还没有变体。点击左侧「添加变体」，输入 key 后在此编写多行内容。</p>
        ) : (
          <>
            <GeneratedField generated={shownGenerated} pending={false}>
              {({ describedBy }) => (
                <>
                  <label className="vedit__label" htmlFor={textId}>变体内容 · {active}</label>
                  <textarea
                    id={textId}
                    ref={editorRef}
                    className="vedit__text"
                    value={valueOf(active)}
                    aria-describedby={[describedBy, error ? errId : null].filter(Boolean).join(' ') || undefined}
                    aria-invalid={error ? true : undefined}
                    onChange={(e) => setDraft(idOf(active), e.target.value, variants[active] ?? '')}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void save(); }
                    }}
                  />
                </>
              )}
            </GeneratedField>
            {error && <div id={errId}><Banner tone="danger">{error}（草稿已保留）</Banner></div>}
            <div className="vedit__bar">
              {dirty && <span className="vedit__dirty"><Icon name="unsaved" /> 未保存</span>}
              <Button variant="primary" disabled={!dirty || busy} onClick={() => void save()}>{busy ? '保存中…' : '保存'}</Button>
              <Button disabled title="规划中" aria-describedby={planId}>请求生成</Button>
              <span id={planId} className="visually-hidden">规划中：请求生成功能本期未开放</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
