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
