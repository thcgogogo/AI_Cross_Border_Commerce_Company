// ===========================================================================
// workbench.js — 工作台 (logged-in home). Phase 2 placeholder: greeting +
// quick entries. The real §3.2 cards (下次送达 countdown, 最新报告, 本月额度,
// 数据收件箱, 企业大脑 teaser) land in Phase 5.
// ===========================================================================
import { state } from "../state.js";
import { esc, pageHead } from "../components.js";
import { openOnboarding } from "./onboarding.js";

export function mount(el, ctx) {
  const name = (state.account && state.account.name) || "";
  el.innerHTML = `<div class="page">
    ${pageHead(`你好，${name}`, "这是你的工作台。完整版（下次送达倒计时、本月额度、数据收件箱）即将上线 — 现在就可以用新手引导生成第一份报告。")}
    <div class="card-grid">
      <div class="card">
        <h3 class="card-title">🚀 生成第一份报告</h3>
        <p class="muted">跟着新手引导走：选行业 → 上传数据（或用示例数据）→ 确认指标 → 自动生成。全程不用写一句话。</p>
        <button type="button" class="run-btn" id="wbOnboard">打开新手引导</button>
      </div>
      <div class="card">
        <h3 class="card-title">🗂️ 历史报告</h3>
        <p class="muted">每一份生成过的报告都会存档，随时在线查看或下载 PDF。</p>
        <button type="button" class="ghost-btn" id="wbReports" style="margin-top:16px;">查看历史报告</button>
      </div>
      <div class="card">
        <h3 class="card-title">🧠 企业大脑</h3>
        <p class="muted">它记住你的公司：指标、口径、术语、偏好。每份报告都会让它更聪明。</p>
        <button type="button" class="ghost-btn" id="wbBrain" style="margin-top:16px;">打开企业大脑</button>
      </div>
    </div>
  </div>`;

  el.querySelector("#wbOnboard").addEventListener("click", () => openOnboarding());
  el.querySelector("#wbReports").addEventListener("click", () => ctx.navigate("/reports"));
  el.querySelector("#wbBrain").addEventListener("click", () => ctx.navigate("/brain"));
}
