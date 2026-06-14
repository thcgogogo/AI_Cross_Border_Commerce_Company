// ===========================================================================
// views/fusion.js — the PUBLIC landing: a multi-turn Fusion chatbot.
//
// Run one prompt across several models, then fuse the single best, fact-checked
// answer — as a conversation. Anonymous (no login); the server pays. Model NAMES
// are shown by design here (this is the one surface that exposes the "council"),
// but never a ¥ cost. Conversation history lives in this module (client-side), so
// the server stays stateless and the chat survives in-session SPA navigation.
// ===========================================================================
import * as api from "../api.js";
import { state, loadFusionModels, fusionModelById } from "../state.js";
import { esc, renderMarkdown } from "../components.js";

// Preset councils (filtered to whatever the server actually has keys for, so the
// first run always works regardless of which providers are configured).
const QUALITY = ["claude-opus-4-7", "gpt-5.1", "gemini-2.5-pro", "deepseek-v4-pro", "qwen-max"];
const BUDGET = ["deepseek-v4-flash", "qwen-plus", "gemini-2.5-flash", "gpt-5-mini", "claude-haiku-4-5"];
const MAX_MODELS = 3;

// Module-level state — persists across in-session navigation (like the founder view).
let selected = [];          // ordered model ids in the council
let fuser = "auto";         // which model reconciles them
let preset = "budget";      // quality | budget | custom
let messages = [];          // [{role:"user"|"assistant", content, fused?, answers?}]
let sending = false;
let error = "";
let draft = "";             // composer text, kept across re-renders

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
  el.innerHTML = `<div class="fusion-page"><p class="muted" style="padding:48px 0;text-align:center">加载模型…</p></div>`;
  await loadFusionModels();
  if (!selected.length) applyPreset(preset);
  render(el, ctx);
  return () => {}; // no timers/listeners outside `el`
}

function render(el, ctx) {
  el.innerHTML = `<div class="fusion-page">
    <header class="fusion-hero">
      <h1>Model Fusion <span class="beta-badge">beta</span></h1>
      <p>同一个问题，多个大模型一起回答，再融合成一个最可靠的答案。一个模型答错时，其它模型会纠正它。</p>
    </header>
    ${controlsHTML()}
    <div class="fusion-thread" id="fusionThread">${threadHTML()}</div>
    ${composerHTML()}
  </div>`;
  wire(el, ctx);
  scrollThread(el);
}

// ---- council controls (presets + model chips + fuser) -----------------------
function controlsHTML() {
  if (!state.fusionModels.length) {
    return `<div class="fusion-controls"><p class="muted">模型目录暂不可用，请稍后重试。</p></div>`;
  }
  const presetChip = (k, label) =>
    `<button type="button" class="fusion-preset ${preset === k ? "on" : ""}" data-preset="${k}">${label}</button>`;

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

  return `<div class="fusion-controls">
    <div class="fusion-presets">
      ${presetChip("quality", "高质量")}
      ${presetChip("budget", "经济")}
      ${presetChip("custom", "自定义")}
    </div>
    <div class="fusion-models">${chips}${addControl}</div>
    <div class="fusion-fuse-row">
      <label class="fusion-fuse-label" for="fusionFuser">融合模型</label>
      <select id="fusionFuser" class="fusion-fuser">${fuserOpts}</select>
    </div>
  </div>`;
}

// ---- conversation thread ----------------------------------------------------
function threadHTML() {
  if (!messages.length && !sending) {
    return `<div class="fusion-empty">
      <div class="fusion-empty-icon">✦</div>
      <p>问任何问题 — 选中的每个模型都会作答，然后融合成一个最佳结果。</p>
    </div>`;
  }
  let html = messages.map(turnHTML).join("");
  if (sending) {
    html += `<div class="ft-turn assistant"><div class="ft-bubble ft-thinking">
      <span class="spinner"></span> ${selected.length} 个模型作答中，正在融合…</div></div>`;
  }
  if (error) html += `<div class="fusion-error">${esc(error)}</div>`;
  return html;
}

function turnHTML(m, i) {
  if (m.role === "user") {
    return `<div class="ft-turn user"><div class="ft-bubble">${esc(m.content)}</div></div>`;
  }
  const fused = `<div class="ft-bubble ft-assistant"><div class="report-body">${renderMarkdown(m.content || "")}</div></div>`;
  const answers = (m.answers || []);
  let panel = "";
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
    const by = m.by_label ? ` · 由 ${esc(m.by_label)} 融合` : "";
    panel = `<details class="ft-detail"><summary>各模型的回答（${answers.length}）${by}</summary>
      <div class="fusion-answers">${cards}</div></details>`;
  }
  return `<div class="ft-turn assistant" data-i="${i}">${fused}${panel}</div>`;
}

// ---- composer ---------------------------------------------------------------
function composerHTML() {
  const disabled = sending ? "disabled" : "";
  return `<form class="fusion-composer" id="fusionForm">
    <textarea id="fusionInput" class="fusion-input" rows="1" placeholder="问任何问题…" ${disabled}>${esc(draft)}</textarea>
    <button type="submit" class="fusion-send" id="fusionSend" ${disabled} aria-label="发送">${sending ? "…" : "↑"}</button>
  </form>
  <p class="fusion-foot muted">融合 ${selected.length} 个模型 · 匿名免费试用，结果由 AI 生成，请自行核实。</p>`;
}

// ---- events -----------------------------------------------------------------
function wire(el, ctx) {
  el.querySelectorAll(".fusion-preset").forEach((b) =>
    b.addEventListener("click", () => { preset = b.dataset.preset; if (preset !== "custom") applyPreset(preset); render(el, ctx); }));
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
    // Send only {role, content} (the fused reply is what carries into context).
    const history = messages.map((m) => ({ role: m.role, content: m.content }));
    const res = await api.fusionChat({ messages: history, models: selected, fuser });
    const fused = res.fused;
    if (fused && fused.text) {
      messages.push({ role: "assistant", content: fused.text, by_label: fused.by_label, answers: res.answers || [] });
    } else {
      messages.push({ role: "assistant",
        content: "_没有模型成功作答 — 运营方可能尚未为所选模型配置密钥。换一个模型再试试。_",
        answers: res.answers || [] });
    }
  } catch (e) {
    if (e.status === 429) error = "请求过于频繁，请稍后再试。免费试用有频率限制 — 登录后可获得更高额度。";
    else error = e.message || "运行失败，请重试。";
  } finally {
    sending = false;
    render(el, ctx);
  }
}
