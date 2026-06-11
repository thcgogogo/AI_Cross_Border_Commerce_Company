// ===========================================================================
// landing.js — 营销首页 (logged-out home), the full §3.1 page (Phase 3).
//
// Sells the RESULT, never the machinery: hero → 三步 → 示例报告 (a real
// pipeline-generated sample) → 数字可溯源 → 行业模板 → 企业大脑 → 定价 (flat,
// from /api/plans) → 安全与隐私 → FAQ → final CTA. Sample assets are read from
// /assets/sample/manifest.json so the section degrades gracefully if the
// sample has not been generated yet (scripts/make_sample_assets.py).
// ===========================================================================
import { state } from "../state.js";
import { esc, cny, planCard, singleReportCard } from "../components.js";
import * as api from "../api.js";
import { openLoginModal, openSignupModal } from "./auth.js";

const FAQ = [
  ["我完全不懂 AI，能用吗？",
   "能。你只需要把平时就有的 Excel / CSV 表格拖进来——系统不要求任何配置，也没有「提示词」这回事。订阅后连上传都可以省掉：到点报告自动送到企业微信和邮箱。"],
  ["报告里的数字可信吗？",
   "每个关键数字都带一个【N】角标，对应文末「数据来源与核验」表——指明它来自哪个文件、哪个表、哪些行；趋势数字由确定性程序计算并标注「机器核验」。任何无法溯源的数字都会被审查环节标记出来。"],
  ["我的数据安全吗？",
   "上传文件与报告 PDF 加密存储，下载需要登录并校验归属。你的数据只用于生成你自己的报告——绝不用于训练，也绝不会出现在其他账号面前。"],
  ["怎么收费？会不会越用越贵？",
   "固定价格：基础版 ¥199/月，企业版 ¥599/月，也可以 ¥29 单买一份。价格与「用量」无关，成本透明，绝不超支。对比一下：一位初级分析师月薪约 ¥8000。"],
  ["可以开发票吗？",
   "可以。支付宝 / 微信支付付款后，在「账户与套餐」里一键申请发票。"],
  ["不想要了怎么办？",
   "随时取消，服务保留到当期结束，不扣后续费用。你的历史报告与企业大脑数据仍可导出。"],
];

export async function mount(el) {
  const meta = state.meta || {};
  const tagline = meta.product_tagline || "你的 AI 经营分析团队";
  const single = meta.single_report_price_yuan;

  el.innerHTML = `
    <section class="hero">
      <span class="hero-kicker">${esc(tagline)}</span>
      <h1 class="hero-title">周一早上，<br/>报告自己到。</h1>
      <p class="hero-sub">把杂乱的 Excel 交给你的 AI 经营分析团队；每周一 08:00，
        一页可转发的经营报告准时送到企业微信和邮箱。不用配置，不写提示词。</p>
      <div class="hero-actions">
        <button type="button" class="hero-btn primary" id="landingSignup">免费体验（送 2 份报告）</button>
        <button type="button" class="hero-btn" id="landingSample" hidden>看一份示例报告</button>
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
        <div class="step3-sub">多重交叉核验，每个数字都能溯源到原始表格。</div>
      </div>
      <div class="step3">
        <div class="step3-num">3</div>
        <div class="step3-title">准时送达</div>
        <div class="step3-sub">企业微信 + 邮箱自动送达，老板看完即可转发。</div>
      </div>
    </div>

    <section class="lsec" id="sampleSec" hidden>
      <h2 class="lsec-h2">这就是你每周收到的报告</h2>
      <p class="lsec-lead">下面是一份真实生成的示例（数据来自虚构的「晨光贸易有限公司」）：
        核心结论、关键指标、风险异常、下周行动 — 一页讲清，老板直接转发。</p>
      <div class="sample-strip" id="sampleStrip"></div>
      <p class="sample-note">
        <a id="samplePdfLink" href="#" target="_blank" rel="noopener">下载示例 PDF ↗</a>
        · 带公司 logo 与品牌色 · 文末附「数据来源与核验」</p>
    </section>

    <section class="lsec lsec-alt" id="traceSec">
      <div class="trace-grid">
        <div>
          <h2 class="lsec-h2">每个数字，都能问一句「哪来的？」</h2>
          <ul class="trace-points">
            <li><strong>【N】溯源角标</strong> — 报告里每个关键数字都标注来源：哪个文件、哪个表、哪些行。</li>
            <li><strong>机器核验的趋势</strong> — 环比、连续变化、异常波动由确定性程序计算，不靠 AI 心算。</li>
            <li><strong>独立审查</strong> — 出稿后还有一道审查：任何无法溯源的数字都会被标记出来。</li>
          </ul>
          <p class="trace-foot">一份要被转发给老板的报告，容不下一个编造的数字 — 这是我们的底线。</p>
        </div>
        <div class="trace-figure" id="traceFigure"></div>
      </div>
    </section>

    <section class="lsec" id="industrySec">
      <h2 class="lsec-h2">懂你的行业</h2>
      <p class="lsec-lead">每个行业有自己的模板：该看的指标、该有的章节、该提醒的风险，开箱即用。</p>
      <div class="chip-row" id="industryChips"></div>
    </section>

    <section class="lsec lsec-alt" id="brainSec">
      <h2 class="lsec-h2">它记住你的公司</h2>
      <p class="lsec-lead">企业大脑：只属于你公司的经营记忆，每份报告都让它更聪明。</p>
      <div class="brain3">
        <div class="card"><div class="card-title">记住指标与口径</div>
          <p>你的表格列名、KPI 定义、财务周口径 — 确认一次，永久生效。</p></div>
        <div class="card"><div class="card-title">术语问一次，记一辈子</div>
          <p>「华东大区」指哪几个省？内部黑话一次确认，以后每份报告都懂。</p></div>
        <div class="card"><div class="card-title">和自己的历史比</div>
          <p>「华东区已连续三周下滑，环比 −12.4%」— 这种话只有记得你全年数据的分析师说得出。</p></div>
      </div>
    </section>

    <section class="lsec" id="pricing">
      <h2 class="lsec-h2">定价 · 像雇人一样简单</h2>
      <p class="lsec-lead">固定价格，按价值计费。成本透明，绝不超支。</p>
      <div class="pricing-grid" id="pricingGrid"></div>
      <p class="pricing-note">支付宝 / 微信支付 · 可开发票${single ? ` · 不想订阅？${esc(cny(single, 0))} 单买一份` : ""}</p>
    </section>

    <section class="lsec lsec-alt" id="secSec">
      <h2 class="lsec-h2">安全与隐私</h2>
      <div class="sec-strip">
        <div class="sec-item">🔒 上传与报告加密存储</div>
        <div class="sec-item">👤 下载需登录并校验归属</div>
        <div class="sec-item">🚫 绝不用于训练</div>
        <div class="sec-item">📄 数据仅用于生成你自己的报告</div>
      </div>
    </section>

    <section class="lsec" id="faqSec">
      <h2 class="lsec-h2">常见问题</h2>
      <div class="faq" id="faqList">
        ${FAQ.map(([q, a]) => `<details class="faq-item">
            <summary>${esc(q)}</summary><p>${esc(a)}</p>
          </details>`).join("")}
      </div>
    </section>

    <section class="cta-final">
      <h2>下周一早上 8 点，收到第一份？</h2>
      <p>注册即送 2 份完整报告 — 用你自己的数据试，或一键体验示例数据。</p>
      <button type="button" class="run-btn" id="landingSignup2">免费体验（送 2 份报告）</button>
      <p class="hero-note">已有账号？<button type="button" class="link-btn" id="landingLogin">登录</button></p>
    </section>`;

  // CTA wiring
  el.querySelector("#landingSignup").addEventListener("click", openSignupModal);
  el.querySelector("#landingSignup2").addEventListener("click", openSignupModal);
  el.querySelector("#landingLogin").addEventListener("click", openLoginModal);

  // Sample showcase — only when the generated assets exist (manifest 404 → stay hidden).
  fetch("/assets/sample/manifest.json").then((r) => (r.ok ? r.json() : null)).then((man) => {
    if (!man || !man.pages) return;
    const sec = el.querySelector("#sampleSec");
    const strip = el.querySelector("#sampleStrip");
    if (!sec || !strip) return; // view was unmounted meanwhile
    strip.innerHTML = Array.from({ length: man.pages }, (_, i) => `
      <a class="sample-page" href="/assets/sample/${esc(man.pdf)}" target="_blank" rel="noopener"
         aria-label="示例报告第 ${i + 1} 页">
        <img src="/assets/sample/page-${i + 1}.png" alt="示例报告第 ${i + 1} 页"
             loading="${i ? "lazy" : "eager"}"></a>`).join("");
    el.querySelector("#samplePdfLink").href = `/assets/sample/${man.pdf}`;
    sec.hidden = false;
    const btn = el.querySelector("#landingSample");
    btn.hidden = false;
    btn.addEventListener("click", () => sec.scrollIntoView({ behavior: "smooth" }));
    // 溯源 block illustration = the sources-appendix page, when the manifest marks one.
    if (man.sources_page) {
      el.querySelector("#traceFigure").innerHTML = `
        <img src="/assets/sample/page-${man.sources_page}.png"
             alt="数据来源与核验附录示例" loading="lazy">`;
    }
  }).catch(() => { /* sample assets absent — section stays hidden */ });

  // 行业模板 chips (config-driven packs).
  api.getIndustries().then((rows) => {
    const chips = el.querySelector("#industryChips");
    if (!chips) return;
    chips.innerHTML = (rows || [])
      .map((r) => `<span class="chip">${esc(r.display_name_cn || r.key)}</span>`)
      .join("") + `<span class="chip chip-soon">更多行业陆续上线</span>`;
  }).catch(() => { /* strip is decorative */ });

  // 定价 cards from /api/plans (+ the 单次 card from meta) — the shared
  // planCard/singleReportCard atoms (components.js), signup CTAs here.
  api.getPlans().then((data) => {
    const grid = el.querySelector("#pricingGrid");
    if (!grid) return;
    const plans = (data && data.plans) || [];
    const byCode = Object.fromEntries(plans.map((p) => [p.code, p]));
    const signupCta = (label) => ({ label, attrs: "data-signup" });
    const cards = [
      planCard(byCode.free, { cta: signupCta("立即免费体验") }),
      singleReportCard(single, { cta: signupCta("注册后购买") }),
      planCard(byCode.basic, { featured: true, cta: signupCta("免费体验后订阅") }),
      planCard(byCode.pro, { cta: signupCta("免费体验后订阅") }),
    ];
    grid.innerHTML = cards.join("");
    grid.querySelectorAll("[data-signup]").forEach((b) =>
      b.addEventListener("click", openSignupModal));
  }).catch(() => {
    const grid = el.querySelector("#pricingGrid");
    if (grid) grid.innerHTML = `<p class="lsec-lead">价格信息加载失败，请刷新重试。</p>`;
  });
}
