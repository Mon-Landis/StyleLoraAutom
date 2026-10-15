import { useEffect, useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { hooks } from './api/client';
import { boot, redeem } from './api/session';
import { useConnection } from './state/connection';
import { usePendingWrites } from './state/writes';
import { Shell } from './shell/Shell';
import { ProjectInfoPage } from './pages/ProjectInfoPage';
import { Workbench } from './pages/Workbench';
import { SourcesPage } from './pages/SourcesPage';
import { PromptsPage } from './pages/PromptsPage';
import { SettingsPage } from './pages/SettingsPage';
import { Banner, Field } from './ui/primitives';
import { Button } from './ui/primitives';

export function App() {
  const dispatch = useConnection((s) => s.dispatch);
  const conn = useConnection((s) => s.state);
  const [phase, setPhase] = useState<'boot' | 'ready' | 'need_code'>('boot');
  const [code, setCode] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    hooks.onUnauthorized = () => dispatch('unauthorized');
    hooks.onNetwork = (ok) => dispatch(ok ? 'rest_ok' : 'rest_fail');
    hooks.onWrite = (d) => usePendingWrites.getState().bump(d);
    void boot().then((r) => {
      if (r === 'ok') {
        dispatch('rest_ok');
        setPhase('ready');
      } else setPhase('need_code');
    });
  }, [dispatch]);

  const submit = async () => {
    try {
      await redeem(code.trim());
      dispatch('reset');
      dispatch('rest_ok');
      setPhase('ready');
    } catch {
      setErr('引导码无效或已过期（一次性，60 秒有效）');
    }
  };

  if (phase === 'boot') return <div className="boot">…</div>;
  if (phase === 'need_code' || conn === 'reauth')
    return (
      <div className="boot">
        <div className="boot__card">
          <h1>StyleLora</h1>
          <p>请从后端启动输出的地址打开，或粘贴一次性引导码。</p>
          <Field label="bootstrap code">
            <input value={code} onChange={(e) => setCode(e.target.value)} />
          </Field>
          {err && <Banner tone="danger">{err}</Banner>}
          <Button variant="primary" onClick={() => void submit()}>连接</Button>
        </div>
      </div>
    );
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/workbench" replace />} />
        <Route path="/workbench" element={<Workbench />} />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="/prompts" element={<PromptsPage />} />
        <Route path="/project" element={<ProjectInfoPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/workbench" replace />} />
      </Routes>
    </Shell>
  );
}
