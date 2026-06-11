// ===========================================================================
// pay.js — the shared cashier panel + order polling (Phase 7, plan §3.8 ②).
// Extracted from founder/unified.js so 账户与套餐 and the founder storefront
// drive the SAME flow: /api/pay/create hands back an order; openPayPanel
// renders its cashier link / QR text and polls /api/pay/status until the
// signed provider notify flips it to paid (the server credits ONLY from that
// verified notify — never from this page).
// ===========================================================================
import * as api from "./api.js";
import { esc, cny } from "./components.js";

let payPollTimer = null;

export function stopPayPoll() {
  if (payPollTimer) { clearInterval(payPollTimer); payPollTimer = null; }
}

// Render a freshly created order's cashier panel into `panel` and start
// polling. `onPaid(statusPayload)` fires exactly once when it flips to paid.
export function openPayPanel(panel, order, { onPaid = null } = {}) {
  if (!panel) return;
  const link = order.pay_url
    ? `<a href="${esc(order.pay_url)}" target="_blank" rel="noopener" class="run-btn" style="display:inline-block;width:auto;text-decoration:none;">打开收银台支付 ${cny(order.amount)}</a>`
    : "";
  const qr = order.qr_text
    ? `<p class="muted" style="word-break:break-all;">微信扫码串（用任意二维码工具生成后扫码）：<code>${esc(order.qr_text)}</code></p>`
    : "";
  panel.innerHTML = `
    <div><strong>订单 ${esc(order.order_no)}</strong> · ${cny(order.amount)} · ${esc(order.provider)}</div>
    <div style="margin:8px 0;">${link}${qr}</div>
    <p class="muted">等待支付确认…（支付完成后自动更新，可随时关闭收银台页）</p>`;
  panel.hidden = false;
  if (order.pay_url) window.open(order.pay_url, "_blank", "noopener");
  pollOrder(order.order_no, { panel, onPaid });
}

export function pollOrder(orderNo, { panel = null, onPaid = null } = {}) {
  stopPayPoll();
  let ticks = 0;
  payPollTimer = setInterval(async () => {
    ticks += 1;
    if (ticks > 150) { stopPayPoll(); return; } // ~5 min, then give up quietly
    try {
      const data = await api.payStatus(orderNo);
      if (data.status === "paid") {
        stopPayPoll();
        if (panel) panel.innerHTML = `<p class="muted">✅ 支付成功，已到账。</p>`;
        if (onPaid) onPaid(data);
      }
    } catch (e) { /* transient — keep polling */ }
  }, 2000);
}
