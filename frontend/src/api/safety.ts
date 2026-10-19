// 安全设置与会话确认 API（F27 §4/§8）。
import { get, patch, post, uuid } from './client';
import type { NsfwDisplayMode, SafetySettings, SessionSafetyState } from './promptGroupTypes';

/** ProductSettings（GET /api/settings）。 */
export interface SettingsView extends Partial<SafetySettings> {
  version?: string;
  revision?: number;
  /** 后端进程内会话确认（GET /settings 随带，docs/frontend-contract-f27.md）。 */
  revealed_this_run?: boolean;
}

export const getSettings = () =>
  get<SettingsView>('/api/settings').then((r) => ({ data: r.data, etag: r.etag ?? (r.data.version ?? null) }));

export interface SettingsPatch {
  nsfw_enabled?: boolean;
  nsfw_display_mode?: NsfwDisplayMode;
}
export const patchSettings = (body: SettingsPatch, etag: string, key = uuid()) =>
  patch<SettingsView>('/api/settings', body, { ifMatch: etag, idempotencyKey: key }).then((r) => ({ data: r.data, etag: r.etag ?? (r.data.version ?? null) }));

/** 点击「显示」：后端 revealed_this_run=true。 */
export const revealSession = () => post<SessionSafetyState>('/api/safety/reveal', {}).then((r) => r.data);
