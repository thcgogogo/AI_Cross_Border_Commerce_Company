// ===========================================================================
// landing.js — 营销首页 (logged-out home). Phase 2 ships the hero + 三步 strip
// so the logged-out experience already sells the report; the full §3.1 page
// (sample showcase, pricing from /api/plans, FAQ…) lands in Phase 3.
// ===========================================================================
import { state } from "../state.js";
import { esc } from "../components.js";
import { openLoginModal, openSignupModal } from "./auth.js";

export function mount(el) {
  const meta = state.meta || {};
  const tagline = meta.product_tagline || "你的 AI 经营分析团队";
  el.innerHTML = `
    <section class="hero">
      <span class="hero-kicker">${esc(tagline)}</span>
      <h1 class="hero-title">周一早上，<br/>报告自己到。</h1>
      <p class="hero-sub">把杂乱的 Excel 交给你的 AI 经营分析团队；每周一 08:00，
        一页可转发的经营报告准时送到企业微信和邮箱。不用配置，不写提示词。</p>
      <div class="hero-actions">
        <button type="button" class="hero-btn primary" id="landingSignup">免费体验（送 2 份报告）</button>
        <button type="button" class="hero-btn" id="landingLogin">登录</button>
      </div>
      <p class="hero-note">注册需手机号验证 · 无需绑定支付方式</p>
    </section>

    <div class="steps3">
      <div class="step3">
        <div class="step3-num">1</div>
        <div class="step3-title">上传数据</div>
        <div class="step3-sub">销售导出、订单表、费用表 — 乱一点也没关系。</div>
      </div>
      <div class="step3">
        <div class="step3-num">2</div>
        <div class="step3-title">AI 团队分析</div>
        <div class="step3-sub">多模型交叉核验，每个数字都能溯源到原始表格。</div>
      </div>
      <div class="step3">
        <div class="step3-num">3</div>
        <div class="step3-title">准时送达</div>
        <div class="step3-sub">企业微信 + 邮箱自动送达，老板看完即可转发。</div>
      </div>
    </div>

    <p class="landing-trust">数字可溯源 · 成本透明，绝不超支 · 数据加密存储，绝不用于训练</p>`;

  el.querySelector("#landingSignup").addEventListener("click", openSignupModal);
  el.querySelector("#landingLogin").addEventListener("click", openLoginModal);
}
