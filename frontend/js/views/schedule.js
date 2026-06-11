// ===========================================================================
// schedule.js — 定时与送达. Phase 2 placeholder; the schedule editor + 送达渠道
// (企业微信机器人 webhook、发送测试消息) + 数据收件箱 (§3.6, B5) land in Phase 5.
// ===========================================================================
import { pageHead, comingSoon } from "../components.js";

export function mount(el) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("定时与送达", "设定「每周一 08:00」，报告自动生成并送到企业微信和邮箱 — 产品的招牌体验。", "即将上线")}
    ${comingSoon({
      icon: "⏰",
      title: "定时送达即将上线",
      lead: "两步设置：选好频率（默认每周一 08:00），配置企业微信群机器人或邮箱，之后老板什么都不用做。",
      points: [
        "每周 / 每月 / 自定义频率",
        "企业微信群机器人 + 邮箱双通道，可发送测试消息",
        "下次报告将自动使用你最近上传的数据",
      ],
    })}
  </div>`;
}
