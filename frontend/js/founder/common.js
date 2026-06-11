// ===========================================================================
// founder/common.js — shared chrome for the 内部工具 machinery views: the dark
// scope wrapper + the is_admin guard. UI gating only; every /admin* page and
// the sensitive APIs keep their own server-side checks.
// ===========================================================================
import { isAdmin, state } from "../state.js";
import { pageHead } from "../components.js";

// Wraps a ported view's HTML in the dark founder scope.
export function founderFrame(innerHTML) {
  return `<div class="founder-scope">
    <div class="founder-inner">
      <div class="founder-banner"><span class="dot"></span>内部工具 · 仅创始人可见 — 客户界面不展示模型与用量机制</div>
      ${innerHTML}
    </div>
  </div>`;
}

// True when the view may render; otherwise fills `el` with a friendly block.
export function requireAdmin(el) {
  if (isAdmin()) return true;
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("内部工具", state.account
      ? "此区域仅对创始人账户开放。"
      : "请先登录创始人账户。")}
    <div class="empty">
      <div class="empty-icon">🔒</div>
      <div class="empty-title">没有访问权限</div>
      <p class="empty-lead">这里是平台的内部运营工具（模型、成本与接口管理），不属于客户产品的一部分。</p>
    </div>
  </div>`;
  return false;
}
