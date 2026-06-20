// ===========================================================================
// views/fusion.js — the SIGN-IN-REQUIRED, multi-turn Fusion chatbot.
//
// Run one prompt across several models, then fuse the single best, fact-checked
// answer — as a conversation. Login is required and Fusion is FREE; each chat is a
// persistent, account-owned conversation (stored encrypted server-side), so history
// survives reloads and a sign-out/sign-in. Model NAMES are shown by design here (the
// one surface that exposes the "council"), but never a ¥ cost. A logged-out visitor
// sees a sign-in splash (the front door), not a broken chat.
//
// Design ("Consensus", 2026 redesign): the ask box is the hero (ask immediately);
// presets default with the model picker behind a 自定义 reveal; the page's signature
// is the CONSENSUS STACK — the per-model voices visibly converge into one verified
// fused answer; and a left rail lists the account's saved conversations.
// ===========================================================================
import * as api from "../api.js";
import { state, loadFusionModels, fusionModelById } from "../state.js";
import { esc, renderMarkdown } from "../components.js";
import { openLoginModal } from "./auth.js";

// Preset councils (filtered to whatever the server actually has keys for, so the
// first run always works regardless of which providers are configured).
const QUALITY = ["claude-opus-4-7", "gpt-5.1", "gemini-2.5-pro", "deepseek-v4-pro", "qwen-max"];
const BUDGET = ["deepseek-v4-flash", "qwen-plus", "gemini-2.5-flash", "gpt-5-mini", "claude-haiku-4-5"];
const MAX_MODELS = 3;

// A few concrete first questions for the boss audience (beat the blank page).
const EXAMPLES = ["帮我分析这季度的销售数据", "写一封专业的催款邮件", "解释一下这条合同条款"];

// Module-level state — persists across in-session navigation (like the founder view).
let selected = [];          // ordered model ids in the council
let fuser = "auto";         // which model reconciles them
let preset = "budget";      // quality | budget | custom
let advanced = false;       // is the model picker revealed?
let messages = [];          // [{role:"user"|"assistant", content, by_label?, answers?, note?}]
let sending = false;
let error = "";
let draft = "";             // composer text, kept across re-renders
let conversations = [];     // the account's saved conversations (rail) [{id,title,...}]
let conversationId = null;  // the open conversation's id (null = a fresh, unsaved chat)
let loadingConvo = false;   // rehydrating a conversation from the server
let lastAccountId = null;   // reset the open chat when the signed-in account changes
// Freemium teaser (anonymous only): a few free turns, then login is required.
const FREE_ANON_TURNS = 3;  // initial display only; the server is authoritative
let freeRemaining = FREE_ANON_TURNS;
let gated = false;          // anonymous free quota exhausted → show the login gate

const available = () => state.fusionModels.filter((m) => m.available);

function applyPreset(key) {
  const ids = key === "quality" ? QUALITY : BUDGET;
  let pick = ids.filter((id) => { const m = fusionModelById(id); return m && m.available; }).slice(0, MAX_MODELS);
  if (!pick.length) pick = available().slice(0, MAX_MODELS).map((m) => m.id);
  if (!pick.length) pick = state.fusionModels.slice(0, MAX_MODELS).map((m) => m.id); // all keyless: still show
  selected = pick;
  if (fuser !== "auto" && !selected.includes(fuser)) fuser = "auto";
}

export async function mount(el, ctx) {
  el.innerHTML = `<div class="fusion-page"><p class="muted" style="padding:48px 0;text-align:center">加载…</p></div>`;
  await loadFusionModels();
  if (!selected.length) applyPreset(preset);
  if (state.account) {
    // Reset the open chat when the signed-in account changes (no cross-account leakage).
    const aid = state.account.email || state.account.id || "?";
    if (aid !== lastAccountId) {
      lastAccountId = aid; conversationId = null; messages = []; gated = false;
    }
    await refreshConversations();
  } else {
    // Anonymous: show the chatbot immediately, but sync the free-turn quota so a reload
    // after the quota is spent lands straight on the gate (the server is authoritative).
    await syncAnonQuota();
  }
  render(el, ctx);
  return () => {}; // no timers/listeners outside `el`
}

async function syncAnonQuota() {
  try {
    const q = await api.getFusionQuota();
    if (q && typeof q.free_remaining === "number") {
      freeRemaining = q.free_remaining;
      gated = freeRemaining <= 0;   // server says the free quota is spent → gate
    }
  } catch (e) { /* default to the optimistic FREE_ANON_TURNS */ }
}

function render(el, ctx) {
  const signedIn = !!state.account;
  const conversing = messages.length || sending;
  const main = loadingConvo
    ? `<div class="fusion-loading muted"><span class="spinner"></span> 加载对话…</div>`
    : (conversing ? threadLayout() : heroLayout());
  if (signedIn) {
    el.innerHTML = `<div class="fusion-page ${conversing ? "conversing" : "landing"} signed-in">
      <div class="fusion-app">
        ${railHTML()}
        <div class="fusion-main">${main}</div>
      </div>
    </div>`;
    wireRail(el, ctx);
  } else {
    // Anonymous: the chatbot front door — no rail; a login gate replaces the composer
    // once the free turns are spent.
    el.innerHTML = `<div class="fusion-page ${conversing ? "conversing" : "landing"}">
      <div class="fusion-main">${main}</div>
    </div>`;
  }
  wire(el, ctx);
  wireGate(el, ctx);
  if (conversing && !loadingConvo) scrollThread(el);
  else { const i = el.querySelector("#fusionInput"); if (i) i.focus(); }
}

// ---- the login gate (anonymous, free quota spent) + the input area switch -----
function gateHTML() {
  return `<div class="fusion-gate">
      <p class="fusion-gate-title">免费体验已用完</p>
      <p class="fusion-gate-sub">登录后免费继续对话，并自动保存你的每段记录。</p>
      <button type="button" class="fusion-splash-btn" id="fusionGateLogin">登录 / 免费注册</button>
    </div>`;
}

// The composer, or — for an anonymous visitor who has spent the free turns — the gate.
function inputAreaHTML(placeholder) {
  if (!state.account && gated) return gateHTML();
  return composerHTML(placeholder);
}

function wireGate(el, ctx) {
  const g = el.querySelector("#fusionGateLogin");
  if (g) g.addEventListener("click", () => openLoginModal());
}

// ---- conversation rail (the account's saved chats) --------------------------
function railHTML() {
  const items = conversations.map((c) => {
    const on = c.id === conversationId ? " on" : "";
    const title = c.title || "新对话";
    return `<li class="fusion-convo${on}">
      <button type="button" class="fc-open" data-cid="${c.id}" title="${esc(title)}">${esc(title)}</button>
      <span class="fc-actions">
        <button type="button" class="fc-ren" data-cid="${c.id}" aria-label="重命名" title="重命名">✎</button>
        <button type="button" class="fc-del" data-cid="${c.id}" aria-label="删除" title="删除">×</button>
      </span>
    </li>`;
  }).join("");
  return `<aside class="fusion-rail">
    <button type="button" class="fusion-new-convo" data-new>＋ 新对话</button>
    <ul class="fusion-convos">${items || `<li class="fusion-convos-empty muted">还没有对话</li>`}</ul>
  </aside>`;
}

async function refreshConversations() {
  try {
    const r = await api.getFusionConversations();
    conversations = (r && r.conversations) || [];
  } catch (e) { /* keep the stale list — a rail blip must not break the chat */ }
}

// Map a stored server turn into the view's render shape.
function fromServerMsg(m) {
  if (m.role === "user") return { role: "user", content: m.content || "" };
  const failed = m.status === "failed";
  const fused = m.fused || {};
  return {
    role: "assistant",
    content: failed ? "_本轮所有模型未作答，请重试。_" : (m.content || ""),
    answers: m.answers || [],
    note: fused.note || "",
    by_label: fused.by_label || "",
  };
}

function newConversation(el, ctx) {
  if (sending) return;
  conversationId = null;
  messages = [];
  error = "";
  draft = "";
  render(el, ctx);
}

async function openConversation(el, ctx, id) {
  if (sending || id === conversationId) return;
  loadingConvo = true;
  conversationId = id;
  messages = [];
  error = "";
  render(el, ctx);
  try {
    const r = await api.getFusionConversation(id);
    messages = (r.messages || []).map(fromServerMsg);
  } catch (e) {
    error = (e && e.message) || "加载对话失败。";
    conversationId = null;
  } finally {
    loadingConvo = false;
    render(el, ctx);
  }
}

async function renameConvo(el, ctx, id) {
  const cur = (conversations.find((c) => c.id === id) || {}).title || "";
  const next = window.prompt("重命名对话", cur);
  if (next == null) return;
  const title = next.trim();
  if (!title) return;
  try { await api.renameFusionConversation(id, title); await refreshConversations(); }
  catch (e) { error = (e && e.message) || "重命名失败。"; }
  render(el, ctx);
}

async function deleteConvo(el, ctx, id) {
  if (!window.confirm("删除这个对话？删除后将从列表中移除。")) return;
  try {
    await api.deleteFusionConversation(id);
    if (id === conversationId) { conversationId = null; messages = []; }
    await refreshConversations();
  } catch (e) { error = (e && e.message) || "删除失败。"; }
  render(el, ctx);
}

function wireRail(el, ctx) {
  el.querySelectorAll(".fusion-new-convo").forEach((b) =>
    b.addEventListener("click", () => newConversation(el, ctx)));
  el.querySelectorAll(".fc-open").forEach((b) =>
    b.addEventListener("click", () => openConversation(el, ctx, Number(b.dataset.cid))));
  el.querySelectorAll(".fc-ren").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); renameConvo(el, ctx, Number(b.dataset.cid)); }));
  el.querySelectorAll(".fc-del").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); deleteConvo(el, ctx, Number(b.dataset.cid)); }));
}

// The foot line adapts: signed-in vs the anonymous free-turn meter vs the spent gate.
function footHTML() {
  if (!state.account) {
    if (gated) {
      return `<p class="fusion-foot muted">登录后免费继续 · 多模型协作，结果由 AI 生成请自行核实。</p>`;
    }
    return `<p class="fusion-foot muted">免费体验中 · 还可免费提问 ${freeRemaining} 次 · 登录后无限畅聊并自动保存对话。</p>`;
  }
  return `<p class="fusion-foot muted">融合 ${selected.length} 个模型 · 登录后免费，对话自动保存，结果由 AI 生成请自行核实。</p>`;
}

// ---- landing (empty) layout: the ask box IS the hero -------------------------
function heroLayout() {
  const gateMode = !state.account && gated;
  return `<div class="fusion-hero">
      <div class="fusion-eyebrow">多模型协作 · 一个可信答案</div>
      <h1 class="fusion-headline">问一次，几位顶级模型一起作答</h1>
      <p class="fusion-subhead">一个模型答错，其它模型会纠正它 — 你只看到核验后的结论。</p>
    </div>
    ${inputAreaHTML("问任何问题，例如：帮我分析这段销售数据…")}
    ${gateMode ? "" : controlsHTML()}
    ${gateMode ? "" : `<div class="fusion-examples">
      ${EXAMPLES.map((q) => `<button type="button" class="fusion-example" data-ex="${esc(q)}">${esc(q)}</button>`).join("")}
    </div>`}
    ${error ? `<div class="fusion-error">${esc(error)}</div>` : ""}
    ${footHTML()}`;
}

// ---- conversing layout: thread + a docked composer (or the gate) ------------
function threadLayout() {
  const gateMode = !state.account && gated;
  return `<div class="fusion-topline">
      <span class="fusion-mark">Model Fusion</span>
      ${state.account ? `<button type="button" class="fusion-new" data-new>＋ 新对话</button>` : ""}
    </div>
    <div class="fusion-thread" id="fusionThread">${threadHTML()}</div>
    <div class="fusion-dock">
      ${gateMode ? "" : controlsHTML()}
      ${inputAreaHTML("继续追问…")}
    </div>
    ${footHTML()}`;
}

// ---- council controls (presets + the 自定义 model picker) --------------------
function controlsHTML() {
  if (!state.fusionModels.length) {
    return `<div class="fusion-controls"><p class="muted">模型目录暂不可用，请稍后重试。</p></div>`;
  }
  const presetChip = (k, label) =>
    `<button type="button" class="fusion-preset ${preset === k ? "on" : ""}" data-preset="${k}">${label}</button>`;

  let picker = "";
  if (advanced) {
    const chips = selected.map((id) => {
      const m = fusionModelById(id);
      const warn = m && !m.available ? ` title="该模型未配置密钥，本次将返回提示" data-warn="1"` : "";
      return `<span class="fusion-chip${m && !m.available ? " unavail" : ""}"${warn}>
        <span class="fc-label">${esc(m ? m.label : id)}</span>
        <button type="button" class="fc-rm" data-id="${esc(id)}" aria-label="移除">&times;</button>
      </span>`;
    }).join("");
    const addable = state.fusionModels.filter((m) => !selected.includes(m.id));
    let addControl = "";
    if (selected.length >= MAX_MODELS) {
      addControl = `<span class="fusion-addfull">最多 ${MAX_MODELS} 个</span>`;
    } else if (addable.length) {
      const opts = addable.map((m) =>
        `<option value="${esc(m.id)}">${esc(m.label)} · ${esc(m.provider_label || m.provider)}${m.available ? "" : "（未配置）"}</option>`).join("");
      addControl = `<select id="fusionAdd" class="fusion-add"><option value="">+ 添加模型</option>${opts}</select>`;
    }
    const fuserOpts = [`<option value="auto" ${fuser === "auto" ? "selected" : ""}>自动（择优）</option>`]
      .concat(selected.map((id) => {
        const m = fusionModelById(id);
        return `<option value="${esc(id)}" ${fuser === id ? "selected" : ""}>${esc(m ? m.label : id)}</option>`;
      })).join("");
    picker = `<div class="fusion-advanced">
      <div class="fusion-models">${chips}${addControl}</div>
      <div class="fusion-fuse-row">
        <label class="fusion-fuse-label" for="fusionFuser">融合模型</label>
        <select id="fusionFuser" class="fusion-fuser">${fuserOpts}</select>
      </div>
    </div>`;
  }

  return `<div class="fusion-controls">
    <div class="fusion-presets">
      ${presetChip("quality", "高质量")}
      ${presetChip("budget", "经济")}
      <button type="button" class="fusion-adv-toggle ${advanced ? "on" : ""}" data-adv
        aria-expanded="${advanced ? "true" : "false"}">自定义模型 ${advanced ? "▴" : "▾"}</button>
    </div>
    ${picker}
  </div>`;
}

// ---- conversation thread ----------------------------------------------------
function threadHTML() {
  let html = messages.map(turnHTML).join("");
  if (sending) {
    html += `<div class="ft-turn assistant"><div class="cs-thinking">
      <span class="spinner"></span> ${selected.length} 个模型作答中，正在融合…</div></div>`;
  }
  if (error) html += `<div class="fusion-error">${esc(error)}</div>`;
  return html;
}

function excerpt(t, n = 46) {
  const s = String(t || "").replace(/[#>*`_]/g, "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}
// Show the 已纠正 chip only on a real reconciliation signal — never fabricated.
const isCorrected = (note) => /纠正|更正|修正|有误|不一致|分歧/.test(String(note || ""));

function turnHTML(m, i) {
  if (m.role === "user") {
    return `<div class="ft-turn user"><div class="ft-bubble">${esc(m.content)}</div></div>`;
  }
  const answers = m.answers || [];
  const okCount = answers.filter((a) => a.ok).length;

  // The signature: the per-model "voices" converging into one verified answer.
  let voices = "";
  if (answers.length) {
    voices = `<div class="cs-voices">${answers.map((a) => `
      <div class="cs-voice ${a.ok ? "ok" : "off"}">
        <span class="cs-voice-icon" aria-hidden="true">${a.ok ? "✓" : "—"}</span>
        <span class="cs-voice-name">${esc(a.label)}</span>
        <span class="cs-voice-take">${a.ok ? esc(excerpt(a.text)) : "未作答"}</span>
      </div>`).join("")}</div>
      <div class="cs-merge" aria-hidden="true"><span class="cs-merge-label">融合</span></div>`;
  }

  // Full per-model answers stay one tap away (secondary).
  let detail = "";
  if (answers.length) {
    const cards = answers.map((a) => {
      const inner = a.ok
        ? `<div class="report-body fr-text">${renderMarkdown(a.text || "")}</div>`
        : `<div class="fr-error">⚠ ${esc(a.error || "未返回")}</div>`;
      return `<div class="fusion-answer ${a.ok ? "" : "failed"}">
        <div class="fa-head"><span class="fa-label">${esc(a.label)}</span>
          <span class="fa-state ${a.ok ? "ok" : "bad"}">${a.ok ? "已作答" : "不可用"}</span></div>
        ${inner}</div>`;
    }).join("");
    detail = `<details class="ft-detail"><summary>看各模型完整回答（${answers.length}）</summary>
      <div class="fusion-answers">${cards}</div></details>`;
  }

  const corrected = isCorrected(m.note);
  const chips = `<span class="cs-chip cs-chip-ok">✓ ${okCount} 模型已核验</span>`
    + (corrected ? `<span class="cs-chip cs-chip-warn">⚠ 已纠正分歧</span>` : "");

  return `<div class="ft-turn assistant" data-i="${i}">
    <div class="consensus-stack">
      ${voices}
      <div class="cs-fused">
        <div class="cs-fused-head"><span class="cs-fused-title">融合答案</span>${chips}</div>
        <div class="report-body">${renderMarkdown(m.content || "")}</div>
        ${m.note && corrected ? `<div class="cs-note">融合说明：${esc(m.note)}</div>` : ""}
      </div>
      ${detail}
    </div>
  </div>`;
}

// ---- composer ---------------------------------------------------------------
function composerHTML(placeholder) {
  const disabled = sending ? "disabled" : "";
  return `<form class="fusion-composer" id="fusionForm">
    <textarea id="fusionInput" class="fusion-input" rows="1" placeholder="${esc(placeholder || "问任何问题…")}" ${disabled}>${esc(draft)}</textarea>
    <button type="submit" class="fusion-send" id="fusionSend" ${disabled} aria-label="发送">${sending ? "…" : "↑"}</button>
  </form>`;
}

// ---- events -----------------------------------------------------------------
function wire(el, ctx) {
  el.querySelectorAll(".fusion-preset").forEach((b) =>
    b.addEventListener("click", () => { preset = b.dataset.preset; applyPreset(preset); render(el, ctx); }));
  const adv = el.querySelector(".fusion-adv-toggle");
  if (adv) adv.addEventListener("click", () => { advanced = !advanced; render(el, ctx); });

  el.querySelectorAll(".fusion-example").forEach((b) =>
    b.addEventListener("click", () => {
      draft = b.dataset.ex;
      const inp = el.querySelector("#fusionInput");
      if (inp) { inp.value = draft; inp.focus(); autoGrow(inp); }
    }));
  // The topline ＋新对话 button (shown in both anon + signed-in conversing views).
  el.querySelectorAll(".fusion-new").forEach((b) =>
    b.addEventListener("click", () => newConversation(el, ctx)));

  el.querySelectorAll(".fc-rm").forEach((b) =>
    b.addEventListener("click", () => {
      selected = selected.filter((id) => id !== b.dataset.id);
      preset = "custom";
      if (fuser === b.dataset.id) fuser = "auto";
      render(el, ctx);
    }));
  const add = el.querySelector("#fusionAdd");
  if (add) add.addEventListener("change", () => {
    const id = add.value;
    if (id && !selected.includes(id) && selected.length < MAX_MODELS) { selected.push(id); preset = "custom"; }
    render(el, ctx);
  });
  const fuserSel = el.querySelector("#fusionFuser");
  if (fuserSel) fuserSel.addEventListener("change", () => { fuser = fuserSel.value; });

  const input = el.querySelector("#fusionInput");
  if (input) {
    autoGrow(input);
    input.addEventListener("input", () => { draft = input.value; autoGrow(input); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(el, ctx); }
    });
  }
  const form = el.querySelector("#fusionForm");
  if (form) form.addEventListener("submit", (e) => { e.preventDefault(); send(el, ctx); });
}

function autoGrow(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
function scrollThread(el) { const t = el.querySelector("#fusionThread"); if (t) t.scrollTop = t.scrollHeight; }

async function send(el, ctx) {
  if (sending) return;
  if (!state.account && gated) return;   // anonymous free quota spent → must log in
  const input = el.querySelector("#fusionInput");
  if (input) draft = input.value;        // read the live value, not just the mirror
  const text = (draft || "").trim();
  error = "";
  if (!text) return;
  if (!selected.length) { error = "请至少选择一个模型。"; render(el, ctx); return; }

  messages.push({ role: "user", content: text });
  draft = "";
  sending = true;
  render(el, ctx);

  try {
    const res = state.account
      // Signed-in: the SERVER is the source of truth — send the new user `text` + the
      // conversation id (null on the first turn → the server creates one + returns it);
      // prior turns are loaded from the encrypted conversation server-side.
      ? await api.fusionChat({ conversation_id: conversationId, text, models: selected, fuser })
      // Anonymous teaser: send the client-maintained history (no server memory).
      : await api.fusionChat({ messages: messages.map((m) => ({ role: m.role, content: m.content })),
                               models: selected, fuser });
    if (res.conversation_id) conversationId = res.conversation_id;
    const fused = res.fused;
    if (fused && fused.text) {
      messages.push({ role: "assistant", content: fused.text, by_label: fused.by_label,
        answers: res.answers || [], note: fused.note });
    } else {
      messages.push({ role: "assistant",
        content: "_没有模型成功作答 — 运营方可能尚未为所选模型配置密钥。换一个模型再试试。_",
        answers: res.answers || [] });
    }
    if (state.account) {
      await refreshConversations();   // surface the new/updated chat in the rail
    } else if (typeof res.free_remaining === "number") {
      freeRemaining = res.free_remaining;
      if (freeRemaining <= 0) gated = true;   // next turn shows the login gate
    }
  } catch (e) {
    if (e.status === 401 && e.data && e.data.login_required) {
      // anonymous free quota exhausted — drop the unanswered user turn + show the gate
      messages.pop();
      freeRemaining = 0;
      gated = true;
    } else if (e.status === 401) {
      error = "登录状态已过期，请重新登录后继续。";
    } else if (e.status === 429) {
      error = "请求过于频繁，请稍后再试。";
    } else {
      error = e.message || "运行失败，请重试。";
    }
  } finally {
    sending = false;
    render(el, ctx);
  }
}
