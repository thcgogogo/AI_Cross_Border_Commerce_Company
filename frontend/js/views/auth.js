// ===========================================================================
// auth.js — 登录 / 注册（免费体验）/ 重置密码 modals + the top-right account
// widget. Ported from the old app.js and restyled in Chinese (plan §3 copy
// deck). One deliberate change for the brief: sign-up no longer reveals the
// gateway API key — customers buy reports, not API access; the storefront
// lives in founder mode only (D4).
// ===========================================================================
import * as api from "../api.js";
import { state, setAccount, loadChangelogState } from "../state.js";
import { esc } from "../components.js";

const $ = (id) => document.getElementById(id);

// main.js hands us callbacks so auth changes re-render the shell and kick off
// the post-login flows (onboarding auto-open, /login redirect …).
let onLogin = () => {};
let onLogout = () => {};

export function init(hooks = {}) {
  onLogin = hooks.onLogin || onLogin;
  onLogout = hooks.onLogout || onLogout;
  const root = $("modalRoot");
  root.innerHTML = loginModalHTML() + resetModalHTML() + signupModalHTML();
  wireLoginModal();
  wireResetModal();
  wireSignupModal();
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("signupModal").hidden) return closeSignupModal();
    if (!$("resetModal").hidden) return closeResetModal();
    if (!$("loginModal").hidden) return closeLoginModal();
  });
}

// ---------------------------------------------------------------------------
// Account widget (top-right)
// ---------------------------------------------------------------------------
export function renderAccountWidget() {
  const el = $("account");
  if (!el) return;
  if (state.account) {
    const c = state.account;
    el.innerHTML = `
      <span class="account-name">
        <span class="an-name">${esc(c.name)}</span>
        ${c.email ? `<span class="an-email">${esc(c.email)}</span>` : ""}
      </span>
      <button type="button" class="account-btn" id="acctLogout">退出登录</button>`;
    $("acctLogout").addEventListener("click", doLogout);
  } else {
    el.innerHTML = `
      <button type="button" class="account-btn" id="acctLogin">登录</button>
      <button type="button" class="account-btn primary" id="acctSignup">免费体验</button>`;
    $("acctLogin").addEventListener("click", openLoginModal);
    $("acctSignup").addEventListener("click", openSignupModal);
  }
}

async function doLogout() {
  try { await api.logout(); } catch (e) { /* drop local state regardless */ }
  setAccount(null);
  onLogout();
}

// ---------------------------------------------------------------------------
// Shared modal helpers
// ---------------------------------------------------------------------------
function openModal(id, focusId) {
  const m = $(id);
  if (!m) return;
  m.hidden = false;
  m.setAttribute("aria-hidden", "false");
  if (focusId) setTimeout(() => { const f = $(focusId); if (f) f.focus(); }, 60);
}

function closeModal(id) {
  const m = $(id);
  if (!m) return;
  m.hidden = true;
  m.setAttribute("aria-hidden", "true");
}

function showEl(id, msg) { const el = $(id); if (el) { el.textContent = msg; el.hidden = false; } }
function hideEl(id) { const el = $(id); if (el) { el.hidden = true; el.textContent = ""; } }

// Whether the server requires phone + SMS verification (from /api/meta).
const phoneVerifyOn = () => !state.meta || state.meta.phone_verification !== false;

// Request an SMS code + run the 60s cooldown (server also rate-caps sends).
async function requestSmsCode({ phone, email, purpose, btn, statusEl }) {
  const setStatus = (msg, ok) => {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.hidden = !msg;
    statusEl.style.color = ok ? "var(--green)" : "var(--red)";
  };
  phone = (phone || "").trim();
  email = (email || "").trim();
  if (!phone && !(purpose === "reset" && email)) { setStatus("请输入手机号。", false); return; }
  if (btn) { btn.disabled = true; btn.textContent = "发送中…"; }
  try {
    await api.sendCode({ phone, email, purpose });
    setStatus("验证码已发送，请查收短信（5 分钟内有效）。", true);
    startSendCooldown(btn, 60);
  } catch (e) {
    setStatus(e.message, false);
    if (btn) { btn.disabled = false; btn.textContent = "发送验证码"; }
  }
}

function startSendCooldown(btn, secs) {
  if (!btn) return;
  let left = secs;
  btn.disabled = true;
  btn.textContent = `${left}s`;
  const tick = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(tick);
      btn.disabled = false;
      btn.textContent = "发送验证码";
    } else {
      btn.textContent = `${left}s`;
    }
  }, 1000);
}

// ---------------------------------------------------------------------------
// 登录
// ---------------------------------------------------------------------------
function loginModalHTML() {
  return `
  <div id="loginModal" class="modal" aria-hidden="true" hidden>
    <div class="modal-backdrop" id="loginBackdrop"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-label="登录">
      <div class="modal-head">
        <h2>登录</h2>
        <button id="loginClose" class="modal-close" aria-label="关闭">&times;</button>
      </div>
      <form id="loginForm" autocomplete="on">
        <label class="field-label" for="loginEmail">邮箱</label>
        <input class="modal-input" type="email" id="loginEmail" placeholder="you@company.com" autocomplete="email" />
        <label class="field-label" for="loginPassword">密码</label>
        <input class="modal-input" type="password" id="loginPassword" placeholder="请输入密码" autocomplete="current-password" />
        <p id="loginError" class="form-error" hidden></p>
        <button type="submit" class="run-btn" id="loginSubmit">登录</button>
      </form>
      <p class="modal-foot"><button type="button" class="link-btn" id="loginToReset">忘记密码？</button></p>
      <p class="modal-foot">还没有账户？<button type="button" class="link-btn" id="loginToSignup">免费体验（送 2 份报告）</button></p>
    </div>
  </div>`;
}

function wireLoginModal() {
  $("loginClose").addEventListener("click", closeLoginModal);
  $("loginBackdrop").addEventListener("click", closeLoginModal);
  $("loginForm").addEventListener("submit", (e) => {
    e.preventDefault();
    doLogin($("loginEmail").value, $("loginPassword").value);
  });
  $("loginToSignup").addEventListener("click", () => { closeLoginModal(); openSignupModal(); });
  $("loginToReset").addEventListener("click", () => {
    const email = ($("loginEmail").value || "").trim();
    closeLoginModal();
    openResetModal(email);
  });
}

export function openLoginModal() {
  hideEl("loginError");
  const form = $("loginForm");
  if (form) form.reset();
  openModal("loginModal", "loginEmail");
}

export function closeLoginModal() { closeModal("loginModal"); }

async function doLogin(email, password) {
  email = (email || "").trim();
  hideEl("loginError");
  if (!email || !password) { showEl("loginError", "请输入邮箱和密码。"); return; }
  const btn = $("loginSubmit");
  btn.disabled = true; btn.textContent = "登录中…";
  try {
    const data = await api.login(email, password);
    setAccount(data.customer);
    closeLoginModal();
    loadChangelogState(); // the 新功能 badge is per-account
    onLogin();
  } catch (e) {
    showEl("loginError", e.message || "邮箱或密码不正确。");
  } finally {
    btn.disabled = false; btn.textContent = "登录";
  }
}

// ---------------------------------------------------------------------------
// 注册（免费体验）— phone-verified per the brief; no API-key reveal.
// ---------------------------------------------------------------------------
function signupModalHTML() {
  return `
  <div id="signupModal" class="modal" aria-hidden="true" hidden>
    <div class="modal-backdrop" id="signupBackdrop"></div>
    <div class="modal-card wide" role="dialog" aria-modal="true" aria-label="免费体验">
      <div class="modal-head">
        <h2>免费体验</h2>
        <button id="signupClose" class="modal-close" aria-label="关闭">&times;</button>
      </div>
      <p class="modal-sub">注册即送 2 份免费报告 — 上传数据，AI 团队替你写好经营周报。</p>
      <form id="signupForm" autocomplete="on">
        <div class="modal-grid">
          <div>
            <label class="field-label" for="suName">姓名</label>
            <input class="modal-input" type="text" id="suName" placeholder="王老板" autocomplete="name" />
          </div>
          <div>
            <label class="field-label" for="suCompany">公司 <span class="field-optional">（选填）</span></label>
            <input class="modal-input" type="text" id="suCompany" placeholder="星河跨境贸易" autocomplete="organization" />
          </div>
          <div>
            <label class="field-label" for="suEmail">邮箱</label>
            <input class="modal-input" type="email" id="suEmail" placeholder="you@company.com" autocomplete="email" />
          </div>
          <div>
            <label class="field-label" for="suPassword">密码 <span class="field-optional">（至少 8 位）</span></label>
            <input class="modal-input" type="password" id="suPassword" placeholder="设置密码" autocomplete="new-password" />
          </div>
        </div>
        <div id="suPhoneBlock"></div>
        <p id="signupError" class="form-error" hidden></p>
        <button type="submit" class="run-btn" id="signupSubmit">创建账户</button>
      </form>
      <p class="modal-foot">已有账户？<button type="button" class="link-btn" id="signupToLogin">直接登录</button></p>
    </div>
  </div>`;
}

function phoneBlockHTML() {
  if (!phoneVerifyOn()) return "";
  return `
    <label class="field-label" for="suPhone">手机号</label>
    <input class="modal-input" type="tel" id="suPhone" placeholder="13800000000" autocomplete="tel" />
    <label class="field-label" for="suCode">短信验证码</label>
    <div class="code-row">
      <input class="modal-input code-input" type="text" id="suCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" autocomplete="one-time-code" />
      <button type="button" class="code-btn" id="suSendCode">发送验证码</button>
    </div>
    <p id="suCodeMsg" class="modal-hint" hidden></p>`;
}

function wireSignupModal() {
  $("signupClose").addEventListener("click", closeSignupModal);
  $("signupBackdrop").addEventListener("click", closeSignupModal);
  $("signupToLogin").addEventListener("click", () => { closeSignupModal(); openLoginModal(); });
  $("signupForm").addEventListener("submit", (e) => { e.preventDefault(); doSignup(); });
}

export function openSignupModal() {
  hideEl("signupError");
  const form = $("signupForm");
  if (form) form.reset();
  // The phone block depends on META (loaded by now); render it fresh each open.
  $("suPhoneBlock").innerHTML = phoneBlockHTML();
  const send = $("suSendCode");
  if (send) send.addEventListener("click", () => {
    requestSmsCode({
      phone: ($("suPhone") || {}).value,
      purpose: "signup",
      btn: send,
      statusEl: $("suCodeMsg"),
    });
  });
  openModal("signupModal", "suName");
}

export function closeSignupModal() { closeModal("signupModal"); }

async function doSignup() {
  const name = ($("suName").value || "").trim();
  const company = ($("suCompany").value || "").trim();
  const email = ($("suEmail").value || "").trim();
  const password = $("suPassword").value || "";
  const phone = (($("suPhone") || {}).value || "").trim();
  const code = (($("suCode") || {}).value || "").trim();
  hideEl("signupError");
  if (!name) return showEl("signupError", "请填写姓名。");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return showEl("signupError", "请输入有效的邮箱地址。");
  if (password.length < 8) return showEl("signupError", "密码至少 8 位。");
  if (phoneVerifyOn()) {
    if (!phone) return showEl("signupError", "请输入手机号。");
    if (!code) return showEl("signupError", "请输入收到的短信验证码。");
  }
  const btn = $("signupSubmit");
  btn.disabled = true; btn.textContent = "创建中…";
  try {
    // The server sets the session and (internally) mints a gateway key. The
    // customer UI deliberately ignores the key — we sell reports, not APIs.
    const data = await api.signup({ name, company, email, password, phone, code });
    setAccount(data.customer);
    closeSignupModal();
    loadChangelogState();
    onLogin({ justSignedUp: true });
  } catch (e) {
    showEl("signupError", e.message || "注册失败，请稍后再试。");
  } finally {
    btn.disabled = false; btn.textContent = "创建账户";
  }
}

// ---------------------------------------------------------------------------
// 重置密码（短信验证码）
// ---------------------------------------------------------------------------
function resetModalHTML() {
  return `
  <div id="resetModal" class="modal" aria-hidden="true" hidden>
    <div class="modal-backdrop" id="resetBackdrop"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-label="重置密码">
      <div class="modal-head">
        <h2>重置密码</h2>
        <button id="resetClose" class="modal-close" aria-label="关闭">&times;</button>
      </div>
      <form id="resetForm" autocomplete="off">
        <label class="field-label" for="resetPhone">手机号</label>
        <input class="modal-input" type="tel" id="resetPhone" placeholder="13800000000" autocomplete="tel" />
        <p class="modal-hint">填手机号，或在下方填账户邮箱 — 验证码将发送至账户绑定的手机。</p>
        <label class="field-label" for="resetEmail">邮箱 <span class="field-optional">（可选）</span></label>
        <input class="modal-input" type="email" id="resetEmail" placeholder="you@company.com" autocomplete="email" />
        <label class="field-label" for="resetCode">短信验证码</label>
        <div class="code-row">
          <input class="modal-input code-input" type="text" id="resetCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" autocomplete="one-time-code" />
          <button type="button" class="code-btn" id="resetSendCode">发送验证码</button>
        </div>
        <label class="field-label" for="resetPassword">新密码</label>
        <input class="modal-input" type="password" id="resetPassword" placeholder="至少 8 位" autocomplete="new-password" />
        <p id="resetMsg" class="form-ok" hidden></p>
        <p id="resetError" class="form-error" hidden></p>
        <button type="submit" class="run-btn" id="resetSubmit">重置密码</button>
      </form>
      <p class="modal-foot">想起来了？<button type="button" class="link-btn" id="resetToLogin">返回登录</button></p>
    </div>
  </div>`;
}

function wireResetModal() {
  $("resetClose").addEventListener("click", closeResetModal);
  $("resetBackdrop").addEventListener("click", closeResetModal);
  $("resetToLogin").addEventListener("click", () => { closeResetModal(); openLoginModal(); });
  $("resetSendCode").addEventListener("click", () => {
    hideEl("resetError");
    requestSmsCode({
      phone: ($("resetPhone") || {}).value,
      email: ($("resetEmail") || {}).value,
      purpose: "reset",
      btn: $("resetSendCode"),
      statusEl: $("resetMsg"),
    });
  });
  $("resetForm").addEventListener("submit", (e) => { e.preventDefault(); doResetPassword(); });
}

export function openResetModal(prefillEmail) {
  const form = $("resetForm");
  if (form) form.reset();
  hideEl("resetError"); hideEl("resetMsg");
  if (prefillEmail) $("resetEmail").value = prefillEmail;
  openModal("resetModal", "resetPhone");
}

export function closeResetModal() { closeModal("resetModal"); }

async function doResetPassword() {
  const phone = ($("resetPhone").value || "").trim();
  const email = ($("resetEmail").value || "").trim();
  const code = ($("resetCode").value || "").trim();
  const newPassword = $("resetPassword").value || "";
  hideEl("resetError");
  if (!phone && !email) return showEl("resetError", "请填写手机号或账户邮箱。");
  if (!code) return showEl("resetError", "请输入验证码。");
  if (newPassword.length < 8) return showEl("resetError", "新密码至少 8 位。");
  const btn = $("resetSubmit");
  btn.disabled = true; btn.textContent = "重置中…";
  try {
    await api.resetPassword({ phone, email, code, new_password: newPassword });
    hideEl("resetMsg");
    showEl("resetMsg", "密码已重置，请用新密码登录。");
    setTimeout(() => { closeResetModal(); openLoginModal(); }, 1200);
  } catch (e) {
    showEl("resetError", e.message || "重置失败，请确认信息后重试。");
  } finally {
    btn.disabled = false; btn.textContent = "重置密码";
  }
}
