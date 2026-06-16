// ===========================================================================
// devices.js — 桌面版与设备 (Desktop & Devices). The customer surface for the
// locally-deployable agents: get the desktop app, pair this account to a machine
// (one-time code), and manage / revoke paired devices.
//
// Pairing model (app.py /api/devices/*): the web app mints a one-time code (shown
// ONCE, 10-min, single-use); the desktop daemon redeems it for a per-device token
// (that exchange happens IN the daemon, never here). Device list is field-stripped
// — no token, no ¥, no model id ever reaches this page.
// ===========================================================================
import * as api from "../api.js";
import { esc, pageHead, skeletonCard } from "../components.js";
import { renderErrorState } from "../errors.js";

const PLATFORM = { darwin: "macOS", win: "Windows", linux: "Linux" };

function devPill(status) {
  const map = {
    active: ["已激活", "pill-done"],
    pending: ["待配对", "pill-running"],
    revoked: ["已撤销", "pill-idle"],
  };
  const [label, cls] = map[status] || [status || "未知", "pill-idle"];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function deviceRowHTML(d) {
  const plat = PLATFORM[d.platform] || d.platform || "—";
  const revokable = d.status !== "revoked";
  return `<div class="dev-row">
    <div class="dev-meta">
      <div class="dev-dname">${esc(d.name || "未命名设备")}</div>
      <div class="dev-sub">${esc(plat)} · 配对于 ${esc(fmtDate(d.created_at))}${d.last_seen_at ? ` · 最近活跃 ${esc(fmtDate(d.last_seen_at))}` : ""}</div>
    </div>
    ${devPill(d.status)}
    ${revokable ? `<button type="button" class="ghost-btn dev-revoke" data-id="${esc(d.id)}">撤销</button>` : ""}
  </div>`;
}

function deviceListHTML(devices) {
  if (!devices.length) {
    return `<div class="empty">
      <div class="empty-icon">💻</div>
      <div class="empty-title">还没有配对的设备</div>
      <p class="empty-lead">在你的电脑上安装并打开桌面版，点「生成配对码」拿到验证码，输入到桌面版里即可完成配对。</p>
    </div>`;
  }
  return devices.map(deviceRowHTML).join("");
}

export async function mount(el, ctx) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("桌面版与设备", "把智能体装到你自己的电脑上，就地处理本地文件 —— 文件不必先上传。")}
    <div id="devBody">${skeletonCard({ lines: 4 })}</div>
  </div>`;
  const body = el.querySelector("#devBody");

  let data;
  try {
    data = await api.getDevices();
  } catch (e) {
    renderErrorState(body, e, () => mount(el, ctx));
    return;
  }
  if (!body.isConnected) return;

  const devices = data.devices || [];
  const privacy = data.privacy_notice || "";

  body.innerHTML = `
    <!-- 1) Get the desktop app -->
    <div class="card">
      <h3 class="card-title">① 获取桌面版</h3>
      <p class="muted" style="margin:0 0 4px">桌面版会在你电脑后台运行一个小程序（系统托盘里有图标），
        在本机打开你的智能体面板，并就地读取 / 生成文件。</p>
      <div class="dev-hero-actions">
        <button type="button" class="run-btn" id="devGetApp">如何获取桌面版</button>
      </div>
      <div class="dev-steps" id="devSteps" hidden>
        <strong>内测阶段：从源码本机运行（5 分钟，免费）</strong>
        <ol>
          <li>装依赖：<code>pip install -r requirements-desktop.txt</code></li>
          <li>接上模型（任选其一）：<code>export ANTHROPIC_API_KEY=…</code>，或先在本页配对后用设备令牌。</li>
          <li>启动：<code>python -m agent_team.runner</code> —— 托盘出现图标，浏览器自动打开本地面板。</li>
        </ol>
        正式的一键安装包（.dmg / .msi，带签名、可自动更新）即将上线。详见仓库内
        <code>docs/desktop/DEPLOY.md</code>。
      </div>
    </div>

    <!-- 2) Pair this account to a machine -->
    <div class="card">
      <h3 class="card-title">② 配对你的电脑</h3>
      <p class="muted" style="margin:0 0 4px">生成一个一次性配对码，输入到桌面版里，即可把这台电脑和你的账户绑定。
        配对码<strong>仅显示一次</strong>、10 分钟内有效。</p>
      <div class="dev-hero-actions">
        <button type="button" class="run-btn" id="devGenCode">生成配对码</button>
      </div>
      <div id="devCodeSlot"></div>
    </div>

    <!-- 3) Manage paired devices -->
    <div class="card">
      <h3 class="card-title">③ 我的设备</h3>
      <div id="devList">${deviceListHTML(devices)}</div>
    </div>

    ${privacy ? `<div class="dev-privacy">🔒 ${esc(privacy)}</div>` : ""}`;

  // ① toggle the "how to get it" steps
  const getApp = body.querySelector("#devGetApp");
  const steps = body.querySelector("#devSteps");
  getApp.addEventListener("click", () => {
    steps.hidden = !steps.hidden;
    getApp.textContent = steps.hidden ? "如何获取桌面版" : "收起";
  });

  // ② generate a one-time pairing code
  const genBtn = body.querySelector("#devGenCode");
  const slot = body.querySelector("#devCodeSlot");
  genBtn.addEventListener("click", async () => {
    genBtn.disabled = true; genBtn.textContent = "生成中…";
    try {
      const out = await api.createPairingCode();
      slot.innerHTML = `<div class="dev-code-box">
        <div class="dev-code">${esc(out.pairing_code)}</div>
        <div class="dev-code-meta">有效期至 ${esc(fmtDate(out.expires_at))} 前后（约 10 分钟）· 仅显示这一次</div>
        <button type="button" class="ghost-btn dev-copy" id="devCopy">复制配对码</button>
      </div>`;
      const copy = slot.querySelector("#devCopy");
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(out.pairing_code); copy.textContent = "✓ 已复制"; }
        catch (e) { copy.textContent = "请手动复制"; }
      });
    } catch (e) {
      slot.innerHTML = `<p class="muted" style="margin-top:10px">${esc(e.message || "生成失败，请重试。")}</p>`;
    }
    genBtn.disabled = false; genBtn.textContent = "再生成一个";
  });

  // ③ revoke a device (instant kill switch)
  body.querySelector("#devList").addEventListener("click", async (e) => {
    const btn = e.target.closest(".dev-revoke");
    if (!btn) return;
    if (!window.confirm("撤销后这台设备将立即失去访问权限，确定吗？")) return;
    btn.disabled = true; btn.textContent = "撤销中…";
    try {
      await api.revokeDevice(btn.dataset.id);
      mount(el, ctx);   // reload the page so the status flips to 已撤销
    } catch (err) {
      btn.disabled = false; btn.textContent = "撤销";
      window.alert(err.message || "撤销失败，请重试。");
    }
  });
}
