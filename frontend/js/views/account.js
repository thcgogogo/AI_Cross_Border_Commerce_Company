// ===========================================================================
// account.js — 账户与套餐 (plan §3.8, Phase 7).
// ① Current-plan card: quota meter (+ 单次报告余量), locked banner, 续费/到期
//   不续; ② plans grid — the four flat offers (免费体验 / 单次 ¥29 / 基础版 /
//   企业版) through the shared planCard atoms + the shared pay flow (js/pay.js);
// ③ 订单与发票 with a proper fapiao form (no window.prompt); ④ the 成本透明，
//   绝不超支 promise; ⑤ security — 修改密码 (SMS code) + 退出所有设备.
//
// EXPLICITLY ABSENT (plan §3.8 "Removed from customer view"): ¥ balance,
// top-up, usage tables, model picker, API key. state.account still carries
// those fields for the founder storefront — this page never reads them.
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, cny, pageHead, quotaMeter, planCard, singleReportCard, skeleton, skeletonCard } from "../components.js";
import { openPayPanel, stopPayPoll } from "../pay.js";

const ORDER_STATE_CN = { created: "待支付", paid: "已支付", failed: "失败", refunded: "已退款" };

export async function mount(el) {
  const c = state.account || {};
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("账户与套餐", "套餐、用量与发票都在这里管理。")}
    <div class="card">
      <h3 class="card-title">当前账户</h3>
      <p class="muted" style="margin:4px 0 0;">${esc(c.name || "")}${c.email ? ` · ${esc(c.email)}` : ""}${c.company ? ` · ${esc(c.company)}` : ""}</p>
    </div>
    <div id="acctPlanBox" style="margin-top:14px;">${skeletonCard({ lines: 3 })}</div>
    <div class="card" style="margin-top:14px;">
      <h3 class="card-title"><span aria-hidden="true">💳</span> 套餐与购买</h3>
      <div class="pricing-grid acct-plans" id="acctPlans" style="margin-top:14px;">${skeleton({ lines: 3 })}</div>
      <p class="pricing-note">支付宝 / 微信支付 · 可开发票</p>
      <div id="acctPayPanel" class="pay-panel" hidden></div>
      <p id="acctPayMsg" class="form-msg" hidden></p>
    </div>
    <div class="card" style="margin-top:14px;">
      <h3 class="card-title"><span aria-hidden="true">📄</span> 订单与发票</h3>
      <div id="acctOrders">${skeleton({ lines: 2 })}</div>
      <div id="fapiaoBox" class="fapiao-form" hidden></div>
      <p id="fapiaoMsg" class="form-msg" hidden></p>
    </div>
    <div class="card acct-promise" style="margin-top:14px;">
      <h3 class="card-title">成本透明，绝不超支</h3>
      <p class="muted" style="margin:6px 0 0;">固定价格，按价值计费——价格与「用量」无关，没有任何按量计费项目。
        生成报告的内部成本由我们持续优化，省下的是我们的事；你看到的永远是套餐价。
        随时取消，服务保留到当期结束，不扣后续费用。</p>
    </div>
    <div class="card" style="margin-top:14px;">
      <h3 class="card-title"><span aria-hidden="true">🔐</span> 安全设置</h3>
      <div class="acct-security">
        <div>
          <h4 class="acct-sub-h">修改密码</h4>
          <p class="muted" style="margin:2px 0 8px;">通过手机验证码修改；修改后所有设备需重新登录。</p>
          <div class="code-row">
            <input class="field-input code-input" type="text" id="pwCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" autocomplete="one-time-code">
            <button type="button" class="code-btn" id="pwSendCode">发送验证码</button>
          </div>
          <label class="field-label" for="pwNew">新密码</label>
          <input class="field-input" type="password" id="pwNew" placeholder="至少 8 位" autocomplete="new-password">
          <button type="button" class="run-btn" id="pwSubmit" style="max-width:220px;margin-top:10px;">确认修改</button>
          <p id="pwMsg" class="form-msg" hidden></p>
        </div>
        <div>
          <h4 class="acct-sub-h">退出所有设备</h4>
          <p class="muted" style="margin:2px 0 8px;">让其他浏览器/手机上的登录全部失效（含本机）。</p>
          <button type="button" class="ghost-btn" id="logoutAllBtn">退出所有设备</button>
          <p id="logoutAllMsg" class="form-msg" hidden></p>
        </div>
      </div>
    </div>
  </div>`;

  wireSecurity(el);
  await refresh(el);
  return () => stopPayPoll();
}

// ---------------------------------------------------------------------------
// data load + the three billing sections
// ---------------------------------------------------------------------------
async function refresh(el) {
  let plans = null;
  let orders = [];
  let failed = false;
  try {
    const [p, o] = await Promise.all([
      api.getPlans(),
      api.getOrders().catch(() => ({ orders: [] })),
    ]);
    plans = p;
    orders = (o && o.orders) || [];
  } catch (e) {
    failed = true;
  }
  if (!el.isConnected) return;

  const planBox = el.querySelector("#acctPlanBox");
  const grid = el.querySelector("#acctPlans");
  if (failed || !plans) {
    planBox.innerHTML = `<div class="card"><p class="muted">套餐信息暂时无法加载。
      <button type="button" class="link-btn" id="acctRetry">重试</button></p></div>`;
    const retry = planBox.querySelector("#acctRetry");
    if (retry) retry.addEventListener("click", () => refresh(el));
    grid.innerHTML = "";
    return;
  }

  renderCurrentPlan(el, planBox, plans);
  renderPlansGrid(el, grid, plans);
  renderOrders(el, el.querySelector("#acctOrders"), orders, plans.plans || []);
}

// ① current plan + quota meter + 续费/到期不续 + locked banner
function renderCurrentPlan(el, box, data) {
  const cur = data.current || {};
  const plans = data.plans || [];
  const p = plans.find((x) => x.code === cur.plan_code) || { label: "免费体验" };
  const credits = Number(cur.report_credits || 0);
  const total = Number(cur.monthly_report_quota || 0) + credits;
  const caption = cur.lifetime_quota ? "已生成（体验额度）" : "本月已生成";

  const lockedBanner = cur.locked
    ? `<div class="acct-locked">⚠️ 订阅已逾期，功能已锁定（仅可导出已有数据）。续费后立即恢复。</div>`
    : "";
  const creditsLine = credits > 0
    ? `<p class="muted" style="margin:8px 0 0;">含单次报告余量 <strong>${credits}</strong> 份（已计入上方额度）。</p>`
    : "";

  const sub = cur.subscription;
  let subLine = "";
  if (sub && sub.status !== "canceled") {
    const end = (sub.current_period_end || "").slice(0, 10);
    subLine = sub.cancel_at_period_end
      ? `<p class="muted" style="margin:8px 0 0;">订阅将于 ${esc(end)} 到期后不再续订。
          <button type="button" class="link-btn" id="subResume">恢复续订</button></p>`
      : `<p class="muted" style="margin:8px 0 0;">当前订阅有效期至 ${esc(end)}（到期前 3 天提醒续费，不自动扣款）。
          <button type="button" class="link-btn" id="subCancel">到期后取消</button></p>`;
  }

  box.innerHTML = `<div class="card">
    <h3 class="card-title">当前套餐：${esc(p.label)}</h3>
    ${lockedBanner}
    <div style="margin-top:10px;">
      ${quotaMeter({ used: Number(cur.reports_used_this_month || 0), total, caption })}
    </div>
    ${creditsLine}
    ${subLine}
    <p id="subMsg" class="form-msg" hidden></p>
  </div>`;

  const showMsg = (text, ok) => {
    const msg = box.querySelector("#subMsg");
    if (!msg) return;
    msg.textContent = text; msg.hidden = false;
    msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
  };
  const cancel = box.querySelector("#subCancel");
  if (cancel) cancel.addEventListener("click", async () => {
    try { await api.subscriptionCancel(); refresh(el); }
    catch (e) { showMsg(e.message, false); }
  });
  const resume = box.querySelector("#subResume");
  if (resume) resume.addEventListener("click", async () => {
    try { await api.subscriptionResume(); refresh(el); }
    catch (e) { showMsg(e.message, false); }
  });
}

// ② the four flat cards — purchases via the shared pay flow
function renderPlansGrid(el, grid, data) {
  const cur = data.current || {};
  const plans = data.plans || [];
  const byCode = Object.fromEntries(plans.map((p) => [p.code, p]));
  const single = (state.meta && state.meta.single_report_price_yuan) || 0;
  const curCode = cur.plan_code || "free";

  const subCta = (p) => ({
    label: p.code === curCode ? `续费 ${cny(p.monthly_price_yuan, 0)}/月` : `订购 ${cny(p.monthly_price_yuan, 0)}/月`,
    attrs: `data-plan="${esc(p.code)}"`,
  });
  grid.innerHTML = [
    planCard(byCode.free, { current: curCode === "free" }),
    singleReportCard(single, { cta: { label: `购买一份 ${cny(single, 0)}`, attrs: "data-single" } }),
    byCode.basic ? planCard(byCode.basic, { featured: curCode !== "basic", current: curCode === "basic", cta: subCta(byCode.basic) }) : "",
    byCode.pro ? planCard(byCode.pro, { current: curCode === "pro", cta: subCta(byCode.pro) }) : "",
  ].join("");

  const payMsg = (text) => {
    const msg = el.querySelector("#acctPayMsg");
    if (!msg) return;
    msg.textContent = text; msg.hidden = false; msg.classList.add("err");
  };
  const startOrder = async (payload) => {
    try {
      const order = await api.payCreate(payload);
      openPayPanel(el.querySelector("#acctPayPanel"), order, {
        onPaid() { refresh(el); },
      });
    } catch (e) {
      payMsg(e.message);
    }
  };
  grid.querySelectorAll("[data-plan]").forEach((b) =>
    b.addEventListener("click", () => startOrder({ type: "subscription", plan_code: b.dataset.plan })));
  grid.querySelectorAll("[data-single]").forEach((b) =>
    b.addEventListener("click", () => startOrder({ type: "single_report" })));
}

// ③ orders table + the fapiao request form (a real form, not window.prompt)
function orderTypeLabel(o, plans) {
  if (o.type === "subscription") {
    const p = (plans || []).find((x) => x.code === o.plan_code);
    return `订阅 · ${p ? p.label : (o.plan_code || "")}`;
  }
  if (o.type === "single_report") return "单次报告";
  return o.subject || "充值";
}

function renderOrders(el, box, orders, plans) {
  if (!orders.length) {
    box.innerHTML = `<p class="muted">还没有订单 — 订购套餐或单次购买后会出现在这里。</p>`;
    return;
  }
  const fapiaoCell = (o) => {
    if (o.status !== "paid") return "—";
    if (o.fapiao_status === "requested") return "审核中";
    if (o.fapiao_status === "issued") return "已开具";
    return `<button type="button" class="link-btn fapiao-req" data-order="${esc(o.order_no)}">申请发票</button>`;
  };
  const rows = orders.map((o) => `<tr>
      <td>${esc((o.created_at || "").slice(0, 10))}</td>
      <td>${esc(orderTypeLabel(o, plans))}</td>
      <td>${cny(o.amount_yuan)}</td>
      <td>${esc(ORDER_STATE_CN[o.status] || o.status)}${o.refunded_yuan ? `（已退 ${cny(o.refunded_yuan)}）` : ""}</td>
      <td>${fapiaoCell(o)}</td>
    </tr>`).join("");
  box.innerHTML = `<div class="md-table-wrap"><table class="md-table">
    <thead><tr><th>日期</th><th>内容</th><th>金额</th><th>状态</th><th>发票</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
  box.querySelectorAll(".fapiao-req").forEach((b) =>
    b.addEventListener("click", () => openFapiaoForm(el, b.dataset.order)));
}

function openFapiaoForm(el, orderNo) {
  const boxEl = el.querySelector("#fapiaoBox");
  if (!boxEl) return;
  boxEl.innerHTML = `
    <h4 class="acct-sub-h">开具发票 · 订单 ${esc(orderNo)}</h4>
    <label class="field-label" for="fpTitle">发票抬头（公司名称）</label>
    <input class="field-input" type="text" id="fpTitle" placeholder="例如：杭州示例科技有限公司">
    <label class="field-label" for="fpTax">税号（统一社会信用代码）</label>
    <input class="field-input" type="text" id="fpTax" placeholder="18 位统一社会信用代码">
    <label class="field-label" for="fpEmail">接收邮箱 <span class="field-optional">（留空使用账户邮箱）</span></label>
    <input class="field-input" type="email" id="fpEmail" placeholder="${esc((state.account || {}).email || "")}">
    <div style="display:flex;gap:10px;margin-top:12px;">
      <button type="button" class="run-btn" id="fpSubmit" style="max-width:180px;">提交申请</button>
      <button type="button" class="ghost-btn" id="fpCancel">取消</button>
    </div>`;
  boxEl.hidden = false;
  const showMsg = (text, ok) => {
    const msg = el.querySelector("#fapiaoMsg");
    if (!msg) return;
    msg.textContent = text; msg.hidden = false;
    msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
  };
  boxEl.querySelector("#fpCancel").addEventListener("click", () => {
    boxEl.hidden = true; boxEl.innerHTML = "";
  });
  boxEl.querySelector("#fpSubmit").addEventListener("click", async () => {
    const title = boxEl.querySelector("#fpTitle").value.trim();
    const taxNo = boxEl.querySelector("#fpTax").value.trim();
    const email = boxEl.querySelector("#fpEmail").value.trim();
    if (!title) return showMsg("请填写发票抬头（公司名称）。", false);
    if (!taxNo) return showMsg("请填写税号。", false);
    try {
      await api.requestFapiao({ order_no: orderNo, title, tax_no: taxNo,
                                email: email || undefined });
      boxEl.hidden = true; boxEl.innerHTML = "";
      showMsg("发票申请已提交，开具后将发送到邮箱。", true);
      refresh(el);
    } catch (e) {
      showMsg(e.message, false);
    }
  });
}

// ⑤ security — 修改密码 (SMS reset flow for the logged-in email) + 退出所有设备
function wireSecurity(el) {
  const email = (state.account || {}).email || "";
  const pwMsg = (text, ok) => {
    const msg = el.querySelector("#pwMsg");
    if (!msg) return;
    msg.textContent = text; msg.hidden = false;
    msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
  };

  el.querySelector("#pwSendCode").addEventListener("click", async () => {
    const btn = el.querySelector("#pwSendCode");
    btn.disabled = true;
    try {
      await api.sendCode({ email, purpose: "reset" });
      pwMsg("验证码已发送到账户绑定的手机号。", true);
      let left = 60;
      const tick = setInterval(() => {
        left -= 1;
        if (left <= 0 || !btn.isConnected) {
          clearInterval(tick);
          btn.disabled = false; btn.textContent = "发送验证码";
        } else {
          btn.textContent = `重新发送（${left}s）`;
        }
      }, 1000);
    } catch (e) {
      btn.disabled = false;
      pwMsg(e.message, false);
    }
  });

  el.querySelector("#pwSubmit").addEventListener("click", async () => {
    const code = el.querySelector("#pwCode").value.trim();
    const pw = el.querySelector("#pwNew").value;
    if (!code) return pwMsg("请填写短信验证码。", false);
    if ((pw || "").length < 8) return pwMsg("新密码至少 8 位。", false);
    try {
      await api.resetPassword({ email, code, new_password: pw });
      pwMsg("密码已修改，所有设备已退出登录，正在跳转重新登录…", true);
      setTimeout(() => location.assign("/login"), 1500);
    } catch (e) {
      pwMsg(e.message, false);
    }
  });

  el.querySelector("#logoutAllBtn").addEventListener("click", async () => {
    if (!window.confirm("确定退出所有设备吗？所有登录（含本机）都将失效。")) return;
    try {
      await api.logoutAll();
      location.assign("/");
    } catch (e) {
      const msg = el.querySelector("#logoutAllMsg");
      if (msg) { msg.textContent = e.message; msg.hidden = false; msg.classList.add("err"); }
    }
  });
}
