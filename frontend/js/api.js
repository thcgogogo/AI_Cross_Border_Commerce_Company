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

// ---- boot / catalog ---------------------------------------------------------
export const getMeta = () => getJSON("/api/meta");
export const getMe = () => getJSON("/api/me");
export const getAgents = () => getJSON("/api/agents");
export const getModels = () => getJSON("/api/models");
export const getProviders = () => getJSON("/api/providers");
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

// ---- devices + agent marketplace (Phase 11 — locally-deployable agents) ------
// Paired desktop daemons. The pairing CODE is shown ONCE (10-min, single-use); the
// daemon redeems it via /api/devices/exchange (that call lives IN the daemon, not
// here, since the web app never holds a device token). List is field-stripped.
export const getDevices = () => getJSON("/api/devices");
export const createPairingCode = () => postJSON("/api/devices/pair-code");
export const revokeDevice = (deviceId) =>
  postJSON(`/api/devices/${encodeURIComponent(deviceId)}/revoke`);
// Add/remove an agent from the account's desktop catalog (active:false = soft uninstall).
export const installAgent = (agentId, active = true) =>
  postJSON(`/api/agents/${encodeURIComponent(agentId)}/install`, { active });

// ---- reports / feedback / learning loop --------------------------------------
export const getReports = () => getJSON("/api/reports");
export const getReport = (runId) => getJSON(`/api/reports/${encodeURIComponent(runId)}`);
export const sendFeedback = (payload) => postJSON("/api/feedback", payload);
export const saveFeedbackPreference = (id) => postJSON("/api/feedback/save-preference", { id });
export const confirmGlossaryTerms = (answers) => postJSON("/api/glossary/confirm", { answers });

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
export const fusionChat = (payload) => postJSON("/api/fusion/chat", payload);

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
