// ===========================================================================
// views/fusion.js — the PUBLIC landing: a multi-turn Fusion chatbot.
//
// Run one prompt across several models, then fuse the single best, fact-checked
// answer — as a conversation. Anonymous (no login); the server pays. Model NAMES
// are shown by design here (this is the one surface that exposes the "council"),
// but never a ¥ cost. Conversation history lives in this module (client-side), so
// the server stays stateless and the chat survives in-session SPA navigation.
//
// Design ("Consensus", 2026 redesign): the ask box is the hero (ask immediately);
// presets default with the model picker behind a 自定义 reveal; and the page's
// signature is the CONSENSUS STACK — the per-model voices visibly converge into
// one verified fused answer (previously buried in a collapsed <details>).
// ===========================================================================
import * as api from "../api.js";
import { state, loadFusionModels, fusionModelById } from "../state.js";
import { esc, renderMarkdown } from "../components.js";

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
  const conversing = messages.length || sending;
  el.innerHTML = `<div class="fusion-page ${conversing ? "conversing" : "landing"}">
    ${conversing ? threadLayout() : heroLayout()}
  </div>`;
  wire(el, ctx);
  if (conversing) scrollThread(el);
  else { const i = el.querySelector("#fusionInput"); if (i) i.focus(); }
}

// ---- landing (empty) layout: the ask box IS the hero -------------------------
function heroLayout() {
  return `<div class="fusion-hero">
      <div class="fusion-eyebrow">多模型协作 · 一个可信答案</div>
      <h1 class="fusion-headline">问一次，几位顶级模型一起作答</h1>
      <p class="fusion-subhead">一个模型答错，其它模型会纠正它 — 你只看到核验后的结论。</p>
    </div>
    ${composerHTML("问任何问题，例如：帮我分析这段销售数据…")}
    ${controlsHTML()}
    <div class="fusion-examples">
      ${EXAMPLES.map((q) => `<button type="button" class="fusion-example" data-ex="${esc(q)}">${esc(q)}</button>`).join("")}
    </div>
    ${error ? `<div class="fusion-error">${esc(error)}</div>` : ""}
    <p class="fusion-foot muted">融合 ${selected.length} 个模型 · 匿名免费试用，结果由 AI 生成，请自行核实。</p>`;
}

// ---- conversing layout: thread + a docked composer --------------------------
function threadLayout() {
  return `<div class="fusion-topline">
      <span class="fusion-mark">Model Fusion</span>
      <button type="button" class="fusion-new" data-new>＋ 新对话</button>
    </div>
    <div class="fusion-thread" id="fusionThread">${threadHTML()}</div>
    <div class="fusion-dock">
      ${controlsHTML()}
      ${composerHTML("继续追问…")}
    </div>
    <p class="fusion-foot muted">融合 ${selected.length} 个模型 · 结果由 AI 生成，请自行核实。</p>`;
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
  const reset = el.querySelector(".fusion-new");
  if (reset) reset.addEventListener("click", () => { messages = []; error = ""; render(el, ctx); });

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
      messages.push({ role: "assistant", content: fused.text, by_label: fused.by_label,
        answers: res.answers || [], note: fused.note });
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
