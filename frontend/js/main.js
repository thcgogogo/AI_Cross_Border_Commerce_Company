// ===========================================================================
// main.js — boot, path router (History API) and shell wiring (plan §5).
//
// Customer IA: the whole data→report→deliver workflow on ONE page ("数据分析+报告
// 生成", views/workflow.js) under the "工作流" tab — plus, for is_admin accounts only,
// the 内部工具 section with the five machinery views and the /admin pages. Logged-out
// visitors see the marketing landing.
// ===========================================================================
import { state, bus, isAdmin, loadMeta, loadMe, loadChangelogState } from "./state.js";
import { esc } from "./components.js";
import * as api from "./api.js";
import * as auth from "./views/auth.js";
import * as onboarding from "./views/onboarding.js";
import * as fusion from "./views/fusion.js";
import * as reportView from "./views/report_view.js";
import * as workflow from "./views/workflow.js";
import * as recon from "./views/recon.js";
import * as fTeam from "./founder/team.js";
import * as fCosts from "./founder/costs.js";
import * as fModels from "./founder/models.js";
import * as fFusion from "./founder/fusion.js";
import * as fUnified from "./founder/unified.js";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Navigation model
// ---------------------------------------------------------------------------
// The report/analytics product — the whole data→report→deliver workflow now lives on
// ONE page ("数据分析+报告生成") under the "工作流" nav tab. The old per-view paths
// (/new /reports /schedule /brain /account) still route to that page with the matching
// in-page tab pre-selected (see ROUTES + views/workflow.js), so deep links keep working.
const WORKFLOW_NAV = [
  { path: "/agents", label: "数据分析+报告生成" },
  { path: "/recon", label: "对账核对" },
];
// Every sub-path the one workflow page owns — used to light the 工作流 nav as active.
const AGENTS_PATHS = ["/agents", "/new", "/reports", "/schedule", "/brain", "/account",
  "/recon"];
const FOUNDER_NAV = [
  { path: "/founder/team", label: "团队与模型" },
  { path: "/founder/costs", label: "成本控制台" },
  { path: "/founder/models", label: "模型与定价" },
  { path: "/founder/api", label: "开发者接口" },
];
// Note: Fusion is now the PUBLIC landing ("/"), so it's dropped from 内部工具. The
// legacy founder /founder/fusion route + /api/fusion endpoint remain for direct access.

const ROUTES = [
  { pattern: /^\/$/, view: () => fusion },                       // public Fusion chat (the landing)
  // The whole report workflow on one page; each legacy path selects its in-page tab.
  { pattern: /^\/agents$/, view: () => workflow, auth: true, params: () => ({ tab: "home" }) },
  { pattern: /^\/new$/, view: () => workflow, auth: true, params: () => ({ tab: "generate" }) },
  { pattern: /^\/reports$/, view: () => workflow, auth: true, params: () => ({ tab: "history" }) },
  { pattern: /^\/reports\/(\d+)$/, view: () => reportView, auth: true, params: (m) => ({ runId: m[1] }) },
  { pattern: /^\/recon$/, view: () => recon, auth: true, params: () => ({}) },
  { pattern: /^\/recon\/(\d+)$/, view: () => recon, auth: true, params: (m) => ({ runId: m[1] }) },
  { pattern: /^\/schedule$/, view: () => workflow, auth: true, params: () => ({ tab: "schedule" }) },
  { pattern: /^\/brain$/, view: () => workflow, auth: true, params: () => ({ tab: "brain" }) },
  { pattern: /^\/account$/, view: () => workflow, auth: true, params: () => ({ tab: "account" }) },
  { pattern: /^\/founder\/team$/, view: () => fTeam, auth: true },
  { pattern: /^\/founder\/costs$/, view: () => fCosts, auth: true },
  { pattern: /^\/founder\/models$/, view: () => fModels, auth: true },
  { pattern: /^\/founder\/fusion$/, view: () => fFusion, auth: true },
  { pattern: /^\/founder\/api$/, view: () => fUnified, auth: true },
];

let cleanup = null;   // the mounted view's teardown (timers etc.)
let renderSeq = 0;    // guards against out-of-order async mounts

export function navigate(path, { replace = false } = {}) {
  if (location.pathname !== path) {
    history[replace ? "replaceState" : "pushState"]({}, "", path);
  }
  closeNavSheet();
  render();
}

async function render() {
  const seq = ++renderSeq;
  const el = $("view");
  if (typeof cleanup === "function") { try { cleanup(); } catch (e) { /* view teardown */ } }
  cleanup = null;

  let path = location.pathname;
  if (path === "/company-brain") { // legacy deep link → the new 企业大脑 path
    history.replaceState({}, "", "/brain");
    path = "/brain";
  }
  if (path === "/login") path = "/"; // the landing renders; boot opens the modal

  let matched = null;
  let params = {};
  for (const r of ROUTES) {
    const m = path.match(r.pattern);
    if (m) { matched = r; params = r.params ? r.params(m) : {}; break; }
  }

  renderNav();
  window.scrollTo(0, 0);

  if (!matched) {
    el.innerHTML = `<div class="page page-narrow">
      <div class="empty" style="margin-top:40px;">
        <div class="empty-icon">🧭</div>
        <div class="empty-title">页面不存在</div>
        <p class="empty-lead">这个地址没有对应的页面。</p>
        <button type="button" class="run-btn" id="nfHome">回到首页</button>
      </div>
    </div>`;
    el.querySelector("#nfHome").addEventListener("click", () => navigate("/"));
    return;
  }

  if (matched.auth && !state.account) {
    // Logged-out hit on an Agents (report-product) route: show the section's sign-in
    // intro. A deep feature route also pops the login modal (they meant to use it);
    // the /agents section landing lets them read first.
    mountAgentsGate(el);
    if (path !== "/agents") auth.openLoginModal();
    return;
  }

  const mod = matched.view();
  const ret = await mod.mount(el, { params, navigate });
  if (seq === renderSeq) cleanup = (typeof ret === "function") ? ret : null;
}

// Logged-out intro for the Agents (report) product. The full report-marketing page
// was retired when Fusion became the landing; this is the concise sign-in gate.
function mountAgentsGate(el) {
  el.innerHTML = `<div class="page page-narrow">
    <div class="agents-gate">
      <div class="empty-icon">🤖</div>
      <h1>AI 经营分析团队</h1>
      <p class="empty-lead">一支 AI 团队替你把杂乱的 Excel 变成每周一早上自动送达的、每个数字可溯源的经营周报。
        登录后即可生成报告、导入历史数据、设置定时送达。</p>
      <div class="agents-gate-actions">
        <button type="button" class="run-btn" id="agentsSignup">免费体验</button>
        <button type="button" class="btn-ghost" id="agentsLogin">登录</button>
      </div>
      <p class="muted" style="margin-top:18px">想先试试 AI？<a href="/" data-nav>回到 Fusion 对话 →</a></p>
    </div>
  </div>`;
  const s = $("agentsSignup"); if (s) s.addEventListener("click", () => auth.openSignupModal());
  const l = $("agentsLogin"); if (l) l.addEventListener("click", () => auth.openLoginModal());
}

// ---------------------------------------------------------------------------
// Shell: brand, nav (top + mobile sheet), account widget, footer
// ---------------------------------------------------------------------------
function productName() { return (state.meta && state.meta.product_name) || "你的 AI 经营分析团队"; }

function applyMeta() {
  const name = productName();
  const tagline = (state.meta && state.meta.product_tagline) || "";
  document.title = tagline ? `${name} · ${tagline}` : name;
  $("brandName").textContent = name;
  $("brandMark").textContent = name.charAt(0) || "报";
}

function navLinkHTML(item, current) {
  const active = item.path === "/" ? current === "/" : current.startsWith(item.path);
  return `<a class="nav-link ${active ? "active" : ""}" href="${esc(item.path)}" data-nav>${esc(item.label)}</a>`;
}

function renderNav() {
  const nav = $("mainNav");
  const current = location.pathname;
  // Fusion is the public home — always shown. 工作流 is a dropdown holding the single
  // consolidated workflow page when logged in, a single link (→ the sign-in gate) out.
  let html = navLinkHTML({ path: "/", label: "Fusion" }, current);
  if (state.account) {
    const open = AGENTS_PATHS.some((p) => (p === "/agents" ? current === "/agents" : current.startsWith(p)));
    html += `<div class="nav-drop" id="agentsDrop">
      <button type="button" class="nav-link ${open ? "active" : ""}" id="agentsDropBtn"
        aria-haspopup="true" aria-expanded="false">工作流 <span class="nav-drop-caret">▾</span></button>
      <div class="nav-drop-menu">${WORKFLOW_NAV.map((i) => `<a class="nav-link ${open ? "active" : ""}" href="${esc(i.path)}" data-nav>${esc(i.label)}</a>`).join("")}</div>
    </div>`;
  } else {
    html += navLinkHTML({ path: "/agents", label: "工作流" }, current);
  }
  if (isAdmin()) {
    const open = current.startsWith("/founder");
    html += `<div class="nav-drop" id="founderDrop">
      <button type="button" class="nav-link ${open ? "active" : ""}" id="founderDropBtn"
        aria-haspopup="true" aria-expanded="false">内部工具 <span class="nav-drop-caret">▾</span></button>
      <div class="nav-drop-menu">
        ${FOUNDER_NAV.map((i) => navLinkHTML(i, current)).join("")}
        <a class="nav-link" href="/admin">运营后台 ↗</a>
      </div>
    </div>`;
  }
  nav.innerHTML = html;
  // Always available — the top nav (Fusion + Agents) is hidden by CSS at mobile
  // widths, so the hamburger must work even logged out (CSS hides it on desktop).
  $("navToggle").hidden = false;
  wireDrop("agentsDrop", "agentsDropBtn");
  wireDrop("founderDrop", "founderDropBtn");
  renderNavSheet();
}

function wireDrop(dropId, btnId) {
  const drop = $(dropId);
  if (!drop) return;
  $(btnId).addEventListener("click", (e) => {
    e.stopPropagation();
    drop.classList.toggle("open");
    $(btnId).setAttribute("aria-expanded", drop.classList.contains("open") ? "true" : "false");
  });
}

function renderNavSheet() {
  const sheet = $("navSheet");
  const current = location.pathname;
  let html = navLinkHTML({ path: "/", label: "Fusion" }, current);
  if (state.account) {
    html += `<div class="nav-sheet-group">工作流</div>` + WORKFLOW_NAV.map((i) => navLinkHTML(i, current)).join("");
  } else {
    html += navLinkHTML({ path: "/agents", label: "工作流" }, current);
  }
  html += `<div class="nav-sheet-group">帮助</div>
    <a class="nav-link" href="/help">帮助中心</a>
    <a class="nav-link" href="/changelog">更新日志${state.changelogHasNew ? '<span class="menu-badge">新功能</span>' : ""}</a>`;
  if (isAdmin()) {
    html += `<div class="nav-sheet-group">内部工具 <span class="nav-founder-tag">FOUNDER</span></div>`
      + FOUNDER_NAV.map((i) => navLinkHTML(i, current)).join("")
      + `<a class="nav-link" href="/admin">运营后台 ↗</a>`;
  }
  sheet.innerHTML = html;
}

function openNavSheet() {
  $("navSheetBackdrop").hidden = false;
  requestAnimationFrame(() => {
    $("navSheetBackdrop").classList.add("show");
    $("navSheet").classList.add("open");
  });
  $("navSheet").setAttribute("aria-hidden", "false");
  $("navToggle").setAttribute("aria-expanded", "true");
}

function closeNavSheet() {
  const backdrop = $("navSheetBackdrop");
  const sheet = $("navSheet");
  sheet.classList.remove("open");
  backdrop.classList.remove("show");
  sheet.setAttribute("aria-hidden", "true");
  $("navToggle").setAttribute("aria-expanded", "false");
  setTimeout(() => { if (!sheet.classList.contains("open")) backdrop.hidden = true; }, 220);
}

function renderFooter() {
  const byline = (state.meta && state.meta.product_byline)
    || `由 ${productName()} AI 团队生成 · 每个数字可溯源`;
  $("footer").innerHTML = `
    <div class="footer-brandline">${esc(byline)}</div>
    <div>
      <a href="/help">帮助中心</a>·
      <a href="/changelog">更新日志${state.changelogHasNew ? '<span class="menu-badge">新功能</span>' : ""}</a>
    </div>`;
}

function renderShell() {
  applyMeta();
  renderNav();
  auth.renderAccountWidget();
  renderFooter();
}

// ---------------------------------------------------------------------------
// 联系支持 — floating button + popover (ported; needs META)
// ---------------------------------------------------------------------------
function wireSupport() {
  const fab = $("supportFab");
  const pop = $("supportPop");
  if (!fab || !pop) return;
  const email = (state.meta && state.meta.support_email) || "";
  const qrUrl = (state.meta && state.meta.support_qr_url) || "";
  const qr = qrUrl
    ? `<img class="support-qr-img" src="${esc(qrUrl)}" alt="企业微信群二维码">`
    : `<div class="support-qr-ph">企业微信群二维码<br>(待配置)</div>`;
  const mail = email
    ? `<p class="support-mail">邮件：<a href="mailto:${esc(email)}">${esc(email)}</a></p>` : "";
  pop.innerHTML = `
    <h3>联系支持</h3>
    <p class="muted" style="margin:0 0 4px">扫码加入企业微信支持群：</p>
    ${qr}
    ${mail}
    <textarea id="supportMsg" placeholder="或者在这里留言，我们会尽快回复…"></textarea>
    <input id="supportEmail" type="email" placeholder="你的邮箱（选填，便于回复）">
    <button class="support-send" id="supportSend" type="button">发送留言</button>
    <div class="support-ok" id="supportOk" hidden>已收到，我们会尽快联系你。</div>`;
  fab.hidden = false;
  fab.addEventListener("click", () => pop.classList.toggle("open"));
  $("supportSend").addEventListener("click", async () => {
    const msg = ($("supportMsg").value || "").trim();
    if (!msg) return;
    const send = $("supportSend");
    send.disabled = true; send.textContent = "发送中…";
    try {
      await api.sendSupport({
        message: msg,
        email: ($("supportEmail").value || "").trim(),
        page: location.pathname,
      });
      $("supportOk").hidden = false;
      $("supportOk").textContent = "已收到，我们会尽快联系你。";
      $("supportMsg").value = "";
    } catch (e) {
      $("supportOk").hidden = false;
      $("supportOk").textContent = e.message;
    }
    send.disabled = false; send.textContent = "发送留言";
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  await Promise.all([loadMeta(), loadMe(), loadChangelogState()]);

  auth.init({
    onLogin() {
      if (location.pathname === "/login") history.replaceState({}, "", "/");
      render();
      const wantsDemo = new URLSearchParams(location.search).get("onboarding") === "demo";
      if (wantsDemo) onboarding.openOnboarding({ demo: true });
      else onboarding.maybeAutoOpenOnboarding();
    },
    onLogout() {
      onboarding.closeOnboarding();
      navigate("/", { replace: true });
    },
  });
  onboarding.init();
  renderShell();
  wireSupport();

  // Shell chrome reacts to session / badge changes.
  bus.addEventListener("account", renderShell);
  bus.addEventListener("changelog", () => { renderFooter(); renderNavSheet(); });

  // Mobile sheet + founder dropdown chrome.
  $("navToggle").addEventListener("click", openNavSheet);
  $("navSheetBackdrop").addEventListener("click", closeNavSheet);
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".nav-drop.open").forEach((drop) => {
      if (!drop.contains(e.target)) drop.classList.remove("open");
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("navSheet").classList.contains("open")) closeNavSheet();
  });

  // SPA link navigation (any <a data-nav>).
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-nav]");
    if (!a) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(a.getAttribute("href"));
  });
  window.addEventListener("popstate", render);

  await render();

  // Honour the URL the page was opened at (ported from the old routeFromPath).
  const params = new URLSearchParams(location.search);
  const wantsDemo = params.get("onboarding") === "demo";
  if (location.pathname === "/login" && !state.account) {
    auth.openLoginModal();
  } else if (wantsDemo) {
    if (state.account) onboarding.openOnboarding({ demo: true });
    else auth.openLoginModal();
  } else if (state.account) {
    onboarding.maybeAutoOpenOnboarding(); // first login → the wizard, until done/dismissed
  }
}

boot();
