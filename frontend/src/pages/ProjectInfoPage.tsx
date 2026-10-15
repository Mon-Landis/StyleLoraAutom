import { useApp } from '../state/app';
import { modeLabel } from '../lib/projectMode';
import { Panel } from '../ui/primitives';
import { Empty } from '../ui/slots';

/** 项目信息占位页：侧边栏「项目」按钮的落点。TODO(backend): 打开项目目录 / 完整元数据接口。 */
export function ProjectInfoPage() {
  const { projectId, projectName, projectMode } = useApp();
  if (!projectId) return <div className="page"><Empty title="尚未打开项目" hint="使用顶栏的「打开项目」选择项目目录。" /></div>;
  return (
    <div className="page">
      <Panel title="项目信息">
        <dl className="kv">
          <dt>名称</dt><dd>{projectName}</dd>
          <dt>ID</dt><dd>{projectId}</dd>
          <dt>类型</dt><dd>{modeLabel(projectMode)}</dd>
        </dl>
        <Empty title="打开项目目录" hint="后端暂未提供该接口（占位）。" />
      </Panel>
    </div>
  );
}
