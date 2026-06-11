// ===========================================================================
// reports.js — 历史报告 (top-level list). Phase 2 placeholder; the grouped
// list with type badges + web report deep links (§3.5, B7) lands in Phase 4.
// ===========================================================================
import { pageHead, comingSoon } from "../components.js";

export function mount(el) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("历史报告", "每一份报告都会按月份归档：在线查看、下载 PDF、转发到企业微信。", "即将上线")}
    ${comingSoon({
      icon: "🗂️",
      title: "报告档案即将上线",
      lead: "生成过的每份报告（周报 / 月报 / 手动 / 导入）都会出现在这里，支持在线阅读与 PDF 下载。",
      points: [
        "按月份分组，一眼找到任何一期",
        "手机端也能直接打开网页版报告",
        "企业微信里的链接会直接跳到对应报告",
      ],
    })}
  </div>`;
}
