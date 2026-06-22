// ===========================================================================
// views/workflow_hub.js — the 工作流 hub (/workflow).
//
// A small landing that presents the two work surfaces as cards, so 工作流 is a
// page of its own rather than only a nav dropdown. Each card links to its
// sub-page; the dropdown menu still offers the same two as a quick jump.
// ===========================================================================
import { esc } from "../components.js";

const REPORT_ICON =
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
     <rect x="4" y="3" width="16" height="18" rx="2"></rect>
     <path d="M8 8h8M8 12h8M8 16h5"></path>
   </svg>`;

const RECON_ICON =
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
     <path d="M4 7h12l-3-3M20 17H8l3 3"></path>
     <path d="M16 11l2 0M6 13l2 0"></path>
   </svg>`;

const MARKET_ICON =
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
     <circle cx="12" cy="12" r="9"></circle>
     <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"></path>
   </svg>`;

const CARDS = [
  {
    path: "/agents", icon: REPORT_ICON,
    title: "数据分析 + 报告生成",
    desc: "上传数据，AI 团队生成可溯源的经营报告，并按时送达。",
  },
  {
    path: "/cross-border", icon: RECON_ICON,
    title: "跨境财务",
    desc: "出口跨境电商一站核对：回款核对·收汇结汇·三流合一·进项发票·退税，可溯源交付。",
  },
  {
    path: "/markets", icon: MARKET_ICON,
    title: "跨境市场情报日报",
    desc: "跨境电商专属：设置关注的平台·市场·类目，每天早 8 点自动汇总政策合规·成本利润·需求机会·竞争态势与截止事项，可溯源交付。",
  },
];

function cardHTML(c) {
  return `<a class="wf-hub-card" href="${esc(c.path)}" data-nav>
      <span class="wf-hub-icon">${c.icon}</span>
      <span class="wf-hub-text">
        <span class="wf-hub-title">${esc(c.title)}</span>
        <span class="wf-hub-desc">${esc(c.desc)}</span>
      </span>
      <span class="wf-hub-go" aria-hidden="true">→</span>
    </a>`;
}

export async function mount(el) {
  el.innerHTML = `<div class="page page-narrow">
    <header class="page-head">
      <h1 class="page-h">工作流</h1>
      <p class="page-lead">选择要使用的工作流。</p>
    </header>
    <div class="wf-hub">${CARDS.map(cardHTML).join("")}</div>
  </div>`;
  return () => {};
}
