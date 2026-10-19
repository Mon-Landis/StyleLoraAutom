// F30 前端纯函数：变体 key 校验、生成/pending 状态读取、条目 PATCH 体构造。
import type { Aspect, BaseField, Entry, EntryPatch } from '../api/promptGroupTypes';

/** 与后端 §2.1 限额一致：每条目最多 64 个 key，key 1–128 字符。 */
export const MAX_VARIANT_KEYS = 64;
export const MAX_VARIANT_KEY_LEN = 128;
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

export const variantsOf = (e: Pick<Entry, 'variants'>): Record<string, string> => e.variants ?? {};

export const isFieldGenerated = (e: Pick<Entry, 'generated'>, f: BaseField): boolean => e.generated?.[f] === true;
export const isVariantGenerated = (e: Pick<Entry, 'generated'>, key: string): boolean => e.generated?.variants?.[key] === true;
export const isFieldPending = (e: Pick<Entry, 'pending'>, f: BaseField): boolean => !!e.pending?.[f];

export type KeyCheck = { ok: true; key: string } | { ok: false; error: string };

/** 去首尾空白、区分大小写；1–128 字符，拒绝控制字符与重名、超出 64 个。 */
export function checkVariantKey(raw: string, existing: readonly string[]): KeyCheck {
  const key = raw.trim();
  if (!key) return { ok: false, error: 'key 不能为空' };
  if (key.length > MAX_VARIANT_KEY_LEN) return { ok: false, error: `key 不能超过 ${MAX_VARIANT_KEY_LEN} 个字符` };
  if (CONTROL_CHARS.test(key)) return { ok: false, error: 'key 不能包含控制字符' };
  if (existing.includes(key)) return { ok: false, error: `key「${key}」已存在` };
  if (existing.length >= MAX_VARIANT_KEYS) return { ok: false, error: `每条最多 ${MAX_VARIANT_KEYS} 个变体` };
  return { ok: true, key };
}

/** 表单当前值（字符串形态）。 */
export interface EntryFormValues {
  nl: string;
  aspect: Aspect | '';
  disable: boolean;
}

/**
 * 编辑模式只提交实际触及的字段：文本字段以「发生过真实输入」为准（改后恢复原值也算，
 * 与后端「同值提交亦清标记」一致）；aspect / disable 仅在与原值不同时提交。
 */
export function buildEntryPatch(entry: Entry, v: EntryFormValues, touched: ReadonlySet<BaseField>): EntryPatch {
  const patch: EntryPatch = {};
  // v5：nl 原样提交，不 trim（F31 §7.2）。
  if (touched.has('nl')) patch.nl = v.nl;
  if ((v.aspect || null) !== (entry.aspect ?? null)) patch.aspect = v.aspect || null;
  if (v.disable !== entry.disable) patch.disable = v.disable;
  return patch;
}

export type PatchField = 'nl' | 'aspect' | 'disable';
const PATCH_FIELDS: readonly PatchField[] = ['nl', 'aspect', 'disable'];
export const PATCH_FIELD_LABEL: Record<PatchField, string> = {
  nl: '提示词', aspect: '图幅', disable: '禁用',
};
export const sameValue = (a: unknown, b: unknown): boolean => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
export interface Overwritten { field: PatchField; server: unknown }

/** 「服务端已更新…」类提示里的字段名列表。 */
export const labelOverwritten = (list: readonly Overwritten[]): string =>
  list.map((o) => `「${PATCH_FIELD_LABEL[o.field]}」`).join('、');

/**
 * 412 后的对账：patch 里每个字段，若服务端当前值既不同于编辑起点（他人改过）
 * 又不同于草稿值（保存会覆盖他人的改动），则列出。服务端未返回的字段跳过。
 */
export function overwrittenFields(base: Entry, patch: EntryPatch, server: Partial<Entry>): Overwritten[] {
  const out: Overwritten[] = [];
  for (const f of PATCH_FIELDS) {
    if (!(f in patch)) continue;
    const s = server[f];
    if (s === undefined) continue;
    if (!sameValue(s, base[f]) && !sameValue(s, patch[f])) out.push({ field: f, server: s });
  }
  return out;
}

/** 应用 patch 后 nl 是否非空（后端要求 nl.strip() 非空；判空用 trim，不改写原值）。 */
export function hasBaseContent(entry: Entry, patch: EntryPatch): boolean {
  return (patch.nl ?? entry.nl).trim().length > 0;
}