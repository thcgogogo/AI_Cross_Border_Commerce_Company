// ===========================================================================
// workflow.js — 数据分析+报告生成: the WHOLE report workflow on ONE page.
//
// Collapses the old six-item "Agents" dropdown (工作台 / 生成报告 / 历史报告 /
// 定时与送达 / 企业大脑 / 账户与套餐) into a single page with an in-page tab strip.
// Each tab mounts the SAME existing view module — zero logic duplication — and only
// ONE is mounted at a time, so there are no element-id collisions and no six-views-
// at-once polling. The old paths (/agents /new /reports /schedule /brain /account)
// still route here (see main.js ROUTES) with the matching tab pre-selected, so every
// existing navigate() call and external deep link keeps working unchanged.
// ===========================================================================
import * as workbench from "./workbench.js";
import * as reportNew from "./report_new.js";
import * as reports from "./reports.js";
import * as schedule from "./schedule.js";
import * as brain from "./brain.js";
import { esc } from "../components.js";

// key ↔ canonical path ↔ the existing view module that renders that section.
// (账户与套餐 is NOT here — it's its own top-level section, mounted standalone by main.js.)
const TABS = [
  { key: "home", path: "/agents", label: "工作台", mod: workbench },
  { key: "generate", path: "/new", label: "生成报告", mod: reportNew },
  { key: "history", path: "/reports", label: "历史报告", mod: reports },
  { key: "schedule", path: "/schedule", label: "定时与送达", mod: schedule },
  { key: "brain", path: "/brain", label: "企业大脑", mod: brain },
];

function tabStripHTML(activeKey) {
  return TABS.map((t) =>
    `<button type="button" class="wf-tab ${t.key === activeKey ? "on" : ""}" data-path="${esc(t.path)}">${esc(t.label)}</button>`
  ).join("");
}

export async function mount(el, ctx) {
  const tab = TABS.find((t) => t.key === (ctx.params && ctx.params.tab)) || TABS[0];

  el.innerHTML = `<div class="wf-bar">
      <div class="wf-bar-inner">
        <div class="wf-titlerow">
          <h1 class="wf-title">数据分析+报告生成</h1>
          <p class="wf-sub">上传数据 → 生成报告 → 定时送达，整个流程在这一页完成。</p>
        </div>
        <div class="wf-tabs" role="tablist">${tabStripHTML(tab.key)}</div>
      </div>
    </div>
    <div id="wfContent" class="wf-content"></div>`;

  // Tab click → navigate to that section's canonical path. The router re-mounts this
  // host with the matching tab (so back/forward + deep links keep working).
  el.querySelector(".wf-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".wf-tab[data-path]");
    if (btn && !btn.classList.contains("on")) ctx.navigate(btn.dataset.path);
  });

  // Mount the active section's existing view into the content area, mirroring
  // main.js's mount/cleanup contract (await async mounts; keep any returned cleanup).
  const content = el.querySelector("#wfContent");
  let subCleanup = null;
  try {
    const ret = await tab.mod.mount(content, ctx);
    subCleanup = (typeof ret === "function") ? ret : null;
  } catch (e) {
    content.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">⚠️</div>
        <div class="empty-title">这个部分加载失败</div>
        <p class="empty-lead">${esc((e && e.message) || "请稍后重试。")}</p>
      </div></div>`;
  }

  // Teardown: stop the active section's timers/polls when the workflow page unmounts.
  return () => { if (typeof subCleanup === "function") { try { subCleanup(); } catch (e) { /* teardown */ } } };
}
