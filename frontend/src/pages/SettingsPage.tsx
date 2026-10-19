import { useQuery } from '@tanstack/react-query';
import { getConfig } from '../api/more';
import { useApp } from '../state/app';
import { useSafety } from '../state/safety';
import { useSafetyMutation } from '../features/useSafetySync';
import { DISPLAY_MODES, DISPLAY_MODE_LABEL } from '../lib/nsfw';
import { ConnDot } from '../shell/ConnDot';
import { Button, Field, Panel } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Loading } from '../ui/slots';
import { EndpointParallelSettings } from '../features/EndpointParallelSettings';

export function SettingsPage() {
  const { theme, density, setTheme, setDensity } = useApp();
  const q = useQuery({ queryKey: ['config'], queryFn: getConfig });
  const safety = useSafety((s) => s.settings);
  const loaded = useSafety((s) => s.loaded);
  const mut = useSafetyMutation();
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
      <Panel title="内容安全（仅影响前端显示）">
        <Field label="NSFW 内容">
          <label className="check">
            <input
              type="checkbox"
              checked={safety.nsfw_enabled}
              disabled={!loaded || mut.isPending}
              onChange={(e) => mut.mutate({ nsfw_enabled: e.target.checked })}
            />
            显示 NSFW 词典与图像（默认关闭）
          </label>
        </Field>
        <Field label="图像表现方式">
          <div className="seg" role="radiogroup" aria-label="图像表现方式">
            {DISPLAY_MODES.map((m) => (
              <Button
                key={m}
                role="radio"
                aria-checked={safety.nsfw_display_mode === m}
                variant={safety.nsfw_display_mode === m ? 'primary' : 'default'}
                disabled={!loaded || mut.isPending}
                onClick={() => mut.mutate({ nsfw_display_mode: m })}
              >
                {DISPLAY_MODE_LABEL[m]}
              </Button>
            ))}
          </div>
        </Field>
        {mut.error != null && <ErrorView error={mut.error} feature="内容安全设置" />}
      </Panel>
      {/* F31 §7.3：「新提示词组默认自动补齐」偏好已隐藏（后端字段保留兼容）：条目只有 nl，无可补齐的基础缺口。 */}
      <EndpointParallelSettings />
      <Panel title="后端配置（只读）">
        {q.isLoading && <Loading />}
        {q.error && <ErrorView error={q.error} feature="配置" />}
        {q.data && <pre className="pre">{JSON.stringify(q.data, null, 2)}</pre>}
      </Panel>
    </div>
  );
}
