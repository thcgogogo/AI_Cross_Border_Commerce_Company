// ===========================================================================
// cash_position.js — 现金位置 / 资金跑道 (the 跨境财务 capstone).
//
// Mounted as the LEAD tab INSIDE the 跨境财务 hub (cross_border.js), not a standalone
// section: its value IS being downstream of the reconciled hub data. The hub passes
// ctx = { params:{runId}, navigate, base:"/cross-border/cash" }. Two surfaces:
//   • base               a DASHBOARD: the headline position (可动用 vs 锁定/在途), the next
//                        cash crunch, an inline 设置假设 editor (assumptions + manual inputs
//                        the hub can't know), and «立即计算» → a run → the report.
//   • base/<runId>       the report: position + week-by-week runway table + 【N】 prose + PDF.
//
// EVERY number is the deterministic engine's (server-side). Decision-support only — never
// advice, never an action. Customer-safe /api/cash/* endpoints (no ¥/model/token).
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, pageHead, renderMarkdown, skeleton, statusPillHTML } from "../components.js";
import { pollJobStatus } from "../poll.js";

const fmtY = (n) => "¥" + Number(n || 0).toLocaleString("zh-CN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// 备货 / replenishment-plan row columns (Phase 3). label/amount/currency/date are the core;
// 定金%(deposit_pct) + 头程(freight_days) are optional → 定金 now, 尾款 on arrival.
const RESTOCK_COLS = [
  { k: "label", ph: "说明，如 夏季补货" }, { k: "amount", ph: "金额", type: "number" },
  { k: "currency", ph: "币种 CNY/USD" }, { k: "due_date", ph: "下单/付款日 2026-07-15" },
  { k: "deposit_pct", ph: "定金%(可选)", type: "number" },
  { k: "freight_days", ph: "头程天(可选)", type: "number" },
];

// ---------------------------------------------------------------------------
export async function mount(el, ctx) {
  const base = (ctx && ctx.base) || "/cross-border/cash";
  if (ctx.params && ctx.params.runId) return mountResult(el, ctx, ctx.params.runId, base);
  return mountDashboard(el, ctx, base);
}

// ---- dashboard ------------------------------------------------------------
async function mountDashboard(el, ctx, base) {
  el.innerHTML = `<div class="page">
    <div class="wb-hero">
      ${pageHead("现金位置 / 资金跑道",
        "你真正能动用的钱有多少、还能撑多久 — 基于你已对账的真实数据，每个数字可溯源。")}
      <button type="button" class="run-btn" id="cpRun">立即计算</button>
    </div>
    <div id="cpBanner"></div>
    <div class="card-grid">
      <div class="card" id="cpPos">${skeleton({ title: true, lines: 2 })}</div>
      <div class="card" id="cpCrunch">${skeleton({ title: true, lines: 2 })}</div>
      <div class="card" id="cpAssume">${skeleton({ title: true, lines: 2 })}</div>
    </div>
    <div class="card-grid">
      <div class="card" id="cpSolve" hidden></div>
      <div class="card" id="cpScenario" hidden></div>
    </div>
    <div class="card" id="cpEditor" hidden></div>
    <div class="card" id="cpProgress" hidden>
      <div class="recon-prog-title">现金位置推演中…</div>
      <div id="cpFeed" class="recon-feed"></div>
    </div>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  let pollTimer = null;

  const [setRes, histRes, plansRes] = await Promise.allSettled([
    api.getCashSettings(), api.getCashHistory(), api.getPlans(),
  ]);
  const settings = (setRes.status === "fulfilled" && setRes.value) || {};
  const history = (histRes.value && histRes.value.reports) || [];
  const plans = (plansRes.value && plansRes.value) || {};
  const curCode = (plans.current && plans.current.plan_code) || "free";
  const curRow = (plans.plans || []).find((p) => p.code === curCode) || {};
  const hasFeature = ((curRow.features) || []).includes("cash_position");

  const assume = settings.assumptions || {};
  const manual = settings.manual_inputs || {};
  const prefs = settings.prefs || {};
  const lags = assume.lags || {};
  const latest = history[0];
  const pos = (latest && latest.position) || {};
  const sum = (latest && latest.summary) || {};
  const crunch = latest && latest.crunch;
  const dc = (latest && latest.data_completeness) || {};

  // ---- data-completeness banner -------------------------------------------
  const missing = dc.missing || [];
  if (!hasFeature) {
    $("cpBanner").innerHTML = `<div class="audit-banner"><div class="ab-title">基础版及以上功能</div>
      <p>升级后即可基于你的对账数据看清「真正能动用多少、还能撑多久」。<a href="/account" data-nav>查看套餐 →</a></p></div>`;
  } else if (!latest) {
    $("cpBanner").innerHTML = `<div class="audit-banner"><div class="ab-title">先算一次</div>
      <p>设置好假设后点「立即计算」，即可得到你的现金位置与资金跑道。还没对账？
        <a href="/cross-border/recon" data-nav>先去回款核对 →</a></p></div>`;
  } else if (missing.length || dc.stale) {
    const items = (dc.stale ? [`对账数据可能已过期（数据截至 ${esc(dc.settlements_through || "未知")}）`] : [])
      .concat(missing.map(esc));
    $("cpBanner").innerHTML = `<div class="audit-banner"><div class="ab-title">数据完整度提示</div>
      <ul class="wb-files">${items.map((m) => `<li>· ${m}</li>`).join("")}</ul></div>`;
  }

  // ---- 可动用 vs 锁定 ------------------------------------------------------
  const locked = pos.locked_now || {};
  $("cpPos").innerHTML = latest ? `<h3 class="card-title">现在能动用</h3>
    <div class="rc-v" style="font-size:1.6rem">${fmtY(pos.available_now ?? sum["可动用现金"])}</div>
    <p class="muted">锁定 / 在途 <strong>${fmtY(locked.total ?? sum["锁定在途合计"])}</strong>
      （平台准备金 ${fmtY(locked.reserve)} · 在途回款 ${fmtY(locked.in_transit)}）；
      预计退税 ${fmtY((pos.expected || {}).rebate ?? sum["预计退税"])}。</p>
    <p class="muted">很多卖家高估了可动用现金 — 锁定/在途的钱暂时动不了。</p>
    <div class="wb-links"><a href="${esc(base)}/${esc(latest.run_id)}" data-nav>查看完整报告 →</a></div>`
    : `<h3 class="card-title">现在能动用</h3><p class="muted">还没有结果，点「立即计算」生成。</p>`;

  // ---- 最早现金紧张 -------------------------------------------------------
  $("cpCrunch").innerHTML = latest ? `<h3 class="card-title">最早现金紧张</h3>` + (crunch
    ? `<div class="rc-v" style="font-size:1.3rem">${esc(crunch.date)}</div>
       <p class="muted">届时可动用约 ${fmtY(crunch.available)}，低于现金缓冲 ${fmtY(sum["现金缓冲"])}，
         缺口 <strong>${fmtY(crunch.shortfall)}</strong>。</p>`
    : `<p class="muted">未来 ${esc(assume.horizon_weeks || 13)} 周内未跌破现金缓冲
        ${fmtY(sum["现金缓冲"])} ✓</p>`)
    : `<h3 class="card-title">最早现金紧张</h3><p class="muted">—</p>`;

  // ---- 关键假设 -----------------------------------------------------------
  $("cpAssume").innerHTML = `<h3 class="card-title">关键假设</h3>
    <p class="muted">现金缓冲 ${fmtY(assume.buffer_cny)} · 汇率 USD→CNY ${esc(assume.fx_rate || "7.20")}
      · 跑道 ${esc(assume.horizon_weeks || 13)} 周</p>
    <p class="muted">时滞(天)：到账 T+${esc(lags.settlement_lag_days ?? 7)} ·
      准备金释放 ${esc(lags.reserve_release_days ?? 14)} · 退税 ${esc(lags.rebate_lag_days ?? 60)}
      ${assume.provisional ? "<span class=\"chip\">默认值·建议核对</span>" : ""}</p>
    <div class="wb-card-cta"><button type="button" class="ghost-btn" id="cpEditBtn">设置假设与计划</button></div>`;

  // ---- inline 设置 editor -------------------------------------------------
  const work = {
    restock: Array.isArray(manual.restock) ? manual.restock.slice() : [],
    fixed_costs: Array.isArray(manual.fixed_costs) ? manual.fixed_costs.slice() : [],
  };
  function rowsHTML(items, cols) {
    return items.map((it, i) => `<div class="cp-row" data-i="${i}">${cols.map((c) =>
      `<input class="field-input cp-cell" data-k="${c.k}" type="${c.type || "text"}"
        value="${esc(it[c.k] ?? "")}" placeholder="${esc(c.ph)}">`).join("")}
      <button type="button" class="ghost-btn cp-del" data-i="${i}">删</button></div>`).join("");
  }
  function renderEditor() {
    const num = (k, label, val, ph) => `<label class="field"><span class="field-label">${esc(label)}</span>
      <input id="cp_${k}" class="field-input" type="number" step="any" value="${esc(val ?? "")}" placeholder="${esc(ph || "")}"></label>`;
    $("cpEditor").innerHTML = `<h3 class="card-title">设置假设与计划</h3>
      <p class="muted">下面的数字都是「假设」，可随时改 — 系统会基于你已对账的真实回款来推演。</p>
      <div class="card-grid">
        ${num("open_cny", "期初可动用·银行(¥)", (manual.opening_balances || {}).CNY, "如 80000")}
        ${num("open_usd", "期初可动用·收款服务商(US$)", (manual.opening_balances || {}).USD, "如 5000")}
        ${num("buffer", "现金缓冲(¥)", assume.buffer_cny, "低于此值即预警")}
        ${num("fx", "汇率 USD→CNY", assume.fx_rate, "如 7.20")}
        ${num("horizon", "跑道周数", assume.horizon_weeks, "如 13")}
        ${num("lag_settle", "到账时滞(天, T+)", lags.settlement_lag_days, "如 7")}
        ${num("lag_reserve", "准备金释放(天)", lags.reserve_release_days, "如 14")}
        ${num("lag_rebate", "退税到账(天)", lags.rebate_lag_days, "如 60")}
      </div>
      <h4 class="card-title" style="margin-top:1rem">计划备货 / 大额支出</h4>
      <p class="muted">填金额或「数量×单价」。可选「定金%」+「头程(天)」：按定金当天付、尾款到货付拆分现金流出。</p>
      <div id="cpRestock">${rowsHTML(work.restock, RESTOCK_COLS)}</div>
      <button type="button" class="ghost-btn" id="cpAddRestock">+ 添加备货/支出</button>
      <h4 class="card-title" style="margin-top:1rem">固定成本（按月）</h4>
      <div id="cpFixed">${rowsHTML(work.fixed_costs,
        [{ k: "label", ph: "说明，如 工资/房租" }, { k: "amount", ph: "月额", type: "number" },
         { k: "day_of_month", ph: "每月几号 5", type: "number" }])}</div>
      <button type="button" class="ghost-btn" id="cpAddFixed">+ 添加固定成本</button>
      <h4 class="card-title" style="margin-top:1rem">自动送达</h4>
      <label class="mi-check-row"><input type="checkbox" id="cp_active" ${prefs && settings.active ? "checked" : ""}>
        开启自动送达（基于下方节奏与渠道）</label>
      <label class="field"><span class="field-label">节奏</span>
        <select id="cp_cadence" class="field-input">
          <option value="weekly" ${(prefs.cadence || "weekly") === "weekly" ? "selected" : ""}>每周一</option>
          <option value="daily" ${prefs.cadence === "daily" ? "selected" : ""}>每天早 8 点</option>
        </select></label>
      <label class="mi-check-row"><input type="checkbox" id="cp_ch_email"
        ${(prefs.delivery_channels || ["email"]).includes("email") ? "checked" : ""}> 邮箱</label>
      <label class="mi-check-row"><input type="checkbox" id="cp_ch_wechat"
        ${(prefs.delivery_channels || []).includes("wechat") ? "checked" : ""}> 企业微信群机器人</label>
      <div class="report-actions">
        <button type="button" id="cpSave" class="run-btn">保存</button>
        <button type="button" id="cpCancel" class="ghost-btn">收起</button>
      </div>
      <p class="form-error" id="cpErr" hidden></p>
      <p class="muted" id="cpMsg" hidden></p>
      <p class="muted">即将推出：接入「库存补货」后，备货计划将自动生成、并提供现金循环周期(CCC)与库存周转分析（需库存数据）。</p>`;

    const bindRows = (hostId, arr, cols, addId) => {
      const host = $(hostId);
      host.addEventListener("input", (e) => {
        const cell = e.target.closest(".cp-cell"); if (!cell) return;
        const row = cell.closest(".cp-row"); const i = +row.dataset.i;
        arr[i] = arr[i] || {}; arr[i][cell.dataset.k] = cell.value;
      });
      host.addEventListener("click", (e) => {
        const del = e.target.closest(".cp-del"); if (!del) return;
        arr.splice(+del.dataset.i, 1); host.innerHTML = rowsHTML(arr, cols);
      });
      $(addId).addEventListener("click", () => { arr.push({}); host.innerHTML = rowsHTML(arr, cols); });
    };
    bindRows("cpRestock", work.restock, RESTOCK_COLS, "cpAddRestock");
    bindRows("cpFixed", work.fixed_costs,
      [{ k: "label", ph: "说明" }, { k: "amount", ph: "月额", type: "number" },
       { k: "day_of_month", ph: "几号", type: "number" }], "cpAddFixed");
    $("cpSave").addEventListener("click", saveSettings);
    $("cpCancel").addEventListener("click", () => { $("cpEditor").hidden = true; });
  }
  function toggleEditor(open) {
    $("cpEditor").hidden = !open;
    if (open) { renderEditor(); $("cpEditor").scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
  const numv = (id) => { const v = ($(id).value || "").trim(); return v === "" ? null : Number(v); };
  function collect() {
    const channels = [];
    if ($("cp_ch_email").checked) channels.push("email");
    if ($("cp_ch_wechat").checked) channels.push("wechat");
    const clean = (arr) => arr.filter((it) => it && (it.amount || it.label));
    return {
      assumptions: {
        buffer_cny: numv("cp_buffer"), fx_rate: numv("cp_fx"),
        horizon_weeks: numv("cp_horizon"),
        lags: {
          settlement_lag_days: numv("cp_lag_settle"),
          reserve_release_days: numv("cp_lag_reserve"),
          rebate_lag_days: numv("cp_lag_rebate"),
        },
      },
      manual_inputs: {
        opening_balances: { CNY: numv("cp_open_cny") || 0, USD: numv("cp_open_usd") || 0 },
        restock: clean(work.restock), fixed_costs: clean(work.fixed_costs),
      },
      prefs: { cadence: $("cp_cadence").value, delivery_channels: channels },
      active: $("cp_active").checked,
    };
  }
  async function saveSettings() {
    $("cpErr").hidden = true; $("cpMsg").hidden = true;
    const btn = $("cpSave"); btn.disabled = true; btn.textContent = "保存中…";
    try {
      await api.saveCashSettings(collect());
      $("cpMsg").hidden = false; $("cpMsg").textContent = "已保存 ✓ 点「立即计算」查看更新后的现金位置。";
    } catch (ex) { $("cpErr").hidden = false; $("cpErr").textContent = (ex.data && ex.data.error) || ex.message; }
    btn.disabled = false; btn.textContent = "保存";
  }
  $("cpEditBtn").addEventListener("click", () => toggleEditor($("cpEditor").hidden));
  if (hasFeature && !latest) toggleEditor(true);   // first-time: open the editor

  // ---- 立即计算 -----------------------------------------------------------
  $("cpRun").addEventListener("click", async () => {
    if (!hasFeature) { ctx.navigate("/account"); return; }
    const btn = $("cpRun"); btn.disabled = true; btn.textContent = "计算中…";
    try {
      const res = await api.startCash();
      $("cpProgress").hidden = false;
      pollTimer = pollJobStatus(res.job_id, $("cpFeed"), async (ok) => {
        if (!ok) { btn.disabled = false; btn.textContent = "重新计算"; return; }
        let runId = res.run_id;
        if (!runId) {
          try { const h = await api.getCashHistory(); runId = ((h.reports || [])[0] || {}).run_id || null; }
          catch (_) { /* handled below */ }
        }
        if (runId) { ctx.navigate(`${base}/${runId}`); return; }
        $("cpProgress").insertAdjacentHTML("beforeend",
          `<p class="muted">✓ 已生成，请刷新查看。</p>`);
        btn.disabled = false; btn.textContent = "立即计算";
      }, { errorText: "计算失败。" });
    } catch (ex) {
      btn.disabled = false; btn.textContent = "立即计算";
      alert((ex.data && ex.data.error) || ex.message);
    }
  });

  // ---- Phase 2: interactive affordability solver + scenarios (synchronous, no run) -------
  if (hasFeature) renderInteractive();

  function renderInteractive() {
    const solve = $("cpSolve"); const scen = $("cpScenario");
    solve.hidden = false; scen.hidden = false;
    solve.innerHTML = `<h3 class="card-title">能否备货？</h3>
      <p class="muted">基于已承诺的资金跑道，算出最多可一次性投入的备货金额，并校验某笔备货是否安全。</p>
      <div class="rc-k">本周起最多可安全备货</div>
      <div class="rc-v" id="cpMaxSafe" style="font-size:1.4rem">计算中…</div>
      <div id="cpMaxNote" class="muted"></div>
      <label class="field"><span class="field-label">这笔备货金额(¥)</span>
        <input id="cpPoAmt" class="field-input" type="number" step="any" placeholder="如 120000"></label>
      <label class="field"><span class="field-label">付款日期</span>
        <input id="cpPoDate" class="field-input" type="date"></label>
      <div class="report-actions"><button type="button" id="cpAfford" class="run-btn">能否承受？</button></div>
      <div id="cpAffordOut" class="muted"></div>`;
    scen.innerHTML = `<h3 class="card-title">压力测试 / 情景</h3>
      <p class="muted">测试不利情况对资金跑道的影响（已承诺部分确定，销售为低置信度预测）。</p>
      <label class="field"><span class="field-label">销售变化(%)</span>
        <input id="cpLvSales" class="field-input" type="number" step="any" value="-20"></label>
      <label class="field"><span class="field-label">退税延迟(天)</span>
        <input id="cpLvRebate" class="field-input" type="number" step="1" value="30"></label>
      <label class="field"><span class="field-label">备货推迟(周)</span>
        <input id="cpLvPo" class="field-input" type="number" step="1" value="0"></label>
      <div class="report-actions"><button type="button" id="cpScenBtn" class="run-btn">运行压力测试</button></div>
      <div id="cpScenOut" class="muted"></div>`;

    // auto-load the max-safe figure
    api.cashSolve({}).then((r) => {
      const ms = r.max_safe || {};
      $("cpMaxSafe").textContent = fmtY(ms.max_amount);
      $("cpMaxNote").textContent = ms.binding_date
        ? `受限于 ${ms.binding_date}（届时余额最接近缓冲 ${fmtY(ms.buffer)}）。`
        : "";
    }).catch((e) => { $("cpMaxSafe").textContent = "—"; });

    $("cpAfford").addEventListener("click", async () => {
      const amount = Number(($("cpPoAmt").value || "").trim());
      const date = ($("cpPoDate").value || "").trim();
      const out = $("cpAffordOut");
      if (!amount || !date) { out.textContent = "请填写金额与付款日期。"; return; }
      out.textContent = "测算中…";
      try {
        const r = await api.cashSolve({ po: { amount, currency: "CNY", date } });
        const a = r.affordability || {};
        out.innerHTML = a.affordable
          ? `<span class="chip">可承受 ✓</span> 之后最低余额 ${fmtY(a.min_balance_after)}（不低于缓冲 ${fmtY(a.buffer)}）。`
          : `<span class="chip">有风险 ⚠️</span> 将有 ${a.weeks_below_buffer} 周低于缓冲，最早 ${esc(a.first_breach_date || "")}；之后最低余额 ${fmtY(a.min_balance_after)}。`;
      } catch (e) { out.textContent = (e.data && e.data.error) || e.message; }
    });

    $("cpScenBtn").addEventListener("click", async () => {
      const out = $("cpScenOut"); out.textContent = "运行中…";
      const levers = {
        sales_delta_pct: Number(($("cpLvSales").value || "0").trim()),
        rebate_delay_days: Number(($("cpLvRebate").value || "0").trim()),
        po_delay_weeks: Number(($("cpLvPo").value || "0").trim()),
      };
      try {
        const r = await api.cashScenario(levers);
        const bc = r.baseline.crunch, sc = r.scenario.crunch;
        const fc = r.forecast_end;
        out.innerHTML = `基准最低余额 ${fmtY(r.baseline.min_balance)}（现金紧张 ${bc ? esc(bc.date) : "无"}）<br>
          情景最低余额 <strong>${fmtY(r.scenario.min_balance)}</strong>（现金紧张 ${sc ? esc(sc.date) : "无"}）
          ，变化 ${fmtY(r.min_balance_delta)}
          ${fc ? `<br><span class="muted">预测期末（低置信度）：基准 ${fmtY(fc.baseline)} → 情景 ${fmtY(fc.scenario)}</span>` : ""}`;
      } catch (e) { out.textContent = (e.data && e.data.error) || e.message; }
    });
  }

  const onGo = (e) => { const go = e.target.closest("[data-go]"); if (go) ctx.navigate(go.dataset.go); };
  el.addEventListener("click", onGo);
  return () => { if (pollTimer) clearInterval(pollTimer); el.removeEventListener("click", onGo); };
}

// ---- result view ----------------------------------------------------------
async function mountResult(el, ctx, runId, base) {
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 6 })}</div></div>`;
  let r;
  try { r = await api.getCashResult(runId); } catch (e) {
    el.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">💰</div>
        <div class="empty-title">现金位置 #${esc(runId)}</div>
        <p class="empty-lead">${esc((e.data && e.data.error) || e.message)}</p>
        <button type="button" class="ghost-btn" id="cpBack">返回</button>
      </div></div>`;
    const b = el.querySelector("#cpBack");
    if (b) b.addEventListener("click", () => ctx.navigate(base));
    return;
  }

  const sum = r.summary || {};
  const pos = r.position || {};
  const locked = pos.locked_now || {};
  const dc = r.data_completeness || {};
  const weekly = (r.runway || {}).weekly || [];

  const cards = [
    ["可动用现金", fmtY(pos.available_now ?? sum["可动用现金"])],
    ["锁定 / 在途", fmtY(locked.total ?? sum["锁定在途合计"])],
    ["预计退税", fmtY((pos.expected || {}).rebate ?? sum["预计退税"])],
    ["最早现金紧张", r.crunch ? esc(r.crunch.date) : "无"],
  ].map(([k, v]) =>
    `<div class="recon-card"><div class="rc-k">${esc(k)}</div><div class="rc-v">${v}</div></div>`).join("");

  const dcMissing = (dc.missing || []);
  const dcBanner = (dc.stale || dcMissing.length)
    ? `<div class="audit-banner"><div class="ab-title">数据完整度提示</div>
       <ul class="wb-files">${(dc.stale ? [`对账数据可能已过期（数据截至 ${esc(dc.settlements_through || "未知")}）`] : [])
        .concat(dcMissing.map(esc)).map((m) => `<li>· ${m}</li>`).join("")}</ul></div>`
    : "";

  const rows = weekly.map((w) =>
    `<tr><td>第${esc(w.week + 1)}周<br><span class="muted">${esc(w.start)}</span></td>
      <td class="${Number(w.available) < 0 ? "cp-neg" : ""}">${fmtY(w.available)}</td>
      <td>${fmtY(w.committed_in)}</td><td>${fmtY(w.out)}</td>
      <td class="muted">${fmtY(w.locked)}</td></tr>`).join("");

  el.innerHTML = `<div class="page page-narrow recon-result">
    <header class="page-head">
      <a class="back-link" href="${esc(base)}" data-nav>← 现金位置 / 资金跑道</a>
      <h1>现金位置与资金跑道</h1>
      <p class="muted">${esc(sum.period_label || dc.settlements_through || "")} · ${statusPillHTML(r.status)}</p>
    </header>
    <div class="recon-cards">${cards}</div>
    ${dcBanner}
    <div class="card recon-report-body">${r.final_report
      ? renderMarkdown(r.final_report, { anchors: true })
      : `<p class="muted">报告正文尚未生成。</p>`}
    </div>
    <h3 class="card-title">资金跑道（按周）</h3>
    <div class="card" style="overflow:auto">
      <table class="data-table cp-runway">
        <thead><tr><th>周</th><th>周末可动用</th><th>本周流入</th><th>本周流出</th><th>仍锁定</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="muted">暂无</td></tr>`}</tbody>
      </table>
      <p class="muted">「流入」为已承诺/在途的回款与退税（确定性）；销售预测驱动的远期区间为后续版本提供。</p>
    </div>
    <div class="report-actions">
      ${r.download_url ? `<a class="run-btn dl-btn" href="${esc(r.download_url)}">⬇ 下载 PDF</a>` : ""}
    </div>
  </div>`;
}
