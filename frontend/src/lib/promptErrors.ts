import { ApiError } from '../api/errors';

export interface PgErrorInfo {
  text: string;
  /** 412/428/409：应重新读取后再试，草稿保留。 */
  conflict: boolean;
  /** 404：移除失效选择。 */
  notFound: boolean;
}

const TEXT: Record<string, string> = {
  'dictionary:revision_conflict': '词典已被修改，请重新读取后再试（草稿已保留）',
  'revision:required': '缺少版本信息，请重新读取后再试',
  'settings:revision_conflict': '设置已被修改，请重新读取后再试',
  'dictionary:not_found': '词典不存在，已移除选择',
  'entry:not_found': '条目不存在，请刷新列表',
  'project:closed': '项目已关闭，无法写入项目级词典',
  'group:id_conflict': '词典 ID 已存在，请更换或使用新 ID',
  'pagination:stale': '列表已变化，请重新读取',
  'pagination:invalid': '分页游标无效，请重新读取',
  'image:invalid': '图像无效（仅支持静态 PNG / JPEG / WebP）',
  'image:too_large': '图像过大（上限 10 MiB）',
  'schema:invalid': '内容校验失败，请检查输入',
  'import:empty': '没有有效条目可导入',
  'preview:expired': '导入预览已过期，请重新预览',
  'completion:unavailable': 'LLM 补全暂不可用',
  'completion:stale': '条目已变化，补全结果需复核',
  'storage:io_error': '存储失败，旧数据未改动，请重试',
};

export function describePgError(e: unknown): PgErrorInfo {
  if (e instanceof ApiError) {
    const code = e.code;
    return {
      text: TEXT[code] ?? `${code}: ${e.message}`,
      conflict: e.status === 412 || e.status === 428 || code === 'pagination:stale',
      notFound: e.status === 404 && (code === 'dictionary:not_found' || code === 'http:404'),
    };
  }
  return { text: e instanceof Error ? e.message : String(e), conflict: false, notFound: false };
}

/** 确定性失败（服务端已给出结果）后应更换幂等键；网络/未知结果保留原键。 */
export const isDefinitive = (e: unknown): boolean => e instanceof ApiError;
