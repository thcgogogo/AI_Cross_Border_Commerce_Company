// ===========================================================================
// market.js — 智能体市场 (the "agent supermarket"). A browse surface that shows
// the agents a business owner can run: cloud (生成报告) or deployed to their own
// computer (桌面版). Public — logged-out visitors can browse; the deploy/cloud CTAs
// funnel to the right place (login modal fires from the gated routes).
//
// This view is curated copy (no per-user API call), so it always renders. The real
// backend wiring is: "本地部署" optionally adds the agent to the account's desktop
// catalog (installAgent) and routes to /devices to pair a machine; "云端运行" routes
// to the existing report flow (/new). NEVER shows models / tokens / ¥ (customer IA).
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, pageHead } from "../components.js";

// Each entry maps to a REAL config.AGENTS id where one exists; `soon` cards are
// honestly flagged 即将上线 (no fake backend). `local` = has on-device file tools.
const CATALOG = [
  {
    id: "data_analyst", icon: "📊", name: "数据分析师", flagship: true,
    tagline: "把杂乱的 Excel / CSV 变成结论：自动清洗、透视、算出关键指标，并生成一个带图表的 Excel。",
    tags: ["读本地文件", "生成 Excel", "联网搜索"], local: true, cloud: true,
  },
  {
    id: "media_producer", icon: "🎨", name: "媒体制作人",
    tagline: "一句话生成 PPT、Word、PDF、海报 —— 真正能下载的文件，不是预览图。",
    tags: ["PPT / Word / PDF", "自动配图"], local: true, cloud: true,
  },
  {
    id: "report_writer", icon: "📝", name: "报告撰写人",
    tagline: "把数据和要点写成一页可转发的经营周报 / 月报，每个数字都可溯源。",
    tags: ["报告写作", "可溯源"], local: false, cloud: true,
  },
  {
    id: "document_analyst", icon: "📄", name: "文档分析师",
    tagline: "上传合同、报告、长 PDF，自动提炼要点、风险与待办事项。",
    tags: ["文档解析", "联网搜索"], local: false, cloud: true,
  },
  {
    id: null, icon: "🛒", name: "电商运营助手",
    tagline: "盯店铺上下架与比价、批量生成商品文案。京东 / 拼多多 / 淘宝。",
    tags: ["电商"], soon: true,
  },
  {
    id: null, icon: "📣", name: "营销文案助手",
    tagline: "公众号、小红书、朋友圈文案与配图，一键成稿。",
    tags: ["内容营销"], soon: true,
  },
];

function badgesHTML(a) {
  const out = [];
  if (a.local) out.push(`<span class="mk-tag">💻 可装到电脑</span>`);
  if (a.cloud) out.push(`<span class="mk-tag">☁️ 云端可跑</span>`);
  return out.join("");
}

function actionsHTML(a) {
  if (a.soon) return `<div class="mk-soon-chip">即将上线</div>`;
  const cloud = a.cloud
    ? `<button type="button" class="run-btn" data-act="cloud">云端运行</button>` : "";
  const local = a.local
    ? `<button type="button" class="ghost-btn" data-act="local" data-id="${esc(a.id)}">装到我的电脑</button>` : "";
  return `<div class="mk-actions">${cloud}${local}</div>`;
}

function cardHTML(a) {
  return `<div class="mk-card ${a.soon ? "mk-soon" : ""}">
    <div class="mk-top">
      <div class="mk-icon">${a.icon}</div>
      <h3 class="mk-name">${esc(a.name)}${a.flagship ? `<span class="mk-flag">旗舰</span>` : ""}</h3>
    </div>
    <p class="mk-tagline">${esc(a.tagline)}</p>
    <div class="mk-tags">${a.tags.map((t) => `<span class="mk-tag">${esc(t)}</span>`).join("")}${badgesHTML(a)}</div>
    ${actionsHTML(a)}
  </div>`;
}

export function mount(el, ctx) {
  el.innerHTML = `<div class="page">
    ${pageHead("智能体市场", "挑一个智能体，让它在云端替你跑，或装到你自己的电脑上、就地处理本地文件。")}
    <div class="mk-grid">${CATALOG.map(cardHTML).join("")}</div>
    <p class="mk-deploy-note">想让智能体直接处理你电脑里的文件？前往
      <a href="/devices" data-nav>桌面版与设备</a> 下载桌面版并配对你的电脑。</p>
  </div>`;

  el.querySelector(".mk-grid").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "cloud") { ctx.navigate("/new"); return; }
    if (act === "local") {
      // Logged in → best-effort add to the account's desktop catalog, then go pair a
      // machine. Logged out → straight to /devices (which gates to the login modal).
      if (state.account && btn.dataset.id) {
        btn.disabled = true; btn.textContent = "已加入…";
        try { await api.installAgent(btn.dataset.id, true); } catch (err) { /* best-effort */ }
        btn.textContent = "✓ 已加入桌面版";
        setTimeout(() => ctx.navigate("/devices"), 600);
      } else {
        ctx.navigate("/devices");
      }
    }
  });
}
