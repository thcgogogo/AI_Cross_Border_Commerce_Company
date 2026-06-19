// ===========================================================================
// views/landing.js — the PUBLIC marketing landing ("/").
//
// Recreated from the Tangent AI design handoff (Claude Design "Landing"): a
// long-scroll marketing home — hero + static product mock, a dark "Fusion"
// concept band, two product pillars (工作流 / Fusion), a dark 技术与算法 section
// (five core-capability cards), an enterprise trust strip, a dark CTA, and the
// footer.
//
// This page renders its OWN nav + footer; main.js hides the shared app chrome on
// this route (body.landing-mode). Everything is styled by the .lp-* namespace in
// style.css. The only behaviours ported from the design's runtime are the
// --accent theming (now plain CSS) and the data-reveal scroll-in (below).
//
// CTAs funnel to the live surfaces: 免费开始 / 开始使用 → /fusion (the chat front
// door); 登录 → the auth modal; 了解工作流 → /agents. Model names are shown here by
// design (Fusion is the one surface that names the council); never a ¥ cost.
// ===========================================================================
import { state } from "../state.js";
import { openLoginModal } from "./auth.js";

const MARK = `<span class="lp-logo-mark" aria-hidden="true">T</span>`;

function navActions() {
  // Logged-in members skip 登录 — they get a direct way into the workspace.
  if (state.account) return `<a class="lp-pill" href="/workflow" data-nav>进入工作台&nbsp;→</a>`;
  return `<button type="button" class="lp-login" id="lpLogin">登录</button>
        <a class="lp-pill" href="/fusion" data-nav>开始使用</a>`;
}

function html() {
  return `<div class="lp" id="lpRoot">

  <!-- NAV -->
  <header class="lp-nav">
    <nav class="lp-nav-inner" aria-label="主导航">
      <a class="lp-logo" href="/" data-nav>${MARK}<span class="lp-logo-name">Tangent&nbsp;AI</span></a>
      <div class="lp-nav-links">
        <a href="#fusion">Fusion</a>
        <a href="#workflows">工作流</a>
        <a href="#tech">技术</a>
        <a href="#enterprise">企业版</a>
      </div>
      <div class="lp-nav-actions">${navActions()}</div>
    </nav>
  </header>

  <!-- HERO -->
  <section class="lp-hero" id="top">
    <div class="lp-hero-glow"></div>
    <div class="lp-container lp-hero-inner">
      <div class="lp-eyebrow"><span class="lp-dot"></span>Tangent 融合引擎</div>
      <h1 class="lp-hero-title">把 token 变成企业可信赖的工作流。</h1>
      <p class="lp-hero-sub">Tangent 让你的难题在多个前沿模型间并行作答、交叉核验，最终给出一个经过验证的结果 —— 每个数字都可溯源。</p>
      <div class="lp-hero-cta">
        <a class="lp-btn lp-btn-primary" href="/fusion" data-nav>免费开始</a>
        <a class="lp-btn lp-btn-ghost" href="#fusion">预约演示&nbsp;→</a>
      </div>
      <p class="lp-hero-note">无需绑卡 · 匿名试用 · SOC 2 Type II</p>

      <!-- PRODUCT MOCK -->
      <div class="lp-mock" data-reveal>
        <div class="lp-mock-card">
          <div class="lp-mock-input">
            <div class="lp-mock-input-text">帮我分析这个季度的销售数据，并标出对不上账的地方……</div>
            <span class="lp-mock-send" aria-hidden="true">↑</span>
          </div>
          <div class="lp-mock-panel">
            <div class="lp-mock-prow">
              <div class="lp-mock-tiers">
                <span class="lp-tier">高质量</span>
                <span class="lp-tier off">经济</span>
              </div>
              <span class="lp-mock-custom">自定义模型 ▾</span>
            </div>
            <div class="lp-mock-models">
              <span class="lp-chip">Opus 4.7&nbsp;&nbsp;×</span>
              <span class="lp-chip">GPT-5.1&nbsp;&nbsp;×</span>
              <span class="lp-chip">DeepSeek V4 Pro&nbsp;&nbsp;×</span>
              <span class="lp-mut">最多 3 个</span>
            </div>
          </div>
        </div>
        <div class="lp-mock-float">
          <span class="lp-check" aria-hidden="true">✓</span>
          <div>
            <div class="lp-float-t">3 个模型一致</div>
            <div class="lp-float-s">已核验 · 可溯源</div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- CONCEPT BAND (dark) -->
  <section class="lp-band" id="fusion" data-reveal>
    <div class="lp-container">
      <div class="lp-band-head">
        <div class="lp-eyebrow-d">Fusion</div>
        <h2>一个模型答错，<br>其它模型会纠正它。</h2>
        <p class="lp-band-sub">问一次，几位顶级模型并行作答、相互辩证、最终收敛。你不必看到中间的噪音 —— 只看到经得起推敲的结论。</p>
      </div>
      <div class="lp-flow">
        <div class="lp-flow-col">
          <div class="lp-flow-row"><span class="lp-flow-name">Opus 4.7</span><span class="lp-flow-tag ok">一致</span></div>
          <div class="lp-flow-row"><span class="lp-flow-name">GPT-5.1</span><span class="lp-flow-tag ok">一致</span></div>
          <div class="lp-flow-row"><span class="lp-flow-name">DeepSeek V4</span><span class="lp-flow-tag warn">质疑 Q3</span></div>
        </div>
        <span class="lp-flow-arrow" aria-hidden="true">→</span>
        <div class="lp-flow-fuse">
          <div class="lp-flow-fuse-k">共识</div>
          <div class="lp-flow-fuse-t">交叉核验 · 化解分歧</div>
        </div>
        <span class="lp-flow-arrow" aria-hidden="true">→</span>
        <div class="lp-flow-out">
          <div class="lp-check" aria-hidden="true">✓</div>
          <div class="lp-flow-out-t">已验证答案</div>
          <div class="lp-flow-out-s">附审计轨迹</div>
        </div>
      </div>
    </div>
  </section>

  <!-- FEATURES -->
  <section class="lp-features" id="workflows">
    <div class="lp-container">
      <div class="lp-section-head" data-reveal>
        <div class="lp-eyebrow-c">产品矩阵</div>
        <h2>三块基石，一条可信流水线。</h2>
      </div>

      <!-- Feature 1: Workflows → email delivery -->
      <div class="lp-feature" data-reveal>
        <div class="lp-feature-text">
          <div class="lp-feature-k">工作流</div>
          <h3>把 token 变成企业可信赖的工作流。</h3>
          <p>我们通过 Tangent 引擎把 token 转换成企业能够依赖的工作流。比如，直接把 token 变成发到你邮箱里的报告 —— 涵盖大量通用专业工作流，以及为各行业量身定制的独有工作流。</p>
          <div class="lp-feature-list">
            <div><span class="d">—</span> token 直达成品，自动送进你的邮箱</div>
            <div><span class="d">—</span> 覆盖广泛的通用专业工作流</div>
            <div><span class="d">—</span> 面向各行业的独有工作流</div>
          </div>
          <a class="lp-feature-link" href="/agents" data-nav>了解工作流&nbsp;→</a>
        </div>
        <div class="lp-feature-visual">
          <div class="lp-panel">
            <div class="lp-panel-head">
              <span class="lp-panel-title">📧 收件箱 · Tangent</span>
              <span class="lp-badge-ok">已送达</span>
            </div>
            <div class="lp-mail">
              <div class="lp-mail-subject">您的 Q3 销售复盘报告已生成</div>
              <div class="lp-mail-meta">Tangent 引擎 · 今天 08:00 自动发送</div>
              <div class="lp-mail-body">已分析 3 份上传材料，生成 6 页报告并完成对账核验。分销渠道存在 1 处异常，详见正文。</div>
              <div class="lp-mail-attach">
                <span class="lp-mail-file">📄 Q3_销售复盘.pdf</span>
                <span class="lp-mail-size">1.2 MB</span>
              </div>
            </div>
            <div class="lp-panel-note">同一套工作流也可输出结算核对、董事会摘要等成品 —— 全部自动送达。</div>
          </div>
        </div>
      </div>

      <!-- Feature 2: Fusion -->
      <div class="lp-feature lp-feature-rev" data-reveal>
        <div class="lp-feature-text">
          <div class="lp-feature-k">Fusion</div>
          <h3>多个前沿模型，一个站得住脚的答案。</h3>
          <p>选择质量档位或你自己的模型组合。Tangent 融合它们的推理，指出分歧所在并加以化解 —— 让单次幻觉永远到不了你的桌面。</p>
          <div class="lp-feature-list">
            <div><span class="d">—</span> 高质量与经济档位</div>
            <div><span class="d">—</span> 每次最多 3 个模型</div>
            <div><span class="d">—</span> 自动择优融合</div>
          </div>
          <a class="lp-feature-link" href="/fusion" data-nav>了解 Fusion 如何运作&nbsp;→</a>
        </div>
        <div class="lp-feature-visual">
          <div class="lp-panel lp-panel-flex">
            <div class="lp-fbar"><span class="lp-fbar-name">Opus 4.7</span><span class="lp-fbar-meter" style="width:64%"></span></div>
            <div class="lp-fbar"><span class="lp-fbar-name">GPT-5.1</span><span class="lp-fbar-meter" style="width:78%"></span></div>
            <div class="lp-fbar"><span class="lp-fbar-name">DeepSeek V4 Pro</span><span class="lp-fbar-meter" style="width:52%"></span></div>
            <div class="lp-fbar-out">
              <span class="lp-check" aria-hidden="true">✓</span>
              <div>
                <div class="lp-fbar-out-t">融合答案已就绪</div>
                <div class="lp-fbar-out-s">择优 · 已化解 1 处分歧</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- TECH & ALGORITHM (dark) -->
  <section class="lp-tech" id="tech">
    <div class="lp-container">
      <div class="lp-tech-head" data-reveal>
        <div class="lp-eyebrow-d">技术与算法</div>
        <h2>引擎背后的五项核心能力。</h2>
        <p>不只是把模型拼在一起。Tangent 在架构、记忆、溯源、行业理解与数据安全上做了深一层的工程。</p>
      </div>

      <div class="lp-tech-grid" data-reveal>

        <!-- 0. Fusion architecture / reasoning -->
        <div class="lp-tech-hero">
          <div>
            <div class="lp-tech-tagrow">
              <span class="lp-tech-tag">Fusion 引擎架构</span>
              <span class="lp-tech-badge">更强推理</span>
            </div>
            <h3>更少的 token，比肩顶尖大模型的推理。</h3>
            <p>Fusion 架构为模型带来被强化的推理能力 —— 在消耗更少 token 的前提下，推理水平比肩国外顶尖大语言模型。更高的智能，更低的成本。</p>
          </div>
          <div class="lp-tech-meters">
            <div>
              <div class="lp-tech-meter-l"><span>推理能力</span><span class="v blue">比肩顶尖</span></div>
              <div class="lp-tech-track"><span class="lp-tech-fill blue" style="width:96%"></span></div>
            </div>
            <div>
              <div class="lp-tech-meter-l"><span>token 消耗</span><span class="v green">↓ 显著更低</span></div>
              <div class="lp-tech-track"><span class="lp-tech-fill green" style="width:42%"></span></div>
            </div>
          </div>
        </div>

        <!-- 1. Compounding memory -->
        <div class="lp-tech-card">
          <div class="lp-tech-tagrow">
            <span class="lp-tech-tag">复利记忆通道</span>
            <span class="lp-tech-badge">越用越聪明</span>
          </div>
          <h3>从过去的每一次运行中积累经验。</h3>
          <p>Tangent 引擎会总结过往工作流的经验，让之后的任务更高效；当它检测到与以往相同的错误时，会主动发出警告，避免你重蹈覆辙。</p>
          <div class="lp-tech-warn">
            <span class="lp-tech-warn-i" aria-hidden="true">⚠</span>
            <div>
              <div class="lp-tech-warn-t">检测到与 3 月相同的对账偏差</div>
              <div class="lp-tech-warn-s">上次因发票口径不一致导致 —— 已自动预警</div>
            </div>
          </div>
        </div>

        <!-- 2. Traceability -->
        <div class="lp-tech-card" id="trace">
          <div class="lp-tech-tag">溯源</div>
          <h3>每个数字都能回到它的来源。</h3>
          <p>没有黑箱。点击任意数字，直接追溯到它来自的单元格、文档与模型运行记录。</p>
          <div class="lp-tech-trace">
            <div class="lp-tech-trace-q">Q3 净营收达到 <span class="lp-tech-hl">$3.21M</span>，环比 Q2 增长 9%。</div>
            <div class="lp-tech-trace-rows">
              <div><span class="k">文件</span><span class="v">finance_q3.xlsx</span></div>
              <div><span class="k">单元格</span><span class="v">Summary!F14</span></div>
              <div><span class="k">运行</span><span class="v blue">#a3f9 · 3 个模型</span></div>
            </div>
          </div>
        </div>

        <!-- 3. Industry workflows -->
        <div class="lp-tech-card">
          <div class="lp-tech-tag">行业工作流</div>
          <h3>懂你的行业，成品自动送到手里。</h3>
          <p>Tangent 内置对行业流程的理解。你只需上传材料，引擎自动完成处理，把可交付的成品直接送到你手里。</p>
          <div class="lp-tech-flow">
            <div class="lp-tech-flow-step">
              <div class="lp-tech-flow-emoji" aria-hidden="true">📤</div>
              <div class="lp-tech-flow-l">上传材料</div>
            </div>
            <span class="lp-tech-flow-arrow" aria-hidden="true">→</span>
            <div class="lp-tech-flow-step b">
              <div class="lp-tech-flow-emoji" aria-hidden="true">⚙️</div>
              <div class="lp-tech-flow-l">自动处理</div>
            </div>
            <span class="lp-tech-flow-arrow" aria-hidden="true">→</span>
            <div class="lp-tech-flow-step g">
              <div class="lp-tech-flow-emoji" aria-hidden="true">📦</div>
              <div class="lp-tech-flow-l">成品送达</div>
            </div>
          </div>
        </div>

        <!-- 4. Data privacy -->
        <div class="lp-tech-card">
          <div class="lp-tech-tag">数据完全保密</div>
          <h3>你的数据，连我们也看不到。</h3>
          <p>数据在隔离环境中运行，全程加密。我们看不到你上传了什么内容、用了什么技术 —— 数据始终只属于你。</p>
          <div class="lp-tech-priv">
            <span class="lp-tech-priv-i" aria-hidden="true">🔒</span>
            <div class="lp-tech-chips">
              <span class="lp-tech-chip">数据隔离</span>
              <span class="lp-tech-chip">端到端加密</span>
              <span class="lp-tech-chip">零数据留存</span>
            </div>
          </div>
        </div>

      </div>
    </div>
  </section>

  <!-- ENTERPRISE -->
  <section class="lp-ent" id="enterprise" data-reveal>
    <div class="lp-container">
      <div class="lp-eyebrow-c">企业版</div>
      <h2>为必须做对的团队而打造。</h2>
      <div class="lp-ent-grid">
        <div class="lp-ent-tile">SOC 2 Type II</div>
        <div class="lp-ent-tile">SSO / SAML</div>
        <div class="lp-ent-tile">数据隔离</div>
        <div class="lp-ent-tile">私有化 / VPC</div>
        <div class="lp-ent-tile">审计日志</div>
      </div>
    </div>
  </section>

  <!-- CTA -->
  <section class="lp-cta" id="cta" data-reveal>
    <div class="lp-container">
      <div class="lp-cta-card">
        <div class="lp-cta-glow"></div>
        <div class="lp-cta-inner">
          <h2>把你最棘手的问题交给它。</h2>
          <p>用匿名试用免费开始。准备好了，随时接入你自己的模型与数据。</p>
          <div class="lp-cta-actions">
            <a class="lp-btn lp-btn-light" href="/fusion" data-nav>免费开始</a>
            <a class="lp-btn lp-btn-dghost" href="#" id="lpSales">联系销售&nbsp;→</a>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- FOOTER -->
  <footer class="lp-footer">
    <div class="lp-footer-grid">
      <div>
        <a class="lp-footer-logo" href="/" data-nav>${MARK}<span class="lp-logo-name">Tangent&nbsp;AI</span></a>
        <p class="lp-footer-tag">把 token 变成企业可信赖的工作流产品。</p>
      </div>
      <div class="lp-footer-col">
        <span class="h">产品</span>
        <a href="#fusion">Fusion</a>
        <a href="#workflows">工作流</a>
        <a href="#trace">溯源</a>
        <a href="#tech">技术</a>
      </div>
      <div class="lp-footer-col">
        <span class="h">公司</span>
        <a href="#">关于我们</a>
        <a href="#">招聘</a>
        <a href="#">联系我们</a>
      </div>
      <div class="lp-footer-col">
        <span class="h">法律</span>
        <a href="#">隐私</a>
        <a href="#">条款</a>
        <a href="#">安全</a>
      </div>
    </div>
    <div class="lp-footer-bar">
      <span>© 2026 Tangent AI. 每个数字可溯源。</span>
      <span class="lp-float-s">状态：所有系统正常运行</span>
    </div>
  </footer>

</div>`;
}

export async function mount(el) {
  el.innerHTML = html();
  const root = el.querySelector("#lpRoot");

  // 登录 → the shared auth modal (lives outside this view, in #modalRoot).
  const login = el.querySelector("#lpLogin");
  if (login) login.addEventListener("click", () => openLoginModal());

  // 联系销售 → reuse the shell's 联系支持 popover (not hidden on this route).
  const sales = el.querySelector("#lpSales");
  if (sales) sales.addEventListener("click", (e) => {
    e.preventDefault();
    const fab = document.getElementById("supportFab");
    if (fab) fab.click();
  });

  // Scroll-reveal — port of the design's IntersectionObserver. Armed only here, so
  // a no-JS / unsupported browser keeps everything visible (.reveal-on absent).
  let io = null;
  if (root && "IntersectionObserver" in window) {
    root.classList.add("reveal-on");
    io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
      });
    }, { threshold: 0.12 });
    root.querySelectorAll("[data-reveal]").forEach((node) => io.observe(node));
  }

  return () => { if (io) io.disconnect(); };
}
