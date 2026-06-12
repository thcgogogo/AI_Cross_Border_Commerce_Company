// ===========================================================================
// reports.js — 历史报告 (plan §3.5). Every run grouped by month with a type
// badge (周报/月报/自定义/历史导入), an extended status pill, the web report
// deep link (/reports/<id>) and the PDF download. Rows without web content
// (has_content false) are not links — PDF-only or plain. The empty state
// sells the Monday-morning schedule.
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, pageHead, skeletonCard, statusPillHTML } from "../components.js";
import { renderErrorState } from "../errors.js";

function typeBadge(r) {
  const templates = (state.meta && state.meta.report_goal_templates) || [];
  const t = templates.find((x) => x.goal === r.goal_text);
  if (t) return t.code === "monthly" ? ["月报", "rep-badge-monthly"] : ["周报", "rep-badge-weekly"];
  if (!r.has_content) return ["历史导入", "rep-badge-import"];
  return ["自定义", "rep-badge-custom"];
}

function monthKey(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "更早";
  return `${d.getFullYear()}年${d.getMonth() + 1}月`;
}

function dayLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function rowHTML(r) {
  const [badge, badgeCls] = typeBadge(r);
  const viewable = !!r.has_content;
  const title = badge === "历史导入" ? "历史数据导入" : `经营${badge === "月报" ? "月报" : badge === "周报" ? "周报" : "分析报告"}`;
  return `<div class="rep-row ${viewable ? "viewable" : ""}" ${viewable ? `data-id="${esc(r.id)}" role="link" tabindex="0"` : ""}>
      <div class="rep-main">
        <span class="rep-title">${esc(title)}</span>
        <span class="rep-badge ${badgeCls}">${esc(badge)}</span>
      </div>
      <span class="rep-date">${esc(dayLabel(r.started_at))}</span>
      ${statusPillHTML(r.status)}
      <div class="rep-actions">
        ${viewable ? `<a href="/reports/${esc(r.id)}" data-nav class="rep-act">查看</a>` : `<span class="rep-act muted-act">仅 PDF</span>`}
        ${r.download_url ? `<a href="${esc(r.download_url)}" class="rep-act">PDF</a>` : ""}
      </div>
    </div>`;
}

export async function mount(el, ctx) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("历史报告", "每一份报告都按月份归档：在线查看、下载 PDF、转发到企业微信。")}
    <div id="repList">${skeletonCard({ lines: 4 })}</div>
  </div>`;
  const list = el.querySelector("#repList");

  let reports = [];
  try {
    reports = (await api.getReports()).reports || [];
  } catch (e) {
    // Error catalogue (B2): 401 → login modal, offline → 重试, else error+重试
    // — a raw "请先登录" paragraph stranded the user here before.
    renderErrorState(list, e, () => mount(el, ctx));
    return;
  }
  if (!list.isConnected) return;

  if (!reports.length) {
    list.innerHTML = `<div class="empty">
        <div class="empty-icon">🗂️</div>
        <div class="empty-title">还没有报告</div>
        <p class="empty-lead">生成第一份报告，然后开启定时任务 — 每周一早上 8 点，经营周报会自动送到企业微信和邮箱，你什么都不用做。</p>
        <button type="button" class="run-btn" id="repNew">立即生成第一份报告</button>
      </div>`;
    list.querySelector("#repNew").addEventListener("click", () => ctx.navigate("/new"));
    return;
  }

  // Group by month, newest first (the API already returns newest-first rows).
  const groups = [];
  const byMonth = {};
  for (const r of reports) {
    const k = monthKey(r.started_at);
    if (!byMonth[k]) { byMonth[k] = []; groups.push(k); }
    byMonth[k].push(r);
  }

  list.innerHTML = groups.map((k) => `
    <div class="rep-month">
      <h3 class="rep-month-h">${esc(k)}</h3>
      <div class="card rep-card">${byMonth[k].map(rowHTML).join("")}</div>
    </div>`).join("");

  // Whole-row click/keyboard → the web report (links inside keep their own behavior).
  list.addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const row = e.target.closest(".rep-row.viewable");
    if (row) ctx.navigate(`/reports/${row.dataset.id}`);
  });
  list.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const row = e.target.closest(".rep-row.viewable");
    if (row) ctx.navigate(`/reports/${row.dataset.id}`);
  });
}
