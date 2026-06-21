// ===========================================================================
// state.js — session / account / catalog cache shared by every view, plus a
// tiny event bus so the shell can re-render when the session changes.
//
// Customer views only ever need `meta` + `account`. The machinery catalogs
// (models / providers / org chart) load lazily, when a founder view (or the
// wizard's live feed, which needs agent display names) first asks for them.
// ===========================================================================
import * as api from "./api.js";

export const state = {
  meta: null,            // /api/meta — product identity, currency, templates…
  account: null,         // /api/me customer view (or null) — carries is_admin
  agents: [],            // org chart (lazily loaded; name_cn drives the feed)
  industries: [],        // industry packs (wizard step 1)
  models: [],            // FULL priced model catalog (founder views only)
  providers: {},         // provider id -> label (founder views only)
  fusionModels: [],      // PUBLIC model list for the Fusion picker (names, no pricing)
  fusionModelCap: 3,     // tier-aware council-size cap (free=3 / paid=5), from the server
  fusionAccountTier: "free", // "free" | "full" — gates premium toggles (web search)
  changelogHasNew: false, // drives the 更新日志 “新功能” badge
};

// Event bus: emits "account" (login/logout), "meta", "changelog".
export const bus = new EventTarget();
export const emit = (name) => bus.dispatchEvent(new Event(name));

export function isAdmin() {
  return !!(state.account && state.account.is_admin);
}

// ---- boot loaders -----------------------------------------------------------
export async function loadMeta() {
  try { state.meta = await api.getMeta(); } catch (e) { state.meta = null; }
  emit("meta");
}

export async function loadMe() {
  try {
    const data = await api.getMe();
    state.account = (data && data.customer) || null;
  } catch (e) {
    state.account = null;
  }
  emit("account");
}

export async function loadChangelogState() {
  try {
    const data = await api.getChangelogState();
    state.changelogHasNew = !!(data && data.has_new);
  } catch (e) {
    state.changelogHasNew = false;
  }
  emit("changelog");
}

export function setAccount(customer) {
  state.account = customer || null;
  emit("account");
}

// ---- lazy catalogs ----------------------------------------------------------
export async function loadAgents(force = false) {
  if (state.agents.length && !force) return state.agents;
  try { state.agents = await api.getAgents(); } catch (e) { /* keep whatever we had */ }
  return state.agents;
}

export async function loadIndustries() {
  if (state.industries.length) return state.industries;
  try { state.industries = await api.getIndustries(); } catch (e) { /* optional */ }
  return state.industries;
}

// Public Fusion picker catalog (names + availability, NO pricing). Separate from the
// founder catalog so the anonymous landing never needs the founder-gated /api/models.
export async function loadFusionModels(force = false) {
  if (state.fusionModels.length && !force) return state.fusionModels;
  try {
    const data = await api.getFusionModels();
    state.fusionModels = (data && data.models) || [];
    if (data && typeof data.model_cap === "number") state.fusionModelCap = data.model_cap;
    if (data && data.account_tier) state.fusionAccountTier = data.account_tier;
  } catch (e) { /* leave empty; the view shows a friendly message */ }
  return state.fusionModels;
}
export const fusionModelById = (id) => state.fusionModels.find((m) => m.id === id) || null;

// Founder views: models + providers + agents together (the old bootstrap set).
export async function loadFounderCatalog() {
  const jobs = [];
  if (!state.models.length) {
    jobs.push(api.getModels().then((m) => { state.models = m || []; }).catch(() => {}));
  }
  if (!Object.keys(state.providers).length) {
    jobs.push(api.getProviders().then((list) => {
      const map = {};
      (list || []).forEach((p) => { map[p.id] = p.label; });
      state.providers = map;
    }).catch(() => {}));
  }
  jobs.push(loadAgents());
  await Promise.all(jobs);
}

// ---- model-catalog helpers (shared by the founder views) ---------------------
export const providerLabel = (id) => state.providers[id] || id || "Other";

export function modelsByProvider(list) {
  const order = [];
  const groups = {};
  for (const m of (list || state.models)) {
    const p = m.provider || "anthropic";
    if (!groups[p]) { groups[p] = []; order.push(p); }
    groups[p].push(m);
  }
  return order.map((p) => ({ provider: p, label: providerLabel(p), items: groups[p] }));
}

export const isTextModel = (m) => (m && (m.kind || "text")) === "text";
export const modelById = (id) => state.models.find((m) => m.id === id) || null;
export const textModels = () => state.models.filter(isTextModel);

export const kindMeta = (k) => (state.meta && state.meta.kinds && state.meta.kinds[k]) || null;
export function kindLabel(k) {
  const km = kindMeta(k);
  return (km && km.label) || (k ? k.charAt(0).toUpperCase() + k.slice(1) : "—");
}
export function modelKinds() {
  const order = (state.meta && state.meta.kind_order) || ["text"];
  const present = new Set(state.models.map((m) => m.kind || "text"));
  const ks = order.filter((k) => present.has(k));
  return ks.length ? ks : ["text"];
}
