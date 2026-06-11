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
    .replace(/"/g, "&quot;");
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
export function renderMarkdown(md) {
  const lines = esc(md).split("\n");
  let html = "";
  let listType = null;
  const closeList = () => {
    if (listType) { html += `</${listType}>`; listType = null; }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*#{1,6}\s+/.test(line)) {
      closeList();
      const level = line.match(/^\s*(#{1,6})/)[1].length;
      html += `<h${level}>${inline(line.replace(/^\s*#{1,6}\s+/, ""))}</h${level}>`;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`;
    } else if (/^\s*\d+\.\s+/.test(line)) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inline(line.replace(/^\s*\d+\.\s+/, ""))}</li>`;
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      html += `<p>${inline(line)}</p>`;
    }
  }
  closeList();
  return html;
}

function inline(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>")
    .replace(/(^|[^*])\*([^*]+?)\*(?!\*)/g, "$1<em>$2</em>");
}

// ---- status pill (Chinese-first) ---------------------------------------------
export function statusPillHTML(status) {
  const map = {
    starting: ["启动中…", "pill-running"],
    running: ["生成中…", "pill-running"],
    done: ["已完成", "pill-done"],
    error: ["出错", "pill-error"],
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
