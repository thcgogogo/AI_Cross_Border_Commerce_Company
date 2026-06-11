// ===========================================================================
// report_view.js — 报告视图 (/reports/<id>). Phase 2 placeholder; the branded
// document frame (brand bar, TOC, tappable 【N】 anchors, sources appendix,
// feedback bars — §3.4) lands in Phase 4.
// ===========================================================================
import { pageHead, comingSoon } from "../components.js";

export function mount(el, ctx) {
  const id = (ctx.params && ctx.params.runId) || "";
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead(`报告 #${id}`, "网页版报告视图即将上线。", "即将上线")}
    ${comingSoon({
      icon: "📄",
      title: "品牌报告视图即将上线",
      lead: "这一页将以文档形式展示报告：品牌色顶栏、固定章节顺序、可点的数据溯源锚点与核验附录。",
      points: [
        "每个【N】锚点可点开查看数据来源",
        "每节都可以 👍/👎 反馈并存为长期偏好",
        "一键下载 PDF / 在企业微信中转发",
      ],
    })}
  </div>`;
}
