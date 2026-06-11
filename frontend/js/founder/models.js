// ===========================================================================
// founder/models.js — 模型与定价 (the old “Models & Pricing” page, ported
// as-is, dark scope): the catalog grouped by provider with list ¥ pricing,
// function tabs, and the per-model spec sheet.
// ===========================================================================
import { state, loadFounderCatalog, modelsByProvider, modelById, kindMeta, kindLabel, modelKinds, providerLabel } from "../state.js";
import { esc, cny } from "../components.js";
import { founderFrame, requireAdmin } from "./common.js";

let kind = "text";       // active function tab
let selectedId = null;   // model spec-sheet subview ("model:<id>" in the old router)

export async function mount(el) {
  if (!requireAdmin(el)) return;
  selectedId = null;
  el.innerHTML = founderFrame(`<p class="muted">加载模型目录…</p>`);
  await loadFounderCatalog();
  render(el);
}

function render(el) {
  el.innerHTML = founderFrame(selectedId ? modelDetailHTML(selectedId) : listHTML());
  el.querySelectorAll(".fn-tab").forEach((btn) =>
    btn.addEventListener("click", () => { kind = btn.dataset.kind; render(el); })
  );
  el.querySelectorAll(".model-card[data-model]").forEach((card) => {
    const open = () => { selectedId = card.dataset.model; render(el); window.scrollTo(0, 0); };
    card.addEventListener("click", open);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });
  const back = el.querySelector("#mdBack");
  if (back) back.addEventListener("click", () => { selectedId = null; render(el); });
}

function listHTML() {
  if (!state.models.length) {
    return `<h2 class="detail-h">模型与定价</h2><p class="muted">模型目录尚未加载。</p>`;
  }
  const kinds = modelKinds();
  if (!kinds.includes(kind)) kind = kinds[0] || "text";
  const tabs = kinds.map((k) => {
    const n = state.models.filter((m) => (m.kind || "text") === k).length;
    return `<button type="button" class="fn-tab ${k === kind ? "on" : ""}" data-kind="${esc(k)}">
      ${esc(kindLabel(k))}<span class="fn-tab-n">${n}</span></button>`;
  }).join("");

  const items = state.models.filter((m) => (m.kind || "text") === kind);
  const sections = modelsByProvider(items).map((g) => `
    <div class="provider-group">
      <div class="provider-head">
        <span class="provider-name">${esc(g.label)}</span>
        <span class="provider-count">${g.items.length} model${g.items.length === 1 ? "" : "s"}</span>
      </div>
      <div class="model-grid">${g.items.map(modelCardHTML).join("")}</div>
    </div>`).join("");

  const km = kindMeta(kind);
  const lead = (km && km.callable === false)
    ? `${esc(kindLabel(kind))} models are catalog entries priced ${esc(km.unit || "per unit")} — browse their specs here. The chat gateway &amp; Fusion call <strong>Text</strong> models only.`
    : `Text models power the chat gateway, Fusion, and every teammate. Click any model for its full spec sheet.`;
  const rate = state.meta ? `≈¥${state.meta.usd_to_cny}/US$` : "the configured rate";

  return `
    <h2 class="detail-h">模型与定价</h2>
    <p class="detail-lead">${state.models.length} 个模型，${Object.keys(state.providers).length || "多"} 家供应商 — 这是我们的进货价目表（内部 COGS），客户只看到固定套餐价。</p>
    <div class="fn-tabs">${tabs}</div>
    <p class="fn-lead">${lead}</p>
    ${sections}
    <p class="models-foot">Claude models run natively on the official Anthropic API (exact pricing). Every other provider runs through one OpenAI-compatible adapter; prices &amp; ids are best-effort public values (marked “approx”) — confirm with each provider. Prices are each provider's list price converted to ¥ (${rate}). Add a provider's key to your <code>.env</code> to activate its models.</p>`;
}

function modelCardHTML(m) {
  const tier = m.tier ? `<span class="mc-tier">${esc(m.tier)}</span>` : "";
  const approx = m.approx ? `<span class="mc-approx" title="Best-effort public pricing — confirm with the provider">approx</span>` : "";
  const live = m.available ? `<span class="mc-live" title="Live now — this provider's key is configured">live</span>` : "";
  const blurb = m.blurb ? `<p class="mc-blurb">${esc(m.blurb)}</p>` : "";
  const ctx = (m.context && m.context !== "—")
    ? `<div class="mc-ctx"><span class="mc-ctx-label">Context</span><span class="mc-ctx-val">${esc(m.context)}</span></div>`
    : "";
  return `<div class="model-card clickable" role="button" tabindex="0" data-model="${esc(m.id)}">
    <div class="mc-head">
      <span class="mc-name">${esc(m.label)}</span>
      <span class="mc-head-chips">${live}${approx}${tier}</span>
    </div>
    <code class="mc-id">${esc(m.id)}</code>
    ${blurb}
    ${modelPriceHTML(m)}
    ${ctx}
  </div>`;
}

function modelPriceHTML(m) {
  const k = m.kind || "text";
  if (k === "text") {
    return `<div class="mc-prices">
        <div class="mc-price"><span class="mc-price-val">${cny(m.input_per_mtok_cny, 2)}</span><span class="mc-price-label">input / 1M tokens</span></div>
        <div class="mc-price"><span class="mc-price-val">${cny(m.output_per_mtok_cny, 2)}</span><span class="mc-price-label">output / 1M tokens</span></div>
      </div>`;
  }
  const unit = m.price_unit || (kindMeta(k) && kindMeta(k).unit) || "per unit";
  const dp = (m.price_cny != null && m.price_cny < 1) ? 4 : 2;
  return `<div class="mc-prices one">
      <div class="mc-price"><span class="mc-price-val">${cny(m.price_cny, dp)}</span><span class="mc-price-label">${esc(unit)}</span></div>
    </div>`;
}

function modelDetailHTML(id) {
  const m = modelById(id);
  if (!m) {
    return `<button type="button" class="md-back" id="mdBack">&lsaquo; 全部模型</button>
      <h2 class="detail-h">未找到该模型</h2>
      <p class="muted">它已不在目录里。</p>`;
  }
  const k = m.kind || "text";
  const km = kindMeta(k);
  const modalities = km ? `${esc(km.input)} &rarr; ${esc(km.output)}` : "—";
  const availability = m.available
    ? `<span class="md-dot live"></span> Live now — this provider's key is configured`
    : `<span class="md-dot"></span> Needs <code>${esc(m.provider || "this provider")}</code>'s key in <code>.env</code>`;
  const access = (km && km.callable)
    ? `Callable from the chat gateway, Fusion &amp; agent teammates`
    : `Catalog model — not callable from the text chat gateway`;
  const spec = (label, val) => val
    ? `<div class="md-spec"><span class="md-k">${esc(label)}</span><span class="md-v">${val}</span></div>` : "";
  return `
    <button type="button" class="md-back" id="mdBack">&lsaquo; 全部模型</button>
    <div class="md-head">
      <div>
        <h2 class="detail-h" style="margin:0 0 6px;">${esc(m.label)} ${m.approx ? `<span class="mc-approx">approx</span>` : ""}</h2>
        <code class="md-id">${esc(m.id)}</code>
      </div>
      <span class="mc-tier">${esc(m.tier || kindLabel(k))}</span>
    </div>
    ${m.blurb ? `<p class="detail-lead">${esc(m.blurb)}</p>` : ""}
    <div class="detail-section">
      <div class="block-title">Pricing</div>
      ${modelPriceHTML(m)}
    </div>
    <div class="detail-section">
      <div class="block-title">Specifications</div>
      <div class="md-specs">
        ${spec("Provider", esc(providerLabel(m.provider)))}
        ${spec("Function", esc(kindLabel(k)))}
        ${spec("Modalities", modalities)}
        ${spec("Context window", (m.context && m.context !== "—") ? esc(m.context) : "")}
        ${spec("Availability", availability)}
        ${spec("Access", access)}
      </div>
    </div>`;
}
