// ===========================================================================
// brain.js — 企业大脑. Phase 2 placeholder; the stats hero (已记住 N 个指标 ·
// M 周历史), KPI trend tiles with sparklines and the restyled editors (§3.7,
// B6) land in Phase 6. The team's memory keeps accumulating server-side in
// the meantime — every report run still reads and writes it.
// ===========================================================================
import { pageHead, comingSoon } from "../components.js";

export function mount(el) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("企业大脑", "这是只属于你公司的经营记忆，每份报告都会让它更聪明。", "升级中")}
    ${comingSoon({
      icon: "🧠",
      title: "企业大脑全新页面即将上线",
      lead: "新版将首先展示记忆全景（已记录指标、覆盖周数、术语与偏好数量）和每个 KPI 的趋势小图，再提供档案 / 指标 / 术语 / 偏好编辑与历史数据导入。",
      points: [
        "已记录指标 N 项 · 覆盖 M 周 · 术语 K 条 · 长期偏好 P 条",
        "每个 KPI 的最新值、环比箭头与连续走势",
        "历史数据导入：第一份报告就带趋势对比",
      ],
    })}
    <p class="muted" style="margin-top:14px;">放心：团队的记忆一直在积累 — 现在生成的每份报告都会自动读取并补充企业大脑。</p>
  </div>`;
}
