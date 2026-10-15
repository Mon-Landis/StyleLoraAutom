import { useQuery } from '@tanstack/react-query';
import { getConfig } from '../api/more';
import { useApp } from '../state/app';
import { ConnDot } from '../shell/ConnDot';
import { Button, Field, Panel } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Loading } from '../ui/slots';

export function SettingsPage() {
  const { theme, density, setTheme, setDensity } = useApp();
  const q = useQuery({ queryKey: ['config'], queryFn: getConfig });
  return (
    <div className="page">
      <Panel title="外观">
        <Field label="主题">
          <div className="seg">
            <Button variant={theme === 'light' ? 'primary' : 'default'} onClick={() => setTheme('light')}>浅色</Button>
            <Button variant={theme === 'dark' ? 'primary' : 'default'} onClick={() => setTheme('dark')}>深色</Button>
          </div>
        </Field>
        <Field label="密度">
          <div className="seg">
            <Button variant={density === 'compact' ? 'primary' : 'default'} onClick={() => setDensity('compact')}>紧凑</Button>
            <Button variant={density === 'comfortable' ? 'primary' : 'default'} onClick={() => setDensity('comfortable')}>舒适</Button>
          </div>
        </Field>
        <Field label="连接状态"><ConnDot showLabel /></Field>
      </Panel>
      <Panel title="后端配置（只读）">
        {q.isLoading && <Loading />}
        {q.error && <ErrorView error={q.error} feature="配置" />}
        {q.data && <pre className="pre">{JSON.stringify(q.data, null, 2)}</pre>}
      </Panel>
    </div>
  );
}
