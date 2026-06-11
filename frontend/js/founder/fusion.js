// ===========================================================================
// founder/fusion.js — Fusion (ported as-is, dark scope): run one prompt across
// several models, then fuse the single best, fact-checked answer.
// ===========================================================================
import * as api from "../api.js";
import { state, loadFounderCatalog, modelById, textModels, providerLabel } from "../state.js";
import { esc, cny, renderMarkdown } from "../components.js";
import { founderFrame, requireAdmin } from "./common.js";
import { openLoginModal } from "../views/auth.js";

let selected = [];        // ordered model ids in the fusion “council”
let fuser = "auto";       // which model reconciles them
let preset = "custom";    // quality | budget | custom
let prompt = "";          // kept across re-renders
let running = false;
let result = null;        // latest {prompt, answers, fused, cost, balance}
let error = "";

export async function mount(el) {
  if (!requireAdmin(el)) return;
  el.innerHTML = founderFrame(`<p class="muted">加载模型目录…</p>`);
  await loadFounderCatalog();
  render(el);
}

const presetModels = (ids) =>
  ids.filter((id) => { const m = modelById(id); return m && (m.kind || "text") === "text"; });

function render(el) {
  el.innerHTML = founderFrame(bodyHTML());
  el.querySelectorAll(".fusion-preset").forEach((btn) =>
    btn.addEventListener("click", () => {
      preset = btn.dataset.preset;
      if (preset === "quality") {
        selected = presetModels(["claude-opus-4-7", "gpt-5.1", "gemini-2.5-pro"]);
      } else if (preset === "budget") {
        selected = presetModels(["claude-haiku-4-5", "gpt-5-mini", "gemini-2.5-flash"]);
      }
      if (fuser !== "auto" && !selected.includes(fuser)) fuser = "auto";
      render(el);
    })
  );
  el.querySelectorAll(".fc-rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      selected = selected.filter((id) => id !== btn.dataset.id);
      preset = "custom";
      if (fuser === btn.dataset.id) fuser = "auto";
      render(el);
    })
  );
  const add = el.querySelector("#fusionAdd");
  if (add) add.addEventListener("change", () => {
    const id = add.value;
    if (id && !selected.includes(id) && selected.length < 5) {
      selected.push(id);
      preset = "custom";
    }
    render(el);
  });
  const fuserSel = el.querySelector("#fusionFuser");
  if (fuserSel) fuserSel.addEventListener("change", () => { fuser = fuserSel.value; });
  const promptEl = el.querySelector("#fusionPrompt");
  if (promptEl) promptEl.addEventListener("input", () => { prompt = promptEl.value; });
  const runBtn = el.querySelector("#fusionRun");
  if (runBtn) runBtn.addEventListener("click", () => run(el));
  const copy = el.querySelector("#fusionCopy");
  if (copy) copy.addEventListener("click", () => {
    const txt = (result && result.fused && result.fused.text) || "";
    navigator.clipboard.writeText(txt).then(() => {
      copy.textContent = "Copied!";
      setTimeout(() => (copy.textContent = "Copy"), 1500);
    });
  });
}

function bodyHTML() {
  if (!state.models.length) {
    return `<h2 class="detail-h">Fusion <span class="beta-badge">beta</span></h2>
      <p class="muted">模型目录尚未加载。</p>`;
  }
  if (!selected.length) selected = textModels().slice(0, 3).map((m) => m.id);

  const chips = selected.map((id) => {
    const m = modelById(id);
    return `<span class="fusion-chip">
      <span class="fc-label">${esc(m ? m.label : id)}</span>
      <button type="button" class="fc-rm" data-id="${esc(id)}" title="Remove" aria-label="Remove">&times;</button>
    </span>`;
  }).join("");

  const addable = textModels().filter((m) => !selected.includes(m.id));
  let addControl;
  if (selected.length >= 5) {
    addControl = `<span class="fusion-addfull">Max 5 models</span>`;
  } else if (!addable.length) {
    addControl = `<span class="fusion-addfull">All models added</span>`;
  } else {
    const opts = addable.map((m) =>
      `<option value="${esc(m.id)}">${esc(m.label)} · ${esc(providerLabel(m.provider))}</option>`).join("");
    addControl = `<select id="fusionAdd" class="fusion-add"><option value="">+ Add model</option>${opts}</select>`;
  }

  const fuserOpts = [`<option value="auto" ${fuser === "auto" ? "selected" : ""}>Auto (best available)</option>`]
    .concat(selected.map((id) => {
      const m = modelById(id);
      return `<option value="${esc(id)}" ${fuser === id ? "selected" : ""}>${esc(m ? m.label : id)}</option>`;
    })).join("");

  const presetChip = (key, label) =>
    `<button type="button" class="fusion-preset ${preset === key ? "on" : ""}" data-preset="${key}">${label}</button>`;

  const err = error ? `<p class="form-error" style="display:block">${esc(error)}</p>` : "";
  const results = running
    ? `<div class="fusion-running"><span class="spinner"></span> Running ${selected.length} model${selected.length === 1 ? "" : "s"} &amp; fusing the best answer…</div>`
    : (result ? resultsHTML(result) : "");

  return `
    <h2 class="detail-h">Fusion <span class="beta-badge">beta</span></h2>
    <p class="detail-lead">Run multiple models side-by-side on the same prompt, then fuse them into one best, fact-checked answer. When one model gets something wrong, the others can catch it.</p>
    <div class="detail-section">
      <div class="fusion-presets">
        ${presetChip("quality", "Quality")}
        ${presetChip("budget", "Budget")}
        ${presetChip("custom", "Custom")}
      </div>
      <div class="fusion-models">${chips}${addControl}</div>
      <div class="fusion-fuse-row">
        <label class="fusion-fuse-label" for="fusionFuser">Fuse with</label>
        <select id="fusionFuser" class="fusion-fuser">${fuserOpts}</select>
      </div>
      <div class="fusion-compose">
        <textarea id="fusionPrompt" class="fusion-prompt" rows="4" placeholder="Ask anything — every selected model answers, then they're fused into one best result.">${esc(prompt)}</textarea>
        <button id="fusionRun" class="run-btn" type="button" ${running ? "disabled" : ""}>${running ? "Running…" : "Run fusion"}</button>
      </div>
      ${err}
    </div>
    ${results}`;
}

function resultsHTML(r) {
  const answers = r.answers || [];
  const fused = r.fused;
  const fusedHTML = fused
    ? `<div class="fusion-final">
        <div class="ff-head">
          <span class="ff-title">Fused answer</span>
          <span class="ff-by">${esc(fused.note || "")}${fused.by_label ? " · by " + esc(fused.by_label) : ""}</span>
          <button id="fusionCopy" class="copy-btn">Copy</button>
        </div>
        <div class="report-body ff-body">${renderMarkdown(fused.text || "")}</div>
      </div>`
    : `<div class="fusion-final empty"><p class="muted">No model returned an answer. Add a provider's key to your <code>.env</code> and try again.</p></div>`;

  const billing = (r.cost != null)
    ? `<div class="fusion-billing">This run cost <strong>${cny(r.cost, 4)}</strong>${r.balance != null ? ` · balance left <strong>${cny(r.balance)}</strong>` : ""}</div>`
    : "";

  const cards = answers.map((a) => {
    const ok = a.ok;
    const inner = ok
      ? `<div class="report-body fr-text">${renderMarkdown(a.text || "")}</div>`
      : `<div class="fr-error">⚠ ${esc(a.error || "Unavailable")}</div>`;
    return `<div class="fusion-answer ${ok ? "" : "failed"}">
      <div class="fa-head">
        <span class="fa-label">${esc(a.label)}</span>
        <span class="fa-state ${ok ? "ok" : "bad"}">${ok ? "answered" : "unavailable"}</span>
      </div>
      ${inner}
    </div>`;
  }).join("");

  return `
    <div class="detail-section fusion-results">
      ${fusedHTML}
      ${billing}
      <div class="block-title" style="margin-top:22px;">Each model's answer (${answers.length})</div>
      <div class="fusion-answers">${cards}</div>
    </div>`;
}

async function run(el) {
  const promptEl = el.querySelector("#fusionPrompt");
  if (promptEl) prompt = promptEl.value;
  const text = (prompt || "").trim();
  error = "";
  if (!text) { error = "Type a question or task to run."; render(el); return; }
  if (!selected.length) { error = "Pick at least one model."; render(el); return; }

  running = true;
  result = null;
  render(el);
  try {
    result = await api.runFusion({ prompt: text, models: selected, fuser });
  } catch (e) {
    error = e.message || "Fusion failed.";
    if (e.status === 401) openLoginModal();
  } finally {
    running = false;
    render(el);
  }
}
