import type { PromptConvertDirection, PromptConvertResult } from '../api/tools';

/** 提示词转换：NAI（上）⇄ Anima（下）。只由用户正在编辑的一侧驱动，另一侧只被结果写入。 */
export type ConvertSide = 'nai' | 'anima';

export const DEBOUNCE_MS = 300;

export const directionFrom = (side: ConvertSide): PromptConvertDirection =>
  side === 'nai' ? 'nai_to_anima' : 'anima_to_nai';
export const otherSide = (side: ConvertSide): ConvertSide => (side === 'nai' ? 'anima' : 'nai');

const WARNING_TEXT: Record<string, string> = {
  W_WEIGHT_CLAMPED: '权重已限制变化（>3.2 截断）',
};
/** 未知 code 原样显示。 */
export const warningText = (code: string): string => WARNING_TEXT[code] ?? code;

export type ConvertFn = (direction: PromptConvertDirection, text: string, signal: AbortSignal) => Promise<PromptConvertResult>;

export interface ConvertController {
  /** 用户在 side 侧编辑了文本。 */
  edit: (side: ConvertSide, text: string) => void;
  /** 取消所有待发/在途请求（卸载时调用）。 */
  dispose: () => void;
}

export interface ConvertHandlers {
  /** 结果写入 target 侧。 */
  onResult: (target: ConvertSide, result: PromptConvertResult) => void;
  /** 空输入：直接清空 target 侧与告警，不发请求。 */
  onCleared: (target: ConvertSide) => void;
  onError: (error: unknown) => void;
  onBusy: (busy: boolean) => void;
}

/**
 * 防抖 + 竞态控制（与 React 解耦以便单测）：
 * - 每次 edit 都取消上一个定时器与在途请求（AbortController），并递增序号；
 * - 响应到达时序号不匹配或已 abort 则丢弃，不写结果、不报错。
 * - 空文本不请求，同步清空另一侧。
 */
export function createConvertController(convert: ConvertFn, h: ConvertHandlers, debounceMs = DEBOUNCE_MS): ConvertController {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let abort: AbortController | null = null;
  let seq = 0;

  const cancel = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    abort?.abort();
    abort = null;
    seq += 1;
  };

  const run = (side: ConvertSide, text: string, mine: number) => {
    const ac = new AbortController();
    abort = ac;
    h.onBusy(true);
    convert(directionFrom(side), text, ac.signal).then(
      (res) => {
        if (mine !== seq || ac.signal.aborted) return;
        abort = null;
        h.onBusy(false);
        h.onResult(otherSide(side), res);
      },
      (err: unknown) => {
        if (mine !== seq || ac.signal.aborted) return;
        abort = null;
        h.onBusy(false);
        h.onError(err);
      },
    );
  };

  return {
    edit(side, text) {
      cancel();
      if (text.trim() === '') {
        h.onBusy(false);
        h.onCleared(otherSide(side));
        return;
      }
      const mine = seq;
      timer = setTimeout(() => {
        timer = null;
        run(side, text, mine);
      }, debounceMs);
    },
    dispose() {
      cancel();
    },
  };
}
