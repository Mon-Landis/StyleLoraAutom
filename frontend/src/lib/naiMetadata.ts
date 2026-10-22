import { ApiError } from '../api/errors';
import type { NaiMetadataFileStatus } from '../api/tools';

/** 逐文件 issue/reason 代码 -> 中文（NAI 元数据打印工具专用，覆盖范围较小，不复用全局 promptErrors 表）。 */
const ISSUE_TEXT: Record<string, string> = {
  'metadata:empty': '未找到正面提示词',
  'metadata:invalid': '图片无效或不是 PNG',
  'metadata:limit': '超出大小限制',
  'storage:io_error': '写入失败',
  'output:conflict': '输出 txt 与其它图片冲突（同名不同大小写/扩展名），未写入',
  'image:unsupported': '不是 PNG 图片，未处理',
};

/** 未收录的 issue/reason 码（如转换告警码 W_INPUT_LIMIT）：显示「处理失败（code）」而不是丢弃原始信息。 */
export const describeNaiIssue = (code: string): string => ISSUE_TEXT[code] ?? `处理失败（${code}）`;

const STATUS_LABEL: Record<NaiMetadataFileStatus, string> = {
  written: '写入',
  skipped: '跳过',
  failed: '失败',
};

/** 写入但覆盖了已存在的同名 txt 时，用更明确的「覆盖写入」文案。 */
export function naiStatusLabel(status: NaiMetadataFileStatus, overwritten: boolean): string {
  if (status === 'written' && overwritten) return '覆盖写入';
  return STATUS_LABEL[status];
}

/** 顶层请求错误 -> 中文；未收录的回退到通用文案。 */
export function describeNaiMetadataError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === 'picker:unavailable') return '当前环境无法打开系统文件选择器';
    if (e.code === 'picker:busy') return '已有文件选择或处理正在进行，请稍后再试';
    if (e.code === 'import:limit') return '一次最多处理 1000 张图片';
    if (e.code === 'input:invalid' || e.code === 'path:invalid') return '请求参数无效，请重试';
    return `${e.code}: ${e.message}`;
  }
  return e instanceof Error ? e.message : String(e);
}
