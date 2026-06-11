// ===========================================================================
// account.js — 账户与套餐. Phase 2 placeholder; the plan card + quota meter +
// plans grid + 订单与发票 (§3.8) land in Phase 7 together with the billing
// alignment (B8). Deliberately NO ¥ balance / token / model UI here.
// ===========================================================================
import { state } from "../state.js";
import { esc, pageHead, comingSoon } from "../components.js";

export function mount(el) {
  const c = state.account || {};
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("账户与套餐", "套餐、用量与发票都在这里管理。", "即将上线")}
    <div class="card">
      <h3 class="card-title">当前账户</h3>
      <p class="muted">${esc(c.name || "")}${c.email ? ` · ${esc(c.email)}` : ""}</p>
    </div>
    ${comingSoon({
      icon: "💳",
      title: "套餐管理即将上线",
      lead: "四档透明定价：免费体验 / 单次 ¥29 / 基础版 ¥199/月 / 企业版 ¥599/月。成本透明，绝不超支。",
      points: [
        "当前套餐 + 本月报告额度一目了然",
        "支付宝 / 微信支付订购，支持开发票",
        "修改密码、退出所有设备等安全设置",
      ],
    })}
  </div>`;
}
