// ===========================================================================
// components.js — shared HTML builders + formatters (ported from app.js).
// Pure functions returning HTML strings; views wire their own listeners.
// ===========================================================================
import { state } from "./state.js";

// ---- escapes / formatters ---------------------------------------------------
export function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

export function fmtNum(n) {
  n = Number(n || 0);
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n % 1_000 === 0 ? 0 : 1) + "K";
  return String(n);
}

export function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    return "";
  }
}

// ¥ formatter — the symbol comes from /api/meta (falls back to "¥").
export function cny(n, dp = 2) {
  if (n == null || isNaN(n)) return "—";
  const sym = (state.meta && state.meta.currency_symbol) || "¥";
  return `${sym}${Number(n).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
}

// ---- tiny, SAFE markdown renderer (escape first, then format) ----------------
// Phase 4 additions, still escape-first: GFM tables (reports carry them — the
// wrap div lets them scroll horizontally on a phone), safe [text](url) links,
// and — opt-in via `opts.anchors` — the report's 【N】 source anchors rendered
// as tappable chips wired by the report view.
export function renderMarkdown(md, opts = {}) {
  const lines = esc(md).split("\n");
  let html = "";
  let listType = null;
  let tableRows = null;
  const closeList = () => {
    if (listType) { html += `</${listType}>`; listType = null; }
  };
  const flushTable = () => {
    if (tableRows) { html += tableHTML(tableRows, opts); tableRows = null; }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*\|.*\|\s*$/.test(line)) {
      closeList();
      (tableRows = tableRows || []).push(line.trim());
      continue;
    }
    flushTable();
    if (/^\s*#{1,6}\s+/.test(line)) {
      closeList();
      const level = line.match(/^\s*(#{1,6})/)[1].length;
      html += `<h${level}>${inline(line.replace(/^\s*#{1,6}\s+/, ""), opts)}</h${level}>`;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ""), opts)}</li>`;
    } else if (/^\s*\d+\.\s+/.test(line)) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inline(line.replace(/^\s*\d+\.\s+/, ""), opts)}</li>`;
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      html += `<p>${inline(line, opts)}</p>`;
    }
  }
  closeList();
  flushTable();
  return html;
}

// "| a | b |" → ["a", "b"] (cells keep their inline markdown for inline()).
function splitTableRow(row) {
  return row.replace(/^\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
}

const isSeparatorRow = (cells) =>
  cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(c));

function tableHTML(rows, opts) {
  const parsed = rows.map(splitTableRow);
  let header = null;
  let body = parsed;
  if (parsed.length >= 2 && isSeparatorRow(parsed[1])) {
    header = parsed[0];
    body = parsed.slice(2).filter((r) => !isSeparatorRow(r));
  }
  const cells = (tag, r) => r.map((c) => `<${tag}>${inline(c, opts)}</${tag}>`).join("");
  let html = '<div class="md-table-wrap"><table class="md-table">';
  if (header) html += `<thead><tr>${cells("th", header)}</tr></thead>`;
  html += `<tbody>${body.map((r) => `<tr>${cells("td", r)}</tr>`).join("")}</tbody>`;
  return html + "</table></div>";
}

function inline(s, opts = {}) {
  s = s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>")
    .replace(/(^|[^*])\*([^*]+?)\*(?!\*)/g, "$1<em>$2</em>")
    // Links: the text/href are already HTML-escaped (escape-first); only
    // http(s)/mailto/relative/in-page destinations become real links.
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, href) => {
      if (!/^(https?:\/\/|mailto:|\/|#)/i.test(href)) return m;
      return `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    });
  if (opts.anchors) {
    s = s.replace(/【\s*(\d+)\s*】/g,
      '<button type="button" class="anchor-chip" data-anchor="$1">【$1】</button>');
  }
  return s;
}

// ---- status pill (Chinese-first) ---------------------------------------------
// Covers both live job snapshots (starting/running/done/error) and the DB run
// statuses the 历史报告 rows carry (running/succeeded/failed/stopped_budget).
export function statusPillHTML(status) {
  const map = {
    starting: ["启动中…", "pill-running"],
    running: ["生成中…", "pill-running"],
    done: ["已完成", "pill-done"],
    error: ["出错", "pill-error"],
    succeeded: ["已完成", "pill-done"],
    failed: ["生成失败", "pill-error"],
    stopped_budget: ["已中止", "pill-idle"],
  };
  const [label, cls] = map[status] || ["待开始", "pill-idle"];
  return `<span class="pill ${cls}">${label}</span>`;
}

// ---- live feed row -------------------------------------------------------------
// Customer-facing display name for a feed event's author: the Chinese role name
// (name_cn from config) when the org chart is loaded, else the raw name.
export function whoName(id) {
  if (id === "system") return "系统";
  if (id === "token_manager") return "成本管家";
  const a = state.agents.find((x) => x.id === id);
  if (!a) return id;
  return a.name_cn || `${a.name} · ${a.title}`;
}

export function feedRowHTML(a) {
  return `<li class="feed-item ${esc(a.type || "info")}">
      <span class="feed-badge"></span>
      <div>
        <span class="feed-who">${esc(whoName(a.agent))}</span>
        <div class="feed-msg">${esc(a.message)}</div>
      </div>
      <span class="feed-time">${fmtTime(a.ts)}</span>
    </li>`;
}

// ---- page scaffolding ------------------------------------------------------------
export function pageHead(title, lead, badge) {
  return `<h1 class="page-h">${esc(title)}${badge ? `<span class="soon-badge">${esc(badge)}</span>` : ""}</h1>
    ${lead ? `<p class="page-lead">${esc(lead)}</p>` : ""}`;
}

// A friendly “即将上线” block for pages whose full experience lands in a later
// phase. `points` previews what the page will do; `cta` is {label, id} for an
// optional action button the view wires itself.
export function comingSoon({ icon = "🛠️", title, lead, points = [], cta = null }) {
  return `<div class="empty">
      <div class="empty-icon">${icon}</div>
      <div class="empty-title">${esc(title)}</div>
      <p class="empty-lead">${esc(lead)}</p>
      ${points.length ? `<ul class="empty-points">${points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
      ${cta ? `<button type="button" id="${esc(cta.id)}" class="run-btn">${esc(cta.label)}</button>` : ""}
    </div>`;
}

// ---- loading skeleton (Phase 10 B3) ------------------------------------------------
// Shimmering placeholder lines, so content settles into the page instead of
// popping in after a "正在载入…" string. Width pattern repeats but staggers,
// so a stack of lines reads as text. `title` adds a wider, taller first bar.
export function skeleton({ lines = 3, title = false } = {}) {
  const widths = [92, 64, 78, 55, 84];
  return `<div class="skeleton" aria-hidden="true">
      ${title ? '<div class="skel skel-title"></div>' : ""}
      ${Array.from({ length: lines }, (_, i) =>
        `<div class="skel skel-line" style="width:${widths[i % widths.length]}%"></div>`).join("")}
    </div>`;
}

// A whole placeholder card (the common case for page-level loads).
export function skeletonCard(opts = {}) {
  return `<div class="card">${skeleton(opts)}</div>`;
}

// ---- stat tile + quota meter (design-system atoms, used from Phase 5 on) ----------
export function statTile({ value, delta = null, label }) {
  let deltaHTML = "";
  if (delta != null && !isNaN(delta)) {
    const up = Number(delta) >= 0;
    deltaHTML = `<span class="stat-delta ${up ? "up" : "down"}">${up ? "↑" : "↓"} ${esc(Math.abs(Number(delta)))}%</span>`;
  }
  return `<div class="stat">
      <span class="stat-val">${esc(value)}</span>
      ${deltaHTML}
      <span class="stat-label">${esc(label)}</span>
    </div>`;
}

export function quotaMeter({ used, total, caption = "本月已生成" }) {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const cls = pct >= 100 ? "exceeded" : pct >= 80 ? "warning" : "ok";
  return `<div class="quota">
      <div class="quota-caption"><span>${esc(caption)}</span><strong>${esc(used)} / ${esc(total)} 份</strong></div>
      <div class="meter"><div class="meter-fill ${cls}" style="width:${pct}%"></div></div>
    </div>`;
}

// Inline SVG sparkline from a numeric series (企业大脑 KPI tiles, B6). Flat
// single-color polyline, no axes/labels — the tile text carries the numbers.
export function sparklineSVG(values, { width = 120, height = 34, stroke = "var(--accent)" } = {}) {
  // Number.isFinite, not !isNaN: an Infinity that slipped through would make
  // every other point's y-coordinate NaN (span = Infinity) and kill the SVG.
  const nums = (values || []).map(Number).filter((v) => Number.isFinite(v));
  if (nums.length < 2) return "";
  const min = Math.min(...nums);
  const span = (Math.max(...nums) - min) || 1;
  const pad = 2;
  const step = (width - pad * 2) / (nums.length - 1);
  const pts = nums.map((v, i) =>
    `${(pad + i * step).toFixed(1)},${(height - pad - ((v - min) / span) * (height - pad * 2)).toFixed(1)}`).join(" ");
  return `<svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    </svg>`;
}

// ---- flat pricing cards (Phase 7 D2 — shared by landing §3.1⑦ + account §3.8②)
// Per-plan marketing blurbs; labels/prices/quotas always come from /api/plans.
// (planCard already renders the quota as the first point — pitches must not
// repeat it, Phase 10 polish.)
export const PLAN_PITCH = {
  free:  { sub: "先看两份自己的报告再决定", points: ["完整报告体验（含品牌 PDF）", "手机号注册即可，无需绑卡", "历史数据导入体验"] },
  basic: { sub: "每周一报，覆盖全月经营", points: ["1 个使用账号", "企业大脑：记住指标与口径", "邮件送达 + 网页查看"] },
  pro:   { sub: "整个团队的 AI 经营分析部", points: ["5 个使用账号", "每周一 08:00 定时自动送达", "企业微信群直达", "合同审查（每月 3 份）", "优先生成通道"] },
};

// One flat plan card. `p` is an /api/plans row; opts:
//   featured — the 最受欢迎 ribbon;  current — marks 当前套餐;
//   cta — {label, attrs} for the action button (attrs go on the <button>
//   verbatim, e.g. `data-signup` or `data-plan="basic"`); null → no button.
export function planCard(p, { featured = false, current = false, cta = null } = {}) {
  if (!p) return "";
  const pitch = PLAN_PITCH[p.code] || { sub: "", points: [] };
  const quota = p.lifetime_quota
    ? `共 ${p.monthly_report_quota} 份报告`
    : `每月 ${p.monthly_report_quota} 份报告额度`;
  return `<div class="card plan-card ${featured ? "featured" : ""} ${current ? "current" : ""}">
    ${featured ? '<span class="plan-flag">最受欢迎</span>' : ""}
    ${current ? '<span class="plan-flag plan-flag-current">当前套餐</span>' : ""}
    <div class="plan-name">${esc(p.label)}</div>
    <div class="plan-price">${p.monthly_price_yuan > 0
      ? `${esc(cny(p.monthly_price_yuan, 0))}<span class="plan-per">/月</span>` : "¥0"}</div>
    <div class="plan-sub">${esc(pitch.sub)}</div>
    <ul class="plan-points">
      <li>${esc(quota)}</li>
      ${pitch.points.map((x) => `<li>${esc(x)}</li>`).join("")}
    </ul>
    ${cta ? `<button type="button" class="run-btn plan-cta" ${cta.attrs || ""}>${esc(cta.label)}</button>` : ""}
  </div>`;
}

// The 单次报告 card — same flat-card shape; price from /api/meta
// (single_report_price_yuan), purchasable via payCreate({type:"single_report"}).
export function singleReportCard(price, { cta = null } = {}) {
  if (!price) return "";
  return `<div class="card plan-card">
    <div class="plan-name">单次报告</div>
    <div class="plan-price">${esc(cny(price, 0))}<span class="plan-per">/份</span></div>
    <div class="plan-sub">不订阅，按需购买</div>
    <ul class="plan-points"><li>一份完整报告（含 PDF）</li><li>同样的溯源与核验标准</li><li>随时升级为订阅</li></ul>
    ${cta ? `<button type="button" class="run-btn plan-cta" ${cta.attrs || ""}>${esc(cta.label)}</button>` : ""}
  </div>`;
}

// Pill tab strip (the .freq-tab pattern generalized into a reusable atom —
// 企业大脑 editors are its first user). Views wire the click handler and call
// it again (or toggle .on) when the active tab changes.
export function tabsHTML(tabs, activeKey) {
  return `<div class="tabs" role="tablist">
      ${tabs.map((t) => `<button type="button" role="tab" class="tab${t.key === activeKey ? " on" : ""}"
        data-tab="${esc(t.key)}" aria-selected="${t.key === activeKey ? "true" : "false"}">${esc(t.label)}</button>`).join("")}
    </div>`;
}
