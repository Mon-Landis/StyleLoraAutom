/**
 * 浏览器前进/后退守卫（HashRouter 不是 data router，不能用 useBlocker，所以用 popstate 拦截）。
 *
 * 原理：在 window 的 popstate **捕获阶段**先于 React Router 的监听器运行（现代浏览器对 target 上的
 * 捕获监听器先于冒泡监听器触发）。检测到「离开当前页」且需要守卫（有未保存草稿）时：
 *  1. stopImmediatePropagation：路由器收不到这次 popstate，页面不会卸载；
 *  2. history.pushState(原 state, '', 原 href)：把地址栏恢复到当前页（浏览器此时已经跳到目标条目）；
 *  3. 执行 guard（flush 草稿 + 发布）；
 *  4. 成功：history.go(-1) 回到目标条目，这次 popstate 放行给路由器，完成原本的导航；
 *     失败：留在本页并调用 onBlocked 通知，草稿保留。
 *
 * 限制：
 *  - 步骤 2 用 pushState，会截断被恢复的前进栈、并多出一条同 URL 的历史记录（仅在被拦截时发生）；
 *  - 依赖「捕获监听器先于路由器监听器」的浏览器行为；若某浏览器顺序相反，页面会先卸载，
 *    此时由 useAutoCompletion 卸载 cleanup 里的 leave 兜底（草稿不会被清除，失败时通知「草稿已保留」）；
 *  - 没有草稿时不拦截，直接放行（离开不再发布任何补齐任务）。
 */
export interface GuardWindow {
  location: { hash: string; href: string };
  history: { state: unknown; pushState: (state: unknown, title: string, url: string) => void; go: (delta: number) => void };
  addEventListener: (type: 'popstate', l: (e: Event) => void, capture: boolean) => void;
  removeEventListener: (type: 'popstate', l: (e: Event) => void, capture: boolean) => void;
}

export interface HistoryGuardOptions {
  /** 当前是否需要拦截离开（如存在未保存草稿）。 */
  shouldGuard: () => boolean;
  /** 执行离开前的保存/发布；返回 false（或抛错）表示不能离开。 */
  guard: () => Promise<boolean>;
  /** 导航被阻止（留在本页）时调用，用于通知。 */
  onBlocked: () => void;
  win?: GuardWindow;
}

/** 取 hash 路由的路径部分：'#/prompts?x=1' -> '/prompts'。 */
export function hashPath(hash: string): string {
  const h = hash.startsWith('#') ? hash.slice(1) : hash;
  return h.split(/[?#]/)[0] || '/';
}

export function installHistoryGuard(o: HistoryGuardOptions): () => void {
  const win: GuardWindow = o.win ?? (window as unknown as GuardWindow);
  const snapshot = () => ({ path: hashPath(win.location.hash), href: win.location.href, state: win.history.state });
  let here = snapshot();
  let busy = false;
  let passThrough = false;

  const run = async () => {
    let ok = false;
    try { ok = await o.guard(); } catch { ok = false; }
    busy = false;
    if (ok) {
      passThrough = true; // 下一次 popstate 是我们自己回到目标条目，放行给路由器
      win.history.go(-1);
    } else {
      o.onBlocked();
    }
  };

  const onPop = (ev: Event) => {
    if (passThrough) { passThrough = false; return; }
    if (hashPath(win.location.hash) === here.path) { here = snapshot(); return; } // 同页内的历史变化
    if (!busy && !o.shouldGuard()) return; // 无草稿：直接离开
    ev.stopImmediatePropagation();
    win.history.pushState(here.state, '', here.href);
    if (busy) return; // 保存进行中又按了后退：只恢复 URL
    busy = true;
    void run();
  };

  win.addEventListener('popstate', onPop, true);
  return () => win.removeEventListener('popstate', onPop, true);
}
