import { ApiError } from '../api/errors';
import { Banner } from './primitives';
import { Unsupported } from './slots';

/** 统一错误展示：501 -> 「未实现」占位；其余显示错误码与消息。 */
export function ErrorView({ error, feature }: { error: unknown; feature: string }) {
  if (error instanceof ApiError && error.unsupported) return <Unsupported feature={feature} />;
  const msg = error instanceof ApiError ? `${error.code}: ${error.message}` : error instanceof Error ? error.message : String(error);
  return <Banner tone="danger">{msg}</Banner>;
}
