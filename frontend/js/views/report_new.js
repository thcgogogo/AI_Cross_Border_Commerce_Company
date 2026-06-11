// ===========================================================================
// report_new.js — 生成报告. Phase 2 placeholder; the zero-prompt report-type
// cards + restyled live feed (§3.3) land in Phase 4. Until then the wizard
// already generates a complete report end-to-end.
// ===========================================================================
import { pageHead, comingSoon } from "../components.js";
import { openOnboarding } from "./onboarding.js";

export function mount(el) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("生成报告", "选择报告类型、拖入数据文件，点一个按钮 — 不用写任何提示词。", "即将上线")}
    ${comingSoon({
      icon: "📑",
      title: "零配置生成即将上线",
      lead: "这里将提供报告类型卡片（经营周报 / 经营月报 / 自定义分析）、数据拖拽上传和实时的 AI 团队工作直播。",
      points: [
        "报告类型 + 数据文件即可开始，无需输入文字",
        "实时查看 AI 团队的分析过程",
        "完成后自动生成可转发的品牌报告",
      ],
      cta: { id: "rnOnboard", label: "现在就用新手引导生成一份" },
    })}
  </div>`;
  el.querySelector("#rnOnboard").addEventListener("click", () => openOnboarding());
}
