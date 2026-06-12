// ===========================================================================
// errors.js — the shared error catalogue (Phase 10 B2). One place decides how
// a failed fetch renders, so every view reacts the same way:
//   401 → re-open the login modal (the session is gone — a raw "请先登录"
//         paragraph strands the user with no way back in);
//   402 → the shared upgrade/quota card (the same copy 生成报告 hand-rolled);
//   no .status (a bare TypeError — offline, DNS, server unreachable; ApiError
//         only covers parsed HTTP responses) → the reusable 重试 block;
//   anything else → an error card with the server's message + 重试.
// Views call renderErrorState(box, e, onRetry) and pass a loader to re-run.
// ===========================================================================
import { esc } from "./components.js";
import { bus } from "./state.js";

export const isAuthError = (e) => !!e && e.status === 401;
export const isQuotaError = (e) => !!e && e.status === 402;
// ApiError always carries the HTTP status; a rejected fetch() throws a bare
// TypeError with no .status — that's the network/offline signature.
export const isNetworkError = (e) => !!e && e.status == null;

// auth.js listens for this and opens the login modal (no import cycle:
// errors.js ← views, auth.js ← main.js, both share the state.js bus).
export function requestLogin() {
  bus.dispatchEvent(new Event("auth-required"));
}

// ---- HTML blocks (pure builders; renderErrorState wires the buttons) --------
export function networkRetryHTML(retryId = "errRetry") {
  return `<div class="empty err-state" style="border:none;padding:26px 10px;">
      <div class="empty-icon" aria-hidden="true">📡</div>
      <div class="empty-title">网络似乎断开了</div>
      <p class="empty-lead">数据暂时取不回来，可能是网络波动。检查一下网络，再试一次。</p>
      <button type="button" class="run-btn" id="${esc(retryId)}" style="max-width:240px;">重试</button>
    </div>`;
}

export function loginRequiredHTML() {
  return `<div class="empty err-state" style="border:none;padding:26px 10px;">
      <div class="empty-icon" aria-hidden="true">🔐</div>
      <div class="empty-title">登录已过期</div>
      <p class="empty-lead">为了保护你的经营数据，长时间未操作需要重新登录。</p>
    </div>`;
}

// The shared 402 card — quota spent / plan feature missing. `message` is the
// server's friendly refusal copy (it already sells the right next step).
export function upgradeCardHTML(message) {
  return `<div class="empty err-state" style="border:none;padding:26px 10px;">
      <div class="empty-icon" aria-hidden="true">🔓</div>
      <div class="empty-title">需要升级套餐</div>
      <p class="empty-lead">${esc(message || "当前套餐的报告额度已用完。升级后可继续生成。")}</p>
      <a class="run-btn" href="/account" data-nav
        style="display:block;max-width:240px;margin:14px auto 0;text-decoration:none;text-align:center;">查看套餐与升级</a>
    </div>`;
}

export function errorRetryHTML(message, retryId = "errRetry") {
  return `<div class="empty err-state" style="border:none;padding:26px 10px;">
      <div class="empty-icon" aria-hidden="true">⚠️</div>
      <div class="empty-title">暂时无法加载</div>
      <p class="empty-lead">${esc(message || "出了点小问题，稍等片刻再试一次。")}</p>
      <button type="button" class="run-btn" id="${esc(retryId)}" style="max-width:240px;">重试</button>
    </div>`;
}

// ---- the dispatcher -----------------------------------------------------------
// Renders the right block for `e` inside `box` (a card/container element) and
// wires 重试 to `onRetry`. On 401 it also pops the login modal via the bus.
export function renderErrorState(box, e, onRetry) {
  if (!box || !box.isConnected) return;
  if (isAuthError(e)) {
    box.innerHTML = `<div class="card">${loginRequiredHTML()}</div>`;
    requestLogin();
    return;
  }
  if (isQuotaError(e)) {
    box.innerHTML = `<div class="card">${upgradeCardHTML(e.message)}</div>`;
    return;
  }
  const html = isNetworkError(e) ? networkRetryHTML() : errorRetryHTML(e && e.message);
  box.innerHTML = `<div class="card">${html}</div>`;
  const btn = box.querySelector("#errRetry");
  if (btn && onRetry) btn.addEventListener("click", onRetry);
}
