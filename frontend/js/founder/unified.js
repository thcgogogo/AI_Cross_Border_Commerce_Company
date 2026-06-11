// ===========================================================================
// founder/unified.js — 开发者接口 (the old “Get Unified API” dashboard, ported
// as-is, dark scope). The API storefront stays alive per decision D4, but only
// founders can reach it: key stats, ¥ balance + top-up, subscription plans,
// orders + fapiao, the per-key model picker, and recent usage.
// ===========================================================================
import * as api from "../api.js";
import { state, setAccount, loadFounderCatalog, modelsByProvider, isTextModel } from "../state.js";
import { esc, cny, fmtNum, fmtTime } from "../components.js";
import { founderFrame, requireAdmin } from "./common.js";

let dashSelected = new Set(); // model ids ticked in the editor
let dashSeededFor = null;     // which account dashSelected was seeded from
let payPollTimer = null;

export async function mount(el) {
  if (!requireAdmin(el)) return;
  el.innerHTML = founderFrame(`<p class="muted">加载账户与目录…</p>`);
  await loadFounderCatalog();
  render(el);
  return () => { if (payPollTimer) { clearInterval(payPollTimer); payPollTimer = null; } };
}

function render(el) {
  const c = state.account;
  if (!c) { requireAdmin(el); return; }
  if (dashSeededFor !== c.id) {
    dashSelected = new Set((c.models || []).map((m) => m.id));
    dashSeededFor = c.id;
  }
  el.innerHTML = founderFrame(`
    <h2 class="detail-h">开发者接口</h2>
    <p class="detail-lead">API 中转站的运营台（决定 D4：保留代码与端点，仅创始人可见）。把任意模型绑到一把 OpenAI 兼容的 key 上，端点是 <code>${esc(location.origin)}/v1</code>。</p>
    ${dashboardHTML(c)}`);
  wire(el);
  loadBillingPanels(el);
}

function dashboardHTML(v) {
  const bal = v.balance == null ? "Unlimited" : cny(v.balance);
  const balLow = (v.balance != null && v.balance <= 0) ? " low" : "";

  const shelf = (m) => {
    const on = dashSelected.has(m.id);
    const live = m.available ? "live" : "";
    const tip = m.available ? "Live now" : "Add this provider's key to .env to activate";
    const approx = m.approx ? `<span class="mc-approx">approx</span>` : "";
    return `<label class="shelf-item ${on ? "on" : ""}" title="${esc(tip)}">
      <input type="checkbox" class="dash-check" value="${esc(m.id)}" ${on ? "checked" : ""} />
      <span class="shelf-main">
        <span class="shelf-name"><span class="live-dot ${live}"></span>${esc(m.label)} ${approx}</span>
        <span class="shelf-id">${esc(m.id)}</span>
      </span>
      <span class="shelf-price">${cny(m.output_per_mtok_cny)}<span class="shelf-price-label">/1M out</span></span>
    </label>`;
  };
  const groups = modelsByProvider()
    .map((g) => {
      const items = g.items.filter(isTextModel);
      if (!items.length) return "";
      return `<div class="provider-group">
        <div class="provider-head">
          <span class="provider-name">${esc(g.label)}</span>
          <button type="button" class="dash-group-all" data-provider="${esc(g.provider)}">select all</button>
        </div>
        <div class="shelf-grid">${items.map(shelf).join("")}</div>
      </div>`;
    }).join("");
  const editor = groups || `<p class="muted">模型目录尚未加载。</p>`;

  const usage = (v.usage || []).slice(0, 12);
  const rows = usage.length
    ? usage.map((u) => `<tr>
        <td>${fmtTime(u.at)}</td>
        <td><code>${esc(u.model)}</code></td>
        <td>${fmtNum(u.input_tokens)} / ${fmtNum(u.output_tokens)}</td>
        <td>${cny(u.cost, 4)}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" class="muted">No calls yet — point any OpenAI client at ${esc(location.origin)}/v1 with this key.</td></tr>`;

  return `<div class="dash">
    <div class="dash-head">
      <div>
        <div class="dash-name">${esc(v.name)}</div>
        <div class="dash-key">${esc(v.key_prefix)}…${esc(v.key_last4)}</div>
      </div>
      <div class="dash-balance">
        <span class="dash-bal-val${balLow}">${bal}</span>
        <span class="dash-bal-label">balance left</span>
      </div>
    </div>
    <div class="dash-stats">
      <div class="dash-stat"><span class="ds-val">${v.requests}</span><span class="ds-label">requests</span></div>
      <div class="dash-stat"><span class="ds-val">${cny(v.spent, 2)}</span><span class="ds-label">spent</span></div>
      <div class="dash-stat"><span class="ds-val" id="dashModelsStat">${(v.models || []).length}</span><span class="ds-label">models on key</span></div>
      <button id="dashRefresh" type="button" class="dash-refresh">Refresh</button>
    </div>

    <div class="dash-topup">
      <span class="dash-topup-label">充值 Top up（支付宝 / 微信支付）</span>
      <div class="dash-topup-row">
        <button type="button" class="topup-quick" data-amt="50">+ ¥50</button>
        <button type="button" class="topup-quick" data-amt="100">+ ¥100</button>
        <button type="button" class="topup-quick" data-amt="200">+ ¥200</button>
        <form id="topupForm" class="inline-form topup-form">
          <input type="number" id="topupAmt" min="1" step="10" placeholder="custom ¥ amount" />
          <button type="submit">去支付</button>
        </form>
      </div>
      <p id="topupMsg" class="dash-save-msg" hidden></p>
      <div id="payPanel" class="pay-panel" hidden></div>
    </div>

    <div class="block-title" style="margin-top:22px;">订阅套餐 Subscription</div>
    <div id="planBox"><p class="muted">套餐信息加载中…</p></div>

    <div class="block-title" style="margin-top:22px;">订单与发票 Orders &amp; Fapiao</div>
    <div id="ordersBox"><p class="muted">订单加载中…</p></div>

    <div class="block-title" style="margin-top:22px;">Your models</div>
    <p class="section-hint">Tick the models this key can call, then Save. The dot shows whether each provider's key is configured on the server (green = live now).</p>
    <div class="dash-editor">${editor}</div>
    <div class="dash-save-bar">
      <div class="store-summary"><span id="dashModelCount">${dashSelected.size}</span> model${dashSelected.size === 1 ? "" : "s"} selected</div>
      <button type="button" id="dashSaveModels" class="run-btn">Save my models</button>
    </div>
    <p id="dashModelsMsg" class="dash-save-msg" hidden></p>

    <div class="block-title" style="margin-top:22px;">Recent usage</div>
    <table class="dash-usage">
      <thead><tr><th>When</th><th>Model</th><th>In / Out</th><th>Cost</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function wire(el) {
  el.querySelectorAll(".dash-check").forEach((cb) =>
    cb.addEventListener("change", () => {
      if (cb.checked) dashSelected.add(cb.value); else dashSelected.delete(cb.value);
      const item = cb.closest(".shelf-item");
      if (item) item.classList.toggle("on", cb.checked);
      const count = el.querySelector("#dashModelCount");
      if (count) count.textContent = String(dashSelected.size);
    })
  );
  el.querySelectorAll(".dash-group-all").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.models
        .filter((m) => (m.provider || "anthropic") === btn.dataset.provider && isTextModel(m))
        .forEach((m) => dashSelected.add(m.id));
      render(el);
    })
  );
  const save = el.querySelector("#dashSaveModels");
  if (save) save.addEventListener("click", () => saveDashModels(el));
  const refresh = el.querySelector("#dashRefresh");
  if (refresh) refresh.addEventListener("click", async () => {
    try {
      const data = await api.getMe();
      setAccount((data && data.customer) || null);
    } catch (e) { /* keep the stale view */ }
    render(el);
  });
  const topForm = el.querySelector("#topupForm");
  if (topForm) topForm.addEventListener("submit", (e) => { e.preventDefault(); doTopup(el); });
  el.querySelectorAll(".topup-quick:not(.plan-buy)").forEach((b) =>
    b.addEventListener("click", () => doTopup(el, Number(b.dataset.amt)))
  );
}

async function saveDashModels(el) {
  const btn = el.querySelector("#dashSaveModels");
  const msg = el.querySelector("#dashModelsMsg");
  const show = (text, ok) => {
    if (!msg) return;
    msg.textContent = text;
    msg.hidden = false;
    msg.classList.toggle("ok", !!ok);
    msg.classList.toggle("err", !ok);
  };
  if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
  try {
    const data = await api.saveAccountModels(Array.from(dashSelected));
    state.account = data.customer;
    dashSeededFor = data.customer.id; // dashSelected already equals the saved set
    const n = (data.customer.models || []).length;
    const stat = el.querySelector("#dashModelsStat");
    if (stat) stat.textContent = String(n);
    show(`Saved — your key now unlocks ${n} model${n === 1 ? "" : "s"}.`, true);
  } catch (e) {
    show(e.message, false);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Save my models"; }
  }
}

// ---- ¥ top-up via a real payment order (server credits only from the signed
// provider notify, never from this page) ------------------------------------
async function doTopup(el, amount) {
  if (amount == null) {
    const input = el.querySelector("#topupAmt");
    amount = input ? Number(input.value) : NaN;
  }
  const fail = (text) => {
    const msg = el.querySelector("#topupMsg");
    if (msg) { msg.textContent = text; msg.hidden = false; msg.classList.add("err"); msg.classList.remove("ok"); }
  };
  if (!amount || isNaN(amount) || amount <= 0) return fail("Enter an amount greater than zero.");
  try {
    const order = await api.payCreate({ type: "topup", amount });
    openPayPanel(el, order);
  } catch (e) {
    fail(e.message);
  }
}

function openPayPanel(el, order) {
  const panel = el.querySelector("#payPanel");
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
    <p class="muted">等待支付确认…（支付完成后余额自动更新，可随时关闭收银台页）</p>`;
  panel.hidden = false;
  if (order.pay_url) window.open(order.pay_url, "_blank", "noopener");
  pollOrder(el, order.order_no);
}

function pollOrder(el, orderNo) {
  if (payPollTimer) clearInterval(payPollTimer);
  let ticks = 0;
  payPollTimer = setInterval(async () => {
    ticks += 1;
    if (ticks > 150) { clearInterval(payPollTimer); payPollTimer = null; return; } // ~5 min
    try {
      const data = await api.payStatus(orderNo);
      if (data.status === "paid") {
        clearInterval(payPollTimer);
        payPollTimer = null;
        try {
          const me = await api.getMe();
          state.account = (me && me.customer) || state.account;
        } catch (e) { /* fine */ }
        render(el);
        const msg = el.querySelector("#topupMsg");
        if (msg) {
          msg.textContent = "✅ 支付成功，已到账。";
          msg.hidden = false; msg.classList.add("ok"); msg.classList.remove("err");
        }
      }
    } catch (e) { /* transient — keep polling */ }
  }, 2000);
}

// ---- subscription plans + orders/fapiao panels ------------------------------
async function loadBillingPanels(el) {
  const planBox = el.querySelector("#planBox");
  const ordersBox = el.querySelector("#ordersBox");
  if (!planBox && !ordersBox) return;
  try {
    const [plans, orders] = await Promise.all([
      api.getPlans(),
      api.getOrders().catch(() => ({ orders: [] })),
    ]);
    if (planBox) renderPlanBox(el, planBox, plans);
    if (ordersBox) renderOrdersBox(el, ordersBox, orders.orders || []);
  } catch (e) {
    if (planBox) planBox.innerHTML = `<p class="muted">套餐信息加载失败，请刷新重试。</p>`;
  }
}

function renderPlanBox(el, box, data) {
  const cur = data.current || {};
  const curCode = cur.plan_code || "free";
  const sub = cur.subscription;
  const cards = (data.plans || []).map((p) => {
    const isCur = p.code === curCode;
    const price = p.monthly_price_yuan > 0 ? `¥${p.monthly_price_yuan}/月` : "免费";
    const buy = p.code === "free" ? "" :
      `<button type="button" class="topup-quick plan-buy" data-plan="${esc(p.code)}">${isCur ? "续费" : "订购"} ${price}</button>`;
    return `<div class="shelf-item ${isCur ? "on" : ""}" style="flex-direction:column;align-items:flex-start;gap:5px;">
      <span class="shelf-name">${esc(p.label)}${isCur ? " · 当前 ✓" : ""}</span>
      <span class="shelf-id">每月 ${p.monthly_report_quota} 份报告 · ${p.seats} 个席位 · ${price}</span>
      ${buy}
    </div>`;
  }).join("");
  const usage = (cur.reports_used_this_month != null && cur.monthly_report_quota)
    ? `<p class="muted">本月已用 ${cur.reports_used_this_month}/${cur.monthly_report_quota} 份报告。${cur.locked ? " ⚠️ 订阅已逾期，功能已锁定（仅可导出数据），续费后恢复。" : ""}</p>`
    : "";
  let subLine = "";
  if (sub && sub.status !== "canceled") {
    const end = (sub.current_period_end || "").slice(0, 10);
    subLine = sub.cancel_at_period_end
      ? `<p class="muted">订阅将于 ${esc(end)} 到期后不再续订。 <button type="button" class="dash-group-all" id="subResume">恢复续订</button></p>`
      : `<p class="muted">当前订阅有效期至 ${esc(end)}（到期前 3 天会发送续费提醒，不自动扣款）。 <button type="button" class="dash-group-all" id="subCancel">到期后取消</button></p>`;
  }
  box.innerHTML = `<div class="shelf-grid">${cards}</div>${usage}${subLine}<p id="planMsg" class="dash-save-msg" hidden></p>`;
  box.querySelectorAll(".plan-buy").forEach((b) =>
    b.addEventListener("click", () => subscribePlan(el, b.dataset.plan)));
  const c = box.querySelector("#subCancel");
  if (c) c.addEventListener("click", () => toggleSubRenew(el, "cancel"));
  const r = box.querySelector("#subResume");
  if (r) r.addEventListener("click", () => toggleSubRenew(el, "resume"));
}

async function subscribePlan(el, planCode) {
  try {
    const order = await api.payCreate({ type: "subscription", plan_code: planCode });
    openPayPanel(el, order);
  } catch (e) {
    const msg = el.querySelector("#planMsg");
    if (msg) { msg.textContent = e.message; msg.hidden = false; msg.classList.add("err"); }
  }
}

async function toggleSubRenew(el, which) {
  try {
    if (which === "cancel") await api.subscriptionCancel();
    else await api.subscriptionResume();
    loadBillingPanels(el);
  } catch (e) {
    const msg = el.querySelector("#planMsg");
    if (msg) { msg.textContent = e.message; msg.hidden = false; msg.classList.add("err"); }
  }
}

function renderOrdersBox(el, box, orders) {
  if (!orders.length) {
    box.innerHTML = `<p class="muted">还没有订单 — 充值或订购套餐后会出现在这里。</p>`;
    return;
  }
  const stateCn = { created: "待支付", paid: "已支付", failed: "失败", refunded: "已退款" };
  const fapiaoCell = (o) => {
    if (o.status !== "paid") return "—";
    if (o.fapiao_status === "requested") return "审核中";
    if (o.fapiao_status === "issued") return "已开具";
    return `<button type="button" class="dash-group-all fapiao-req" data-order="${esc(o.order_no)}">申请发票</button>`;
  };
  const rows = orders.map((o) => `<tr>
      <td>${fmtTime(o.created_at)}</td>
      <td>${o.type === "subscription" ? `订阅 ${esc(o.plan_code || "")}` : "充值"}</td>
      <td>${cny(o.amount_yuan)}</td>
      <td>${stateCn[o.status] || esc(o.status)}${o.refunded_yuan ? `（已退 ${cny(o.refunded_yuan)}）` : ""}</td>
      <td>${esc(o.provider)}</td>
      <td>${fapiaoCell(o)}</td>
    </tr>`).join("");
  box.innerHTML = `<table class="dash-usage">
    <thead><tr><th>时间</th><th>类型</th><th>金额</th><th>状态</th><th>渠道</th><th>发票</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p id="fapiaoMsg" class="dash-save-msg" hidden></p>`;
  box.querySelectorAll(".fapiao-req").forEach((b) =>
    b.addEventListener("click", () => requestFapiao(el, b.dataset.order)));
}

async function requestFapiao(el, orderNo) {
  const title = (window.prompt("发票抬头（公司名称）：") || "").trim();
  if (!title) return;
  const taxNo = (window.prompt("税号（统一社会信用代码）：") || "").trim();
  if (!taxNo) return;
  const email = (window.prompt("接收发票的邮箱（留空使用账户邮箱）：") || "").trim();
  const show = (text, ok) => {
    const msg = el.querySelector("#fapiaoMsg");
    if (msg) {
      msg.textContent = text; msg.hidden = false;
      msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
    }
  };
  try {
    await api.requestFapiao({ order_no: orderNo, title, tax_no: taxNo, email: email || undefined });
    await loadBillingPanels(el);
    show("发票申请已提交，开具后将发送到邮箱。", true);
  } catch (e) {
    show(e.message, false);
  }
}
