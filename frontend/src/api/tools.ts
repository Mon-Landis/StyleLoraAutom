import { post } from './client';

/** 工具箱接口。提示词转换：纯计算，不落盘，不应触发顶栏「保存中」。 */
export type PromptConvertDirection = 'nai_to_anima' | 'anima_to_nai';

export interface PromptConvertWarning {
  code: string;
  start: number;
  end: number;
}

export interface PromptConvertResult {
  text: string;
  warnings: PromptConvertWarning[];
}

export const convertPrompt = (direction: PromptConvertDirection, text: string, signal?: AbortSignal) =>
  post<PromptConvertResult>('/api/tools/prompt-convert', { direction, text }, { signal, silent: true }).then((r) => r.data);

/** NAI 元数据打印：由后端打开系统选择器（files/folder），同步处理并在源目录写出同名 txt。 */
export type NaiMetadataSource = 'files' | 'folder';
export type NaiMetadataDropMode = 'none' | 'head' | 'global';

export interface NaiMetadataIssue {
  code: string;
  severity: string;
  message: string;
}

export type NaiMetadataFileStatus = 'written' | 'skipped' | 'failed';

export interface NaiMetadataFile {
  name: string;
  path: string;
  txt: string | null;
  status: NaiMetadataFileStatus;
  source: string | null;
  overwritten: boolean;
  chars: number;
  reason?: string;
  issues: NaiMetadataIssue[];
  warnings: string[];
}

export interface NaiMetadataResult {
  canceled: boolean;
  root: string | null;
  summary: { total: number; written: number; skipped: number; failed: number };
  files: NaiMetadataFile[];
}

export interface NaiMetadataOptions {
  source: NaiMetadataSource;
  dropMode: NaiMetadataDropMode;
  toAnima: boolean;
}

/**
 * 后端会弹出系统文件选择器（files/folder）并同步处理最多 1000 张图片，耗时可能较长。
 * 不传 signal：依赖浏览器默认（无超时），避免用户还在对话框里选择时请求被中止。
 */
export const runNaiMetadata = ({ source, dropMode, toAnima }: NaiMetadataOptions) =>
  post<NaiMetadataResult>('/api/tools/nai-metadata/run', {
    source,
    drop_mode: dropMode,
    to_anima: toAnima,
  }).then((r) => r.data);
