// ===========================================================================
// api.js — every server call in one place (extracted from the old app.js).
// All helpers return parsed JSON and throw ApiError(message, status, data)
// on a non-2xx response, so views can show err.message directly.
// ===========================================================================

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

async function request(url, opts = {}) {
  // LOCAL_MODE (desktop daemon): echo the per-session CSRF token the server injected
  // into our loopback HTML on every mutating call, so the daemon accepts it. A
  // cross-origin page can't read this token (CORS), so it can't forge such a call.
  // No-op on the cloud server (window.__LOCAL_TOKEN__ is "").
  if (window.__LOCAL_TOKEN__) {
    const method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      opts.headers = { ...(opts.headers || {}), "X-Tangent-Local-Token": window.__LOCAL_TOKEN__ };
    }
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  if (!res.ok) {
    const msg = (data && data.error) || `请求失败（${res.status}）`;
    throw new ApiError(msg, res.status, data);
  }
  return data;
}

const getJSON = (url) => request(url);
const postJSON = (url, body) => request(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const postForm = (url, formData) => request(url, { method: "POST", body: formData });
const patchJSON = (url, body) => request(url, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const delJSON = (url) => request(url, { method: "DELETE" });

// ---- boot / catalog ---------------------------------------------------------
export const getMeta = () => getJSON("/api/meta");
export const getMe = () => getJSON("/api/me");
export const getAgents = () => getJSON("/api/agents");
export const getModels = () => getJSON("/api/models");
export const getProviders = () => getJSON("/api/providers");
export const getAnalytics = () => getJSON("/api/analytics");
export const getIndustries = () => getJSON("/api/industries");
export const getChangelogState = () => getJSON("/api/changelog/state");

// ---- auth -------------------------------------------------------------------
export const login = (email, password) => postJSON("/api/login", { email, password });
export const logout = () => postJSON("/api/logout");
export const logoutAll = () => postJSON("/api/logout-all");
export const signup = (payload) => postJSON("/api/signup", payload);
export const sendCode = (payload) => postJSON("/api/send-code", payload);
export const resetPassword = (payload) => postJSON("/api/reset-password", payload);

// ---- agent-team runs ----------------------------------------------------------
export const startRun = (formData) => postForm("/api/run", formData);
export const getJobStatus = (jobId) => getJSON(`/api/status/${encodeURIComponent(jobId)}`);
export const setAgentModel = (agentId, model) =>
  postJSON("/api/agents/model", { agent_id: agentId, model });

// ---- reports / feedback / learning loop --------------------------------------
export const getReports = () => getJSON("/api/reports");
export const getReport = (runId) => getJSON(`/api/reports/${encodeURIComponent(runId)}`);
export const sendFeedback = (payload) => postJSON("/api/feedback", payload);
export const saveFeedbackPreference = (id) => postJSON("/api/feedback/save-preference", { id });
export const confirmGlossaryTerms = (answers) => postJSON("/api/glossary/confirm", { answers });

// ---- 对账 / 平台结算核对 (settlement reconciliation) ---------------------------
export const getReconPlatforms = () => getJSON("/api/recon/platforms");
export const startRecon = (formData) => postForm("/api/recon/run", formData);
export const getReconResult = (runId) => getJSON(`/api/recon/result/${encodeURIComponent(runId)}`);
export const getReconHistory = () => getJSON("/api/recon/reports");
export const getReconExceptions = (params = {}) => {
  const q = new URLSearchParams(params).toString();
  return getJSON(`/api/recon/exceptions${q ? "?" + q : ""}`);
};
export const classifyReconException = (excId, label) =>
  postJSON(`/api/recon/exceptions/${encodeURIComponent(excId)}/classify`, { label });
export const reconLedgerUrl = (runId) => `/api/recon/ledger/${encodeURIComponent(runId)}`;

// ---- 应收账款管理 + 催收 (AR aging + dunning) ----------------------------------
export const getArPacks = () => getJSON("/api/ar/packs");
export const startAr = (formData) => postForm("/api/ar/run", formData);
export const getArResult = (runId) => getJSON(`/api/ar/result/${encodeURIComponent(runId)}`);
export const getArHistory = () => getJSON("/api/ar/reports");
export const getArFlags = (params = {}) => {
  const q = new URLSearchParams(params).toString();
  return getJSON(`/api/ar/flags${q ? "?" + q : ""}`);
};
export const classifyArFlag = (flagId, label) =>
  postJSON(`/api/ar/flags/${encodeURIComponent(flagId)}/classify`, { label });
export const arLedgerUrl = (runId) => `/api/ar/ledger/${encodeURIComponent(runId)}`;
export const getArDunning = (params = {}) => {
  const q = new URLSearchParams(params).toString();
  return getJSON(`/api/ar/dunning${q ? "?" + q : ""}`);
};
export const editArDunning = (id, body) =>
  postJSON(`/api/ar/dunning/${encodeURIComponent(id)}/edit`, { body });
export const approveArDunning = (id) =>
  postJSON(`/api/ar/dunning/${encodeURIComponent(id)}/approve`, {});
export const dismissArDunning = (id) =>
  postJSON(`/api/ar/dunning/${encodeURIComponent(id)}/dismiss`, {});
export const sendArDunning = (id, payload) =>
  postJSON(`/api/ar/dunning/${encodeURIComponent(id)}/send`, payload);

// ---- 跨境财务 (cross-border finance hub) — 贸易方式 settings --------------------
export const getCrossBorderSettings = () => getJSON("/api/cross-border/settings");
export const saveCrossBorderSettings = (payload) => postJSON("/api/cross-border/settings", payload);

// ---- 跨境财务 evidence engines (三流合一 / 退税 / 发票审核 / 无票免税) ------------
// One kind-parameterized surface; `kind` ∈ threeflow|rebate|invoice|vatexempt.
export const getCbConfig = (kind) => getJSON(`/api/crossborder/${encodeURIComponent(kind)}/config`);
export const startCb = (kind, formData) => postForm(`/api/crossborder/${encodeURIComponent(kind)}/run`, formData);
export const getCbResult = (kind, runId) =>
  getJSON(`/api/crossborder/${encodeURIComponent(kind)}/result/${encodeURIComponent(runId)}`);
export const getCbHistory = (kind) => getJSON(`/api/crossborder/${encodeURIComponent(kind)}/reports`);
export const getCbFlags = (kind, params = {}) => {
  const q = new URLSearchParams(params).toString();
  return getJSON(`/api/crossborder/${encodeURIComponent(kind)}/flags${q ? "?" + q : ""}`);
};
export const classifyCbFlag = (flagId, label) =>
  postJSON(`/api/crossborder/flags/${encodeURIComponent(flagId)}/classify`, { label });
export const scheduleCb = (kind, formData) =>
  postForm(`/api/crossborder/${encodeURIComponent(kind)}/schedule`, formData);

// ---- token manager (founder 成本控制台) ---------------------------------------
export const getTokens = () => getJSON("/api/tokens");
export const setTokensTotal = (limit) => postJSON("/api/tokens/total", { limit });
export const setTokensBudget = (name, limit) => postJSON("/api/tokens/budget", { name, limit });
export const removeTokensBudget = (name) => postJSON("/api/tokens/budget/remove", { name });
export const resetTokens = () => postJSON("/api/tokens/reset");

// ---- unified API storefront (founder 开发者接口) -------------------------------
export const saveAccountModels = (ids) => postJSON("/api/account/models", { models: ids });
export const runFusion = (payload) => postJSON("/api/fusion", payload);

// ---- public Fusion chat (the landing page) ------------------------------------
export const getFusionModels = () => getJSON("/api/fusion/models");
export const getFusionQuota = () => getJSON("/api/fusion/quota");
export const fusionChat = (payload) => postJSON("/api/fusion/chat", payload);
// Sign-in-required Fusion conversation memory (account-owned, encrypted at rest).
export const getFusionConversations = () => getJSON("/api/fusion/conversations");
export const getFusionConversation = (id) => getJSON(`/api/fusion/conversations/${id}`);
export const createFusionConversation = (body) => postJSON("/api/fusion/conversations", body || {});
export const importFusionConversation = (messages) =>
  postJSON("/api/fusion/conversations/import", { messages });
export const renameFusionConversation = (id, title) =>
  patchJSON(`/api/fusion/conversations/${id}`, { title });
export const pinFusionConversation = (id, pinned) =>
  patchJSON(`/api/fusion/conversations/${id}`, { is_pinned: pinned });
export const getFusionPersonas = () => getJSON("/api/fusion/personas");
export const setFusionPersona = (id, systemPrompt) =>
  patchJSON(`/api/fusion/conversations/${id}`, { system_prompt: systemPrompt });
export const deleteFusionConversation = (id) => delJSON(`/api/fusion/conversations/${id}`);
// R6 hand-off — seed the report team from a conversation; returns {job_id, run_id}.
export const fusionConversationHandoff = (id) =>
  postJSON(`/api/fusion/conversations/${id}/handoff`, {});
export const attachFusionFile = (conversationId, file) => {
  const fd = new FormData();
  fd.append("file", file);
  return postForm(`/api/fusion/conversations/${conversationId}/attach`, fd);
};

// ---- billing ------------------------------------------------------------------
export const getPlans = () => getJSON("/api/plans");
export const getOrders = () => getJSON("/api/orders");
export const payCreate = (payload) => postJSON("/api/pay/create", payload);
export const payStatus = (orderNo) => getJSON(`/api/pay/status/${encodeURIComponent(orderNo)}`);
export const subscriptionCancel = () => postJSON("/api/subscription/cancel");
export const subscriptionResume = () => postJSON("/api/subscription/resume");
export const requestFapiao = (payload) => postJSON("/api/fapiao/request", payload);

// ---- 企业大脑 (company brain) ---------------------------------------------------
export const getBrainSummary = () => getJSON("/api/company-brain/summary");
export const getBrainProfile = () => getJSON("/api/company-brain/profile");
export const saveBrainProfile = (payload) => postJSON("/api/company-brain/profile", payload);
export const getBrainGlossary = () => getJSON("/api/company-brain/glossary");
export const addGlossaryTerm = (term, definition) =>
  postJSON("/api/company-brain/glossary", { term, definition });
export const updateGlossaryTerm = (id, term, definition) =>
  postJSON("/api/company-brain/glossary/update", { id, term, definition });
export const deleteGlossaryTerm = (id) => postJSON("/api/company-brain/glossary/delete", { id });
export const getBrainPreferences = () => getJSON("/api/company-brain/preferences");
export const addBrainPreference = (instruction) =>
  postJSON("/api/company-brain/preferences", { instruction });
export const toggleBrainPreference = (id, active) =>
  postJSON("/api/company-brain/preferences/toggle", { id, active });
export const deleteBrainPreference = (id) =>
  postJSON("/api/company-brain/preferences/delete", { id });
export const getBrainBranding = () => getJSON("/api/company-brain/branding");
export const saveBrainBranding = (formData) => postForm("/api/company-brain/branding", formData);
export const estimateBrainImport = (formData) =>
  postForm("/api/company-brain/import/estimate", formData);
export const startBrainImport = (formData) => postForm("/api/company-brain/import", formData);
export const getBrainImport = (jobId) =>
  getJSON(`/api/company-brain/import/${encodeURIComponent(jobId)}`);

// ---- 定时任务 (scheduled jobs) ---------------------------------------------------
export const getScheduledJobs = () => getJSON("/api/scheduled-jobs");
export const createScheduledJob = (payload) => postJSON("/api/scheduled-jobs", payload);
export const updateScheduledJob = (payload) => postJSON("/api/scheduled-jobs/update", payload);
export const deleteScheduledJob = (id) => postJSON("/api/scheduled-jobs/delete", { id });

// ---- 送达渠道 + 数据收件箱 (Phase 5 / B5) ------------------------------------------
export const deliveryTest = (channel) => postJSON("/api/delivery/test", { channel });
export const getUploadsLatest = () => getJSON("/api/uploads/latest");
export const modifyUploads = (formData) => postForm("/api/uploads", formData);
export const removeUploads = (names) => postJSON("/api/uploads", { remove: names });

// ---- onboarding wizard -----------------------------------------------------------
export const onboardingState = () => getJSON("/api/onboarding/state");
export const onboardingStart = () => postJSON("/api/onboarding/start");
export const onboardingSkip = (step) => postJSON("/api/onboarding/skip", { step });
export const onboardingCompany = (payload) => postJSON("/api/onboarding/company", payload);
export const onboardingUpload = (formData) => postForm("/api/onboarding/upload", formData);
export const onboardingDemoData = () => postJSON("/api/onboarding/demo-data");
export const onboardingKpiConfirm = (kpiMappings) =>
  postJSON("/api/onboarding/kpi-confirm", { kpi_mappings: kpiMappings });
export const onboardingGenerate = () => postJSON("/api/onboarding/generate");
export const onboardingSchedule = (channels) => postJSON("/api/onboarding/schedule", { channels });

// ---- support ----------------------------------------------------------------------
export const sendSupport = (payload) => postJSON("/api/support", payload);
