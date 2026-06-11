// ===========================================================================
// workbench.js — 工作台 (logged-in home, plan §3.2). Cards: ① 下次送达 (live
// countdown to the earliest active job's next_run_at — or the setup/upgrade
// CTA per D3), ② 最新报告, ③ 本月额度 quota meter, ④ 数据收件箱 mini card,
// ⑤ 企业大脑 teaser (live stats arrive with B6 in Phase 6), ⑥ the primary
// action 立即生成报告. The countdown timer is returned as the mount cleanup.
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, pageHead, quotaMeter, statusPillHTML } from "../components.js";
import { openOnboarding } from "./onboarding.js";
import { cronLabel } from "./schedule.js";

function reportTitle(goalText) {
  const templates = (state.meta && state.meta.report_goal_templates) || [];
  const t = templates.find((x) => x.goal === goalText);
  return t ? t.label : "经营分析报告";
}

function fmtDateCN(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "";
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function fmtCountdown(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  const d = Math.floor(s / 86400);
  const rest = `${pad(Math.floor((s % 86400) / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  return d > 0 ? `${d} 天 ${rest}` : rest;
}

const channelLabel = (c) => ({ email: "邮箱", wechat: "企业微信", service_account: "服务号" }[c] || c);

export function mount(el, ctx) {
  const name = (state.account && state.account.name) || "";
  el.innerHTML = `<div class="page">
    <div class="wb-hero">
      ${pageHead(`你好，${name}`, "你的 AI 经营分析团队，随时待命。")}
      <button type="button" class="run-btn" id="wbNew">立即生成报告</button>
    </div>
    <div class="card-grid">
      <div class="card" id="wbNext"><h3 class="card-title">⏰ 下次送达</h3><p class="muted">正在载入…</p></div>
      <div class="card" id="wbLatest"><h3 class="card-title">📄 最新报告</h3><p class="muted">正在载入…</p></div>
      <div class="card" id="wbQuota"><h3 class="card-title">📊 本月额度</h3><p class="muted">正在载入…</p></div>
      <div class="card" id="wbInbox"><h3 class="card-title">📂 数据收件箱</h3><p class="muted">正在载入…</p></div>
      <div class="card" id="wbBrain">
        <h3 class="card-title">🧠 企业大脑</h3>
        <p class="muted">它记住你的公司：指标、口径、术语、偏好。每份报告都让它更聪明 — 已记住的指标与趋势统计即将在这里展示。</p>
        <div class="wb-card-cta"><button type="button" class="ghost-btn" id="wbBrainGo">打开企业大脑</button></div>
      </div>
    </div>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  $("wbNew").addEventListener("click", () => ctx.navigate("/new"));
  $("wbBrainGo").addEventListener("click", () => ctx.navigate("/brain"));
  el.addEventListener("click", (e) => {
    const go = e.target.closest("[data-go]");
    if (go) ctx.navigate(go.dataset.go);
  });

  let timer = null;

  // ---- ① 下次送达 — live countdown ------------------------------------------
  (async () => {
    let plansData = null;
    let jobs = [];
    try { plansData = await api.getPlans(); } catch (e) { /* treated as free below */ }
    const cur = (plansData && plansData.current) || {};
    const planRow = ((plansData && plansData.plans) || []).find((p) => p.code === cur.plan_code) || {};
    const canSchedule = ((planRow.features) || []).includes("scheduled_delivery");
    if (canSchedule) {
      try { jobs = (await api.getScheduledJobs()).jobs || []; } catch (e) { /* empty */ }
    }
    const box = $("wbNext");
    if (!box || !box.isConnected) return;

    const upcoming = jobs
      .filter((j) => j.active && j.next_run_at && !isNaN(new Date(j.next_run_at)))
      .sort((a, b) => new Date(a.next_run_at) - new Date(b.next_run_at))[0];

    if (upcoming) {
      const chans = (upcoming.delivery_channels || []).map(channelLabel).join(" + ") || "—";
      box.innerHTML = `<h3 class="card-title">⏰ 下次送达</h3>
        <div class="wb-count-val" id="wbCount">--:--:--</div>
        <div class="wb-count-sub">${esc(cronLabel(upcoming.cron_expr))} → ${esc(chans)}</div>
        <div class="wb-links"><a href="/schedule" data-nav>管理定时与送达 →</a></div>`;
      const target = new Date(upcoming.next_run_at);
      const tick = () => {
        const n = el.querySelector("#wbCount");
        if (!n) { clearInterval(timer); timer = null; return; }
        const left = target - Date.now();
        n.textContent = left <= 0 ? "正在生成…" : fmtCountdown(left);
      };
      tick();
      timer = setInterval(tick, 1000);
    } else if (canSchedule) {
      box.innerHTML = `<h3 class="card-title">⏰ 下次送达</h3>
        <p class="muted">还没有定时任务。两步设置「每周一 08:00」，之后报告自己到。</p>
        <div class="wb-card-cta"><button type="button" class="ghost-btn" data-go="/schedule">去开启每周自动送达</button></div>`;
    } else {
      box.innerHTML = `<h3 class="card-title">⏰ 下次送达</h3>
        <p class="muted">升级到企业版后，每周一早上 8 点报告自动送到企业微信和邮箱 — 你什么都不用做。</p>
        <div class="wb-card-cta"><button type="button" class="ghost-btn" data-go="/account">查看套餐与升级</button></div>`;
    }
  })();

  // ---- ② 最新报告 -------------------------------------------------------------
  (async () => {
    let reports = [];
    try { reports = (await api.getReports()).reports || []; } catch (e) { /* empty */ }
    const box = $("wbLatest");
    if (!box || !box.isConnected) return;
    const r = reports[0];
    if (!r) {
      box.innerHTML = `<h3 class="card-title">📄 最新报告</h3>
        <p class="muted">还没有报告。跟着新手引导走（可用示例数据），几分钟后第一份就出来了。</p>
        <div class="wb-card-cta"><button type="button" class="ghost-btn" id="wbOnboard">打开新手引导</button></div>`;
      const btn = box.querySelector("#wbOnboard");
      if (btn) btn.addEventListener("click", () => openOnboarding());
      return;
    }
    const links = [];
    if (r.has_content) links.push(`<a href="/reports/${esc(r.id)}" data-nav>查看 →</a>`);
    if (r.download_url) links.push(`<a href="${esc(r.download_url)}">下载 PDF</a>`);
    links.push(`<a href="/reports" data-nav>全部历史 →</a>`);
    box.innerHTML = `<h3 class="card-title">📄 最新报告</h3>
      <div class="wb-report-title">${esc(reportTitle(r.goal_text))}</div>
      <div class="wb-report-meta"><span>${esc(fmtDateCN(r.started_at))}</span>${statusPillHTML(r.status)}</div>
      <div class="wb-links">${links.join("")}</div>`;
  })();

  // ---- ③ 本月额度 -------------------------------------------------------------
  (async () => {
    let cur = null;
    try { cur = (await api.getPlans()).current; } catch (e) { /* hidden */ }
    const box = $("wbQuota");
    if (!box || !box.isConnected) return;
    if (!cur || !cur.monthly_report_quota) {
      box.innerHTML = `<h3 class="card-title">📊 本月额度</h3><p class="muted">额度信息暂不可用。</p>`;
      return;
    }
    const used = cur.reports_used_this_month || 0;
    const total = cur.monthly_report_quota;
    let extra = "";
    let caption = "本月已生成";
    if (cur.lifetime_quota) {
      caption = "免费体验额度";
      extra = `<p class="muted" style="margin:6px 0 0;">剩余体验 ${Math.max(0, total - used)} 份报告。</p>`;
    }
    box.innerHTML = `<h3 class="card-title">📊 本月额度</h3>
      ${quotaMeter({ used, total, caption })}${extra}
      <div class="wb-links"><a href="/account" data-nav>套餐与购买 →</a></div>`;
  })();

  // ---- ④ 数据收件箱 mini card ---------------------------------------------------
  (async () => {
    let inbox = null;
    try { inbox = await api.getUploadsLatest(); } catch (e) { /* empty */ }
    const box = $("wbInbox");
    if (!box || !box.isConnected) return;
    const files = (inbox && inbox.files) || [];
    if (!files.length) {
      box.innerHTML = `<h3 class="card-title">📂 数据收件箱</h3>
        <p class="muted">收件箱是空的。把最近的经营数据放进来，下次报告（含定时任务）就会用上。</p>
        <div class="wb-card-cta"><button type="button" class="ghost-btn" data-go="/schedule">去添加数据</button></div>`;
      return;
    }
    const shown = files.slice(0, 4);
    box.innerHTML = `<h3 class="card-title">📂 数据收件箱</h3>
      <p class="muted" style="margin:4px 0 0;">下次报告将使用 ${files.length} 个文件：</p>
      <ul class="wb-files">${shown.map((f) => `<li>· ${esc(f.name)}</li>`).join("")}${files.length > shown.length ? `<li>…等 ${files.length} 个</li>` : ""}</ul>
      <div class="wb-links"><a href="/schedule" data-nav>管理数据收件箱 →</a></div>`;
  })();

  return () => { if (timer) clearInterval(timer); };
}
